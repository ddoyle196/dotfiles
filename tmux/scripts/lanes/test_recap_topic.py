import os
import subprocess
import unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[3] / "claude" / "hooks" / "cc-recap-gen.sh"


def parse(raw):
    proc = subprocess.run(
        ["bash", "-c", f'. "{GEN}"; topic_from_raw "$1"', "x", raw],
        capture_output=True, text=True, env=dict(os.environ, CC_RECAP_SOURCE_ONLY="1"), timeout=5,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TopicParseTest(unittest.TestCase):
    def test_existing_topic(self):
        self.assertEqual(parse("STATE: pickup\nRECAP: x\nWAITING_ON: none\nTOPIC: onboarding"),
                         "onboarding")

    def test_new_prefix_and_case(self):
        self.assertEqual(parse("TOPIC: new: Hub Billing"), "hub-billing")

    def test_decoration_stripped(self):
        self.assertEqual(parse("  TOPIC:  `Release_Notes`. "), "release-notes")

    def test_missing_line_is_empty(self):
        self.assertEqual(parse("STATE: done\nRECAP: x"), "")

    def test_only_first_topic_line(self):
        self.assertEqual(parse("TOPIC: a\nTOPIC: b"), "a")

    def test_long_slug_capped(self):
        self.assertLessEqual(len(parse("TOPIC: " + "word " * 30)), 40)

    def test_script_still_valid_bash_and_asks_six_lines(self):
        subprocess.run(["bash", "-n", str(GEN)], check=True)
        text = GEN.read_text()
        self.assertIn("exactly six lines", text)
        self.assertIn("ASK: only when STATE is answer", text)
        self.assertIn("topic_suggest", text)
        self.assertIn("goal:$g", text)


if __name__ == "__main__":
    unittest.main()
