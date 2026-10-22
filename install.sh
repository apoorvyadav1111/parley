#!/bin/sh
# Install parley into Claude Code: the skill files plus its three hooks.
#
#   ./install.sh              copy the skill into ~/.claude/skills/parley (manual route;
#                             or install it as a plugin instead: see README, not both)
#   ./install.sh --link       symlink it instead, so edits in this repo apply live
#   ./install.sh --uninstall  remove the skill and its hooks (messages in ~/.claude/parley are kept)
#
# Safe to re-run: hook entries are replaced, never duplicated, and settings.json is
# backed up before every change.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/skills/parley"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
DEST="$CLAUDE_HOME/skills/parley"
SETTINGS="$CLAUDE_HOME/settings.json"
FILES="SKILL.md reference.md parley.py"

mode=copy
case "${1:-}" in
  "") ;;
  --link) mode=link ;;
  --uninstall) mode=uninstall ;;
  *) echo "usage: $0 [--link | --uninstall]" >&2; exit 2 ;;
esac

command -v python3 >/dev/null || { echo "parley needs python3" >&2; exit 1; }

# Add (or with "remove", drop) parley's hook entries in settings.json.
# Also drops entries from the old session-bus skill.
hooks() {
  python3 - "$SETTINGS" "$1" "$DEST/parley.py" <<'EOF'
import json, os, shutil, sys, time
path, action, script = sys.argv[1:]
try:
    with open(path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
if os.path.exists(path):
    shutil.copy2(path, f"{path}.bak-parley-{time.strftime('%Y%m%d%H%M%S')}")
hooks = cfg.setdefault("hooks", {})
ours = ("parley.py on-", "session-bus/bus.py hook")
for event in list(hooks):
    groups = []
    for g in hooks[event]:
        g["hooks"] = [h for h in g.get("hooks", []) if not any(o in h.get("command", "") for o in ours)]
        if g["hooks"]:
            groups.append(g)
    if groups:
        hooks[event] = groups
    else:
        del hooks[event]
if action == "add":
    for event, sub in (("SessionStart", "on-start"), ("UserPromptSubmit", "on-prompt"),
                       ("SessionEnd", "on-end")):
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": f"{script} {sub}", "timeout": 10}]})
if not hooks:
    del cfg["hooks"]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
EOF
}

if [ "$mode" = uninstall ]; then
  hooks remove
  if [ -L "$DEST" ]; then rm "$DEST"; elif [ -d "$DEST" ]; then
    for f in $FILES; do rm -f "$DEST/$f"; done
    rmdir "$DEST" 2>/dev/null || echo "left $DEST in place (it has other files)"
  fi
  echo "parley uninstalled. Messages are kept in $CLAUDE_HOME/parley."
  exit 0
fi

mkdir -p "$CLAUDE_HOME/skills"
if [ -L "$DEST" ]; then rm "$DEST"; fi
if [ "$mode" = link ]; then
  [ -d "$DEST" ] && { for f in $FILES; do rm -f "$DEST/$f"; done; rmdir "$DEST"; }
  ln -s "$SRC" "$DEST"
else
  mkdir -p "$DEST"
  for f in $FILES; do cp "$SRC/$f" "$DEST/$f"; done
fi
chmod +x "$SRC/parley.py" "$DEST/parley.py"
hooks add

echo "parley installed ($mode) at $DEST, hooks added to $SETTINGS."
echo "Sessions already running pick it up when restarted or resumed."
echo "Check from inside a session: $DEST/parley.py selfcheck"
