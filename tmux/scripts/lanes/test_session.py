"""Builds the lanes session on a private tmux server with stand-in programs."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "lanes-session.sh"
SOCKET = "lanes-test"


def tmux(*args):
    return subprocess.run(["tmux", "-f", "/dev/null", "-L", SOCKET, *args], capture_output=True, text=True, timeout=10)


@unittest.skipUnless(shutil.which("tmux") and shutil.which("zsh"), "needs tmux and zsh")
class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(tmux, "kill-server")
        (self.dir / "chat.sh").write_text("#!/bin/sh\necho CHAT \"$CC_LANES_MAIN\" \"$@\"\nsleep 30\n")
        (self.dir / "tui.py").write_text("import time\nprint('TUI')\ntime.sleep(30)\n")
        (self.dir / "chat.sh").chmod(0o755)
        self.env = dict(
            os.environ, LANES_TMUX_SOCKET=SOCKET, LANES_CLAUDE=str(self.dir / "chat.sh"),
            LANES_TUI=str(self.dir / "tui.py"), HELM_DIR=str(self.dir), LANES_STATE_DIR=str(self.dir),
            LANES_DB=str(self.dir / "helm.db"), COLUMNS="160", LINES="40", TMUX="",
        )
        self.env.pop("TMUX")

    def ensure(self):
        return subprocess.run([str(SCRIPT), "--ensure"], capture_output=True, text=True,
                              env=self.env, timeout=20)

    def wait_for(self, pane, text):
        import time
        for _ in range(50):
            if text in tmux("capture-pane", "-p", "-t", pane).stdout:
                return True
            time.sleep(0.1)
        return False

    def test_builds_two_panes_and_records_ids(self):
        proc = self.ensure()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        chat = tmux("show-option", "-qv", "-t", "lanes", "@lanes_chat_pane").stdout.strip()
        tui = tmux("show-option", "-qv", "-t", "lanes", "@lanes_tui_pane").stdout.strip()
        panes = tmux("list-panes", "-t", "=lanes", "-F", "#{pane_id}").stdout.split()
        self.assertEqual(sorted(panes), sorted([chat, tui]))
        self.assertTrue(self.wait_for(chat, "CHAT 1"))
        self.assertTrue(self.wait_for(tui, "TUI"))

    def test_list_sits_left_of_the_chat(self):
        self.ensure()
        chat = tmux("show-option", "-qv", "-t", "lanes", "@lanes_chat_pane").stdout.strip()
        tui = tmux("show-option", "-qv", "-t", "lanes", "@lanes_tui_pane").stdout.strip()
        left = lambda p: int(tmux("display", "-p", "-t", p, "#{pane_left}").stdout)
        self.assertLess(left(tui), left(chat))

    def test_chat_border_carries_the_lane_chip(self):
        self.ensure()
        fmt = tmux("show-options", "-wqv", "-t", "=lanes:", "pane-border-format").stdout
        self.assertIn("@lane_label", fmt)
        self.ensure()
        fmt2 = tmux("show-options", "-wqv", "-t", "=lanes:", "pane-border-format").stdout
        self.assertEqual(fmt2.count("@lane_label"), fmt.count("@lane_label"))

    def test_chat_gets_helm_flags_and_no_resume_without_transcript(self):
        self.ensure()
        chat = tmux("show-option", "-qv", "-t", "lanes", "@lanes_chat_pane").stdout.strip()
        self.assertTrue(self.wait_for(chat, "--dangerously-skip-permissions"))
        self.assertNotIn("--resume", tmux("capture-pane", "-p", "-t", chat).stdout)

    def test_second_run_reuses_session(self):
        self.ensure()
        before = tmux("list-panes", "-t", "=lanes", "-F", "#{pane_id}").stdout
        self.assertEqual(self.ensure().returncode, 0)
        self.assertEqual(tmux("list-panes", "-t", "=lanes", "-F", "#{pane_id}").stdout, before)

    def test_reopening_restores_a_list_that_exited(self):
        self.ensure()
        chat = tmux("show-option", "-qv", "-t", "lanes", "@lanes_chat_pane").stdout.strip()
        old = tmux("show-option", "-qv", "-t", "lanes", "@lanes_tui_pane").stdout.strip()
        tmux("kill-pane", "-t", old)
        self.assertEqual(self.ensure().returncode, 0)
        new = tmux("show-option", "-qv", "-t", "lanes", "@lanes_tui_pane").stdout.strip()
        panes = tmux("list-panes", "-t", "=lanes", "-F", "#{pane_id}").stdout.split()
        self.assertNotEqual(new, old)
        self.assertEqual(sorted(panes), sorted([chat, new]))
        self.assertTrue(self.wait_for(new, "TUI"))

    def test_help(self):
        proc = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--ensure", proc.stdout)


if __name__ == "__main__":
    unittest.main()
