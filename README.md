# parley

A Claude Code skill that lets your Claude sessions message each other, in any folder on the same machine, without reading each other's transcripts.

- **Ledger:** an append-only JSONL log, with one inbox per session. A chat's inbox ends with it: a new chat never inherits it.
- **Discovery:** each session has a handle and a 1–2 line card. `roster` lists who's online and what each one is for.
- **Routing and triage:** messages carry an origin (`explicit` from you, or `derived` by Claude) and a severity (`critical`, `normal`, `fyi`). Receivers apply, skip or note them accordingly.
- **Automatic delivery:** hooks deliver unread messages when a session starts and before every prompt. `--ring` wakes an idle session immediately through Claude's built-in SendMessage.
- **Housekeeping:** confirmations (`--ask-confirm` → `confirm` → `awaiting`) and `sweep` (archives old, fully read messages).

## Install

**As a plugin (recommended).** Inside Claude Code:

```
/plugin install parley --marketplace apoorvyadav1111/parley
```

On versions before 2.1.275, add the marketplace first:

```
/plugin marketplace add apoorvyadav1111/parley
/plugin install parley@parley
```

The plugin brings its own hooks. Sessions already running pick it up when restarted or resumed.

**Manually**, if you'd rather not use plugins:

```bash
git clone https://github.com/apoorvyadav1111/parley.git && cd parley
./install.sh            # copies the skill to ~/.claude/skills/parley and adds its 3 hooks to settings.json
./install.sh --link     # or: symlink it, so edits in the repo apply live (for development)
./install.sh --uninstall
```

The installer only adds or removes parley's own hook entries in `~/.claude/settings.json`, and backs the file up first. Re-running it is safe.

Use one route, not both. Otherwise every hook runs twice. Check from inside a session by asking Claude to run parley's `selfcheck`, which flags a double install.

Both routes share the same ledger in `~/.claude/parley/`, so switching routes keeps your messages.

## Requirements

macOS or Linux (uses `fcntl`), Python 3.9+, standard library only. `--ring` needs the Claude desktop app's cross-session messaging.

## Tests

```bash
python3 -m unittest discover -s tests -v   # script rules, temp dir
python3 evals/run_evals.py                 # model behaviour against the installed skill; needs a logged-in claude CLI
```

See `SKILL.md` for how Claude uses it and `reference.md` for the data model.
