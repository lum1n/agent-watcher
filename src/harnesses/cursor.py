"""Cursor Agent discover + classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.
"""

def classify_cursor(db_path):
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    age_ms = store_age_ms(db_path)

    def decode_meta_value(raw):
        if raw is None:
            return None
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", "ignore")
            except Exception:
                return None
        s = str(raw).strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            pass
        try:
            if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) % 2 == 0:
                return json.loads(bytes.fromhex(s).decode("utf-8", "ignore"))
        except Exception:
            pass
        return None

    def blob_to_text(data):
        if data is None:
            return ""
        if isinstance(data, memoryview):
            data = data.tobytes()
        if isinstance(data, (bytes, bytearray)):
            try:
                text = data.decode("utf-8")
                if text.isprintable() or "\n" in text or "{" in text:
                    return text
            except Exception:
                pass
            try:
                return bytes.fromhex(data.decode("ascii", "ignore").strip()).decode(
                    "utf-8", "ignore"
                )
            except Exception:
                return data.decode("utf-8", "ignore")
            return ""
        return str(data)

    def is_context_user_tail(tail):
        """Cursor injects huge role=user context dumps that aren't real prompts."""
        if "<user_query>" in tail:
            return False
        markers = (
            "<user_info>",
            "<git_status>",
            "<agent_skills>",
            "<available_skills>",
            "<always_applied_workspace_rules>",
            "<agent_transcripts>",
            "<rules>",
            "<open_and_recently_viewed_files>",
        )
        return any(m in tail for m in markers)

    def classify_blob_text(text):
        """Return (state, tool_name, tool_target) or None if no usable role."""
        if not text:
            return None
        roles = list(re.finditer(r'"role"\s*:\s*"(user|assistant|system|tool)"', text))
        if not roles:
            return None
        # Walk from the end; skip system + context-injection user blobs.
        for m in reversed(roles):
            role = m.group(1)
            tail = text[m.start() :]
            if role == "system":
                continue
            if role == "user" and is_context_user_tail(tail):
                continue
            if role == "assistant":
                has_tool = bool(
                    re.search(
                        r'"type"\s*:\s*"(?:tool_use|tool-call|tool_call)"',
                        tail,
                    )
                )
                has_final_text = bool(re.search(r'"type"\s*:\s*"text"', tail))
                has_reasoning = bool(
                    re.search(r'"type"\s*:\s*"reasoning"', tail)
                )
                if has_tool and not has_final_text:
                    nm = re.search(
                        r'"(?:name|toolName)"\s*:\s*"([^"]+)"',
                        tail,
                    )
                    return (
                        "running-tool",
                        nm.group(1) if nm else None,
                        None,
                    )
                if has_final_text:
                    return ("idle", None, None)
                if has_reasoning:
                    return ("thinking", None, None)
                return ("idle", None, None)
            if role in ("user", "tool"):
                return ("thinking", None, None)
        return None

    def extract_role(obj):
        if isinstance(obj, dict):
            role = obj.get("role") or obj.get("type")
            if role in ("user", "assistant", "system", "tool"):
                return role
            if obj.get("type") in ("user", "assistant") and isinstance(
                obj.get("message"), dict
            ):
                return extract_role(obj["message"])
            msg = obj.get("message")
            if isinstance(msg, dict):
                return extract_role(msg)
        return None

    def extract_tool(obj):
        if isinstance(obj, dict):
            if obj.get("type") in ("tool_use", "tool-call", "tool_call") or obj.get(
                "role"
            ) == "tool":
                name = obj.get("name") or obj.get("toolName") or obj.get("tool_name") or ""
                inp = obj.get("input") or obj.get("args") or {}
                return (name, tool_target_from(inp) if isinstance(inp, dict) else "")
            for v in obj.values():
                r = extract_tool(v)
                if r[0]:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = extract_tool(v)
                if r[0]:
                    return r
        return (None, None)

    state, tool_name, tool_target, classified = "idle", None, None, False
    summary = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except Exception:
        return {"state": "idle", "ageMs": age_ms}
    try:
        # Blobs are content-addressed SHA-256 ids — ORDER BY id is NOT recency.
        # meta.latestRootBlobId is the real tip of the session tree.
        latest_id = None
        try:
            for _k, val in conn.execute("SELECT key, value FROM meta"):
                meta = decode_meta_value(val)
                if not isinstance(meta, dict):
                    continue
                tip = meta.get("latestRootBlobId") or meta.get("latest_root_blob_id")
                if isinstance(tip, str) and tip.strip():
                    latest_id = tip.strip()
                    break
        except Exception:
            latest_id = None

        if latest_id:
            try:
                row = conn.execute(
                    "SELECT data FROM blobs WHERE id = ?", (latest_id,)
                ).fetchone()
            except Exception:
                row = None
            if row:
                got = classify_blob_text(blob_to_text(row[0]))
                if got:
                    state, tool_name, tool_target = got
                    classified = True

        if not classified:
            # Fallback: scan blobs but do NOT trust id ordering — prefer any
            # assistant+text (idle) over a random user/tool hit.
            try:
                blob_rows = conn.execute("SELECT id, data FROM blobs").fetchall()
            except Exception:
                blob_rows = []
            idle_hit = None
            busy_hit = None
            for _bid, data in blob_rows:
                got = classify_blob_text(blob_to_text(data))
                if not got:
                    continue
                st, tn, tt = got
                if st == "idle":
                    idle_hit = (st, tn, tt)
                elif st == "running-tool" and busy_hit is None:
                    busy_hit = (st, tn, tt)
                elif st == "thinking" and busy_hit is None:
                    busy_hit = (st, tn, tt)
            # Without a tip pointer, bias toward idle if we saw a completed
            # assistant — hash order otherwise invents permanent thinking.
            if idle_hit and age_ms > 5_000:
                state, tool_name, tool_target = idle_hit
                classified = True
            elif busy_hit:
                state, tool_name, tool_target = busy_hit
                classified = True
            elif idle_hit:
                state, tool_name, tool_target = idle_hit
                classified = True

        if not classified:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            for _k, val in rows:
                meta = decode_meta_value(val)
                if meta is None:
                    continue
                role = extract_role(meta)
                if role == "assistant":
                    tn, tt = extract_tool(meta)
                    if tn:
                        state = "running-tool"
                        tool_name, tool_target = tn, tt
                    else:
                        state = "idle"
                    classified = True
                elif role in ("user", "tool"):
                    state = "thinking"
                    classified = True

        try:
            blob_rows = conn.execute(
                "SELECT data FROM blobs ORDER BY rowid DESC LIMIT 80"
            ).fetchall()
        except Exception:
            blob_rows = []
        for (data,) in blob_rows:
            text = blob_to_text(data)
            if not text:
                continue
            if _USER_QUERY_RE.search(text):
                summary = summarize_user_prompt(text)
                if summary:
                    break
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            if extract_role(obj) != "user":
                continue
            if is_context_user_tail(text):
                continue
            content = None
            if isinstance(obj, dict):
                content = obj.get("content")
                msg = obj.get("message")
                if content is None and isinstance(msg, dict):
                    content = msg.get("content")
            summary = summarize_user_prompt(text_from_content(content))
            if summary:
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Do not age_out_busy here. Cursor often streams assistant text (disk
    # looks idle/done) and long tools never touch store.db — the TUI chrome
    # in apply_cursor_pane_attention is the source of truth for idle.
    result = {
        "state": state,
        "toolName": tool_name,
        "toolTarget": tool_target,
        "ageMs": age_ms,
    }
    if summary:
        result["summary"] = summary
    return result

def discover_cursor(session, window):
    home = Path.home()
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []

    def looks_like_cursor(cmd):
        c = (cmd or "").lower()
        if "cursor-agent" in c:
            return True
        parts = c.split()
        if not parts:
            return False
        base = Path(parts[0]).name
        if base == "agent" and ("cursor" in c or "cursor-agent" in c):
            return True
        return base == "cursor-agent"

    agent_pids = [p for p in pids if looks_like_cursor(read_cmdline(p))]
    if not agent_pids:
        agent_pids = pids

    def chat_roots():
        roots = []
        cursor_cfg = os.environ.get("CURSOR_CONFIG_DIR", "").strip()
        if cursor_cfg:
            roots.append(Path(cursor_cfg) / "chats")
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        cfg = Path(xdg) if xdg else (home / ".config")
        for root in (cfg / "cursor" / "chats", home / ".cursor" / "chats"):
            if root not in roots:
                roots.append(root)
        return roots

    def is_cursor_store(norm):
        return norm.endswith("/store.db") and (
            "/.config/cursor/chats/" in norm
            or "/.cursor/chats/" in norm
            or "/cursor/chats/" in norm
        )

    def project_dirs(path):
        if not path:
            return []
        try:
            resolved = str(Path(path).resolve())
        except Exception:
            resolved = path
        digest = hashlib.md5(resolved.encode()).hexdigest()
        return [root / digest for root in chat_roots()]

    store = None
    for p in agent_pids + pids:
        for path in read_open_paths(p):
            norm = path.replace("\\", "/")
            if is_cursor_store(norm):
                cand = Path(path)
                if cand.is_file():
                    store = cand
                    break
        if store:
            break

    if store is None and cwd:
        files = []
        for root in project_dirs(cwd):
            if root.is_dir():
                files.extend(root.glob("*/store.db"))
        if len(files) == 1:
            store = files[0]
        elif len(files) > 1:
            pane_text = capture_pane(target, True)
            for f in files:
                if f.parent.name and len(f.parent.name) >= 6 and f.parent.name in pane_text:
                    store = f
                    break
            if store is None:
                store = pick_by_activity(files, target)

    return str(store) if store else None, None


# Cursor keeps the composer ❯ on screen while generating. Shared idle-prompt
# demotion (Pi/Claude) therefore flips thinking→idle mid-turn.
#
# Live working chrome (v2026+ Ink TUI), in priority order:
#   1. `ctrl+c to stop` right-aligned on the composer — rendered every frame
#      of a turn. Spinner text above it varies (`⠘⠤ Composing`, `⠠⠛ Running`).
#   2. Braille/glyph spinner + verb in the status slot immediately above the
#      input — e.g. "⠠⠜ Reading  8.96k tokens".
# Done: composer is back, that status slot is empty, and ctrl+c is gone.
_CURSOR_BUSY_VERBS = (
    r"(thinking|composing|working|generating|running|editing|reading|"
    r"searching|planning|writing|applying|executing|fetching|listing|"
    r"compacting|summarizing|coding)"
)
_CURSOR_SPINNER = r"(?:[\u2800-\u28FF]+|[^\w\s]{1,4})"
_CURSOR_TOKENS = r"\d[\d.,]*\s*[kKmM]?\s*tokens?"
CURSOR_BUSY_LINE_RE = re.compile(
    r"(?i)^\s*"
    + _CURSOR_SPINNER
    + r"\s+"
    + _CURSOR_BUSY_VERBS
    + r"\b\s+"
    + _CURSOR_TOKENS
)
# Spinner + verb still counts if capture-pane clips the token count or the
# footer appends model / auto-review badges on the same line.
CURSOR_BUSY_STRIP_RE = re.compile(
    r"(?i)^\s*" + _CURSOR_SPINNER + r"\s+" + _CURSOR_BUSY_VERBS + r"\b"
)
CURSOR_INTERRUPT_RE = re.compile(r"(?i)ctrl\s*\+\s*c\s+to\s+stop")
CURSOR_PLACEHOLDER_RE = re.compile(
    r"(?i)add a follow-up|plan, search, build anything"
)
# Current allowlist menu (not `[y] approve`). Require the question *and* an
# option row so leftover "Run this command?" in a short transcript is ignored.
# Idle footer badge "Run Everything" must not match on its own.
CURSOR_ALLOWLIST_QUESTION_RE = re.compile(r"(?i)run this command\?")
CURSOR_ALLOWLIST_OPTIONS_RE = re.compile(
    r"(?i)(?:"
    r"run\s*\(\s*once\s*\)|"
    r"not in allowlist|"
    r"add \S+ to allowlist|"
    r"skip\s*\(\s*esc|"
    r"skip & tell"
    r")"
)
CURSOR_TRUST_RE = re.compile(
    r"(?i)(?:"
    r"workspace trust required|"
    r"trust this workspace|"
    r"do you trust the contents of this directory"
    r")"
)
_CURSOR_TOOL_VERBS = frozenset(
    ("running", "editing", "reading", "executing", "fetching", "listing")
)


def _cursor_composer_index(lines):
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        if IDLE_PROMPT_LINE_RE.match(line) or IDLE_PROMPT_END_RE.search(line):
            return i
        if CURSOR_PLACEHOLDER_RE.search(line):
            return i
        if CURSOR_INTERRUPT_RE.search(line):
            return i
    return None


def cursor_status_region(lines):
    """Composer plus the live status slot immediately above it, and footer."""
    idx = _cursor_composer_index(lines)
    if idx is None:
        return "\n".join(lines[-6:])
    found = 0
    start = idx
    for i in range(idx - 1, -1, -1):
        start = i
        if lines[i].strip():
            found += 1
            # Status is one line above the input — don't scan transcript.
            if found >= 1:
                break
    end = idx
    for i in range(idx + 1, len(lines)):
        if lines[i].strip():
            end = i
            break
    return "\n".join(lines[start : end + 1])


def _cursor_line_busy_verb(raw):
    if not raw:
        return None
    m = CURSOR_BUSY_LINE_RE.search(raw) or CURSOR_BUSY_STRIP_RE.search(raw)
    return m.group(1).lower() if m else None


def _cursor_busy_verb(text, lines):
    """Working chrome: ctrl+c on the composer, or spinner in the status slot.

    Tool output can sit between a far-away spinner and the input — ctrl+c is
    the reliable mid-turn marker then. A leftover spinner in the transcript
    must not keep the agent busy after the status slot has cleared.
    """
    if CURSOR_INTERRUPT_RE.search(text or ""):
        region = cursor_status_region(lines)
        for line in region.splitlines():
            verb = _cursor_line_busy_verb(line.strip())
            if verb:
                return verb
        return "thinking"
    for line in cursor_status_region(lines).splitlines():
        verb = _cursor_line_busy_verb(line.strip())
        if verb:
            return verb
    return None


def cursor_shows_permission(lines):
    """Live allowlist / trust / legacy y/n overlay — not leftover transcript."""
    tail = "\n".join(lines[-20:])
    if CURSOR_TRUST_RE.search(tail):
        return True
    if CURSOR_ALLOWLIST_QUESTION_RE.search(tail) and CURSOR_ALLOWLIST_OPTIONS_RE.search(
        tail
    ):
        return True
    if PERMISSION_RE.search(live_permission_region(lines)):
        return True
    return False


def cursor_shows_idle(text, lines):
    """Composer on screen, nothing in the status slot, no ctrl+c, no overlay.

    That is the done TUI. `Add a follow-up` is also painted mid-turn, so it
    is not required — absence of working chrome is the idle signal.
    """
    if CURSOR_INTERRUPT_RE.search(text or ""):
        return False
    if _cursor_busy_verb(text, lines):
        return False
    if cursor_shows_permission(lines):
        return False
    return _cursor_composer_index(lines) is not None


def apply_cursor_pane_attention(state, text):
    """Cursor TUI chrome is authoritative. Disk/stale-store idle must not win."""
    if not text:
        return state
    lines = text.splitlines()
    # Working first: leftover allowlist text can still sit in the last 20
    # lines after y/n, but `ctrl+c to stop` means the turn resumed.
    verb = _cursor_busy_verb(text, lines)
    if verb in _CURSOR_TOOL_VERBS:
        return "running-tool"
    if verb:
        return "thinking"
    if cursor_shows_permission(lines):
        return "waiting-permission"
    if cursor_shows_idle(text, lines):
        return "idle"
    # No composer in the capture (blank/failed pane) — keep incoming state.
    err_tail = "\n".join(lines[-8:])
    if state == "idle" and AGENT_ERROR_RE.search(err_tail):
        return "errored"
    return state


HARNESS_PANE_ATTENTION["cursor"] = apply_cursor_pane_attention
