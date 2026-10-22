# parley reference

Read this only when `SKILL.md` isn't enough: debugging, edge cases, or questions about how it works.

## Data (`~/.claude/parley/`, override with `PARLEY_DIR`)

| Path | Contents |
|---|---|
| `log.jsonl` | One JSON message per line. Append-only: never edit by hand. |
| `sessions.json` | Registry: session id → `name` (handle), `about` (card), `cwd`, `pid`, `host_id` (ring address), `status`, `started`, `seen`, `ended`. |
| `cursors/<session id>` | Byte offset into `log.jsonl` this session has read up to. |
| `archive/log-*.jsonl.gz` | Prefixes removed by `sweep`. Read with `gzcat`. |
| `.lock` | Taken for every append, registry/cursor write and sweep. |

Message fields: `v` (schema, 1), `id` (`m-` + 8 hex), `ts`, `from`, `from_name`, `to` (session id or `*`), `to_name`, `type` (`update` or `ack`), `body`, plus `severity`, `origin`, `ack_requested`, and `wake` (rung) on updates, or `ack_of` on confirmations.

## Identity and inboxes

- **Inbox = session id.** `dispatch` resolves each handle to a session id at send time and addresses the message there. A new session that later takes the same handle has a different id, so it never sees those messages.
- **New sessions** start reading at the current end of the log, so old broadcasts don't flood them.
- **Resuming** the same chat keeps its id, so it gets everything sent to it while offline. If another online session took its handle meanwhile, it gets a suffixed one (`backend-1a2b`).
- **Online** means started and not ended, with a live process (`CLAUDE_PID`). Crashes and app quits that skip `SessionEnd` are caught by the process check. Offline entries are forgotten after 30 days.
- **Rotated session ids:** the desktop app sometimes ends a chat's Claude Code session and starts a new one with a new id. The app's id for the chat (`host_id`) stays the same, so parley merges the entries: the new id keeps the handle, card and the stricter of the two seals, and the old id becomes an alias. Messages sent to the old id after it last read are replayed exactly once (`replay_until`). Different chats never share a `host_id`, so they never inherit. Terminal sessions have no `host_id` and aren't merged. Each entry records its `start_source` (startup, resume, clear, compact) and `end_reason`.
- **Resolving a handle:** online matches win; if only offline matches exist, the most recently seen one is used (with a warning). Several online matches are refused: use the session id prefix the error shows.

## Seals

- `seal --mode` stores the mode on this session's registry entry. Resuming keeps it, and sealed sessions are never forgotten, so a resumed private chat stays private.
- `--folder` writes the mode to `config.json` under this folder's real path. New sessions started there, or in any subfolder, start with it. The deepest configured folder wins. `seal --mode open --folder` removes the folder rule.
- A session that can't receive has its cursor moved to the end of the log on every delivery attempt, so messages are dropped, not queued. Reopening it starts fresh.
- A session that can't send gets `dispatch` and `confirm` refused.

## Delivery

- Hooks: `on-start` (SessionStart: registers, lists project peers, delivers), `on-prompt` (UserPromptSubmit: delivers before Claude answers), `on-end` (SessionEnd: marks offline). Turns started by a ring also run `on-prompt`.
- An injection holds at most 8 messages / ~3,000 characters. The cursor only advances past what was shown; the rest waits for `collect` or the next prompt. `collect` returns up to 20 at a time.
- `dispatch` and `confirm` first deliver anything unread for the sender ("arrived first").

## Rules the script enforces

- `--origin` is required. A derived message can't be critical, can't be broadcast, and needs `--why` (shown to the receiver as "why this is for you"). Critical implies a confirmation request.
- A body over 1,500 characters is refused: summarize and point to a file. A card over 300 characters is refused.
- Every recipient is resolved before anything is written, so one bad handle sends nothing.
- `confirm` only works on messages addressed to this session (or broadcasts).
- `--ring` is refused for `*`, for offline sessions or ones with no `host_id`, and within 10 minutes of being rung by that same session.

## Sweep

It cuts only a prefix of the log where every message is older than the retention period (30 days by default, `--days N`) and already read by every online session. The prefix goes to `archive/`, and every cursor is shifted down by the cut size. It runs automatically after a dispatch once the log passes 1 MB. `sweep --dry-run` previews.

## Troubleshooting

- **Nothing arrives / hooks seem dead:** run `selfcheck`. It checks the three hooks in `~/.claude/settings.json`, ledger readability, and this session's registration, card and ring address. Sessions opened before the hooks were installed need a restart or resume.
- **`no session id`:** you're outside a Claude Code session; pass `--session <id>`.
- **A peer is missing from `roster`:** it hasn't been active since parley was installed, or it's offline (`roster --all`).
- **Source and tests** live in the cloned repo, not in the installed copy. Run `python3 -m unittest discover -s tests -v` there (temp dir, never touches real data), then `./install.sh` to update the installed skill.
