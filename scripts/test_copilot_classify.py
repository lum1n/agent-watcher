#!/usr/bin/env python3
"""Offline classify checks for the Copilot CLI harness."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHER = os.path.join(ROOT, "src", "agent_watcher.py")


def ts_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_events(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def classify(path):
    env = os.environ.copy()
    env["SESSH_CLASSIFY_KIND"] = "copilot"
    env["SESSH_SESSION_PATH"] = path
    out = subprocess.check_output(
        [sys.executable, WATCHER, "--classify"],
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )
    return json.loads(out.strip().splitlines()[-1])


def expect(label, got, want_state, tool_name=None):
    if got.get("state") != want_state:
        print("FAIL  %s: state=%r want=%r full=%s" % (label, got.get("state"), want_state, got))
        return False
    if tool_name is not None and got.get("toolName") != tool_name:
        print(
            "FAIL  %s: toolName=%r want=%r full=%s"
            % (label, got.get("toolName"), tool_name, got)
        )
        return False
    print("ok   %s → %s" % (label, want_state))
    return True


def detect_kind_check():
    # Import detect via a tiny exec of the watcher helpers would be heavy;
    # shell out through Python loading the module as a script namespace.
    code = (
        "import runpy,sys;"
        "g=runpy.run_path(sys.argv[1]);"
        "print(g['detect_kind'](sys.argv[2]) or '')"
    )
    cases = [
        ("copilot", "copilot"),
        ("/usr/bin/copilot --resume=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "copilot"),
        ("node /home/u/node_modules/@github/copilot/index.js", "copilot"),
        ("claude", "claude"),
        ("codex", "codex"),
    ]
    ok = True
    for cmd, want in cases:
        out = subprocess.check_output(
            [sys.executable, "-c", code, WATCHER, cmd],
            text=True,
        ).strip()
        if out != want:
            print("FAIL  detect_kind(%r)=%r want=%r" % (cmd, out, want))
            ok = False
        else:
            print("ok   detect_kind(%r) → %s" % (cmd, want))
    return ok


def main() -> int:
    failed = 0
    if not detect_kind_check():
        failed += 1

    with tempfile.TemporaryDirectory(prefix="copilot-classify-") as tmp:
        path = os.path.join(tmp, "events.jsonl")
        base = [
            {
                "type": "session.start",
                "data": {
                    "sessionId": "11111111-2222-3333-4444-555555555555",
                    "context": {"cwd": tmp},
                },
                "timestamp": ts_now(),
            },
            {
                "type": "user.message",
                "data": {"content": "list files in this directory please"},
                "timestamp": ts_now(),
            },
        ]

        # After user message → thinking
        write_events(path, base)
        # Touch mtime so age_out_busy does not demote.
        os.utime(path, None)
        got = classify(path)
        if not expect("after user.message", got, "thinking"):
            failed += 1
        if got.get("summary") != "list files in this directory please":
            # Cap is 80; this fits.
            print("FAIL  summary missing: %s" % got)
            failed += 1
        else:
            print("ok   summary extracted")

        # Running tool
        write_events(
            path,
            base
            + [
                {
                    "type": "assistant.turn_start",
                    "data": {"turnId": "0"},
                    "timestamp": ts_now(),
                },
                {
                    "type": "assistant.message",
                    "data": {
                        "content": "I'll list the files.",
                        "toolRequests": [
                            {
                                "toolCallId": "t1",
                                "name": "report_intent",
                                "arguments": {"intent": "list"},
                            },
                            {
                                "toolCallId": "t2",
                                "name": "bash",
                                "arguments": {"command": "ls -la"},
                            },
                        ],
                    },
                    "timestamp": ts_now(),
                },
                {
                    "type": "tool.execution_start",
                    "data": {
                        "toolCallId": "t2",
                        "toolName": "bash",
                        "arguments": {"command": "ls -la"},
                    },
                    "timestamp": ts_now(),
                },
            ],
        )
        os.utime(path, None)
        got = classify(path)
        if not expect("tool.execution_start", got, "running-tool", tool_name="bash"):
            failed += 1
        if got.get("toolTarget") != "ls -la":
            print("FAIL  toolTarget=%r" % got.get("toolTarget"))
            failed += 1
        else:
            print("ok   toolTarget=ls -la")

        # Permission prompt
        write_events(
            path,
            base
            + [
                {
                    "type": "assistant.message",
                    "data": {
                        "content": "",
                        "toolRequests": [
                            {
                                "toolCallId": "t3",
                                "name": "bash",
                                "arguments": {"command": "rm -rf /tmp/x"},
                            }
                        ],
                    },
                    "timestamp": ts_now(),
                },
                {
                    "type": "permission.requested",
                    "data": {"toolCallId": "t3"},
                    "timestamp": ts_now(),
                },
            ],
        )
        os.utime(path, None)
        got = classify(path)
        if not expect("permission.requested", got, "waiting-permission"):
            failed += 1

        # Idle after final assistant message
        write_events(
            path,
            base
            + [
                {
                    "type": "assistant.message",
                    "data": {"content": "Here are the files.", "toolRequests": []},
                    "timestamp": ts_now(),
                },
                {
                    "type": "assistant.turn_end",
                    "data": {"turnId": "0"},
                    "timestamp": ts_now(),
                },
            ],
        )
        os.utime(path, None)
        got = classify(path)
        if not expect("assistant.turn_end", got, "idle"):
            failed += 1

        # Missing path
        got = classify(os.path.join(tmp, "missing.jsonl"))
        if not expect("missing path", got, "idle"):
            failed += 1
        if not got.get("pathMissing"):
            print("FAIL  pathMissing not set: %s" % got)
            failed += 1
        else:
            print("ok   pathMissing on absent file")

        # Stale busy demoted by age_out_busy
        stale = os.path.join(tmp, "stale.jsonl")
        write_events(
            stale,
            base
            + [
                {
                    "type": "tool.execution_start",
                    "data": {
                        "toolCallId": "t9",
                        "toolName": "bash",
                        "arguments": {"command": "sleep 1"},
                    },
                    # Old timestamp so age_out_busy fires even if mtime is fresh —
                    # classify prefers event timestamp when present.
                    "timestamp": "2000-01-01T00:00:00.000Z",
                },
            ],
        )
        # Also age the file mtime in case timestamp parsing fails.
        old = time.time() - 200
        os.utime(stale, (old, old))
        got = classify(stale)
        if not expect("stale running-tool demoted", got, "idle"):
            failed += 1

    if failed:
        print("%d check(s) failed" % failed)
        return 1
    print("all copilot classify checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
