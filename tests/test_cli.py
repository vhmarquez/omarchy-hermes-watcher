import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_collector import write_record


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes_bot_status.py"


class CollectorCliTests(unittest.TestCase):
    def test_snapshot_fails_when_managed_state_anchor_is_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_home = Path(tmp) / "state"
            state_home.mkdir()
            victim = Path(tmp) / "victim"
            victim.mkdir()
            (state_home / "omarchy").symlink_to(victim, target_is_directory=True)
            env = dict(os.environ, XDG_STATE_HOME=str(state_home))

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "snapshot"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_snapshot_command_returns_ui_contract_and_pending_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "omarchy/hermes-bots"
            write_record(
                state_root,
                "coder",
                "done",
                state="succeeded",
                startedAt=1.0,
                finishedAt=10.0,
                durationSec=9.0,
            )
            env = dict(os.environ, XDG_STATE_HOME=tmp)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "snapshot", "--now", "11"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["activeBotCount"], 0)
            self.assertEqual(data["pendingNotifications"][0]["eventId"], "done")


if __name__ == "__main__":
    unittest.main()
