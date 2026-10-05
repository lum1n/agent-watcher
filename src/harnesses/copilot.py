"""GitHub Copilot CLI discover + classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.

Sessions live under ${COPILOT_HOME:-~/.copilot}/session-state/:
  <uuid>/events.jsonl          (current)
  <uuid>.jsonl                 (legacy, pre-1.0.11)

Event envelope: {type, data, id, parentId?, timestamp}. Decisive types:
  permission.requested / permission.completed
  tool.execution_start / tool.execution_complete
  assistant.turn_start / assistant.message / assistant.turn_end
  user.message / session.error / session.shutdown
"""

# Intent telemetry — not a real tool for status display.
_COPILOT_SKIP_TOOLS = frozenset(("report_intent",))

# Noise / telemetry — never decisive for idle/busy.
_COPILOT_SKIP_TYPES = frozenset(
    (
        "session.start",
        "session.resume",
        "session.info",
        "session.model_change",
        "session.truncation",
        "session.auto_mode_resolved",
        "session.usage_checkpoint",
        "system.message",
        "system.notification",
        "hook.start",
        "hook.end",
        "assistant.intent",
        "subagent.started",
        "subagent.completed",
    )
)


def copilot_home():
    env = (os.environ.get("COPILOT_HOME") or "").strip()
    if env:
        return Path(env)
    return Path.home() / ".copilot"


def copilot_session_root():
    return copilot_home() / "session-state"


def _copilot_data(ev):
    data = ev.get("data") if isinstance(ev, dict) else None
    return data if isinstance(data, dict) else {}


def _copilot_tool_from_requests(reqs):
    if not isinstance(reqs, list):
        return None, None
    for req in reqs:
        if not isinstance(req, dict):
            continue
        name = (req.get("name") or req.get("toolName") or "").strip()
        if not name or name in _COPILOT_SKIP_TOOLS:
            continue
        args = req.get("arguments") or req.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        return name, tool_target_from(args)
    return None, None


def last_user_summary_copilot(entries):
    if not entries:
        return None
    for ev in reversed(entries):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "user.message":
            continue
        content = _copilot_data(ev).get("content")
        summary = summarize_user_prompt(text_from_content(content))
        if summary:
            return summary
    return None


def classify_copilot(path):
    if not path or not os.path.isfile(path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    now = time.time()
    entries = read_jsonl_tail(path)
    if entries is None:
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    if not entries:
        return {"state": "idle", "ageMs": 0}

    last_ts, state = 0.0, "idle"
    tool_name, tool_target = None, None

    for ev in reversed(entries):
        if not isinstance(ev, dict):
            continue
        et = ev.get("type") or ""
        if et in _COPILOT_SKIP_TYPES:
            continue
        data = _copilot_data(ev)
        ts = parse_ts(ev.get("timestamp"))
        if ts and not last_ts:
            last_ts = ts

        if et == "permission.requested":
            state = "waiting-permission"
            break
        if et == "permission.completed":
            # Overlay resolved; keep scanning for the turn state under it.
            continue
        if et == "tool.execution_start":
            state = "running-tool"
            name = (data.get("toolName") or data.get("name") or "").strip()
            if name and name not in _COPILOT_SKIP_TOOLS:
                tool_name = name
                args = data.get("arguments") or data.get("args") or {}
                if isinstance(args, dict):
                    tool_target = tool_target_from(args)
            break
        if et == "tool.execution_complete":
            # Tool finished; model usually continues the turn.
            state = "thinking"
            break
        if et in (
            "assistant.turn_start",
            "assistant.reasoning",
            "assistant.reasoning_delta",
            "assistant.streaming_delta",
            "assistant.message_delta",
        ):
            state = "thinking"
            break
        if et == "assistant.message":
            reqs = data.get("toolRequests") or []
            content = str(data.get("content") or "").strip()
            tn, tt = _copilot_tool_from_requests(reqs)
            if tn:
                state = "running-tool"
                tool_name, tool_target = tn, tt
            elif content:
                state = "idle"
            else:
                # Empty assistant payload often means tools-only work already
                # covered by tool.* events we would have hit above — or still
                # streaming. Treat as thinking.
                state = "thinking"
            break
        if et == "assistant.turn_end":
            state = "idle"
            break
        if et == "user.message":
            state = "thinking"
            break
        if et == "session.error":
            state = "errored"
            break
        if et == "session.shutdown":
            state = "idle"
            break

    if not last_ts:
        last_ts = mtime(path)
    age_ms = int((now - last_ts) * 1000) if last_ts else 0
    if age_ms < 0:
        age_ms = 0
    state = age_out_busy(state, age_ms)
    summary = last_user_summary_copilot(entries)
    result = {
        "state": state,
        "toolName": tool_name,
        "toolTarget": tool_target,
        "ageMs": age_ms,
    }
    if summary:
        result["summary"] = summary
    return result


def _read_workspace_cwd(session_dir):
    wp = Path(session_dir) / "workspace.yaml"
    if not wp.is_file():
        return None
    try:
        text = wp.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("cwd:"):
            return s.split(":", 1)[1].strip().strip("\"'")
    return None


def _cwd_variants(cwd):
    if not cwd:
        return []
    variants = [cwd, cwd.rstrip("/")]
    try:
        variants.append(str(Path(cwd).resolve()))
    except Exception:
        pass
    for v in list(variants):
        if v.startswith("/private/"):
            variants.append(v[len("/private") :])
        elif v.startswith("/var/") or v == "/var":
            variants.append("/private" + v)
    out, seen = [], set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _session_id_from_cmdline(cmd):
    if not cmd:
        return None
    m = re.search(r"--resume(?:=|\s+)([0-9a-fA-F-]{36})", cmd)
    if m:
        return m.group(1)
    m = re.search(r"--session(?:=|\s+)([0-9a-fA-F-]{36})", cmd)
    if m:
        return m.group(1)
    return None


def _looks_like_copilot_store(norm):
    if "/.copilot/session-state/" not in norm and "/copilot/session-state/" not in norm:
        return False
    if norm.endswith("/events.jsonl"):
        return True
    # Legacy flat layout: <uuid>.jsonl directly under session-state/
    if norm.endswith(".jsonl") and "/session-state/" in norm:
        # Exclude nested non-transcript jsonl if any
        parts = norm.split("/session-state/", 1)[-1]
        return "/" not in parts.rstrip("/")
    return False


def _list_session_files(root):
    """Return transcript paths under session-state (current + legacy)."""
    if not root.is_dir():
        return []
    files = []
    try:
        for child in root.iterdir():
            if child.is_dir():
                ev = child / "events.jsonl"
                if ev.is_file():
                    files.append(ev)
            elif child.is_file() and child.suffix == ".jsonl":
                files.append(child)
    except OSError:
        return []
    return files


def _files_for_cwd(root, cwd):
    if not cwd or not root.is_dir():
        return []
    wanted = set(_cwd_variants(cwd))
    hits = []
    for f in _list_session_files(root):
        # Prefer workspace.yaml cwd; fall back to session.start in the file.
        session_cwd = None
        if f.name == "events.jsonl":
            session_cwd = _read_workspace_cwd(f.parent)
        if not session_cwd:
            try:
                with f.open("r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh):
                        if i > 40:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("type") == "session.start":
                            ctx = _copilot_data(ev).get("context") or {}
                            if isinstance(ctx, dict):
                                session_cwd = ctx.get("cwd")
                            break
            except OSError:
                pass
        if session_cwd and session_cwd in wanted:
            hits.append(f)
            continue
        if session_cwd:
            for v in _cwd_variants(session_cwd):
                if v in wanted:
                    hits.append(f)
                    break
    return hits


def discover_copilot(session, window):
    """Bind this pane to a Copilot CLI events.jsonl (or legacy flat jsonl)."""
    root = copilot_session_root()
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []

    def looks_like_copilot(cmd):
        c = (cmd or "").lower()
        if not c:
            return False
        parts = c.split()
        base = Path(parts[0]).name if parts else ""
        if base == "copilot":
            return True
        if "copilot-cli" in c:
            return True
        norm = c.replace("\\", "/")
        if "/@github/copilot/" in norm or "/github/copilot-cli/" in norm:
            return True
        return False

    agent_pids = [p for p in pids if looks_like_copilot(read_cmdline(p))]
    if not agent_pids:
        agent_pids = pids

    jsonl = None

    # 1) Open FDs — strongest live-session signal.
    for p in agent_pids + pids:
        for path in read_open_paths(p):
            norm = path.replace("\\", "/")
            if _looks_like_copilot_store(norm):
                cand = Path(path)
                if cand.is_file():
                    jsonl = cand
                    break
        if jsonl:
            break

    # 2) --resume=<uuid> / --session=<uuid> on the process command line.
    if jsonl is None:
        for p in agent_pids:
            sid = _session_id_from_cmdline(read_cmdline(p))
            if not sid:
                continue
            for cand in (
                root / sid / "events.jsonl",
                root / (sid + ".jsonl"),
            ):
                if cand.is_file():
                    jsonl = cand
                    break
            if jsonl:
                break
            env = read_proc_environ(p)
            for key in ("COPILOT_SESSION_ID", "COPILOT_SESSION"):
                val = (env.get(key) or "").strip()
                if val and re.fullmatch(r"[0-9a-fA-F-]{36}", val):
                    for cand in (
                        root / val / "events.jsonl",
                        root / (val + ".jsonl"),
                    ):
                        if cand.is_file():
                            jsonl = cand
                            break
                if jsonl:
                    break
            if jsonl:
                break

    # 3) Sessions whose workspace cwd matches the pane.
    if jsonl is None and cwd:
        files = _files_for_cwd(root, cwd)
        if len(files) == 1:
            jsonl = files[0]
        elif len(files) > 1:
            pane_text = capture_pane(target, True)
            for f in files:
                # Directory name (or legacy stem) is the resume UUID.
                token = f.parent.name if f.name == "events.jsonl" else f.stem
                if token and len(token) >= 8 and token in pane_text:
                    jsonl = f
                    break
            if jsonl is None:
                jsonl = pick_by_activity(files, target)

    # 4) Ambiguous global pool — only bind when unique or clearly identified.
    if jsonl is None:
        files = _list_session_files(root)
        if len(files) == 1:
            jsonl = files[0]
        elif len(files) > 1:
            pane_text = capture_pane(target, True)
            for f in files:
                token = f.parent.name if f.name == "events.jsonl" else f.stem
                if token and len(token) >= 8 and token in pane_text:
                    jsonl = f
                    break
            # Never pick newest-by-mtime alone when multiple candidates exist.

    return str(jsonl) if jsonl else None, None
