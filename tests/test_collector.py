import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.test_observer import load_observer


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes_bot_status.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("hermes_bot_status_collector", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_record(root: Path, profile: str, name: str, **values):
    directory = root / "events" / profile
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1,
        "eventId": name,
        "profile": profile,
        "sessionId": f"session-{name}",
        "turnId": f"turn-{name}",
        "state": "running",
        "startedAt": 100.0,
        "updatedAt": 100.0,
        "model": "model-x",
        "platform": "cli",
        "writerPid": 10,
        "writerProcessStart": "20",
    }
    record.update(values)
    (directory / f"{name}.json").write_text(json.dumps(record))


class TrackedCmdline:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def open(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int) -> bytes:
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class CollectorTests(unittest.TestCase):
    def test_snapshot_lists_available_profiles_with_native_avatars(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            default_avatar = hermes_root / "assets" / "avatar.png"
            coder_avatar = hermes_root / "profiles" / "coder" / "assets" / "avatar.webp"
            default_avatar.parent.mkdir(parents=True)
            coder_avatar.parent.mkdir(parents=True)
            default_avatar.write_bytes(b"\x89PNG\r\n\x1a\ndefault-avatar")
            coder_avatar.write_bytes(b"RIFF\x10\x00\x00\x00WEBPcoder-avatar")

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(
                [row["profile"] for row in snapshot["availableProfiles"]],
                ["default", "coder"],
            )
            self.assertTrue(snapshot["availableProfiles"][0]["avatarUrl"].startswith(default_avatar.as_uri()))
            self.assertTrue(snapshot["availableProfiles"][1]["avatarUrl"].startswith(coder_avatar.as_uri()))

    def test_available_profiles_ignore_symlinked_profile_directories(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            profiles = hermes_root / "profiles"
            outside = Path(tmp) / "outside"
            profiles.mkdir(parents=True)
            outside.mkdir()
            (profiles / "linked").symlink_to(outside, target_is_directory=True)

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(snapshot["availableProfiles"], [{"profile": "default"}])

    def test_available_profiles_match_hermes_ids_and_exclude_deleted_or_reserved_names(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            profiles = hermes_root / "profiles"
            names = (
                "live-bot", "Upper", "dotted.bot", "a" * 65,
                "hermes", "test", "tmp", "root", "sudo", "deleted-bot",
            )
            for name in names:
                (profiles / name).mkdir(parents=True)
            tombstone = profiles / ".deleted" / "deleted-bot"
            tombstone.parent.mkdir()
            tombstone.write_text("deleted\n")

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(
                snapshot["availableProfiles"],
                [{"profile": "default"}, {"profile": "live-bot"}],
            )

    def test_profile_avatar_fifo_does_not_block_snapshot_collection(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            assets = hermes_root / "profiles" / "coder" / "assets"
            assets.mkdir(parents=True)
            os.mkfifo(assets / "avatar.png")
            completed = threading.Event()
            result = []

            def probe():
                try:
                    result.append(collector._profile_avatar_url("coder", hermes_root=hermes_root))
                finally:
                    completed.set()

            thread = threading.Thread(target=probe, daemon=True)
            thread.start()

            self.assertTrue(completed.wait(0.5), "avatar FIFO blocked the collector")
            self.assertEqual(result, [""])

    def test_cmdline_reader_stops_before_one_shot_prompt_text(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(b"python3\0/opt/hermes/hermes\0--oneshot=TOP SECRET PROMPT\0")

            args = collector._read_hermes_cmdline_prefix(cmdline)

            self.assertEqual(args, ["python3", "/opt/hermes/hermes", "--oneshot"])
            self.assertNotIn("TOP SECRET PROMPT", args)

    def test_cmdline_reader_stops_before_attached_resume_names(self):
        collector = load_collector()
        for option in ("--resume=", "--continue=", "-r", "-c"):
            with self.subTest(option=option):
                prefix = f"hermes\0{option}".encode()
                cmdline = TrackedCmdline(prefix + b"TOP_SECRET_SESSION\0")

                args = collector._read_hermes_cmdline_prefix(cmdline)

                expected = {
                    "--resume=": "--resume",
                    "--continue=": "--continue",
                    "-r": "-r",
                    "-c": "-c",
                }[option]
                self.assertEqual(args, ["hermes", expected])
                self.assertEqual(cmdline.offset, len(prefix))

    def test_cmdline_reader_accepts_documented_compact_short_options(self):
        collector = load_collector()
        cases = (
            (b"hermes\0-w\0-pcoder\0", ["hermes", "--profile", "coder"]),
            (b"hermes\0-mcustom\0-pcoder\0", ["hermes", "--profile", "coder"]),
            (b"hermes\0-tterminal\0-pcoder\0", ["hermes", "--profile", "coder"]),
            (b"hermes\0-sskill\0-pcoder\0", ["hermes", "--profile", "coder"]),
            (b"hermes\0-rSESSION\0", ["hermes", "-r"]),
            (b"hermes\0-cSESSION\0", ["hermes", "-c"]),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(collector._read_hermes_cmdline_prefix(TrackedCmdline(payload)), expected)

    def test_cmdline_reader_rejects_non_hermes_before_later_arguments(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(b"python3\0/tmp/other.py\0TOP SECRET ARGUMENT\0")

            self.assertEqual(collector._read_hermes_cmdline_prefix(cmdline), [])

    def test_cmdline_reader_fails_closed_before_unknown_option_values(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(b"hermes\0--api-key\0TOPSECRET\0plugins\0list\0")

            self.assertEqual(collector._read_hermes_cmdline_prefix(cmdline), [])

    def test_cmdline_reader_fails_closed_when_bounded_read_is_exhausted(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(b"hermes\0" + (b"--model\0x\0" * 600) + b"plugins\0list\0")

            self.assertEqual(collector._read_hermes_cmdline_prefix(cmdline), [])

    def test_cmdline_reader_fails_closed_on_unterminated_option_value(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(b"hermes\0--model\0TOPSECRET")

            self.assertEqual(collector._read_hermes_cmdline_prefix(cmdline), [])

    def test_cmdline_reader_honors_pending_profile_value_that_starts_with_marker(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(
                b"python3\0/opt/hermes/hermes\0--profile\0chatops\0chat\0"
            )

            self.assertEqual(
                collector._read_hermes_cmdline_prefix(cmdline),
                ["python3", "/opt/hermes/hermes", "--profile", "chatops", "chat"],
            )

    def test_cmdline_reader_preserves_attached_profile_value(self):
        collector = load_collector()
        for profile_flag in ("--profile=chatops", "-p=chatops"):
            with self.subTest(profile_flag=profile_flag), tempfile.TemporaryDirectory() as tmp:
                cmdline = Path(tmp) / "cmdline"
                cmdline.write_bytes(b"hermes\0" + profile_flag.encode() + b"\0chat\0")

                self.assertEqual(
                    collector._read_hermes_cmdline_prefix(cmdline),
                    ["hermes", "--profile", "chatops", "chat"],
                )

    def test_cmdline_reader_discards_non_profile_option_values(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = Path(tmp) / "cmdline"
            cmdline.write_bytes(
                b"python3\0/opt/hermes/hermes\0--in\0/home/alice/private-work\0"
                b"--skills\0secret-skill-name\0-p\0coder\0chat\0"
            )

            args = collector._read_hermes_cmdline_prefix(cmdline)

            self.assertEqual(args, ["python3", "/opt/hermes/hermes", "-p", "coder", "chat"])
            self.assertNotIn("/home/alice/private-work", args)
            self.assertNotIn("secret-skill-name", args)

    def test_cmdline_reader_discards_attached_known_option_values(self):
        collector = load_collector()
        for option in ("--model=private-model", "-m=private-model", "--in=/private/work"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                cmdline = Path(tmp) / "cmdline"
                cmdline.write_bytes(b"hermes\0" + option.encode() + b"\0-p\0coder\0chat\0")

                args = collector._read_hermes_cmdline_prefix(cmdline)

                self.assertEqual(args, ["hermes", "-p", "coder", "chat"])
                self.assertNotIn(option.split("=", 1)[1], args)

    def test_resume_session_args_are_interactive(self):
        collector = load_collector()

        self.assertTrue(collector._is_interactive_hermes_args(["--continue"]))
        self.assertTrue(collector._is_interactive_hermes_args(["--resume", "20260902_session"]))
        self.assertFalse(collector._is_interactive_hermes_args(["plugins", "list"]))

    def test_cmdline_reader_discards_attached_resume_name(self):
        collector = load_collector()
        for option in ("--resume=private-session", "-r=private-session", "--continue=private-session"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                cmdline = Path(tmp) / "cmdline"
                cmdline.write_bytes(b"hermes\0" + option.encode() + b"\0")

                args = collector._read_hermes_cmdline_prefix(cmdline)

                self.assertEqual(args, ["hermes", option.split("=", 1)[0]])
                self.assertNotIn("private-session", args)

    def test_discovers_open_interactive_hermes_sessions_without_reading_conversations(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")

            def process(pid, argv, start_ticks):
                directory = proc / str(pid)
                directory.mkdir()
                directory.joinpath("cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
                fields = [str(pid), "(hermes)", "S"] + ["0"] * 18 + [str(start_ticks)]
                directory.joinpath("stat").write_text(" ".join(fields) + "\n")

            process(101, ["python3", "/opt/hermes/hermes"], 25000)
            process(102, ["python3", "/opt/hermes/hermes", "-p", "coder", "chat"], 50000)
            process(103, ["python3", "/opt/hermes/hermes", "plugins", "list"], 75000)
            process(104, ["python3", "/tmp/not-hermes"], 80000)
            process(105, ["python3", "/tmp/script.py", "hermes"], 81000)

            sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual([row["profile"] for row in sessions], ["coder", "default"])
            self.assertEqual([row["pid"] for row in sessions], [102, 101])
            self.assertEqual([row["processStart"] for row in sessions], ["50000", "25000"])
            self.assertEqual(sessions[0]["runningForSec"], 500.0)
            self.assertNotIn("cmdline", sessions[0])

    def test_session_discovery_preserves_attached_profile_value(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            directory = proc / "101"
            directory.mkdir()
            directory.joinpath("cmdline").write_bytes(b"hermes\0--profile=coder\0chat\0")
            fields = ["101", "(hermes)", "S"] + ["0"] * 18 + ["25000"]
            directory.joinpath("stat").write_text(" ".join(fields) + "\n")

            sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual([row["profile"] for row in sessions], ["coder"])

    def test_session_discovery_rejects_marker_prefixed_arguments(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            for pid, argument in ((101, "chatty-secret-prompt"), (102, "--resume-private")):
                directory = proc / str(pid)
                directory.mkdir()
                directory.joinpath("cmdline").write_bytes(
                    b"hermes\0" + argument.encode() + b"\0"
                )
                fields = [str(pid), "(hermes)", "S"] + ["0"] * 18 + ["25000"]
                directory.joinpath("stat").write_text(" ".join(fields) + "\n")

            sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual(sessions, [])

    def test_cmdline_reader_stops_when_token_cannot_be_interactive(self):
        collector = load_collector()
        cases = (
            (b"hermes\0chatty-TOP_SECRET_PROMPT\0", len(b"hermes\0chatt")),
            (b"hermes\0--resume-private-session\0", len(b"hermes\0--resume-")),
        )
        for payload, maximum_offset in cases:
            with self.subTest(payload=payload):
                cmdline = TrackedCmdline(payload)

                self.assertEqual(collector._read_hermes_cmdline_prefix(cmdline), [])
                self.assertLessEqual(cmdline.offset, maximum_offset)

    def test_session_discovery_rejects_pid_replaced_during_cmdline_read(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            directory = proc / "101"

            def write_process(path, profile, start_ticks):
                path.mkdir()
                path.joinpath("comm").write_text("hermes\n")
                path.joinpath("cmdline").write_bytes(
                    f"hermes\0--profile={profile}\0chat\0".encode()
                )
                fields = ["101", "(hermes)", "S"] + ["0"] * 18 + [str(start_ticks)]
                path.joinpath("stat").write_text(" ".join(fields) + "\n")

            write_process(directory, "original", 25000)
            original_reader = collector._read_hermes_cmdline_prefix
            swapped = False

            def swap_then_read(path, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    directory.rename(proc / "detached")
                    write_process(directory, "replacement", 50000)
                return original_reader(path, *args, **kwargs)

            with mock.patch.object(
                collector, "_read_hermes_cmdline_prefix", side_effect=swap_then_read
            ):
                sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertTrue(swapped)
            self.assertEqual(sessions, [])

    def test_session_discovery_rejects_processes_owned_by_another_effective_uid(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            directory = proc / "101"
            directory.mkdir()
            directory.joinpath("comm").write_text("hermes\n")
            directory.joinpath("cmdline").write_bytes(b"hermes\0")
            fields = ["101", "(hermes)", "S"] + ["0"] * 18 + ["25000"]
            directory.joinpath("stat").write_text(" ".join(fields) + "\n")

            with (
                mock.patch.object(collector.os, "geteuid", return_value=directory.stat().st_uid + 1),
                mock.patch.object(
                    collector,
                    "_read_hermes_cmdline_prefix",
                    wraps=collector._read_hermes_cmdline_prefix,
                ) as read_cmdline,
            ):
                sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual(sessions, [])
            read_cmdline.assert_not_called()

    def test_session_discovery_rejects_numeric_symlink_before_reading_cmdline(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            target = proc / "process-target"
            target.mkdir()
            target.joinpath("comm").write_text("hermes\n")
            target.joinpath("cmdline").write_bytes(b"hermes\0")
            fields = ["101", "(hermes)", "S"] + ["0"] * 18 + ["25000"]
            target.joinpath("stat").write_text(" ".join(fields) + "\n")
            (proc / "101").symlink_to(target, target_is_directory=True)

            with mock.patch.object(
                collector,
                "_read_hermes_cmdline_prefix",
                wraps=collector._read_hermes_cmdline_prefix,
            ) as read_cmdline:
                sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual(sessions, [])
            read_cmdline.assert_not_called()

    def test_process_stat_parsing_handles_spaces_and_parentheses_in_comm(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "uptime").write_text("1000.00 0.00\n")
            directory = proc / "101"
            directory.mkdir()
            directory.joinpath("cmdline").write_bytes(b"hermes\0")
            fields = ["101", "(hermes worker (child))", "S"] + ["0"] * 18 + ["25000"]
            directory.joinpath("stat").write_text(" ".join(fields) + "\n")

            sessions = collector._discover_hermes_sessions(proc_root=proc, clock_ticks=100)

            self.assertEqual(sessions[0]["runningForSec"], 750.0)

    def test_snapshot_reports_one_online_agent_row_per_session_without_loaded_lifecycle_hook(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = collector.build_snapshot(
                Path(tmp),
                now=100.0,
                online_sessions=[
                    {"profile": "default", "pid": 10, "processStart": "100", "runningForSec": 90.0},
                    {"profile": "coder", "pid": 11, "processStart": "110", "runningForSec": 50.0},
                    {"profile": "coder", "pid": 12, "processStart": "120", "runningForSec": 20.0},
                ],
            )

            self.assertEqual(snapshot["onlineBotCount"], 3)
            self.assertEqual(snapshot["onlineSessionCount"], 3)
            self.assertEqual(
                [row["profile"] for row in snapshot["onlineProfiles"]],
                ["coder", "coder", "default"],
            )
            self.assertEqual(
                [row["runningForSec"] for row in snapshot["onlineProfiles"]],
                [50.0, 20.0, 90.0],
            )
            self.assertTrue(all(row["activeTurnCount"] == 0 for row in snapshot["onlineProfiles"]))
            self.assertTrue(all("sessionCount" not in row for row in snapshot["onlineProfiles"]))

    def test_online_session_key_is_stable_across_poll_updates_and_hides_process_identity(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = collector.build_snapshot(
                root,
                now=100.0,
                online_sessions=[
                    {"profile": "coder", "pid": 12345, "processStart": "67890", "runningForSec": 50.0}
                ],
            )
            second = collector.build_snapshot(
                root,
                now=102.0,
                online_sessions=[
                    {"profile": "coder", "pid": 12345, "processStart": "67890", "runningForSec": 52.0}
                ],
            )

            first_key = first["onlineProfiles"][0]["sessionKey"]
            second_key = second["onlineProfiles"][0]["sessionKey"]
            self.assertEqual(first_key, second_key)
            self.assertEqual(len(first_key), 64)
            self.assertNotIn("12345", first_key)
            self.assertNotIn("67890", first_key)
            self.assertNotIn("pid", first["onlineProfiles"][0])
            self.assertNotIn("processStart", first["onlineProfiles"][0])

    def test_snapshot_keeps_concurrent_profile_sessions_as_distinct_active_agent_rows(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "session-a-turn",
                sessionId="session-a",
                writerPid=11,
                writerProcessStart="110",
                updatedAt=120.0,
                model="model-a",
                platform="cli",
                workDescription="Work in session A",
                contextUsed=75_000,
                contextMax=100_000,
            )
            write_record(
                root,
                "coder",
                "session-b-turn",
                sessionId="session-b",
                writerPid=12,
                writerProcessStart="120",
                updatedAt=121.0,
                model="model-b",
                platform="tui",
                workDescription="Work in session B",
                contextUsed=20_000,
                contextMax=100_000,
            )

            snapshot = collector.build_snapshot(
                root,
                now=125.0,
                online_sessions=[
                    {"profile": "coder", "pid": 11, "processStart": "110", "runningForSec": 50.0},
                    {"profile": "coder", "pid": 12, "processStart": "120", "runningForSec": 20.0},
                ],
            )

            self.assertEqual(snapshot["activeBotCount"], 2)
            self.assertEqual(snapshot["onlineBotCount"], 2)
            self.assertEqual(
                [
                    (
                        row["profile"],
                        row["model"],
                        row["platform"],
                        row["workDescription"],
                        row["runningForSec"],
                        row["contextPercent"],
                    )
                    for row in snapshot["onlineProfiles"]
                ],
                [
                    ("coder", "model-a", "cli", "Work in session A", 50.0, 75),
                    ("coder", "model-b", "tui", "Work in session B", 20.0, 20),
                ],
            )
            self.assertTrue(all(row["activeTurnCount"] == 1 for row in snapshot["onlineProfiles"]))

    def test_snapshot_exposes_the_online_profiles_native_avatar(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            avatar = hermes_root / "profiles" / "coder" / "assets" / "avatar.png"
            avatar.parent.mkdir(parents=True)
            avatar.write_bytes(b"\x89PNG\r\n\x1a\nprofile-avatar")

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
                )

            avatar_url = snapshot["onlineProfiles"][0]["avatarUrl"]
            self.assertTrue(avatar_url.startswith(avatar.as_uri() + "?v="))

    def test_snapshot_rejects_a_symlinked_profile_avatar(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            assets = hermes_root / "profiles" / "coder" / "assets"
            assets.mkdir(parents=True)
            outside = Path(tmp) / "outside.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate-image")
            (assets / "avatar.png").symlink_to(outside)

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
                )

            self.assertNotIn("avatarUrl", snapshot["onlineProfiles"][0])

    def test_idle_online_profile_reuses_latest_confirmed_context(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "confirmed-context",
                state="succeeded",
                updatedAt=120.0,
                finishedAt=120.0,
                durationSec=20.0,
                exitReason="text_response",
                contextUsed=68_000,
                contextMax=100_000,
                contextConfirmed=True,
            )

            snapshot = collector.build_snapshot(
                root,
                now=125.0,
                online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["activeTurnCount"], 0)
            self.assertEqual(profile["contextUsed"], 68_000)
            self.assertEqual(profile["contextMax"], 100_000)
            self.assertEqual(profile["contextPercent"], 68)
            self.assertIs(profile["contextIsLastKnown"], True)

    def test_idle_context_stays_with_the_matching_online_session(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "confirmed-context",
                sessionId="session-a",
                writerPid=11,
                writerProcessStart="110",
                state="succeeded",
                updatedAt=120.0,
                finishedAt=120.0,
                durationSec=20.0,
                exitReason="text_response",
                model="model-a",
                platform="cli",
                contextUsed=68_000,
                contextMax=100_000,
                contextConfirmed=True,
            )

            snapshot = collector.build_snapshot(
                root,
                now=125.0,
                online_sessions=[
                    {"profile": "coder", "pid": 11, "processStart": "110", "runningForSec": 50.0},
                    {"profile": "coder", "pid": 12, "processStart": "120", "runningForSec": 20.0},
                ],
            )

            first, second = snapshot["onlineProfiles"]
            self.assertEqual(first["contextUsed"], 68_000)
            self.assertEqual(first["model"], "model-a")
            self.assertIs(first["contextIsLastKnown"], True)
            self.assertNotIn("contextUsed", second)
            self.assertEqual(second["model"], "")

    def test_idle_last_context_ties_break_deterministically_by_event_id(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for event_id, context_used in (("context-a", 20_000), ("context-b", 80_000)):
                write_record(
                    root,
                    "coder",
                    event_id,
                    state="succeeded",
                    updatedAt=120.0,
                    finishedAt=120.0,
                    durationSec=20.0,
                    exitReason="text_response",
                    contextUsed=context_used,
                    contextMax=100_000,
                    contextConfirmed=True,
                )

            with mock.patch.object(
                collector,
                "_event_json_paths",
                return_value=[
                    root / "events" / "coder" / "context-a.json",
                    root / "events" / "coder" / "context-b.json",
                ],
            ):
                snapshot = collector.build_snapshot(
                    root,
                    now=125.0,
                    online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
                )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["contextUsed"], 80_000)
            self.assertIs(profile["contextIsLastKnown"], True)

    def test_idle_last_context_uses_finished_time_when_updated_time_is_malformed(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "confirmed-context",
                state="succeeded",
                updatedAt="not-a-number",
                finishedAt=120.0,
                durationSec=20.0,
                exitReason="text_response",
                contextUsed=68_000,
                contextMax=100_000,
                contextConfirmed=True,
            )

            snapshot = collector.build_snapshot(
                root,
                now=125.0,
                online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["contextUsed"], 68_000)
            self.assertIs(profile["contextIsLastKnown"], True)

    def test_snapshot_exposes_highest_context_pressure_for_concurrent_turns(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "older",
                sessionId="session-coder",
                state="running",
                updatedAt=110.0,
                contextUsed=250_000,
                contextMax=272_000,
                contextPercent=92,
            )
            write_record(
                root,
                "coder",
                "latest",
                sessionId="session-coder",
                state="running",
                updatedAt=120.0,
                contextUsed=186_000,
                contextMax=272_000,
                contextPercent=68,
            )

            snapshot = collector.build_snapshot(
                root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["contextUsed"], 250_000)
            self.assertEqual(profile["contextMax"], 272_000)
            self.assertEqual(profile["contextPercent"], 92)

    def test_snapshot_exposes_latest_active_work_description(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "older",
                sessionId="session-coder",
                updatedAt=110.0,
                workDescription="Review the API",
            )
            write_record(
                root,
                "coder",
                "latest",
                sessionId="session-coder",
                updatedAt=120.0,
                workDescription="Add agent descriptions",
            )

            snapshot = collector.build_snapshot(
                root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["workDescription"], "Add agent descriptions")

    def test_snapshot_drops_unbounded_or_control_character_descriptions(self):
        collector = load_collector()
        for description in ("x" * 161, "private\x00text"):
            with self.subTest(description=description), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_record(root, "coder", "turn", workDescription=description)

                snapshot = collector.build_snapshot(
                    root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
                )

                self.assertNotIn("workDescription", snapshot["onlineProfiles"][0])

    def test_snapshot_keeps_model_platform_and_context_from_highest_pressure_turn(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "high-pressure",
                sessionId="session-coder",
                updatedAt=110.0,
                model="high-pressure-model",
                platform="tui",
                reasoningLevel="high",
                contextUsed=250_000,
                contextMax=272_000,
            )
            write_record(
                root,
                "coder",
                "latest",
                sessionId="session-coder",
                updatedAt=120.0,
                model="latest-model",
                platform="cli",
                reasoningLevel="low",
                contextUsed=100_000,
                contextMax=272_000,
            )

            snapshot = collector.build_snapshot(
                root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["model"], "high-pressure-model")
            self.assertEqual(profile["platform"], "tui")
            self.assertEqual(profile["reasoningLevel"], "high")
            self.assertEqual(profile["contextUsed"], 250_000)
            self.assertEqual(profile["contextMax"], 272_000)
            self.assertEqual(profile["contextPercent"], 92)

    def test_snapshot_ignores_fractional_near_zero_context_values(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "fractional-context",
                contextUsed=0.25,
                contextMax=0.5,
            )

            snapshot = collector.build_snapshot(
                root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertNotIn("contextUsed", profile)
            self.assertNotIn("contextMax", profile)
            self.assertNotIn("contextPercent", profile)

    def test_snapshot_breaks_rounded_context_ties_by_exact_ratio(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            higher_max = (1 << 53) - 1
            higher_used = (higher_max - 1) // 2
            lower_max = higher_max - 2
            lower_used = (lower_max - 1) // 2
            self.assertEqual(lower_used / lower_max, higher_used / higher_max)
            write_record(
                root,
                "coder",
                "a-lower-ratio",
                sessionId="session-coder",
                contextUsed=lower_used,
                contextMax=lower_max,
            )
            write_record(
                root,
                "coder",
                "b-higher-ratio",
                sessionId="session-coder",
                contextUsed=higher_used,
                contextMax=higher_max,
            )

            lower_path = root / "events" / "coder" / "a-lower-ratio.json"
            higher_path = root / "events" / "coder" / "b-higher-ratio.json"
            with mock.patch.object(
                collector, "_event_json_paths", return_value=[lower_path, higher_path]
            ):
                snapshot = collector.build_snapshot(
                    root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
                )

            profile = snapshot["onlineProfiles"][0]
            self.assertEqual(profile["contextUsed"], higher_used)
            self.assertEqual(profile["contextMax"], higher_max)
            self.assertEqual(profile["contextPercent"], 50)

    def test_snapshot_ignores_context_beyond_json_safe_integer_range(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root,
                "coder",
                "huge-context",
                contextUsed=10**1000,
                contextMax=1,
            )

            snapshot = collector.build_snapshot(
                root, now=125.0, process_alive=lambda *_: True, online_sessions=[]
            )

            profile = snapshot["onlineProfiles"][0]
            self.assertNotIn("contextUsed", profile)
            self.assertNotIn("contextMax", profile)
            self.assertNotIn("contextPercent", profile)

    def test_prune_removes_only_old_terminal_records(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "running", state="running")
            for index in range(4):
                write_record(
                    root,
                    "coder",
                    f"done-{index}",
                    state="succeeded",
                    finishedAt=float(index),
                    durationSec=10.0,
                )

            self.assertEqual(collector.prune(root, keep_terminal=2), 2)
            remaining = {path.stem for path in (root / "events/coder").glob("*.json")}
            self.assertEqual(remaining, {"running", "done-2", "done-3"})

    def test_prune_ignores_valid_non_object_json_records(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "events/coder"
            directory.mkdir(parents=True)
            (directory / "array.json").write_text("[]")
            write_record(
                root,
                "coder",
                "done",
                state="succeeded",
                finishedAt=1.0,
                durationSec=1.0,
            )

            self.assertEqual(collector.prune(root, keep_terminal=0), 1)
            self.assertTrue((directory / "array.json").exists())

    def test_prune_removes_acknowledgements_for_deleted_records(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                event_id = f"done-{index}"
                write_record(
                    root,
                    "coder",
                    event_id,
                    state="succeeded",
                    finishedAt=float(index),
                    durationSec=1.0,
                )
                collector.acknowledge(root, event_id)

            collector.prune(root, keep_terminal=1)

            self.assertEqual(collector._acknowledged(root), {"done-2"})

    def test_prune_revalidates_selected_record_under_event_lock_before_unlink(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "selected", state="succeeded",
                startedAt=1.0, finishedAt=2.0, durationSec=1.0,
            )
            collector.acknowledge(root, "selected")
            original_lock = collector._event_lock
            interleaved = False

            @contextmanager
            def interleaving_lock(lock_root, path):
                nonlocal interleaved
                with original_lock(lock_root, path):
                    if not interleaved:
                        interleaved = True
                        write_record(
                            lock_root, "coder", "selected", state="running",
                            startedAt=10.0, updatedAt=10.0,
                        )
                    yield

            with mock.patch.object(collector, "_event_lock", interleaving_lock):
                deleted = collector.prune(root, keep_terminal=0)

            self.assertTrue(interleaved)
            self.assertEqual(deleted, 0)
            self.assertEqual(
                json.loads((root / "events/coder/selected.json").read_text())["state"],
                "running",
            )
            self.assertIn("selected", collector._acknowledged(root))

    def test_prune_revalidates_that_rewritten_terminal_record_is_still_eligible(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "selected", state="succeeded",
                startedAt=1.0, finishedAt=2.0, durationSec=1.0,
            )
            write_record(
                root, "coder", "retained", state="succeeded",
                startedAt=90.0, finishedAt=100.0, durationSec=10.0,
            )
            original_lock = collector._event_lock

            @contextmanager
            def interleaving_lock(lock_root, path):
                with original_lock(lock_root, path):
                    write_record(
                        lock_root, "coder", "selected", state="succeeded",
                        startedAt=1.0, finishedAt=200.0, durationSec=199.0,
                    )
                    yield

            with mock.patch.object(collector, "_event_lock", interleaving_lock):
                deleted = collector.prune(root, keep_terminal=1)

            self.assertEqual(deleted, 0)
            self.assertTrue((root / "events/coder/selected.json").exists())

    def test_concurrent_acknowledgements_are_not_lost(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            barrier = threading.Barrier(16)

            def worker(index):
                barrier.wait()
                collector.acknowledge(root, f"event-{index}")

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(
                collector._acknowledged(root),
                {f"event-{index}" for index in range(16)},
            )

    def test_acknowledge_rejects_event_ids_that_record_validation_rejects(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"

            with self.assertRaises(ValueError):
                collector.acknowledge(root, "x" * 129)

            self.assertFalse((root / "consumer.json").exists())

    def test_acknowledgements_do_not_drop_events_at_an_arbitrary_hash_order_limit(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(550):
                collector.acknowledge(root, f"event-{index:04d}")

            self.assertEqual(len(collector._acknowledged(root)), 550)
            self.assertIn("event-0000", collector._acknowledged(root))
            self.assertIn("event-0549", collector._acknowledged(root))

    def test_notification_acknowledgement_prevents_replay(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "historic", state="succeeded",
                startedAt=30.0, updatedAt=40.0, finishedAt=40.0, durationSec=10.0,
            )
            collector.initialize(root)
            write_record(
                root, "coder", "new", state="succeeded",
                startedAt=43.0, updatedAt=55.0, finishedAt=55.0, durationSec=12.0,
            )
            snapshot = collector.build_snapshot(root, now=60.0, process_alive=lambda *_: True)

            pending = collector.pending_notifications(root, snapshot, now=60.0)
            self.assertEqual([item["eventId"] for item in pending], ["new"])

            collector.acknowledge(root, "new")
            self.assertEqual(collector.pending_notifications(root, snapshot, now=60.0), [])

    def test_malformed_consumer_state_fails_closed(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(parents=True, exist_ok=True)
            (root / "consumer.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                collector._acknowledged(root)

    def test_acknowledge_rejects_symlinked_consumer_and_lock_files(self):
        collector = load_collector()
        for managed_name in ("consumer.json", ".consumer.lock"):
            with self.subTest(managed_name=managed_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "state"
                root.mkdir()
                victim = Path(tmp) / "victim"
                victim.write_text("do not change")
                (root / managed_name).symlink_to(victim)

                with self.assertRaises(OSError):
                    collector.acknowledge(root, "event")

                self.assertEqual(victim.read_text(), "do not change")

    def test_snapshot_ignores_symlinked_event_profiles_and_json_records(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            events = root / "events"
            events.mkdir(parents=True)
            victim_profile = Path(tmp) / "victim-profile"
            write_record(victim_profile.parent, victim_profile.name, "linked-profile")
            (events / "linked").symlink_to(victim_profile, target_is_directory=True)
            real_profile = events / "real"
            real_profile.mkdir()
            victim_json = Path(tmp) / "victim.json"
            victim_json.write_text(json.dumps({
                "schemaVersion": 1, "eventId": "linked-json", "profile": "real",
                "state": "succeeded", "startedAt": 1, "finishedAt": 2, "durationSec": 1,
            }))
            (real_profile / "linked.json").symlink_to(victim_json)

            snapshot = collector.build_snapshot(root, now=10, online_sessions=[])

            self.assertEqual(snapshot["recent"], [])

    def test_pending_notifications_are_not_truncated_by_ui_history_limit(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(21):
                write_record(
                    root,
                    "coder",
                    f"event-{index}",
                    state="succeeded",
                    startedAt=1.0,
                    finishedAt=50.0 + index,
                    durationSec=10.0,
                )
            snapshot = collector.build_snapshot(root, now=100.0, history_limit=20)
            pending = collector.pending_notifications(root, snapshot, now=100.0)
            self.assertEqual(len(snapshot["recent"]), 20)
            self.assertEqual(len(pending), 21)

    def test_prune_preserves_ack_for_record_created_while_waiting_for_consumer_lock(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "old", state="succeeded", finishedAt=1.0, durationSec=1.0)
            original_lock = collector._consumer_lock

            @contextmanager
            def interleaving_lock(lock_root):
                write_record(lock_root, "coder", "late", state="succeeded", finishedAt=2.0, durationSec=1.0)
                collector._write_acknowledged(lock_root, {"late"})
                with original_lock(lock_root):
                    yield

            with mock.patch.object(collector, "_consumer_lock", interleaving_lock):
                collector.prune(root, keep_terminal=10)

            self.assertIn("late", collector._acknowledged(root))

    def test_dead_writer_becomes_stale_after_grace_not_successful(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "dead", startedAt=10.0, updatedAt=20.0)

            snapshot = collector.build_snapshot(
                root,
                now=60.0,
                stale_grace_sec=30,
                process_alive=lambda *_: False,
            )

            self.assertEqual(snapshot["activeBotCount"], 0)
            self.assertEqual(snapshot["recent"][0]["state"], "stale")
            self.assertEqual(snapshot["recent"][0]["exitReason"], "writer_process_exited")

    def test_stale_transition_is_persisted_once_for_history_and_pruning(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "dead", startedAt=10.0, updatedAt=20.0)

            first = collector.build_snapshot(
                root,
                now=60.0,
                stale_grace_sec=30,
                process_alive=lambda *_: False,
            )
            second = collector.build_snapshot(
                root,
                now=90.0,
                stale_grace_sec=30,
                process_alive=lambda *_: False,
            )
            persisted = json.loads((root / "events/coder/dead.json").read_text())

            self.assertEqual(persisted["state"], "stale")
            self.assertEqual(persisted["finishedAt"], 60.0)
            self.assertEqual(second["recent"][0]["finishedAt"], 60.0)
            self.assertEqual(first["recent"][0]["durationSec"], 50.0)

    def test_real_completion_wins_a_race_with_stale_detection(self):
        collector = load_collector()
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "omarchy/hermes-bots"
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id="s", turn_id="t")
                path = next((root / "events/coder").glob("*.json"))
                record = json.loads(path.read_text())
                record.update(startedAt=10.0, updatedAt=20.0)
                path.write_text(json.dumps(record))

                started = threading.Event()
                finished = threading.Event()
                writer = None

                def complete_turn():
                    started.set()
                    observer.on_turn_end(session_id="s", turn_id="t", completed=True)
                    finished.set()

                def process_dead(*_):
                    nonlocal writer
                    writer = threading.Thread(target=complete_turn)
                    writer.start()
                    self.assertTrue(started.wait(1))
                    finished.wait(0.1)
                    return False

                collector.build_snapshot(
                    root,
                    now=60.0,
                    stale_grace_sec=30,
                    process_alive=process_dead,
                )
                writer.join(1)

            self.assertFalse(writer.is_alive())
            self.assertEqual(json.loads(path.read_text())["state"], "succeeded")

    def test_snapshot_counts_active_sessions_and_keeps_profile_summary(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "events/bad"
            directory.mkdir(parents=True)
            (directory / "missing-profile.json").write_text(
                json.dumps({"schemaVersion": 1, "eventId": "bad", "state": "running"})
            )
            (directory / "bad-time.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "eventId": "bad-time",
                        "profile": "bad",
                        "state": "running",
                        "startedAt": "not-a-number",
                    }
                )
            )
            (directory / "bad-pid.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "eventId": "bad-pid",
                        "profile": "bad",
                        "state": "running",
                        "startedAt": 1.0,
                        "updatedAt": 1.0,
                        "writerPid": "not-a-pid",
                    }
                )
            )
            for event_id, bad_pid in (("fractional-pid", 1.9), ("boolean-pid", True)):
                (directory / f"{event_id}.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "eventId": event_id,
                            "profile": "bad",
                            "state": "running",
                            "startedAt": 1.0,
                            "updatedAt": 1.0,
                            "writerPid": bad_pid,
                        }
                    )
                )
            write_record(root, "coder", "one", startedAt=90.0)
            write_record(root, "coder", "two", startedAt=95.0)
            write_record(
                root,
                "researcher",
                "done",
                state="succeeded",
                startedAt=50.0,
                finishedAt=80.0,
                durationSec=30.0,
            )

            snapshot = collector.build_snapshot(root, now=110.0, process_alive=lambda *_: True)

            self.assertEqual(snapshot["activeBotCount"], 2)
            self.assertEqual(snapshot["activeTurnCount"], 2)
            self.assertEqual(snapshot["profiles"][0]["profile"], "coder")
            self.assertEqual(snapshot["profiles"][0]["activeTurnCount"], 2)
            self.assertEqual(snapshot["profiles"][0]["runningForSec"], 20.0)
            self.assertEqual(snapshot["recent"][0]["eventId"], "done")

    def test_snapshot_rejects_record_profile_that_does_not_match_containing_directory(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "forged-profile", state="succeeded",
                finishedAt=80.0, durationSec=30.0,
            )
            path = root / "events/coder/forged-profile.json"
            record = json.loads(path.read_text())
            record["profile"] = "researcher"
            path.write_text(json.dumps(record))

            snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(snapshot["recent"], [])

    def test_snapshot_rejects_record_event_id_that_does_not_match_filename(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "record-name", eventId="impersonated-event",
                state="succeeded", finishedAt=80.0, durationSec=30.0,
            )

            snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(snapshot["recent"], [])

    def test_snapshot_rejects_running_timestamp_beyond_five_second_clock_skew(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "within-skew", startedAt=100.0, updatedAt=105.0)
            write_record(root, "coder", "beyond-skew", startedAt=100.0, updatedAt=105.001)

            snapshot = collector.build_snapshot(
                root, now=100.0, process_alive=lambda *_: True, online_sessions=[],
            )

            self.assertEqual(snapshot["activeTurnCount"], 1)
            self.assertEqual(snapshot["profiles"][0]["profile"], "coder")

    def test_snapshot_rejects_future_started_at_timestamp(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "future-start", startedAt=105.001, updatedAt=100.0)

            snapshot = collector.build_snapshot(
                root, now=100.0, process_alive=lambda *_: True, online_sessions=[],
            )

            self.assertEqual(snapshot["activeTurnCount"], 0)

    def test_snapshot_rejects_oversized_numeric_timestamp_without_crashing(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "oversized-start", startedAt=10**1000)

            snapshot = collector.build_snapshot(
                root, now=100.0, process_alive=lambda *_: True, online_sessions=[]
            )

            self.assertEqual(snapshot["activeTurnCount"], 0)

    def test_snapshot_rejects_future_finished_at_timestamp(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(
                root, "coder", "future-finish", state="succeeded",
                startedAt=90.0, finishedAt=105.001, durationSec=10.0,
            )

            snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(snapshot["recent"], [])

    def test_snapshot_filters_profiles_without_leaking_other_history(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_record(root, "coder", "active", startedAt=90.0)
            write_record(
                root,
                "researcher",
                "done",
                state="succeeded",
                startedAt=50.0,
                finishedAt=80.0,
                durationSec=30.0,
            )

            snapshot = collector.build_snapshot(
                root,
                now=110.0,
                process_alive=lambda *_: True,
                profile_filter={"coder"},
            )

            self.assertEqual([row["profile"] for row in snapshot["profiles"]], ["coder"])
            self.assertEqual(snapshot["recent"], [])


if __name__ == "__main__":
    unittest.main()
