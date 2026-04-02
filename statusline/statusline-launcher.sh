#!/bin/bash
# Statusline launcher - captures terminal width before pipe starts

# Suppress ALL stderr from this script to prevent display corruption
exec 2>/dev/null

# Try to get terminal width from /dev/tty (most reliable when available)
if [[ -c /dev/tty ]] && [[ -r /dev/tty ]]; then
    COLUMNS=$(stty size </dev/tty 2>/dev/null | cut -d' ' -f2)
fi

# Fallback to tput if stty failed
if [[ -z "$COLUMNS" || "$COLUMNS" -eq 0 ]]; then
    COLUMNS=$(tput cols 2>/dev/null)
fi

# Final fallback
if [[ -z "$COLUMNS" || "$COLUMNS" -eq 0 ]]; then
    COLUMNS=120
fi

export COLUMNS

# Run the main statusline script with all arguments
exec /home/user/.claude/scripts/statusline-wrapper.sh "$@"
