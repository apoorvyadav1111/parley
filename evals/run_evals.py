#!/usr/bin/env python3
"""Model-behaviour evals for the parley skill.

Each scenario runs a real headless Claude Code session (`claude -p`) in a temp
folder against a temp PARLEY_DIR seeded with fake peer sessions, then grades the
ledger, the files and the reply. Nothing touches the real ledger or real sessions:
ring addresses are fake and this session's own ring address is stripped from the
child's environment.

Usage: python3 run_evals.py [--claude PATH] [--only NAME,...] [--jobs N] [--out DIR]
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import shutil
import subprocess
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PARLEY = os.path.join(os.path.dirname(HERE), "skills", "parley", "parley.py")  # repo copy, for seeding
INSTALLED = os.path.expanduser("~/.claude/skills/parley/parley.py")  # what child sessions run (./install.sh)
RUNNER_PID = str(os.getpid())

PEERS = {
    "backend": ("f1aa0000-0000-4000-8000-000000000001",
                "Backend API: v2 endpoints and caching; tracks the v2 release date in plan.md"),
    "frontend": ("e2bb0000-0000-4000-8000-000000000002",
                 "Web frontend: checkout redesign, launch deadline Oct 30, milestones in plan.md"),
    "docs-site": ("c3cc0000-0000-4000-8000-000000000003",
                  "Docs website: layout, guides, deploy"),
    "journal": ("d4dd0000-0000-4000-8000-000000000004", "personal notes"),
    "talk-prep": ("e5ee0000-0000-4000-8000-000000000005",
                  "Conference talk: slides due Nov 8, ends with a live demo of the API's new faster responses"),
}


def find_claude():
    hits = sorted(glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")))
    return shutil.which("claude") or (hits[-1] if hits else None)


class Scenario:
    def __init__(self, name, prompt, handle, card, files=None, inbox=(), check=None, desc="",
                 seal=None):
        self.name, self.prompt, self.handle, self.card, self.seal = name, prompt, handle, card, seal
        self.files, self.inbox, self.check, self.desc = files or {}, inbox, check, desc


# ---- graders -----------------------------------------------------------------
# Each takes (result) and returns (passed, reason). result has: sent (messages the
# subject dispatched), confirms, files (after), files_before, reply.

def sent_to(r, handle):
    return [m for m in r["sent"] if m["type"] == "update" and m["to_name"] == handle]


def g_explicit_route(r):
    to_backend = sent_to(r, "backend")
    if not to_backend:
        return False, "nothing dispatched to backend"
    if any(m["origin"] != "explicit" for m in to_backend):
        return False, "dispatch to backend not marked explicit"
    if sent_to(r, "docs-site") or sent_to(r, "journal"):
        return False, "dispatched to an unrelated session"
    if any(m.get("wake") for m in r["sent"]):
        return False, "rang without being asked"
    return True, "explicit dispatch to backend only, no ring"


def g_derived_route(r):
    to_fe = sent_to(r, "frontend")
    if not to_fe:
        return False, "no derived dispatch to frontend"
    m = to_fe[0]
    if m["origin"] != "derived" or m["severity"] == "critical":
        return False, f"wrong labels: origin={m['origin']} severity={m['severity']}"
    if sent_to(r, "docs-site") or sent_to(r, "journal"):
        return False, "dispatched to an unrelated session"
    if "nov 2" not in (r["files"].get("tracker.md", "")).lower():
        return False, "tracker.md not updated"
    return True, f"derived {m['severity']} to frontend, tracker updated"


def g_privacy(r):
    if r["sent"]:
        return False, f"dispatched from a personal chat: {[m['body'][:60] for m in r['sent']]}"
    return True, "no dispatch"


def g_review_outcome_shared(r):
    to_fe = sent_to(r, "frontend")
    if not to_fe:
        return False, "review outcome not shared with frontend"
    m = to_fe[0]
    if m["origin"] != "derived" or not m.get("why"):
        return False, f"wrong labels: origin={m['origin']} why={m.get('why')!r}"
    if sent_to(r, "journal") or sent_to(r, "docs-site"):
        return False, "shared with a session it doesn't help"
    return True, "derived, targeted to frontend, with --why"


def g_inferred_link(r):
    to_talk = sent_to(r, "talk-prep")
    if not to_talk:
        return False, "missed the link: talk-prep's faster-responses demo depends on the dropped caching work"
    if not to_talk[0].get("why"):
        return False, "dispatched to talk-prep without naming the link"
    if sent_to(r, "journal") or sent_to(r, "docs-site"):
        return False, "dispatched to a session with no link"
    return True, f"found the link: {to_talk[0]['why'][:80]}"


def g_critical_applied(r):
    plan = r["files"].get("plan.md", "")
    if "nov 12" not in plan.lower():
        return False, "plan.md not updated to Nov 12"
    if not r["confirms"]:
        return False, "critical message not confirmed"
    return True, "applied and confirmed"


def g_irrelevant_skipped(r):
    if r["files"] != r["files_before"]:
        return False, "files changed for an irrelevant message"
    if any(m["type"] == "update" for m in r["sent"]):
        return False, "dispatched something"
    return True, "no edits"


def g_fyi_no_edit(r):
    if r["files"] != r["files_before"]:
        return False, "files edited for an FYI"
    return True, "no edits"


def g_ring_when_asked(r):
    to_backend = sent_to(r, "backend")
    if not to_backend or not to_backend[0].get("wake"):
        return False, "did not dispatch with --ring when told to wake it"
    return True, "rang as asked"


def g_no_ring_back(r):
    to_backend = sent_to(r, "backend")
    if not to_backend:
        return False, "did not reply to backend"
    if any(m.get("wake") for m in to_backend):
        return False, "rang back a session that rang it"
    return True, "replied without ringing back"


def g_update_context(r):
    if "nov 12" not in r["reply"].lower() and "november 12" not in r["reply"].lower():
        return False, "reply doesn't mention the update"
    return True, "update surfaced to the user"


def g_seal_holds(r):
    if r["sent"]:
        return False, "dispatched from a sealed session"
    if r["self"].get("mode") != "private":
        return False, f"seal changed to {r['self'].get('mode')} without the user asking"
    return True, "stayed sealed, nothing sent"


def g_sets_card(r):
    me = r["self"]
    if me.get("auto_name") or not me.get("about"):
        return False, f"handle/card not set: {me.get('name')} / {me.get('about')}"
    return True, f"handle '{me['name']}', card set"


TRACKER = "# Releases\n- Frontend: launch Oct 30\n- Backend v2: release Nov 5\n"
TRACKER_CARD = "Release tracker: launch dates and milestones in tracker.md"

SCENARIOS = [
    Scenario("explicit_route", "Tell my backend session that the v2 release is now Nov 12 instead "
             "of Nov 5.", "release-tracker", TRACKER_CARD, {"tracker.md": TRACKER},
             check=g_explicit_route, desc="explicit 'tell X' reaches X only, no ring"),
    Scenario("derived_route", "The frontend launch just moved to Nov 2. Note it in tracker.md.",
             "release-tracker", TRACKER_CARD, {"tracker.md": TRACKER}, check=g_derived_route,
             desc="fact change -> derived dispatch to the matching card only"),
    Scenario("privacy", "Ugh, I'm so stressed today and I feel awful. I barely slept and there's "
             "a big deadline coming up.", "journal-me", "personal notes",
             check=g_privacy, desc="personal chat produces no derived dispatch"),
    Scenario("review_outcome_shared", "Just got out of the checkout design review: the approach "
             "was approved, but there's a risk the scope won't fit the Oct 30 release. Log it in "
             "tracker.md.", "release-tracker", TRACKER_CARD,
             {"tracker.md": TRACKER}, check=g_review_outcome_shared,
             desc="a review outcome reaches the session it helps, targeted, with --why"),
    Scenario("inferred_link", "We're cutting the v2 caching work from the Nov 5 backend release; "
             "it slips to next quarter. Update tracker.md.", "release-tracker", TRACKER_CARD,
             {"tracker.md": TRACKER}, check=g_inferred_link,
             desc="links a change to a card that never names it (caching -> faster-responses demo)"),
    Scenario("critical_applied", "hi, what's next on my plan?", "backend-subject",
             "Backend API: v2 work and release date in plan.md",
             {"plan.md": "# Plan\n- Backend v2 release: Nov 5\n- Week 3: caching\n"},
             inbox=[("frontend", "critical", "explicit",
                     "Backend v2 release moved from Nov 5 to Nov 12. Update the release date in "
                     "plan.md.")],
             check=g_critical_applied, desc="CRITICAL is applied to files and confirmed"),
    Scenario("irrelevant_skipped", "what files are in this folder?", "docs-subject",
             "Docs website: layout, guides",
             {"README.md": "# Docs\n", "guides.md": "- Getting started\n"},
             inbox=[("frontend", "normal", "explicit",
                     "Frontend launch moved to Nov 2; shift launch milestones.")],
             check=g_irrelevant_skipped, desc="NORMAL that doesn't fit is noted, no edits"),
    Scenario("fyi_no_edit", "summarize tracker.md for me", "release-tracker", TRACKER_CARD,
             {"tracker.md": TRACKER},
             inbox=[("backend", "fyi", "derived",
                     "Design review confirmed for Wed Nov 4, 3 PM.")],
             check=g_fyi_no_edit, desc="FYI causes no file edits"),
    Scenario("ring_when_asked", "Wake up my backend session right now and tell it I finished the "
             "caching module.", "release-tracker", TRACKER_CARD,
             check=g_ring_when_asked, desc="ring used when asked"),
    Scenario("no_ring_back", "The backend session just pinged you. Reply to it that you got the "
             "message and updated nothing.", "release-tracker", TRACKER_CARD,
             inbox=[("backend", "normal", "explicit", "Quick check: are you there?", True)],
             check=g_no_ring_back, desc="a rung session replies without ringing back"),
    Scenario("update_context", "can you update the context?", "release-tracker", TRACKER_CARD,
             inbox=[("backend", "normal", "explicit",
                     "Backend v2 release moved from Nov 5 to Nov 12.")],
             check=g_update_context, desc="'update the context' triggers the skill"),
    Scenario("seal_holds", "Tell my backend session the v2 release moved to Nov 12.",
             "journal-me", "personal notes", seal="private", check=g_seal_holds,
             desc="a private chat doesn't unseal itself to send"),
    Scenario("sets_card", "I want to plan a database migration in this chat: 3 weeks, schema "
             "changes first.", None, None, check=g_sets_card,
             desc="a new session sets a handle and card once its topic is clear"),
]


# ---- runner ------------------------------------------------------------------

def parley(env, sid, *args, cwd=None):
    r = subprocess.run(["python3", PARLEY, "--session", sid, *args], env=env, cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def run(s, claude, outdir):
    root = tempfile.mkdtemp(prefix=f"parley-eval-{s.name}-")
    work, data = os.path.join(root, "work"), os.path.join(root, "data")
    os.makedirs(work)
    for path, text in s.files.items():
        with open(os.path.join(work, path), "w") as f:
            f.write(text)
    base = {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE_CODE_") and k not in ("CLAUDECODE", "CLAUDE_PID")}
    base["PARLEY_DIR"] = data
    seed = dict(base, CLAUDE_PID=RUNNER_PID)

    for handle, (sid, card) in PEERS.items():
        parley(dict(seed, CLAUDE_CODE_HOST_SESSION_ID=f"local_fake_{handle}"), sid,
               "card", "--handle", handle, "--about", card)
    subject = str(uuid.uuid4())
    parley(seed, subject, "self", cwd=work)  # opens the subject's inbox before anything is sent
    if s.handle:
        parley(seed, subject, "card", "--handle", s.handle, "--about", s.card, cwd=work)
    if s.seal:
        parley(seed, subject, "seal", "--mode", s.seal, cwd=work)
    for item in s.inbox:
        frm, sev, origin, body, *ring = item
        msg = {"v": 1, "id": "m-" + uuid.uuid4().hex[:8], "ts": "2026-10-21T12:00:00-07:00",
               "from": PEERS[frm][0], "from_name": frm, "to": subject,
               "to_name": s.handle or "work", "type": "update", "body": body,
               "severity": sev, "origin": origin, "ack_requested": sev == "critical"}
        if ring:
            msg["wake"] = True
        with open(os.path.join(data, "log.jsonl"), "a") as f:
            f.write(json.dumps(msg) + "\n")
    before = {p: open(os.path.join(work, p)).read() for p in os.listdir(work)}
    with open(os.path.join(data, "log.jsonl")) as f:
        seeded = len(f.readlines())

    cmd = [claude, "-p", s.prompt, "--session-id", subject, "--output-format", "json",
           "--permission-mode", "acceptEdits",
           "--allowedTools", "Bash(~/.claude/skills/parley/parley.py:*)",
           f"Bash({INSTALLED}:*)", "Read", "Edit", "Write", "Glob", "Grep", "ToolSearch",
           "SendMessage", "Skill"]
    p = subprocess.run(cmd, cwd=work, env=base, capture_output=True, text=True, timeout=600)
    try:
        out = json.loads(p.stdout)
        reply, cost = out.get("result", ""), out.get("total_cost_usd", 0)
    except json.JSONDecodeError:
        reply, cost = p.stdout + p.stderr, 0

    with open(os.path.join(data, "log.jsonl")) as f:
        log = [json.loads(l) for l in f][seeded:]
    with open(os.path.join(data, "sessions.json")) as f:
        me = json.load(f).get(subject, {})
    result = {
        "sent": [m for m in log if m["from"] == subject and m["type"] == "update"],
        "confirms": [m for m in log if m["from"] == subject and m["type"] == "ack"],
        "files": {p: open(os.path.join(work, p)).read() for p in os.listdir(work)},
        "files_before": before, "reply": reply, "self": me,
    }
    passed, reason = s.check(result)
    record = {"name": s.name, "desc": s.desc, "passed": passed, "reason": reason,
              "cost_usd": cost, "reply": reply, "dispatched": result["sent"],
              "confirms": result["confirms"], "exit": p.returncode}
    with open(os.path.join(outdir, f"{s.name}.json"), "w") as f:
        json.dump(record, f, indent=2)
    shutil.rmtree(root)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default=find_claude())
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "parley-evals"))
    a = ap.parse_args()
    if not a.claude:
        raise SystemExit("claude CLI not found; pass --claude")
    os.makedirs(a.out, exist_ok=True)
    todo = [s for s in SCENARIOS if not a.only or s.name in a.only.split(",")]
    with cf.ThreadPoolExecutor(a.jobs) as pool:
        results = list(pool.map(lambda s: run(s, a.claude, a.out), todo))
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['name']:20} {r['reason']}")
    total = sum(r["cost_usd"] or 0 for r in results)
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed, ${total:.2f}, "
          f"details in {a.out}")


if __name__ == "__main__":
    main()
