"""Tests for lanes-tui.py: pure view model first, then a smoke run on a private tmux server.

The smoke tests only ever talk to `tmux -L lanes-test` and kill only that server.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TUI_PATH = os.path.join(HERE, "lanes-tui.py")
SOCKET = "lanes-test"
NOW = 1_800_000_000

spec = importlib.util.spec_from_file_location("lanes_tui", TUI_PATH)
tui = importlib.util.module_from_spec(spec)
sys.modules["lanes_tui"] = tui
spec.loader.exec_module(tui)
import lanedb  # noqa: E402


def mk(id, **kw):
    base = {
        "id": id, "kind": "conversation", "title": id, "parent": None, "topic": None,
        "topic_source": None, "source": None, "created": NOW - 9999,
        "last_activity": NOW - 60, "status": "idle", "status_detail": "",
        "stale_since": None, "archived_at": 0, "trail": [],
    }
    base.update(kw)
    return base


def ev(kind, title="t", at=NOW - 100, detail=None):
    return {"kind": kind, "title": title, "at": at, "detail": detail}


class Grouping(unittest.TestCase):
    def setUp(self):
        self.lanes = [
            mk("chat", kind="chat", title="chat"),
            mk("a", topic="hub", status="idle", last_activity=NOW - 500),
            mk("b", topic="hub", status="working", last_activity=NOW - 900),
            mk("c", topic="docs", status="done", last_activity=NOW - 10),
            mk("d", topic="hub", status="you", last_activity=NOW - 3000),
            mk("e", topic="hub", status="you", last_activity=NOW - 4000),
            mk("s1", topic="old", status="idle", stale_since=NOW - 5, last_activity=NOW - 900000),
            mk("x", topic="hub", archived_at=NOW),
            mk("wf1", kind="workflow", parent="a", topic=None, status="working"),
        ]

    def keys(self, **kw):
        return [s.key for s in tui.build_sections(self.lanes, **kw)]

    def test_section_order_chat_needs_working_topics_stale(self):
        self.assertEqual(self.keys(), ["chat", "needs", "working", "topic:docs", "stale"])

    def test_needs_you_oldest_first(self):
        needs = tui.build_sections(self.lanes)[1]
        self.assertEqual([l["id"] for l in needs.lanes], ["e", "d"])

    def test_working_holds_running_lanes_with_workflows_nested_under_parents(self):
        secs = {s.key: s for s in tui.build_sections(self.lanes)}
        working = secs["working"].lanes
        self.assertEqual([l["id"] for l in working], ["a", "wf1", "b"])
        self.assertTrue(working[1].get("_child"))
        self.assertEqual(secs["working"].total, 2)

    def test_topic_group_keeps_status_order_and_nests_finished_workflows(self):
        lanes = [mk("a", topic="hub", status="idle", last_activity=NOW - 500),
                 mk("b", topic="hub", status="ready", last_activity=NOW - 900),
                 mk("wf1", kind="workflow", parent="a", status="done")]
        secs = {s.key: s for s in tui.build_sections(lanes)}
        self.assertEqual([l["id"] for l in secs["topic:hub"].lanes], ["b", "a", "wf1"])

    def test_old_finished_workflows_are_not_nested(self):
        lanes = [mk("a", topic="hub", status="ready", last_activity=NOW - 50),
                 mk("w_old", kind="workflow", parent="a", status="done", last_activity=NOW - 3 * 3600),
                 mk("w_new", kind="workflow", parent="a", status="done", last_activity=NOW - 600)]
        ids = tui.flat_ids(tui.build_sections(lanes, now=NOW))
        self.assertEqual(ids, ["a", "w_new"])

    def test_running_conversation_spins(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.environ["LANES_STATE_DIR"] = tmp
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR", None)
        lanes = [mk("cc_1", status="working"), mk("cc_2", status="ready")]
        self.assertEqual(tui.live_lanes(lanes, None, time.time()), {"cc_1"})

    def test_nested_row_draws_a_branch_and_no_topic_tag(self):
        row = mk("wf1", kind="workflow", title="Sweep", status="working", topic="hub", _child=True)
        head = tui.plain(tui.lane_lines(row, 60, NOW, False)[0])
        self.assertIn("└ wf Sweep", head)
        self.assertNotIn("· hub", head)
        top = tui.plain(tui.lane_lines(mk("b", status="working", topic="hub"), 60, NOW, False)[0])
        self.assertIn("· hub", top)

    def test_archived_hidden(self):
        self.assertNotIn("x", tui.flat_ids(tui.build_sections(self.lanes, show_stale=True)))

    def test_stale_collapsed_by_default_with_count(self):
        stale = tui.build_sections(self.lanes)[-1]
        self.assertTrue(stale.collapsed)
        self.assertEqual((stale.lanes, stale.total), ([], 1))
        self.assertNotIn("s1", tui.flat_ids(tui.build_sections(self.lanes)))

    def test_stale_expands(self):
        self.assertIn("s1", tui.flat_ids(tui.build_sections(self.lanes, show_stale=True)))

    def test_active_status_never_stale(self):
        lane = mk("w", status="working", stale_since=NOW)
        self.assertFalse(tui.is_stale(lane))

    def test_filter_matches_title_topic_status(self):
        got = tui.flat_ids(tui.build_sections(self.lanes, query="DOCS"))
        self.assertEqual(got, ["c"])
        got = tui.flat_ids(tui.build_sections(self.lanes, query="needs"))
        self.assertEqual(got, [])
        got = tui.flat_ids(tui.build_sections(self.lanes, query="you hub"))
        self.assertEqual(sorted(got), ["d", "e"])

    def test_filter_reveals_stale_matches(self):
        self.assertIn("s1", tui.flat_ids(tui.build_sections(self.lanes, query="old")))

    def test_no_topic_goes_to_unsorted(self):
        secs = tui.build_sections([mk("z")])
        self.assertEqual(secs[0].title, tui.MISC_TOPIC)


class Selection(unittest.TestCase):
    def test_reselect_keeps_id(self):
        self.assertEqual(tui.reselect(["a", "b", "c"], "b", ["c", "b", "a"]), "b")

    def test_reselect_takes_neighbour_when_lane_gone(self):
        self.assertEqual(tui.reselect(["a", "b", "c"], "b", ["a", "c"]), "c")
        self.assertEqual(tui.reselect(["a", "b", "c"], "c", ["a", "b"]), "b")

    def test_reselect_empty(self):
        self.assertIsNone(tui.reselect(["a"], "a", []))

    def test_step_clamps(self):
        self.assertEqual(tui.step_selection(["a", "b"], "b", 1), "b")
        self.assertEqual(tui.step_selection(["a", "b"], "a", -1), "a")
        self.assertEqual(tui.step_selection(["a", "b"], None, 1), "a")

    def test_next_needing_wraps(self):
        by_id = {"a": mk("a", status="you"), "b": mk("b"), "c": mk("c", status="you")}
        self.assertEqual(tui.next_needing(["a", "b", "c"], by_id, "a"), "c")
        self.assertEqual(tui.next_needing(["a", "b", "c"], by_id, "c"), "a")
        self.assertIsNone(tui.next_needing(["b"], by_id, "b"))


class Glyphs(unittest.TestCase):
    def test_status_cells(self):
        cases = {
            ("you", ""): "[◇ needs you · 23m]",
            ("working", "Build"): "[▸ working · Build]",
            ("person", "Alex"): "[◷ waiting on Alex]",
            ("review", "repo#2231"): "[◎ review repo#2231]",
            ("ci", ""): "[⇡ ci]",
            ("ready", ""): "[› ready]",
            ("done", ""): "[✓ done]",
            ("stalled", ""): "[! stalled]",
        }
        for (status, detail), want in cases.items():
            lane = mk("a", status=status, status_detail=detail, last_activity=NOW - 23 * 60)
            self.assertEqual(tui.status_cell(lane, NOW)[0], want, status)

    def test_unknown_status_reads_idle(self):
        self.assertEqual(tui.status_cell(mk("a", status="??"), NOW)[0], "[· idle]")

    def test_trail_glyphs_joined(self):
        trail = [ev("checkpoint"), ev("answer"), ev("ask"), ev("block"), ev("pr"), ev("phase")]
        line = tui.trail_line(mk("a", status="idle", trail=trail), 60, NOW, indent=0)
        self.assertEqual(tui.plain(line), "●─◆─◇─⊘─⑂─▪─ [· idle]")

    def test_trail_keeps_cell_when_narrow(self):
        trail = [ev("checkpoint")] * 8
        line = tui.plain(tui.trail_line(mk("a", status="ci", trail=trail), 22, NOW, indent=0))
        self.assertTrue(line.endswith("[⇡ ci]"))
        self.assertLessEqual(tui.text_width(line), 22)

    def test_trail_marks_stop(self):
        trail = [ev("checkpoint"), ev("ask")]
        line = tui.trail_line(mk("a", trail=trail), 40, NOW, stop_pos=1, indent=0)
        marked = [s for t, s in line if t == "◇"]
        self.assertTrue(marked[0].endswith("+rev"))

    def test_only_last_eight_events(self):
        trail = [ev("checkpoint")] * 12
        line = tui.plain(tui.trail_line(mk("a", trail=trail), 80, NOW, indent=0))
        self.assertEqual(line.count("●"), 8)


class LaneLines(unittest.TestCase):
    def test_two_lines_when_not_selected(self):
        lines = tui.lane_lines(mk("a", title="billing export"), 50, NOW, False)
        self.assertEqual(len(lines), 2)
        self.assertIn("billing export", tui.plain(lines[0]))
        self.assertTrue(tui.plain(lines[0]).rstrip().endswith("1m"))

    def test_selected_expands_to_six_stops(self):
        events = [ev("checkpoint", "stop %d" % i, at=NOW - i * 60) for i in range(9)]
        lines = tui.lane_lines(mk("a"), 50, NOW, True, events)
        self.assertEqual(len(lines), 2 + 6)
        self.assertIn("stop 0", tui.plain(lines[2]))

    def test_lines_never_exceed_width(self):
        trail = [ev("checkpoint")] * 8
        lane = mk("a", title="x" * 90, status="review", status_detail="repo#2231", trail=trail)
        for width in (24, 30, 44, 80):
            for line in tui.lane_lines(lane, width, NOW, True, [ev("ask", "y" * 99)]):
                self.assertLessEqual(tui.text_width(tui.plain(line)), width)

    def test_second_line_is_summary_by_default_and_trail_on_toggle(self):
        lane = mk("a", status="you", status_detail="Merge it as is?", topic="web",
                  trail=[ev("checkpoint"), ev("ask")])
        summary = tui.plain(tui.lane_lines(lane, 70, NOW, False)[1])
        self.assertEqual(summary.lstrip("▏ "), "◇ Merge it as is?")
        self.assertNotIn("●", summary)
        trail = tui.plain(tui.lane_lines(lane, 70, NOW, False, trails=True)[1])
        self.assertIn("●", trail)

    def test_summary_falls_back_to_recap_then_goal(self):
        self.assertEqual(tui.summary_text(mk("a", status="ready", recap="R", goal="G")), "R")
        self.assertEqual(tui.summary_text(mk("a", status="ready", goal="G")), "G")
        self.assertEqual(tui.summary_text(mk("w", kind="workflow", recap="R")), "")

    def test_needs_you_row_names_its_topic(self):
        head = tui.plain(tui.lane_lines(mk("a", status="you", topic="web"), 60, NOW, False)[0])
        self.assertIn("· web", head)
        other = tui.plain(tui.lane_lines(mk("b", status="ready", topic="web"), 60, NOW, False)[0])
        self.assertNotIn("web", other)

    def test_card_shows_task_and_where_it_stands_above_the_ask(self):
        lane = mk("a", status="you", status_detail="Ship?", goal="Fix export deadlines",
                  recap="Bug found; note pending.")
        text = [tui.plain(l) for l in tui.card_lines(lane, {"events": []}, 60, NOW)]
        joined = "\n".join(text)
        self.assertLess(joined.index("TASK"), joined.index("WHERE IT STANDS"))
        self.assertLess(joined.index("WHERE IT STANDS"), joined.index("NEEDS YOU"))
        self.assertIn("Fix export deadlines", joined)

    def test_topics_on_screen_never_share_a_colour(self):
        names = ["hub", "api", "web", "mobile", "helm", "reports", "onboarding",
                 "code-review", "unsorted", "personal"]
        tui.assign_topic_colors(names)
        self.addCleanup(tui.assign_topic_colors, [])
        self.assertEqual(len({tui.topic_style(n) for n in names}), len(names))
        before = tui.topic_style("api")
        tui.assign_topic_colors(names)
        self.assertEqual(tui.topic_style("api"), before)

    def test_row_marker_takes_its_topic_colour(self):
        line = tui.lane_lines(mk("a", topic="api"), 50, NOW, False)[0]
        self.assertEqual(line[0], ("▏", tui.topic_style("api")))

    def test_help_has_a_legend(self):
        self.assertTrue(any("◇ needs you" in l for l in tui.HELP_LINES))
        self.assertTrue(any("● checkpoint" in l for l in tui.HELP_LINES))

    def test_workflow_and_chat_names(self):
        self.assertTrue(tui.lane_name(mk("w", kind="workflow", title="Sweep")).startswith("wf "))
        self.assertIn("chat", tui.lane_name(mk("chat", kind="chat")))


class Layout(unittest.TestCase):
    def test_scroll_keeps_selected_visible(self):
        lanes = [mk("l%02d" % i, topic="t", last_activity=NOW - i) for i in range(30)]
        secs = tui.build_sections(lanes)
        lay = tui.layout_list(secs, 40, NOW, "l25")
        top = tui.scroll_to_show(lay, "l25", 0, 20)
        self.assertLessEqual(top, lay.first_line["l25"])
        self.assertGreater(top + 20, lay.last_line["l25"])
        top = tui.scroll_to_show(lay, "l00", top, 20)
        self.assertEqual(top, 0 if lay.first_line["l00"] <= 1 else top)

    def test_one_column_over_the_footer(self):
        regions = tui.split_layout(120, 40)
        self.assertEqual(regions["list"], (0, 0, 39, 120))
        self.assertEqual(regions["foot"], (39, 0, 1, 120))
        self.assertNotIn("card", regions)

    def test_selected_lane_opens_its_context_in_place(self):
        lane = mk("a", status="you", status_detail="Ship it?", goal="Fix export deadlines",
                  recap="Bug found.", topic="web")
        other = mk("b", status="ready", topic="web")
        secs = tui.build_sections([lane, other])
        ctx = tui.card_lines(lane, {"events": []}, 56, NOW, header=False)
        lay = tui.layout_list(secs, 60, NOW, "a", expanded=ctx)
        text = [tui.plain(l) for l in lay.lines]
        joined = "\n".join(text)
        self.assertIn("TASK", joined)
        self.assertIn("Fix export deadlines", joined)
        self.assertIn("Ship it?", joined)
        self.assertEqual(sum("a · web" in t for t in text), 1)
        self.assertLess(lay.first_line["a"], lay.first_line["b"])
        self.assertTrue(all(tui.text_width(t) <= 60 for t in text))

    def test_tall_open_lane_scrolls_to_its_name(self):
        lane = mk("a")
        ctx = [[("x", "plain")]] * 50
        lay = tui.layout_list(tui.build_sections([mk("z" + str(i)) for i in range(5)] + [lane]),
                              60, NOW, "a", expanded=ctx)
        self.assertEqual(tui.scroll_to_show(lay, "a", 0, 20), lay.first_line["a"])

    def test_card_without_header_skips_title_row(self):
        lane = mk("a", title="billing", goal="G")
        text = [tui.plain(l) for l in tui.card_lines(lane, {"events": []}, 50, NOW, header=False)]
        self.assertFalse(any("billing" in t for t in text))
        self.assertIn("TASK", text[0])

    def test_needs_header_label(self):
        secs = tui.build_sections([mk("a", status="you")])
        lay = tui.layout_list(secs, 40, NOW, "a")
        self.assertIn("Needs you · 1", tui.plain(lay.lines[0]))


class Card(unittest.TestCase):
    def card(self, lane, events=(), **kw):
        out = tui.card_lines(lane, {"events": list(events)}, kw.pop("width", 50), NOW, **kw)
        return [tui.plain(l) for l in out]

    def test_needs_you_shows_question(self):
        lane = mk("a", status="you", status_detail="Draft the ask to IT?")
        text = "\n".join(self.card(lane))
        self.assertIn("NEEDS YOU", text)
        self.assertIn("Draft the ask to IT?", text)

    def test_blocked_shows_person(self):
        lane = mk("a", status="person", status_detail="Alex")
        text = "\n".join(self.card(lane))
        self.assertIn("waiting on: Alex", text)

    def test_pr_labels_listed(self):
        lane = mk("a", status="review", status_detail="repo#2231")
        text = "\n".join(self.card(lane, [ev("pr", "repo#2231 open")]))
        self.assertIn("PRs:", text)

    def test_since_you_looked_filters_by_last_seen(self):
        events = [ev("checkpoint", "new", at=NOW - 10), ev("checkpoint", "old", at=NOW - 5000)]
        text = "\n".join(self.card(mk("a"), events, last_seen=NOW - 1000))
        self.assertIn("SINCE YOU LOOKED", text)
        self.assertIn("new", text)
        self.assertNotIn("old", text)

    def test_nothing_new(self):
        text = "\n".join(self.card(mk("a"), [ev("checkpoint", at=NOW - 5000)], last_seen=NOW))
        self.assertIn("nothing new", text)

    def test_workflow_shows_phase_and_counts(self):
        lane = mk("w", kind="workflow", status="working", status_detail="Build")
        text = "\n".join(
            tui.plain(l)
            for l in tui.card_lines(lane, {"events": [ev("phase", "Build")]}, 50, NOW, counts=(4, 2))
        )
        self.assertIn("phase: Build", text)
        self.assertIn("agents: 4 started · 2 finished", text)
        self.assertIn("PHASES", text)

    def test_stop_box_when_moved(self):
        events = [ev("checkpoint", "latest"), ev("ask", "older question", detail="why?")]
        text = "\n".join(self.card(mk("a"), events, stop_idx=1))
        self.assertIn("STOP", text)
        self.assertIn("older question", text)

    def test_recap_replaces_the_now_box_when_nothing_is_asked(self):
        lane = mk("t", kind="task", status="ready", recap="Merge held until colors land.")
        text = "\n".join(self.card(lane, [ev("checkpoint", "started: x")]))
        self.assertIn("Merge held until colors land.", text)
        self.assertNotIn("NOW", text)
        self.assertNotIn("started: x", text.split("RECENT")[0])

    def test_now_skips_the_opening_started_line(self):
        events = [ev("checkpoint", "started: x", at=NOW - 10), ev("checkpoint", "real step", at=NOW - 50)]
        text = "\n".join(self.card(mk("t", kind="task", status="ready"), events))
        self.assertIn("real step", text.split("RECENT")[0])

    def test_wraps_within_width(self):
        lane = mk("a", status="you", status_detail="word " * 80)
        for width in (30, 50, 90):
            for line in self.card(lane, width=width):
                self.assertLessEqual(tui.text_width(line), width)

    def test_empty_selection(self):
        self.assertEqual(tui.plain(tui.card_lines(None, None, 40, NOW)[0]).strip(), "nothing selected")


class Spinner(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.environ["LANES_STATE_DIR"] = self.dir
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR", None)
        self.addCleanup(tui._spinning.clear)

    def journal(self, age, agent_age=None):
        run = tempfile.mkdtemp(dir=self.dir)
        path = os.path.join(run, "journal.jsonl")
        for name, a in (("journal.jsonl", age), ("agent-x.jsonl", agent_age)):
            if a is None:
                continue
            open(os.path.join(run, name), "w").close()
            t = time.time() - a
            os.utime(os.path.join(run, name), (t, t))
        return path

    def test_working_workflow_and_its_lane_spin_stalled_and_done_do_not(self):
        lanes = [mk("t1", kind="task"),
                 mk("wf_a", kind="workflow", status="working", parent="t1"),
                 mk("wf_b", kind="workflow", status="stalled"),
                 mk("wf_c", kind="workflow", status="done")]
        self.assertEqual(tui.live_lanes(lanes, None, time.time()), {"wf_a", "t1"})

    def test_busy_main_chat_spins_the_selected_lane_until_its_transcript_goes_quiet(self):
        busy = os.path.join(self.dir, "main-busy")
        with open(busy, "w") as fh:
            json.dump({"transcript": self.journal(3)}, fh)
        self.assertEqual(tui.live_lanes([], {"id": "cc_1"}, time.time()), {"chat", "cc_1"})
        with open(busy, "w") as fh:
            json.dump({"transcript": self.journal(300)}, fh)
        self.assertEqual(tui.live_lanes([], {"id": "cc_1"}, time.time()), set())
        os.remove(busy)
        self.assertEqual(tui.live_lanes([], {"id": "cc_1"}, time.time()), set())

    def test_spinning_cell_animates_and_reads_working(self):
        tui._spinning.add("a")
        text, _ = tui.status_cell(mk("a", status="ready"), NOW)
        self.assertIn(text[1], tui.SPINNER)
        self.assertIn("working", text)
        still, _ = tui.status_cell(mk("b", status="ready"), NOW)
        self.assertEqual(still, "[› ready]")


class EventText(unittest.TestCase):
    def test_clipped_title_is_replaced_by_full_detail(self):
        ev = {"title": "Ship the thing and then…", "detail": "Ship the thing and then verify it"}
        self.assertEqual(tui.event_text(ev), "Ship the thing and then verify it")

    def test_distinct_detail_is_appended(self):
        ev = {"title": "merged", "detail": "repo#1"}
        self.assertEqual(tui.event_text(ev), "merged\nrepo#1")

    def test_no_detail_is_title_only(self):
        self.assertEqual(tui.event_text({"title": "x"}), "x")


class JournalCounts(unittest.TestCase):
    def write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines))
        self.addCleanup(os.unlink, path)
        return path

    def test_counts_started_and_finished(self):
        path = self.write([
            '{"type":"launched"}',
            '{"type":"started","agentId":"1"}',
            '{"type":"started","agentId":"2"}',
            "not json",
            '{"type":"result","agentId":"1","result":"x"}',
        ])
        self.assertEqual(tui.journal_counts(path), (2, 1))

    def test_missing_or_odd_degrades(self):
        self.assertIsNone(tui.journal_counts("/nonexistent/j.jsonl"))
        self.assertIsNone(tui.journal_counts(None))
        self.assertEqual(tui.journal_counts(self.write(["[1,2]", "7"])), (0, 0))


class FakeTmux:
    def __init__(self, options=None, panes=("%1", "%2")):
        self.options = dict(options or {})
        self.panes = list(panes)
        self.calls = []

    def option(self, name):
        return self.options.get(name, "")

    def set_option(self, name, value):
        self.options[name] = value

    def pane_exists(self, pane):
        return pane in self.panes

    def set_pane_option(self, pane, name, value):
        self.calls.append(("pane_option", pane, name, value))

    def respawn(self, pane, command):
        self.calls.append(("respawn", pane, command))
        return True

    def focus(self, pane):
        self.calls.append(("focus", pane))

    def swap(self, src, dst):
        self.calls.append(("swap", src, dst))
        return True

    def new_hidden(self, beside, command):
        self.calls.append(("new_hidden", beside, command))
        self.panes.append("%7")
        return "%7"


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.environ["LANES_STATE_DIR"] = self.dir
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR", None)
        self.tmux = FakeTmux({"@lanes_chat_pane": "%1"})
        self.ctl = tui.Controller(self.tmux, host="/bin/true")

    def test_selection_written_before_pane_change(self):
        lane = mk("cc_1", title="Billing")
        self.ctl.activate(lane)
        self.assertEqual(lanedb.read_selection()["id"], "cc_1")

    def test_enter_on_a_conversation_stays_in_the_main_chat(self):
        msg = self.ctl.activate(mk("cc_1", title="Billing"))
        self.assertEqual(self.tmux.calls, [("focus", "%1")])
        self.assertEqual(lanedb.read_selection()["id"], "cc_1")
        self.assertEqual(msg, "chat · about Billing")
        self.assertNotIn("@lanes_staged", {k for k, v in self.tmux.options.items() if v})

    def test_chat_chip_names_the_lane_in_its_topic_colour(self):
        self.ctl.paint_chat(mk("t_1", kind="task", title="Invoice export fix", topic="api"))
        self.assertEqual(self.tmux.calls, [
            ("pane_option", "%1", "@lane_label", "Invoice export fix · api"),
            ("pane_option", "%1", "@lane_color", tui.topic_colour("api")),
        ])
        self.tmux.calls.clear()
        self.ctl.paint_chat(mk("chat", kind="chat"))
        self.assertEqual([c[3] for c in self.tmux.calls], ["", ""])

    def test_topic_colour_matches_the_list_palette(self):
        slot = int(tui.topic_style("api")[len("topic"):])
        self.assertEqual(tui.topic_colour("api"), "colour%d" % tui.TOPIC_256[slot])

    def test_first_stage_hides_the_chat_instead_of_killing_it(self):
        self.ctl.open_original(mk("cc_1"))
        kinds = [c[0] for c in self.tmux.calls]
        self.assertEqual(kinds, ["new_hidden", "swap", "focus"])
        self.assertIn("cc_1", self.tmux.calls[0][2])
        self.assertEqual(self.tmux.calls[1], ("swap", "%7", "%1"))
        self.assertNotIn("respawn", kinds)
        self.assertEqual(self.tmux.options["@lanes_staged"], "cc_1")
        self.assertEqual(self.tmux.options["@lanes_stage_pane"], "%7")

    def test_restaging_same_conversation_only_focuses(self):
        self.ctl.open_original(mk("cc_1"))
        self.tmux.calls.clear()
        self.ctl.open_original(mk("cc_1"))
        self.assertEqual(self.tmux.calls, [("focus", "%7")])

    def test_switching_conversations_reuses_the_stage_pane(self):
        self.ctl.open_original(mk("cc_1"))
        self.tmux.calls.clear()
        self.ctl.open_original(mk("cc_2"))
        self.assertEqual([c[0] for c in self.tmux.calls], ["respawn", "focus"])
        self.assertEqual(self.tmux.calls[0][1], "%7")
        self.assertEqual(self.tmux.options["@lanes_staged"], "cc_2")

    def test_workflow_swaps_main_chat_back(self):
        self.ctl.open_original(mk("cc_1"))
        self.tmux.calls.clear()
        self.ctl.open_original(mk("wf_1", kind="workflow"))
        self.assertEqual(self.tmux.calls, [("swap", "%1", "%7"), ("focus", "%1")])
        self.assertEqual(self.tmux.options["@lanes_staged"], "")

    def test_chat_when_nothing_staged_only_focuses(self):
        self.ctl.activate(mk("chat", kind="chat"))
        self.assertEqual(self.tmux.calls, [("focus", "%1")])

    def test_missing_chat_pane_reports(self):
        ctl = tui.Controller(FakeTmux({"@lanes_chat_pane": "%9"}), host="/bin/true")
        self.assertIn("helm lanes", ctl.activate(mk("cc_1")))


# --------------------------------------------------------------- app + smoke


def make_fixture(root):
    """A db with every status, a stale lane, and a workflow; plus a fake cc-host."""
    os.makedirs(os.path.join(root, "threads"), exist_ok=True)
    db = os.path.join(root, "helm.db")
    con = lanedb.connect(db)
    now = int(time.time())
    lanedb.upsert_lane(con, id="chat", kind="chat", title="chat", status="idle",
                       created=now, last_activity=now)
    rows = [
        ("cc_ask", "billing export", "hub", "you", "Want me to draft the ask to IT?", 1500),
        ("cc_work", "report render", "hub", "working", "Build", 120),
        ("cc_wait", "vendor onboarding", "hr", "person", "Alex", 3000),
        ("cc_pr", "billing fix", "hub", "review", "repo#2231", 4000),
        ("cc_old", "ancient thing", "misc", "idle", "", 90 * 86400),
    ]
    for lid, title, topic, status, detail, age in rows:
        lanedb.upsert_lane(
            con, id=lid, kind="conversation", title=title, topic=topic,
            topic_source="registry", status=status, status_detail=detail,
            created=now - age - 100, last_activity=now - age,
            stale_since=(now - 5) if lid == "cc_old" else None,
        )
        lanedb.add_event(con, lid, now - age - 50, "checkpoint", "started " + title)
        lanedb.add_event(con, lid, now - age, "ask" if status == "you" else "checkpoint",
                         "latest on " + title, "more detail here")
    lanedb.upsert_lane(con, id="wf_1", kind="workflow", title="Sweep workflow", parent="cc_work",
                       status="working", status_detail="Verify", created=now - 400,
                       last_activity=now - 30)
    lanedb.add_event(con, "wf_1", now - 30, "phase", "Verify")
    con.close()
    host = os.path.join(root, "fake-host.sh")
    log = os.path.join(root, "host.log")
    with open(host, "w") as fh:
        fh.write('#!/bin/sh\necho "$@" >> "%s"\n' % log)
    os.chmod(host, os.stat(host).st_mode | stat.S_IEXEC)
    return db, host, log


class AppTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.db, self.host, self.log = make_fixture(self.root)
        os.environ["LANES_STATE_DIR"] = self.root
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR", None)
        self.app = tui.App(self.db, tui.Controller(FakeTmux({"@lanes_chat_pane": "%1"}), host=self.host))

    def press(self, *keys):
        for k in keys:
            self.assertTrue(self.app.handle(k))

    def test_starts_on_chat_and_navigates(self):
        self.assertEqual(self.app.selected, "chat")
        self.press("j")
        self.assertEqual(self.app.selected, "cc_ask")
        self.press("k")
        self.assertEqual(self.app.selected, "chat")

    def pick(self, lane_id, at):
        with open(os.path.join(self.root, "selected-lane"), "w") as fh:
            json.dump({"id": lane_id, "kind": "conversation", "title": lane_id, "at": at}, fh)

    def test_jumps_to_a_lane_the_chat_selects(self):
        self.pick("cc_ask", int(time.time()) + 100)
        self.app.reload("")
        self.assertEqual(self.app.selected, "cc_ask")
        self.press("k")
        self.app.reload("")
        self.assertEqual(self.app.selected, "chat")

    def test_old_pick_on_disk_does_not_move_the_list(self):
        self.pick("cc_ask", 1)
        app = tui.App(self.db, tui.Controller(FakeTmux({"@lanes_chat_pane": "%1"}), host=self.host))
        app.reload("")
        self.assertEqual(app.selected, "chat")

    def test_esc_in_the_filter_clears_it(self):
        everyone = list(self.app.order)
        self.press("/", "b", "i", "l", "l")
        self.assertLess(len(self.app.order), len(everyone))
        self.press("\x1b")
        self.assertEqual((self.app.mode, self.app.query), ("normal", ""))
        self.assertEqual(self.app.order, everyone)

    def test_esc_after_a_submitted_filter_clears_it(self):
        everyone = list(self.app.order)
        self.press("/", "b", "i", "l", "l", "\n")
        self.assertEqual(self.app.query, "bill")
        self.press("\x1b")
        self.assertEqual(self.app.query, "")
        self.assertEqual(self.app.order, everyone)

    def test_n_jumps_to_needs_you(self):
        self.press("n")
        self.assertEqual(self.app.selected, "cc_ask")
        self.press("n")
        self.assertEqual(self.app.msg, "")
        self.assertEqual(self.app.selected, "cc_ask")

    def test_stale_toggle(self):
        self.assertNotIn("cc_old", self.app.order)
        self.press("s")
        self.assertIn("cc_old", self.app.order)

    def test_filter_prompt_narrows_and_escape_keeps(self):
        self.press("/", "w", "o", "r", "k")
        self.assertEqual(self.app.mode, "prompt")
        self.assertEqual(self.app.order, ["cc_work", "wf_1"])
        self.press("\n")
        self.assertEqual(self.app.mode, "normal")
        self.assertEqual(self.app.query, "work")
        self.press("/", "KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_BACKSPACE", "\n")
        self.assertIn("cc_ask", self.app.order)

    def test_topic_prompt_pins_and_completes(self):
        self.press("n", "t")
        self.assertEqual(self.app.buf, "hub")
        self.press("KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_BACKSPACE", "h", "\t")
        self.assertEqual(self.app.buf, "hr")
        self.press("\n")
        con = lanedb.connect(self.db)
        row = lanedb.lane(con, "cc_ask")
        con.close()
        self.assertEqual((row["topic"], row["topic_source"]), ("hr", "you"))

    def test_archive_lane_updates_db_and_host(self):
        self.press("n", "a")
        self.assertNotIn("cc_ask", self.app.order)
        con = lanedb.connect(self.db)
        self.assertEqual([l["id"] for l in lanedb.lanes(con) if l["id"] == "cc_ask"], [])
        con.close()
        self.assertIn("archive cc_ask", wait_log(self.log, "archive cc_ask"))

    def test_chat_cannot_be_archived(self):
        self.press("a")
        self.assertIn("cannot", self.app.msg)
        self.assertIn("chat", self.app.order)

    def test_archive_stale_needs_confirmation(self):
        self.press("A")
        self.assertEqual(self.app.mode, "confirm")
        self.press("n")
        self.assertIn("cc_old", [l["id"] for l in self.app.lanes])
        self.press("A", "y")
        self.assertNotIn("cc_old", [l["id"] for l in self.app.lanes])

    def test_selection_survives_reload_resort(self):
        self.press("j", "j")
        picked = self.app.selected
        con = lanedb.connect(self.db)
        lanedb.upsert_lane(con, id="cc_wait", kind="conversation", title="vendor onboarding", last_activity=int(time.time()) + 5)
        con.close()
        self.app.reload("")
        self.assertEqual(self.app.selected, picked)

    def test_stop_motion_clamped(self):
        self.press("n", "J", "J", "J")
        self.assertEqual(self.app.stop_idx, 1)
        self.press("K", "K")
        self.assertEqual(self.app.stop_idx, 0)

    def test_enter_writes_selection(self):
        self.press("n", "\n")
        self.assertEqual(lanedb.read_selection()["id"], "cc_ask")

    def test_quit(self):
        self.assertFalse(self.app.handle("q"))


def wait_log(path, needle, timeout=3):
    """Background host calls land asynchronously; poll the fake host's log."""
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as fh:
                text = fh.read()
            if needle in text:
                break
        time.sleep(0.05)
    return text


def tmux(*args, check=False):
    return subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True, check=check)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class Smoke(unittest.TestCase):
    """Runs the real curses app inside a private tmux server."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.db, self.host, self.log = make_fixture(self.root)
        os.environ["LANES_STATE_DIR"] = self.root
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR", None)
        tmux("kill-server")
        self.addCleanup(tmux, "kill-server")

    def start(self, cols=140, rows=40, chat_pane=False):
        env = (
            "env LANES_DB=%s LANES_STATE_DIR=%s CC_HOST=%s LANES_CLI=/nonexistent "
            "LANES_ATTACH_CMD='echo STAGED {id}; sleep 60' TERM=xterm-256color "
        ) % (self.db, self.root, self.host)
        cmd = "%s python3 %s" % (env, TUI_PATH)
        tmux("new-session", "-d", "-x", str(cols), "-y", str(rows), "-s", "t", "echo MAIN; sleep 600" if chat_pane else cmd)
        if chat_pane:
            chat = tmux("display-message", "-p", "-t", "t", "#{pane_id}").stdout.strip()
            tui_pane = tmux("split-window", "-h", "-d", "-P", "-F", "#{pane_id}", "-t", chat, cmd).stdout.strip()
            tmux("set-option", "-t", "t", "@lanes_chat_pane", chat)
            tmux("set-option", "-t", "t", "@lanes_tui_pane", tui_pane)
            self.chat, self.pane = chat, tui_pane
        else:
            self.pane = "t"
        self.wait_for("billing export")

    def screen(self, pane=None):
        return tmux("capture-pane", "-p", "-t", pane or self.pane).stdout

    def wait_for(self, text, pane=None, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if text in self.screen(pane):
                return
            time.sleep(0.1)
        self.fail("%r not on screen:\n%s" % (text, self.screen(pane)))

    def keys(self, *keys):
        tmux("send-keys", "-t", self.pane, *keys)
        time.sleep(0.35)

    def test_list_groups_and_rows(self):
        self.start()
        screen = self.screen()
        self.assertIn("Needs you · 1", screen)
        self.assertIn("hub · ", screen)
        self.assertIn("◇ Want me to draft the ask to IT?", screen)
        self.assertNotIn("[◇ needs you", screen)
        self.assertIn("[◷ waiting on Alex]", screen)
        self.assertNotIn("[◷ waiting on Alex] Alex", screen)
        self.assertIn("NEEDS YOU", screen.upper())
        self.assertIn("Stale · 1", screen)
        self.assertNotIn("ancient thing", screen)

    def test_navigation_and_stale_and_filter(self):
        self.start()
        self.keys("n")
        self.wait_for("Want me to draft the ask to IT?")
        self.keys("s")
        self.wait_for("ancient thing")
        self.keys("s")
        self.assertNotIn("ancient thing", self.screen())
        self.keys("/")
        self.keys("v", "e", "n", "d")
        self.assertIn("vendor onboarding", self.screen())
        self.assertNotIn("report render", self.screen())
        self.keys("Escape")
        self.keys("/")
        self.keys("BSpace", "BSpace", "BSpace", "BSpace")
        self.keys("Enter")
        self.wait_for("billing export")
        self.keys("?")
        self.wait_for("Lanes · keys")
        self.keys("x")
        self.wait_for("billing export")

    def test_stop_motion_shows_stop_box(self):
        self.start()
        self.keys("n", "J")
        self.wait_for("STOP")

    def test_narrow_layout_stacks_card_below(self):
        self.start(cols=80, rows=40)
        self.keys("n")
        self.wait_for("NEEDS YOU")
        rows = self.screen().splitlines()
        list_row = next(i for i, r in enumerate(rows) if "billing export" in r)
        card_row = next(i for i, r in enumerate(rows) if "NEEDS YOU" in r)
        self.assertLess(list_row, card_row)

    def test_quit_exits_cleanly(self):
        self.start()
        self.keys("q")
        deadline = time.time() + 3
        while time.time() < deadline and tmux("has-session", "-t", "t").returncode == 0:
            time.sleep(0.1)
        self.assertNotEqual(tmux("has-session", "-t", "t").returncode, 0)

    def test_enter_keeps_the_main_chat_and_selects_the_lane(self):
        self.start(chat_pane=True)
        visible = lambda: tmux("list-panes", "-t", self.pane, "-F", "#{pane_id}").stdout.split()
        self.keys("n", "Enter")
        time.sleep(0.3)
        self.assertIn(self.chat, visible())
        self.assertEqual(lanedb.read_selection()["id"], "cc_ask")
        self.assertEqual(tmux("show-options", "-qv", "-t", "t", "@lanes_staged").stdout.strip(), "")

    def test_o_stages_conversation_then_chat_restores(self):
        self.start(chat_pane=True)
        visible = lambda: tmux("list-panes", "-t", self.pane, "-F", "#{pane_id}").stdout.split()
        self.keys("n", "o")
        stage = ""
        deadline = time.time() + 5
        while not stage and time.time() < deadline:
            stage = tmux("show-options", "-qv", "-t", "t", "@lanes_stage_pane").stdout.strip()
            time.sleep(0.1)
        self.wait_for("STAGED cc_ask", pane=stage)
        self.assertIn(stage, visible())
        self.assertNotIn(self.chat, visible())
        self.assertEqual(lanedb.read_selection()["id"], "cc_ask")
        self.assertEqual(tmux("show-options", "-qv", "-t", "t", "@lanes_staged").stdout.strip(), "cc_ask")
        self.keys("c")
        time.sleep(0.3)
        self.assertIn(self.chat, visible())
        self.assertIn("MAIN", self.screen(self.chat))
        self.assertEqual(lanedb.read_selection()["id"], "chat")
        self.assertIn("seen cc_ask", wait_log(self.log, "seen cc_ask"))

if __name__ == "__main__":
    unittest.main()
