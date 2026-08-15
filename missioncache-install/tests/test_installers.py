"""Tests for missioncache_install.installers - consent flow and filesystem behavior.

These tests focus on the pure-logic pieces of the installers (consent prompts,
symlink/copy helpers, uninstall preservation rules). The subprocess-heavy pieces
(pipx install, claude plugins install) are not exercised here - they require
real CLI tools and are covered by the end-to-end clean-VM verification in M10.6.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from missioncache_install import installers, settings, state
from missioncache_install import subprocess_utils


def _make_ctx(
    mode: str = "pypi",
    *,
    repo_root: Path | None = None,
    assume_yes: bool = False,
) -> installers.InstallContext:
    return installers.InstallContext(
        mode=mode,  # type: ignore[arg-type]
        repo_root=repo_root,
        skip_service=True,
        port=8787,
        assume_yes=assume_yes,
    )


# ---------------------------------------------------------------------------
# _symlink_md_dir
# ---------------------------------------------------------------------------

def test_symlink_md_dir_creates_links_for_md_files(tmp_path: Path) -> None:
    """Every *.md in src gets a symlink in dst; non-md files are skipped."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# a")
    (src / "b.md").write_text("# b")
    (src / "ignore.txt").write_text("not a rule")

    dst = tmp_path / "dst"
    dst.mkdir()

    installers._symlink_md_dir(src, dst)

    assert (dst / "a.md").is_symlink(), "a.md should be symlinked"
    assert (dst / "a.md").readlink() == src / "a.md"
    assert (dst / "b.md").is_symlink(), "b.md should be symlinked"
    assert not (dst / "ignore.txt").exists(), \
        "Non-md files in src must not be touched in dst"


def test_symlink_md_dir_backs_up_existing_regular_file(tmp_path: Path) -> None:
    """An existing regular file at the destination is preserved as .bak."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "rule.md").write_text("new content")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").write_text("user's original content")

    installers._symlink_md_dir(src, dst)

    assert (dst / "rule.md").is_symlink(), \
        "Destination should be replaced with a symlink"
    assert (dst / "rule.md.bak").read_text() == "user's original content", \
        "Original content must be preserved at .bak"


def test_symlink_md_dir_idempotent_when_already_linked(tmp_path: Path) -> None:
    """Re-running with correct symlinks in place is a no-op."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "rule.md").write_text("# rule")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(src / "rule.md")

    installers._symlink_md_dir(src, dst)  # should not raise

    assert (dst / "rule.md").is_symlink()
    assert (dst / "rule.md").readlink() == src / "rule.md"
    assert not (dst / "rule.md.bak").exists(), \
        "Idempotent re-run should not create a redundant .bak"


def test_symlink_md_dir_replaces_stale_symlink(tmp_path: Path) -> None:
    """A symlink pointing at a different target gets updated to the new source."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "rule.md").write_text("# rule")
    stale_target = tmp_path / "old-location" / "rule.md"
    stale_target.parent.mkdir()
    stale_target.write_text("# old")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(stale_target)

    installers._symlink_md_dir(src, dst)

    assert (dst / "rule.md").readlink() == src / "rule.md", \
        "Stale symlink should be updated to the new source"


# ---------------------------------------------------------------------------
# _copy_bundled_dir - mocked resources.files
# ---------------------------------------------------------------------------

# Captured at import time - the conftest autouse fixture rebinds the module
# attribute to a no-op, but this reference still points at the real function.
_REAL_WARM = installers._warm_hook_interpreter


class TestServiceKind:
    """_service_kind labels the dashboard service mechanism into install state,
    which the uninstall path reads back."""

    def test_skip_is_none(self):
        assert installers._service_kind(True) == "none"

    def test_win32_is_schtasks(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "win32")
        assert installers._service_kind(False) == "schtasks"

    def test_darwin_is_launchd(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "darwin")
        assert installers._service_kind(False) == "launchd"

    def test_linux_is_systemd(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "linux")
        assert installers._service_kind(False) == "systemd"


class TestHooksJsonShape:
    """The plugin's hooks.json is the whole native-Windows story, and the
    Windows CI job runs pytest, not Claude Code hooks - so a typo'd script name
    or a launcher that drifts from the warm would ship silently. Pin the shape
    here (the install suite is the only one with the repo tree in reach)."""

    def _hooks_json(self):
        import json

        # tests/ -> missioncache-install/ -> repo root -> hooks/hooks.json
        repo_root = Path(__file__).resolve().parents[2]
        return json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def _command_entries(self):
        data = self._hooks_json()
        for event_groups in data["hooks"].values():
            for group in event_groups:
                for entry in group["hooks"]:
                    yield entry

    def test_every_hook_is_exec_form_uv(self):
        entries = list(self._command_entries())
        assert entries, "no hook entries found"
        for entry in entries:
            assert entry["type"] == "command"
            assert entry["command"] == "uv"
            assert isinstance(entry["args"], list)

    def test_launcher_args_match_the_warm_prefix(self):
        """Each hook's args start with the same uv prefix _warm_hook_interpreter
        uses, so raising the >=3.11 floor cannot silently desync the two."""
        for entry in self._command_entries():
            assert entry["args"][: len(installers.HOOK_UV_ARGS)] == installers.HOOK_UV_ARGS

    def test_every_hook_script_exists(self):
        repo_root = Path(__file__).resolve().parents[2]
        for entry in self._command_entries():
            script = entry["args"][-1].replace("${CLAUDE_PLUGIN_ROOT}", str(repo_root))
            assert Path(script).is_file(), f"hook script missing: {entry['args'][-1]}"


class TestWarmHookInterpreter:
    """The pre-warm that keeps a first-run uv Python download from racing the
    5s UserPromptSubmit timeout. conftest stubs it autouse; these drive the real."""

    def test_missing_uv_warns_and_returns(self, monkeypatch):
        monkeypatch.setattr(installers.shutil, "which", lambda _n: None)
        warns = []
        monkeypatch.setattr(installers.ui, "warn", lambda m, **k: warns.append(m))
        ran = []
        monkeypatch.setattr(installers.subprocess_utils, "run_streaming",
                            lambda *a, **k: ran.append(a))
        _REAL_WARM()
        assert not ran  # never spawns when uv is absent
        assert any("uv not found" in w for w in warns)

    def test_success_streams_and_reports(self, monkeypatch):
        monkeypatch.setattr(installers.shutil, "which", lambda _n: "/usr/bin/uv")
        cmds = []
        monkeypatch.setattr(installers.subprocess_utils, "run_streaming",
                            lambda cmd, **k: cmds.append(cmd) or 0)
        monkeypatch.setattr(installers.ui, "detail", lambda *a, **k: None)
        _REAL_WARM()
        assert cmds == [["uv", *installers.HOOK_UV_ARGS, "-V"]]

    def test_failed_warm_does_not_raise(self, monkeypatch):
        """A warm failure must never fail the plugin install."""
        monkeypatch.setattr(installers.shutil, "which", lambda _n: "/usr/bin/uv")

        def boom(cmd, **k):
            raise installers.subprocess_utils.CommandFailed(cmd, 1, "", "boom")

        monkeypatch.setattr(installers.subprocess_utils, "run_streaming", boom)
        monkeypatch.setattr(installers.ui, "warn", lambda *a, **k: None)
        monkeypatch.setattr(installers.ui, "detail", lambda *a, **k: None)
        _REAL_WARM()  # no raise


class TestIsOurStatusline:
    def test_bare_name(self):
        assert installers._is_our_statusline("missioncache-statusline")

    def test_windows_absolute_path(self):
        assert installers._is_our_statusline("C:/Users/jane/Scripts/missioncache-statusline.exe")

    def test_quoted_spaced_path(self):
        assert installers._is_our_statusline('"C:/Users/Jane Doe/Scripts/missioncache-statusline.exe"')

    def test_user_wrapper_mentioning_the_name_is_not_ours(self):
        """The substring form would wrongly claim this and overwrite it."""
        assert not installers._is_our_statusline("my-status --fallback missioncache-statusline")

    def test_non_string_is_not_ours(self):
        assert not installers._is_our_statusline({"cmd": "x"})
        assert not installers._is_our_statusline(None)


def _raise_winerror_1314(self, target):
    """A symlink_to stand-in that raises the no-Developer-Mode OSError."""
    err = OSError("privilege not held")
    err.winerror = 1314  # type: ignore[attr-defined]
    raise err


class TestSymlinkOrCopyFallback:
    """The WinError 1314 (no Developer Mode) symlink->copy fallback. Reachable
    on POSIX by making symlink_to raise a 1314 OSError."""

    def test_symlink_success_returns_true(self, tmp_path):
        src = tmp_path / "src.md"
        src.write_text("x", encoding="utf-8")
        assert installers._symlink_or_copy(tmp_path / "dst.md", src) is True

    def test_file_fallback_copies_and_returns_false(self, tmp_path, monkeypatch):
        src = tmp_path / "src.md"
        src.write_text("content", encoding="utf-8")
        dst = tmp_path / "dst.md"
        monkeypatch.setattr(Path, "symlink_to", _raise_winerror_1314)
        monkeypatch.setattr(installers.ui, "warn", lambda *a, **k: None)
        assert installers._symlink_or_copy(dst, src) is False
        assert dst.read_text(encoding="utf-8") == "content"

    def test_dir_fallback_copies_with_ignore_patterns(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.md").write_text("k", encoding="utf-8")
        (src / ".env").write_text("SECRET=1", encoding="utf-8")
        (src / "local.db").write_text("db", encoding="utf-8")
        dst = tmp_path / "dst"

        def raise_1314(self, target):
            err = OSError("privilege not held")
            err.winerror = 1314  # type: ignore[attr-defined]
            raise err

        monkeypatch.setattr(Path, "symlink_to", raise_1314)
        monkeypatch.setattr(installers.ui, "warn", lambda *a, **k: None)
        assert installers._symlink_or_copy(dst, src) is False
        assert (dst / "keep.md").exists()
        assert not (dst / ".env").exists()  # secret excluded
        assert not (dst / "local.db").exists()  # local DB excluded

    def test_non_1314_oserror_reraises(self, tmp_path, monkeypatch):
        src = tmp_path / "src.md"
        src.write_text("x", encoding="utf-8")

        def raise_other(self, target):
            err = OSError("something else")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err

        monkeypatch.setattr(Path, "symlink_to", raise_other)
        with pytest.raises(OSError):
            installers._symlink_or_copy(tmp_path / "dst.md", src)


class TestStatuslineCommand:
    """The statusLine command written to settings.json on win32."""

    def test_posix_uses_bare_entry_point(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "darwin")
        assert installers._statusline_command() == "missioncache-statusline"

    def test_win32_writes_forward_slash_path(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(
            installers.shutil, "which",
            lambda _n: r"C:\Users\jane\Scripts\missioncache-statusline.exe",
        )
        assert installers._statusline_command() == "C:/Users/jane/Scripts/missioncache-statusline.exe"

    def test_win32_quotes_a_spaced_path(self, monkeypatch):
        """An unquoted space splits the command in every shell the statusline
        can run under - the quoted form at least works under Git Bash."""
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(
            installers.shutil, "which",
            lambda _n: r"C:\Users\Jane Doe\Scripts\missioncache-statusline.exe",
        )
        assert installers._statusline_command() == '"C:/Users/Jane Doe/Scripts/missioncache-statusline.exe"'

    def test_win32_falls_back_to_bare_name_when_unresolvable(self, monkeypatch):
        monkeypatch.setattr(installers.sys, "platform", "win32")
        monkeypatch.setattr(installers.shutil, "which", lambda _n: None)
        assert installers._statusline_command() == "missioncache-statusline"


class _FakeTraversable:
    """Minimal stand-in for importlib.resources Traversable, backed by Path."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.name = path.name

    def iterdir(self) -> list[_FakeTraversable]:
        return [_FakeTraversable(p) for p in self._path.iterdir()]

    def read_text(self, encoding: str | None = None) -> str:
        return self._path.read_text(encoding=encoding)


def test_copy_bundled_dir_copies_md_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_copy_bundled_dir copies every *.md out of the bundled package."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "one.md").write_text("# one")
    (bundled / "two.md").write_text("# two")
    (bundled / "skip.txt").write_text("not md")

    monkeypatch.setattr(
        installers.resources, "files", lambda _pkg: _FakeTraversable(bundled)
    )

    dst = tmp_path / "dst"
    dst.mkdir()

    installers._copy_bundled_dir(
        "missioncache_install.bundled.rules", dst, ownership="marker"
    )

    assert (dst / "one.md").read_text() == "# one"
    assert (dst / "two.md").read_text() == "# two"
    assert not (dst / "skip.txt").exists(), \
        "Only *.md files should be copied"


MARKED = f"<!-- {installers.MANAGED_MARKER} -->\n# rule body v1"
MARKED_V2 = f"<!-- {installers.MANAGED_MARKER} -->\n# rule body v2"


def test_install_md_file_marker_refreshes_managed_file(tmp_path: Path) -> None:
    """A file carrying the managed marker on line 1 is MissionCache's and is
    refreshed in place - no .bak churn for our own file."""
    dst = tmp_path
    (dst / "rule.md").write_text(MARKED)

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert (dst / "rule.md").read_text() == MARKED_V2
    assert not (dst / "rule.md.bak").exists(), \
        "Refreshing a managed file must not create a backup"


def test_install_md_file_marker_never_touches_unmarked_file(tmp_path: Path) -> None:
    """A file WITHOUT the marker is user-owned (the marker text says removing
    it means taking ownership): the installer must not overwrite or move it."""
    dst = tmp_path
    (dst / "rule.md").write_text("# my own rule, marker removed")

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert (dst / "rule.md").read_text() == "# my own rule, marker removed", \
        "User-owned file must be left byte-identical"
    assert not (dst / "rule.md.bak").exists(), \
        "User-owned file must not be renamed to .bak"


def test_install_md_file_equal_content_is_noop(tmp_path: Path) -> None:
    """Identical content means nothing to do - no rewrite, no backup."""
    dst = tmp_path
    (dst / "cmd.md").write_text("same content")

    installers._install_md_file("cmd.md", "same content", dst, ownership="filename")

    assert (dst / "cmd.md").read_text() == "same content"
    assert not (dst / "cmd.md.bak").exists(), \
        "Equal content must not produce a backup"


def test_install_md_file_filename_backs_up_existing_once(tmp_path: Path) -> None:
    """filename ownership: a different existing file is backed up to .bak."""
    dst = tmp_path
    (dst / "cmd.md").write_text("user's version")

    installers._install_md_file("cmd.md", "bundled v1", dst, ownership="filename")

    assert (dst / "cmd.md").read_text() == "bundled v1"
    assert (dst / "cmd.md.bak").read_text() == "user's version"


def test_install_md_file_second_run_preserves_first_backup(tmp_path: Path) -> None:
    """The first .bak is the user's original; a later run (an update shipping
    new content) must never overwrite it with MissionCache's own previous
    version."""
    dst = tmp_path
    (dst / "cmd.md").write_text("user's original")

    installers._install_md_file("cmd.md", "bundled v1", dst, ownership="filename")
    installers._install_md_file("cmd.md", "bundled v2", dst, ownership="filename")

    assert (dst / "cmd.md").read_text() == "bundled v2"
    assert (dst / "cmd.md.bak").read_text() == "user's original", \
        "Run two must not replace the user's backup with bundled v1"


def test_install_md_file_marker_replaces_symlink_to_managed_content(
    tmp_path: Path,
) -> None:
    """A symlink whose target carries the managed marker (a local-mode
    leftover) is replaced by a real file; the target file survives."""
    target = tmp_path / "repo" / "rule.md"
    target.parent.mkdir()
    target.write_text(MARKED)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(target)

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert not (dst / "rule.md").is_symlink()
    assert (dst / "rule.md").read_text() == MARKED_V2
    assert target.read_text() == MARKED, \
        "Replacing the link must not modify the linked target"


def test_install_md_file_marker_preserves_user_owned_symlink(
    tmp_path: Path,
) -> None:
    """A symlink whose target lacks the marker is user config (e.g. a link
    into a dotfiles repo) - the link must survive, same as an unmarked
    regular file."""
    target = tmp_path / "dotfiles" / "rule.md"
    target.parent.mkdir()
    target.write_text("# my own rule wired via dotfiles")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(target)

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert (dst / "rule.md").is_symlink(), \
        "User-owned symlink wiring must survive the install"
    assert target.read_text() == "# my own rule wired via dotfiles"


def test_install_md_file_marker_leaves_equal_content_symlink_in_place(
    tmp_path: Path,
) -> None:
    """A symlink whose target already equals the new content stays a symlink
    (the maintainer local-mode case where the clone is current)."""
    target = tmp_path / "repo" / "rule.md"
    target.parent.mkdir()
    target.write_text(MARKED_V2)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(target)

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert (dst / "rule.md").is_symlink()


def test_install_md_file_marker_replaces_dangling_symlink(tmp_path: Path) -> None:
    """A dangling symlink is not functioning config - it gets replaced."""
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").symlink_to(tmp_path / "gone" / "rule.md")

    installers._install_md_file("rule.md", MARKED_V2, dst, ownership="marker")

    assert not (dst / "rule.md").is_symlink()
    assert (dst / "rule.md").read_text() == MARKED_V2


def test_install_md_file_filename_replaces_symlink(tmp_path: Path) -> None:
    """filename ownership owns the name outright: any symlink is replaced by
    a real file; the target survives."""
    target = tmp_path / "elsewhere" / "cmd.md"
    target.parent.mkdir()
    target.write_text("linked content")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "cmd.md").symlink_to(target)

    installers._install_md_file("cmd.md", "bundled v1", dst, ownership="filename")

    assert not (dst / "cmd.md").is_symlink()
    assert (dst / "cmd.md").read_text() == "bundled v1"
    assert target.read_text() == "linked content", \
        "Replacing the link must not modify the linked target"


# ---------------------------------------------------------------------------
# install_statusline - consent flow
# ---------------------------------------------------------------------------

def _write_existing_statusline(command: str) -> None:
    settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SETTINGS_FILE.write_text(json.dumps({
        "statusLine": {"type": "command", "command": command}
    }))


def test_install_statusline_declines_overwrite_preserves_existing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user declines, the existing non-orbit statusLine is untouched."""
    _write_existing_statusline("my-custom-statusline")
    monkeypatch.setattr("missioncache_install.ui.ask_yn", lambda *a, **k: False)

    result = installers.install_statusline(_make_ctx())

    assert result is False, "Declining should return False"
    preserved = json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"]
    assert preserved == "my-custom-statusline", \
        "User's original statusline must be preserved when they decline"
    assert "statusline" not in state.load().get("components", {}), \
        "Declined install must not be recorded in state"


def test_install_statusline_accepts_overwrite_creates_backup(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting the overwrite writes missioncache-statusline and backs up the original."""
    _write_existing_statusline("my-custom-statusline")
    monkeypatch.setattr("missioncache_install.ui.ask_yn", lambda *a, **k: True)

    result = installers.install_statusline(_make_ctx())

    assert result is True
    assert json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"] \
        == "missioncache-statusline"
    bak = settings.SETTINGS_FILE.with_suffix(".json.bak")
    assert bak.exists(), "Backup file must be written"


def test_install_statusline_no_existing_skips_prompt(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no existing statusLine, the installer writes directly with no prompt."""
    prompts: list[Any] = []

    def track(*a: Any, **k: Any) -> bool:
        prompts.append(a)
        return True

    monkeypatch.setattr("missioncache_install.ui.ask_yn", track)

    result = installers.install_statusline(_make_ctx())

    assert result is True
    assert prompts == [], \
        "Fresh install should not prompt - nothing to overwrite"


def test_install_statusline_assume_yes_skips_prompt_even_with_conflict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes bypasses the overwrite confirmation (for CI and scripted installs)."""
    _write_existing_statusline("my-other")
    prompts: list[Any] = []
    monkeypatch.setattr(
        "missioncache_install.ui.ask_yn",
        lambda *a, **k: prompts.append(a) or False,
    )

    result = installers.install_statusline(_make_ctx(assume_yes=True))

    assert result is True, "assume_yes should allow the overwrite to proceed"
    assert prompts == [], "No prompt must fire when assume_yes=True"


# ---------------------------------------------------------------------------
# Uninstall preservation rules
# ---------------------------------------------------------------------------

def test_uninstall_user_commands_only_removes_known_files(
    isolated_home: Path,
) -> None:
    """Only whats-new.md and optimize-prompt.md are removed; user files stay."""
    cmds = isolated_home / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "whats-new.md").write_text("orbit")
    (cmds / "optimize-prompt.md").write_text("orbit")
    (cmds / "my-custom.md").write_text("user")

    installers.uninstall_user_commands(_make_ctx())

    assert not (cmds / "whats-new.md").exists(), "whats-new.md should be removed"
    assert not (cmds / "optimize-prompt.md").exists(), "optimize-prompt.md should be removed"
    assert (cmds / "my-custom.md").read_text() == "user", \
        "User-owned slash commands must never be touched"


def test_uninstall_rules_preserves_files_without_marker(
    isolated_home: Path,
) -> None:
    """Rules without the `missioncache-plugin:managed` marker are user-owned."""
    rules_dir = isolated_home / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "managed.md").write_text(
        "<!-- missioncache-plugin:managed -->\n# orbit content\n"
    )
    (rules_dir / "user-rule.md").write_text("# my own rule, no marker\n")

    installers.uninstall_rules(_make_ctx())

    assert not (rules_dir / "managed.md").exists(), \
        "Files with the orbit-managed marker should be removed"
    assert (rules_dir / "user-rule.md").exists(), \
        "User-owned rule files (no marker) must be preserved"


def test_uninstall_rules_removes_symlinks_pointing_at_repo(
    isolated_home: Path, tmp_path: Path
) -> None:
    """Symlinks that point at a repo rules/ dir are missioncache-installed and removable."""
    repo_rules = tmp_path / "repo" / "rules"
    repo_rules.mkdir(parents=True)
    src = repo_rules / "managed.md"
    src.write_text("# rule")

    rules_dir = isolated_home / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "managed.md").symlink_to(src)

    installers.uninstall_rules(_make_ctx())

    assert not (rules_dir / "managed.md").exists(), \
        "Symlink to repo rules should be removed"


def test_uninstall_preserves_user_data_directory(isolated_home: Path) -> None:
    """Uninstalling components must never touch ~/.missioncache/ (project data)."""
    orbit_data = isolated_home / ".missioncache" / "active" / "sample"
    orbit_data.mkdir(parents=True)
    (orbit_data / "sample-context.md").write_text("project state")

    ctx = _make_ctx()
    installers.uninstall_rules(ctx)
    installers.uninstall_user_commands(ctx)
    installers.uninstall_statusline(ctx)

    assert (orbit_data / "sample-context.md").read_text() == "project state", \
        "User project data in ~/.missioncache/ must survive an uninstall"


# ---------------------------------------------------------------------------
# pipx dist-name literals - rename tripwires
# ---------------------------------------------------------------------------
#
# install_dashboard / install_missioncache_auto / install_missioncache_db call
# _pipx_install(<dist-name>) in pypi mode. The dist-name literal is the
# string that goes to PyPI; a botched mechanical rename here (e.g.
# "missioncache-dashboard" silently rewritten to "missioncache-dashboard" before
# the PyPI package is republished) would survive every other gate. These
# tests pin the EXACT literal each installer passes.

@pytest.mark.parametrize(
    "installer_name, expected_dist",
    [
        ("install_dashboard", "missioncache-dashboard"),
        ("install_missioncache_auto", "missioncache-auto"),
        ("install_missioncache_db", "missioncache-db"),
    ],
)
def test_pypi_installer_passes_exact_dist_name(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_name: str,
    expected_dist: str,
) -> None:
    """In pypi mode, each installer must pass its exact PyPI dist-name literal.

    Rename tripwire: if the source rename sweep changes the literal at the
    call-site without updating these tests, the parametrize id reveals the
    exact installer that drifted.
    """
    captured: list[str] = []

    def fake_pipx_install(package: str) -> None:
        captured.append(package)

    monkeypatch.setattr(installers, "_pipx_install", fake_pipx_install)
    # Neutralize the side-effects that follow _pipx_install in install_dashboard
    # so the test exercises the install path without trying to actually find
    # the entry-point binary on PATH.
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installer = getattr(installers, installer_name)
    installer(_make_ctx(mode="pypi"))

    assert captured == [expected_dist], (
        f"{installer_name} must call _pipx_install exactly once with "
        f"the literal {expected_dist!r}, got {captured!r}"
    )


def test_install_dashboard_records_state_with_pypi_dist_path(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_dashboard records the dashboard component in state after pipx install."""
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_install", lambda pkg: captured.append(pkg)
    )
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.install_dashboard(_make_ctx(mode="pypi"))

    assert captured == ["missioncache-dashboard"]
    components = state.load().get("components", {})
    assert "dashboard" in components, \
        "install_dashboard must record the dashboard component in state"
    assert components["dashboard"]["mode"] == "pypi"


def test_install_missioncache_auto_records_state_under_missioncache_auto_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_missioncache_auto records under the `missioncache_auto` state key (rename tripwire)."""
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_install", lambda pkg: captured.append(pkg)
    )
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.install_missioncache_auto(_make_ctx(mode="pypi"))

    assert captured == ["missioncache-auto"]
    components = state.load().get("components", {})
    assert "missioncache_auto" in components, \
        "install_missioncache_auto must record state under the literal key 'missioncache_auto'"


def test_install_missioncache_db_records_state_under_missioncache_db_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_missioncache_db records under the `missioncache_db` state key (rename tripwire)."""
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_install", lambda pkg: captured.append(pkg)
    )
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.install_missioncache_db(_make_ctx(mode="pypi"))

    assert captured == ["missioncache-db"]
    components = state.load().get("components", {})
    assert "missioncache_db" in components, \
        "install_missioncache_db must record state under the literal key 'missioncache_db'"


@pytest.mark.parametrize(
    "installer_name, expected_dist",
    [
        ("uninstall_dashboard", "missioncache-dashboard"),
        ("uninstall_missioncache_auto", "missioncache-auto"),
        ("uninstall_missioncache_db", "missioncache-db"),
    ],
)
def test_pypi_uninstaller_passes_exact_dist_name(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_name: str,
    expected_dist: str,
) -> None:
    """In pypi mode, each uninstaller must pass the same exact dist-name literal.

    The PyPI dist-name on install MUST equal the dist-name on uninstall - any
    drift between the two strands the user with an orphaned pipx package.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_uninstall", lambda pkg: captured.append(pkg)
    )
    # Avoid spawning `missioncache-dashboard uninstall-service` for the dashboard
    # uninstall path.
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    uninstaller = getattr(installers, installer_name)
    uninstaller(_make_ctx(mode="pypi"))

    assert captured == [expected_dist], (
        f"{installer_name} must call _pipx_uninstall exactly once with "
        f"the literal {expected_dist!r}, got {captured!r}"
    )


def test_write_local_marketplace_json_idempotent(tmp_path: Path) -> None:
    """Re-running the local installer must not duplicate the plugin entry. The
    dedupe check keys on the entry `name`; if it looks for a different name than
    the one written, every re-run appends a duplicate and marketplace.json grows
    unbounded.
    """
    mp = tmp_path / ".claude-plugin" / "marketplace.json"
    mp.parent.mkdir(parents=True)

    installers._write_local_marketplace_json(mp)
    installers._write_local_marketplace_json(mp)

    names = [p["name"] for p in json.loads(mp.read_text())["plugins"]]
    assert names.count("missioncache") == 1, f"duplicate plugin entries: {names}"


def test_install_plugin_local_symlink_matches_marketplace_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin symlink directory name must equal the marketplace entry's
    `source` basename. If they diverge, Claude resolves the entry's source to a
    path with no symlink and the install is broken. This couples the two sides
    that a half-applied rename pulled apart (symlink at plugins/X, source
    ./plugins/Y).
    """
    mkt = tmp_path / "local-marketplace"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(installers, "MARKETPLACE_DIR", mkt)
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)
    monkeypatch.setattr(installers.settings, "enable_plugin", lambda *_a, **_k: None)

    installers._install_plugin_local(_make_ctx(mode="local", repo_root=repo))

    entry = json.loads(
        (mkt / ".claude-plugin" / "marketplace.json").read_text()
    )["plugins"][0]
    source_name = Path(entry["source"]).name
    symlink = mkt / "plugins" / source_name
    assert symlink.is_symlink(), (
        f"marketplace source is ./plugins/{source_name} but no symlink exists "
        f"there - install/marketplace plugin names diverged"
    )
    assert symlink.readlink() == repo


# ---------------------------------------------------------------------------
# Uninstall/update honor the RECORDED install mode, not the CWD-derived mode
# ---------------------------------------------------------------------------

def test_uninstall_dashboard_uses_recorded_pypi_mode_over_ctx(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pypi-installed dashboard is pipx-uninstalled even when uninstall runs
    from a clone (ctx.mode='local'). The recorded per-component mode wins."""
    state.record_component("dashboard", {"mode": "pypi", "service": "none", "port": 8787})
    captured: list[str] = []
    monkeypatch.setattr(installers, "_pipx_uninstall", lambda pkg: captured.append(pkg))
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.uninstall_dashboard(_make_ctx(mode="local"))

    assert captured == ["missioncache-dashboard"], (
        "recorded pypi mode must drive the pipx uninstall despite ctx.mode='local'"
    )


def test_uninstall_dashboard_skips_pipx_for_recorded_local_mode(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable (local) install is never pipx-uninstalled even when uninstall
    runs from outside the clone (ctx.mode='pypi')."""
    state.record_component("dashboard", {"mode": "local", "service": "none", "port": 8787})
    captured: list[str] = []
    monkeypatch.setattr(installers, "_pipx_uninstall", lambda pkg: captured.append(pkg))
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.uninstall_dashboard(_make_ctx(mode="pypi"))

    assert captured == [], (
        "recorded local (editable) mode must not attempt a pipx uninstall "
        "despite ctx.mode='pypi'"
    )


def test_update_all_reinstalls_in_recorded_mode(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update run from a clone (ctx.mode='local') reinstalls a pypi install in
    pypi mode - the recorded install mode is authoritative, not the CWD."""
    state.set_mode("pypi")
    state.record_component("dashboard", {"mode": "pypi"})
    captured_modes: list[str] = []
    monkeypatch.setattr(
        installers,
        "install_components",
        lambda comps, ctx: captured_modes.append(ctx.mode) or [],
    )

    installers.update_all(_make_ctx(mode="local", repo_root=isolated_home))

    assert captured_modes == ["pypi"], (
        "update must reinstall in the recorded mode, not the CWD-derived mode"
    )


# ---------------------------------------------------------------------------
# install_components: per-component error isolation
# ---------------------------------------------------------------------------

def test_install_components_isolates_failure_and_continues(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CommandFailed in one installer is reported; later independent components
    still run. install_components returns the list of failed components."""
    calls: list[str] = []

    def make_ok(name: str):
        def _inst(ctx: installers.InstallContext) -> None:
            calls.append(name)
        return _inst

    def boom(ctx: installers.InstallContext) -> None:
        calls.append("dashboard")
        raise subprocess_utils.CommandFailed(["pipx", "install", "x"], 1, "", "boom")

    monkeypatch.setattr(
        installers,
        "_INSTALLERS",
        {"plugin": make_ok("plugin"), "dashboard": boom, "rules": make_ok("rules")},
    )

    failed = installers.install_components(["plugin", "dashboard", "rules"], _make_ctx())

    assert failed == ["dashboard"]
    assert calls == ["plugin", "dashboard", "rules"], (
        "components after the failed one must still be installed"
    )


def test_install_components_propagates_non_command_errors(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only CommandFailed is isolated; a real bug (any other exception) still
    surfaces so it is not silently swallowed."""
    def boom(ctx: installers.InstallContext) -> None:
        raise RuntimeError("programming bug")

    monkeypatch.setattr(installers, "_INSTALLERS", {"plugin": boom})

    with pytest.raises(RuntimeError):
        installers.install_components(["plugin"], _make_ctx())


# ---------------------------------------------------------------------------
# Plugin: enable/record only after the CLI install actually succeeds
# ---------------------------------------------------------------------------

def test_install_plugin_pypi_enables_only_after_successful_install(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings.enable_plugin must run AFTER `claude plugins install` succeeds."""
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(installers.shutil, "which", lambda _name: "/usr/bin/claude")

    def fake_run(cmd, **kwargs):
        events.append(("run", tuple(cmd)))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
    monkeypatch.setattr(
        installers.settings, "enable_plugin", lambda pid: events.append(("enable", pid))
    )

    installers._install_plugin_pypi()

    install_idx = next(
        i for i, e in enumerate(events) if e[0] == "run" and "install" in e[1]
    )
    enable_idx = next(i for i, e in enumerate(events) if e[0] == "enable")
    assert enable_idx > install_idx, "enable must not precede the plugins-install command"


def test_install_plugin_pypi_does_not_enable_on_failed_install(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `claude plugins install` leaves no phantom enabledPlugins entry."""
    monkeypatch.setattr(installers.shutil, "which", lambda _name: "/usr/bin/claude")
    enabled: list[str] = []

    def fake_run(cmd, **kwargs):
        if "install" in cmd:
            raise subprocess_utils.CommandFailed(list(cmd), 1, "", "no such package")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)
    monkeypatch.setattr(installers.settings, "enable_plugin", lambda pid: enabled.append(pid))

    with pytest.raises(subprocess_utils.CommandFailed):
        installers._install_plugin_pypi()

    assert enabled == [], "enable_plugin must not run when the install command fails"


def test_install_plugin_local_failure_skips_enable_and_state(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing local `claude plugins install` must not enable the plugin, must
    not record it in state, and must not report success (propagates instead)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(installers, "MARKETPLACE_DIR", tmp_path / "mkt")
    monkeypatch.setattr(installers.shutil, "which", lambda _name: "/usr/bin/claude")
    enabled: list[str] = []
    monkeypatch.setattr(installers.settings, "enable_plugin", lambda pid: enabled.append(pid))

    def fake_run(cmd, **kwargs):
        raise subprocess_utils.CommandFailed(list(cmd), 1, "", "boom")

    monkeypatch.setattr(installers.subprocess_utils, "run", fake_run)

    with pytest.raises(subprocess_utils.CommandFailed):
        installers.install_plugin(_make_ctx(mode="local", repo_root=repo))

    assert enabled == [], "local plugin install failure must not enable the plugin"
    assert "plugin" not in state.load().get("components", {}), (
        "a failed plugin install must not be recorded as installed"
    )
