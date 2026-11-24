"""Tests for lanedb, sync and the lane CLI. Fixtures live in a temp dir; the
real registry, session-index and tmux server are never touched."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lanedb  # noqa: E402
import sync  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
LANE_BIN = os.path.join(HERE, "lane")

FAKE_HOST = """#!/bin/sh
case "$1" in
  list) cat "$FAKE_ROWS" ;;
  archive) echo "$2" >> "$FAKE_ARCHIVED" ;;
esac
"""


def load_lane_module():
    loader = importlib.machinery.SourceFileLoader("lane_cli", LANE_BIN)
    spec = importlib.util.spec_from_loader("lane_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def row(id, **over):
    base = {
        "id": id, "topic": "", "label": id, "recap": "", "unread": False,
        "prs": [], "state": "pickup", "session_id": "", "blocked_on": "",
        "blocked_since": 0, "wake_at": 0, "archived": False,
        "updated_at": NOW - 60, "created": NOW - DAY, "cold": False,
        "question": "", "waiting_suggest": "",
    }
    base.update(over)
    return base


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.helm = os.path.join(t, "helm")
        self.projects = os.path.join(t, "projects")
        self.index = os.path.join(t, "index")
        for d in (self.helm, self.projects, self.index):
            os.makedirs(d)
        self.rows_file = os.path.join(t, "rows.json")
        self.archived_log = os.path.join(t, "archived.log")
        host = os.path.join(t, "host.sh")
        with open(host, "w") as fh:
            fh.write(FAKE_HOST)
        os.chmod(host, os.stat(host).st_mode | stat.S_IXUSR)
        self.env = {
            "LANES_STATE_DIR": self.helm,
            "LANES_DB": os.path.join(self.helm, "helm.db"),
            "CC_HOST": host, "CLAUDE_PROJECTS": self.projects,
            "SESSION_INDEX": self.index,
            "FAKE_ROWS": self.rows_file, "FAKE_ARCHIVED": self.archived_log,
        }
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.set_rows([])
        self.con = lanedb.connect()

    def tearDown(self):
        self.con.close()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def set_rows(self, rows):
        with open(self.rows_file, "w") as fh:
            json.dump(rows, fh)

    def run_sync(self, now=NOW):
        return sync.sync(self.con, now=now)

    def lane(self, id):
        return lanedb.lane(self.con, id)

    def add_journal(self, session, run, lines, mtime, script=None):
        d = os.path.join(self.projects, "proj", session, "subagents",
                         "workflows", run)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "journal.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(
                l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n")
        os.utime(path, (mtime, mtime))
        if script:
            sd = os.path.join(self.projects, "proj", session, "workflows",
                              "scripts")
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, f"x-{run}.js"), "w") as fh:
                fh.write(script)
        return path


def started(key, phase="Build", agent="a1"):
    return {"type": "started", "key": key, "agentId": agent,
            "label": "l", "phase": phase}


def result(key, text="ok", agent="a1"):
    return {"type": "result", "key": key, "agentId": agent, "result": text}


class LaneDbTests(Fixture):
    def test_old_db_gains_brief_columns(self):
        path = os.path.join(self.helm, "old.db")
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                    " title TEXT NOT NULL)")
        old.commit()
        old.close()
        con = lanedb.connect(path)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(lanes)")}
        self.assertTrue({"goal", "goal_source", "recap", "recap_at"} <= cols)
        con.close()

    def test_schema_is_idempotent(self):
        lanedb.upsert_lane(self.con, id="a", kind="conversation", title="A")
        self.con.close()
        self.con = lanedb.connect()
        self.assertEqual(self.lane("a")["title"], "A")

    def test_dedupe_blocks_second_insert(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        self.assertTrue(lanedb.add_event(self.con, "a", 1, "note", "x", dedupe="k"))
        self.assertFalse(lanedb.add_event(self.con, "a", 2, "note", "y", dedupe="k"))
        self.assertEqual(len(self.lane("a")["events"]), 1)

    def test_events_without_dedupe_all_insert(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        lanedb.add_event(self.con, "a", 1, "note", "x")
        lanedb.add_event(self.con, "a", 2, "note", "x")
        self.assertEqual(len(self.lane("a")["events"]), 2)

    def test_event_text_is_clipped(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        lanedb.add_event(self.con, "a", 1, "note", "t" * 500, "d" * 900)
        event = self.lane("a")["events"][0]
        self.assertLessEqual(len(event["title"]), 90)
        self.assertLessEqual(len(event["detail"]), 400)

    def test_pinned_topic_survives_upsert(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        lanedb.set_topic(self.con, "a", "mine")
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A",
                           topic="other", topic_source="ai")
        self.assertEqual(self.lane("a")["topic"], "mine")
        self.assertEqual(self.lane("a")["topic_source"], "you")

    def test_force_topic_overrides_pin(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        lanedb.set_topic(self.con, "a", "mine")
        lanedb.upsert_lane(self.con, force_topic=True, id="a", kind="chat",
                           title="A", topic="other", topic_source="ai")
        self.assertEqual(self.lane("a")["topic"], "other")

    def test_upsert_keeps_unmentioned_columns(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A", created=5)
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="B")
        self.assertEqual(self.lane("a")["created"], 5)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            lanedb.upsert_lane(self.con, id="a", kind="chat", title="A", bogus=1)

    def test_trail_is_last_eight_oldest_first(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        for i in range(12):
            lanedb.add_event(self.con, "a", i, "note", f"e{i}")
        trail = lanedb.lanes(self.con)[0]["trail"]
        self.assertEqual([e["title"] for e in trail],
                         [f"e{i}" for i in range(4, 12)])

    def test_lane_events_newest_first_capped(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        for i in range(40):
            lanedb.add_event(self.con, "a", i, "note", f"e{i}")
        events = self.lane("a")["events"]
        self.assertEqual(len(events), 30)
        self.assertEqual(events[0]["title"], "e39")

    def test_archived_hidden_unless_asked(self):
        lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
        lanedb.archive(self.con, "a", at=9)
        self.assertEqual(lanedb.lanes(self.con), [])
        self.assertEqual(len(lanedb.lanes(self.con, include_archived=True)), 1)

    def test_missing_lane_is_none(self):
        self.assertIsNone(self.lane("nope"))

    def test_meta_roundtrip(self):
        self.assertIsNone(lanedb.get_meta(self.con, "k"))
        lanedb.set_meta(self.con, "k", "1")
        lanedb.set_meta(self.con, "k", "2")
        self.assertEqual(lanedb.get_meta(self.con, "k"), "2")

    def test_selection_roundtrip_and_absent(self):
        self.assertIsNone(lanedb.read_selection())
        lanedb.write_selection({"id": "a", "kind": "chat", "title": "A",
                                "status": "idle"})
        sel = lanedb.read_selection()
        self.assertEqual((sel["id"], sel["kind"], sel["status"]),
                         ("a", "chat", "idle"))
        self.assertEqual(lanedb.selection_path(),
                         os.path.join(self.helm, "selected-lane"))

    def test_corrupt_selection_reads_as_none(self):
        with open(lanedb.selection_path(), "w") as fh:
            fh.write("{nope")
        self.assertIsNone(lanedb.read_selection())

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with lanedb.transaction(self.con):
                lanedb.upsert_lane(self.con, id="a", kind="chat", title="A")
                raise RuntimeError
        self.assertIsNone(self.lane("a"))


class StatusMappingTests(unittest.TestCase):
    def status(self, **over):
        return sync.derive_status(row("x", **over))

    def test_answer_is_you_with_question(self):
        self.assertEqual(self.status(state="answer", question="Q?"), ("you", "Q?"))

    def test_answer_without_question_uses_recap(self):
        self.assertEqual(self.status(state="answer", recap="R"), ("you", "R"))

    def test_running_is_working(self):
        self.assertEqual(self.status(state="running")[0], "working")

    def test_blocked_on_is_person(self):
        self.assertEqual(self.status(state="waiting", blocked_on="Alex"),
                         ("person", "Alex"))

    def test_waiting_with_suggestion_is_person(self):
        self.assertEqual(self.status(state="waiting", waiting_suggest="Ana"),
                         ("person", "Ana"))

    def test_waiting_without_name_is_idle(self):
        self.assertEqual(self.status(state="waiting")[0], "idle")

    def test_suggestion_ignored_outside_waiting(self):
        self.assertEqual(self.status(state="pickup", waiting_suggest="Ana")[0],
                         "ready")

    def test_open_and_approved_prs_are_review(self):
        for mark in ("open", "approved"):
            got = self.status(prs=[{"label": "r#1", "mark": mark}])
            self.assertEqual(got, ("review", "r#1"), mark)

    def test_fail_and_pending_are_ci(self):
        for mark in ("fail", "pending"):
            got = self.status(prs=[{"label": "r#1", "mark": mark}])
            self.assertEqual(got, ("ci", "r#1"), mark)

    def test_inert_marks_fall_through(self):
        for mark in ("draft", "merged", "closed", "unknown"):
            got = self.status(state="done", prs=[{"label": "r#1", "mark": mark}])
            self.assertEqual(got[0], "done", mark)

    def test_review_beats_ci_across_prs(self):
        prs = [{"label": "a#1", "mark": "fail"}, {"label": "b#2", "mark": "open"}]
        self.assertEqual(self.status(prs=prs), ("review", "b#2"))

    def test_precedence_you_over_everything(self):
        got = self.status(state="answer", blocked_on="M",
                          prs=[{"label": "a#1", "mark": "open"}])
        self.assertEqual(got[0], "you")

    def test_person_over_review(self):
        got = self.status(state="pickup", blocked_on="M",
                          prs=[{"label": "a#1", "mark": "open"}])
        self.assertEqual(got[0], "person")

    def test_pickup_ready_done_idle(self):
        self.assertEqual(self.status(state="pickup", recap="r"), ("ready", "r"))
        self.assertEqual(self.status(state="done")[0], "done")
        for state in ("dead", "empty", ""):
            self.assertEqual(self.status(state=state)[0], "idle", state)


class StaleRuleTests(unittest.TestCase):
    def test_old_idle_is_stale(self):
        self.assertTrue(sync.is_stale("idle", NOW - 8 * DAY, NOW))

    def test_recent_idle_is_not(self):
        self.assertFalse(sync.is_stale("idle", NOW - 6 * DAY, NOW))

    def test_done_stale_after_two_days(self):
        self.assertTrue(sync.is_stale("done", NOW - 3 * DAY, NOW))
        self.assertFalse(sync.is_stale("done", NOW - DAY, NOW))

    def test_empty_registry_state_after_a_day(self):
        self.assertTrue(sync.is_stale("idle", NOW - 2 * DAY, NOW, "empty"))
        self.assertFalse(sync.is_stale("idle", NOW - 3600, NOW, "empty"))

    def test_protected_statuses_never_stale(self):
        for status in sync.PROTECTED:
            self.assertFalse(sync.is_stale(status, NOW - 30 * DAY, NOW), status)


class ConversationSyncTests(Fixture):
    def test_upserts_lanes_and_summary(self):
        self.set_rows([
            row("cc_1", state="answer", question="Q?", label="one"),
            row("cc_2", updated_at=NOW - 10 * DAY, label="two"),
            row("", state="empty", topic="declared"),
        ])
        counts = self.run_sync()
        self.assertEqual(counts, {"lanes": 3, "need_you": 1, "stale": 1})
        self.assertEqual(sync.summary_line(counts),
                         "3 lanes · 1 need you · 1 stale")
        self.assertEqual(self.lane("cc_1")["status"], "you")
        self.assertEqual(self.lane("cc_1")["title"], "one")
        self.assertIsNotNone(self.lane("chat"))

    def test_resync_is_idempotent(self):
        self.set_rows([row("cc_1", recap="did x", state="answer", question="Q",
                           prs=[{"label": "r#1", "mark": "open"}],
                           blocked_on="M", blocked_since=NOW - 5)])
        self.run_sync()
        first = len(self.lane("cc_1")["events"])
        self.run_sync(NOW + 30)
        self.run_sync(NOW + 60)
        self.assertEqual(len(self.lane("cc_1")["events"]), first)
        kinds = {e["kind"] for e in self.lane("cc_1")["events"]}
        self.assertEqual(kinds, {"checkpoint", "ask", "block", "pr"})

    def test_new_recap_adds_checkpoint(self):
        self.set_rows([row("cc_1", recap="one", updated_at=NOW - 100)])
        self.run_sync()
        self.set_rows([row("cc_1", recap="two", updated_at=NOW - 10)])
        self.run_sync()
        titles = [e["title"] for e in self.lane("cc_1")["events"]]
        self.assertEqual(sorted(titles), ["one", "two"])

    def test_pr_mark_change_adds_event(self):
        self.set_rows([row("cc_1", prs=[{"label": "r#1", "mark": "pending"}])])
        self.run_sync()
        self.set_rows([row("cc_1", prs=[{"label": "r#1", "mark": "merged"}])])
        self.run_sync(NOW + 5)
        titles = {e["title"] for e in self.lane("cc_1")["events"]}
        self.assertEqual(titles, {"r#1 pending", "r#1 merged"})

    def test_block_then_unblock(self):
        self.set_rows([row("cc_1", blocked_on="Alex", blocked_since=NOW - 50)])
        self.run_sync()
        self.set_rows([row("cc_1")])
        self.run_sync(NOW + 10)
        kinds = [e["kind"] for e in self.lane("cc_1")["events"]]
        self.assertIn("block", kinds)
        self.assertIn("unblock", kinds)

    def test_conversation_missing_from_registry_is_retired(self):
        self.set_rows([row("cc_1", state="answer", question="Q")])
        self.run_sync()
        self.set_rows([])
        counts = self.run_sync()
        self.assertEqual(counts["need_you"], 0)
        self.assertTrue(self.lane("cc_1")["archived_at"])

    def test_archive_mirrors_registry_and_returns(self):
        self.set_rows([row("cc_1", archived=True)])
        self.run_sync()
        self.assertTrue(self.lane("cc_1")["archived_at"])
        self.assertEqual(lanedb.lanes(self.con)[0]["id"], "chat")
        self.set_rows([row("cc_1", archived=False)])
        self.run_sync(NOW + 5)
        self.assertEqual(self.lane("cc_1")["archived_at"], 0)

    def test_registry_topic_then_pin_wins(self):
        self.set_rows([row("cc_1", topic="Hub")])
        self.run_sync()
        self.assertEqual(
            (self.lane("cc_1")["topic"], self.lane("cc_1")["topic_source"]),
            ("hub", "registry"))
        lanedb.set_topic(self.con, "cc_1", "mine")
        self.set_rows([row("cc_1", topic="other")])
        self.run_sync(NOW + 5)
        self.assertEqual(self.lane("cc_1")["topic"], "mine")

    def write_index(self, sid, data):
        with open(os.path.join(self.index, sid + ".json"), "w") as fh:
            json.dump(data, fh)

    def test_topic_suggest_applies_over_registry(self):
        self.write_index("s1", {"topic_suggest": "new: Billing Export"})
        self.set_rows([row("cc_1", topic="misc", session_id="s1")])
        self.run_sync()
        self.assertEqual(
            (self.lane("cc_1")["topic"], self.lane("cc_1")["topic_source"]),
            ("billing-export", "ai"))

    def test_topic_suggest_never_overrides_pin_or_applied_ai(self):
        self.write_index("s1", {"topic_suggest": "elsewhere"})
        self.set_rows([row("cc_1", session_id="s1")])
        lanedb.upsert_lane(self.con, id="cc_1", kind="conversation", title="t",
                           topic="pinned", topic_source="you")
        self.run_sync()
        self.assertEqual(self.lane("cc_1")["topic"], "pinned")
        lanedb.set_topic(self.con, "cc_1", "consolidated", "ai")
        self.run_sync(NOW + 5)
        self.assertEqual(self.lane("cc_1")["topic"], "consolidated")

    def test_missing_or_broken_host_keeps_existing_lanes(self):
        self.set_rows([row("cc_1")])
        self.run_sync()
        with open(self.rows_file, "w") as fh:
            fh.write("not json")
        self.run_sync(NOW + 5)
        self.assertIsNotNone(self.lane("cc_1"))

    def test_stale_set_and_cleared(self):
        self.set_rows([row("cc_1", updated_at=NOW - 9 * DAY)])
        self.run_sync()
        self.assertEqual(self.lane("cc_1")["stale_since"], NOW)
        self.set_rows([row("cc_1", updated_at=NOW - 5)])
        self.run_sync(NOW + 5)
        self.assertIsNone(self.lane("cc_1")["stale_since"])

    def test_registry_empty_state_goes_stale_after_a_day(self):
        self.set_rows([row("cc_1", state="empty", updated_at=NOW - 2 * DAY)])
        self.run_sync()
        self.assertTrue(self.lane("cc_1")["stale_since"])

    def test_chat_never_stale(self):
        self.run_sync()
        self.run_sync(NOW + 30 * DAY)
        self.assertIsNone(self.lane("chat")["stale_since"])


class WorkflowSyncTests(Fixture):
    def test_running_workflow_with_parent_and_title(self):
        self.set_rows([row("cc_1", session_id="sess1", topic="hub")])
        self.add_journal("sess1", "wf_aaa", [
            {"type": "launched"}, started("k1"), started("k2", "UI", "a2"),
        ], NOW - 30, script="export const meta = {\n  name: 'n',\n"
                            "  description: 'Build the thing',\n}\n")
        self.run_sync()
        wf = self.lane("wf_aaa")
        self.assertEqual((wf["kind"], wf["parent"]), ("workflow", "cc_1"))
        self.assertEqual(wf["title"], "Build the thing")
        self.assertEqual(wf["topic"], "hub")
        self.assertEqual((wf["status"], wf["status_detail"]), ("working", "UI"))
        phases = [e["title"] for e in wf["events"] if e["kind"] == "phase"]
        self.assertEqual(sorted(phases), ["Build", "UI"])
        self.assertEqual(json.loads(lanedb.get_meta(self.con, "wf:wf_aaa"))
                         ["started"], 2)

    def add_run_record(self, session, run, record):
        d = os.path.join(self.projects, "proj", session, "workflows")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{run}.json"), "w") as fh:
            json.dump(record, fh)

    def test_stopped_run_is_idle_not_working(self):
        self.add_journal("s", "wf_k", [started("k")], NOW - 30)
        self.add_run_record("s", "wf_k", {"status": "killed", "summary": "Audit"})
        self.run_sync()
        wf = self.lane("wf_k")
        self.assertEqual((wf["status"], wf["status_detail"]), ("idle", "stopped"))

    def test_resumed_run_takes_title_from_its_record(self):
        self.add_journal("s", "wf_r", [started("k"), result("k")], NOW - 10 * 60)
        self.add_run_record("s", "wf_r", {"status": "completed",
                                          "summary": "Audit old conversations"})
        self.run_sync()
        wf = self.lane("wf_r")
        self.assertEqual(wf["title"], "Audit old conversations")
        self.assertEqual(wf["status"], "done")

    def test_unreadable_record_falls_back_to_journal(self):
        self.add_journal("s", "wf_u", [started("k")], NOW - 30)
        d = os.path.join(self.projects, "proj", "s", "workflows")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wf_u.json"), "w") as fh:
            fh.write("{not json")
        self.run_sync()
        self.assertEqual((self.lane("wf_u")["status"], self.lane("wf_u")["title"]),
                         ("working", "wf_u"))

    def make_task(self, lane_id, topic="api"):
        lanedb.upsert_lane(self.con, id=lane_id, kind="task", title=lane_id, topic=topic,
                           topic_source="chat", created=NOW - 500, last_activity=NOW - 500,
                           status="ready")

    def test_main_chat_workflow_files_under_the_selected_lane(self):
        lanedb.set_meta(self.con, "main_session_id", "main")
        self.make_task("t_1")
        sel = os.path.join(self.helm, "selected-lane")
        with open(sel, "w") as fh:
            json.dump({"id": "t_1", "kind": "task", "title": "t_1", "at": 1000}, fh)
        self.add_journal("main", "wf_m", [started("k")], NOW - 30)
        self.run_sync()
        self.assertEqual((self.lane("wf_m")["parent"], self.lane("wf_m")["topic"]),
                         ("t_1", "api"))
        self.assertEqual(self.lane("t_1")["status"], "working")
        with open(sel, "w") as fh:
            json.dump({"id": "chat", "kind": "chat", "title": "chat", "at": NOW}, fh)
        self.add_journal("main", "wf_m", [started("k"), result("k")], NOW - 5 * 60)
        self.run_sync()
        self.assertEqual(self.lane("wf_m")["parent"], "t_1")
        self.assertEqual(self.lane("t_1")["status"], "ready")

    def test_selection_made_after_launch_does_not_claim_the_run(self):
        lanedb.set_meta(self.con, "main_session_id", "main")
        self.make_task("t_2")
        with open(os.path.join(self.helm, "selected-lane"), "w") as fh:
            # Picked after the journal file was born (born = real file time).
            json.dump({"id": "t_2", "kind": "task", "title": "t_2", "at": NOW * 2}, fh)
        self.add_journal("main", "wf_n", [started("k")], NOW - 30)
        self.run_sync()
        self.assertEqual(self.lane("wf_n")["parent"], "chat")

    def test_labelled_workflows_file_under_their_lanes_whatever_is_selected(self):
        lanedb.set_meta(self.con, "main_session_id", "main")
        self.make_task("t_aaaa01", topic="api")
        self.make_task("t_bbbb02", topic="hub")
        with open(os.path.join(self.helm, "selected-lane"), "w") as fh:
            json.dump({"id": "t_aaaa01", "kind": "task", "title": "a", "at": 1000}, fh)
        for run, lane in (("wf_a", "t_aaaa01"), ("wf_b", "t_bbbb02")):
            self.add_journal("main", run, [started("k")], NOW - 30)
            self.add_run_record("main", run, {"status": "running",
                                              "summary": f"[{lane}] Check {run}"})
        self.run_sync()
        self.assertEqual((self.lane("wf_a")["parent"], self.lane("wf_a")["topic"]),
                         ("t_aaaa01", "api"))
        self.assertEqual((self.lane("wf_b")["parent"], self.lane("wf_b")["topic"]),
                         ("t_bbbb02", "hub"))
        self.assertEqual(self.lane("wf_b")["title"], "Check wf_b")

    def test_label_for_an_unknown_lane_falls_back(self):
        lanedb.set_meta(self.con, "main_session_id", "main")
        self.add_journal("main", "wf_x", [started("k")], NOW - 30)
        self.add_run_record("main", "wf_x", {"status": "running", "summary": "[t_ffff00] Go"})
        self.run_sync()
        self.assertEqual(self.lane("wf_x")["parent"], "chat")

    def test_split_lane_label(self):
        self.assertEqual(sync.split_lane_label("[cc_91f2fc72] Draft note"),
                         ("cc_91f2fc72", "Draft note"))
        self.assertEqual(sync.split_lane_label("Plain title"), (None, "Plain title"))
        self.assertEqual(sync.split_lane_label("[not a lane] x"), (None, "[not a lane] x"))

    def test_held_task_status_survives_sync(self):
        self.make_task("t_3")
        self.con.execute("UPDATE lanes SET status='done' WHERE id='t_3'")
        self.run_sync()
        self.assertEqual(self.lane("t_3")["status"], "done")

    def test_malformed_journal_does_not_break_sync(self):
        self.set_rows([row("cc_1")])
        self.add_journal("s", "wf_bad", [
            '{"type":"started","key":["a","b"],"phase":"x"}',
            '{"type":"started","key":"k","phase":["P"]}',
        ], NOW - 10)
        self.run_sync()
        self.assertIsNotNone(self.lane("cc_1"))

    def test_chat_parent_via_meta(self):
        lanedb.set_meta(self.con, "main_session_id", "mainsess")
        self.add_journal("mainsess", "wf_c", [started("k")], NOW - 10)
        self.run_sync()
        self.assertEqual(self.lane("wf_c")["parent"], "chat")
        self.assertEqual(self.lane("wf_c")["title"], "wf_c")

    def test_unknown_session_has_no_parent(self):
        self.add_journal("other", "wf_o", [started("k")], NOW - 10)
        self.run_sync()
        self.assertIsNone(self.lane("wf_o")["parent"])

    def test_stalled_after_twenty_quiet_minutes(self):
        self.add_journal("s", "wf_s", [started("k")], NOW - 25 * 60)
        self.run_sync()
        self.assertEqual(self.lane("wf_s")["status"], "stalled")

    def test_quiet_but_under_twenty_minutes_still_working(self):
        self.add_journal("s", "wf_w", [started("k")], NOW - 10 * 60)
        self.run_sync()
        self.assertEqual(self.lane("wf_w")["status"], "working")

    def test_finished_workflow_is_done_with_clipped_result(self):
        self.add_journal("s", "wf_d", [
            started("k"), result("k", "R" * 5000)], NOW - 300)
        self.run_sync()
        wf = self.lane("wf_d")
        self.assertEqual(wf["status"], "done")
        done = [e for e in wf["events"] if e["kind"] == "done"][0]
        self.assertEqual(len(done["detail"]), 200)
        self.run_sync(NOW + 60)
        self.assertEqual(
            len([e for e in self.lane("wf_d")["events"] if e["kind"] == "done"]), 1)

    def test_just_finished_is_still_working(self):
        self.add_journal("s", "wf_j", [started("k"), result("k")], NOW - 30)
        self.run_sync()
        self.assertEqual(self.lane("wf_j")["status"], "working")

    def test_malformed_lines_are_skipped(self):
        self.add_journal("s", "wf_m", [
            "{not json", started("k"), "[1,2]", "", result("k"), '{"type":'],
            NOW - 300)
        self.run_sync()
        self.assertEqual(self.lane("wf_m")["status"], "done")

    def test_old_journals_ignored(self):
        self.add_journal("s", "wf_old", [started("k")], NOW - 15 * DAY)
        self.run_sync()
        self.assertIsNone(self.lane("wf_old"))

    def test_unreadable_journal_does_not_break_sync(self):
        path = self.add_journal("s", "wf_x", [started("k")], NOW - 10)
        self.add_journal("s", "wf_y", [started("k")], NOW - 10)
        os.chmod(path, 0)
        try:
            self.run_sync()
        finally:
            os.chmod(path, 0o644)
        self.assertIsNotNone(self.lane("wf_y"))

    def test_resync_does_not_duplicate_events(self):
        self.add_journal("s", "wf_i", [started("k"), result("k")], NOW - 300)
        self.run_sync()
        count = len(self.lane("wf_i")["events"])
        self.run_sync(NOW + 5)
        self.assertEqual(len(self.lane("wf_i")["events"]), count)


class CliTests(Fixture):
    def cli(self, *args, check=True):
        env = dict(os.environ)
        proc = subprocess.run([sys.executable, LANE_BIN, *args], env=env,
                              capture_output=True, text=True)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def seed(self):
        self.set_rows([
            row("cc_1", state="answer", question="Ship it?", label="billing",
                topic="hub", recap="did a thing"),
            row("cc_2", label="old", updated_at=1),
            row("cc_3", label="live", topic="hub", recap="r"),
        ])
        self.cli("sync")

    def test_archive_fails_when_host_fails(self):
        self.set_rows([row("cc_1")])
        self.cli("sync")
        os.environ["CC_HOST"] = "/nonexistent"
        proc = subprocess.run([sys.executable, LANE_BIN, "archive", "cc_1"],
                              env=dict(os.environ), capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(lanedb.lane(self.con, "cc_1")["archived_at"])

    def test_sync_prints_summary(self):
        self.set_rows([row("cc_1", state="answer", question="Q")])
        out = self.cli("sync").stdout.strip()
        self.assertRegex(out, r"^2 lanes · 1 need you · 0 stale$")

    def test_list_groups_and_json(self):
        self.seed()
        text = self.cli("list").stdout
        self.assertLess(text.index("chat"), text.index("Needs you"))
        self.assertLess(text.index("Needs you"), text.index("hub"))
        self.assertIn("Stale · 1", text)
        data = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual({d["id"] for d in data},
                         {"chat", "cc_1", "cc_2", "cc_3"})

    def test_show_is_short_and_has_sections(self):
        self.seed()
        out = self.cli("show", "cc_1").stdout.splitlines()
        self.assertLessEqual(len(out), 25)
        self.assertIn("billing", out[0])
        self.assertIn("needs you", out[0])
        self.assertIn("topic: hub", out[0])
        self.assertTrue(any(l.startswith("NOW: Ship it?") for l in out))
        self.assertTrue(any(l.startswith("SINCE YOU LOOKED") for l in out))

    def test_show_uses_registry_last_seen(self):
        self.seed()
        threads = os.path.join(self.helm, "threads")
        os.makedirs(threads)
        with open(os.path.join(threads, "cc_1.json"), "w") as fh:
            json.dump({"last_seen": NOW + 10 ** 9}, fh)
        out = self.cli("show", "cc_1").stdout
        self.assertIn("nothing new", out)

    def test_show_unknown_lane_fails(self):
        self.seed()
        proc = self.cli("show", "nope", check=False)
        self.assertEqual(proc.returncode, 1)

    def test_digest_skips_chat_and_stale(self):
        self.seed()
        lines = self.cli("digest").stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[you] billing · hub"))
        self.assertFalse(any("old" in l for l in lines))

    def test_digest_is_capped(self):
        self.set_rows([row(f"cc_{i}", label=f"lane{i}", recap="x" * 400)
                       for i in range(60)])
        self.cli("sync")
        lines = self.cli("digest").stdout.splitlines()
        self.assertEqual(len(lines), 40)
        self.assertTrue(lines[-1].startswith("+"))
        self.assertTrue(all(len(l) <= 140 for l in lines))

    def test_log_and_validation(self):
        self.seed()
        self.cli("log", "cc_3", "hello", "--detail", "d")
        self.assertIn("hello", [e["title"] for e in self.lane("cc_3")["events"]])
        self.assertEqual(
            self.cli("log", "cc_3", "x", "--kind", "bogus", check=False).returncode, 1)
        self.assertEqual(self.cli("log", "zzz", "x", check=False).returncode, 1)

    def test_log_at_backdates_the_event(self):
        self.seed()
        self.cli("log", "cc_3", "iso", "--at", "2026-10-01T09:30")
        self.cli("log", "cc_3", "epoch", "--at", "1700000000")
        at = {e["title"]: e["at"] for e in self.lane("cc_3")["events"]}
        self.assertEqual(at["epoch"], 1700000000)
        self.assertEqual(at["iso"], int(__import__("datetime").datetime(
            2026, 10, 1, 9, 30).timestamp()))
        self.assertEqual(
            self.cli("log", "cc_3", "x", "--at", "soon", check=False).returncode, 1)

    def test_ask_marks_needs_you_until_the_conversation_moves_on(self):
        self.seed()
        self.cli("ask", "cc_3", "Merge it as is?")
        self.assertEqual(self.lane("cc_3")["status"], "you")
        self.cli("sync")
        item = self.lane("cc_3")
        self.assertEqual((item["status"], item["status_detail"]),
                         ("you", "Merge it as is?"))
        self.assertIn("ask", [e["kind"] for e in item["events"]])
        self.set_rows([row("cc_3", label="live", topic="hub", recap="moved",
                           updated_at=NOW + 10)])
        self.cli("sync")
        self.assertEqual(self.lane("cc_3")["status"], "ready")

    def test_ask_rewords_a_conversation_already_asking(self):
        self.seed()
        self.assertEqual(self.lane("cc_1")["status_detail"], "Ship it?")
        self.cli("ask", "cc_1", "Billing export: ship now, or wait for QA?")
        self.cli("sync")
        item = self.lane("cc_1")
        self.assertEqual((item["status"], item["status_detail"]),
                         ("you", "Billing export: ship now, or wait for QA?"))

    def test_ask_clear_restores_derived_status(self):
        self.seed()
        self.cli("ask", "cc_3", "Q?")
        self.cli("ask", "cc_3", "--clear")
        self.assertEqual(self.lane("cc_3")["status"], "ready")
        self.cli("sync")
        self.assertEqual(self.lane("cc_3")["status"], "ready")

    def test_ask_does_not_hide_running_work(self):
        self.seed()
        self.cli("ask", "cc_3", "Q?")
        self.set_rows([row("cc_3", label="live", topic="hub", state="running")])
        self.cli("sync")
        self.assertEqual(self.lane("cc_3")["status"], "working")

    def test_ask_needs_a_question_or_clear(self):
        self.seed()
        self.assertEqual(self.cli("ask", "cc_3", check=False).returncode, 1)

    def test_brief_sets_goal_and_recap_and_show_prints_them(self):
        self.seed()
        self.cli("brief", "cc_3", "--goal", "Fix export deadlines",
                 "--recap", "Bug found; note to Jordan pending.")
        out = self.cli("show", "cc_3").stdout
        self.assertIn("TASK: Fix export deadlines", out)
        self.assertIn("WHERE IT STANDS: Bug found; note to Jordan pending.", out)
        self.assertEqual(self.cli("brief", "cc_3", check=False).returncode, 1)

    def test_set_goal_survives_sync_and_recap_yields_to_newer_turn(self):
        self.set_rows([row("cc_3", label="live", recap="r", updated_at=1000)])
        self.cli("sync")
        self.cli("brief", "cc_3", "--goal", "G", "--recap", "mine")
        self.cli("sync")
        self.assertEqual((self.lane("cc_3")["goal"], self.lane("cc_3")["recap"]),
                         ("G", "mine"))
        self.set_rows([row("cc_3", label="live", topic="hub", recap="newer",
                           updated_at=NOW + 10**8)])
        self.cli("sync")
        self.assertEqual((self.lane("cc_3")["goal"], self.lane("cc_3")["recap"]),
                         ("G", "newer"))

    def test_goal_comes_from_session_index_unless_set(self):
        with open(os.path.join(self.index, "s1.json"), "w") as fh:
            json.dump({"goal": "Ship billing export"}, fh)
        self.set_rows([row("cc_1", session_id="s1", recap="r")])
        self.cli("sync")
        self.assertEqual(self.lane("cc_1")["goal"], "Ship billing export")
        self.assertEqual(self.lane("cc_1")["recap"], "r")

    def new_task(self, *args):
        return self.cli("new", *args).stdout.strip()

    def test_new_makes_a_selected_task_lane_that_sync_keeps(self):
        self.seed()
        lane_id = self.new_task("Invoice export fix", "--goal", "Sam wants API",
                                "--topic", "API")
        self.assertTrue(lane_id.startswith("t_"))
        item = self.lane(lane_id)
        self.assertEqual((item["kind"], item["topic"], item["goal"], item["status"]),
                         ("task", "api", "Sam wants API", "ready"))
        self.assertEqual(lanedb.read_selection()["id"], lane_id)
        self.cli("sync")
        item = self.lane(lane_id)
        self.assertEqual((item["status"], item["archived_at"]), ("ready", 0))
        self.assertIn("TASK: Sam wants API", self.cli("show", lane_id).stdout)

    def test_task_ask_done_and_clear(self):
        self.seed()
        lane_id = self.new_task("x")
        self.cli("ask", lane_id, "Which org?")
        self.cli("sync")
        self.assertEqual(self.lane(lane_id)["status"], "you")
        self.cli("ask", lane_id, "--clear")
        self.assertEqual(self.lane(lane_id)["status"], "ready")
        self.cli("done", lane_id, "--note", "shipped")
        self.cli("sync")
        self.assertEqual(self.lane(lane_id)["status"], "done")
        self.assertEqual(self.cli("done", "cc_3", check=False).returncode, 1)

    def test_link_files_a_workflow_under_a_lane(self):
        self.seed()
        lane_id = self.new_task("x", "--topic", "hub", "--no-select")
        self.add_journal("other", "wf_l", [started("k")], int(__import__("time").time()))
        self.cli("sync")
        self.cli("link", "wf_l", lane_id)
        self.assertEqual((self.lane("wf_l")["parent"], self.lane("wf_l")["topic"]),
                         (lane_id, "hub"))
        self.assertEqual(self.cli("link", lane_id, "wf_l", check=False).returncode, 1)

    def test_resolved_dismisses_a_conversations_own_question(self):
        self.seed()
        self.assertEqual(self.lane("cc_1")["status"], "you")
        self.cli("ask", "cc_1", "--resolved", "moved to t_c45f2a")
        item = self.lane("cc_1")
        self.assertEqual((item["status"], item["status_detail"]), ("ready", "moved to t_c45f2a"))
        self.assertIn("answer", [e["kind"] for e in item["events"]])
        self.cli("sync")
        self.assertEqual(self.lane("cc_1")["status"], "ready")
        self.set_rows([row("cc_1", state="answer", question="New Q?", label="billing",
                           updated_at=NOW + 10**8)])
        self.cli("sync")
        self.assertEqual(self.lane("cc_1")["status"], "you")

    def test_resolved_on_a_task_ask(self):
        self.seed()
        lane_id = self.cli("new", "x").stdout.strip()
        self.cli("ask", lane_id, "Q?")
        self.cli("ask", lane_id, "--resolved", "yes")
        self.cli("sync")
        self.assertEqual(self.lane(lane_id)["status"], "ready")

    def test_topic_pins(self):
        self.seed()
        self.cli("topic", "cc_3", "mine")
        self.cli("sync")
        self.assertEqual(self.lane("cc_3")["topic"], "mine")

    def test_stale_lists_and_archive_stale_calls_host(self):
        self.seed()
        self.assertIn("cc_2", self.cli("stale").stdout)
        self.assertEqual(self.cli("archive", "--stale").stdout.strip(),
                         "archived 1")
        with open(self.archived_log) as fh:
            self.assertEqual(fh.read().split(), ["cc_2"])
        self.assertTrue(self.lane("cc_2")["archived_at"])

    def test_archive_workflow_skips_host(self):
        self.add_journal("s", "wf_a", [started("k")], int(__import__("time").time()))
        self.cli("sync")
        self.cli("archive", "wf_a")
        self.assertFalse(os.path.exists(self.archived_log))

    def test_archive_requires_target(self):
        self.seed()
        self.assertEqual(self.cli("archive", check=False).returncode, 1)

    def test_select_writes_file(self):
        self.seed()
        self.cli("select", "cc_1")
        sel = lanedb.read_selection()
        self.assertEqual((sel["id"], sel["kind"], sel["status"]),
                         ("cc_1", "conversation", "you"))

    def test_topics_delegates_to_topics_main(self):
        stub = os.path.join(self.tmp.name, "stubs")
        os.makedirs(stub)
        with open(os.path.join(stub, "topics.py"), "w") as fh:
            fh.write("import sys\ndef main(argv):\n"
                     "    print('topics', *argv)\n    return 3\n")
        # Stub shadows the real module only if it is first on the path, so run
        # the CLI against a copy of the lane script in an isolated directory.
        copy = os.path.join(stub, "lane")
        for name in ("lane", "lanedb.py", "sync.py"):
            with open(os.path.join(HERE, name)) as src, \
                    open(os.path.join(stub, name), "w") as dst:
                dst.write(src.read())
        proc = subprocess.run([sys.executable, copy, "topics", "suggest", "-x"],
                              env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("topics suggest -x", proc.stdout)

    def test_topics_missing_module_exits_one_line(self):
        stub = os.path.join(self.tmp.name, "bare")
        os.makedirs(stub)
        for name in ("lane", "lanedb.py", "sync.py"):
            with open(os.path.join(HERE, name)) as src, \
                    open(os.path.join(stub, name), "w") as dst:
                dst.write(src.read())
        proc = subprocess.run(
            [sys.executable, os.path.join(stub, "lane"), "topics", "suggest"],
            env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)


class CliPureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_lane_module()

    def test_age(self):
        self.assertEqual([self.cli.age(s) for s in (5, 60, 3600, 2 * 86400)],
                         ["now", "1m", "1h", "2d"])

    def test_status_cells(self):
        item = {"status": "person", "status_detail": "Alex",
                "last_activity": NOW}
        self.assertEqual(self.cli.status_cell(item, NOW), "waiting on Alex")
        item.update(status="you", last_activity=NOW - 23 * 60)
        self.assertEqual(self.cli.status_cell(item, NOW), "needs you · 23m")


if __name__ == "__main__":
    unittest.main()
