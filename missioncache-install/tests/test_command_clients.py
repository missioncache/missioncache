"""Tests for orbit_install.command_clients - per-tool slash command installers.

Coverage:
- Render transformations (filename prefix, frontmatter strip, MCP rewrite)
- OpenCode/VSCode/Codex install + uninstall round-trips
- Idempotency (re-installing doesn't double-write or change unrelated state)
- Tool-not-detected -> warn-and-skip
- chat.promptFilesLocations merge preserves user keys + indent style
- Codex marketplace.json + plugin.json + config.toml stanza writes correctly
- Uninstall removes only what install wrote (other commands/keys preserved)

Bundled package access via `resources.files("orbit_install.bundled.commands")`
is mocked with a `_FakeTraversable` because the bundled/ tree exists only in
built wheels, not the editable dev install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit_install import command_clients, installers, mcp_clients, state


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

def _make_ctx(
    *, mcp_ready: tuple[str, ...] = ("codex", "opencode", "vscode")
) -> installers.InstallContext:
    """Minimal ctx for installers that read only `mode` (PyPI path).

    Defaults to all three MCP tools marked as ready in this run, since most
    tests exercise the post-MCP-success path. Tests that exercise the gate
    (parent failed / parent did not run) should pass `mcp_ready=()` or a
    narrower tuple.
    """
    return installers.InstallContext(
        mode="pypi",
        repo_root=None,
        skip_service=True,
        port=8787,
        assume_yes=True,
        mcp_success={tool: True for tool in mcp_ready},
    )


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class _FakeTraversable:
    """Minimal stand-in for importlib.resources.files traversable.

    Supports `/ name` to descend, `.read_text()` to read a file, and
    `.is_file()` for sanity. Backed by a real filesystem path under tmp.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __truediv__(self, other: str) -> "_FakeTraversable":
        return _FakeTraversable(self._path / other)

    def read_text(self) -> str:
        return self._path.read_text()

    def is_file(self) -> bool:
        return self._path.is_file()


@pytest.fixture
def fake_bundled_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Stand up a fake bundled/commands/ tree with one minimal command per name.

    Tests that need real command content can write into the returned path
    before invoking install_*. Default content is a minimal valid command
    file with frontmatter + an mcp__plugin_orbit_pm__* reference so the
    transformations have something to chew on.
    """
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    for name in command_clients.CANONICAL_COMMANDS:
        (bundle_root / f"{name}.md").write_text(
            f"---\n"
            f'description: "test command {name}"\n'
            f'argument-hint: "[arg]"\n'
            f"---\n"
            f"\n"
            f"# /{name}\n"
            f"\n"
            f"Body for {name}. Calls `mcp__plugin_orbit_pm__list_active_tasks`.\n"
        )
    monkeypatch.setattr(
        command_clients.resources, "files",
        lambda pkg: _FakeTraversable(bundle_root) if pkg == "orbit_install.bundled.commands" else _FakeTraversable(tmp_path / "_no_such_pkg"),
    )
    return bundle_root


def _set_opencode_detected(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    monkeypatch.setattr(mcp_clients, "_opencode_detected", lambda: present)


def _set_vscode_detected(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    monkeypatch.setattr(mcp_clients, "_vscode_detected", lambda: present)


def _set_codex_present(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    monkeypatch.setattr(
        command_clients.shutil, "which",
        lambda binary: "/usr/local/bin/codex" if (present and binary == "codex") else None,
    )


# ---------------------------------------------------------------------------
# _render_for_non_claude: transformations
# ---------------------------------------------------------------------------

def test_render_strips_argument_hint_from_frontmatter() -> None:
    src = (
        "---\n"
        'description: "x"\n'
        'argument-hint: "[name]"\n'
        "---\n"
        "\n"
        "Body.\n"
    )
    rendered = command_clients._render_for_non_claude(src)
    assert "argument-hint:" not in rendered
    assert 'description: "x"' in rendered
    assert "Body." in rendered


def test_render_substitutes_mcp_prefix_everywhere() -> None:
    src = (
        "---\n"
        'description: "x"\n'
        "---\n"
        "Call `mcp__plugin_orbit_pm__list_active_tasks(repo_path='.')`.\n"
        "Also `mcp__plugin_orbit_pm__get_task` and a stray reference.\n"
    )
    rendered = command_clients._render_for_non_claude(src)
    assert "mcp__plugin_orbit_pm__" not in rendered
    assert rendered.count("mcp__orbit__list_active_tasks") == 1
    assert rendered.count("mcp__orbit__get_task") == 1


def test_render_handles_file_without_frontmatter() -> None:
    src = "Plain markdown.\n`mcp__plugin_orbit_pm__foo`\n"
    rendered = command_clients._render_for_non_claude(src)
    assert rendered == "Plain markdown.\n`mcp__orbit__foo`\n"


def test_render_preserves_other_frontmatter_keys() -> None:
    """description, agent, model, etc. must survive the transformation."""
    src = (
        "---\n"
        'description: "x"\n'
        'agent: build\n'
        'model: anthropic/claude-3-5-sonnet-20241022\n'
        'argument-hint: "[name]"\n'
        "---\n"
        "Body.\n"
    )
    rendered = command_clients._render_for_non_claude(src)
    assert 'description: "x"' in rendered
    assert "agent: build" in rendered
    assert "model: anthropic/claude-3-5-sonnet-20241022" in rendered
    assert "argument-hint:" not in rendered


# ---------------------------------------------------------------------------
# OpenCode install/uninstall
# ---------------------------------------------------------------------------

def test_install_opencode_commands_writes_six_files(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_opencode_detected(monkeypatch, True)

    command_clients.install_opencode_commands(_make_ctx())

    files = sorted(p.name for p in command_clients.OPENCODE_COMMANDS_DIR.glob("*.md"))
    assert files == sorted(
        f"orbit-{name}.md" for name in command_clients.CANONICAL_COMMANDS
    )
    info = state.load()["components"]["opencode_commands"]
    assert len(info["files"]) == 6


def test_install_opencode_commands_renders_transformations(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_opencode_detected(monkeypatch, True)

    command_clients.install_opencode_commands(_make_ctx())

    content = (command_clients.OPENCODE_COMMANDS_DIR / "orbit-go.md").read_text()
    assert "argument-hint:" not in content
    assert "mcp__plugin_orbit_pm__" not in content
    assert "mcp__orbit__list_active_tasks" in content


def test_install_opencode_commands_skips_when_tool_missing(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_opencode_detected(monkeypatch, False)

    command_clients.install_opencode_commands(_make_ctx())

    assert not command_clients.OPENCODE_COMMANDS_DIR.exists()
    assert "opencode_commands" not in state.load().get("components", {})


def test_uninstall_opencode_commands_removes_only_orbit_files(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_opencode_detected(monkeypatch, True)
    command_clients.install_opencode_commands(_make_ctx())

    # User has their own command living alongside ours - must survive uninstall.
    user_cmd = command_clients.OPENCODE_COMMANDS_DIR / "my-custom.md"
    user_cmd.write_text("# my command")

    command_clients.uninstall_opencode_commands(_make_ctx())

    remaining = sorted(p.name for p in command_clients.OPENCODE_COMMANDS_DIR.glob("*.md"))
    assert remaining == ["my-custom.md"]
    assert "opencode_commands" not in state.load().get("components", {})


def test_install_opencode_commands_idempotent_state(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_opencode_detected(monkeypatch, True)

    command_clients.install_opencode_commands(_make_ctx())
    state_after_first = state.load()
    command_clients.install_opencode_commands(_make_ctx())
    state_after_second = state.load()

    assert (
        state_after_first["components"]["opencode_commands"]["files"]
        == state_after_second["components"]["opencode_commands"]["files"]
    )


# ---------------------------------------------------------------------------
# VSCode install/uninstall
# ---------------------------------------------------------------------------

def test_install_vscode_commands_writes_prompt_md_and_registers_location(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.install_vscode_commands(_make_ctx())

    files = sorted(p.name for p in command_clients.VSCODE_PROMPTS_DIR.glob("*.prompt.md"))
    assert files == sorted(
        f"orbit-{name}.prompt.md" for name in command_clients.CANONICAL_COMMANDS
    )
    settings_data = json.loads(command_clients.VSCODE_USER_SETTINGS_PATH.read_text())
    assert settings_data["chat.promptFilesLocations"][
        str(command_clients.VSCODE_PROMPTS_DIR)
    ] is True


def test_install_vscode_commands_preserves_other_settings_keys(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True)
    command_clients.VSCODE_USER_SETTINGS_PATH.write_text(json.dumps({
        "github.copilot.chat.localeOverride": "en",
        "chat.instructionsFilesLocations": {".github/instructions": True},
        "editor.fontSize": 14,
    }, indent=2))

    command_clients.install_vscode_commands(_make_ctx())

    out = json.loads(command_clients.VSCODE_USER_SETTINGS_PATH.read_text())
    assert out["github.copilot.chat.localeOverride"] == "en"
    assert out["chat.instructionsFilesLocations"] == {".github/instructions": True}
    assert out["editor.fontSize"] == 14
    assert out["chat.promptFilesLocations"][
        str(command_clients.VSCODE_PROMPTS_DIR)
    ] is True


def test_install_vscode_commands_preserves_existing_promptFilesLocations(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True)
    command_clients.VSCODE_USER_SETTINGS_PATH.write_text(json.dumps({
        "chat.promptFilesLocations": {
            "/some/other/path": True,
            "/yet/another": False,
        }
    }, indent=2))

    command_clients.install_vscode_commands(_make_ctx())

    locations = json.loads(command_clients.VSCODE_USER_SETTINGS_PATH.read_text())[
        "chat.promptFilesLocations"
    ]
    assert locations["/some/other/path"] is True
    assert locations["/yet/another"] is False
    assert locations[str(command_clients.VSCODE_PROMPTS_DIR)] is True


def test_install_vscode_commands_preserves_tab_indent(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True)
    command_clients.VSCODE_USER_SETTINGS_PATH.write_text(
        '{\n\t"github.copilot.chat.localeOverride": "en"\n}'
    )

    command_clients.install_vscode_commands(_make_ctx())

    raw = command_clients.VSCODE_USER_SETTINGS_PATH.read_text()
    assert "\n\t" in raw, "Tab indent must survive the merge"


def test_install_vscode_commands_skips_on_non_macos(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    command_clients.install_vscode_commands(_make_ctx())

    assert not command_clients.VSCODE_PROMPTS_DIR.exists()
    assert "vscode_commands" not in state.load().get("components", {})


def test_install_vscode_commands_skips_when_app_missing(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, False)

    command_clients.install_vscode_commands(_make_ctx())

    assert not command_clients.VSCODE_PROMPTS_DIR.exists()
    assert "vscode_commands" not in state.load().get("components", {})


def test_uninstall_vscode_commands_round_trips_cleanly(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True)
    command_clients.VSCODE_USER_SETTINGS_PATH.write_text(json.dumps({
        "chat.instructionsFilesLocations": {".github/instructions": True},
    }, indent=2))

    command_clients.install_vscode_commands(_make_ctx())
    command_clients.uninstall_vscode_commands(_make_ctx())

    out = json.loads(command_clients.VSCODE_USER_SETTINGS_PATH.read_text())
    assert out["chat.instructionsFilesLocations"] == {".github/instructions": True}
    locations = out.get("chat.promptFilesLocations", {})
    assert str(command_clients.VSCODE_PROMPTS_DIR) not in locations
    assert not command_clients.VSCODE_PROMPTS_DIR.exists()
    assert "vscode_commands" not in state.load().get("components", {})


# ---------------------------------------------------------------------------
# Codex install/uninstall
# ---------------------------------------------------------------------------

def test_install_codex_commands_builds_full_marketplace_tree(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )

    command_clients.install_codex_commands(_make_ctx())

    plugin_dir = command_clients.CODEX_MARKETPLACE_DIR / "plugins" / "orbit"
    assert (plugin_dir / ".codex-plugin" / "plugin.json").exists()
    assert (
        command_clients.CODEX_MARKETPLACE_DIR / ".agents" / "plugins" / "marketplace.json"
    ).exists()
    cmds = sorted(p.name for p in (plugin_dir / "commands").glob("*.md"))
    assert cmds == sorted(
        f"orbit-{name}.md" for name in command_clients.CANONICAL_COMMANDS
    )

    manifest = json.loads(
        (plugin_dir / ".codex-plugin" / "plugin.json").read_text()
    )
    assert manifest["name"] == "orbit"
    assert manifest["version"] == command_clients.CODEX_PLUGIN_VERSION

    marketplace = json.loads(
        (command_clients.CODEX_MARKETPLACE_DIR / ".agents" / "plugins" / "marketplace.json").read_text()
    )
    assert marketplace["name"] == "orbit"
    assert marketplace["plugins"][0]["source"] == {
        "source": "local", "path": "./plugins/orbit"
    }


def test_install_codex_commands_writes_config_stanza(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )

    command_clients.install_codex_commands(_make_ctx())

    text = command_clients.CODEX_CONFIG_TOML.read_text()
    assert '[plugins."orbit@orbit"]' in text


def test_install_codex_commands_preserves_existing_config_stanzas(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )

    command_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    command_clients.CODEX_CONFIG_TOML.write_text(
        '[mcp_servers.orbit]\ncommand = "mcp-orbit"\n\n'
        '[plugins."github@openai-curated"]\n'
    )

    command_clients.install_codex_commands(_make_ctx())

    text = command_clients.CODEX_CONFIG_TOML.read_text()
    assert "[mcp_servers.orbit]" in text
    assert '[plugins."github@openai-curated"]' in text
    assert '[plugins."orbit@orbit"]' in text


def test_install_codex_commands_idempotent_on_config(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )

    command_clients.install_codex_commands(_make_ctx())
    command_clients.install_codex_commands(_make_ctx())

    text = command_clients.CODEX_CONFIG_TOML.read_text()
    assert text.count('[plugins."orbit@orbit"]') == 1


def test_install_codex_commands_skips_when_codex_missing(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, False)

    command_clients.install_codex_commands(_make_ctx())

    assert not command_clients.CODEX_MARKETPLACE_DIR.exists()
    assert "codex_commands" not in state.load().get("components", {})


def test_install_codex_commands_calls_marketplace_add(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    calls: list[list[str]] = []

    def _record(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _proc()

    monkeypatch.setattr(command_clients.subprocess_utils, "run", _record)

    command_clients.install_codex_commands(_make_ctx())

    assert ["codex", "plugin", "marketplace", "add", str(command_clients.CODEX_MARKETPLACE_DIR)] in calls


def test_install_codex_commands_treats_already_added_as_success(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)

    def _fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["codex", "plugin", "marketplace", "add"]:
            raise command_clients.subprocess_utils.CommandFailed(
                cmd=cmd, returncode=1, stdout="", stderr="marketplace already exists"
            )
        return _proc()

    monkeypatch.setattr(command_clients.subprocess_utils, "run", _fake_run)

    command_clients.install_codex_commands(_make_ctx())

    assert "codex_commands" in state.load()["components"]


def test_uninstall_codex_commands_round_trips(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    _set_codex_present(monkeypatch, True)
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )
    command_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    command_clients.CODEX_CONFIG_TOML.write_text(
        '[mcp_servers.orbit]\ncommand = "mcp-orbit"\n'
    )

    command_clients.install_codex_commands(_make_ctx())
    command_clients.uninstall_codex_commands(_make_ctx())

    text = command_clients.CODEX_CONFIG_TOML.read_text()
    assert "[mcp_servers.orbit]" in text
    assert '[plugins."orbit@orbit"]' not in text
    assert not command_clients.CODEX_MARKETPLACE_DIR.exists()
    assert "codex_commands" not in state.load().get("components", {})


def test_strip_codex_plugin_stanza_removes_only_orbit() -> None:
    """Stripping orbit's stanza must not touch other [plugins.*] sections."""
    text = (
        '[mcp_servers.orbit]\n'
        'command = "mcp-orbit"\n'
        '\n'
        '[plugins."github@openai-curated"]\n'
        '\n'
        '[plugins."orbit@orbit"]\n'
        '\n'
        '[some.other.section]\n'
        'key = "value"\n'
    )
    out = command_clients._strip_codex_plugin_stanza(text)
    assert '[plugins."orbit@orbit"]' not in out
    assert '[plugins."github@openai-curated"]' in out
    assert '[some.other.section]' in out
    assert 'key = "value"' in out


def test_strip_codex_plugin_stanza_handles_body_lines() -> None:
    """If the orbit stanza has body content (overrides), strip those too."""
    text = (
        '[plugins."orbit@orbit"]\n'
        'enabled = true\n'
        'foo = "bar"\n'
        '\n'
        '[other]\n'
        'k = "v"\n'
    )
    out = command_clients._strip_codex_plugin_stanza(text)
    assert '[plugins."orbit@orbit"]' not in out
    assert 'enabled = true' not in out
    assert 'foo = "bar"' not in out
    assert '[other]\nk = "v"\n' in out


# ---------------------------------------------------------------------------
# Bug fixes from team review (subsection leak, fragile substring matches,
# misleading success messages, cross-reference rewrites, errno filtering)
# ---------------------------------------------------------------------------

def test_strip_codex_plugin_stanza_strips_orbit_subsections() -> None:
    """A user-added subsection of orbit must be stripped, not leaked.

    Bug: substring `[plugins."orbit@orbit".overrides]` starts with `[` and
    ends with `]`, so a naive "any header ends skip" parser would treat it
    as a sibling section and leak the subsection plus everything after it.
    """
    text = (
        '[plugins."orbit@orbit"]\n'
        'enabled = true\n'
        '\n'
        '[plugins."orbit@orbit".overrides]\n'
        'x = 1\n'
        '\n'
        '[plugins."orbit@orbit".env]\n'
        'KEY = "value"\n'
        '\n'
        '[other.section]\n'
        'k = "v"\n'
    )
    out = command_clients._strip_codex_plugin_stanza(text)
    assert '[plugins."orbit@orbit"' not in out
    assert 'enabled = true' not in out
    assert 'x = 1' not in out
    assert 'KEY = "value"' not in out
    assert '[other.section]\nk = "v"\n' in out


def test_codex_already_registered_re_matches_only_intended_phrases() -> None:
    """Anchored regex must match canonical idempotency wordings - no broader.

    Critical bug if this regex is too loose: a real conflict like "marketplace
    exists at this path with different content" would be treated as benign
    success, and the install would proceed to enable a plugin pointing at the
    wrong marketplace.
    """
    matches = [
        "marketplace already registered",
        "Already registered.",
        "Already added at the requested path",
        "Already exists",
        "Already installed",
    ]
    non_matches = [
        "marketplace exists at this path with different content",
        "already in use by another marketplace",
        "the marketplace cache directory does not exist",
        "marketplace exists at /tmp/orbit",
    ]
    for m in matches:
        assert command_clients._CODEX_ALREADY_REGISTERED_RE.search(m), (
            f"expected match for {m!r}"
        )
    for m in non_matches:
        assert not command_clients._CODEX_ALREADY_REGISTERED_RE.search(m), (
            f"did not expect match for {m!r}"
        )


def test_register_codex_marketplace_returns_true_on_clean_success(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_clients.subprocess_utils, "run", lambda cmd, **kw: _proc()
    )
    assert command_clients._register_codex_marketplace() is True


def test_register_codex_marketplace_returns_true_on_already_registered(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise command_clients.subprocess_utils.CommandFailed(
            cmd=cmd, returncode=1, stdout="", stderr="marketplace already registered"
        )
    monkeypatch.setattr(command_clients.subprocess_utils, "run", _fake)
    assert command_clients._register_codex_marketplace() is True


def test_register_codex_marketplace_returns_false_on_unknown_failure(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise command_clients.subprocess_utils.CommandFailed(
            cmd=cmd, returncode=1, stdout="", stderr="permission denied",
        )
    monkeypatch.setattr(command_clients.subprocess_utils, "run", _fake)
    assert command_clients._register_codex_marketplace() is False


def test_install_codex_commands_skips_state_record_on_marketplace_failure(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """When marketplace registration fails, do not enable plugin or record state."""
    _set_codex_present(monkeypatch, True)

    def _fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:4] == ["codex", "plugin", "marketplace", "add"]:
            raise command_clients.subprocess_utils.CommandFailed(
                cmd=cmd, returncode=1, stdout="", stderr="permission denied",
            )
        return _proc()

    monkeypatch.setattr(command_clients.subprocess_utils, "run", _fake)

    command_clients.install_codex_commands(_make_ctx())

    assert "codex_commands" not in state.load().get("components", {}), (
        "state must NOT record success when marketplace registration fails"
    )
    if command_clients.CODEX_CONFIG_TOML.exists():
        assert '[plugins."orbit@orbit"]' not in command_clients.CODEX_CONFIG_TOML.read_text(), (
            "stanza must NOT be written when marketplace registration failed"
        )


def test_render_rewrites_orbit_slash_cross_references() -> None:
    """Body cross-refs like /orbit:prompts must rewrite to /orbit-prompts."""
    src = (
        "---\n"
        'description: "x"\n'
        "---\n"
        "After this finishes, run `/orbit:prompts <project>` to generate prompts.\n"
        "See also `/orbit:save` and `/orbit:done`.\n"
    )
    rendered = command_clients._render_for_non_claude(src)
    assert "/orbit:prompts" not in rendered
    assert "/orbit-prompts" in rendered
    assert "/orbit-save" in rendered
    assert "/orbit-done" in rendered


def test_render_does_not_rewrite_unrelated_colons() -> None:
    """The /orbit: rewrite must not match URLs, timestamps, or namespaces."""
    src = "Body with https://example.com and 12:30:00 timestamp.\n"
    rendered = command_clients._render_for_non_claude(src)
    assert rendered == src, "no /orbit: literal -> no rewrite"


def test_install_opencode_commands_warns_on_partial(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """Missing source files emit ui.warn rather than misleading ui.success.

    The ui.success path used to hardcode all six command names regardless of
    how many actually wrote. Now success only fires when every CANONICAL_COMMANDS
    entry was written.
    """
    _set_opencode_detected(monkeypatch, True)
    # Drop one source file so the install is partial.
    (fake_bundled_commands / "save.md").unlink()

    success_calls: list[str] = []
    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "success", lambda msg: success_calls.append(msg))
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    command_clients.install_opencode_commands(_make_ctx())

    assert success_calls == [], "no success on partial install"
    assert any("5/6" in m or "incomplete" in m.lower() for m in warn_calls), (
        f"expected partial-install warn; got {warn_calls!r}"
    )


def test_install_vscode_commands_warns_when_settings_unparsable(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """A truly malformed settings.json must not emit a misleading success.

    JSONC (comments + trailing commas) is now parsed successfully by the
    json5 fallback in `_load_json_object`. This test exercises the path
    where neither strict JSON nor json5 can recover - genuinely corrupt
    input that the user has to fix by hand.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    command_clients.VSCODE_USER_SETTINGS_PATH.parent.mkdir(parents=True)
    # Truly malformed: unclosed brace, no recovery path even for JSONC.
    command_clients.VSCODE_USER_SETTINGS_PATH.write_text('{"theme": "dark"')

    success_calls: list[str] = []
    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "success", lambda msg: success_calls.append(msg))
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    command_clients.install_vscode_commands(_make_ctx())

    assert success_calls == [], "registration was skipped; no success allowed"
    assert any("incomplete" in m.lower() or "skipped" in m.lower() for m in warn_calls), (
        f"expected incomplete-install warn; got {warn_calls!r}"
    )


def test_enable_codex_plugin_treats_commented_stanza_as_absent(
    isolated_home: Path,
) -> None:
    """A commented-out stanza is not active; install must add the live one."""
    command_clients.CODEX_CONFIG_TOML.parent.mkdir(parents=True)
    command_clients.CODEX_CONFIG_TOML.write_text(
        '# [plugins."orbit@orbit"]\n# disabled for testing\n'
    )

    command_clients._enable_codex_plugin()

    text = command_clients.CODEX_CONFIG_TOML.read_text()
    assert '# [plugins."orbit@orbit"]' in text, "comment must be preserved"
    # The non-commented stanza must now also exist.
    assert command_clients._CODEX_PLUGIN_STANZA_RE.search(text), (
        "live stanza must be appended even though a comment matches the substring"
    )


def test_uninstall_vscode_commands_warns_on_unexpected_oserror(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """Permission/access errors on rmdir must surface, not be swallowed."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)
    command_clients.install_vscode_commands(_make_ctx())

    # Make rmdir raise EACCES instead of ENOTEMPTY.
    real_rmdir = Path.rmdir

    def _failing_rmdir(self: Path) -> None:
        if self == command_clients.VSCODE_PROMPTS_DIR:
            raise OSError(13, "Permission denied")
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", _failing_rmdir)
    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    command_clients.uninstall_vscode_commands(_make_ctx())

    assert any("Permission denied" in m or "Could not remove" in m for m in warn_calls), (
        f"expected EACCES warning; got {warn_calls!r}"
    )


# ---------------------------------------------------------------------------
# Per-run MCP success gating (_mcp_ready_for)
# ---------------------------------------------------------------------------

def test_install_codex_commands_skips_when_codex_mcp_not_run_this_session(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """User runs `--codex-commands --no-codex`: parent never ran -> child skips.

    The pre-fix behavior was to warn and proceed, leaving registered commands
    that call mcp__orbit__* tools that aren't registered with Codex.
    """
    _set_codex_present(monkeypatch, True)

    warn_calls: list[str] = []
    success_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))
    monkeypatch.setattr(command_clients.ui, "success", lambda msg: success_calls.append(msg))

    # mcp_ready=() leaves ctx.mcp_success empty (parent didn't run this session).
    command_clients.install_codex_commands(_make_ctx(mcp_ready=()))

    # Gate must short-circuit BEFORE state.record_component or marketplace build.
    assert "codex_commands" not in state.load().get("components", {})
    assert success_calls == [], "no success message when parent MCP did not run"
    assert any("orbit-install --codex" in m for m in warn_calls), (
        f"expected pointer to `orbit-install --codex`; got {warn_calls!r}"
    )


def test_install_codex_commands_skips_when_codex_mcp_failed_this_session(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """Parent MCP install ran and failed -> child skips with a different pointer."""
    _set_codex_present(monkeypatch, True)

    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    ctx = _make_ctx(mcp_ready=())
    ctx.mcp_success["codex"] = False
    command_clients.install_codex_commands(ctx)

    assert "codex_commands" not in state.load().get("components", {})
    assert any("failed earlier in this run" in m for m in warn_calls), (
        f"expected 'failed earlier' wording; got {warn_calls!r}"
    )


def test_install_opencode_commands_skips_when_opencode_mcp_not_ready(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """OpenCode commands gate has the same shape as Codex's."""
    _set_opencode_detected(monkeypatch, True)

    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    command_clients.install_opencode_commands(_make_ctx(mcp_ready=()))

    assert "opencode_commands" not in state.load().get("components", {})
    assert any("orbit-install --opencode" in m for m in warn_calls), (
        f"expected pointer to `orbit-install --opencode`; got {warn_calls!r}"
    )


def test_install_vscode_commands_skips_when_vscode_mcp_not_ready(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bundled_commands: Path,
) -> None:
    """VSCode commands gate has the same shape as Codex's."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _set_vscode_detected(monkeypatch, True)

    warn_calls: list[str] = []
    monkeypatch.setattr(command_clients.ui, "warn", lambda msg: warn_calls.append(msg))

    command_clients.install_vscode_commands(_make_ctx(mcp_ready=()))

    assert "vscode_commands" not in state.load().get("components", {})
    assert any("orbit-install --vscode" in m for m in warn_calls), (
        f"expected pointer to `orbit-install --vscode`; got {warn_calls!r}"
    )
