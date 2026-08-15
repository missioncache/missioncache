"""Boot the MCP server the way Claude Code does - via uvx over stdio - and
answer one JSON-RPC initialize. The pytest suite imports the server code but
never exercises the uvx spawn path, which is the single most load-bearing
Windows component (a .cmd-shim or console issue would pass every unit test and
still fail to launch). Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _send(proc, obj) -> None:
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def main() -> int:
    # uvx --from ./mcp-server mcp-missioncache: the exact spawn shape plugin.json
    # uses, resolving the local build against the sibling missioncache-db.
    cmd = [
        "uvx", "--from", str(REPO / "mcp-server"),
        "--with", str(REPO / "missioncache-db"),
        "mcp-missioncache",
    ]
    print("spawning:", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ci-smoke", "version": "0"},
            },
        })

        # Read lines until we see the initialize result or time out.
        deadline = time.monotonic() + 60
        got_result = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue  # server log line, not a JSON-RPC frame
            if msg.get("id") == 1 and "result" in msg:
                server_info = msg["result"].get("serverInfo", {})
                print("initialize OK, serverInfo:", server_info)
                got_result = True
                break
        if not got_result:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            print("no initialize result; stderr:\n", err, file=sys.stderr)
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
