#!/usr/bin/env python3
"""Pane-attention checks for the Claude harness (wide and narrow panes)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.argv = [sys.argv[0]]

import agent_watcher as aw  # noqa: E402

RULE = "─" * 80
WIDE_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"
NARROW_FOOTER = (
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · gh auth login for PR status · es…"
)
IDLE_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def pane(*body, footer):
    return "\n".join(["⏺ Earlier reply.", "", *body, "", RULE, "❯ ", RULE, footer])


CASES = [
    # (label, disk state, pane text, want)
    (
        "wide busy footer keeps thinking",
        "thinking",
        pane("✶ Cogitating… (12s · ↑ 1.2k tokens)", footer=WIDE_FOOTER),
        "thinking",
    ),
    (
        "narrow pane: spinner keeps thinking",
        "thinking",
        pane("✽ Shimmying… (58s · ↓ 5.0k tokens)", footer=NARROW_FOOTER),
        "thinking",
    ),
    (
        "narrow pane: spinner + tip keeps running-tool",
        "running-tool",
        pane(
            "· Shimmying… (1m 25s · ↓ 7.6k tokens · thought for 7s)",
            "  ⎿  Tip: Use /btw to ask a quick side question without interrupting",
            "     work",
            footer=NARROW_FOOTER,
        ),
        "running-tool",
    ),
    (
        "finished turn demotes stale thinking",
        "thinking",
        pane("✻ Cooked for 1m 2s", footer=IDLE_FOOTER),
        "idle",
    ),
    (
        "no spinner, no footer demotes running-tool",
        "running-tool",
        pane("⏺ Done.", footer=IDLE_FOOTER),
        "idle",
    ),
    (
        "spinner-like text quoted mid-line does not count",
        "thinking",
        pane("     for s in ['✶ Cogitating… (2m 3s ·…", footer=IDLE_FOOTER),
        "idle",
    ),
    (
        "spinner never invents busy from idle disk",
        "idle",
        pane("✽ Shimmying… (58s · ↓ 5.0k tokens)", footer=NARROW_FOOTER),
        "idle",
    ),
]


def main():
    ok = True
    for label, state, text, want in CASES:
        got = aw.apply_pane_attention(state, text, "claude")
        if got != want:
            print("FAIL  %s: got=%r want=%r" % (label, got, want))
            ok = False
        else:
            print("ok   %s → %s" % (label, want))
    if not ok:
        sys.exit(1)
    print("\nall ok")


if __name__ == "__main__":
    main()
