#!/usr/bin/env python3
"""Sessh agent watcher — long-lived, zero-install, stdout NDJSON.

Shared package (agent-watcher). Sessh embeds this file over SSH; the local
tmux plugin runs it in-process. Not installed on the remote disk.

Emits v1 events: hello, snapshot, state, gone, unbound, error, quota.
Accepts stdin control lines: {"cmd":"snapshot"|"ping"|"bind"|"stop"}.
Optional --listen PATH fans the same events to a 0700 Unix socket.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Emit ─────────────────────────────────────────────────────────────────

_emit_lock = threading.Lock()
_clients_lock = threading.Lock()
_clients = []  # list of connected AF_UNIX sockets


def _broadcast(line):
    """Copy one NDJSON line to socket subscribers. Drop dead peers."""
    data = line.encode("utf-8") if isinstance(line, str) else line
    with _clients_lock:
        live = []
        for conn in _clients:
            try:
                conn.sendall(data)
                live.append(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
        _clients[:] = live


def emit(obj):
    obj.setdefault("v", 1)
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()
        _broadcast(line)


def send_one(conn, obj):
    obj.setdefault("v", 1)
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    conn.sendall(line.encode("utf-8"))


def emit_error(message, **extra):
    payload = {"type": "error", "message": str(message)}
    payload.update(extra)
    emit(payload)


# ── Pane / process helpers ───────────────────────────────────────────────

def mtime(p):
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


def tmux_fmt(target, fmt):
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", target, fmt],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def capture_pane(target, scrollback=True, lines=200):
    """Capture pane text. Never dump unbounded scrollback — that blocks the loop."""
    try:
        if scrollback:
            args = [
                "tmux",
                "capture-pane",
                "-p",
                "-J",
                "-S",
                "-%d" % max(1, int(lines)),
                "-t",
                target,
            ]
        else:
            args = ["tmux", "capture-pane", "-p", "-J", "-t", target]
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        if scrollback:
            try:
                return subprocess.check_output(
                    ["tmux", "capture-pane", "-p", "-J", "-t", target],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                return ""
        return ""


def read_jsonl_tail(path, max_bytes=512 * 1024):
    """Load trailing JSONL objects — enough for last-event classification."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    entries = []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop partial first line
            text = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


def pane_last_activity(target):
    try:
        return float(tmux_fmt(target, "#{pane_last_activity}") or "0")
    except Exception:
        return 0.0


def pick_by_activity(files, target, max_dt=900, min_gap=30):
    if not files:
        return None
    activity = pane_last_activity(target)
    if activity <= 0:
        return None
    scored = []
    for f in files:
        mt = mtime(f)
        if mt <= 0:
            continue
        scored.append((abs(mt - activity), f))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    best_dt, best = scored[0]
    second_dt = scored[1][0] if len(scored) > 1 else 1e99
    close = [f for dt, f in scored if dt <= max_dt]
    if len(close) == 1:
        return close[0]
    if best_dt <= max_dt and (second_dt - best_dt) >= min_gap:
        return best
    return None


def clear_content_winner(scored, min_score=1, min_gap=1):
    """scored: list of (score, item) — require a unique winner."""
    if not scored:
        return None
    scored = sorted(scored, key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else -1
    if best_score >= min_score and best_score - second >= min_gap:
        return best
    return None


_ps_cache_lock = threading.Lock()
_ps_cache = {"at": 0.0, "children": None}
_PS_CACHE_S = 1.5


def process_children_map():
    """One `ps -ax` shared by scan + discover. Cache briefly so N panes ≠ N ps."""
    now = time.time()
    with _ps_cache_lock:
        cached = _ps_cache["children"]
        if cached is not None and now - _ps_cache["at"] < _PS_CACHE_S:
            return cached
    children = {}
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,ppid=,comm="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
    except Exception:
        children = {}
    with _ps_cache_lock:
        _ps_cache["at"] = time.time()
        _ps_cache["children"] = children
    return children


def list_descendant_pids(root_pid, children=None):
    pids = [root_pid]
    if children is None:
        children = process_children_map()
    stack, seen = [root_pid], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for ch in children.get(cur, []):
            pids.append(ch)
            stack.append(ch)
    return pids


def read_cmdline(pid):
    path = Path("/proc/%d/cmdline" % pid)
    if path.is_file():
        try:
            return path.read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
        except Exception:
            pass
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def read_open_paths(pid):
    paths = []
    fd_dir = Path("/proc/%d/fd" % pid)
    if fd_dir.is_dir():
        for fd in fd_dir.iterdir():
            try:
                paths.append(os.readlink(fd))
            except Exception:
                continue
        return paths
    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(pid), "-Fn"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if line.startswith("n"):
                paths.append(line[1:])
    except Exception:
        pass
    return paths


def read_proc_environ(pid):
    env = {}
    path = Path("/proc/%d/environ" % pid)
    if path.is_file():
        try:
            for item in path.read_bytes().split(b"\0"):
                if b"=" in item:
                    k, v = item.split(b"=", 1)
                    env[k.decode("utf-8", "ignore")] = v.decode("utf-8", "ignore")
            return env
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "eww", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for tok in out.replace("\n", " ").split():
            if "=" in tok and tok.split("=", 1)[0].isupper():
                k, v = tok.split("=", 1)
                env[k] = v
    except Exception:
        pass
    return env


# ── Agent kind detection (mirrors src/agents/detect.ts) ──────────────────

def detect_kind(command):
    """Match agent CLIs, including mid-argv under bwrap/sandbox wrappers."""
    if not command:
        return None
    c = command.strip()
    low = c.lower()
    if "cursor-agent" in low or "anysphere" in low:
        return "cursor"
    if "claude-code" in low:
        return "claude"
    bases = set()
    for tok in c.split():
        if not tok or tok.startswith("-"):
            continue
        base = Path(tok).name.lower()
        if base:
            bases.add(base)
    if "claude" in bases:
        return "claude"
    if "codex" in bases:
        return "codex"
    if "opencode" in bases or "open-code" in bases:
        return "opencode"
    if "pi" in bases:
        return "pi"
    if "copilot" in bases or "copilot-cli" in low or "/@github/copilot/" in low.replace(
        "\\", "/"
    ):
        return "copilot"
    # Cursor's `agent` symlink — basename only, so bare flag text cannot match.
    if "cursor-agent" in bases or "agent" in bases:
        return "cursor"
    return None


def scan_agents():
    """Return list of {session, window, kind, attached, windows}."""
    try:
        out = subprocess.check_output(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_pid}\t#{pane_current_command}\t#{session_attached}\t#{session_windows}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    # session -> win -> kind (first match wins)
    found = {}
    meta = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        session, win_s, pid_s, cmd = parts[0], parts[1], parts[2], parts[3]
        attached_s, windows_s = parts[4], parts[5]
        try:
            win = int(win_s)
            pid = int(pid_s) if pid_s else 0
        except ValueError:
            continue
        meta[session] = {
            "attached": attached_s not in ("0", "", "false"),
            "windows": int(windows_s) if windows_s.isdigit() else 1,
        }
        per = found.setdefault(session, {})
        if win in per:
            continue
        kind = detect_kind(cmd)
        if not kind and pid:
            kind = detect_kind(read_cmdline(pid))
        if not kind and pid:
            tree = process_children_map()
            for child in list_descendant_pids(pid, tree):
                kind = detect_kind(read_cmdline(child))
                if kind:
                    break
        if kind:
            per[win] = kind

    agents = []
    for session, per in found.items():
        m = meta.get(session) or {"attached": False, "windows": 1}
        for win, kind in sorted(per.items()):
            agents.append(
                {
                    "session": session,
                    "window": win,
                    "kind": kind,
                    "attached": m["attached"],
                    "windows": m["windows"],
                }
            )
    return agents


# ── Discover session paths ───────────────────────────────────────────────

def target_for(session, window):
    return "=%s:%d" % (session, window)


# ── Classification ───────────────────────────────────────────────────────

THINKING_STALE_MS = 90_000

# Live dialog chrome only — composer overlay, not leftover transcript.
# Bare "would you like" / "[y]" in a finished reply must not match.
PERMISSION_RE = re.compile(
    r"(?i)(?:"
    r"\[.*permission|"
    r"permission required|"
    r"do you want to\s+(?:allow|run|execute|approve|continue|apply|proceed)\b|"
    r"would you like to\s+(?:run|execute|allow|approve|continue|apply)\b|"
    r"do you trust\b|"
    r"allow\s+(?:this|the|once|always)\b|"
    r"always allow|"
    r"always run|"
    r"waiting for (?:your )?(?:approval|permission)|"
    r"needs? (?:your )?permission|"
    r"approve this action|"
    r"press y (?:again )?to|"
    r"\[\s*[yY]\s*/\s*[nN]\s*\]|"
    r"\(\s*[yY]\s*/\s*[nN]\s*\)|"
    r"command approval|"
    r"workspace trust|"
    r"confirm folder trust|"
    r"reject permission|"
    r"(?:^|\s)(?:y|Y)\s+(?:approve|run|allow)\b|"
    r"\[\s*[yY]\s*\]\s*(?:approve|allow|run|yes|reject)"
    r")"
)
# Agent-failure chrome only — bare "error" matches finished Cursor/Claude
# transcripts that merely *discussed* errors and caused idle↔errored flapping
# (plus spam error notifications) on every attention poll.
AGENT_ERROR_RE = re.compile(
    r"(?i)(?:"
    r"api\s*error|"
    r"rate\s*limits?|"
    r"context\s*(?:length|window)\s*(?:exceeded)?"
    r"|authentication\s*failed|"
    r"(?:agent|request|connection|provider)\s+(?:error|failed)|"
    r"something went wrong|"
    r"failed to (?:connect|authenticate|reach|load)|"
    r"\bfatal\s+error\b|"
    r"(?:❌|✖)\s*(?:error|failed)|"
    r"error:\s*(?:api|auth|network|timeout|overloaded)"
    r")"
)
# Bottom-of-pane prompts that mean the agent is waiting for input (idle).
# Used to demote stale thinking/running-tool — never to invent busy state.
IDLE_PROMPT_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[❯›≫»]\s*"
    r"|(?:Human|User|You)\s*:\s*"
    r")\s*$"
)
IDLE_PROMPT_END_RE = re.compile(r"[❯›≫»]\s*$")


def content_blocks(content):
    if isinstance(content, list):
        return content
    return []


def find_tool_use(content):
    """Return a tool-call content block. Pi uses camelCase `toolCall`."""
    for c in content_blocks(content):
        if isinstance(c, dict) and c.get("type") in (
            "tool_use",
            "tool-call",
            "tool_call",
            "toolCall",
        ):
            return c
    return None


def tool_input_from(tool):
    if not isinstance(tool, dict):
        return {}
    inp = tool.get("input") or tool.get("arguments") or tool.get("args") or {}
    return inp if isinstance(inp, dict) else {}


def tool_name_from(tool):
    if not isinstance(tool, dict):
        return ""
    return tool.get("name") or tool.get("tool") or ""


def tool_target_from(inp):
    if not isinstance(inp, dict):
        return ""
    raw = (
        inp.get("command")
        or inp.get("file_path")
        or inp.get("filePath")
        or inp.get("path")
        or inp.get("url")
        or ""
    )
    if not isinstance(raw, str):
        raw = str(raw) if raw is not None else ""
    # Collapse newlines / tags for notification + Live Activity copy; keep
    # the path/command words that describe the work.
    return clean_notification_text(raw)


SUMMARY_MAX = 80
_CONTEXT_USER_MARKERS = (
    "<user_info>",
    "<git_status>",
    "<agent_skills>",
    "<available_skills>",
    "<always_applied_workspace_rules>",
    "<agent_transcripts>",
    "<rules>",
    "<open_and_recently_viewed_files>",
)
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.S | re.I
)
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_XML_OR_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_LITERAL_ESCAPED_WS_RE = re.compile(r"\\[nrt]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMPTY_ANGLE_BRACKETS_RE = re.compile(r"<\s*>")


def text_from_content(content):
    """Flatten user message content; skip tool_result / image blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("tool_result", "tool_use", "tool-call", "tool_call", "image"):
            continue
        parts.append(str(b.get("text") or b.get("content") or ""))
    return "\n".join(parts)


def clean_notification_text(text):
    """Strip tags / escapes / ANSI so lock-screen copy stays readable."""
    if not text or not isinstance(text, str):
        return ""
    text = _LITERAL_ESCAPED_WS_RE.sub(" ", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_OSC_RE.sub("", text)
    text = _XML_OR_HTML_TAG_RE.sub(" ", text)
    text = _EMPTY_ANGLE_BRACKETS_RE.sub(" ", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def summarize_user_prompt(text):
    """First line of a real user prompt, capped for notification copy."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    queries = _USER_QUERY_RE.findall(text)
    if queries:
        text = str(queries[-1]).strip()
    elif any(m in text for m in _CONTEXT_USER_MARKERS):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    text = clean_notification_text(text)
    if len(text) < 2:
        return None
    if len(text) > SUMMARY_MAX:
        text = text[: SUMMARY_MAX - 1].rstrip() + "…"
    return text


def last_user_summary_from_events(entries):
    if not entries:
        return None
    for ev in reversed(entries):
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        role = None
        content = None
        if msg:
            role = msg.get("role") or ev.get("type")
            content = msg.get("content")
        if role != "user":
            role = ev.get("role") or ev.get("type")
            content = ev.get("content") if content is None else content
        if role != "user":
            continue
        summary = summarize_user_prompt(text_from_content(content))
        if summary:
            return summary
    return None


def store_age_ms(path):
    """Age from newest of db / WAL / SHM — SQLite often leaves the main file stale."""
    if not path:
        return 0
    newest = 0.0
    for p in (path, path + "-wal", path + "-shm"):
        try:
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            pass
    if newest <= 0:
        return 0
    age = int((time.time() - newest) * 1000)
    return age if age > 0 else 0


def parse_ts(raw):
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000
        return ts
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return 0.0
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        try:
            ts = float(s)
            if ts > 1e12:
                ts /= 1000
            return ts
        except Exception:
            return 0.0
    return 0.0


def age_out_busy(state, age_ms):
    """Demote stale busy states when the store has not been written recently.

    Cursor skips this in classify_cursor — its TUI chrome is the idle
    signal, and store.db often goes quiet while the agent is still working.
    Long-running tools on other harnesses that never touch the session file
    are handled by idle-prompt demotion in apply_pane_attention instead.
    """
    if age_ms > THINKING_STALE_MS and state in ("thinking", "running-tool"):
        return "idle"
    return state


def pane_shows_idle_prompt(lines):
    """True when the bottom of the pane looks like an agent waiting for input."""
    checked = 0
    for line in reversed(lines):
        if not line.strip():
            continue
        if IDLE_PROMPT_LINE_RE.match(line) or IDLE_PROMPT_END_RE.search(line):
            return True
        checked += 1
        # Skip a couple of footer/status lines under the real prompt.
        if checked >= 4:
            break
    return False


def live_permission_region(lines):
    """Text where a y/n overlay actually sits — on/just above the composer.

    Finished Cursor replies often still contain an older `[y] approve` row
    a few lines up. Scanning 15 lines of transcript kept those agents stuck
    on waiting-permission after they were done.
    """
    prompt_at = None
    checked = 0
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        if IDLE_PROMPT_LINE_RE.match(line) or IDLE_PROMPT_END_RE.search(line):
            prompt_at = i
            break
        checked += 1
        if checked >= 4:
            break
    if prompt_at is None:
        return "\n".join(lines[-8:])
    start = max(0, prompt_at - 3)
    return "\n".join(lines[start : prompt_at + 1])


def apply_pane_attention(state, text, kind=None):
    """Reconcile disk classify with pane chrome (pure; testable).

    Harnesses may register a kind-specific reader. Cursor's composer ❯ stays
    visible while generating — the shared idle-prompt demotion is wrong there.
    """
    fn = HARNESS_PANE_ATTENTION.get(kind) if kind else None
    if fn:
        return fn(state, text)
    return apply_default_pane_attention(state, text)


def apply_default_pane_attention(state, text):
    """Pi / Claude / Codex / OpenCode / Copilot: demote stale busy when the prompt is back."""
    if not text:
        return state
    lines = text.splitlines()
    if PERMISSION_RE.search(live_permission_region(lines)):
        return "waiting-permission"
    # Finished agents often still look "busy" on disk (stale tool_use leaf)
    # while the TUI already shows a ready prompt.
    if state in (
        "thinking",
        "running-tool",
        "errored",
        "waiting-permission",
    ) and pane_shows_idle_prompt(lines):
        return "idle"
    if state == "waiting-permission":
        # Overlay chrome is gone; don't keep attention forever.
        return "idle"
    if state == "running-tool":
        return state
    err_tail = "\n".join(lines[-8:])
    if state == "idle" and AGENT_ERROR_RE.search(err_tail):
        return "errored"
    return state


def attention_from_pane(session, window, state, kind=None):
    """Reconcile disk classify with live pane capture."""
    text = capture_pane(target_for(session, window), False)
    return apply_pane_attention(state, text, kind)


# Optional hooks each harness may register while being exec'd.
HARNESS_REQUIRES_SID = set()
HARNESS_FIND_STORE = {}
HARNESS_PROBE_ACTIVE = {}
HARNESS_LIVE_STORE = {}
HARNESS_PANE_ATTENTION = {}

# === BEGIN_HARNESS_LOAD ===
def _load_harnesses():
    """Exec per-harness discover/classify modules into this namespace.

    Repo checkout loads harnesses/*.py next to this file. Sessh's
    embed-agent-watcher.mjs inlines those files in place of this loader.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    hdir = os.path.join(here, "harnesses")
    if not os.path.isdir(hdir):
        return
    for name in ("pi", "claude", "codex", "cursor", "opencode", "copilot", "quota"):
        path = os.path.join(hdir, name + ".py")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            exec(compile(fh.read(), path, "exec"), globals())


_load_harnesses()
# === END_HARNESS_LOAD ===


DISCOVER = {
    "pi": discover_pi,
    "claude": discover_claude,
    "codex": discover_codex,
    "cursor": discover_cursor,
    "opencode": discover_opencode,
    "copilot": discover_copilot,
}

CLASSIFY = {
    "pi": lambda path, sid: classify_pi(path),
    "claude": lambda path, sid: classify_claude(path),
    "codex": lambda path, sid: classify_codex(path),
    "cursor": lambda path, sid: classify_cursor(path),
    "opencode": lambda path, sid: classify_opencode(path, sid),
    "copilot": lambda path, sid: classify_copilot(path),
}


def harness_requires_sid(kind):
    return kind in HARNESS_REQUIRES_SID


def harness_find_store(kind):
    fn = HARNESS_FIND_STORE.get(kind)
    return fn() if fn else None


def harness_probe_active(kind, session, window, exclude_sids=None):
    fn = HARNESS_PROBE_ACTIVE.get(kind)
    if not fn:
        return None
    return fn(session, window, exclude_sids=exclude_sids)


def harness_live_store(kind, session, window):
    fn = HARNESS_LIVE_STORE.get(kind)
    if not fn:
        return None
    return fn(session, window)


# ── Watcher loop ─────────────────────────────────────────────────────────

REDISCOVER_S = 8.0
# Busy agents still feel responsive at ~1.5s; sub-second capture-pane was
# the main CPU cost when several panes were thinking/tooling at once.
POLL_BUSY_S = 1.5
POLL_IDLE_S = 4.0
ATTENTION_EVERY_S = 8.0
# Cursor disk often looks idle while the TUI status line still says working.
ATTENTION_CURSOR_S = 2.0
BUSY_STATES = frozenset(("thinking", "running-tool", "waiting-permission"))

# key -> last emitted fingerprint
_last = {}
# key -> {path, sessionId, kind, session, window, attached, windows, sig}
_bound = {}
# key -> last attention pane check (monotonic)
_attention_at = {}
_lock = threading.Lock()
_snapshot_requested = threading.Event()
_stop = threading.Event()
# kind -> last emitted quota fingerprint / payload for stale fallback
_quota_last = {}
_next_quota_at = 0.0


def agent_key(session, window):
    return "%s:w%d" % (session, window)


def list_live_pane_keys():
    """All tmux panes, including shells that scan_agents has not classified."""
    try:
        out = subprocess.check_output(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{session_attached}\t#{session_windows}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return {}
    panes = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            win = int(parts[1])
        except ValueError:
            continue
        panes[agent_key(parts[0], win)] = {
            "session": parts[0],
            "window": win,
            "attached": parts[2] not in ("0", "", "false"),
            "windows": int(parts[3]) if parts[3].isdigit() else 1,
        }
    return panes


def file_sig(path):
    """Stat path plus SQLite WAL/SHM so Cursor/OpenCode updates are visible."""
    if not path:
        return None
    parts = []
    found = False
    for p in (path, path + "-wal", path + "-shm"):
        try:
            st = os.stat(p)
            parts.append(
                (
                    p,
                    st.st_mtime_ns if hasattr(st, "st_mtime_ns") else st.st_mtime,
                    st.st_size,
                )
            )
            found = True
        except OSError:
            parts.append((p, 0, 0))
    if not found:
        return None
    return tuple(parts)


def classify_agent(
    kind, path, session_id, session, window, attention=True, last_state=None
):
    fn = CLASSIFY.get(kind)
    if not fn:
        return {"state": "idle", "ageMs": 0}
    result = fn(path, session_id)
    if result.get("pathMissing"):
        return result
    state = result.get("state") or "idle"
    # Pane capture is relatively expensive (forks tmux). Skip it when the
    # caller knows the store has not changed and the agent is idle.
    # Permission waits often look idle on disk — those ticks pass
    # attention=True so the overlay still wins.
    if attention:
        pane_in = state
        # Cursor disk is often idle while the TUI is still working (streamed
        # assistant text, store not touched for 90s). Floor on the last busy
        # / permission state so an inconclusive pane cannot flip to idle.
        if kind == "cursor":
            if state in BUSY_STATES:
                pane_in = state
            elif last_state in BUSY_STATES:
                pane_in = last_state
        state = attention_from_pane(session, window, pane_in, kind)
    result["state"] = state
    return result


def build_agent_payload(info, result=None, path=None, unbound=False):
    payload = {
        "session": info["session"],
        "window": info["window"],
        "kind": info["kind"],
        "attached": info.get("attached", False),
        "windows": info.get("windows", 1),
    }
    if unbound or not path:
        payload["unbound"] = True
        return payload
    payload["path"] = path
    if result:
        payload["state"] = result.get("state") or "idle"
        if result.get("toolName"):
            payload["toolName"] = result["toolName"]
        if result.get("toolTarget"):
            payload["toolTarget"] = result["toolTarget"]
        if result.get("summary"):
            payload["summary"] = result["summary"]
        payload["ageMs"] = result.get("ageMs", 0)
    return payload


def emit_state(info, result, path):
    key = agent_key(info["session"], info["window"])
    sid = info.get("sessionId")
    fp = (
        result.get("state"),
        result.get("toolName"),
        result.get("toolTarget"),
        result.get("summary"),
        path,
        sid,
    )
    with _lock:
        prev = _last.get(key)
        if prev == fp:
            return
        _last[key] = fp
    payload = {
        "type": "state",
        "session": info["session"],
        "window": info["window"],
        "kind": info["kind"],
        "state": result.get("state") or "idle",
        "toolName": result.get("toolName"),
        "toolTarget": result.get("toolTarget"),
        "path": path,
        "sessionId": sid,
        "ts": int(time.time()),
        "attached": info.get("attached", False),
        "windows": info.get("windows", 1),
    }
    if result.get("summary"):
        payload["summary"] = result["summary"]
    emit(payload)


def emit_unbound(info, reason="path-missing"):
    key = agent_key(info["session"], info["window"])
    fp = ("unbound", reason)
    with _lock:
        prev = _last.get(key)
        if prev == fp:
            return
        _last[key] = fp
    emit(
        {
            "type": "unbound",
            "session": info["session"],
            "window": info["window"],
            "kind": info["kind"],
            "reason": reason,
            "attached": info.get("attached", False),
            "windows": info.get("windows", 1),
        }
    )


def emit_gone(session, window):
    key = agent_key(session, window)
    with _lock:
        _last.pop(key, None)
        _bound.pop(key, None)
    emit({"type": "gone", "session": session, "window": window})


def _run_discover(info, claimed_sids):
    discover = DISCOVER.get(info["kind"])
    if not discover:
        return None, None
    try:
        if harness_requires_sid(info["kind"]):
            path, sid = discover(
                info["session"], info["window"], exclude_sids=claimed_sids
            )
        else:
            path, sid = discover(info["session"], info["window"])
    except TypeError:
        # Older discover signatures without exclude_sids.
        path, sid = discover(info["session"], info["window"])
    except Exception as e:
        emit_error("discover failed", kind=info["kind"], detail=str(e))
        return None, None
    # Shared-store harnesses (OpenCode) refuse to bind without a unique sid.
    if harness_requires_sid(info["kind"]) and (not sid or sid in claimed_sids):
        return None, None
    if harness_requires_sid(info["kind"]) and sid:
        claimed_sids.add(sid)
    return path, sid


def full_snapshot():
    agents = scan_agents()
    items = []
    seen_keys = set()
    claimed_sids = set()
    for info in agents:
        key = agent_key(info["session"], info["window"])
        seen_keys.add(key)
        path, sid = _run_discover(info, claimed_sids)
        if not path or not os.path.isfile(path):
            # Chat history binds with stronger heuristics; a failed rediscover
            # must not wipe a still-valid sticky bind (that left Pi panes
            # permanently unbound after leaving chat).
            with _lock:
                prev = _bound.get(key)
            keep = (
                prev
                and prev.get("kind") == info["kind"]
                and not harness_requires_sid(info["kind"])
                and prev.get("path")
                and os.path.isfile(prev["path"])
            )
            if keep:
                path = prev["path"]
                sid = prev.get("sessionId")
            else:
                with _lock:
                    _bound[key] = {
                        "path": None,
                        "sessionId": sid,
                        "kind": info["kind"],
                        "session": info["session"],
                        "window": info["window"],
                        "attached": info.get("attached", False),
                        "windows": info.get("windows", 1),
                        "sig": None,
                    }
                items.append(build_agent_payload(info, unbound=True))
                continue
        try:
            result = classify_agent(
                info["kind"],
                path,
                sid,
                info["session"],
                info["window"],
                last_state=last_emitted_state(key),
            )
        except Exception as e:
            emit_error("classify failed", kind=info["kind"], detail=str(e))
            result = {"state": "idle", "ageMs": 0}
        if result.get("pathMissing"):
            items.append(build_agent_payload(info, unbound=True))
            continue
        with _lock:
            _bound[key] = {
                "path": path,
                "sessionId": sid,
                "kind": info["kind"],
                "session": info["session"],
                "window": info["window"],
                "attached": info.get("attached", False),
                "windows": info.get("windows", 1),
                "sig": file_sig(path),
            }
            _last[key] = (
                result.get("state"),
                result.get("toolName"),
                result.get("toolTarget"),
                result.get("summary"),
                path,
                sid,
            )
        items.append(build_agent_payload(info, result, path))

    # Chat binds can land before scan_agents classifies the pane (Cursor
    # often shows as `node`). Keep those sticky and include them here.
    live_panes = list_live_pane_keys()
    with _lock:
        extras = [
            (k, dict(prev))
            for k, prev in _bound.items()
            if k not in seen_keys and prev.get("path") and prev.get("kind")
        ]
    for k, prev in extras:
        pane = live_panes.get(k)
        if not pane:
            continue
        path = prev.get("path")
        sid = prev.get("sessionId")
        if not path or not os.path.isfile(path):
            continue
        if harness_requires_sid(prev.get("kind")) and not sid:
            continue
        info = {
            "session": prev["session"],
            "window": prev["window"],
            "kind": prev["kind"],
            "attached": pane.get("attached", prev.get("attached", False)),
            "windows": pane.get("windows", prev.get("windows", 1)),
        }
        try:
            result = classify_agent(
                info["kind"],
                path,
                sid,
                info["session"],
                info["window"],
                last_state=last_emitted_state(k),
            )
        except Exception as e:
            emit_error("classify failed", kind=info["kind"], detail=str(e))
            result = {"state": "idle", "ageMs": 0}
        if result.get("pathMissing"):
            continue
        seen_keys.add(k)
        with _lock:
            _bound[k] = {
                "path": path,
                "sessionId": sid,
                "kind": info["kind"],
                "session": info["session"],
                "window": info["window"],
                "attached": info.get("attached", False),
                "windows": info.get("windows", 1),
                "sig": file_sig(path),
            }
            _last[k] = (
                result.get("state"),
                result.get("toolName"),
                result.get("toolTarget"),
                result.get("summary"),
                path,
                sid,
            )
        items.append(build_agent_payload(info, result, path))

    with _lock:
        stale = [k for k in list(_bound.keys()) if k not in seen_keys]
        for k in stale:
            prev = _bound.get(k)
            if prev and prev.get("path") and k in live_panes:
                continue
            _bound.pop(k, None)
            _last.pop(k, None)

    emit({"type": "snapshot", "agents": items, "ts": int(time.time())})


def rediscover():
    """Scan panes; only run expensive path discovery when unbound/stale."""
    agents = scan_agents()
    seen = set()
    live = {agent_key(a["session"], a["window"]): a for a in agents}
    claimed_sids = set()
    stale_clear = []
    # Snapshot binds then probe active sid outside the lock (lsof/HTTP can be slow).
    with _lock:
        bound_snapshot = [
            (key, dict(prev)) for key, prev in _bound.items()
        ]
    for key, prev in bound_snapshot:
        kind = prev.get("kind")
        if not harness_requires_sid(kind) or not prev.get("sessionId"):
            continue
        if not prev.get("path") or not os.path.isfile(prev.get("path") or ""):
            continue
        live_info = live.get(key)
        if not live_info or live_info.get("kind") != kind:
            continue
        active = harness_probe_active(
            kind, prev["session"], prev["window"], exclude_sids=set()
        )
        if active and active != prev.get("sessionId"):
            with _lock:
                cur = _bound.get(key)
                if cur and cur.get("sessionId") == prev.get("sessionId"):
                    _bound[key] = {
                        **cur,
                        "path": None,
                        "sessionId": None,
                        "sig": None,
                    }
            stale_clear.append(
                {
                    "session": prev["session"],
                    "window": prev["window"],
                    "kind": kind,
                    "attached": prev.get("attached", False),
                    "windows": prev.get("windows", 1),
                }
            )
            continue
        # Only hard-claim when the live server confirms this sid. Unverified
        # claims (HTTP down) used to pin a wrong chat bind forever.
        if active == prev.get("sessionId"):
            claimed_sids.add(prev["sessionId"])

    for prev in stale_clear:
        emit_unbound(prev, reason="stale-sid")

    for info in agents:
        key = agent_key(info["session"], info["window"])
        seen.add(key)
        with _lock:
            prev = _bound.get(key)
        need_discover = (
            prev is None
            or not prev.get("path")
            or not os.path.isfile(prev.get("path") or "")
            or prev.get("kind") != info["kind"]
            or (harness_requires_sid(info["kind"]) and not prev.get("sessionId"))
        )
        # Shared-store sticky bind can lag behind chat/TUI session switches —
        # re-probe the live active session even when already bound.
        if (
            not need_discover
            and harness_requires_sid(info["kind"])
            and prev
            and prev.get("sessionId")
        ):
            others = {s for s in claimed_sids if s != prev.get("sessionId")}
            active = harness_probe_active(
                info["kind"], info["session"], info["window"], exclude_sids=others
            )
            if active and active != prev.get("sessionId"):
                need_discover = True
        # Live store path changed (Pi opens a new jsonl on `/new`). Sticky
        # bind on the old file left the pane looking dead until rediscovery.
        if not need_discover and prev and prev.get("path"):
            live_store = harness_live_store(
                info["kind"], info["session"], info["window"]
            )
            if live_store:
                try:
                    same = os.path.samefile(live_store, prev["path"])
                except OSError:
                    same = os.path.realpath(live_store) == os.path.realpath(
                        prev["path"]
                    )
                if not same:
                    need_discover = True
        path = prev.get("path") if prev and not need_discover else None
        sid = prev.get("sessionId") if prev and not need_discover else None
        if need_discover:
            # Temporarily release this pane's prior claim so it can re-bind.
            if prev and prev.get("sessionId") in claimed_sids:
                claimed_sids.discard(prev.get("sessionId"))
            path, sid = _run_discover(info, claimed_sids)
            # Keep a still-valid sticky bind when rediscover is inconclusive
            # (same pitfall as full_snapshot for parallel Pi sessions).
            if (
                (not path or not os.path.isfile(path))
                and prev
                and prev.get("kind") == info["kind"]
                and not harness_requires_sid(info["kind"])
                and prev.get("path")
                and os.path.isfile(prev["path"])
            ):
                path = prev["path"]
                sid = prev.get("sessionId")
        elif harness_requires_sid(info["kind"]) and sid:
            claimed_sids.add(sid)
        bound_path = path if path and os.path.isfile(path) else None
        if harness_requires_sid(info["kind"]) and not sid:
            bound_path = None
        with _lock:
            _bound[key] = {
                "path": bound_path,
                "sessionId": sid,
                "kind": info["kind"],
                "session": info["session"],
                "window": info["window"],
                "attached": info.get("attached", False),
                "windows": info.get("windows", 1),
                "sig": file_sig(bound_path) if bound_path else None,
            }
        if not bound_path:
            emit_unbound(info)
            continue
        # Classification is poll_bound's job — avoid double full-file work here.

    live_panes = list_live_pane_keys()
    with _lock:
        known = list(_bound.keys())
    for key in known:
        if key in seen or key in live_panes:
            continue
        with _lock:
            info = _bound.get(key)
        if info:
            emit_gone(info["session"], info["window"])
        else:
            m = re.match(r"^(.*):w(\d+)$", key)
            if m:
                emit_gone(m.group(1), int(m.group(2)))


def last_emitted_state(key):
    with _lock:
        prev = _last.get(key)
    if isinstance(prev, tuple) and prev:
        return prev[0]
    return None


def poll_interval_s():
    """Sub-second while any agent is busy; a few seconds when everything is idle."""
    with _lock:
        for fp in _last.values():
            if isinstance(fp, tuple) and fp and fp[0] in BUSY_STATES:
                return POLL_BUSY_S
    return POLL_IDLE_S


def bound_quota_kinds():
    """Subscription agent kinds currently bound on this host."""
    kinds = set()
    with _lock:
        for info in _bound.values():
            kind = info.get("kind")
            if kind in QUOTA_KINDS:
                kinds.add(kind)
    return kinds


def emit_quota_for_kind(kind, force=False):
    """Probe one vendor usage API; emit only on change (or force/stale)."""
    global _quota_last
    payload, err = fetch_quota(kind)
    now = time.time()
    if payload:
        fingerprint = _quota_fingerprint(payload)
        prev = _quota_last.get(kind) or {}
        if not force and prev.get("fp") == fingerprint and not prev.get("stale"):
            _quota_last[kind] = {
                "fp": fingerprint,
                "payload": payload,
                "stale": False,
                "at": now,
            }
            return
        ev = build_quota_event(kind, payload, stale=False)
        if not ev:
            return
        _quota_last[kind] = {
            "fp": fingerprint,
            "payload": payload,
            "stale": False,
            "at": now,
        }
        emit(ev)
        return

    # Keep last good reading, marked stale
    prev = _quota_last.get(kind)
    if prev and prev.get("payload"):
        if prev.get("stale") and prev.get("err") == err and not force:
            return
        ev = build_quota_event(
            kind, prev["payload"], stale=True, reason=err or "fetch-failed"
        )
        if not ev:
            return
        _quota_last[kind] = {
            "fp": prev.get("fp"),
            "payload": prev["payload"],
            "stale": True,
            "err": err,
            "at": now,
        }
        emit(ev)


def poll_quota(force=False):
    """Slow subscription usage probe for bound Claude/Codex/Cursor agents."""
    global _next_quota_at
    kinds = bound_quota_kinds()
    if not kinds:
        _next_quota_at = time.time() + QUOTA_INTERVAL_S
        return
    for kind in sorted(kinds):
        try:
            emit_quota_for_kind(kind, force=force)
        except Exception as e:
            emit_error("quota failed", detail=str(e), kind=kind)
    _next_quota_at = time.time() + QUOTA_INTERVAL_S


def poll_bound():
    """Reclassify busy agents every tick. Idle agents skip disk+pane until due."""
    with _lock:
        items = list(_bound.items())
    now = time.time()
    for key, info in items:
        path = info.get("path")
        if not path:
            continue
        if harness_requires_sid(info.get("kind")) and not info.get("sessionId"):
            emit_unbound(
                {
                    "session": info["session"],
                    "window": info["window"],
                    "kind": info["kind"],
                    "attached": info.get("attached", False),
                    "windows": info.get("windows", 1),
                },
                reason="missing-session-id",
            )
            with _lock:
                if key in _bound:
                    _bound[key]["path"] = None
                    _bound[key]["sig"] = None
                    _bound[key]["sessionId"] = None
            continue
        sig = file_sig(path)
        if sig is None:
            emit_unbound(
                {
                    "session": info["session"],
                    "window": info["window"],
                    "kind": info["kind"],
                    "attached": info.get("attached", False),
                    "windows": info.get("windows", 1),
                }
            )
            with _lock:
                if key in _bound:
                    _bound[key]["path"] = None
                    _bound[key]["sig"] = None
            continue

        changed = sig != info.get("sig")
        with _lock:
            if key in _bound:
                _bound[key]["sig"] = sig

        last_state = last_emitted_state(key)
        busy = last_state in BUSY_STATES
        last_att = _attention_at.get(key, 0)
        att_every = (
            ATTENTION_CURSOR_S if info.get("kind") == "cursor" else ATTENTION_EVERY_S
        )
        want_attention = changed or busy or (now - last_att) >= att_every
        if not changed and not busy and not want_attention:
            continue
        if want_attention:
            _attention_at[key] = now

        payload_info = {
            "session": info["session"],
            "window": info["window"],
            "kind": info["kind"],
            "attached": info.get("attached", False),
            "windows": info.get("windows", 1),
            "sessionId": info.get("sessionId"),
        }

        if not changed and not busy:
            # Unchanged idle store — pane only (permission / error chrome).
            try:
                state = attention_from_pane(
                    info["session"],
                    info["window"],
                    last_state or "idle",
                    info.get("kind"),
                )
            except Exception as e:
                emit_error(
                    "classify failed",
                    kind=info.get("kind"),
                    detail=str(e),
                    session=info.get("session"),
                    window=info.get("window"),
                )
                continue
            if state != (last_state or "idle"):
                emit_state(payload_info, {"state": state, "ageMs": 0}, path)
            continue

        try:
            result = classify_agent(
                info["kind"],
                path,
                info.get("sessionId"),
                info["session"],
                info["window"],
                attention=want_attention,
                last_state=last_state,
            )
        except Exception as e:
            emit_error(
                "classify failed",
                kind=info.get("kind"),
                detail=str(e),
                session=info.get("session"),
                window=info.get("window"),
            )
            continue
        if result.get("pathMissing"):
            emit_unbound(
                {
                    "session": info["session"],
                    "window": info["window"],
                    "kind": info["kind"],
                    "attached": info.get("attached", False),
                    "windows": info.get("windows", 1),
                }
            )
            continue
        emit_state(payload_info, result, path)


def apply_client_bind(msg):
    """Phone chat history resolved a store — adopt it for this pane.

    Chat history rediscovers every poll with stronger heuristics than the
    sticky watcher bind; without this hint, chat-driven turns stay
    idle/unbound while the transcript itself updates.

    Never steal an OpenCode ses_ from another *live* agent pane — a wrong
    window index from chat permanently unbound the real pane (claimed sid
    could not be reclaimed).
    """
    session = msg.get("session")
    window = msg.get("window")
    kind = msg.get("kind")
    if not isinstance(session, str) or not session:
        return
    try:
        window = int(window)
    except Exception:
        return
    if not isinstance(kind, str) or kind not in DISCOVER:
        return
    path = msg.get("path")
    sid = msg.get("sessionId")
    if isinstance(path, str) and path:
        path = path.strip() or None
    else:
        path = None
    if isinstance(sid, str) and sid:
        sid = sid.strip() or None
    else:
        sid = None
    if harness_requires_sid(kind):
        if not sid:
            return
        if not path:
            path = harness_find_store(kind)
    if not path or not os.path.isfile(path):
        return
    if harness_requires_sid(kind) and not sid:
        return

    key = agent_key(session, window)
    live = {
        agent_key(a["session"], a["window"]): a for a in scan_agents()
    }
    pane = list_live_pane_keys().get(key) or {}
    target = live.get(key)
    if target and target.get("kind") != kind:
        # Stale/wrong window from the phone — ignore rather than poison binds.
        return
    # Pane not in this scan yet (Cursor often hides behind `node` until we
    # walk descendants) — still adopt the chat bind so the next snapshot
    # classifies instead of staying unbound.

    # Decide reclaim outside the lock (may HTTP-probe other panes).
    to_reclaim = []
    if harness_requires_sid(kind) and sid:
        with _lock:
            others = [
                (k, dict(prev))
                for k, prev in _bound.items()
                if k != key
                and prev.get("kind") == kind
                and prev.get("sessionId") == sid
            ]
        for k, prev in others:
            other_live = live.get(k)
            if other_live and other_live.get("kind") == kind:
                other_active = harness_probe_active(
                    kind, prev["session"], prev["window"], exclude_sids=set()
                )
                if other_active == sid:
                    # True conflict — another live pane still owns this sid.
                    return
            to_reclaim.append((k, prev))

    reclaimed = []
    with _lock:
        for k, prev in to_reclaim:
            cur = _bound.get(k)
            if not cur or cur.get("sessionId") != sid:
                continue
            _bound[k] = {
                **cur,
                "path": None,
                "sessionId": None,
                "sig": None,
            }
            reclaimed.append(
                {
                    "session": prev["session"],
                    "window": prev["window"],
                    "kind": kind,
                    "attached": prev.get("attached", False),
                    "windows": prev.get("windows", 1),
                }
            )
        meta = _bound.get(key) or {}
        attached = (target or pane or meta).get("attached", False)
        windows = (target or pane or meta).get("windows", 1)
        _bound[key] = {
            "path": path,
            "sessionId": sid,
            "kind": kind,
            "session": session,
            "window": window,
            "attached": attached,
            "windows": windows,
            "sig": file_sig(path),
        }

    for prev in reclaimed:
        emit_unbound(prev, reason="sid-reclaimed")

    info = {
        "session": session,
        "window": window,
        "kind": kind,
        "attached": attached,
        "windows": windows,
        "sessionId": sid,
    }
    try:
        result = classify_agent(
            kind,
            path,
            sid,
            session,
            window,
            last_state=last_emitted_state(agent_key(session, window)),
        )
    except Exception as e:
        emit_error("bind classify failed", kind=kind, detail=str(e))
        return
    if result.get("pathMissing"):
        emit_unbound(info, reason="bind-path-missing")
        return
    emit_state(info, result, path)


def handle_control_line(line, source="stdin"):
    """Apply one control JSON object.

    Returns \"halt\" to stop the watcher, \"disconnect\" to drop a socket
    client (stop from a subscriber must not kill the host daemon), or None.
    """
    line = (line or "").strip()
    if not line or not line.startswith("{"):
        return None
    try:
        msg = json.loads(line)
    except Exception:
        return None
    cmd = msg.get("cmd")
    if cmd == "snapshot":
        _snapshot_requested.set()
    elif cmd == "ping":
        emit({"type": "hello", "pong": True, "ts": int(time.time())})
    elif cmd == "bind":
        try:
            apply_client_bind(msg)
        except Exception as e:
            emit_error("bind failed", detail=str(e))
    elif cmd == "stop":
        if source == "socket":
            return "disconnect"
        _stop.set()
        _snapshot_requested.set()
        return "halt"
    return None


def stdin_loop():
    while not _stop.is_set():
        try:
            if sys.stdin is None or sys.stdin.closed:
                break
            # Non-blocking-ish read via select when possible.
            if hasattr(select, "select"):
                r, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not r:
                    continue
            line = sys.stdin.readline()
            if line == "":
                # EOF — keep watching; phone may not send more commands.
                time.sleep(0.5)
                continue
            if handle_control_line(line, source="stdin") == "halt":
                break
        except Exception:
            time.sleep(0.5)


def _drop_client(conn):
    with _clients_lock:
        if conn in _clients:
            _clients.remove(conn)
    try:
        conn.close()
    except Exception:
        pass


def socket_client_loop(conn):
    try:
        send_one(
            conn,
            {
                "type": "hello",
                "role": "agent-watcher",
                "via": "socket",
                "ts": int(time.time()),
            },
        )
        _snapshot_requested.set()
        with _clients_lock:
            _clients.append(conn)
        buf = b""
        while not _stop.is_set():
            if hasattr(select, "select"):
                r, _, _ = select.select([conn], [], [], 0.5)
                if not r:
                    continue
            try:
                chunk = conn.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                action = handle_control_line(
                    raw.decode("utf-8", "ignore"), source="socket"
                )
                if action == "disconnect":
                    return
    finally:
        _drop_client(conn)


def listen_loop(path):
    sock = None
    try:
        try:
            os.unlink(path)
        except OSError:
            pass
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            except OSError:
                pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            sock.bind(path)
        finally:
            os.umask(old)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        sock.listen(16)
        sock.settimeout(0.5)
        while not _stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except Exception:
                if _stop.is_set():
                    break
                time.sleep(0.2)
                continue
            t = threading.Thread(
                target=socket_client_loop, args=(conn,), name="sock-client", daemon=True
            )
            t.start()
    finally:
        try:
            sock.close()
        except Exception:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass


def main(listen_path=None):
    if listen_path:
        threading.Thread(
            target=listen_loop, args=(listen_path,), name="listen", daemon=True
        ).start()
        # Bind before advertising hello so a racing probe can connect.
        time.sleep(0.05)
    emit({"type": "hello", "role": "agent-watcher", "ts": int(time.time())})
    t = threading.Thread(target=stdin_loop, name="stdin", daemon=True)
    t.start()
    try:
        full_snapshot()
    except Exception as e:
        emit_error("initial snapshot failed", detail=str(e))

    next_discover = time.time() + REDISCOVER_S
    next_poll = time.time() + poll_interval_s()
    global _next_quota_at
    _next_quota_at = time.time() + 2.0  # first probe shortly after bind
    while not _stop.is_set():
        now = time.time()
        if _snapshot_requested.is_set():
            _snapshot_requested.clear()
            if _stop.is_set():
                break
            try:
                full_snapshot()
            except Exception as e:
                emit_error("snapshot failed", detail=str(e))
            now = time.time()
            next_discover = now + REDISCOVER_S
            next_poll = now + poll_interval_s()
            # New agents may have appeared — probe soon if due / never probed.
            if bound_quota_kinds():
                _next_quota_at = min(_next_quota_at, now + 1.0)
        elif now >= next_discover:
            try:
                rediscover()
            except Exception as e:
                emit_error("rediscover failed", detail=str(e))
            next_discover = time.time() + REDISCOVER_S
        elif now >= next_poll:
            try:
                poll_bound()
            except Exception as e:
                emit_error("poll failed", detail=str(e))
            next_poll = time.time() + poll_interval_s()
        elif now >= _next_quota_at:
            try:
                poll_quota()
            except Exception as e:
                emit_error("quota failed", detail=str(e))
        wait = min(next_discover, next_poll, _next_quota_at) - time.time()
        if wait > 0:
            _snapshot_requested.wait(timeout=wait)


if __name__ == "__main__":
    # Offline classify mode for tests:
    #   SESSH_CLASSIFY_KIND=claude SESSH_SESSION_PATH=/path python agent_watcher.py --classify
    #   SESSH_STATE=idle python agent_watcher.py --attention < pane.txt
    argv = sys.argv[1:]
    if argv and argv[0] == "--classify":
        kind = os.environ.get("SESSH_CLASSIFY_KIND", "claude")
        path = os.environ.get("SESSH_SESSION_PATH", "")
        sid = os.environ.get("SESSH_SESSION_ID")
        fn = CLASSIFY.get(kind)
        if not fn:
            print(json.dumps({"state": "idle", "ageMs": 0}))
            sys.exit(0)
        print(json.dumps(fn(path, sid)))
        sys.exit(0)
    if argv and argv[0] == "--attention":
        state = os.environ.get("SESSH_STATE", "idle")
        kind = os.environ.get("SESSH_ATTENTION_KIND") or None
        text = sys.stdin.read()
        print(json.dumps({"state": apply_pane_attention(state, text, kind)}))
        sys.exit(0)
    if argv and argv[0] == "--quota-parse":
        # Offline parser: SESSH_QUOTA_KIND=claude SESSH_QUOTA_JSON=/path
        kind = os.environ.get("SESSH_QUOTA_KIND", "claude")
        path = os.environ.get("SESSH_QUOTA_JSON", "")
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        parsers = {
            "claude": parse_claude_usage,
            "codex": parse_codex_usage,
            "cursor": parse_cursor_usage,
        }
        fn = parsers.get(kind)
        if not fn:
            print(json.dumps({"error": "unsupported-kind"}))
            sys.exit(1)
        payload = fn(raw)
        ev = build_quota_event(kind, payload) if payload else None
        print(json.dumps(ev if ev else {"error": "parse-failed"}))
        sys.exit(0)
    if argv and argv[0] == "--quota-auth":
        # Offline credential presence (no network). HOME can be redirected.
        kind = os.environ.get("SESSH_QUOTA_KIND", "claude")
        if kind == "claude":
            tok = read_claude_access_token()
            print(
                json.dumps(
                    {"kind": kind, "hasToken": bool(tok), "plan": read_claude_plan()}
                )
            )
        elif kind == "codex":
            tok, acct = read_codex_auth()
            print(
                json.dumps(
                    {
                        "kind": kind,
                        "hasToken": bool(tok),
                        "hasAccount": bool(acct),
                    }
                )
            )
        elif kind == "cursor":
            tok = read_cursor_access_token()
            print(json.dumps({"kind": kind, "hasToken": bool(tok)}))
        else:
            print(json.dumps({"error": "unsupported-kind"}))
            sys.exit(1)
        sys.exit(0)
    listen_path = None
    if "--listen" in argv:
        i = argv.index("--listen")
        if i + 1 < len(argv):
            listen_path = argv[i + 1]
    main(listen_path=listen_path)
