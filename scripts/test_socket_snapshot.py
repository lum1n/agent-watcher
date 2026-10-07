#!/usr/bin/env python3
"""Socket snapshot requests are answered at once from current state; stdin is unchanged."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHER = os.path.join(ROOT, "src", "agent_watcher.py")


def load():
    spec = importlib.util.spec_from_file_location("agent_watcher_under_test", WATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_lines(conn):
    conn.settimeout(0.5)
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    return [json.loads(line) for line in buf.decode().splitlines() if line]


def main() -> int:
    mod = load()
    failed = 0

    def ok(name, cond):
        nonlocal failed
        print(("ok    " if cond else "FAIL  ") + name)
        failed += not cond

    stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        a, b = socket.socketpair()
        mod.handle_control_line('{"cmd":"snapshot"}', source="socket", conn=a)
        a.close()
        before = read_lines(b)
        mod._snapshot_requested.clear()

        mod.emit({"type": "snapshot", "agents": [
            {"session": "dev", "window": 0, "kind": "claude", "path": "/p", "state": "idle"},
            {"session": "dev", "window": 1, "kind": "codex", "unbound": True},
            {"session": "old", "window": 2, "kind": "pi", "path": "/q", "state": "idle"}]})
        mod.emit({"type": "state", "session": "dev", "window": 0, "kind": "claude", "state": "thinking",
                  "toolName": None, "path": "/p", "sessionId": "s", "ts": 1, "attached": True, "windows": 1})
        mod.emit({"type": "unbound", "session": "new", "window": 3, "kind": "copilot", "reason": "x",
                  "attached": False, "windows": 1})
        mod.emit({"type": "gone", "session": "old", "window": 2})
        emitted = sys.stdout.getvalue()

        a, b = socket.socketpair()
        mod.handle_control_line('{"cmd":"snapshot"}', source="socket", conn=a)
        a.close()
        replies = read_lines(b)
        socket_requested = mod._snapshot_requested.is_set()
        mod._snapshot_requested.clear()
        mod.handle_control_line('{"cmd":"snapshot"}', source="stdin")
        stdin_requested = mod._snapshot_requested.is_set()
        after = sys.stdout.getvalue()
    finally:
        sys.stdout = stdout

    ok("no reply before the first snapshot", before == [])
    ok("one immediate snapshot reply", [r.get("type") for r in replies] == ["snapshot"])
    rows = {(r["session"], r["window"]): r for r in replies[0]["agents"]} if replies else {}
    ok("state event updates the row", rows.get(("dev", 0), {}).get("state") == "thinking")
    ok("row drops event-only fields", not {"type", "v", "ts", "sessionId", "toolName"} & set(rows.get(("dev", 0), {})))
    ok("unbound stays unbound", rows.get(("dev", 1), {}).get("unbound") is True)
    ok("unbound event adds unbound row", rows.get(("new", 3), {}).get("unbound") is True)
    ok("gone removes the row", ("old", 2) not in rows)
    ok("reply is v1 and marked cached", replies and replies[0].get("v") == 1 and replies[0].get("cached") is True)
    ok("socket request still triggers a rescan", socket_requested)
    ok("stdin request still triggers a rescan", stdin_requested)
    ok("requests write nothing to stdout", after == emitted)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
