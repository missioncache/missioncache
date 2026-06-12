"""Tests for orbit_install.installers - consent flow and filesystem behavior.

These tests focus on the pure-logic pieces of the installers (consent prompts,
symlink/copy helpers, uninstall preservation rules). The subprocess-heavy pieces
(pipx install, claude plugins install) are not exercised here - they require
real CLI tools and are covered by the end-to-end clean-VM verification in M10.6.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orbit_install import installers, settings, state


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

class _FakeTraversable:
    """Minimal stand-in for importlib.resources Traversable, backed by Path."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.name = path.name

    def iterdir(self) -> list[_FakeTraversable]:
        return [_FakeTraversable(p) for p in self._path.iterdir()]

    def read_text(self) -> str:
        return self._path.read_text()


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

    installers._copy_bundled_dir("orbit_install.bundled.rules", dst)

    assert (dst / "one.md").read_text() == "# one"
    assert (dst / "two.md").read_text() == "# two"
    assert not (dst / "skip.txt").exists(), \
        "Only *.md files should be copied"


def test_copy_bundled_dir_backs_up_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing file at the destination is preserved as .bak."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "rule.md").write_text("bundled version")

    monkeypatch.setattr(
        installers.resources, "files", lambda _pkg: _FakeTraversable(bundled)
    )

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "rule.md").write_text("user's version")

    installers._copy_bundled_dir("orbit_install.bundled.rules", dst)

    assert (dst / "rule.md").read_text() == "bundled version"
    assert (dst / "rule.md.bak").read_text() == "user's version"


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
    monkeypatch.setattr("orbit_install.ui.ask_yn", lambda *a, **k: False)

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
    """Accepting the overwrite writes orbit-statusline and backs up the original."""
    _write_existing_statusline("my-custom-statusline")
    monkeypatch.setattr("orbit_install.ui.ask_yn", lambda *a, **k: True)

    result = installers.install_statusline(_make_ctx())

    assert result is True
    assert json.loads(settings.SETTINGS_FILE.read_text())["statusLine"]["command"] \
        == "orbit-statusline"
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

    monkeypatch.setattr("orbit_install.ui.ask_yn", track)

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
        "orbit_install.ui.ask_yn",
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
    """Rules without the `orbit-plugin:managed` marker are user-owned."""
    rules_dir = isolated_home / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "managed.md").write_text(
        "<!-- orbit-plugin:managed -->\n# orbit content\n"
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
    """Symlinks that point at a repo rules/ dir are orbit-installed and removable."""
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
    """Uninstalling components must never touch ~/.orbit/ (project data)."""
    orbit_data = isolated_home / ".orbit" / "active" / "sample"
    orbit_data.mkdir(parents=True)
    (orbit_data / "sample-context.md").write_text("project state")

    ctx = _make_ctx()
    installers.uninstall_rules(ctx)
    installers.uninstall_user_commands(ctx)
    installers.uninstall_statusline(ctx)

    assert (orbit_data / "sample-context.md").read_text() == "project state", \
        "User project data in ~/.orbit/ must survive an uninstall"


# ---------------------------------------------------------------------------
# pipx dist-name literals - rename tripwires
# ---------------------------------------------------------------------------
#
# install_dashboard / install_orbit_auto / install_orbit_db call
# _pipx_install(<dist-name>) in pypi mode. The dist-name literal is the
# string that goes to PyPI; a botched mechanical rename here (e.g.
# "orbit-dashboard" silently rewritten to "missioncache-dashboard" before
# the PyPI package is republished) would survive every other gate. These
# tests pin the EXACT literal each installer passes.

@pytest.mark.parametrize(
    "installer_name, expected_dist",
    [
        ("install_dashboard", "orbit-dashboard"),
        ("install_orbit_auto", "orbit-auto"),
        ("install_orbit_db", "orbit-db"),
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

    assert captured == ["orbit-dashboard"]
    components = state.load().get("components", {})
    assert "dashboard" in components, \
        "install_dashboard must record the dashboard component in state"
    assert components["dashboard"]["mode"] == "pypi"


def test_install_orbit_auto_records_state_under_orbit_auto_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_orbit_auto records under the `orbit_auto` state key (rename tripwire)."""
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_install", lambda pkg: captured.append(pkg)
    )
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.install_orbit_auto(_make_ctx(mode="pypi"))

    assert captured == ["orbit-auto"]
    components = state.load().get("components", {})
    assert "orbit_auto" in components, \
        "install_orbit_auto must record state under the literal key 'orbit_auto'"


def test_install_orbit_db_records_state_under_orbit_db_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_orbit_db records under the `orbit_db` state key (rename tripwire)."""
    captured: list[str] = []
    monkeypatch.setattr(
        installers, "_pipx_install", lambda pkg: captured.append(pkg)
    )
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    installers.install_orbit_db(_make_ctx(mode="pypi"))

    assert captured == ["orbit-db"]
    components = state.load().get("components", {})
    assert "orbit_db" in components, \
        "install_orbit_db must record state under the literal key 'orbit_db'"


@pytest.mark.parametrize(
    "installer_name, expected_dist",
    [
        ("uninstall_dashboard", "orbit-dashboard"),
        ("uninstall_orbit_auto", "orbit-auto"),
        ("uninstall_orbit_db", "orbit-db"),
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
    # Avoid spawning `orbit-dashboard uninstall-service` for the dashboard
    # uninstall path.
    monkeypatch.setattr(installers.shutil, "which", lambda _name: None)

    uninstaller = getattr(installers, installer_name)
    uninstaller(_make_ctx(mode="pypi"))

    assert captured == [expected_dist], (
        f"{installer_name} must call _pipx_uninstall exactly once with "
        f"the literal {expected_dist!r}, got {captured!r}"
    )
