"""Integration tests for parley.py. Each test runs the real script against a temp PARLEY_DIR.

Run from the repo root: python3 -m unittest discover -s tests -v
"""
import gzip
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "parley", "parley.py")
LIVE_PID = str(os.getpid())
DEAD_PID = "999999"


class Parley(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="parley-test-")
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(self.proj)
        self.data = os.path.join(self.dir, "data")

    def tearDown(self):
        shutil.rmtree(self.dir)

    # -- helpers -------------------------------------------------------------

    def env(self, pid=LIVE_PID, host=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID")}
        env.update(PARLEY_DIR=self.data, CLAUDE_PID=pid)
        if host:
            env["CLAUDE_CODE_HOST_SESSION_ID"] = host
        return env

    def run_cli(self, sid, *args, stdin="", pid=LIVE_PID, host=None, ok=True):
        r = subprocess.run(["python3", SCRIPT, "--session", sid, *args], input=stdin,
                           capture_output=True, text=True, env=self.env(pid, host), cwd=self.proj)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def hook(self, name, payload, pid=LIVE_PID, host=None):
        r = subprocess.run(["python3", SCRIPT, name], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env(pid, host), cwd=self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout:
            return ""
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def start(self, sid, **kw):
        return self.hook("on-start", {"session_id": sid, "cwd": self.proj}, **kw)

    def prompt(self, sid, **kw):
        return self.hook("on-prompt", {"session_id": sid}, **kw)

    def end(self, sid):
        return self.hook("on-end", {"session_id": sid, "reason": "prompt_input_exit"})

    def make(self, sid, handle, about=None, **kw):
        self.start(sid, **kw)
        args = ["card", "--handle", handle] + (["--about", about] if about else [])
        self.run_cli(sid, *args, **kw)

    def dispatch(self, sid, to, body, *extra, ok=True, **kw):
        return self.run_cli(sid, "dispatch", "--to", to, "--origin", "explicit", *extra, body,
                            ok=ok, **kw)

    def log(self):
        with open(os.path.join(self.data, "log.jsonl")) as f:
            return [json.loads(l) for l in f]

    def ids(self, text):
        return [w.split("=")[1] for w in text.split() if w.startswith("id=")]

    # -- identity & inboxes --------------------------------------------------

    def test_new_session_gets_auto_handle_and_card_prompt(self):
        ctx = self.start("aaaa1111")
        self.assertIn("handle is 'proj'", ctx)
        self.assertIn("card --handle", ctx)

    def test_handles_unique_among_online(self):
        self.make("aaaa1111", "frontend")
        self.start("ffff2222")
        r = self.run_cli("ffff2222", "card", "--handle", "frontend", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("held by another online session", r.stderr)

    def test_ended_inbox_never_inherited(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "release Nov 10")
        self.end("ffff2222")
        r = self.dispatch("aaaa1111", "backend", "second update")
        self.assertIn("offline", r.stdout)
        self.make("nnnn3333", "backend")           # new chat takes the handle
        self.assertEqual(self.prompt("nnnn3333"), "")  # and sees none of the old mail
        self.dispatch("aaaa1111", "backend", "for the new chat")
        self.assertIn("for the new chat", self.prompt("nnnn3333"))
        ctx = self.start("ffff2222")                   # old chat resumes
        self.assertIn("backend-", ctx)              # gets a suffixed handle
        self.assertIn("release Nov 10", ctx)
        self.assertIn("second update", ctx)
        self.assertNotIn("for the new chat", ctx)

    def test_legacy_entry_without_cursor_still_receives(self):
        os.makedirs(os.path.join(self.data, "cursors"))
        with open(os.path.join(self.data, "sessions.json"), "w") as f:
            json.dump({"llll0000": {"name": "legacy", "cwd": "/x",
                                    "seen": "2026-10-21T00:00:00-07:00"}}, f)
        self.make("aaaa1111", "frontend")
        self.dispatch("aaaa1111", "legacy", "while you were away")
        self.assertIn("while you were away", self.start("llll0000"))

    def test_dead_pid_is_offline(self):
        self.make("aaaa1111", "frontend")
        self.make("cccc4444", "crashed", pid=DEAD_PID)
        roster = self.run_cli("aaaa1111", "roster").stdout
        self.assertNotIn("crashed", roster)
        self.assertIn("offline", self.run_cli("aaaa1111", "roster", "--all").stdout)

    def test_roster_shows_cards(self):
        self.make("aaaa1111", "frontend", "Web frontend, launch Oct 30")
        self.make("ffff2222", "backend")
        out = self.run_cli("ffff2222", "roster").stdout
        self.assertIn("card: Web frontend, launch Oct 30", out)

    # -- dispatch rules ------------------------------------------------------

    def test_origin_required(self):
        self.make("aaaa1111", "frontend")
        r = self.run_cli("aaaa1111", "dispatch", "--to", "*", "x", ok=False)
        self.assertNotEqual(r.returncode, 0)

    def test_derived_critical_refused(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        r = self.run_cli("aaaa1111", "dispatch", "--to", "backend", "--origin", "derived",
                         "--severity", "critical", "--why", "y", "x", ok=False)
        self.assertIn("can't be critical", r.stderr)
        self.assertEqual(self.log(), [])

    def test_critical_implies_confirm(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "x", "--severity", "critical")
        self.assertTrue(self.log()[0]["ack_requested"])

    def test_unknown_recipient_in_list_sends_nothing(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        r = self.dispatch("aaaa1111", "backend,nope", "x", ok=False)
        self.assertIn("no session with handle 'nope'", r.stderr)
        self.assertEqual(self.log(), [])

    def test_ambiguous_prefix_refused(self):
        self.make("aaaa1111", "frontend")
        self.make("ab110000", "one")
        self.make("ab220000", "two")
        r = self.dispatch("aaaa1111", "ab", "x", ok=False)
        self.assertIn("several online sessions", r.stderr)

    def test_multi_recipient_one_message_each(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.make("pppp3333", "docs-site")
        self.dispatch("aaaa1111", "backend,docs-site,backend", "x")
        self.assertEqual(sorted(m["to_name"] for m in self.log()), ["backend", "docs-site"])

    def test_body_cap_and_stdin(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        r = self.dispatch("aaaa1111", "backend", "-", stdin="x" * 1501, ok=False)
        self.assertIn("under 1500", r.stderr)
        self.dispatch("aaaa1111", "backend", "-", stdin="from stdin\n")
        self.assertEqual(self.log()[0]["body"], "from stdin")

    def test_messages_have_schema_version(self):
        self.make("aaaa1111", "frontend")
        self.dispatch("aaaa1111", "*", "hello")
        self.assertEqual(self.log()[0]["v"], 1)

    def test_broadcast_not_seen_by_later_sessions(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "*", "bcast")
        self.assertIn("bcast", self.prompt("ffff2222"))
        self.assertNotIn("bcast", self.start("zzzz9999"))

    def test_sender_never_sees_own_message(self):
        self.make("aaaa1111", "frontend")
        self.dispatch("aaaa1111", "*", "mine")
        self.assertEqual(self.prompt("aaaa1111"), "")

    def test_dispatch_catches_up_first(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "first")
        r = self.dispatch("ffff2222", "frontend", "reply")
        self.assertIn("arrived first", r.stdout)
        self.assertIn("first", r.stdout)
        self.assertEqual(self.prompt("ffff2222"), "")

    # -- confirmations -------------------------------------------------------

    def test_confirm_roundtrip(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "x", "--ask-confirm")
        mid = self.log()[0]["id"]
        self.assertIn("AWAITING", self.run_cli("aaaa1111", "awaiting").stdout)
        self.run_cli("ffff2222", "confirm", mid, "moved milestone")
        out = self.run_cli("aaaa1111", "awaiting").stdout
        self.assertIn("confirmed by backend", out)
        self.assertIn("moved milestone", out)

    def test_cannot_confirm_someone_elses_message(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.make("pppp3333", "docs-site")
        self.dispatch("aaaa1111", "backend", "x", "--ask-confirm")
        r = self.run_cli("pppp3333", "confirm", self.log()[0]["id"], "hi", ok=False)
        self.assertIn("wasn't addressed", r.stderr)

    # -- ring ----------------------------------------------------------------

    def test_ring_prints_address_and_guards_loops(self):
        self.make("aaaa1111", "frontend", host="local_aaa")
        self.make("ffff2222", "backend", host="local_fff")
        r = self.dispatch("aaaa1111", "backend", "now", "--ring", host="local_aaa")
        self.assertIn('to="local_fff"', r.stdout)
        self.assertIn("rung=yes", self.prompt("ffff2222", host="local_fff"))
        r = self.dispatch("ffff2222", "frontend", "thanks", "--ring", ok=False, host="local_fff")
        self.assertIn("rang this session", r.stderr)
        self.dispatch("ffff2222", "frontend", "thanks", host="local_fff")

    def test_ring_refused_for_broadcast_offline_and_no_address(self):
        self.make("aaaa1111", "frontend")
        self.make("nnnn3333", "nohost")
        self.assertIn("named recipients",
                      self.dispatch("aaaa1111", "*", "x", "--ring", ok=False).stderr)
        self.assertIn("no recorded address",
                      self.dispatch("aaaa1111", "nohost", "x", "--ring", ok=False).stderr)
        self.assertEqual(self.log(), [])

    # -- delivery ------------------------------------------------------------

    def test_hooks_silent_when_idle(self):
        self.make("aaaa1111", "frontend")
        self.assertEqual(self.prompt("aaaa1111"), "")

    def test_injection_is_delimited_and_capped(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        for i in range(11):
            self.dispatch("aaaa1111", "backend", f"update {i}")
        ctx = self.prompt("ffff2222")
        self.assertEqual(ctx.count("<<parley-msg"), 8)
        self.assertEqual(ctx.count("<<end>>"), 8)
        self.assertIn("3 more waiting", ctx)
        rest = self.run_cli("ffff2222", "collect").stdout
        self.assertEqual(sorted(self.ids(rest)), sorted(m["id"] for m in self.log()[8:]))
        self.assertIn("no new messages", self.run_cli("ffff2222", "collect").stdout)

    def test_injection_char_cap(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        for i in range(4):
            self.dispatch("aaaa1111", "backend", f"{i} " + "y" * 1300)  # ~1450 rendered
        ctx = self.prompt("ffff2222")
        self.assertEqual(ctx.count("<<parley-msg"), 2)
        self.assertIn("2 more waiting", ctx)

    def test_peek_does_not_advance(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "x")
        self.assertIn("1 new", self.run_cli("ffff2222", "collect", "--peek").stdout)
        self.assertIn("1 new", self.run_cli("ffff2222", "collect").stdout)

    def test_on_prompt_latency(self):
        self.make("aaaa1111", "frontend")
        t = time.perf_counter()
        for _ in range(5):
            self.prompt("aaaa1111")
        self.assertLess((time.perf_counter() - t) / 5, 0.15)

    # -- seal (privacy guard) ------------------------------------------------

    def test_no_send_blocks_dispatch_and_confirm(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "x", "--ask-confirm")
        self.run_cli("ffff2222", "seal", "--mode", "no-send")
        self.assertIn("sealed", self.dispatch("ffff2222", "frontend", "y", ok=False).stderr)
        r = self.run_cli("ffff2222", "confirm", self.log()[0]["id"], "ok", ok=False)
        self.assertIn("sealed", r.stderr)
        self.assertEqual(len(self.log()), 1)
        self.assertIn("<<parley-msg", self.prompt("ffff2222"))  # still receives

    def test_no_receive_refuses_named_and_drops_broadcasts(self):
        self.make("aaaa1111", "frontend")
        self.make("jjjj5555", "journal", "personal notes")
        self.run_cli("jjjj5555", "seal", "--mode", "no-receive")
        r = self.dispatch("aaaa1111", "journal", "hello", ok=False)
        self.assertIn("doesn't accept", r.stderr)
        self.dispatch("aaaa1111", "*", "broadcast")
        self.assertEqual(self.prompt("jjjj5555"), "")
        self.run_cli("jjjj5555", "seal", "--mode", "open")   # reopening doesn't replay
        self.assertEqual(self.prompt("jjjj5555"), "")
        self.dispatch("aaaa1111", "*", "after reopening")
        self.assertIn("after reopening", self.prompt("jjjj5555"))

    def test_sealed_hidden_from_roster_and_peer_lists(self):
        self.make("aaaa1111", "frontend")
        self.make("jjjj5555", "journal", "personal notes")
        self.run_cli("jjjj5555", "seal", "--mode", "private")
        self.assertNotIn("journal", self.run_cli("aaaa1111", "roster").stdout)
        self.assertNotIn("journal", self.start("aaaa1111"))
        everything = self.run_cli("aaaa1111", "roster", "--all").stdout
        self.assertIn("sealed (private)", everything)
        self.assertNotIn("personal notes", everything)

    def test_private_session_sees_nothing_at_start(self):
        self.make("aaaa1111", "frontend", "Web frontend, launch Oct 30")
        self.make("jjjj5555", "journal")
        self.run_cli("jjjj5555", "seal", "--mode", "private")
        self.dispatch("aaaa1111", "*", "bcast")
        ctx = self.start("jjjj5555")
        self.assertIn("sealed (private)", ctx)
        for leaked in ("frontend", "Web frontend", "bcast"):
            self.assertNotIn(leaked, ctx)

    def test_folder_default_seals_new_sessions(self):
        self.start("aaaa1111")
        self.run_cli("aaaa1111", "seal", "--mode", "private", "--folder")
        sub = os.path.join(self.proj, "deeper")
        os.makedirs(sub)
        ctx = self.hook("on-start", {"session_id": "bbbb2222", "cwd": sub})
        self.assertIn("sealed (private)", ctx)
        other = os.path.join(self.dir, "proj-other")   # sibling with a shared prefix
        os.makedirs(other)
        ctx = self.hook("on-start", {"session_id": "cccc3333", "cwd": other})
        self.assertNotIn("is sealed", ctx)

    def test_sealed_sessions_are_not_forgotten(self):
        self.make("aaaa1111", "frontend")
        self.make("jjjj5555", "journal")
        self.run_cli("jjjj5555", "seal", "--mode", "private")
        self.end("jjjj5555")
        path = os.path.join(self.data, "sessions.json")
        with open(path) as f:
            reg = json.load(f)
        reg["jjjj5555"]["seen"] = "2020-01-01T00:00:00+00:00"
        with open(path, "w") as f:
            json.dump(reg, f)
        self.start("aaaa1111")                     # triggers forgetting
        with open(path) as f:
            self.assertEqual(json.load(f)["jjjj5555"]["mode"], "private")

    def test_acting_for_another_session_keeps_its_address(self):
        self.make("jjjj5555", "journal", host="local_journal")
        env = dict(self.env(host="local_other"), CLAUDE_CODE_SESSION_ID="aaaa1111")
        r = subprocess.run(["python3", SCRIPT, "--session", "jjjj5555", "seal", "--mode", "no-send"],
                           capture_output=True, text=True, env=env, cwd=self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.data, "sessions.json")) as f:
            self.assertEqual(json.load(f)["jjjj5555"]["host_id"], "local_journal")

    # -- derived messages are targeted --------------------------------------

    def test_derived_needs_why_and_a_named_recipient(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        r = self.run_cli("aaaa1111", "dispatch", "--to", "*", "--origin", "derived",
                         "--why", "y", "x", ok=False)
        self.assertIn("can't be broadcast", r.stderr)
        r = self.run_cli("aaaa1111", "dispatch", "--to", "backend", "--origin", "derived", "x",
                         ok=False)
        self.assertIn("needs --why", r.stderr)
        self.assertEqual(self.log(), [])
        self.run_cli("aaaa1111", "dispatch", "--to", "backend", "--origin", "derived",
                     "--why", "you track the release date", "release moved")
        self.assertIn("why this is for you: you track the release date", self.prompt("ffff2222"))

    def test_start_context_states_sharing_rule(self):
        self.assertIn("Sharing rule:", self.start("aaaa1111"))

    # -- rotated session ids (same app chat) --------------------------------

    def test_rotation_keeps_handle_card_and_gap_messages(self):
        self.make("aaaa1111", "frontend")
        self.make("oldd0001", "billing", "Billing service: invoices and refunds", host="local_chat")
        self.dispatch("aaaa1111", "billing", "before rotation")
        self.assertIn("before rotation", self.prompt("oldd0001", host="local_chat"))
        self.dispatch("aaaa1111", "billing", "sent in the gap")
        self.end("oldd0001")
        ctx = self.hook("on-start", {"session_id": "neww0002", "cwd": self.proj,
                                     "source": "resume"}, host="local_chat")
        self.assertIn("handle is 'billing'", ctx)
        self.assertIn("sent in the gap", ctx)
        self.assertNotIn("before rotation", ctx)
        self.dispatch("aaaa1111", "billing", "after rotation")      # handle -> new id
        self.assertEqual(self.log()[-1]["to"], "neww0002")
        self.assertIn("after rotation", self.prompt("neww0002", host="local_chat"))
        with open(os.path.join(self.data, "sessions.json")) as f:
            reg = json.load(f)
        self.assertNotIn("oldd0001", reg)
        self.assertEqual(reg["neww0002"]["about"], "Billing service: invoices and refunds")
        self.assertEqual(reg["neww0002"]["aliases"], ["oldd0001"])
        self.assertEqual(reg["neww0002"]["start_source"], "resume")

    def test_rotation_no_duplicate_deliveries(self):
        self.make("aaaa1111", "frontend")
        self.make("oldd0001", "billing", host="local_chat")
        self.end("oldd0001")
        self.hook("on-start", {"session_id": "neww0002", "cwd": self.proj})  # no host yet
        self.dispatch("aaaa1111", "*", "broadcast to new id")
        self.assertIn("broadcast to new id", self.prompt("neww0002"))
        self.prompt("neww0002", host="local_chat")                        # merge happens now
        self.assertEqual(self.prompt("neww0002", host="local_chat"), "")

    def test_rotation_keeps_the_stricter_seal(self):
        self.make("oldd0001", "journal", host="local_chat")
        self.run_cli("oldd0001", "seal", "--mode", "private", host="local_chat")
        self.end("oldd0001")
        ctx = self.hook("on-start", {"session_id": "neww0002", "cwd": self.proj},
                        host="local_chat")
        self.assertIn("sealed (private)", ctx)

    def test_different_chat_never_inherits(self):
        self.make("aaaa1111", "frontend")
        self.make("oldd0001", "billing", "Billing service: invoices and refunds", host="local_chat_a")
        self.dispatch("aaaa1111", "billing", "for chat A")
        self.end("oldd0001")
        ctx = self.hook("on-start", {"session_id": "othr0003", "cwd": self.proj},
                        host="local_chat_b")
        self.assertNotIn("billing", ctx)
        self.assertNotIn("for chat A", ctx)

    # -- attachments ---------------------------------------------------------

    def write_file(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_attachment_is_a_read_only_snapshot(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        draft = self.write_file("draft.md", "# Draft\nversion one\n")
        self.dispatch("aaaa1111", "backend", "please review the attached draft",
                      "--attach", draft)
        self.write_file("draft.md", "changed after sending")          # sender edits later
        (item,) = self.log()[0]["attachments"]
        self.assertTrue(item["path"].startswith(os.path.join(self.data, "attachments")))
        with open(item["path"]) as f:
            self.assertEqual(f.read(), "# Draft\nversion one\n")
        self.assertFalse(os.access(item["path"], os.W_OK))
        ctx = self.prompt("ffff2222")
        self.assertIn("attached files", ctx)
        self.assertIn("draft.md", ctx)

    def test_bad_attachments_send_nothing(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        binary = self.write_file("image.png", "x")
        big = self.write_file("big.md", "x" * 2_000_001)
        for path, error in ((binary, "only text files"), (big, "the limit is"),
                            (os.path.join(self.dir, "missing.md"), "no such file")):
            r = self.dispatch("aaaa1111", "backend", "x", "--attach", path, ok=False)
            self.assertIn(error, r.stderr)
        self.assertEqual(self.log(), [])

    def test_sweep_removes_archived_attachments(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "old", "--attach", self.write_file("a.md", "a"))
        path = os.path.join(self.data, "log.jsonl")
        rows = self.log()
        rows[0]["ts"] = "2026-08-01T10:00:00-07:00"
        with open(path, "w") as f:
            f.write(json.dumps(rows[0]) + "\n")
        self.prompt("ffff2222")
        self.prompt("aaaa1111")   # every online reader, sender included, has to be past it
        self.assertIn("archived 1", self.run_cli("aaaa1111", "sweep").stdout)
        self.assertFalse(os.path.exists(os.path.join(self.data, "attachments", rows[0]["id"])))

    # -- awaiting counts as seen ----------------------------------------------

    def test_confirmation_seen_in_awaiting_is_not_redelivered(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        self.dispatch("aaaa1111", "backend", "question", "--ask-confirm")
        self.prompt("ffff2222")
        self.run_cli("ffff2222", "confirm", self.log()[0]["id"], "the answer")
        self.assertIn("the answer", self.run_cli("aaaa1111", "awaiting").stdout)
        self.assertEqual(self.prompt("aaaa1111"), "")

    # -- sweep ---------------------------------------------------------------

    def test_sweep_cuts_old_read_prefix_and_shifts_cursors(self):
        self.make("aaaa1111", "frontend")
        self.make("ffff2222", "backend")
        for i in range(4):
            self.dispatch("aaaa1111", "backend", f"old {i}")
        path = os.path.join(self.data, "log.jsonl")
        rows = self.log()
        for m in rows:
            m["ts"] = "2026-08-01T10:00:00-07:00"
        with open(path, "w") as f:
            f.write("".join(json.dumps(m) + "\n" for m in rows))
        self.dispatch("aaaa1111", "backend", "fresh")
        self.assertIn("nothing to sweep", self.run_cli("aaaa1111", "sweep").stdout)  # unread
        self.prompt("ffff2222")
        self.assertIn("archived 4", self.run_cli("aaaa1111", "sweep").stdout)
        self.assertEqual([m["body"] for m in self.log()], ["fresh"])
        self.dispatch("aaaa1111", "backend", "after sweep")
        ctx = self.prompt("ffff2222")
        self.assertIn("after sweep", ctx)
        self.assertNotIn("fresh", ctx)
        archive = os.path.join(self.data, "archive")
        (name,) = os.listdir(archive)
        with gzip.open(os.path.join(archive, name)) as g:
            self.assertEqual(len(g.read().splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
