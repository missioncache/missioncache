#!/usr/bin/env bash
set -euo pipefail

# ─── Colors and symbols ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

CHECK="${GREEN}✔${NC}"
CROSS="${RED}✖${NC}"
ARROW="${CYAN}→${NC}"
DOT="${DIM}·${NC}"

ORBIT_ROOT="$HOME/.claude/orbit"
ORBIT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETPLACE="$HOME/.claude/plugins/local-marketplace"

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo -e "  ${ARROW} $1"; }
success() { echo -e "  ${CHECK} $1"; }
warn()    { echo -e "  ${YELLOW}!${NC} $1"; }
fail()    { echo -e "  ${CROSS} $1"; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}Step $1: $2${NC}"; }
detail()  { echo -e "  ${DOT} $1"; }

ask_yn() {
    local prompt="$1" default="${2:-Y}"
    local yn
    if [[ "$default" == "Y" ]]; then
        read -rp "  $prompt [Y/n] " yn
        yn="${yn:-Y}"
    else
        read -rp "  $prompt [y/N] " yn
        yn="${yn:-N}"
    fi
    [[ "$yn" =~ ^[Yy] ]]
}

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}  ┌─────────────────────────────────────────┐${NC}"
echo -e "${BOLD}${CYAN}  │       Orbit - Project Manager for       │${NC}"
echo -e "${BOLD}${CYAN}  │            Claude Code                  │${NC}"
echo -e "${BOLD}${CYAN}  └─────────────────────────────────────────┘${NC}"
echo ""
echo -e "  ${DIM}This script will set up Orbit on your system.${NC}"
echo ""

# ─── Prerequisites ────────────────────────────────────────────────────────────
echo -e "${BOLD}Checking prerequisites...${NC}"

# Python 3.11+
PYTHON=""
for cmd in python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major="${ver%%.*}"
        minor="${ver#*.}"
        if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 11 ]]; then
            PYTHON="$cmd"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    warn "Python 3.11+ is required but not found"
    detail "Tried: python3.11, python3.12, python3.13, python3.14, python3"
    detail "Install options:"
    detail "  macOS (Homebrew):  brew install python@3.11"
    detail "  Ubuntu/Debian:     sudo apt install python3.11 python3.11-venv"
    detail "  Other:             https://www.python.org/downloads/"
    exit 1
fi
success "Python $($PYTHON --version 2>&1 | awk '{print $2}')"

# pip
if ! "$PYTHON" -m pip --version &>/dev/null; then
    fail "pip is required but not found (try: $PYTHON -m ensurepip)"
fi
success "pip"

# uvx (needed by the plugin MCP server at runtime; dashboard/orbit-auto don't need it)
if command -v uvx &>/dev/null; then
    success "uvx"
else
    warn "uvx not found - the orbit MCP server needs it to run slash commands"
    detail "Install with: pip install uv"
    detail "    or:       curl -LsSf https://astral.sh/uv/install.sh | sh"
    detail "Continuing - dashboard, orbit-auto, and statusline don't need uvx"
fi

# Claude Code CLI
if command -v claude &>/dev/null; then
    success "Claude Code CLI"
else
    warn "Claude Code CLI not found - plugin installation will be skipped"
    warn "Install from: https://claude.ai/code"
fi

# ─── Step 1: Core Plugin ─────────────────────────────────────────────────────
step 1 "Core Plugin"

# Create data directories (always - these are shared between quick and full installs)
mkdir -p "$ORBIT_ROOT/active" "$ORBIT_ROOT/completed"
detail "Created $ORBIT_ROOT/{active,completed}/"

# Detect existing marketplace install so users running setup.sh after a quick
# install don't get a duplicate local-marketplace entry.
SETTINGS="$HOME/.claude/settings.json"
EXISTING_PLUGIN=""
if [[ -f "$SETTINGS" ]]; then
    EXISTING_PLUGIN=$("$PYTHON" -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    ep = d.get('enabledPlugins', {})
    for key in ep:
        if key.startswith('orbit@') and key != 'orbit@local' and ep[key]:
            print(key)
            break
except Exception:
    pass
" 2>/dev/null)
fi

SKIP_LOCAL_MARKETPLACE=0
if [[ -n "$EXISTING_PLUGIN" ]]; then
    warn "Orbit is already installed via $EXISTING_PLUGIN."
    detail "Continuing without installing orbit@local will leave the rest of setup.sh"
    detail "(orbit-db, dashboard, orbit-auto, statusline) pointing at THIS checkout while"
    detail "your Claude Code session still runs plugin code from $EXISTING_PLUGIN. That's"
    detail "fine for a user who wants the extras, but it means 'setup.sh' will NOT test the"
    detail "plugin code in this clone. Maintainers doing a dev loop should install orbit@local."
    echo ""
    if ask_yn "Install orbit@local from this clone alongside $EXISTING_PLUGIN?" "Y"; then
        info "Installing orbit@local from this clone..."
    else
        SKIP_LOCAL_MARKETPLACE=1
        detail "Skipping local marketplace - plugin code will stay on $EXISTING_PLUGIN"
        detail "To uninstall the marketplace copy later: claude plugins uninstall $EXISTING_PLUGIN"
    fi
fi

if [[ $SKIP_LOCAL_MARKETPLACE -eq 0 ]]; then
    [[ -z "$EXISTING_PLUGIN" ]] && info "Installing Orbit plugin for Claude Code..."

# Set up local marketplace
mkdir -p "$MARKETPLACE/plugins" "$MARKETPLACE/.claude-plugin"

# Create or update marketplace.json
MARKETPLACE_JSON="$MARKETPLACE/.claude-plugin/marketplace.json"
if [[ -f "$MARKETPLACE_JSON" ]]; then
    # Check if orbit is already listed
    if "$PYTHON" -c "import json; d=json.load(open('$MARKETPLACE_JSON')); exit(0 if any(p['name']=='orbit' for p in d.get('plugins',[])) else 1)" 2>/dev/null; then
        detail "Orbit already registered in marketplace"
    else
        # Add orbit to existing marketplace
        "$PYTHON" -c "
import json
with open('$MARKETPLACE_JSON') as f:
    d = json.load(f)
d.setdefault('plugins', []).append({
    'name': 'orbit',
    'source': './plugins/orbit',
    'description': 'Project management with time tracking and autonomous execution',
    'category': 'productivity'
})
with open('$MARKETPLACE_JSON', 'w') as f:
    json.dump(d, f, indent=2)
"
        detail "Added Orbit to marketplace"
    fi
else
    cat > "$MARKETPLACE_JSON" << 'MKJSON'
{
  "name": "local",
  "owner": {
    "name": "Tomer Brami"
  },
  "plugins": [
    {
      "name": "orbit",
      "source": "./plugins/orbit",
      "description": "Project management with time tracking and autonomous execution",
      "category": "productivity"
    }
  ]
}
MKJSON
    detail "Created marketplace.json"
fi

# Symlink the repo into the marketplace
PLUGIN_LINK="$MARKETPLACE/plugins/orbit"
if [[ -L "$PLUGIN_LINK" ]]; then
    current_target=$(readlink "$PLUGIN_LINK")
    if [[ "$current_target" == "$ORBIT_REPO" ]]; then
        detail "Symlink already exists"
    else
        rm "$PLUGIN_LINK"
        ln -s "$ORBIT_REPO" "$PLUGIN_LINK"
        detail "Updated symlink -> $ORBIT_REPO"
    fi
elif [[ -d "$PLUGIN_LINK" ]]; then
    warn "Removing existing orbit directory (not a symlink)"
    rm -rf "$PLUGIN_LINK"
    ln -s "$ORBIT_REPO" "$PLUGIN_LINK"
    detail "Created symlink -> $ORBIT_REPO"
else
    ln -s "$ORBIT_REPO" "$PLUGIN_LINK"
    detail "Created symlink -> $ORBIT_REPO"
fi

# Install the plugin via Claude CLI
if command -v claude &>/dev/null; then
    # Enable in settings.json
    SETTINGS="$HOME/.claude/settings.json"
    if [[ -f "$SETTINGS" ]]; then
        "$PYTHON" -c "
import json
with open('$SETTINGS') as f:
    d = json.load(f)
ep = d.setdefault('enabledPlugins', {})
ep['orbit@local'] = True
with open('$SETTINGS', 'w') as f:
    json.dump(d, f, indent=2)
"
        detail "Enabled orbit@local in settings.json"
    fi

    claude plugins install orbit@local 2>/dev/null && detail "Plugin installed" || warn "Plugin install failed - you may need to run: claude plugins install orbit@local"
else
    warn "Skipped plugin install (Claude CLI not found)"
fi

fi  # close EXISTING_PLUGIN guard

success "Core plugin installed"

# ─── Step 2: Orbit Database ──────────────────────────────────────────────────
step 2 "Orbit Database"
info "Installing orbit-db for task tracking..."

"$PYTHON" -m pip install -e "$ORBIT_REPO/orbit-db" --quiet
detail "Installed orbit-db (editable mode)"

# Initialize the database. `TaskDB()` alone is lazy and does not touch the filesystem -
# the schema is only created when a connection is first needed. `python -m orbit_db init`
# calls `TaskDB().initialize()` which runs SCHEMA_SQL and actually creates the file.
# This also verifies the package is importable and the schema runs cleanly.
"$PYTHON" -m orbit_db init

success "Orbit database ready"

# ─── Step 3: Orbit Dashboard ─────────────────────────────────────────────────
step 3 "Orbit Dashboard"
info "Installing the Orbit Dashboard web UI..."
echo -e "  ${DIM}Features:${NC}"
echo -e "  ${DIM}  - Task tracking with time analytics${NC}"
echo -e "  ${DIM}  - Orbit Auto execution monitoring${NC}"
echo -e "  ${DIM}  - Claude Code usage statistics${NC}"
echo ""

# Install dashboard dependencies
"$PYTHON" -m pip install -r "$ORBIT_REPO/orbit-dashboard/requirements.txt" --quiet
detail "Installed dashboard dependencies"

# Ask about background service
if [[ "$(uname)" == "Darwin" ]]; then
    if ask_yn "Run dashboard as a background service (launchd)?" "Y"; then
        PLIST_NAME="com.orbit.dashboard"
        PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
        PYTHON_PATH=$(command -v "$PYTHON")

        # Unload existing service if present
        launchctl unload "$PLIST_PATH" 2>/dev/null || true

        mkdir -p "$HOME/.claude/logs"

        cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$ORBIT_REPO/orbit-dashboard/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ORBIT_REPO/orbit-dashboard</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.claude/logs/orbit-dashboard-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.claude/logs/orbit-dashboard-stderr.log</string>
</dict>
</plist>
PLIST
        launchctl load "$PLIST_PATH"
        detail "Dashboard running as launchd service"
        detail "Logs: ~/.claude/logs/orbit-dashboard-{stdout,stderr}.log"
    else
        info "To start manually: $PYTHON $ORBIT_REPO/orbit-dashboard/server.py"
    fi
elif command -v systemctl &>/dev/null; then
    if ask_yn "Run dashboard as a background service (systemd)?" "Y"; then
        SERVICE_PATH="$HOME/.config/systemd/user/orbit-dashboard.service"
        mkdir -p "$(dirname "$SERVICE_PATH")"
        PYTHON_PATH=$(command -v "$PYTHON")

        cat > "$SERVICE_PATH" << SERVICE
[Unit]
Description=Orbit Dashboard
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON_PATH $ORBIT_REPO/orbit-dashboard/server.py
WorkingDirectory=$ORBIT_REPO/orbit-dashboard
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SERVICE
        systemctl --user daemon-reload
        systemctl --user enable --now orbit-dashboard.service
        detail "Dashboard running as systemd service"
    else
        info "To start manually: $PYTHON $ORBIT_REPO/orbit-dashboard/server.py"
    fi
else
    info "To start the dashboard: $PYTHON $ORBIT_REPO/orbit-dashboard/server.py"
fi

success "Orbit Dashboard installed (port 8787)"

# ─── Step 4: Orbit Auto CLI ──────────────────────────────────────────────────
step 4 "Orbit Auto CLI"
info "Installing Orbit Auto for autonomous task execution..."
echo -e "  ${DIM}Features:${NC}"
echo -e "  ${DIM}  - Parallel execution with dependency-aware scheduling${NC}"
echo -e "  ${DIM}  - Integrated with Orbit Dashboard monitoring${NC}"
echo ""

"$PYTHON" -m pip install -e "$ORBIT_REPO/orbit-auto" --quiet
detail "Installed orbit-auto (editable mode)"

# Verify the package actually imports before claiming success.
"$PYTHON" -c "import orbit_auto"
detail "orbit-auto package verified"

if command -v orbit-auto &>/dev/null; then
    detail "CLI command available: orbit-auto"
else
    warn "orbit-auto not in PATH - you may need to restart your shell"
fi

success "Orbit Auto CLI installed"

# ─── Step 5: Pre-build MCP Server ────────────────────────────────────────────
step 5 "Pre-build MCP Server"
info "Building MCP server virtual environment..."

if command -v uvx &>/dev/null; then
    # `</dev/null` is critical: mcp-orbit is a FastMCP server that reads stdin on startup,
    # and without this redirect the subprocess inherits and consumes setup.sh's stdin,
    # breaking any subsequent `read` prompts in Step 6/7 (and producing "press enter twice"
    # symptoms on a real interactive TTY).
    uvx --from "$ORBIT_REPO/mcp-server" --with "$ORBIT_REPO/orbit-db" mcp-orbit --help </dev/null &>/dev/null 2>&1 && detail "MCP server venv built" || detail "MCP server will build on first use"
else
    detail "uvx not found - MCP server will build on first use via Claude Code"
fi

success "MCP server ready"

# ─── Step 6: Statusline (optional) ───────────────────────────────────────────
step 6 "Statusline ${DIM}(optional)${NC}"
echo -e "  ${DIM}The Orbit statusline enhances Claude Code with a rich status display:${NC}"
echo -e "  ${DIM}  - Active project name and progress${NC}"
echo -e "  ${DIM}  - Git branch and dirty status${NC}"
echo -e "  ${DIM}  - Model, token usage, and context percentage${NC}"
echo -e "  ${DIM}  - Session time and edit count${NC}"
echo -e "  ${DIM}  - Kubernetes context (if available)${NC}"
echo ""

if ask_yn "Would you like to install the statusline?" "Y"; then
    SCRIPTS_DIR="$HOME/.claude/scripts"
    mkdir -p "$SCRIPTS_DIR"

    # Create symlinks (not copies) to avoid dual-source-of-truth
    for f in statusline.py statusline-launcher.sh; do
        target="$ORBIT_REPO/statusline/$f"
        link="$SCRIPTS_DIR/$f"
        if [[ -L "$link" ]]; then
            rm "$link"
        elif [[ -f "$link" ]]; then
            mv "$link" "$link.bak"
            detail "Backed up existing $f -> $f.bak"
        fi
        ln -s "$target" "$link"
    done
    detail "Created symlinks in ~/.claude/scripts/"

    # Update settings.json with statusline command.
    # Create an empty settings.json if the user has never run Claude Code yet - both this
    # write and the health-services write below assume the file exists.
    SETTINGS="$HOME/.claude/settings.json"
    if [[ ! -f "$SETTINGS" ]]; then
        mkdir -p "$(dirname "$SETTINGS")"
        echo "{}" > "$SETTINGS"
        detail "Created empty settings.json"
    fi
    "$PYTHON" -c "
import json
with open('$SETTINGS') as f:
    d = json.load(f)
d['statusLine'] = {
    'type': 'command',
    'command': '$PYTHON $SCRIPTS_DIR/statusline.py'
}
with open('$SETTINGS', 'w') as f:
    json.dump(d, f, indent=2)
"
    detail "Updated settings.json with statusline command"

    echo ""
    echo -e "  ${DIM}Statusline visibility (Codex, Claude subscription, Claude status,${NC}"
    echo -e "  ${DIM}status services) is configured from the dashboard Settings screen:${NC}"
    echo -e "  ${DIM}  http://localhost:8787/#settings${NC}"
    echo -e "  ${DIM}Defaults: everything visible, monitoring Code + Claude API.${NC}"

    success "Statusline installed"
else
    detail "Skipped statusline installation"
fi

# ─── Step 7: Claude Rules ────────────────────────────────────────────────────
step 7 "Claude Rules"
echo -e "  ${DIM}Orbit ships rule files that teach Claude how to use the plugin effectively:${NC}"
echo -e "  ${DIM}  - Skill reference, context preservation patterns${NC}"
echo -e "  ${DIM}  - Session resolution logic for statusline integration${NC}"
echo -e "  ${DIM}  - Compatible with any terminal (Ghostty, iTerm2, Windows Terminal)${NC}"
echo -e "  ${DIM}Installs to ~/.claude/rules/ as symlinks so updates flow automatically.${NC}"
echo ""

if ask_yn "Install Orbit rule files into ~/.claude/rules/?" "Y"; then
    RULES_SRC="$ORBIT_REPO/rules"
    RULES_DST="$HOME/.claude/rules"
    mkdir -p "$RULES_DST"

    if [[ -d "$RULES_SRC" ]]; then
        (
            shopt -s nullglob
            for src in "$RULES_SRC"/*.md; do
                fname=$(basename "$src")
                link="$RULES_DST/$fname"
                if [[ -L "$link" ]]; then
                    current_target=$(readlink "$link")
                    if [[ "$current_target" == "$src" ]]; then
                        detail "Already linked: $fname"
                        continue
                    fi
                    rm "$link"
                elif [[ -f "$link" ]]; then
                    mv "$link" "$link.bak"
                    detail "Backed up existing $fname -> $fname.bak"
                elif [[ -e "$link" ]]; then
                    warn "$link exists but is not a regular file - skipping $fname"
                    continue
                fi
                ln -s "$src" "$link"
                detail "Linked $fname -> $src"
            done
        )
        success "Rule files installed"
    else
        warn "Rules directory not found at $RULES_SRC - skipping"
    fi
else
    detail "Skipped rule installation"
fi

# ─── Step 8: User-Level Slash Commands ───────────────────────────────────────
step 8 "User-Level Slash Commands"
echo -e "  ${DIM}Orbit ships /whats-new - a slash command that scans the Claude Code${NC}"
echo -e "  ${DIM}changelog since the version you last reviewed and marks that version${NC}"
echo -e "  ${DIM}as seen so the statusline can render the version indicator in green.${NC}"
echo ""

if ask_yn "Install user-level slash commands into ~/.claude/commands/?" "Y"; then
    CMDS_SRC="$ORBIT_REPO/user-commands"
    CMDS_DST="$HOME/.claude/commands"
    mkdir -p "$CMDS_DST"

    if [[ -d "$CMDS_SRC" ]]; then
        (
            shopt -s nullglob
            for src in "$CMDS_SRC"/*.md; do
                fname=$(basename "$src")
                link="$CMDS_DST/$fname"
                if [[ -L "$link" ]]; then
                    current_target=$(readlink "$link")
                    if [[ "$current_target" == "$src" ]]; then
                        detail "Already linked: $fname"
                        continue
                    fi
                    rm "$link"
                elif [[ -f "$link" ]]; then
                    mv "$link" "$link.bak"
                    detail "Backed up existing $fname -> $fname.bak"
                elif [[ -e "$link" ]]; then
                    warn "$link exists but is not a regular file - skipping $fname"
                    continue
                fi
                ln -s "$src" "$link"
                detail "Linked $fname -> $src"
            done
        )
        success "User-level slash commands installed"
    else
        warn "user-commands directory not found at $CMDS_SRC - skipping"
    fi
else
    detail "Skipped user-level slash command installation"
fi

# ─── Open Dashboard ──────────────────────────────────────────────────────────
echo ""
info "Opening Orbit Dashboard..."
sleep 2  # Give the service a moment to start
if [[ "$(uname)" == "Darwin" ]]; then
    open "http://localhost:8787" 2>/dev/null || true
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:8787" 2>/dev/null || true
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ┌─────────────────────────────────────────┐${NC}"
echo -e "${BOLD}${GREEN}  │         Setup complete!                  │${NC}"
echo -e "${BOLD}${GREEN}  └─────────────────────────────────────────┘${NC}"
echo ""
echo -e "  ${BOLD}Get started:${NC}"
echo -e "    ${ARROW} Create a project:  ${CYAN}/orbit:new my-project${NC}"
echo -e "    ${ARROW} Resume work:       ${CYAN}/orbit:go${NC}"
echo -e "    ${ARROW} Save progress:     ${CYAN}/orbit:save${NC}"
echo -e "    ${ARROW} Complete project:  ${CYAN}/orbit:done${NC}"
echo ""
echo -e "  ${BOLD}Dashboard:${NC}  ${CYAN}http://localhost:8787${NC}"
echo -e "  ${BOLD}Docs:${NC}       ${CYAN}$ORBIT_REPO/README.md${NC}"
echo ""
