#!/usr/bin/env python3
"""Agent kind detection from a pane's command line, including sandbox wrappers."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.argv = [sys.argv[0]]

import agent_watcher as aw  # noqa: E402

CASES = [
    # (label, command, want)
    ("bare claude", "claude", "claude"),
    ("claude by path", "/home/u/.local/bin/claude --resume x", "claude"),
    ("npm claude-code under node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "claude"),
    ("claude under bwrap", "bwrap --ro-bind / / --dev /dev -- /usr/bin/claude", "claude"),
    ("codex under sandbox wrapper", "sandbox-exec -f p.sb /opt/bin/codex", "codex"),
    ("opencode", "opencode", "opencode"),
    ("open-code alias", "open-code run", "opencode"),
    ("pi", "pi --model x", "pi"),
    ("copilot", "copilot", "copilot"),
    ("copilot npm path", "node /usr/lib/node_modules/@github/copilot/index.js", "copilot"),
    ("cursor-agent", "cursor-agent", "cursor"),
    ("cursor anysphere bundle", "node /home/u/.local/share/anysphere/agent.js", "cursor"),
    ("cursor `agent` symlink", "/home/u/.local/bin/agent", "cursor"),
    ("cursor wins over claude mention", "cursor-agent --model claude", "cursor"),
    ("flag text is not a basename", "vim --claude", None),
    ("flag text cannot be `agent`", "ssh --agent host", None),
    ("substring of a basename is not a match", "claudette", None),
    ("plain shell", "-zsh", None),
    ("empty", "", None),
    ("none", None, None),
]


def main():
    ok = True
    for label, command, want in CASES:
        got = aw.detect_kind(command)
        if got != want:
            print("FAIL  %s: got=%r want=%r" % (label, got, want))
            ok = False
        else:
            print("ok    %s → %s" % (label, want))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
