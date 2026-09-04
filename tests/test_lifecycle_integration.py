import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES = shutil.which("hermes")
HERMES_COMMAND = HERMES or "hermes"


@unittest.skipUnless(HERMES, "Hermes CLI is required for lifecycle integration tests")
class RealHermesLifecycleTests(unittest.TestCase):
    def test_sticky_named_profile_cannot_redirect_default_profile_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            home = sandbox / "home"
            hermes_root = home / ".hermes"
            home.mkdir()
            (sandbox / "run").mkdir()
            env = dict(
                os.environ,
                HOME=str(home),
                HERMES_HOME=str(hermes_root),
                HERMES_ROOT=str(hermes_root),
                XDG_DATA_HOME=str(sandbox / "data"),
                XDG_STATE_HOME=str(sandbox / "state"),
                XDG_RUNTIME_DIR=str(sandbox / "run"),
            )
            env.pop("HERMES_PROFILE", None)

            for command in (
                [HERMES_COMMAND, "profile", "create", "coder", "--no-alias", "--no-skills"],
                [HERMES_COMMAND, "profile", "use", "coder"],
            ):
                result = subprocess.run(
                    command,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            setup = subprocess.run(
                [str(ROOT / "scripts/setup-profiles")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            for profile in ("default", "coder"):
                listed = subprocess.run(
                    [
                        HERMES_COMMAND,
                        "--profile",
                        profile,
                        "plugins",
                        "list",
                        "--plain",
                        "--no-bundled",
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertIn("enabled", listed.stdout)
                self.assertIn("omarchy-bot-status", listed.stdout)

            remove = subprocess.run(
                [str(ROOT / "scripts/remove-profiles")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(remove.returncode, 0, remove.stderr)
            self.assertFalse((hermes_root / "plugins/omarchy-bot-status").exists())
            self.assertFalse((hermes_root / "profiles/coder/plugins/omarchy-bot-status").exists())

    def test_setup_import_and_remove_round_trip_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            home = sandbox / "home"
            hermes_root = home / ".hermes"
            data_home = sandbox / "data"
            runtime = sandbox / "run"
            home.mkdir()
            runtime.mkdir()
            env = dict(
                os.environ,
                HOME=str(home),
                HERMES_HOME=str(hermes_root),
                HERMES_ROOT=str(hermes_root),
                XDG_DATA_HOME=str(data_home),
                XDG_STATE_HOME=str(sandbox / "state"),
                XDG_RUNTIME_DIR=str(runtime),
            )
            env.pop("HERMES_PROFILE", None)
            env.pop("PYTHONDONTWRITEBYTECODE", None)

            setup = subprocess.run(
                [str(ROOT / "scripts/setup-profiles")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            observer = data_home / "vhm.hermes-bots/hermes-plugin"
            self.assertTrue((hermes_root / "plugins/omarchy-bot-status").is_symlink())

            doctor = subprocess.run(
                [HERMES_COMMAND, "plugins", "doctor", "--ci", str(observer)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            compile_result = subprocess.run(
                ["python3", "-m", "compileall", "-q", str(observer)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            self.assertTrue((observer / "__pycache__").is_dir())

            remove = subprocess.run(
                [str(ROOT / "scripts/remove-profiles")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(remove.returncode, 0, remove.stderr)
            self.assertFalse((hermes_root / "plugins/omarchy-bot-status").exists())
            self.assertFalse((data_home / "vhm.hermes-bots").exists())


if __name__ == "__main__":
    unittest.main()
