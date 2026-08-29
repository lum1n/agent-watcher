"""Pi discover + classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.
Uses watcher helpers: target_for, tmux_fmt, capture_pane, pick_by_activity,
clear_content_winner, list_descendant_pids, read_cmdline, read_open_paths,
read_proc_environ, read_jsonl_tail, find_tool_use, content_blocks, parse_ts,
age_out_busy, mtime.
"""

def classify_pi(path):
    if not path or not os.path.isfile(path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    now = time.time()
    raw = read_jsonl_tail(path)
    if raw is None:
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    entries, order = {}, []
    for ev in raw:
        eid = ev.get("id")
        if not eid:
            continue
        entries[eid] = ev
        order.append(eid)
    if not order:
        return {"state": "idle", "ageMs": 0}
    leaf_id = order[-1]
    seen, cur, path_ids = set(), leaf_id, []
    while cur and cur not in seen:
        seen.add(cur)
        path_ids.append(cur)
        cur = entries.get(cur, {}).get("parentId")
    path_ids.reverse()
    state, tool_name, tool_target, last_ts = "idle", None, None, 0.0
    for eid in reversed(path_ids):
        ev = entries.get(eid) or {}
        if ev.get("type", "") != "message":
            continue
        msg = ev.get("message") or {}
        role = msg.get("role", "")
        content = msg.get("content") or []
        ts = msg.get("timestamp") or ev.get("timestamp")
        last_ts = parse_ts(ts) or last_ts
        if role == "assistant":
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
            if tool:
                # Pi often leaves toolCall blocks on the leaf assistant message
                # even after the turn finished with text. Text ⇒ done.
                if has_text:
                    state = "idle"
                else:
                    state = "running-tool"
                    tool_name = tool_name_from(tool)
                    tool_target = tool_target_from(tool_input_from(tool))
            else:
                # Mid-turn: thinking-only / stopReason toolUse before toolCall lands.
                stop = str(msg.get("stopReason") or "")
                if has_thinking and not has_text:
                    state = "thinking"
                elif stop in ("toolUse", "tool_use", "pending"):
                    state = "thinking"
                else:
                    state = "idle"
            break
        elif role in ("user", "tool", "toolResult"):
            state = "thinking"
            break
    if not last_ts:
        last_ts = mtime(path)
    age_ms = int((now - last_ts) * 1000) if last_ts else 0
    if age_ms < 0:
        age_ms = 0
    state = age_out_busy(state, age_ms)
    path_events = [entries.get(eid) for eid in path_ids]
    summary = last_user_summary_from_events(path_events)
    result = {
        "state": state,
        "toolName": tool_name,
        "toolTarget": tool_target,
        "ageMs": age_ms,
    }
    if summary:
        result["summary"] = summary
    return result

def score_pi_jsonl(path, pane_text):
    """Score a Pi JSONL against pane text (mirrors history.ts).

    Filename uuids rarely appear in the TUI — message text overlap is the
    durable signal when several sessions share one cwd slug.
    """
    if not pane_text:
        return 0
    try:
        p = Path(path)
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > 256 * 1024:
                f.seek(-256 * 1024, os.SEEK_END)
                f.readline()
            lines = f.read().decode("utf-8", "ignore").splitlines()[-150:]
    except Exception:
        return 0
    score = 0
    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "message":
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text")
                    if isinstance(t, str):
                        texts.append(t)
                elif isinstance(c, str):
                    texts.append(c)
        for t in texts:
            norm = re.sub(r"\s+", " ", t).strip()
            if len(norm) < 24:
                continue
            needle = norm[:80]
            if needle in seen:
                continue
            seen.add(needle)
            if needle in pane_text or norm[:40] in pane_text:
                score += 2 if msg.get("role") == "user" else 1
            if score >= 8:
                return score
    return score


def peek_pi_open_jsonl(session, window):
    """Return the Pi session JSONL currently open by the pane's pi process."""
    target = target_for(session, window)
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    if not pid:
        return None
    pids = list_descendant_pids(pid)

    def looks_like_pi(cmd):
        parts = cmd.split()
        if not parts:
            return False
        base = Path(parts[0]).name.lower()
        return base == "pi" or base.startswith("pi-")

    agent_pids = [p for p in pids if looks_like_pi(read_cmdline(p))]
    if not agent_pids:
        agent_pids = pids

    best = None
    best_mtime = -1.0
    for p in agent_pids + pids:
        for path in read_open_paths(p):
            norm = path.replace("\\", "/")
            if not path.endswith(".jsonl"):
                continue
            if "/.pi/" not in norm and "/agent/sessions/" not in norm:
                continue
            cand = Path(path)
            if not cand.is_file():
                continue
            mt = mtime(cand)
            if mt >= best_mtime:
                best_mtime = mt
                best = cand
    return str(best) if best else None


def discover_pi(session, window):
    home = Path.home()
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []

    def looks_like_pi(cmd):
        parts = cmd.split()
        if not parts:
            return False
        base = Path(parts[0]).name.lower()
        return base == "pi" or base.startswith("pi-")

    agent_pids = [p for p in pids if looks_like_pi(read_cmdline(p))]
    if not agent_pids:
        agent_pids = pids

    def encode_cwd(p):
        # Pi: `--${resolvedCwd.replace(/^[/\\]/, "").replace(/[/\\:]/g, "-")}--`
        try:
            resolved = str(Path(p).resolve())
        except Exception:
            resolved = p
        slugs = []
        for candidate in (resolved, p):
            if not candidate:
                continue
            norm = candidate.replace("\\", "/").strip("/")
            slugs.append("--" + re.sub(r"[/\\:]", "-", norm) + "--")
        out, seen = [], set()
        for s in slugs:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def agent_roots_from_env():
        roots = []
        for p in agent_pids + pids:
            env = read_proc_environ(p)
            if env.get("PI_CODING_AGENT_SESSION_DIR"):
                roots.append(Path(env["PI_CODING_AGENT_SESSION_DIR"]))
            if env.get("PI_CODING_AGENT_DIR"):
                roots.append(Path(env["PI_CODING_AGENT_DIR"]) / "sessions")
        roots.append(home / ".pi" / "agent" / "sessions")
        seen, out = set(), []
        for r in roots:
            s = str(r)
            if s not in seen:
                seen.add(s)
                out.append(r)
        return out

    # 1) Open FDs — strongest live-session signal. Prefer newest mtime when
    # several jsonl fds are visible (parent shells / prior sessions).
    jsonl = None
    open_path = peek_pi_open_jsonl(session, window)
    if open_path:
        jsonl = Path(open_path)

    if jsonl is None:
        for p in agent_pids:
            cmd = read_cmdline(p)
            m = re.search(r"(?:^|\s)--session\s+(\S+)", cmd)
            if m:
                arg = m.group(1)
                cand = Path(arg)
                if cand.is_file():
                    jsonl = cand
                    break
                for root in agent_roots_from_env():
                    if not root.is_dir():
                        continue
                    hits = list(root.rglob("*" + arg + "*.jsonl"))
                    if hits:
                        jsonl = max(hits, key=mtime)
                        break
            if jsonl:
                break

    if jsonl is None and cwd:
        for root in agent_roots_from_env():
            if not root.is_dir():
                continue
            for slug in encode_cwd(cwd):
                d = root / slug
                if not d.is_dir():
                    continue
                files = list(d.glob("*.jsonl"))
                if not files:
                    continue
                if len(files) == 1:
                    jsonl = files[0]
                    break
                pane_text = capture_pane(target, True)
                for f in files:
                    stem = f.stem
                    uuid = stem.split("_")[-1] if "_" in stem else stem
                    if (stem and stem in pane_text) or (
                        uuid and len(uuid) >= 8 and uuid in pane_text
                    ):
                        jsonl = f
                        break
                if jsonl:
                    break
                # Pane-content overlap — history uses this and succeeds when
                # activity is ambiguous across parallel same-cwd sessions.
                # Without it, leaving chat (full snapshot) permanently unbinds
                # the pane even though chat itself resolved the store.
                scored = [(score_pi_jsonl(f, pane_text), f) for f in files]
                winner = clear_content_winner(scored, min_score=2, min_gap=1)
                if winner is not None:
                    jsonl = winner
                    break
                hit = pick_by_activity(files, target)
                if hit is not None:
                    jsonl = hit
                    break
            if jsonl:
                break

    return str(jsonl) if jsonl else None, None


HARNESS_LIVE_STORE["pi"] = peek_pi_open_jsonl
