import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "profile_installer", ROOT / "scripts/profile_installer.py"
)
profile_installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(profile_installer)


class ProfileInstallerRaceTests(unittest.TestCase):
    def test_directory_swap_before_atomic_replace_cannot_modify_victim(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_parent = Path(tmp) / "data"
            observer = data_parent / "vhm.hermes-bots/hermes-plugin"
            detached = observer.with_name("hermes-plugin.detached")
            victim = Path(tmp) / "victim"
            victim.mkdir()
            victim_payload = victim / "plugin.yaml"
            victim_payload.write_text("do not change")
            original_replace = os.replace
            swapped = False

            def swap_then_replace(source, destination, *args, **kwargs):
                nonlocal swapped
                if destination == "plugin.yaml" and not swapped:
                    swapped = True
                    observer.rename(detached)
                    observer.symlink_to(victim, target_is_directory=True)
                return original_replace(source, destination, *args, **kwargs)

            with mock.patch.object(profile_installer.os, "replace", side_effect=swap_then_replace):
                profile_installer.install_data(data_parent, ROOT)

            self.assertTrue(swapped)
            self.assertEqual(victim_payload.read_text(), "do not change")
            self.assertEqual((detached / "plugin.yaml").read_text(),
                             (ROOT / "hermes-plugin/plugin.yaml").read_text())

    def test_profile_plugins_swap_cannot_redirect_link_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp) / "profile"
            plugins = profile_home / "plugins"
            plugins.mkdir(parents=True)
            detached = profile_home / "plugins.detached"
            victim = Path(tmp) / "victim"
            victim.mkdir()
            observer = Path(tmp) / "observer"
            observer.mkdir()
            original_symlink = os.symlink
            swapped = False

            def swap_then_symlink(source, destination, *args, **kwargs):
                nonlocal swapped
                if destination == "omarchy-bot-status" and not swapped:
                    swapped = True
                    plugins.rename(detached)
                    plugins.symlink_to(victim, target_is_directory=True)
                return original_symlink(source, destination, *args, **kwargs)

            with mock.patch.object(profile_installer.os, "symlink", side_effect=swap_then_symlink):
                profile_installer.install_profile_link(profile_home, observer)

            self.assertTrue(swapped)
            self.assertEqual(list(victim.iterdir()), [])
            self.assertEqual((detached / "omarchy-bot-status").resolve(), observer)

    def test_profile_plugins_swap_cannot_redirect_link_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp) / "profile"
            plugins = profile_home / "plugins"
            plugins.mkdir(parents=True)
            detached = profile_home / "plugins.detached"
            victim = Path(tmp) / "victim"
            victim.mkdir()
            victim_entry = victim / "omarchy-bot-status"
            victim_entry.write_text("keep")
            observer = Path(tmp) / "observer"
            observer.mkdir()
            (plugins / "omarchy-bot-status").symlink_to(observer)
            original_unlink = os.unlink
            swapped = False

            def swap_then_unlink(name, *args, **kwargs):
                nonlocal swapped
                if name == "omarchy-bot-status" and not swapped:
                    swapped = True
                    plugins.rename(detached)
                    plugins.symlink_to(victim, target_is_directory=True)
                return original_unlink(name, *args, **kwargs)

            with mock.patch.object(profile_installer.os, "unlink", side_effect=swap_then_unlink):
                result = profile_installer.remove_profile_link(profile_home, observer)

            self.assertEqual(result, "removed")
            self.assertTrue(swapped)
            self.assertEqual(victim_entry.read_text(), "keep")
            self.assertFalse((detached / "omarchy-bot-status").exists())


if __name__ == "__main__":
    unittest.main()
