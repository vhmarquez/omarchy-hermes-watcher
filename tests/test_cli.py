import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_collector import write_record


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes_bot_status.py"


class CollectorCliTests(unittest.TestCase):
    def test_snapshot_privacy_flag_persists_policy_and_purges_excerpts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_home = Path(tmp) / "state"
            root = state_home / "omarchy/hermes-bots"
            write_record(
                root,
                "default",
                "legacy-running",
                workDescription="legacy private excerpt",
            )
            env = dict(
                os.environ,
                XDG_STATE_HOME=str(state_home),
                HERMES_ROOT=str(Path(tmp) / "hermes"),
                PYTHONDONTWRITEBYTECODE="1",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "snapshot",
                    "--no-show-work-description",
                ],
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(
                all("workDescription" not in row for row in payload["onlineProfiles"])
            )
            policy = json.loads((root / "privacy.json").read_text())
            self.assertIs(policy["showWorkDescription"], False)
            record = json.loads((root / "events/default/legacy-running.json").read_text())
            self.assertNotIn("workDescription", record)

    def test_clear_history_command_removes_terminal_records_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state/omarchy/hermes-bots"
            now = __import__("time").time()
            write_record(
                state_root,
                "coder",
                "terminal-cli",
                state="succeeded",
                startedAt=now - 10,
                finishedAt=now,
                durationSec=10,
            )
            write_record(
                state_root,
                "coder",
                "running-cli",
                startedAt=now,
                updatedAt=now,
                writerPid=os.getpid(),
                writerProcessStart="1",
            )
            env = dict(os.environ, XDG_STATE_HOME=str(Path(tmp) / "state"))

            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "clear-history"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"removed": 1})
            self.assertTrue((state_root / "events/coder/running-cli.json").exists())

    def test_watch_stream_emits_initial_and_event_driven_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state/omarchy/hermes-bots"
            env = dict(
                os.environ,
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                HERMES_ROOT=str(Path(tmp) / "hermes"),
                PYTHONDONTWRITEBYTECODE="1",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "watch",
                    "--health-scan-sec",
                    "60",
                    "--history-limit",
                    "6",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertIsNotNone(process.stdout)
            self.assertIsNotNone(process.stderr)
            stdout = process.stdout
            stderr = process.stderr
            assert stdout is not None and stderr is not None
            try:
                ready, _, _ = select.select([stdout], [], [], 2)
                self.assertTrue(ready, stderr.read() if process.poll() is not None else "")
                initial_line = stdout.readline()
                self.assertNotEqual(
                    initial_line,
                    "",
                    stderr.read() if process.poll() is not None else "watch stream closed",
                )
                initial = json.loads(initial_line)
                self.assertEqual(initial["schemaVersion"], 1)

                now = __import__("time").time()
                write_record(
                    state_root,
                    "coder",
                    "event-driven",
                    state="succeeded",
                    startedAt=now - 10,
                    finishedAt=now,
                    durationSec=10,
                )

                ready, _, _ = select.select([stdout], [], [], 2)
                self.assertTrue(ready, "watch stream did not react to a lifecycle record")
                updated = json.loads(stdout.readline())
                self.assertEqual(updated["recent"][0]["eventId"], "event-driven")
            finally:
                process.terminate()
                process.communicate(timeout=2)

    def test_deliver_notification_claims_sends_and_acknowledges_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state/omarchy/hermes-bots"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            notify_log = Path(tmp) / "notify.log"
            fake_notify = bin_dir / "notify-send"
            fake_notify.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$NOTIFY_LOG\"\n"
            )
            fake_notify.chmod(0o755)
            env = dict(
                os.environ,
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                NOTIFY_LOG=str(notify_log),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "deliver-notification",
                    "event-cli",
                    "--icon",
                    "/tmp/icon.svg",
                    "--title",
                    "Hermes finished",
                    "--body",
                    "coder succeeded",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                notify_log.read_text().splitlines(),
                [
                    "--app-name=Hermes Watcher",
                    "--urgency=normal",
                    "--icon=/tmp/icon.svg",
                    "Hermes finished",
                    "coder succeeded",
                ],
            )
            consumer = json.loads((state_root / "consumer.json").read_text())
            self.assertIn("event-cli", consumer["acknowledged"])
            self.assertNotIn("event-cli", consumer["claimed"])

    def test_snapshot_repairs_malformed_consumer_without_losing_core_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "omarchy/hermes-bots"
            state_root.mkdir(parents=True)
            (state_root / "consumer.json").write_text("[]")
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
            self.assertEqual(data["recent"][0]["eventId"], "done")
            self.assertEqual(data["pendingNotifications"], [])
            self.assertEqual(data["notificationError"], "Notification history was repaired")
            consumer = json.loads((state_root / "consumer.json").read_text())
            self.assertIn("done", consumer["acknowledged"])

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
