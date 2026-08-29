# agent-watcher

Long-lived classifier for AI coding agents in tmux. Used by [sessh](../sessh) (embedded over SSH) and [tmux-agent-state](../tmux-agent-state) (local statusline / session switcher).

Nothing is installed on a remote host — sessh still ships one inlined file. This repo is the source of truth for discover + classify.

## States

`idle` · `thinking` · `running-tool` · `waiting-permission` · `errored`

Harnesses: Claude, Codex, OpenCode, Pi, Cursor.

## Run

```bash
python3 src/agent_watcher.py          # NDJSON on stdout
python3 src/agent_watcher.py --listen /path/to.sock
python3 src/agent_watcher.py --classify
python3 src/agent_watcher.py --attention < pane.txt
```

`--listen` binds a `0700` Unix socket and copies every event to subscribers. Socket `{"cmd":"stop"}` disconnects that client only. Stdin `stop` still exits the process.

Stdin / socket control: `{"cmd":"snapshot"}` / `ping` / `bind` / `stop`.

Events: `hello`, `snapshot`, `state`, `gone`, `unbound`, `error`.

## Layout

```
src/agent_watcher.py
src/harnesses/{claude,codex,cursor,opencode,pi}.py
```

Sessh embeds via `npm run embed:watcher` (reads this tree). After editing, regenerate the embed and run `npm run test:watcher` in sessh.
