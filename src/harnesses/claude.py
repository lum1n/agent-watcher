"""Claude Code discover + classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.
"""

def classify_claude(path):
    if not path or not os.path.isfile(path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    now = time.time()
    entries = read_jsonl_tail(path)
    if entries is None:
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    if not entries:
        return {"state": "idle", "ageMs": 0}
    last_ts, state, tool_name, tool_target = 0.0, "idle", None, None
    for ev in reversed(entries):
        et = ev.get("type", "")
        if et not in ("assistant", "user"):
            continue
        last_ts = parse_ts(ev.get("timestamp") or ev.get("time"))
        msg = ev.get("message") or {}
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if et == "assistant":
            tool = find_tool_use(content)
            blocks = content_blocks(content)
            has_thinking = any(
                isinstance(b, dict)
                and b.get("type") in ("thinking", "reasoning")
                for b in blocks
            )
            has_text = any(
                isinstance(b, dict)
                and b.get("type") == "text"
                and str(b.get("text") or "").strip()
                for b in blocks
            )
            has_stop_key = isinstance(msg, dict) and (
                "stop_reason" in msg or "stopReason" in msg
            )
            stop = None
            if isinstance(msg, dict):
                stop = msg.get("stop_reason")
                if stop is None:
                    stop = msg.get("stopReason")
            stop_s = str(stop).strip() if stop is not None else ""
            if tool:
                # Tool-only / tool_use stop ⇒ still running. Text with an
                # end_turn (or no pending tool stop) ⇒ finished.
                if has_text and stop_s in ("end_turn", "max_tokens", "stop_sequence"):
                    state = "idle"
                elif has_text and not has_stop_key:
                    state = "idle"
                elif stop_s in ("tool_use", "toolUse", "pending") or not has_text:
                    state = "running-tool"
                    tool_name = tool_name_from(tool)
                    tool_target = tool_target_from(tool_input_from(tool))
                else:
                    state = "idle"
            else:
                # Claude Code streams with stop_reason=null until the turn
                # finishes. Older/test transcripts omit the key entirely —
                # those with final text are idle.
                if has_stop_key and stop is None:
                    state = "thinking"
                elif stop_s in ("tool_use", "toolUse", "pending"):
                    state = "thinking"
                elif has_thinking and not has_text:
                    state = "thinking"
                else:
                    state = "idle"
            break
        state = "thinking"
        break
    if not last_ts:
        last_ts = mtime(path)
    age_ms = int((now - last_ts) * 1000) if last_ts else 0
    if age_ms < 0:
        age_ms = 0
    state = age_out_busy(state, age_ms)
    summary = last_user_summary_from_events(entries)
    result = {
        "state": state,
        "toolName": tool_name,
        "toolTarget": tool_target,
        "ageMs": age_ms,
    }
    if summary:
        result["summary"] = summary
    return result

def claude_child_pids(proc_pids):
    """Descendants of the innermost Claude CLI process(es) in proc_pids."""
    children = process_children_map()
    out, seen = [], set()
    for p in proc_pids:
        cmd = read_cmdline(p)
        argv0 = Path(cmd.split(" ", 1)[0]).name.lower()
        # Native binary, or an npm install run through node/bun.
        if argv0 != "claude" and not (
            argv0 in ("node", "bun") and detect_kind(cmd) == "claude"
        ):
            continue
        for c in list_descendant_pids(p, children)[1:]:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


# Claude keeps its `❯` composer on screen mid-turn, so the shared idle-prompt
# demotion would always flip a working Claude to idle. The footer carries
# `esc to interrupt` only while a turn is in flight.
CLAUDE_BUSY_RE = re.compile(r"(?i)\besc to interrupt\b")


def apply_claude_pane_attention(state, text):
    if not text:
        return state
    lines = text.splitlines()
    if PERMISSION_RE.search(live_permission_region(lines)):
        return "waiting-permission"
    if state in ("thinking", "running-tool") and CLAUDE_BUSY_RE.search(
        "\n".join(lines[-6:])
    ):
        return state
    return apply_default_pane_attention(state, text)


HARNESS_PANE_ATTENTION["claude"] = apply_claude_pane_attention


def discover_claude(session, window):
    home = Path.home()
    # Claude has used both roots; keep both.
    project_roots = [
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []

    def path_slugs(project_cwd):
        """Claude encodes project paths by turning separators into '-' and
        keeping other characters (e.g. dots). Older Sessh used a broader
        non-alnum → '-' slug — try both."""
        if not project_cwd:
            return []
        norm = project_cwd.replace("\\", "/")
        slugs = [
            norm.replace("/", "-"),  # Claude canonical
            re.sub(r"[^A-Za-z0-9]", "-", project_cwd),  # legacy
        ]
        out, seen = [], set()
        for s in slugs:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def project_files(project_cwd):
        files = []
        for root in project_roots:
            if not root.is_dir():
                continue
            for slug in path_slugs(project_cwd):
                d = root / slug
                if not d.is_dir():
                    continue
                files.extend(d.glob("*.jsonl"))
                sess_dir = d / "sessions"
                if sess_dir.is_dir():
                    files.extend(sess_dir.glob("*.jsonl"))
        # de-dupe
        seen, out = set(), []
        for f in files:
            s = str(f)
            if s not in seen:
                seen.add(s)
                out.append(f)
        return out

    def session_id_from_procs(proc_pids):
        for p in proc_pids:
            cmd = read_cmdline(p)
            m = re.search(r"--session-id(?:\s+|=)(\S+)", cmd)
            if m:
                return m.group(1).strip()
            m = re.search(r"--resume(?:\s+|=)([0-9a-fA-F-]{36})", cmd)
            if m:
                return m.group(1).strip()
        # Claude exports its session id to the processes it spawns (MCP
        # servers, tool shells). Only trust those: the pane shell, a bwrap
        # wrapper or claude itself may have inherited a *parent* Claude's id
        # when tmux or the agent was launched from inside another session.
        for p in claude_child_pids(proc_pids):
            env = read_proc_environ(p)
            for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
                val = (env.get(key) or "").strip()
                if val:
                    return val
        return None

    def find_by_sid(files, sid):
        if not sid:
            return None
        for f in files:
            if f.stem == sid or sid in f.name:
                return f
        hits = []
        for root in project_roots:
            if not root.is_dir():
                continue
            hits.extend(root.glob("*/" + sid + ".jsonl"))
            hits.extend(root.glob("*/sessions/" + sid + ".jsonl"))
        return max(hits, key=mtime) if hits else None

    def looks_like_claude_jsonl(norm):
        return norm.endswith(".jsonl") and (
            "/.claude/" in norm or "/claude/projects/" in norm
        )

    jsonl = None
    for p in pids:
        for path in read_open_paths(p):
            norm = path.replace("\\", "/")
            if looks_like_claude_jsonl(norm):
                cand = Path(path)
                if cand.is_file():
                    jsonl = cand
                    break
        if jsonl:
            break

    files = project_files(cwd)
    if jsonl is None:
        sid = session_id_from_procs(pids)
        hit = find_by_sid(files, sid) if sid else None
        if hit is None and sid:
            hit = find_by_sid([], sid)
        if hit is not None:
            jsonl = hit

    if jsonl is None and files:
        pane_text = capture_pane(target, True)
        for f in files:
            if f.stem and len(f.stem) >= 8 and f.stem in pane_text:
                jsonl = f
                break

    if jsonl is None and files:
        hit = pick_by_activity(files, target)
        if hit is not None:
            jsonl = hit

    if jsonl is None and len(files) == 1:
        jsonl = files[0]

    # Last resort: newest recently-touched jsonl under project roots whose
    # absolute path slug matches cwd — handles odd encodings.
    if jsonl is None and cwd:
        candidates = []
        for root in project_roots:
            if not root.is_dir():
                continue
            for f in root.rglob("*.jsonl"):
                # skip subagent transcripts nested under sessions/
                if "subagent" in f.name.lower():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                # Prefer files touched in the last 24h
                if time.time() - st.st_mtime > 86400:
                    continue
                candidates.append(f)
        if candidates:
            hit = pick_by_activity(candidates, target)
            if hit is None and len(candidates) == 1:
                hit = candidates[0]
            if hit is not None:
                jsonl = hit

    return str(jsonl) if jsonl else None, None
