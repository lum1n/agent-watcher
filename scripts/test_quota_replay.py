#!/usr/bin/env python3
"""New socket subscribers get the last quota reading; nothing goes to stdout."""
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
    conn.settimeout(1)
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
    window = {"id": "session", "label": "5h", "usedPercent": 33.0}
    mod._quota_last.update({
        "codex": {"fp": "a", "payload": {"plan": "plus", "windows": [window]}, "stale": False},
        "claude": {"fp": "b", "payload": {"windows": [window]}, "stale": True, "err": "auth-expired"},
    })
    stdout, sys.stdout = sys.stdout, io.StringIO()
    server, client = socket.socketpair()
    try:
        mod.replay_quota(server)
        server.close()
        written = sys.stdout.getvalue()
    finally:
        sys.stdout = stdout
    events = read_lines(client)
    failed = 0

    def ok(name, cond):
        nonlocal failed
        print(("ok    " if cond else "FAIL  ") + name)
        failed += not cond

    ok("replay writes nothing to stdout", written == "")
    ok("one event per kind, sorted", [e.get("kind") for e in events] == ["claude", "codex"])
    ok("events are v1 quota", all(e.get("v") == 1 and e.get("type") == "quota" for e in events))
    ok("stale reading keeps reason", events[0].get("stale") is True and events[0].get("reason") == "auth-expired")
    ok("fresh reading keeps plan", events[1].get("plan") == "plus" and events[1].get("stale") is False)
    mod._quota_last.clear()
    a, b = socket.socketpair()
    mod.replay_quota(a)
    a.close()
    ok("no readings, no replay", read_lines(b) == [])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
