import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / "claude" / "hooks" / "lane-context.py"


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def run_hook(self, payload, main=True, lane_script=None):
        env = {k: v for k, v in os.environ.items() if k != "CC_LANES_MAIN"}
        env["LANES_STATE_DIR"] = str(self.dir)
        env.pop("LANES_DB", None)
        if main:
            env["CC_LANES_MAIN"] = "1"
        if lane_script is not None:
            env["LANES_BIN"] = str(lane_script)
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=8,
        )

    def select(self, **fields):
        sel = {"id": "cc_1", "kind": "conversation", "title": "billing export",
               "status": "you", "at": 1}
        sel.update(fields)
        (self.dir / "selected-lane").write_text(json.dumps(sel))

    def fake_lane(self, body, code=0):
        path = self.dir / "lane"
        path.write_text(f"#!/bin/sh\n{body}\nexit {code}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def prompt(self, **kw):
        return self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1"}, **kw)

    def test_env_gate_silent_and_stores_nothing(self):
        self.select()
        proc = self.prompt(main=False)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertFalse((self.dir / "helm.db").exists())

    def test_recap_child_is_ignored(self):
        self.select()
        env = dict(os.environ, CC_LANES_MAIN="1", CC_RECAP_CHILD="1",
                   LANES_STATE_DIR=str(self.dir))
        env.pop("LANES_DB", None)
        proc = subprocess.run(
            [sys.executable, str(HOOK)], capture_output=True, text=True, env=env, timeout=8,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "kid"}))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertFalse((self.dir / "helm.db").exists())

    def context(self, **kw):
        proc = self.prompt(**kw)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout else ""

    def test_prompt_with_selection_injects_brief_once(self):
        self.select()
        lane = self.fake_lane('echo "TASK: Ship billing export"')
        first = self.context(lane_script=lane)
        self.assertIn('the user has lane cc_1 "billing export" selected', first)
        self.assertIn("clearly about different work", first)
        self.assertIn("TASK: Ship billing export", first)
        self.assertIn("lane brief cc_1", first)
        second = self.context(lane_script=lane)
        self.assertIn('lane cc_1 "billing export" is still selected', second)
        self.assertIn("lane new", second)
        self.assertNotIn("TASK:", second)

    def test_new_lane_or_changed_brief_sends_it_again(self):
        self.select()
        self.context(lane_script=self.fake_lane('echo "TASK: a"'))
        self.assertIn("TASK: b", self.context(lane_script=self.fake_lane('echo "TASK: b"')))
        self.select(id="cc_2")
        self.assertIn("TASK: b", self.context(lane_script=self.fake_lane('echo "TASK: b"')))

    def test_compact_resets_so_the_brief_returns(self):
        self.select()
        lane = self.fake_lane('echo "TASK: a"')
        self.context(lane_script=lane)
        self.run_hook({"hook_event_name": "SessionStart", "source": "compact",
                       "session_id": "s1"}, lane_script=lane)
        self.assertIn("TASK: a", self.context(lane_script=lane))

    def test_prompt_marks_the_main_chat_busy_and_stop_clears_it(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                       "transcript_path": "/tmp/t.jsonl"})
        busy = self.dir / "main-busy"
        self.assertEqual(json.loads(busy.read_text())["transcript"], "/tmp/t.jsonl")
        trigger = HOOK.parent / "cc-recap-trigger.sh"
        env = dict(os.environ, LANES_STATE_DIR=str(self.dir), CC_LANES_MAIN="1")
        subprocess.run([str(trigger)], input="{}", text=True, env=env, timeout=5)
        self.assertFalse(busy.exists())

    def test_other_sessions_never_mark_busy(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1"}, main=False)
        self.assertFalse((self.dir / "main-busy").exists())

    def test_chat_selection_is_silent(self):
        self.select(id="chat", kind="chat")
        self.assertEqual(self.prompt().stdout, "")

    def test_failing_lane_cli_falls_back_to_a_pointer(self):
        self.select()
        text = self.context(lane_script=self.fake_lane("", code=1))
        self.assertIn("lane show cc_1", text)

    def test_prompt_without_selection_is_silent(self):
        proc = self.prompt()
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_corrupt_selection_is_silent(self):
        (self.dir / "selected-lane").write_text("{not json")
        proc = self.prompt()
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_title_with_quotes_stays_valid_json(self):
        self.select(title='say "hi"\nnow')
        self.assertIn('say "hi"', json.loads(self.prompt().stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_session_id_stored_as_main_session(self):
        self.prompt()
        row = sqlite3.connect(self.dir / "helm.db").execute(
            "SELECT v FROM meta WHERE k='main_session_id'").fetchone()
        self.assertEqual(row[0], "s1")

    def test_session_id_overwrites_previous(self):
        self.prompt()
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "s2", "source": "startup"})
        row = sqlite3.connect(self.dir / "helm.db").execute(
            "SELECT v FROM meta WHERE k='main_session_id'").fetchone()
        self.assertEqual(row[0], "s2")

    def start(self, source, lane):
        return self.run_hook(
            {"hook_event_name": "SessionStart", "session_id": "s1", "source": source},
            lane_script=lane)

    def start_text(self, source, lane):
        proc = self.start(source, lane)
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_compact_injects_rules_then_digest(self):
        lane = self.fake_lane("echo '[you] one'; echo '[working] two'")
        out = json.loads(self.start("compact", lane).stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        text = out["additionalContext"]
        self.assertTrue(text.startswith("Helm lanes: you are the main chat"))
        self.assertTrue(text.endswith("Helm lanes:\n[you] one\n[working] two"))

    def test_startup_gets_rules_but_no_digest(self):
        lane = self.fake_lane("echo '[you] one'")
        text = self.start_text("startup", lane)
        self.assertIn("lane new", text)
        self.assertIn("One lane per deliverable", text)
        self.assertIn("default, not an assumption", text)
        self.assertIn('"[t_60793b] ', text)
        self.assertIn("lane new --no-select", text)
        self.assertNotIn("[you] one", text)
        self.assertIn("[you] one", self.start_text("resume", lane))

    def test_digest_capped_at_40_lines(self):
        lane = self.fake_lane("i=0; while [ $i -lt 100 ]; do echo \"line $i\"; i=$((i+1)); done")
        text = self.start_text("compact", lane).split("\n\n")[-1]
        self.assertEqual(len(text.splitlines()), 41)
        self.assertEqual(text.splitlines()[-1], "line 39")

    def test_failing_or_missing_lane_still_sends_rules(self):
        failing = self.fake_lane("echo oops", code=1)
        self.assertNotIn("oops", self.start_text("compact", failing))
        proc = self.start("compact", self.dir / "nope")
        self.assertEqual(proc.returncode, 0)

    def test_slow_lane_is_cut_off(self):
        lane = self.fake_lane("sleep 5; echo late")
        self.assertNotIn("late", self.start_text("compact", lane))

    def test_garbage_stdin_exits_zero_silently(self):
        env = dict(os.environ, CC_LANES_MAIN="1", LANES_STATE_DIR=str(self.dir))
        proc = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True,
                              text=True, env=env, timeout=5)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
