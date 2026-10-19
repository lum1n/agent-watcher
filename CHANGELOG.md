# Changelog

## Unreleased

- Discover and classify GitHub Copilot CLI (`~/.copilot/session-state/*/events.jsonl`).
- Keep Claude busy on narrow panes where the footer cuts off `esc to interrupt`.
- `quota` event: subscription usage for Claude, Codex, and Cursor, emitted on change.
  `--no-quota` turns the probes off (no credential reads, no vendor API requests).
- New `--listen` subscribers get the last `quota` reading per kind right after `hello`.
- Socket `{"cmd":"snapshot"}` is answered at once from current state (`"cached": true`);
  the full rescan snapshot still follows.

## 0.1.0

- Discover and classify Claude, Codex, OpenCode, Pi, and Cursor in tmux.
- NDJSON protocol: `hello`, `snapshot`, `state`, `gone`, `unbound`, `error`.
- `--listen` Unix socket (`0700`); socket `stop` disconnects one client.
