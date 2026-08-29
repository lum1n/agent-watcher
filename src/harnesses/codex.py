"""Codex discover + classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.
"""

def classify_codex(path):
    if not path or not os.path.isfile(path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    now = time.time()
    entries = read_jsonl_tail(path)
    if entries is None:
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    if not entries:
        return {"state": "idle", "ageMs": 0}
    last_ts = mtime(path)
    age_ms = int((now - last_ts) * 1000) if last_ts else 0
    if age_ms < 0:
        age_ms = 0
    state, tool_name, tool_target = "idle", None, None
    for ev in reversed(entries):
        role = ev.get("role", "") or ev.get("type", "")
        content = ev.get("content", "")
        if role == "assistant":
            tool = find_tool_use(content)
            if tool:
                state = "running-tool"
                tool_name = tool_name_from(tool)
                tool_target = tool_target_from(tool_input_from(tool))
            else:
                state = "idle"
            break
        elif role in ("user", "tool", "toolResult"):
            state = "thinking"
            break
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

def discover_codex(session, window):
    """Bind Codex session without newest-global fallback when ambiguous."""
    home = Path.home()
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []

    jsonl = None
    for p in pids:
        for path in read_open_paths(p):
            if "/.codex/" in path.replace("\\", "/") and (
                path.endswith(".jsonl") or path.endswith(".json")
            ):
                cand = Path(path)
                if cand.is_file():
                    jsonl = cand
                    break
        if jsonl:
            break

    if jsonl is None:
        roots = [home / ".codex" / "sessions", home / ".codex" / "history"]
        files = []
        for root in roots:
            if root.is_dir():
                files.extend(list(root.glob("*.jsonl")) + list(root.glob("*.json")))
        if len(files) == 1:
            jsonl = files[0]
        elif len(files) > 1:
            pane_text = capture_pane(target, True)
            for f in files:
                stem = f.stem
                if stem and len(stem) >= 8 and stem in pane_text:
                    jsonl = f
                    break
            if jsonl is None:
                jsonl = pick_by_activity(files, target)
            # Never pick newest-by-mtime when multiple candidates exist.

    return str(jsonl) if jsonl else None, None
