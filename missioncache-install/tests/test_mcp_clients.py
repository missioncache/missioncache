"""Tests for missioncache_install.mcp_clients - per-tool MCP server registration.

Focus: idempotent JSON-merge correctness, preservation of unknown user keys,
and warn-and-skip when the corresponding tool is missing. Subprocess-driven
codex paths are exercised via a recording fake so we can assert exact CLI
calls without hitting a real Codex install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from missioncache_install import installers, mcp_clients, state


def _make_ctx() -> installers.InstallContext:
    """Minimal ctx for installers that don't read any of its fields."""
    return installers.InstallContext(
        mode="pypi",
        repo_root=None,
        skip_service=True,
        port=8787,
        assume_yes=True,
    )


# A canned CompletedProcess for monkeypatching subprocess_utils.run.
def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# _load_json_object: returns (data, indent)
# ---------------------------------------------------------------------------

def test_load_json_object_parses_valid_object(tmp_path: Path) -> None:
    """Standard happy path: parsed dict + indent + JSONC-fallback flag is False."""
    p = tmp_path / "x.json"
    p.write_text('{\n  "a": 1,\n  "b": "two"\n}')
    data, indent, used_jsonc = mcp_clients._load_json_object(p)
    assert data == {"a": 1, "b": "two"}
    assert indent == "  "
    assert used_jsonc is False


def test_load_json_object_missing_file_returns_default(tmp_path: Path) -> None:
    """Missing files yield ({}, '  ', False) so a fresh write uses 2-space."""
    data, indent, used_jsonc = mcp_clients._load_json_object(tmp_path / "absent.json")
    assert data == {}
    assert indent == "  "
    assert used_jsonc is False


def test_load_json_object_treats_empty_file_as_empty_dict(tmp_path: Path) -> None:
    """An empty / whitespace-only config file is parsed as `{}`."""
    p = tmp_path / "x.json"
    p.write_text("")
    assert mcp_clients._load_json_object(p) == ({}, "  ", False)

    p.write_text("   \n  \t  \n")
    assert mcp_clients._load_json_object(p) == ({}, "  ", False)


def test_load_json_object_rejects_non_object_root(tmp_path: Path) -> None:
    """Arrays and scalars at the root are user-data we will not silently overwrite."""
    p = tmp_path / "x.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(json.JSONDecodeError):
        mcp_clients._load_json_object(p)

    p.write_text('"just a string"')
    with pytest.raises(json.JSONDecodeError):
        mcp_clients._load_json_object(p)


# ---------------------------------------------------------------------------
# _load_json_object indent detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected_indent", [
    ('{\n\t"a": 1\n}', "\t"),
    ('{\n  "a": 1\n}', "  "),
    ('{\n    "a": 1\n}', "    "),
    ('{}', "  "),                                            # compact -> default
    ('{\n\t\t"deep": {\n\t\t\t"x": 1\n\t\t}\n}', "\t"),      # tabs at any depth
])
def test_load_json_object_detects_indent_style(
    tmp_path: Path, raw: str, expected_indent: str
) -> None:
    """First content line with leading whitespace decides the indent style."""
    p = tmp_path / "x.json"
    p.write_text(raw)
    _, indent, _ = mcp_clients._load_json_object(p)
    assert indent == expected_indent


# ---------------------------------------------------------------------------
# _load_json_object: JSONC fallback (comments + trailing commas)
# ---------------------------------------------------------------------------

def test_load_json_object_parses_jsonc_with_line_comments(tmp_path: Path) -> None:
    """OpenCode docs guarantee JSONC support; `// comment` must not break the parse.

    The third return value MUST be True so callers know not to auto-mutate-and-write.
    """
    p = tmp_path / "config.json"
    p.write_text(
        '// top-level comment\n'
        '{\n'
        '  // explain this key\n'
        '  "theme": "dark",\n'
        '  "model": "claude" // inline comment\n'
        '}\n'
    )
    data, _, used_jsonc = mcp_clients._load_json_object(p)
    assert data == {"theme": "dark", "model": "claude"}
    assert used_jsonc is True


def test_load_json_object_parses_jsonc_with_block_comments(tmp_path: Path) -> None:
    """`/* block */` comments are also valid JSONC and must parse cleanly."""
    p = tmp_path / "config.json"
    p.write_text('{\n  /* explain */\n  "theme": "dark"\n}\n')
    data, _, used_jsonc = mcp_clients._load_json_object(p)
    assert data == {"theme": "dark"}
    assert used_jsonc is True


def test_load_json_object_parses_jsonc_with_trailing_commas(tmp_path: Path) -> None:
    """Trailing commas are JSONC-valid and rejected by strict json.loads."""
    p = tmp_path / "config.json"
    p.write_text('{\n  "a": 1,\n  "b": 2,\n}\n')
    data, _, used_jsonc = mcp_clients._load_json_object(p)
    assert data == {"a": 1, "b": 2}
    assert used_jsonc is True


def test_load_json_object_raises_on_truly_malformed_input(tmp_path: Path) -> None:
    """Genuinely broken JSON (not just JSONC) must still raise so the caller warns."""
    p = tmp_path / "config.json"
    # Unclosed brace - no fallback can recover this.
    p.write_text('{"a": 1')
    with pytest.raises(json.JSONDecodeError):
        mcp_clients._load_json_object(p)


def test_load_json_object_raises_on_jsonc_non_object_root(tmp_path: Path) -> None:
    """JSONC array at root is parseable but still a config we won't overwrite."""
    p = tmp_path / "config.json"
    p.write_text('// comment\n[1, 2, 3]\n')
    with pytest.raises(json.JSONDecodeError):
        mcp_clients._load_json_object(p)


def test_load_json_object_strict_path_does_not_import_json5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict-JSON happy path must not trigger the json5 lazy import.

    Without this, every install pays the json5 import cost even when no
    config files use JSONC. Validates the lazy-import design.
    """
    monkeypatch.delitem(sys.modules, "json5", raising=False)

    p = tmp_path / "config.json"
    p.write_text('{"a": 1}')
    data, _, _ = mcp_clients._load_json_object(p)
    assert data == {"a": 1}
    assert "json5" not in sys.modules, "lazy import paid on strict-JSON happy path"


# ---------------------------------------------------------------------------
# OpenCode: install_opencode
# ---------------------------------------------------------------------------

def _set_opencode_detected(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    """Make `_opencode_detected()` return the requested truthy value."""
    monkeypatch.setattr(
        mcp_clients, "_opencode_detected", lambda: present
    )


def _set_mcp_missioncache_path_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend mcp-missioncache is already on PATH so the prereq check returns True."""
    monkeypatch.setattr(
        mcp_clients, "_ensure_mcp_missioncache_on_path", lambda: True
    )


def test_install_opencode_creates_entry_in_fresh_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-time install writes a minimal config with mcp.missioncache set."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.install_opencode(_make_ctx())

    data = json.loads(mcp_clients.OPENCODE_CONFIG_PATH.read_text())
    assert data == {"mcp": {"missioncache": {"type": "local", "command": ["mcp-missioncache"]}}}
    assert state.load()["components"]["opencode"]["path"] == str(
        mcp_clients.OPENCODE_CONFIG_PATH
    )


def test_install_opencode_preserves_schema_and_other_top_level_keys(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode auto-injects $schema; the merge must leave it (and friends) intact."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "theme": "tokyonight",
        "model": "claude-sonnet-4",
    }))

    mcp_clients.install_opencode(_make_ctx())

    data = json.loads(mcp_clients.OPENCODE_CONFIG_PATH.read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["theme"] == "tokyonight"
    assert data["model"] == "claude-sonnet-4"
    assert data["mcp"]["missioncache"] == {"type": "local", "command": ["mcp-missioncache"]}


def test_install_opencode_preserves_other_mcp_servers(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing entries under `mcp` must survive the merge."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(json.dumps({
        "mcp": {
            "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp"},
            "tavily": {"type": "local", "command": ["tavily-mcp"]},
        }
    }))

    mcp_clients.install_opencode(_make_ctx())

    data = json.loads(mcp_clients.OPENCODE_CONFIG_PATH.read_text())
    assert data["mcp"]["context7"]["url"] == "https://mcp.context7.com/mcp"
    assert data["mcp"]["tavily"]["command"] == ["tavily-mcp"]
    assert data["mcp"]["missioncache"]["command"] == ["mcp-missioncache"]


def test_install_opencode_idempotent_when_missioncache_already_set(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running with an already-correct entry should not rewrite the file."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    correct = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"missioncache": {"type": "local", "command": ["mcp-missioncache"]}},
    }
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(json.dumps(correct, indent=2))
    mtime_before = mcp_clients.OPENCODE_CONFIG_PATH.stat().st_mtime_ns

    mcp_clients.install_opencode(_make_ctx())

    assert mcp_clients.OPENCODE_CONFIG_PATH.stat().st_mtime_ns == mtime_before, (
        "Idempotent install must not touch a file that already has the correct entry"
    )
    assert state.load()["components"]["opencode"]["path"] == str(
        mcp_clients.OPENCODE_CONFIG_PATH
    )


def test_install_opencode_merges_and_preserves_user_added_keys(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's extra keys inside the missioncache entry survive an --update re-run.

    A user who edits `mcp.missioncache` to add keys the installer does not own
    (e.g. `"enabled": false` to temporarily disable it) must not have those keys
    dropped - nor the server silently re-enabled - when the installer updates
    its own owned keys. Only `type`/`command` are the installer's to set.
    """
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(
        json.dumps(
            {
                "mcp": {
                    "missioncache": {
                        "type": "local",
                        "command": ["stale-binary"],
                        "enabled": False,
                    }
                }
            },
            indent=2,
        )
    )

    mcp_clients.install_opencode(_make_ctx())

    entry = json.loads(mcp_clients.OPENCODE_CONFIG_PATH.read_text())["mcp"]["missioncache"]
    assert entry["command"] == ["mcp-missioncache"], "owned key must be refreshed"
    assert entry["enabled"] is False, (
        "user-added key must be preserved, not wiped by a wholesale overwrite"
    )


def test_install_opencode_preserves_tab_indent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tab-indented OpenCode config stays tab-indented after merge."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    # Tab-indented input - matches what some users / editors produce.
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(
        '{\n\t"$schema": "https://opencode.ai/config.json"\n}'
    )

    mcp_clients.install_opencode(_make_ctx())

    out = mcp_clients.OPENCODE_CONFIG_PATH.read_text()
    assert "\n\t" in out, (
        "Tab indent must be preserved through the merge - "
        f"got: {out!r}"
    )
    # 2-space indent must NOT have been introduced.
    assert "\n  \"" not in out, "2-space indent leaked into a tab-indented file"


def test_install_opencode_warns_and_skips_when_not_detected(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OpenCode CLI -> no config file written and no state recorded."""
    _set_opencode_detected(monkeypatch, False)

    mcp_clients.install_opencode(_make_ctx())

    assert not mcp_clients.OPENCODE_CONFIG_PATH.exists()
    assert "opencode" not in state.load().get("components", {})


# ---------------------------------------------------------------------------
# OpenCode: uninstall_opencode
# ---------------------------------------------------------------------------

def test_uninstall_opencode_removes_only_missioncache(
    isolated_home: Path,
) -> None:
    """Uninstall must drop only mcp.missioncache; other keys and other servers stay."""
    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "theme": "tokyonight",
        "mcp": {
            "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp"},
            "missioncache": {"type": "local", "command": ["mcp-missioncache"]},
        },
    }))
    state.record_component("opencode", {"path": str(mcp_clients.OPENCODE_CONFIG_PATH)})

    mcp_clients.uninstall_opencode(_make_ctx())

    data = json.loads(mcp_clients.OPENCODE_CONFIG_PATH.read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["theme"] == "tokyonight"
    assert data["mcp"]["context7"]["url"] == "https://mcp.context7.com/mcp"
    assert "missioncache" not in data["mcp"], "missioncache entry must be gone"
    assert "opencode" not in state.load().get("components", {})


def test_uninstall_opencode_no_op_when_config_missing(
    isolated_home: Path,
) -> None:
    """Missing config file is a clean no-op (don't create it just to remove missioncache)."""
    state.record_component("opencode", {"path": str(mcp_clients.OPENCODE_CONFIG_PATH)})

    mcp_clients.uninstall_opencode(_make_ctx())

    assert not mcp_clients.OPENCODE_CONFIG_PATH.exists()
    assert "opencode" not in state.load().get("components", {})


# ---------------------------------------------------------------------------
# VSCode: install_vscode
# ---------------------------------------------------------------------------

def _set_vscode_detected(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: present)


def test_install_vscode_skips_on_non_darwin(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 11.1 only ships VSCode for macOS; Linux/Windows must warn-and-skip."""
    monkeypatch.setattr(sys, "platform", "linux")

    mcp_clients.install_vscode(_make_ctx())

    assert not mcp_clients.VSCODE_USER_MCP_PATH.exists()
    assert "vscode" not in state.load().get("components", {})


def test_install_vscode_preserves_existing_servers(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real users have ~17 existing servers in mcp.json; merge must keep them all."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    existing = {
        "servers": {
            "github": {"type": "stdio", "command": "mcp-github"},
            "jira": {"type": "stdio", "command": "mcp-jira"},
            "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
        },
        "inputs": [{"id": "github_token", "type": "promptString"}],
    }
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(json.dumps(existing))

    mcp_clients.install_vscode(_make_ctx())

    data = json.loads(mcp_clients.VSCODE_USER_MCP_PATH.read_text())
    assert data["servers"]["github"]["command"] == "mcp-github"
    assert data["servers"]["jira"]["command"] == "mcp-jira"
    assert data["servers"]["context7"]["url"] == "https://mcp.context7.com/mcp"
    assert data["servers"]["missioncache"] == {"type": "stdio", "command": "mcp-missioncache"}
    assert data["inputs"] == [{"id": "github_token", "type": "promptString"}], (
        "Top-level keys other than `servers` must be preserved verbatim"
    )


def test_install_vscode_merges_and_preserves_user_added_keys(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's extra keys inside the VSCode missioncache entry survive a re-run."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(
        json.dumps(
            {
                "servers": {
                    "missioncache": {
                        "type": "stdio",
                        "command": "stale-binary",
                        "env": {"MISSIONCACHE_DEBUG": "1"},
                    }
                }
            }
        )
    )

    mcp_clients.install_vscode(_make_ctx())

    entry = json.loads(mcp_clients.VSCODE_USER_MCP_PATH.read_text())["servers"]["missioncache"]
    assert entry["command"] == "mcp-missioncache", "owned key must be refreshed"
    assert entry["env"] == {"MISSIONCACHE_DEBUG": "1"}, (
        "user-added key must be preserved, not wiped by a wholesale overwrite"
    )


def test_install_vscode_preserves_tab_indent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's existing tab-indented VSCode mcp.json stays tab-indented."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    # Tab-indented input matching VSCode's default editor output.
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(
        '{\n\t"servers": {\n\t\t"github": {\n\t\t\t"type": "stdio",\n'
        '\t\t\t"command": "mcp-github"\n\t\t}\n\t}\n}'
    )

    mcp_clients.install_vscode(_make_ctx())

    out = mcp_clients.VSCODE_USER_MCP_PATH.read_text()
    assert "\n\t" in out, "Tab indent must be preserved across the merge"
    # Round-trip parses cleanly and contains both old and new entries.
    data = json.loads(out)
    assert data["servers"]["github"]["command"] == "mcp-github"
    assert data["servers"]["missioncache"] == {"type": "stdio", "command": "mcp-missioncache"}


def test_uninstall_vscode_preserves_tab_indent(
    isolated_home: Path,
) -> None:
    """Tab indent survives a missioncache-only uninstall (we still rewrite the file)."""
    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(
        '{\n\t"servers": {\n\t\t"github": {\n\t\t\t"type": "stdio",\n'
        '\t\t\t"command": "mcp-github"\n\t\t},\n'
        '\t\t"missioncache": {\n\t\t\t"type": "stdio",\n\t\t\t"command": "mcp-missioncache"\n\t\t}\n'
        '\t}\n}'
    )
    state.record_component("vscode", {"path": str(mcp_clients.VSCODE_USER_MCP_PATH)})

    mcp_clients.uninstall_vscode(_make_ctx())

    out = mcp_clients.VSCODE_USER_MCP_PATH.read_text()
    assert "\n\t" in out
    assert "missioncache" not in json.loads(out)["servers"]


def test_install_vscode_idempotent_when_missioncache_already_set(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running with the entry already correct should not touch the file."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(json.dumps({
        "servers": {"missioncache": {"type": "stdio", "command": "mcp-missioncache"}}
    }, indent=2))
    mtime_before = mcp_clients.VSCODE_USER_MCP_PATH.stat().st_mtime_ns

    mcp_clients.install_vscode(_make_ctx())

    assert mcp_clients.VSCODE_USER_MCP_PATH.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# VSCode: uninstall_vscode
# ---------------------------------------------------------------------------

def test_uninstall_vscode_removes_only_missioncache(
    isolated_home: Path,
) -> None:
    """Other servers + top-level keys must survive the uninstall."""
    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(json.dumps({
        "servers": {
            "github": {"type": "stdio", "command": "mcp-github"},
            "missioncache": {"type": "stdio", "command": "mcp-missioncache"},
        },
        "inputs": [{"id": "tok", "type": "promptString"}],
    }))
    state.record_component("vscode", {"path": str(mcp_clients.VSCODE_USER_MCP_PATH)})

    mcp_clients.uninstall_vscode(_make_ctx())

    data = json.loads(mcp_clients.VSCODE_USER_MCP_PATH.read_text())
    assert data["servers"]["github"]["command"] == "mcp-github"
    assert "missioncache" not in data["servers"]
    assert data["inputs"] == [{"id": "tok", "type": "promptString"}]
    assert "vscode" not in state.load().get("components", {})


# ---------------------------------------------------------------------------
# Codex: install_codex via subprocess fake
# ---------------------------------------------------------------------------

def test_install_codex_warns_and_skips_when_cli_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `codex` on PATH -> no subprocess call, no state."""
    monkeypatch.setattr(mcp_clients.shutil, "which", lambda _: None)

    mcp_clients.install_codex(_make_ctx())

    assert "codex" not in state.load().get("components", {})


def test_install_codex_runs_mcp_add_when_not_registered(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: `codex mcp list` shows nothing -> we run `codex mcp add missioncache -- mcp-missioncache`."""
    # Make `which("codex")` truthy so detection passes; everything else can return None.
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[:3] == ["codex", "mcp", "list"]:
            # Empty list = missioncache not registered yet.
            return _proc(stdout="(no servers configured)\n")
        return _proc()

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run)

    mcp_clients.install_codex(_make_ctx())

    assert ["codex", "mcp", "list"] in calls
    assert ["codex", "mcp", "add", "missioncache", "--", "mcp-missioncache"] in calls
    assert state.load()["components"]["codex"]["command"] == "mcp-missioncache"


def test_install_codex_idempotent_when_missioncache_listed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `codex mcp list` already shows missioncache, `add` must not be called again."""
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[:3] == ["codex", "mcp", "list"]:
            return _proc(stdout="missioncache  mcp-missioncache  connected\nfoo  bar  connected\n")
        return _proc()

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run)

    mcp_clients.install_codex(_make_ctx())

    assert ["codex", "mcp", "list"] in calls
    assert not any(c[:4] == ["codex", "mcp", "add", "missioncache"] for c in calls), (
        "Idempotent install must not call `codex mcp add` when missioncache is already listed"
    )
    assert state.load()["components"]["codex"]["command"] == "mcp-missioncache"


# ---------------------------------------------------------------------------
# Per-run MCP success tracking (ctx.mcp_success)
# ---------------------------------------------------------------------------

def test_install_codex_sets_mcp_success_true_on_success(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful Codex registration must record this-run success on ctx."""
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)
    monkeypatch.setattr(
        mcp_clients.subprocess_utils,
        "run",
        lambda cmd, **_: _proc(stdout="(no servers configured)\n") if cmd[:3] == ["codex", "mcp", "list"] else _proc(),
    )

    ctx = _make_ctx()
    mcp_clients.install_codex(ctx)
    assert ctx.mcp_success.get("codex") is True


def test_install_codex_sets_mcp_success_true_on_already_registered(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent path (already registered) must also count as this-run success."""
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)
    monkeypatch.setattr(
        mcp_clients.subprocess_utils,
        "run",
        lambda cmd, **_: _proc(stdout="missioncache  mcp-missioncache  connected\n"),
    )

    ctx = _make_ctx()
    mcp_clients.install_codex(ctx)
    assert ctx.mcp_success.get("codex") is True


def test_install_codex_sets_mcp_success_false_when_cli_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex CLI not found -> ctx.mcp_success['codex'] = False (not absent).

    Distinguishes 'parent ran and failed' from 'parent did not run' so the
    command-installer gate emits the right pointer.
    """
    monkeypatch.setattr(mcp_clients.shutil, "which", lambda _: None)
    ctx = _make_ctx()
    mcp_clients.install_codex(ctx)
    assert ctx.mcp_success.get("codex") is False


def test_install_codex_sets_mcp_success_false_when_mcp_add_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex mcp add` returning non-zero must record failure, not omit the key."""
    import subprocess as _subprocess

    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)

    def fake_run(cmd: list[str], **_: Any) -> _subprocess.CompletedProcess[str]:
        if cmd[:3] == ["codex", "mcp", "list"]:
            return _proc(stdout="(no servers configured)\n")
        # `codex mcp add ...` raises CommandFailed.
        raise mcp_clients.subprocess_utils.CommandFailed(
            cmd=cmd, returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run)

    ctx = _make_ctx()
    with pytest.raises(mcp_clients.subprocess_utils.CommandFailed):
        mcp_clients.install_codex(ctx)
    assert ctx.mcp_success.get("codex") is False


def test_install_opencode_sets_mcp_success_true_on_success(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    ctx = _make_ctx()
    mcp_clients.install_opencode(ctx)
    assert ctx.mcp_success.get("opencode") is True


def test_install_opencode_sets_mcp_success_false_when_not_detected(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_opencode_detected(monkeypatch, False)
    ctx = _make_ctx()
    mcp_clients.install_opencode(ctx)
    assert ctx.mcp_success.get("opencode") is False


def test_install_opencode_sets_mcp_success_false_on_unparseable_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truly malformed opencode.json (not just JSONC) must record failure."""
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    # Unclosed brace - json5 fallback can't recover this either.
    mcp_clients.OPENCODE_CONFIG_PATH.write_text('{"mcp":')

    ctx = _make_ctx()
    with pytest.raises(mcp_clients.subprocess_utils.CommandFailed):
        mcp_clients.install_opencode(ctx)
    assert ctx.mcp_success.get("opencode") is False


def test_install_opencode_refuses_jsonc_and_preserves_user_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSONC config: parse via json5, then REFUSE auto-merge to keep comments.

    json.dumps would silently strip the user's comments and trailing commas
    when writing the merged file back. We refuse, leave the user's file
    untouched, and tell them exactly what to add manually. This is the
    regression test for the data-loss path that fix D originally introduced.
    """
    _set_opencode_detected(monkeypatch, True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.OPENCODE_CONFIG_PATH.parent.mkdir(parents=True)
    original = (
        '// my custom config\n'
        '{\n'
        '  "theme": "dark",\n'
        '  "mcp": {\n'
        '    "other": {"type": "local", "command": ["other-mcp"]},\n'
        '  },\n'
        '}\n'
    )
    mcp_clients.OPENCODE_CONFIG_PATH.write_text(original)

    warn_calls: list[str] = []
    monkeypatch.setattr(mcp_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    ctx = _make_ctx()
    with pytest.raises(mcp_clients.subprocess_utils.CommandFailed) as exc:
        mcp_clients.install_opencode(ctx)

    # File must be byte-for-byte unchanged.
    assert mcp_clients.OPENCODE_CONFIG_PATH.read_text() == original
    # MCP registration is reported as failed-this-run so the gate skips
    # opencode_commands install.
    assert ctx.mcp_success.get("opencode") is False
    assert "opencode" not in state.load().get("components", {})
    # User gets the exact snippet to add manually - carried in the raised
    # failure, which install_components surfaces as the component's warning.
    assert '"missioncache"' in exc.value.stderr and "manually" in exc.value.stderr.lower()


def test_install_vscode_refuses_jsonc_and_preserves_user_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refuse-and-preserve contract for VSCode mcp.json (JSONC by convention)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    mcp_clients.VSCODE_USER_MCP_PATH.parent.mkdir(parents=True)
    original = (
        '{\n'
        '  // user-managed servers\n'
        '  "servers": {\n'
        '    "context7": {"type": "http", "url": "https://example.com/mcp"}\n'
        '  }\n'
        '}\n'
    )
    mcp_clients.VSCODE_USER_MCP_PATH.write_text(original)

    warn_calls: list[str] = []
    monkeypatch.setattr(mcp_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    ctx = _make_ctx()
    with pytest.raises(mcp_clients.subprocess_utils.CommandFailed) as exc:
        mcp_clients.install_vscode(ctx)

    assert mcp_clients.VSCODE_USER_MCP_PATH.read_text() == original
    assert ctx.mcp_success.get("vscode") is False
    assert "vscode" not in state.load().get("components", {})
    assert '"missioncache"' in exc.value.stderr and "manually" in exc.value.stderr.lower()


def test_install_vscode_sets_mcp_success_true_on_success(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: True)
    _set_mcp_missioncache_path_ok(monkeypatch)

    ctx = _make_ctx()
    mcp_clients.install_vscode(ctx)
    assert ctx.mcp_success.get("vscode") is True


def test_install_vscode_sets_mcp_success_false_on_non_darwin(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = _make_ctx()
    mcp_clients.install_vscode(ctx)
    assert ctx.mcp_success.get("vscode") is False


# ---------------------------------------------------------------------------
# Codex helper: _codex_missioncache_registered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stdout, expected", [
    ("missioncache  mcp-missioncache  connected\n", True),
    ("missioncache\n", True),
    ("- missioncache  mcp-missioncache  connected\n", False),  # leading bullet -> first token is `-`
    ("missioncache-extra  mcp-missioncache  connected\n", False),  # different name, must not match
    ("(no servers configured)\n", False),
    ("", False),
])
def test_codex_missioncache_registered_matching(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: bool
) -> None:
    """`_codex_missioncache_registered` matches a line whose first whitespace token is exactly missioncache."""
    monkeypatch.setattr(
        mcp_clients.subprocess_utils,
        "run",
        lambda cmd, **_: _proc(stdout=stdout),
    )
    assert mcp_clients._codex_missioncache_registered() is expected


# ---------------------------------------------------------------------------
# _ensure_mcp_missioncache_on_path
# ---------------------------------------------------------------------------

def test_ensure_mcp_missioncache_on_path_returns_true_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pipx install when mcp-missioncache is already on PATH."""
    monkeypatch.setattr(mcp_clients.shutil, "which", lambda _: "/x/mcp-missioncache")
    pipx_called = MagicMock()
    monkeypatch.setattr(installers, "_pipx_install", pipx_called)

    assert mcp_clients._ensure_mcp_missioncache_on_path() is True
    pipx_called.assert_not_called()


# ---------------------------------------------------------------------------
# _ensure_codex_auto_approval
# ---------------------------------------------------------------------------

def _codex_config_with_missioncache() -> str:
    return (
        "[projects.x]\n"
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.missioncache]\n"
        'command = "mcp-missioncache"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-server"\n'
    )


def test_ensure_codex_auto_approval_inserts_key_in_missioncache_section(
    isolated_home: Path,
) -> None:
    """The key must land INSIDE [mcp_servers.missioncache], never leak into a
    following section (that would auto-approve a stranger's tools)."""
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text(_codex_config_with_missioncache())

    mcp_clients._ensure_codex_auto_approval()

    text = mcp_clients.CODEX_CONFIG_TOML.read_text()
    section = text[text.index("[mcp_servers.missioncache]"):text.index("[mcp_servers.other]")]
    assert 'default_tools_approval_mode = "approve"' in section
    other = text[text.index("[mcp_servers.other]"):]
    assert "default_tools_approval_mode" not in other
    assert 'trust_level = "trusted"' in text, "unrelated sections preserved"


def test_ensure_codex_auto_approval_idempotent(isolated_home: Path) -> None:
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text(_codex_config_with_missioncache())

    mcp_clients._ensure_codex_auto_approval()
    once = mcp_clients.CODEX_CONFIG_TOML.read_text()
    mcp_clients._ensure_codex_auto_approval()

    assert mcp_clients.CODEX_CONFIG_TOML.read_text() == once
    assert once.count("default_tools_approval_mode") == 1


def test_ensure_codex_auto_approval_respects_existing_user_value(
    isolated_home: Path,
) -> None:
    """A user who chose "prompt" keeps their choice - never overwrite."""
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text(
        "[mcp_servers.missioncache]\n"
        'command = "mcp-missioncache"\n'
        'default_tools_approval_mode = "prompt"\n'
    )

    mcp_clients._ensure_codex_auto_approval()

    text = mcp_clients.CODEX_CONFIG_TOML.read_text()
    assert 'default_tools_approval_mode = "prompt"' in text
    assert text.count("default_tools_approval_mode") == 1, (
        "must not ALSO insert approve alongside the user's value"
    )


def test_ensure_codex_auto_approval_warns_when_section_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text("[projects.x]\n")
    warns: list[str] = []
    monkeypatch.setattr(mcp_clients.ui, "warn", lambda msg: warns.append(msg))

    mcp_clients._ensure_codex_auto_approval()

    assert mcp_clients.CODEX_CONFIG_TOML.read_text() == "[projects.x]\n", "untouched"
    assert any("not found" in w for w in warns)


def test_install_codex_configures_approval_on_both_paths(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh add AND already-registered must both end with tool approval set -
    the already-registered path is exactly the machine where every tool call
    was auto-cancelled in exec mode (live, codex 0.144.1)."""
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["codex", "mcp", "list"]:
            return _proc(stdout="(no servers configured)\n")
        if cmd[:3] == ["codex", "mcp", "add"]:
            # codex mcp add writes the server section itself.
            mcp_clients.CODEX_CONFIG_TOML.write_text(
                '[mcp_servers.missioncache]\ncommand = "mcp-missioncache"\n'
            )
        return _proc()

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run)
    mcp_clients.install_codex(_make_ctx())
    assert 'default_tools_approval_mode = "approve"' in mcp_clients.CODEX_CONFIG_TOML.read_text()

    # Second run takes the already-registered branch; wipe the key first.
    mcp_clients.CODEX_CONFIG_TOML.write_text(
        '[mcp_servers.missioncache]\ncommand = "mcp-missioncache"\n'
    )

    def fake_run_registered(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["codex", "mcp", "list"]:
            return _proc(stdout="missioncache  mcp-missioncache\n")
        return _proc()

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run_registered)
    mcp_clients.install_codex(_make_ctx())
    assert 'default_tools_approval_mode = "approve"' in mcp_clients.CODEX_CONFIG_TOML.read_text()


def test_ensure_codex_auto_approval_respects_commented_out_key(
    isolated_home: Path,
) -> None:
    """A commented-out key is the user's visible choice (they disabled it
    deliberately or codex left it as documentation) - never insert a live
    value over it."""
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text(
        "[mcp_servers.missioncache]\n"
        'command = "mcp-missioncache"\n'
        '# default_tools_approval_mode = "prompt"\n'
    )

    mcp_clients._ensure_codex_auto_approval()

    text = mcp_clients.CODEX_CONFIG_TOML.read_text()
    assert text.count("default_tools_approval_mode") == 1
    assert 'default_tools_approval_mode = "approve"' not in text


def test_ensure_codex_auto_approval_header_at_eof_without_newline(
    isolated_home: Path,
) -> None:
    """Header as the last line with no trailing newline: inserting must not
    glue the key onto the header (that produced invalid TOML)."""
    import tomllib

    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text("[mcp_servers.missioncache]")

    mcp_clients._ensure_codex_auto_approval()

    text = mcp_clients.CODEX_CONFIG_TOML.read_text()
    parsed = tomllib.loads(text)  # must stay valid TOML
    assert parsed["mcp_servers"]["missioncache"]["default_tools_approval_mode"] == "approve"


def test_ensure_codex_auto_approval_write_failure_warns_not_raises(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing config write must warn and return (best-effort contract),
    never abort the whole install with an uncaught OSError."""
    mcp_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    mcp_clients.CODEX_CONFIG_TOML.write_text(
        '[mcp_servers.missioncache]\ncommand = "mcp-missioncache"\n'
    )
    warns: list[str] = []
    monkeypatch.setattr(mcp_clients.ui, "warn", lambda msg: warns.append(msg))

    def _boom(path, text):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(mcp_clients.fs_utils, "write_config_text", _boom)

    mcp_clients._ensure_codex_auto_approval()  # must not raise

    assert any("Could not write" in w for w in warns)


def test_install_codex_raises_when_mcp_add_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed codex mcp add must reach the installer's failed list (raise),
    not warn into a green checkmark, and mcp_success must record the failure
    for the commands child."""
    monkeypatch.setattr(
        mcp_clients.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else "/x/mcp-missioncache",
    )
    _set_mcp_missioncache_path_ok(monkeypatch)

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["codex", "mcp", "list"]:
            return _proc(stdout="(no servers configured)\n")
        if cmd[:3] == ["codex", "mcp", "add"]:
            raise mcp_clients.subprocess_utils.CommandFailed(
                cmd=cmd, returncode=1, stdout="", stderr="network unreachable"
            )
        return _proc()

    monkeypatch.setattr(mcp_clients.subprocess_utils, "run", fake_run)
    ctx = _make_ctx()

    with pytest.raises(mcp_clients.subprocess_utils.CommandFailed):
        mcp_clients.install_codex(ctx)

    assert ctx.mcp_success["codex"] is False
