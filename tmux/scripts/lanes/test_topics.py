import json
import os
import tempfile
import unittest
from pathlib import Path

import topics


class SlugTest(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(topics.slugify("new: Onboarding!"), "onboarding")
        self.assertEqual(topics.slugify("  --PR_reviews-- "), "pr-reviews")

    def test_caps_length_without_trailing_hyphen(self):
        slug = topics.slugify("a" * 39 + " bbbb")
        self.assertEqual(slug, "a" * 39)

    def test_empty(self):
        self.assertEqual(topics.slugify("!!!"), "")


class ParseTest(unittest.TestCase):
    IDS = ["a", "b", "c"]

    def test_good_json(self):
        got = topics.parse_proposal('{"mapping": {"a": "Hub", "b": "new: pr reviews"}}', self.IDS)
        self.assertEqual(got, {"a": "hub", "b": "pr-reviews"})

    def test_fenced_json_accepted(self):
        got = topics.parse_proposal('```json\n{"mapping": {"a": "hub"}}\n```', self.IDS)
        self.assertEqual(got, {"a": "hub"})

    def test_unknown_ids_dropped(self):
        got = topics.parse_proposal('{"mapping": {"a": "hub", "zzz": "x"}}', self.IDS)
        self.assertEqual(got, {"a": "hub"})

    def test_prose_around_json_rejected(self):
        with self.assertRaises(topics.ProposalError):
            topics.parse_proposal('Sure! {"mapping": {"a": "hub"}}', self.IDS)

    def test_bad_shapes_rejected(self):
        for bad in ["", "[]", '{"mapping": []}', '{"mapping": {"a": 3}}',
                    '{"mapping": {"zzz": "x"}}', '{"other": 1}']:
            with self.subTest(bad=bad), self.assertRaises(topics.ProposalError):
                topics.parse_proposal(bad, self.IDS)


class PlanTest(unittest.TestCase):
    ROWS = [
        {"id": "a", "title": "A", "topic": "pr-review", "recap": ""},
        {"id": "b", "title": "B", "topic": "hub", "recap": ""},
        {"id": "c", "title": "C", "topic": "", "recap": ""},
    ]

    def test_unchanged_and_pinned_are_skipped(self):
        got = topics.changes(self.ROWS, {"a": "pr-reviews", "b": "hub", "c": "misc"}, {"c"})
        self.assertEqual([c["id"] for c in got], ["a"])

    def test_table_mentions_before_and_after(self):
        got = topics.changes(self.ROWS, {"a": "pr-reviews"}, set())
        table = topics.format_table(got)
        self.assertIn("pr-review -> pr-reviews", table)
        self.assertIn("1 lanes change across 1 topics", table)

    def test_empty_table(self):
        self.assertEqual(topics.format_table([]), "No topic changes proposed.")

    def test_conversation_rows_filter_and_truncate(self):
        lanes = [
            {"id": "x", "kind": "conversation", "title": "T", "topic": "t",
             "trail": [{"kind": "checkpoint", "title": "r" * 300}]},
            {"id": "w", "kind": "workflow", "title": "W"},
            {"id": "old", "kind": "conversation", "title": "O", "archived_at": 5},
        ]
        rows = topics.conversation_rows(lanes)
        self.assertEqual([r["id"] for r in rows], ["x"])
        self.assertEqual(len(rows[0]["recap"]), topics.RECAP_CHARS)

    def test_known_topics_merges_declared_and_used_without_dupes(self):
        got = topics.known_topics([{"topic": "Hub"}, {"topic": ""}, {"topic": "misc"}],
                                  ["hub", "docs"])
        self.assertEqual(got, ["hub", "docs", "misc"])

    def test_prompt_lists_every_row(self):
        prompt = topics.build_prompt(self.ROWS)
        for row in self.ROWS:
            self.assertIn(f'"id": "{row["id"]}"', prompt)


class FakeDbTest(unittest.TestCase):
    """suggest/apply against a stub lanedb, so they do not depend on its internals."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["LANES_STATE_DIR"] = tmp.name
        self.addCleanup(os.environ.pop, "LANES_STATE_DIR")
        self.set_calls = []
        self.lanes = [
            {"id": "a", "kind": "conversation", "title": "A", "topic": "old", "topic_source": "registry"},
            {"id": "b", "kind": "conversation", "title": "B", "topic": "mine", "topic_source": "you"},
        ]
        outer = self

        class Stub:
            connect = staticmethod(lambda: None)
            lanes = staticmethod(lambda con: outer.lanes)
            lane = staticmethod(lambda con, i: next((x for x in outer.lanes if x["id"] == i), None))

            @staticmethod
            def set_topic(con, lane_id, topic, source="you"):
                outer.set_calls.append((lane_id, topic, source))

        import sys
        self.saved = sys.modules.get("lanedb")
        sys.modules["lanedb"] = Stub
        self.addCleanup(self.restore)

    def restore(self):
        import sys
        if self.saved is None:
            sys.modules.pop("lanedb", None)
        else:
            sys.modules["lanedb"] = self.saved

    def test_suggest_saves_proposal_and_never_proposes_pinned(self):
        reply = '{"mapping": {"a": "hub", "b": "hub"}}'
        out = topics.suggest(model=lambda prompt: reply)
        self.assertIn("old -> hub", out)
        saved = json.loads(topics.proposal_path().read_text())
        self.assertEqual(saved, {"mapping": {"a": "hub"}})

    def test_suggest_bad_reply_leaves_no_proposal(self):
        with self.assertRaises(topics.ProposalError):
            topics.suggest(model=lambda prompt: "not json")
        self.assertFalse(topics.proposal_path().exists())

    def test_apply_marks_ai_and_skips_pinned(self):
        topics.proposal_path().write_text(json.dumps({"mapping": {"a": "hub", "b": "hub"}}))
        topics.apply()
        self.assertEqual(self.set_calls, [("a", "hub", "ai")])

    def test_apply_without_proposal_errors(self):
        with self.assertRaises(topics.ProposalError):
            topics.apply()


if __name__ == "__main__":
    unittest.main()
