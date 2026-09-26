"""Subscription usage probes for Claude / Codex / Cursor.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.
Emits normalized `quota` payloads (percents + resets) — never tokens.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUOTA_KINDS = frozenset(("claude", "codex", "cursor"))
QUOTA_INTERVAL_S = 180.0
_QUOTA_HTTP_TIMEOUT_S = 12.0

# last good payload per kind: {payload, fingerprint, fetched_at}
_quota_cache = {}


def _as_float(raw):
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except Exception:
            return None
    return None


def normalize_percent(raw):
    """Accept 0–1 or 0–100; return 0–100 or None."""
    v = _as_float(raw)
    if v is None:
        return None
    if 0.0 <= v <= 1.0:
        v = v * 100.0
    if v < 0.0:
        v = 0.0
    if v > 100.0:
        v = 100.0
    return round(v, 1)


def _iso_reset(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # Numeric string (unix s / ms) from Cursor billingCycleEnd
        if re.fullmatch(r"\d+(\.\d+)?", s):
            try:
                raw = float(s)
            except Exception:
                return s
        else:
            return s
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            return None
    return None


def _window(wid, label, used_percent, resets_at=None):
    pct = normalize_percent(used_percent)
    if pct is None:
        return None
    out = {"id": wid, "label": label, "usedPercent": pct}
    reset = _iso_reset(resets_at)
    if reset:
        out["resetsAt"] = reset
    return out


def _window_label_from_mins(mins):
    if mins is None:
        return None
    try:
        m = int(mins)
    except Exception:
        return None
    if 240 <= m <= 360:
        return "5h"
    if 1400 <= m <= 1600:
        return "1d"
    if 9000 <= m <= 11000:
        return "7d"
    if 40000 <= m <= 46000:
        return "30d"
    if m >= 60 and m % 60 == 0:
        return "%dh" % (m // 60)
    if m > 0:
        return "%dm" % m
    return None


def _window_label_from_secs(secs):
    if secs is None:
        return None
    try:
        s = int(secs)
    except Exception:
        return None
    return _window_label_from_mins(s // 60 if s >= 60 else 0) or (
        "%ds" % s if s > 0 else None
    )


# ── Parsers (pure; fixture-tested) ───────────────────────────────────────


def parse_claude_usage(raw):
    """Map Anthropic oauth/usage JSON → quota payload or None."""
    if not raw or not isinstance(raw, dict):
        return None
    windows = []
    limits = raw.get("limits")
    if isinstance(limits, list) and limits:
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or entry.get("id") or "").strip().lower()
            name = str(entry.get("display_name") or entry.get("name") or "").strip()
            util = entry.get("utilization")
            if util is None:
                util = entry.get("used_percent")
            if util is None:
                util = entry.get("usedPercent")
            resets = (
                entry.get("resets_at")
                or entry.get("resetsAt")
                or entry.get("reset_at")
            )
            if kind in ("session", "five_hour", "five-hour", "5h"):
                wid, label = "session", name or "5h"
            elif kind in ("weekly_all", "seven_day", "seven-day", "7d", "week"):
                wid, label = "week", name or "7d"
            elif kind in ("weekly_scoped", "seven_day_opus", "seven_day_sonnet"):
                wid = "week-" + (kind.replace("weekly_scoped", "model")[:24] or "model")
                label = name or "model"
            else:
                wid = kind or "limit"
                label = name or wid
            w = _window(wid, label, util, resets)
            if w:
                windows.append(w)
    if not windows:
        for key, wid, label in (
            ("five_hour", "session", "5h"),
            ("seven_day", "week", "7d"),
            ("seven_day_opus", "week-opus", "Opus"),
            ("seven_day_sonnet", "week-sonnet", "Sonnet"),
        ):
            bucket = raw.get(key)
            if not isinstance(bucket, dict):
                continue
            w = _window(
                wid,
                label,
                bucket.get("utilization"),
                bucket.get("resets_at") or bucket.get("resetsAt"),
            )
            if w:
                windows.append(w)
    if not windows:
        return None
    plan = raw.get("plan") or raw.get("plan_type") or raw.get("planType")
    out = {"windows": windows}
    if isinstance(plan, str) and plan.strip():
        out["plan"] = plan.strip()
    return out


def parse_codex_usage(raw):
    """Map ChatGPT wham/usage (or nested rate_limit) → quota payload."""
    if not raw or not isinstance(raw, dict):
        return None
    rate = raw.get("rate_limit")
    if not isinstance(rate, dict):
        rate = raw.get("rateLimit") if isinstance(raw.get("rateLimit"), dict) else raw

    windows = []
    for key, default_id, default_label in (
        ("primary_window", "session", "5h"),
        ("secondary_window", "week", "7d"),
        ("primary", "session", "5h"),
        ("secondary", "week", "7d"),
    ):
        bucket = rate.get(key) if isinstance(rate, dict) else None
        if not isinstance(bucket, dict):
            continue
        used = bucket.get("used_percent")
        if used is None:
            used = bucket.get("usedPercent")
        mins = bucket.get("window_duration_mins")
        if mins is None:
            mins = bucket.get("windowDurationMins")
        if mins is None:
            secs = bucket.get("limit_window_seconds") or bucket.get(
                "limitWindowSeconds"
            )
            label = _window_label_from_secs(secs) or default_label
        else:
            label = _window_label_from_mins(mins) or default_label
        resets = (
            bucket.get("reset_at")
            or bucket.get("resets_at")
            or bucket.get("resetsAt")
            or bucket.get("resetAt")
        )
        wid = "session" if default_id == "session" else "week"
        # Avoid duplicates when both naming styles appear
        if any(w["id"] == wid for w in windows):
            continue
        w = _window(wid, label, used, resets)
        if w:
            windows.append(w)

    if not windows:
        return None
    plan = (
        raw.get("plan_type")
        or raw.get("planType")
        or (rate.get("planType") if isinstance(rate, dict) else None)
        or (rate.get("plan_type") if isinstance(rate, dict) else None)
    )
    out = {"windows": windows}
    if isinstance(plan, str) and plan.strip():
        out["plan"] = plan.strip()
    return out


def _percent_from_display_message(*msgs):
    """Parse 'You've used N% of your included usage' (Cursor's included axis)."""
    for msg in msgs:
        if not isinstance(msg, str):
            continue
        # Exhausted copy — do not scrape a bogus 100 from other text.
        low = msg.lower()
        if "hit your usage limit" in low or "reached your usage limit" in low:
            return 100.0
        m = re.search(
            r"used\s+(\d+(?:\.\d+)?)\s*%\s+of\s+your\s+included",
            msg,
            re.I,
        )
        if m:
            return float(m.group(1))
    return None


def parse_cursor_usage(raw):
    """Map GetCurrentPeriodUsage → included spend percent (not totalPercentUsed).

    Prefer displayMessage (same string Cursor's Plan & Usage uses). Fall back to
    includedSpend/limit, with a cents-vs-dollars guard and remaining cross-check.
    Never use totalPercentUsed — that is a different weighted pool.
    """
    if not raw or not isinstance(raw, dict):
        return None
    plan = raw.get("planUsage")
    if not isinstance(plan, dict):
        plan = raw.get("plan_usage")
    if not isinstance(plan, dict):
        return None

    included = _as_float(plan.get("includedSpend"))
    if included is None:
        included = _as_float(plan.get("included_spend"))
    remaining = _as_float(plan.get("remaining"))
    limit = _as_float(plan.get("limit"))

    used_pct = _percent_from_display_message(
        raw.get("displayMessage"),
        plan.get("displayMessage"),
    )

    if used_pct is None and included is not None and limit is not None and limit > 0:
        # Guard: included in cents, limit sometimes in whole dollars.
        adj_limit = limit
        if included > limit * 50 and limit < 1000:
            adj_limit = limit * 100.0
        used_pct = (included / adj_limit) * 100.0

    if used_pct is None and remaining is not None and limit is not None and limit > 0:
        adj_limit = limit
        if remaining > limit * 50 and limit < 1000:
            adj_limit = limit * 100.0
        used_pct = ((adj_limit - remaining) / adj_limit) * 100.0

    if used_pct is None:
        return None

    # remaining > 0 means the included bucket is not exhausted — trust it
    # over a saturating includedSpend/limit ratio (Cursor sometimes omits
    # consistency between those fields).
    if remaining is not None and remaining > 0 and used_pct >= 99.5:
        if limit is not None and limit > remaining:
            adj_limit = limit
            if remaining > limit * 50 and limit < 1000:
                adj_limit = limit * 100.0
            if adj_limit > remaining:
                used_pct = ((adj_limit - remaining) / adj_limit) * 100.0
        elif included is not None and included >= 0:
            used_pct = (included / (included + remaining)) * 100.0
        else:
            used_pct = 99.0

    resets = raw.get("billingCycleEnd") or raw.get("billing_cycle_end")
    w = _window("cycle", "plan", used_pct, resets)
    if not w:
        return None
    out = {"windows": [w]}
    plan_name = raw.get("planName") or raw.get("plan_name") or raw.get("plan")
    if isinstance(plan_name, str) and plan_name.strip():
        out["plan"] = plan_name.strip()
    return out


# ── Credentials (local files only; never emitted) ────────────────────────


def _claude_config_dir():
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude"


def read_claude_access_token():
    path = _claude_config_dir() / ".credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth") or data.get("claude_ai_oauth") or {}
    if isinstance(oauth, dict):
        tok = oauth.get("accessToken") or oauth.get("access_token")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    tok = data.get("accessToken") or data.get("access_token")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


def _codex_home():
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codex"


def read_codex_auth():
    """Return (access_token, account_id) or (None, None) for non-subscription."""
    path = _codex_home() / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    # API-key logins have no ChatGPT subscription allotment
    if data.get("OPENAI_API_KEY") and not (
        isinstance(data.get("tokens"), dict)
        and (data["tokens"].get("access_token") or data["tokens"].get("accessToken"))
    ):
        return None, None
    mode = str(data.get("auth_mode") or data.get("authMode") or "").lower()
    if mode in ("apikey", "api_key", "api-key"):
        return None, None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
    tok = tokens.get("access_token") or tokens.get("accessToken")
    if not isinstance(tok, str) or not tok.strip():
        return None, None
    acct = tokens.get("account_id") or tokens.get("accountId")
    if not isinstance(acct, str):
        acct = None
    return tok.strip(), (acct.strip() if acct else None)


def _cursor_auth_paths():
    paths = []
    cursor_cfg = os.environ.get("CURSOR_CONFIG_DIR", "").strip()
    if cursor_cfg:
        paths.append(Path(cursor_cfg) / "auth.json")
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    cfg = Path(xdg) if xdg else (Path.home() / ".config")
    paths.append(cfg / "cursor" / "auth.json")
    paths.append(Path.home() / ".cursor" / "auth.json")
    # de-dupe while preserving order
    seen = set()
    out = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def read_cursor_access_token():
    env = os.environ.get("CURSOR_API_KEY", "").strip()
    if env:
        return env
    for path in _cursor_auth_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        tok = data.get("accessToken") or data.get("access_token")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    return None


# ── HTTP ─────────────────────────────────────────────────────────────────


def _http_json(method, url, headers, body=None, timeout=None):
    timeout = _QUOTA_HTTP_TIMEOUT_S if timeout is None else timeout
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code, None, str(e.reason or e)
    except Exception as e:
        return None, None, str(e)
    try:
        parsed = json.loads(raw.decode("utf-8", "ignore") or "null")
    except Exception:
        return status, None, "invalid-json"
    return status, parsed, None


def fetch_claude_usage():
    tok = read_claude_access_token()
    if not tok:
        return None, "no-credentials"
    status, body, err = _http_json(
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": "Bearer " + tok,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.0.0",
            "Accept": "application/json",
        },
    )
    if status == 401:
        return None, "auth-expired"
    if status == 429:
        return None, "rate-limited"
    if status != 200 or not isinstance(body, dict):
        return None, err or ("http-%s" % status)
    parsed = parse_claude_usage(body)
    if not parsed:
        return None, "parse-failed"
    return parsed, None


def fetch_codex_usage():
    tok, account_id = read_codex_auth()
    if not tok:
        return None, "no-credentials"
    headers = {
        "Authorization": "Bearer " + tok,
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, body, err = _http_json(
        "GET",
        "https://chatgpt.com/backend-api/wham/usage",
        headers,
    )
    if status == 401:
        return None, "auth-expired"
    if status == 429:
        return None, "rate-limited"
    if status != 200 or not isinstance(body, dict):
        return None, err or ("http-%s" % status)
    parsed = parse_codex_usage(body)
    if not parsed:
        return None, "parse-failed"
    return parsed, None


def fetch_cursor_usage():
    tok = read_cursor_access_token()
    if not tok:
        return None, "no-credentials"
    status, body, err = _http_json(
        "POST",
        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        {
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Accept": "application/json",
        },
        body=b"{}",
    )
    if status == 401:
        return None, "auth-expired"
    if status == 429:
        return None, "rate-limited"
    if status != 200 or not isinstance(body, dict):
        return None, err or ("http-%s" % status)
    parsed = parse_cursor_usage(body)
    if not parsed:
        return None, "parse-failed"
    return parsed, None


def fetch_quota(kind):
    if kind == "claude":
        return fetch_claude_usage()
    if kind == "codex":
        return fetch_codex_usage()
    if kind == "cursor":
        return fetch_cursor_usage()
    return None, "unsupported"


def _quota_fingerprint(payload):
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(payload)


def build_quota_event(kind, payload, stale=False, reason=None):
    if not payload or not isinstance(payload, dict):
        return None
    windows = payload.get("windows")
    if not isinstance(windows, list) or not windows:
        return None
    ev = {
        "type": "quota",
        "kind": kind,
        "stale": bool(stale),
        "windows": windows,
        "ts": int(time.time()),
    }
    plan = payload.get("plan")
    if isinstance(plan, str) and plan.strip():
        ev["plan"] = plan.strip()
    if reason and stale:
        ev["reason"] = str(reason)[:80]
    return ev
