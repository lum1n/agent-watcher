#!/usr/bin/env python3
"""Exited agents get `gone`, phone binds need a live agent, and leaving clients are dropped quietly."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHER = os.path.join(ROOT, "src", "agent_watcher.py")


def load():
    spec = importlib.util.spec_from_file_location("agent_watcher_under_test", WATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load()
    failed = 0

    def ok(name, cond):
        nonlocal failed
        print(("ok    " if cond else "FAIL  ") + name)
        failed += not cond

    def events(out):
        return [json.loads(line) for line in out.getvalue().splitlines() if line]

    key = mod.agent_key("dev", 0)
    agents = []
    panes = {key: {"session": "dev", "window": 0, "attached": True, "windows": 1}}
    mod.scan_agents = lambda: list(agents)
    mod.list_live_pane_keys = lambda: dict(panes)
    mod.classify_agent = lambda *a, **k: {"state": "idle", "ageMs": 0}

    fd, store = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        with mod._lock:
            mod._bound[key] = {"kind": "claude", "session": "dev", "window": 0, "path": None}
        mod.rediscover()
        first = events(sys.stdout)
        with mod._lock:
            still_bound = key in mod._bound
        mod.rediscover()
        second = events(sys.stdout)[len(first):]
        with mod._lock:
            dropped = key not in mod._bound

        with mod._lock:
            mod._bound[key] = {"kind": "claude", "session": "dev", "window": 0, "path": None}
        mod.rediscover()
        agents.append({"session": "dev", "window": 0, "kind": "claude", "attached": True, "windows": 1})
        mod._run_discover = lambda info, claimed: (None, None)
        mod.rediscover()
        agents.clear()
        mod.rediscover()
        mark = len(events(sys.stdout))
        with mod._lock:
            regrace = key in mod._bound

        with mod._lock:
            mod._bound.clear()
        mod._absent.clear()
        panes.clear()
        with mod._lock:
            mod._bound["old:w3"] = {"kind": "pi", "session": "old", "window": 3}
        mod.rediscover()
        closed = events(sys.stdout)[mark:]

        panes[key] = {"session": "dev", "window": 0, "attached": True, "windows": 1}
        bind = json.dumps({"cmd": "bind", "session": "dev", "window": 0, "kind": "claude", "path": store})
        with mod._lock:
            mod._bound.clear()
        mark = len(events(sys.stdout))
        mod.handle_control_line(bind)
        agents.append({"session": "dev", "window": 0, "kind": "codex", "attached": True, "windows": 1})
        mod.handle_control_line(bind)
        with mod._lock:
            rejected = key not in mod._bound
        rejected_out = events(sys.stdout)[mark:]
        agents[:] = [{"session": "dev", "window": 0, "kind": "claude", "attached": True, "windows": 1}]
        mod.handle_control_line(bind)
        with mod._lock:
            adopted = mod._bound.get(key, {}).get("path") == store
    finally:
        sys.stdout = stdout
        os.unlink(store)

    ok("first scan without the agent keeps the bind", still_bound and not any(e["type"] == "gone" for e in first))
    ok("second scan without the agent emits gone",
       [(e["type"], e.get("session"), e.get("window")) for e in second] == [("gone", "dev", 0)] and dropped)
    ok("agent coming back resets the grace", regrace)
    ok("closed pane is gone at once", [(e["type"], e.get("session")) for e in closed] == [("gone", "old")])
    ok("bind ignored without any agent in the pane", rejected and rejected_out == [])
    ok("bind adopted for a live agent of that kind", adopted)

    mod._stop.clear()
    a, b = socket.socketpair()
    b.close()
    raised = None
    try:
        mod.socket_client_loop(a)
    except Exception as e:  # noqa: BLE001
        raised = e
    ok("client that left mid-hello raises nothing", raised is None)
    ok("client that left is dropped and closed", a not in mod._clients and a.fileno() == -1)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
