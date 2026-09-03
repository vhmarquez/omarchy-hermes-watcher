import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProfileScriptTests(unittest.TestCase):
    def test_profile_links_and_data_removal_use_descriptor_safe_helper(self):
        setup = (ROOT / "scripts/setup-profiles").read_text()
        removal = (ROOT / "scripts/remove-profiles").read_text()

        self.assertIn('install-profile-link', setup)
        self.assertNotIn('ln -s -- "$observer_source" "$destination"', setup)
        self.assertIn('remove-profile-link', removal)
        self.assertNotIn('rm -- "$destination"', removal)
        self.assertIn('remove-data', removal)
        self.assertNotIn('rm -rf -- "$data_root"', removal)

    def test_remove_disables_observer_and_removes_only_managed_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log, env = self.make_environment(tmp)
            subprocess.run(["bash", str(ROOT / "scripts/setup-profiles")], env=env, check=True)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts/remove-profiles")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data_root = Path(env["XDG_DATA_HOME"]) / "vhm.hermes-bots"
            self.assertFalse((base / "plugins/omarchy-bot-status").exists())
            self.assertFalse((base / "profiles/coder/plugins/omarchy-bot-status").exists())
            self.assertFalse((data_root / "hermes-plugin").exists())
            commands = log.read_text().splitlines()
            self.assertIn("plugins disable omarchy-bot-status", commands)
            self.assertIn("-p coder plugins disable omarchy-bot-status", commands)

    def test_remove_does_not_disable_an_unrelated_plugin_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log, env = self.make_environment(tmp)
            unrelated = base / "profiles/coder/plugins/omarchy-bot-status"
            unrelated.mkdir(parents=True)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts/remove-profiles")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(unrelated.is_dir())
            commands = log.read_text() if log.exists() else ""
            self.assertNotIn("-p coder plugins disable omarchy-bot-status", commands)

    def test_remove_does_not_disable_when_managed_link_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, log, env = self.make_environment(tmp)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts/remove-profiles")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text() if log.exists() else ""
            self.assertNotIn("plugins disable omarchy-bot-status", commands)

    def make_environment(self, tmp):
        home = Path(tmp) / "home"
        base = home / ".hermes"
        (base / "profiles/coder").mkdir(parents=True)
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        log = Path(tmp) / "hermes.log"
        fake_hermes = fake_bin / "hermes"
        fake_hermes.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$HERMES_TEST_LOG"\n')
        fake_hermes.chmod(0o755)
        runtime = Path(tmp) / "run"
        runtime.mkdir()
        env = dict(
            os.environ,
            HOME=str(home),
            HERMES_ROOT=str(base),
            HERMES_TEST_LOG=str(log),
            XDG_STATE_HOME=str(Path(tmp) / "state"),
            XDG_DATA_HOME=str(Path(tmp) / "data"),
            XDG_RUNTIME_DIR=str(runtime),
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )
        return base, log, env

    def test_conditional_remove_releases_lock_when_shell_ipc_hangs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _, env = self.make_environment(tmp)
            setup = str(ROOT / "scripts/setup-profiles")
            subprocess.run([setup], env=env, check=True, stdout=subprocess.DEVNULL)
            fake_shell = Path(tmp) / "bin/omarchy-shell"
            fake_shell.write_text("#!/usr/bin/env bash\nsleep 30\n")
            fake_shell.chmod(0o755)
            remove = Path(env["XDG_DATA_HOME"]) / "vhm.hermes-bots/scripts/remove-profiles"

            result = subprocess.run(
                ["timeout", "5", str(remove), "--if-disabled"],
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 124)
            self.assertTrue((base / "plugins/omarchy-bot-status").exists())

    def test_setup_explicitly_denies_builtin_tool_override(self):
        setup = (ROOT / "scripts/setup-profiles").read_text()
        self.assertIn("--no-allow-tool-override", setup)

    def test_setup_links_and_enables_observer_for_default_and_named_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log, env = self.make_environment(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/setup-profiles")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed_observer = Path(env["XDG_DATA_HOME"]) / "vhm.hermes-bots/hermes-plugin"
            self.assertEqual((base / "plugins/omarchy-bot-status").resolve(), installed_observer)
            self.assertEqual((base / "profiles/coder/plugins/omarchy-bot-status").resolve(), installed_observer)
            self.assertTrue((installed_observer / "plugin.yaml").is_file())
            self.assertTrue((installed_observer / "hermes_proc.py").is_file())
            self.assertTrue((installed_observer / "secure_paths.py").is_file())
            commands = log.read_text().splitlines()
            self.assertIn("plugins enable --no-allow-tool-override omarchy-bot-status", commands)
            self.assertIn("-p coder plugins enable --no-allow-tool-override omarchy-bot-status", commands)
            self.assertTrue((Path(env["XDG_STATE_HOME"]) / "omarchy/hermes-bots/consumer.json").exists())

    def test_repeated_setup_does_not_acknowledge_new_completions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _, env = self.make_environment(tmp)
            setup = str(ROOT / "scripts/setup-profiles")
            subprocess.run([setup], env=env, check=True, stdout=subprocess.DEVNULL)
            state_root = Path(env["XDG_STATE_HOME"]) / "omarchy/hermes-bots"
            events = state_root / "events/coder"
            events.mkdir(parents=True)
            (events / "new.json").write_text(
                '{"schemaVersion":1,"eventId":"new","profile":"coder","state":"succeeded",'
                '"startedAt":1,"finishedAt":2,"durationSec":1}'
            )

            subprocess.run([setup], env=env, check=True, stdout=subprocess.DEVNULL)

            consumer = __import__("json").loads((state_root / "consumer.json").read_text())
            self.assertNotIn("new", consumer["acknowledged"])

    def test_setup_and_remove_are_idempotent_with_symlinked_xdg_data_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _, env = self.make_environment(tmp)
            actual_data = Path(tmp) / "actual-data"
            actual_data.mkdir()
            data_link = Path(tmp) / "data-link"
            data_link.symlink_to(actual_data, target_is_directory=True)
            env["XDG_DATA_HOME"] = str(data_link)

            subprocess.run([str(ROOT / "scripts/setup-profiles")], env=env, check=True, stdout=subprocess.DEVNULL)
            second = subprocess.run(
                [str(ROOT / "scripts/setup-profiles")], env=env, text=True, capture_output=True
            )
            removed = subprocess.run(
                [str(ROOT / "scripts/remove-profiles")], env=env, text=True, capture_output=True
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse((base / "plugins/omarchy-bot-status").exists())

    def test_remove_refuses_symlinked_managed_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, env = self.make_environment(tmp)
            data_home = Path(env["XDG_DATA_HOME"])
            data_home.mkdir(parents=True)
            victim = Path(tmp) / "victim"
            victim.mkdir()
            (victim / "keep").write_text("safe")
            (data_home / "vhm.hermes-bots").symlink_to(victim, target_is_directory=True)

            result = subprocess.run(
                [str(ROOT / "scripts/remove-profiles")], env=env, text=True, capture_output=True
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((victim / "keep").is_file())

    def test_setup_ignores_symlinked_profile_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log, env = self.make_environment(tmp)
            outside = Path(tmp) / "outside-profile"
            outside.mkdir()
            (base / "profiles/linked").symlink_to(outside, target_is_directory=True)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts/setup-profiles")],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((outside / "plugins/omarchy-bot-status").exists())
            self.assertNotIn("-p linked plugins enable omarchy-bot-status", log.read_text())

    def test_setup_refuses_symlinks_in_nested_managed_data_directories(self):
        for nested in ("hermes-plugin", "scripts"):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as tmp:
                _, _, env = self.make_environment(tmp)
                data_root = Path(env["XDG_DATA_HOME"]) / "vhm.hermes-bots"
                data_root.mkdir(parents=True)
                victim = Path(tmp) / "victim"
                victim.mkdir()
                (data_root / nested).symlink_to(victim, target_is_directory=True)

                result = subprocess.run(
                    [str(ROOT / "scripts/setup-profiles")], env=env, text=True, capture_output=True
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(victim.iterdir()), [])

    def test_setup_refuses_symlinked_managed_payload_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, env = self.make_environment(tmp)
            observer = Path(env["XDG_DATA_HOME"]) / "vhm.hermes-bots/hermes-plugin"
            observer.mkdir(parents=True)
            victim = Path(tmp) / "victim"
            victim.write_text("do not change")
            (observer / "plugin.yaml").symlink_to(victim)

            result = subprocess.run(
                [str(ROOT / "scripts/setup-profiles")], env=env, text=True, capture_output=True
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), "do not change")
            self.assertTrue((observer / "plugin.yaml").is_symlink())

    def test_setup_does_not_follow_directory_swapped_during_payload_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, env = self.make_environment(tmp)
            victim = Path(tmp) / "victim"
            victim.mkdir()
            victim_payload = victim / "plugin.yaml"
            victim_payload.write_text("do not change")
            real_install = subprocess.run(
                ["bash", "-lc", "command -v install"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            fake_install = Path(tmp) / "bin/install"
            fake_install.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "destination=${!#}\n"
                "if [[ $destination == */hermes-plugin/plugin.yaml ]]; then\n"
                "  observer=${destination%/plugin.yaml}\n"
                "  mv -- \"$observer\" \"$observer.detached\"\n"
                f"  ln -s -- {str(victim)!r} \"$observer\"\n"
                "fi\n"
                f"exec {real_install!r} \"$@\"\n"
            )
            fake_install.chmod(0o755)

            subprocess.run(
                [str(ROOT / "scripts/setup-profiles")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(victim_payload.read_text(), "do not change")

    def test_setup_and_remove_serialize_on_the_shared_lifecycle_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, log, env = self.make_environment(tmp)
            fake_hermes = Path(tmp) / "bin/hermes"
            fake_hermes.write_text(
                '#!/usr/bin/env bash\nsleep 0.4\nprintf "%s\\n" "$*" >> "$HERMES_TEST_LOG"\n'
            )
            fake_hermes.chmod(0o755)
            setup = subprocess.Popen(
                [str(ROOT / "scripts/setup-profiles")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            lock = Path(env["XDG_RUNTIME_DIR"]) / f"vhm-hermes-bots-{os.getuid()}.lock"
            for _ in range(100):
                if lock.is_file():
                    break
                __import__("time").sleep(0.01)

            removed = subprocess.run(
                [str(ROOT / "scripts/remove-profiles")],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            _, setup_stderr = setup.communicate(timeout=5)

            self.assertEqual(setup.returncode, 0, setup_stderr)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(log.exists())

    def test_setup_refuses_symlinked_lock_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, env = self.make_environment(tmp)
            lock = Path(env["XDG_RUNTIME_DIR"]) / f"vhm-hermes-bots-{os.getuid()}.lock"
            victim = Path(tmp) / "victim"
            victim.write_text("do not change")
            lock.symlink_to(victim)

            result = subprocess.run(
                [str(ROOT / "scripts/setup-profiles")], env=env, text=True, capture_output=True
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), "do not change")


if __name__ == "__main__":
    unittest.main()
