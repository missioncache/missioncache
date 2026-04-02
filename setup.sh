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
    fail "Python 3.11+ is required but not found"
fi
success "Python $($PYTHON --version 2>&1 | awk '{print $2}')"

# pip
if ! "$PYTHON" -m pip --version &>/dev/null; then
    fail "pip is required but not found (try: $PYTHON -m ensurepip)"
fi
success "pip"

# Claude Code CLI
if command -v claude &>/dev/null; then
    success "Claude Code CLI"
else
    warn "Claude Code CLI not found - plugin installation will be skipped"
    warn "Install from: https://claude.ai/code"
fi

# ─── Step 1: Core Plugin ─────────────────────────────────────────────────────
step 1 "Core Plugin"
info "Installing Orbit plugin for Claude Code..."

# Create data directories
mkdir -p "$ORBIT_ROOT/active" "$ORBIT_ROOT/completed"
detail "Created $ORBIT_ROOT/{active,completed}/"

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

success "Core plugin installed"

# ─── Step 2: Orbit Database ──────────────────────────────────────────────────
step 2 "Orbit Database"
info "Installing orbit-db for task tracking..."

"$PYTHON" -m pip install -e "$ORBIT_REPO/orbit-db" --quiet 2>/dev/null
detail "Installed orbit-db (editable mode)"

# Initialize database if it doesn't exist
"$PYTHON" -c "from orbit_db import TaskDB; TaskDB()" 2>/dev/null
detail "Database initialized at ~/.claude/tasks.db"

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
"$PYTHON" -m pip install fastapi uvicorn duckdb --quiet 2>/dev/null
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

"$PYTHON" -m pip install -e "$ORBIT_REPO/orbit-auto" --quiet 2>/dev/null
detail "Installed orbit-auto (editable mode)"

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
    uvx --from "$ORBIT_REPO/mcp-server" mcp-orbit --help &>/dev/null 2>&1 && detail "MCP server venv built" || detail "MCP server will build on first use"
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

    # Update settings.json with statusline command
    SETTINGS="$HOME/.claude/settings.json"
    if [[ -f "$SETTINGS" ]]; then
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
    fi

    success "Statusline installed"
else
    detail "Skipped statusline installation"
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
