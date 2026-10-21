#!/usr/bin/env python3
"""Claude usage parsing, plan labels, and credential lookup (file, then env)."""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.argv = [sys.argv[0]]

import agent_watcher as aw  # noqa: E402


def main() -> int:
    failed = 0

    def ok(name, cond):
        nonlocal failed
        print(("ok    " if cond else "FAIL  ") + name)
        failed += not cond

    ok("max 5x plan label", aw.claude_plan_label("max", "default_claude_max_5x") == "Max 5x")
    ok("plan label without tier multiplier", aw.claude_plan_label(" Pro ", "default_claude_pro") == "Pro")
    ok("plan label with no tier", aw.claude_plan_label("team", None) == "Team")
    ok("no subscription, no label", aw.claude_plan_label("", "default_claude_max_20x") is None)
    ok("non-string subscription, no label", aw.claude_plan_label(None, None) is None)

    parsed = aw.parse_claude_usage({"limits": [
        {"kind": "session", "percent": 12, "resets_at": "2026-10-19T20:00:00Z"},
        {"kind": "weekly_all", "utilization": 0.5},
        {"kind": "weekly_scoped", "percent": 80, "scope": {"model": {"display_name": "Fable 5"}}},
        {"kind": "weekly_scoped", "percent": 30, "scope": {"surface": "Claude Code"}},
        {"kind": "weekly_scoped", "percent": 5},
        {"kind": "session"},
        "junk",
    ]})
    windows = (parsed or {}).get("windows") or []
    by_id = {w["id"]: w for w in windows}
    ok("session window from `kind` + `percent`",
       by_id.get("session") == {"id": "session", "label": "5h", "usedPercent": 12.0,
                                "resetsAt": "2026-10-19T20:00:00Z"})
    ok("weekly window from fractional utilization", by_id.get("week", {}).get("usedPercent") == 50.0)
    ok("model scope names the weekly cap",
       by_id.get("week-fable-5", {}).get("label") == "Fable 5"
       and by_id["week-fable-5"]["usedPercent"] == 80.0)
    ok("surface scope names the weekly cap", by_id.get("week-claude-code", {}).get("label") == "Claude Code")
    ok("unnamed scoped cap falls back to model", by_id.get("week-model", {}).get("label") == "model")
    ok("entries without a percent are dropped", len(windows) == 5)

    long_name = aw.parse_claude_usage({"limits": [
        {"kind": "weekly_scoped", "percent": 1, "display_name": "A very long model name indeed"}]})
    ok("scoped window id is capped", long_name["windows"][0]["id"] == "week-" + "a-very-long-model-name-indeed"[:24])
    legacy = aw.parse_claude_usage({"five_hour": {"utilization": 40}, "seven_day_opus": {"utilization": 10}})
    ok("legacy buckets still parse", [w["id"] for w in legacy["windows"]] == ["session", "week-opus"])
    ok("nothing usable → None", aw.parse_claude_usage({"limits": [{"kind": "session"}]}) is None)

    saved = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CLAUDE_CONFIG_DIR"] = tmp
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            creds = os.path.join(tmp, ".credentials.json")
            with open(creds, "w", encoding="utf-8") as f:
                json.dump({"claudeAiOauth": {"accessToken": " tok-file ", "subscriptionType": "max",
                                             "rateLimitTier": "default_claude_max_20x"}}, f)
            ok("token from credentials file", aw.read_claude_access_token() == "tok-file")
            ok("plan from credentials file", aw.read_claude_plan() == "Max 20x")

            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "tok-env"
            ok("file token beats env token", aw.read_claude_access_token() == "tok-file")

            with open(creds, "w", encoding="utf-8") as f:
                json.dump({"accessToken": "tok-flat"}, f)
            ok("flat token in credentials file", aw.read_claude_access_token() == "tok-flat")
            ok("no oauth block, no plan", aw.read_claude_plan() is None)

            os.unlink(creds)
            if sys.platform != "darwin":
                ok("env token when no credentials file", aw.read_claude_access_token() == "tok-env")
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "  "
                ok("blank env token → None", aw.read_claude_access_token() is None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
