"""OpenCode discover, bind, and classify.

Loaded into agent_watcher.py (repo) or inlined by embed-agent-watcher.mjs.

All OpenCode agents share one opencode.db — discover must return a unique
ses_ id per pane. Classify requires that id. Reasoning / step-start parts
persist on the finished assistant message and must not keep thinking after
a text reply lands.
"""


def opencode_part_status(part):
    """OpenCode stores tool status in part.state.status (state is a dict)."""
    if not isinstance(part, dict):
        return ""
    s = part.get("status")
    if isinstance(s, str) and s:
        return s.lower()
    st = part.get("state")
    if isinstance(st, str) and st:
        return st.lower()
    if isinstance(st, dict):
        inner = st.get("status")
        if isinstance(inner, str) and inner:
            return inner.lower()
    return ""


def opencode_part_input(part):
    if not isinstance(part, dict):
        return {}
    st = part.get("state")
    if isinstance(st, dict) and isinstance(st.get("input"), dict):
        return st["input"]
    inp = part.get("input") or part.get("args") or {}
    return inp if isinstance(inp, dict) else {}


def opencode_part_text(part):
    if not isinstance(part, dict):
        return ""
    text = part.get("text") or part.get("content") or ""
    if isinstance(text, dict):
        text = text.get("text") or text.get("content") or ""
    return str(text).strip()


def opencode_message_finished(info):
    """True when the assistant message itself is marked complete."""
    if not isinstance(info, dict):
        return False
    t = info.get("time")
    if isinstance(t, dict) and t.get("completed"):
        return True
    for key in ("status", "finish", "finishReason", "stopReason"):
        s = info.get(key)
        if isinstance(s, str) and s.lower() in (
            "complete",
            "completed",
            "done",
            "finished",
            "stop",
            "end_turn",
        ):
            return True
    return False


OPENCODE_TOOL_DONE = frozenset(
    ("completed", "done", "error", "failed", "output-available", "output-error")
)
OPENCODE_TOOL_RUNNING = frozenset(("pending", "running", "in_progress", "in-progress"))


def last_user_summary_opencode(conn, session_id):
    """Newest real user prompt in this OpenCode session, if any."""
    try:
        rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ? "
            "ORDER BY time_created DESC LIMIT 40",
            (session_id,),
        ).fetchall()
    except Exception:
        return None
    for mid, data_s in rows:
        try:
            info = json.loads(data_s)
        except Exception:
            continue
        if info.get("role") != "user":
            continue
        texts = []
        try:
            prows = conn.execute(
                "SELECT data FROM part WHERE message_id = ? ORDER BY id LIMIT 16",
                (mid,),
            ).fetchall()
        except Exception:
            prows = []
        for (pdata,) in prows:
            try:
                part = json.loads(pdata)
            except Exception:
                continue
            t = opencode_part_text(part)
            if t:
                texts.append(t)
        blob = "\n".join(texts)
        if not blob:
            t = info.get("text")
            blob = t if isinstance(t, str) else ""
        summary = summarize_user_prompt(blob)
        if summary:
            return summary
    return None


def classify_opencode(db_path, session_id=None):
    import sqlite3

    if not db_path or not os.path.isfile(db_path):
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    # Never classify the shared DB globally — that makes every OpenCode agent
    # mirror whichever session wrote last.
    if not session_id:
        return {"state": "idle", "ageMs": 0, "pathMissing": True}
    age_ms = store_age_ms(db_path)
    state, tool_name, tool_target, summary = "idle", None, None, None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except Exception:
        return {"state": "idle", "ageMs": age_ms}
    try:
        rows = conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 8",
            (session_id,),
        ).fetchall()
        for mid, data_s, created in rows:
            try:
                info = json.loads(data_s)
            except Exception:
                continue
            # Prefer message timestamp over (possibly stale) db mtime.
            try:
                created_ts = float(created)
                if created_ts > 1e12:
                    created_ts /= 1000.0
                msg_age = int((time.time() - created_ts) * 1000)
                if msg_age >= 0:
                    age_ms = min(age_ms, msg_age) if age_ms else msg_age
            except Exception:
                pass
            role = info.get("role", "")
            if role == "assistant":
                parts = conn.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY id", (mid,)
                ).fetchall()
                pending_tool = False
                has_complete_text = False
                has_streaming_text = False
                has_reasoning = False
                has_step_start = False
                has_step_finish = False
                for (pdata,) in parts:
                    try:
                        part = json.loads(pdata)
                    except Exception:
                        continue
                    ptype = part.get("type", "")
                    status = opencode_part_status(part)
                    if ptype in ("reasoning", "thinking"):
                        has_reasoning = True
                    elif ptype in ("step-start",):
                        has_step_start = True
                    elif ptype in ("step-finish", "step-end", "finish"):
                        has_step_finish = True
                    elif ptype in ("text", "markdown"):
                        text = opencode_part_text(part)
                        if text and status not in OPENCODE_TOOL_RUNNING:
                            has_complete_text = True
                        elif text or status in OPENCODE_TOOL_RUNNING:
                            has_streaming_text = True
                    elif ptype in ("tool_use", "tool", "tool-call", "tool_call"):
                        if status in OPENCODE_TOOL_DONE:
                            continue
                        pending_tool = True
                        state = "running-tool"
                        tool_name = part.get("name") or part.get("tool") or ""
                        tool_target = tool_target_from(opencode_part_input(part))
                        break
                finished = opencode_message_finished(info)
                if pending_tool:
                    pass
                elif has_complete_text or has_step_finish or finished:
                    # Reasoning / step-start parts stay on the finished message.
                    state = "idle"
                elif has_reasoning or has_streaming_text or has_step_start:
                    state = "thinking"
                else:
                    state = "idle"
                break
            elif role in ("user", "tool"):
                state = "thinking"
                break
        summary = last_user_summary_opencode(conn, session_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    state = age_out_busy(state, age_ms)
    result = {
        "state": state,
        "toolName": tool_name,
        "toolTarget": tool_target,
        "ageMs": age_ms,
    }
    if summary:
        result["summary"] = summary
    return result

def cwd_directory_variants(cwd):
    """OpenCode stores directory verbatim; macOS/resolve can drift."""
    if not cwd:
        return []
    variants = [cwd, cwd.rstrip("/")]
    try:
        variants.append(str(Path(cwd).resolve()))
    except Exception:
        pass
    # /var ↔ /private/var (macOS)
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


def extract_opencode_sid(data):
    if data is None:
        return None
    if isinstance(data, str) and data.startswith("ses_"):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("id", "sessionID", "session_id", "sessionId"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("ses_"):
            return val
    nested = data.get("session") or data.get("data")
    if nested is not None and nested is not data:
        return extract_opencode_sid(nested)
    return None


def discover_opencode(session, window, exclude_sids=None):
    """Bind this pane to a unique OpenCode session id.

    All OpenCode agents share one opencode.db. Without a per-pane ses_ id,
    classify would read the global latest message and every idle agent would
    mirror the busy one.
    """
    exclude = set(exclude_sids or [])
    home = Path.home()
    db_paths = []
    env_db = (os.environ.get("OPENCODE_DB") or "").strip()
    if env_db:
        db_paths.append(Path(env_db))
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    data_home = Path(xdg) if xdg else (home / ".local" / "share")
    db_paths.extend(
        [
            data_home / "opencode" / "opencode.db",
            home / ".local/share/opencode/opencode.db",
            home / "Library/Application Support/opencode/opencode.db",
        ]
    )
    for root in (data_home / "opencode", home / ".local/share/opencode"):
        if root.is_dir():
            db_paths.extend(sorted(root.glob("opencode*.db")))
    seen_db, unique_dbs = set(), []
    for p in db_paths:
        s = str(p)
        if s not in seen_db:
            seen_db.add(s)
            unique_dbs.append(p)
    db_path = next((p for p in unique_dbs if p.is_file()), None)
    target = target_for(session, window)
    cwd = tmux_fmt(target, "#{pane_current_path}")
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []
    agent_pids = [p for p in pids if "opencode" in read_cmdline(p).lower()]
    if not agent_pids:
        agent_pids = pids

    sid = None

    def accept(cand):
        if not cand or cand in exclude:
            return None
        return str(cand)

    def listen_ports(plist):
        ports = []
        for p in plist:
            try:
                out = subprocess.check_output(
                    [
                        "lsof",
                        "-nP",
                        "-a",
                        "-p",
                        str(p),
                        "-iTCP",
                        "-sTCP:LISTEN",
                        "-Fn",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                for line in out.splitlines():
                    if line.startswith("n"):
                        m = re.search(r":(\d+)$", line[1:])
                        if m:
                            ports.append(int(m.group(1)))
            except Exception:
                pass
        return list(dict.fromkeys(ports))

    def http_json(url, timeout=1.5):
        try:
            req = __import__("urllib.request").request.Request(
                url, headers={"Accept": "application/json"}
            )
            with __import__("urllib.request").request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            return None

    for port in listen_ports(agent_pids):
        for path in (
            "/tui/active-session",
            "/api/session/active",
            "/session/active",
        ):
            hit = accept(extract_opencode_sid(http_json("http://127.0.0.1:%d%s" % (port, path))))
            if hit:
                sid = hit
                break
        if sid:
            break

    if not sid:
        for p in agent_pids:
            cmd = read_cmdline(p)
            m = re.search(r"(?:--session|-s)\s+(ses_[A-Za-z0-9_-]+)", cmd)
            if m:
                hit = accept(m.group(1))
                if hit:
                    sid = hit
                    break

    if not sid:
        for p in agent_pids + pids:
            env = read_proc_environ(p)
            for key in ("OPENCODE_SESSION_ID", "OPENCODE_SESSION"):
                hit = accept((env.get(key) or "").strip())
                if hit and hit.startswith("ses_"):
                    sid = hit
                    break
            if sid:
                break

    pane_text = None

    def pane():
        nonlocal pane_text
        if pane_text is None:
            pane_text = capture_pane(target, True)
        return pane_text

    def sessions_for_cwd():
        if not db_path or not cwd:
            return []
        import sqlite3

        dirs = cwd_directory_variants(cwd)
        if not dirs:
            return []
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            placeholders = ",".join("?" for _ in dirs)
            rows = con.execute(
                "SELECT id, slug, title, time_updated, directory FROM session "
                "WHERE directory IN (%s) ORDER BY time_updated DESC LIMIT 80"
                % placeholders,
                tuple(dirs),
            ).fetchall()
            con.close()
            return rows
        except Exception:
            return []

    def known_session_ids():
        if not db_path:
            return set()
        import sqlite3

        try:
            con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            rows = con.execute("SELECT id FROM session").fetchall()
            con.close()
            return {r[0] for r in rows if r and r[0]}
        except Exception:
            return set()

    # Pane often shows ses_… in the status/header — strongest local signal.
    if not sid:
        text = pane()
        found = re.findall(r"ses_[A-Za-z0-9_-]{6,}", text or "")
        if found:
            known = known_session_ids()
            ordered = []
            for cand in found:
                if cand in exclude:
                    continue
                if cand not in ordered:
                    ordered.append(cand)
            in_db = [c for c in ordered if not known or c in known]
            pool = in_db or ordered
            if len(pool) == 1:
                sid = pool[0]
            elif len(pool) > 1:
                # Prefer a ses_ that also belongs to this cwd.
                cwd_ids = {r[0] for r in sessions_for_cwd()}
                cwd_hits = [c for c in pool if c in cwd_ids]
                if len(cwd_hits) == 1:
                    sid = cwd_hits[0]

    if not sid:
        text = pane()
        for sid_c, slug, title, _tu, _directory in sessions_for_cwd():
            if sid_c in exclude:
                continue
            for needle in (slug, title, sid_c):
                if needle and str(needle) in text:
                    sid = sid_c
                    break
            if sid:
                break

    # Score session message text against pane scrollback — same signal chat
    # history uses. Critical when HTTP active-session is unavailable.
    if not sid and db_path:
        text = pane()
        rows = [
            (sid_c, slug, title)
            for sid_c, slug, title, _tu, _directory in sessions_for_cwd()
            if sid_c not in exclude
        ]
        if text and rows:
            import sqlite3

            try:
                con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
                scored = []
                for sid_c, _slug, _title in rows:
                    score = 0
                    try:
                        mids = con.execute(
                            "SELECT id, data FROM message WHERE session_id = ? "
                            "ORDER BY time_created DESC LIMIT 30",
                            (sid_c,),
                        ).fetchall()
                    except Exception:
                        mids = []
                    for mid, data_s in mids:
                        try:
                            info = json.loads(data_s)
                        except Exception:
                            info = {}
                        texts = []
                        try:
                            prows = con.execute(
                                "SELECT data FROM part WHERE message_id = ? ORDER BY id LIMIT 12",
                                (mid,),
                            ).fetchall()
                        except Exception:
                            prows = []
                        for (pdata,) in prows:
                            try:
                                part = json.loads(pdata)
                            except Exception:
                                continue
                            if part.get("type") == "text":
                                t = part.get("text")
                                if isinstance(t, str) and t.strip():
                                    texts.append(t.strip())
                        t = info.get("text")
                        if isinstance(t, str) and t.strip():
                            texts.append(t.strip())
                        for t in texts:
                            norm = re.sub(r"\s+", " ", t).strip()
                            if len(norm) < 24:
                                continue
                            if norm[:80] in text or norm[:40] in text:
                                score += 2 if info.get("role") == "user" else 1
                        if score >= 8:
                            break
                    scored.append((score, sid_c))
                con.close()
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored:
                    best_score, best = scored[0]
                    second = scored[1][0] if len(scored) > 1 else -1
                    if best_score >= 2 and best_score - second >= 1:
                        sid = best
            except Exception:
                pass

    if not sid:
        rows = [
            (sid_c, tu)
            for sid_c, _slug, _title, tu, _directory in sessions_for_cwd()
            if sid_c not in exclude
        ]
        activity = pane_last_activity(target)
        if activity > 0 and rows:
            scored = []
            for sid_c, tu in rows:
                try:
                    ts = float(tu)
                    if ts > 1e12:
                        ts = ts / 1000.0
                except Exception:
                    continue
                scored.append((abs(ts - activity), sid_c))
            scored.sort(key=lambda x: x[0])
            if scored:
                best_dt, best = scored[0]
                second_dt = scored[1][0] if len(scored) > 1 else 1e99
                close = [s for dt, s in scored if dt <= 900]
                if len(close) == 1:
                    sid = close[0]
                elif best_dt <= 900 and (second_dt - best_dt) >= 30:
                    sid = best

    if not sid:
        rows = [r[0] for r in sessions_for_cwd() if r[0] not in exclude]
        # Unique among remaining — never "newest of many".
        if len(rows) == 1:
            sid = rows[0]

    # Shared DB is useless without a per-pane session id.
    if not sid:
        return None, None
    return (str(db_path) if db_path else None), sid

def find_opencode_db():
    home = Path.home()
    candidates = []
    env_db = (os.environ.get("OPENCODE_DB") or "").strip()
    if env_db:
        candidates.append(Path(env_db))
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    data_home = Path(xdg) if xdg else (home / ".local" / "share")
    candidates.extend(
        [
            data_home / "opencode" / "opencode.db",
            home / ".local/share/opencode/opencode.db",
            home / "Library/Application Support/opencode/opencode.db",
        ]
    )
    for root in (data_home / "opencode", home / ".local/share/opencode"):
        if root.is_dir():
            candidates.extend(sorted(root.glob("opencode*.db")))
    for p in candidates:
        if p.is_file():
            return str(p)
    return None

def probe_opencode_active_sid(session, window, exclude_sids=None):
    """Cheap HTTP-only active session probe for sticky-bind refresh."""
    exclude = set(exclude_sids or [])
    target = target_for(session, window)
    try:
        pid = int(tmux_fmt(target, "#{pane_pid}") or "0")
    except Exception:
        pid = 0
    pids = list_descendant_pids(pid) if pid else []
    agent_pids = [p for p in pids if "opencode" in read_cmdline(p).lower()]
    if not agent_pids:
        agent_pids = pids

    def listen_ports(plist):
        ports = []
        for p in plist:
            try:
                out = subprocess.check_output(
                    [
                        "lsof",
                        "-nP",
                        "-a",
                        "-p",
                        str(p),
                        "-iTCP",
                        "-sTCP:LISTEN",
                        "-Fn",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                for line in out.splitlines():
                    if line.startswith("n"):
                        m = re.search(r":(\d+)$", line[1:])
                        if m:
                            ports.append(int(m.group(1)))
            except Exception:
                pass
        return list(dict.fromkeys(ports))

    def http_json(url, timeout=1.0):
        try:
            req = __import__("urllib.request").request.Request(
                url, headers={"Accept": "application/json"}
            )
            with __import__("urllib.request").request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            return None

    for port in listen_ports(agent_pids):
        for path in (
            "/tui/active-session",
            "/api/session/active",
            "/session/active",
        ):
            hit = extract_opencode_sid(http_json("http://127.0.0.1:%d%s" % (port, path)))
            if hit and hit not in exclude:
                return hit
    return None


HARNESS_REQUIRES_SID.add("opencode")
HARNESS_FIND_STORE["opencode"] = find_opencode_db
HARNESS_PROBE_ACTIVE["opencode"] = probe_opencode_active_sid
