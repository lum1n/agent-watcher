# Changelog

## Unreleased

- Discover and classify GitHub Copilot CLI (`~/.copilot/session-state/*/events.jsonl`).
- Keep Claude busy on narrow panes where the footer cuts off `esc to interrupt`.

## 0.1.0

- Discover and classify Claude, Codex, OpenCode, Pi, and Cursor in tmux.
- NDJSON protocol: `hello`, `snapshot`, `state`, `gone`, `unbound`, `error`.
- `--listen` Unix socket (`0700`); socket `stop` disconnects one client.
