# agent-watcher

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Long-lived classifier for AI coding agents in tmux. The public consumer is [tmux-agent-state](https://github.com/lum1n/tmux-agent-state) (TPM plugin; pins this repo as a submodule). Other tools can subscribe to the same NDJSON stream on stdout or a Unix socket.

This repo is the source of truth for discover + classify.

## States

`idle` · `thinking` · `running-tool` · `waiting-permission` · `errored`

Harnesses: Claude, Codex, OpenCode, Pi, Cursor, Copilot.

## Run

```bash
python3 src/agent_watcher.py          # NDJSON on stdout
python3 src/agent_watcher.py --listen /path/to.sock
python3 src/agent_watcher.py --classify
python3 src/agent_watcher.py --attention < pane.txt
```

`--listen` binds a `0700` Unix socket and copies every event to subscribers. Socket `{"cmd":"stop"}` disconnects that client only. Stdin `stop` still exits the process.

Stdin / socket control: `{"cmd":"snapshot"}` / `ping` / `bind` / `stop`.

Events: `hello`, `snapshot`, `state`, `gone`, `unbound`, `error`, `quota`.

`quota` is emitted on change. `--no-quota` disables the usage probes entirely:
no credential reads and no vendor API requests. A new `--listen` socket subscriber also gets the
last reading per kind right after `hello`; stdout output is unchanged.

A `{"cmd":"snapshot"}` sent on the socket is answered immediately with the
current state (last snapshot plus later events, marked `"cached": true`) to that
client; the usual full rescan still runs and its snapshot follows to everyone.

## Layout

```
src/agent_watcher.py
src/harnesses/{claude,codex,copilot,cursor,opencode,pi}.py
```

Clients can run this tree as-is, embed those files, or attach to `--listen`.

## License

MIT. See [LICENSE](LICENSE).
