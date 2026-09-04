import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/qml/ServiceRuntime.qml"
RECONCILE_HARNESS = ROOT / "tests/qml/ReconcileRuntime.qml"
STATUS_AGE_HARNESS = ROOT / "tests/qml/StatusAgeRuntime.qml"
COLLECTOR_HARNESS = ROOT / "tests/qml/CollectorRuntime.qml"
NOTIFICATION_QUEUE_HARNESS = ROOT / "tests/qml/NotificationQueueRuntime.qml"
QUICKSHELL = shutil.which("quickshell")


@unittest.skipUnless(QUICKSHELL, "quickshell is not installed")
class QmlRuntimeTests(unittest.TestCase):
    def test_notification_setting_refresh_drops_newly_ineligible_queued_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "qml"
            config_dir.mkdir()
            shutil.copy2(ROOT / "Service.qml", config_dir / "Service.qml")
            shutil.copy2(ROOT / "Model.js", config_dir / "Model.js")
            shutil.copy2(NOTIFICATION_QUEUE_HARNESS, config_dir / "shell.qml")
            env = dict(
                os.environ,
                HOME=tmp,
                XDG_CONFIG_HOME=str(Path(tmp) / "config"),
                XDG_DATA_HOME=str(Path(tmp) / "data"),
                XDG_RUNTIME_DIR=str(Path(tmp) / "runtime"),
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                QT_QPA_PLATFORM="offscreen",
            )
            Path(env["XDG_RUNTIME_DIR"]).mkdir()
            result = subprocess.run(
                [QUICKSHELL or "quickshell", "--no-color", "-p", str(config_dir)],
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("HERMES_WATCHER_NOTIFICATION_FILTER_RUNTIME_PASS", output)
        self.assertNotIn("HERMES_WATCHER_NOTIFICATION_FILTER_RUNTIME_FAIL", output)

    def test_service_streams_snapshots_and_refreshes_without_relaunching_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "qml"
            config_dir.mkdir()
            shutil.copy2(ROOT / "Service.qml", config_dir / "Service.qml")
            shutil.copy2(ROOT / "Model.js", config_dir / "Model.js")
            shutil.copy2(COLLECTOR_HARNESS, config_dir / "shell.qml")
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            launch_log = Path(tmp) / "collector-launch.log"
            fake_python = bin_dir / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$COLLECTOR_LAUNCH_LOG\"\n"
                "printf '%s\\n' '{\"schemaVersion\":1,\"generatedAt\":1,\"hermesRoot\":\"/tmp\",\"onlineProfiles\":[],\"availableProfiles\":[],\"profiles\":[],\"recent\":[],\"recentSessions\":[],\"pendingNotifications\":[]}'\n"
                "IFS= read -r request\n"
                "if [[ \"$request\" == refresh ]]; then\n"
                "  printf '%s\\n' '{\"schemaVersion\":1,\"generatedAt\":2,\"hermesRoot\":\"/tmp\",\"onlineProfiles\":[],\"availableProfiles\":[],\"profiles\":[],\"recent\":[],\"recentSessions\":[],\"pendingNotifications\":[]}'\n"
                "fi\n"
                "while IFS= read -r request; do :; done\n"
            )
            fake_python.chmod(0o755)
            env = dict(
                os.environ,
                HOME=tmp,
                XDG_CONFIG_HOME=str(Path(tmp) / "config"),
                XDG_DATA_HOME=str(Path(tmp) / "data"),
                XDG_RUNTIME_DIR=str(Path(tmp) / "runtime"),
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                QT_QPA_PLATFORM="offscreen",
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                COLLECTOR_LAUNCH_LOG=str(launch_log),
            )
            Path(env["XDG_RUNTIME_DIR"]).mkdir()
            try:
                result = subprocess.run(
                    [QUICKSHELL or "quickshell", "--no-color", "-p", str(config_dir)],
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                self.fail(
                    "persistent collector runtime did not finish: "
                    + str(error)
                    + "\n"
                    + str(error.stdout or "")
                    + str(error.stderr or "")
                )
            launches = launch_log.read_text().splitlines() if launch_log.exists() else []

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("HERMES_WATCHER_COLLECTOR_RUNTIME_PASS", output)
        self.assertNotIn("HERMES_WATCHER_COLLECTOR_RUNTIME_FAIL", output)
        self.assertEqual(len(launches), 1)
        self.assertIn(" watch ", f" {launches[0]} ")

    def test_snapshot_age_clock_advances_while_status_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "qml"
            config_dir.mkdir()
            shutil.copy2(ROOT / "Service.qml", config_dir / "Service.qml")
            shutil.copy2(ROOT / "Model.js", config_dir / "Model.js")
            shutil.copy2(STATUS_AGE_HARNESS, config_dir / "shell.qml")
            env = dict(
                os.environ,
                HOME=tmp,
                XDG_CONFIG_HOME=str(Path(tmp) / "config"),
                XDG_DATA_HOME=str(Path(tmp) / "data"),
                XDG_RUNTIME_DIR=str(Path(tmp) / "runtime"),
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                QT_QPA_PLATFORM="offscreen",
            )
            Path(env["XDG_RUNTIME_DIR"]).mkdir()
            result = subprocess.run(
                [QUICKSHELL or "quickshell", "--no-color", "-p", str(config_dir)],
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("HERMES_WATCHER_AGE_RUNTIME_PASS", output)
        self.assertNotIn("HERMES_WATCHER_AGE_RUNTIME_FAIL", output)

    def test_busy_setup_does_not_mark_profile_reconciliation_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "qml"
            config_dir.mkdir()
            shutil.copy2(ROOT / "Service.qml", config_dir / "Service.qml")
            shutil.copy2(ROOT / "Model.js", config_dir / "Model.js")
            shutil.copy2(RECONCILE_HARNESS, config_dir / "shell.qml")
            setup_script = config_dir / "scripts/setup-profiles"
            setup_script.parent.mkdir()
            setup_script.write_text("#!/usr/bin/env bash\nsleep 1\n")
            setup_script.chmod(0o755)
            env = dict(
                os.environ,
                HOME=tmp,
                XDG_CONFIG_HOME=str(Path(tmp) / "config"),
                XDG_DATA_HOME=str(Path(tmp) / "data"),
                XDG_RUNTIME_DIR=str(Path(tmp) / "runtime"),
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                QT_QPA_PLATFORM="offscreen",
            )
            Path(env["XDG_RUNTIME_DIR"]).mkdir()
            result = subprocess.run(
                [QUICKSHELL or "quickshell", "--no-color", "-p", str(config_dir)],
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
                check=False,
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("HERMES_WATCHER_RECONCILE_RUNTIME_PASS", output)
        self.assertNotIn("HERMES_WATCHER_RECONCILE_RUNTIME_FAIL", output)

    def test_service_loads_and_reports_a_failed_launch_without_closing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "qml"
            config_dir.mkdir()
            shutil.copy2(ROOT / "Service.qml", config_dir / "Service.qml")
            shutil.copy2(ROOT / "Model.js", config_dir / "Model.js")
            shutil.copy2(HARNESS, config_dir / "shell.qml")
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            launch_log = Path(tmp) / "launch.log"
            fake_omarchy = bin_dir / "omarchy"
            fake_omarchy.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$OMARCHY_TEST_LOG\"\n"
                "printf 'OMARCHY_ARGS:%s\\n' \"$*\" >&2\n"
                "exit 1\n"
            )
            fake_omarchy.chmod(0o755)
            env = dict(
                os.environ,
                HOME=tmp,
                XDG_CONFIG_HOME=str(Path(tmp) / "config"),
                XDG_DATA_HOME=str(Path(tmp) / "data"),
                XDG_RUNTIME_DIR=str(Path(tmp) / "runtime"),
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                QT_QPA_PLATFORM="offscreen",
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                OMARCHY_TEST_LOG=str(launch_log),
            )
            Path(env["XDG_RUNTIME_DIR"]).mkdir()
            result = subprocess.run(
                [QUICKSHELL or "quickshell", "--no-color", "-p", str(config_dir)],
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
                check=False,
            )
            launch_args = launch_log.read_text().splitlines()

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("HERMES_WATCHER_QML_RUNTIME_PASS", output)
        self.assertNotIn("QQmlApplicationEngine failed to load component", output)
        self.assertEqual(
            launch_args,
            ["launch", "terminal", "env", "HERMES_HOME=/tmp", "false"],
        )


if __name__ == "__main__":
    unittest.main()
