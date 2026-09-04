import importlib.util
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
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


def write_session_db(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "id", "title", "source", "started_at", "ended_at", "last_activity_at",
        "archived", "hidden", "message_count", "end_reason", "model_config",
        "parent_session_id", "session_key",
    )
    defaults = {
        "title": None,
        "source": "cli",
        "started_at": 1.0,
        "ended_at": None,
        "last_activity_at": None,
        "archived": 0,
        "hidden": 0,
        "message_count": 1,
        "end_reason": None,
        "model_config": None,
        "parent_session_id": None,
        "session_key": None,
    }
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                source TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                last_activity_at REAL,
                archived INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                end_reason TEXT,
                model_config TEXT,
                parent_session_id TEXT,
                session_key TEXT
            )
            """
        )
        for row in rows:
            values = {**defaults, **row}
            database.execute(
                f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )


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
    def test_persistent_collector_arms_watches_before_initial_reconciliation(self):
        collector = load_collector()
        events = []

        class StopWatch(Exception):
            pass

        class Monitor:
            def refresh(self, _paths):
                events.append("arm")
                return True

            def close(self):
                events.append("close")

        def stop_after_scan(*_args):
            events.append("scan")
            raise StopWatch()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(collector, "FilesystemChangeMonitor", return_value=Monitor()),
            mock.patch.object(collector, "configure_privacy"),
            mock.patch.object(collector, "_collect_health", side_effect=stop_after_scan),
        ):
            args = mock.Mock(show_work_description=True)
            with self.assertRaises(StopWatch):
                collector.watch_snapshots(Path(tmp) / "state", args)

        self.assertEqual(events[:2], ["arm", "scan"])

    def test_inotify_self_move_drops_the_stale_watch_mapping(self):
        collector = load_collector()
        monitor = collector.FilesystemChangeMonitor.__new__(
            collector.FilesystemChangeMonitor
        )
        watched = Path("/tmp/watched")
        monitor._fd = 123
        monitor._paths = {watched: 5}
        monitor._descriptors = {5: watched}
        payload = collector._INOTIFY_EVENT.pack(5, 0x00000800, 0, 0)

        with mock.patch.object(
            collector.os,
            "read",
            side_effect=[payload, BlockingIOError()],
        ):
            changed, intact = monitor.drain()

        self.assertTrue(changed)
        self.assertFalse(intact)
        self.assertEqual(monitor._paths, {})
        self.assertEqual(monitor._descriptors, {})

    def test_public_projection_bounds_legacy_record_strings(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(
                root,
                "default",
                "legacy",
                model="m" * 500,
                platform="p" * 300,
                workDescription="w" * 500,
            )

            snapshot = collector.build_snapshot(
                root,
                now=110,
                online_sessions=[],
                process_alive=lambda _pid, _start: True,
            )

            session = snapshot["onlineProfiles"][0]
            self.assertEqual(len(session["model"]), 200)
            self.assertEqual(len(session["platform"]), 100)
            self.assertNotIn("workDescription", session)

            write_record(
                root,
                "coder",
                "legacy-context",
                state="succeeded",
                finishedAt=109,
                durationSec=9,
                model="m" * 500,
                platform="p" * 300,
                contextUsed=50,
                contextMax=100,
                contextPercent=50,
                contextConfirmed=True,
            )
            snapshot = collector.build_snapshot(
                root,
                now=110,
                online_sessions=[
                    {
                        "profile": "coder",
                        "pid": 10,
                        "processStart": "20",
                        "runningForSec": 9,
                    }
                ],
            )
            context_session = next(
                row for row in snapshot["onlineProfiles"] if row["profile"] == "coder"
            )
            self.assertEqual(len(context_session["model"]), 200)
            self.assertEqual(len(context_session["platform"]), 100)

    def test_public_snapshot_bounds_arrays_before_qml_projection(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"HERMES_ROOT": str(Path(tmp) / "hermes")},
        ):
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            profiles_root = hermes_root / "profiles"
            profiles_root.mkdir(parents=True)
            for index in range(110):
                (profiles_root / f"profile_{index}").mkdir()
            online = [
                {
                    "profile": f"profile_{index}",
                    "pid": 1000 + index,
                    "processStart": str(2000 + index),
                    "runningForSec": 1,
                }
                for index in range(110)
            ]

            snapshot = collector.build_snapshot(root, now=10, online_sessions=online)

            self.assertEqual(len(snapshot["onlineProfiles"]), 100)
            self.assertEqual(snapshot["onlineSessionCount"], 110)
            self.assertEqual(snapshot["onlineBotCount"], 110)
            self.assertEqual(len(snapshot["availableProfiles"]), 100)

    def test_snapshot_serialization_enforces_a_total_payload_budget(self):
        collector = load_collector()
        snapshot = {
            "schemaVersion": 1,
            "generatedAt": 1.0,
            "hermesRoot": "/tmp/hermes",
            "activeBotCount": 0,
            "activeTurnCount": 0,
            "onlineBotCount": 0,
            "onlineSessionCount": 0,
            "onlineProfiles": [],
            "availableProfiles": [
                {
                    "profile": f"profile-{index}",
                    "avatarUrl": "file:///" + "x" * 4000,
                }
                for index in range(100)
            ],
            "profiles": [],
            "recent": [],
            "recentSessions": [
                {
                    "sessionId": f"session-{index}",
                    "profile": "default",
                    "description": "y" * 4000,
                    "startedAt": 1.0,
                    "updatedAt": 2.0,
                }
                for index in range(100)
            ],
            "pendingNotifications": [
                {
                    "eventId": "oldest-pending",
                    "profile": "default",
                    "state": "succeeded",
                    "durationSec": 1.0,
                    "finishedAt": 2.0,
                }
            ],
        }

        self.assertTrue(
            hasattr(collector, "serialize_snapshot"),
            "collector does not enforce a total public snapshot budget",
        )
        payload = collector.serialize_snapshot(snapshot)

        self.assertLessEqual(len(payload.encode("utf-8")), 256 * 1024)
        decoded = json.loads(payload)
        self.assertTrue(
            all("avatarUrl" not in profile for profile in decoded["availableProfiles"])
        )
        self.assertEqual(
            decoded["pendingNotifications"][0]["eventId"],
            "oldest-pending",
        )

    def test_clear_history_removes_only_terminal_watcher_records(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(root, "coder", "running")
            write_record(
                root,
                "coder",
                "terminal",
                state="failed",
                finishedAt=200.0,
                durationSec=100.0,
            )
            collector.acknowledge(root, "terminal")
            collector.configure_privacy(root, show_work_description=False)
            process_dir = root / "processes/coder"
            process_dir.mkdir(parents=True)
            marker = process_dir / "observer.json"
            marker.write_text("{}")

            self.assertTrue(
                hasattr(collector, "clear_history"),
                "collector does not provide explicit history clearing",
            )
            removed = collector.clear_history(root)

            self.assertEqual(removed, 1)
            self.assertTrue((root / "events/coder/running.json").exists())
            self.assertFalse((root / "events/coder/terminal.json").exists())
            self.assertTrue((root / "privacy.json").exists())
            self.assertTrue(marker.exists())
            self.assertNotIn("terminal", collector._acknowledged(root))

    def test_clear_history_surfaces_partial_deletion_failure(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(
                root,
                "coder",
                "first",
                state="failed",
                finishedAt=200,
                durationSec=100,
            )
            write_record(
                root,
                "coder",
                "second",
                state="succeeded",
                finishedAt=201,
                durationSec=101,
            )
            original = collector.ManagedTree.unlink_regular

            def fail_second(tree, parts):
                if parts[-1] == "second.json":
                    raise OSError("simulated deletion failure")
                return original(tree, parts)

            with mock.patch.object(collector.ManagedTree, "unlink_regular", fail_second):
                with self.assertRaises(OSError):
                    collector.clear_history(root)

            self.assertFalse((root / "events/coder/first.json").exists())
            self.assertTrue((root / "events/coder/second.json").exists())

    def test_disabling_recent_titles_skips_all_session_database_access(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            hermes_root.mkdir()
            with (
                mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}),
                mock.patch.object(collector, "_profile_recent_sessions") as recent_reader,
            ):
                self.assertIn(
                    "include_recent_sessions",
                    inspect.signature(collector.build_snapshot).parameters,
                )
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    online_sessions=[],
                    include_recent_sessions=False,
                )

            self.assertEqual(snapshot["recentSessions"], [])
            recent_reader.assert_not_called()

    def test_disabling_work_descriptions_purges_existing_persisted_excerpts(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(root, "coder", "running", workDescription="Private running text")
            write_record(
                root,
                "coder",
                "terminal",
                state="succeeded",
                finishedAt=200.0,
                durationSec=100.0,
                workDescription="Private terminal text",
            )

            self.assertTrue(
                hasattr(collector, "configure_privacy"),
                "collector does not persist the work-description privacy policy",
            )
            collector.configure_privacy(root, show_work_description=False)

            policy = json.loads((root / "privacy.json").read_text())
            self.assertIs(policy["showWorkDescription"], False)
            for path in (root / "events/coder").glob("*.json"):
                self.assertNotIn("workDescription", json.loads(path.read_text()))

    def test_disabled_work_descriptions_are_suppressed_at_projection_boundary(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(
                root,
                "coder",
                "legacy-running",
                workDescription="legacy observer excerpt",
            )

            self.assertIn(
                "include_work_descriptions",
                inspect.signature(collector.build_snapshot).parameters,
            )
            snapshot = collector.build_snapshot(
                root,
                now=110,
                online_sessions=[],
                process_alive=lambda _pid, _start: True,
                include_work_descriptions=False,
            )

            self.assertTrue(snapshot["onlineProfiles"])
            self.assertTrue(
                all("workDescription" not in row for row in snapshot["onlineProfiles"])
            )

    def test_public_snapshot_projects_minimal_dtos_without_internal_record_fields(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            write_record(
                root,
                "coder",
                "public-event",
                state="failed",
                finishedAt=200.0,
                durationSec=100.0,
                taskId="task-private",
                turnId="turn-private",
                writerPid=999,
                writerProcessStart="1234",
                exitReason="raw-provider-reason",
                contextApiRequestId="request-private",
            )

            snapshot = collector.build_snapshot(root, now=201.0, online_sessions=[])
            snapshot["pendingNotifications"] = collector.pending_notifications(
                root,
                snapshot,
                now=201.0,
                min_duration_sec=0,
            )
            snapshot.pop("_notificationCandidates", None)

            expected = {"eventId", "profile", "state", "durationSec", "finishedAt"}
            self.assertEqual(set(snapshot["recent"][0]), expected)
            self.assertEqual(set(snapshot["pendingNotifications"][0]), expected)
            forbidden = {
                "writerPid",
                "writerProcessStart",
                "taskId",
                "turnId",
                "exitReason",
                "contextApiRequestId",
            }

            def keys(value):
                if isinstance(value, dict):
                    return set(value).union(*(keys(item) for item in value.values()))
                if isinstance(value, list):
                    return set().union(*(keys(item) for item in value)) if value else set()
                return set()

            self.assertTrue(forbidden.isdisjoint(keys(snapshot)))

    def test_pending_notification_batch_is_bounded_before_qml_projection(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            candidates = [
                {
                    "eventId": f"event-{index}",
                    "profile": "default",
                    "state": "succeeded",
                    "startedAt": float(index),
                    "finishedAt": float(index + 10),
                    "durationSec": 10.0,
                }
                for index in reversed(range(150))
            ]

            pending = collector.pending_notifications(
                root,
                {"_notificationCandidates": candidates},
                now=200.0,
                min_duration_sec=0,
                max_catchup_age_sec=1000,
            )

            self.assertLessEqual(len(pending), 100)
            self.assertEqual(
                [record["eventId"] for record in pending],
                [f"event-{index}" for index in range(100)],
            )
            collector.acknowledge(root, "event-0")
            next_page = collector.pending_notifications(
                root,
                {"_notificationCandidates": candidates},
                now=200.0,
                min_duration_sec=0,
                max_catchup_age_sec=1000,
            )
            self.assertEqual(next_page[-1]["eventId"], "event-100")

    def test_snapshot_cache_reuses_unchanged_events_avatars_and_session_databases(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            write_record(
                root,
                "default",
                "cached-event",
                state="succeeded",
                finishedAt=900.0,
                durationSec=800.0,
            )
            write_session_db(
                hermes_root / "state.db",
                [{"id": "cached-session", "title": "Cached session"}],
            )
            assets = hermes_root / "assets"
            assets.mkdir()
            (assets / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\ncached")
            self.assertTrue(
                hasattr(collector, "SnapshotCache"),
                "collector does not provide persistent snapshot caching",
            )
            cache = collector.SnapshotCache()
            real_read_json = collector._read_json
            self.assertTrue(
                hasattr(collector, "_read_avatar_header"),
                "avatar cache is not keyed from a validated descriptor",
            )
            real_avatar_header = collector._read_avatar_header
            real_connect = collector.sqlite3.connect
            with (
                mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}),
                mock.patch.object(collector, "_read_json", wraps=real_read_json) as read_json,
                mock.patch.object(
                    collector,
                    "_read_avatar_header",
                    wraps=real_avatar_header,
                ) as avatar_header,
                mock.patch.object(collector.sqlite3, "connect", wraps=real_connect) as connect,
            ):
                first = collector.build_snapshot(root, now=1000.0, cache=cache, online_sessions=[])
                first_counts = (read_json.call_count, avatar_header.call_count, connect.call_count)
                second = collector.build_snapshot(root, now=1001.0, cache=cache, online_sessions=[])

            self.assertEqual(first["recent"][0]["eventId"], "cached-event")
            self.assertEqual(second["recentSessions"][0]["sessionId"], "cached-session")
            self.assertEqual(avatar_header.call_count, 1)
            self.assertEqual(
                (read_json.call_count, avatar_header.call_count, connect.call_count),
                first_counts,
            )

    def test_avatar_cache_refreshes_recent_rows_without_reopening_unchanged_database(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [{"id": "avatar-session", "title": "Avatar session"}],
            )
            assets = hermes_root / "assets"
            assets.mkdir()
            avatar = assets / "avatar.png"
            avatar.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
            cache = collector.SnapshotCache()
            real_connect = collector.sqlite3.connect
            with (
                mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}),
                mock.patch.object(collector.sqlite3, "connect", wraps=real_connect) as connect,
            ):
                first = collector.build_snapshot(root, now=100.0, cache=cache, online_sessions=[])
                original_mtime = avatar.stat().st_mtime_ns
                replacement = assets / "replacement.png"
                replacement.write_bytes(b"\x89PNG\r\n\x1a\nother")
                os.utime(replacement, ns=(original_mtime, original_mtime))
                replacement.replace(avatar)
                second = collector.build_snapshot(root, now=101.0, cache=cache, online_sessions=[])

            self.assertEqual(connect.call_count, 1)
            self.assertNotEqual(
                first["recentSessions"][0]["avatarUrl"],
                second["recentSessions"][0]["avatarUrl"],
            )

    def test_snapshot_lists_only_the_six_latest_titled_local_sessions_across_profiles(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": f"default-{index}",
                        "title": f"Default session {index}",
                        "last_activity_at": float(index),
                    }
                    for index in range(1, 5)
                ],
            )
            write_session_db(
                hermes_root / "profiles" / "coder" / "state.db",
                [
                    {
                        "id": f"coder-{index}",
                        "title": f"Coder session {index}",
                        "last_activity_at": float(index),
                    }
                    for index in range(5, 9)
                ],
            )

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    history_limit=20,
                    online_sessions=[],
                )

            self.assertEqual(len(snapshot["recentSessions"]), 6)
            self.assertEqual(
                [row["sessionId"] for row in snapshot["recentSessions"]],
                ["coder-8", "coder-7", "coder-6", "coder-5", "default-4", "default-3"],
            )
            self.assertEqual(snapshot["recentSessions"][0]["description"], "Coder session 8")
            self.assertEqual(snapshot["recentSessions"][0]["profile"], "coder")
            self.assertEqual(snapshot["recentSessions"][0]["recentAt"], 8.0)

    def test_recent_sessions_resolve_a_relative_hermes_root_and_expose_the_exact_launch_root(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [{"id": "relative-1", "title": "Relative root session"}],
            )
            try:
                os.chdir(tmp)
                with mock.patch.dict(os.environ, {"HERMES_ROOT": "hermes"}):
                    snapshot = collector.build_snapshot(
                        Path(tmp) / "state",
                        now=100.0,
                        online_sessions=[],
                    )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(snapshot["hermesRoot"], str(hermes_root))
            self.assertEqual(
                [row["sessionId"] for row in snapshot["recentSessions"]],
                ["relative-1"],
            )

    def test_recent_sessions_skip_malformed_and_non_finite_timestamps(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {"id": "bad-time", "title": "Bad time", "last_activity_at": "bad"},
                    *[
                        {
                            "id": f"infinite-time-{index}",
                            "title": f"Infinite time {index}",
                            "last_activity_at": float("inf"),
                        }
                        for index in range(6)
                    ],
                    {"id": "valid-time", "title": "Valid time", "last_activity_at": 12.0},
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["valid-time"])
            json.dumps(rows, allow_nan=False)

    def test_recent_sessions_filter_invalid_ids_before_applying_the_limit(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    *[
                        {
                            "id": f"invalid\x00id-{index}",
                            "title": f"Invalid ID {index}",
                            "last_activity_at": 100.0 + index,
                        }
                        for index in range(6)
                    ],
                    {"id": "valid-id", "title": "Valid ID", "last_activity_at": 12.0},
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["valid-id"])

    def test_recent_sessions_match_hermes_user_visible_child_boundary(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": "root-session",
                        "title": "Root session",
                        "last_activity_at": 1.0,
                    },
                    {
                        "id": "delegate-session",
                        "title": "Delegate session",
                        "parent_session_id": "root-session",
                        "model_config": json.dumps({"_delegate_from": "root-session"}),
                        "last_activity_at": 10.0,
                    },
                    {
                        "id": "internal-child",
                        "title": "Internal child",
                        "parent_session_id": "root-session",
                        "last_activity_at": 9.0,
                    },
                    {
                        "id": "branch-session",
                        "title": "Branch session",
                        "parent_session_id": "root-session",
                        "model_config": json.dumps({"_branched_from": "root-session"}),
                        "last_activity_at": 8.0,
                    },
                    {
                        "id": "reset-session",
                        "title": "Reset session",
                        "parent_session_id": "root-session",
                        "model_config": json.dumps({"_reset_from": "root-session"}),
                        "last_activity_at": 7.0,
                    },
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual(
                [row["sessionId"] for row in rows],
                ["branch-session", "reset-session", "root-session"],
            )

    def test_recent_sessions_surface_the_compression_tip_not_its_predecessor(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": "compression-root",
                        "title": "Compressed conversation",
                        "end_reason": "compression",
                        "last_activity_at": 5.0,
                    },
                    {
                        "id": "compression-tip",
                        "title": "Compressed conversation",
                        "parent_session_id": "compression-root",
                        "last_activity_at": 10.0,
                    },
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["compression-tip"])

    def test_recent_sessions_keep_a_compression_root_until_its_tip_exists(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": "compression-root",
                        "title": "Compressed conversation",
                        "end_reason": "compression",
                        "last_activity_at": 5.0,
                    }
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["compression-root"])

    def test_recent_sessions_choose_the_live_compression_tip_over_a_stale_sibling(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": "compression-root",
                        "title": "Compressed conversation",
                        "end_reason": "compression",
                        "last_activity_at": 5.0,
                    },
                    {
                        "id": "live-tip",
                        "title": "Compressed conversation",
                        "parent_session_id": "compression-root",
                        "last_activity_at": 10.0,
                    },
                    {
                        "id": "stale-sibling",
                        "title": "Stale sibling",
                        "parent_session_id": "compression-root",
                        "ended_at": 30.0,
                        "end_reason": "ws_orphan_reap",
                        "last_activity_at": 30.0,
                    },
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["live-tip"])

    def test_recent_sessions_ignore_malformed_compression_siblings_before_ranking(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [
                    {
                        "id": "compression-root",
                        "title": "Compressed conversation",
                        "end_reason": "compression",
                        "last_activity_at": 5.0,
                    },
                    {
                        "id": "infinite-sibling",
                        "title": "Infinite sibling",
                        "parent_session_id": "compression-root",
                        "last_activity_at": float("inf"),
                    },
                    {
                        "id": "text-sibling",
                        "title": "Text sibling",
                        "parent_session_id": "compression-root",
                        "last_activity_at": "bad",
                    },
                    {
                        "id": "control-title-sibling",
                        "title": "Control\x00Title",
                        "parent_session_id": "compression-root",
                        "last_activity_at": 20.0,
                    },
                    {
                        "id": "invalid/id-sibling",
                        "title": "Invalid ID sibling",
                        "parent_session_id": "compression-root",
                        "last_activity_at": 25.0,
                    },
                    {
                        "id": "hidden-sibling",
                        "title": "Hidden sibling",
                        "parent_session_id": "compression-root",
                        "hidden": 1,
                        "last_activity_at": 30.0,
                    },
                    {
                        "id": "archived-sibling",
                        "title": "Archived sibling",
                        "parent_session_id": "compression-root",
                        "archived": 1,
                        "last_activity_at": 35.0,
                    },
                    {
                        "id": "empty-sibling",
                        "title": "Empty sibling",
                        "parent_session_id": "compression-root",
                        "message_count": 0,
                        "last_activity_at": 40.0,
                    },
                    {
                        "id": "remote-sibling",
                        "title": "Remote sibling",
                        "parent_session_id": "compression-root",
                        "source": "telegram",
                        "last_activity_at": 45.0,
                    },
                    {
                        "id": "inherited-delegate-sibling",
                        "title": "Inherited delegate sibling",
                        "parent_session_id": "compression-root",
                        "model_config": json.dumps({"_delegate_from": "another-parent"}),
                        "last_activity_at": 50.0,
                    },
                    {
                        "id": "valid-tip",
                        "title": "Compressed conversation",
                        "parent_session_id": "compression-root",
                        "last_activity_at": 10.0,
                    },
                ],
            )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["valid-tip"])

    def test_recent_session_reads_do_not_create_missing_wal_sidecars(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            database_path = hermes_root / "state.db"
            write_session_db(
                database_path,
                [{"id": "wal-session", "title": "WAL session"}],
            )
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("PRAGMA journal_mode = WAL")
                database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            sidecars = [Path(str(database_path) + suffix) for suffix in ("-wal", "-shm")]
            for sidecar in sidecars:
                sidecar.unlink(missing_ok=True)

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["wal-session"])
            self.assertFalse(any(sidecar.exists() for sidecar in sidecars))

    def test_recent_session_read_cannot_recreate_sidecars_removed_before_connect(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            database_path = hermes_root / "state.db"
            write_session_db(
                database_path,
                [{"id": "race-session", "title": "Race session"}],
            )
            writer = sqlite3.connect(database_path)
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                ("Race session", "race-session"),
            )
            writer.commit()
            sidecars = [Path(str(database_path) + suffix) for suffix in ("-wal", "-shm")]
            self.assertTrue(all(sidecar.exists() for sidecar in sidecars))
            real_connect = sqlite3.connect
            writer_closed = False

            def close_writer_then_connect(*args, **kwargs):
                nonlocal writer_closed
                writer.close()
                writer_closed = True
                self.assertFalse(any(sidecar.exists() for sidecar in sidecars))
                return real_connect(*args, **kwargs)

            try:
                with mock.patch.object(
                    collector.sqlite3,
                    "connect",
                    side_effect=close_writer_then_connect,
                ):
                    rows = collector._profile_recent_sessions(
                        "default",
                        hermes_root=hermes_root,
                        limit=6,
                    )
            finally:
                if not writer_closed:
                    writer.close()

            self.assertEqual([row["sessionId"] for row in rows], ["race-session"])
            self.assertFalse(any(sidecar.exists() for sidecar in sidecars))

    def test_recent_sessions_preserve_started_at_as_the_equal_recency_tiebreaker(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            write_session_db(
                hermes_root / "state.db",
                [{"id": "z-default", "title": "Default", "started_at": 1.0, "last_activity_at": 10.0}],
            )
            write_session_db(
                hermes_root / "profiles" / "coder" / "state.db",
                [{"id": "a-coder", "title": "Coder", "started_at": 2.0, "last_activity_at": 10.0}],
            )

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(root, now=100.0, online_sessions=[])

            self.assertEqual(
                [row["sessionId"] for row in snapshot["recentSessions"]],
                ["a-coder", "z-default"],
            )

    def test_recent_sessions_support_the_legacy_minimum_session_schema(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            database_path = hermes_root / "state.db"
            database_path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database_path)) as database, database:
                database.execute(
                    """
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        source TEXT NOT NULL,
                        started_at REAL NOT NULL
                    )
                    """
                )
                database.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                    ("legacy-1", "Legacy session", "cli", 5.0),
                )

            rows = collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
            )

            self.assertEqual([row["sessionId"] for row in rows], ["legacy-1"])

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

    def test_snapshot_profile_filter_precedes_avatar_and_recent_session_projection(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            for profile in ("coder", "researcher"):
                (hermes_root / "profiles" / profile).mkdir(parents=True)
            avatar_reads = []
            session_reads = []

            def avatar_url(profile, **_kwargs):
                avatar_reads.append(profile)
                return f"file:///{profile}.png"

            def recent_sessions(profile, **_kwargs):
                session_reads.append(profile)
                return [
                    {
                        "profile": profile,
                        "sessionId": f"{profile}-session",
                        "description": f"{profile} session",
                        "source": "cli",
                        "recentAt": 1.0,
                        "_startedAt": 1.0,
                    }
                ]

            with (
                mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}),
                mock.patch.object(collector, "_profile_avatar_url", side_effect=avatar_url),
                mock.patch.object(
                    collector, "_profile_recent_sessions", side_effect=recent_sessions
                ),
            ):
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    profile_filter={"coder"},
                    online_sessions=[],
                )

            self.assertEqual(snapshot["availableProfiles"], [
                {"profile": "coder", "avatarUrl": "file:///coder.png"}
            ])
            self.assertEqual(avatar_reads, ["coder"])
            self.assertEqual(session_reads, ["coder"])
            self.assertEqual(
                [row["profile"] for row in snapshot["recentSessions"]],
                ["coder"],
            )

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
            self.assertTrue(all(row["observerLoaded"] is False for row in snapshot["onlineProfiles"]))
            self.assertTrue(all("sessionCount" not in row for row in snapshot["onlineProfiles"]))

    def test_snapshot_marks_a_matching_observer_process_handshake_as_loaded(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = "coder"
            pid = 11
            process_start = "110"
            identity = hashlib.sha256(
                "\0".join((profile, str(pid), process_start)).encode("utf-8")
            ).hexdigest()
            directory = root / "processes" / profile
            directory.mkdir(parents=True)
            (directory / f"{identity}.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "profile": profile,
                        "writerPid": pid,
                        "writerProcessStart": process_start,
                    }
                )
            )

            snapshot = collector.build_snapshot(
                root,
                now=100.0,
                online_sessions=[
                    {
                        "profile": profile,
                        "pid": pid,
                        "processStart": process_start,
                        "runningForSec": 50.0,
                    }
                ],
            )

            self.assertIs(snapshot["onlineProfiles"][0]["observerLoaded"], True)
            self.assertIs(
                snapshot["onlineProfiles"][0].get("workDescriptionPolicyLoaded", False),
                False,
            )
            (directory / f"{identity}.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "profile": profile,
                        "writerPid": pid,
                        "writerProcessStart": process_start,
                        "capabilities": ["work-description-policy-v1"],
                    }
                )
            )
            upgraded = collector.build_snapshot(
                root,
                now=101.0,
                online_sessions=[
                    {
                        "profile": profile,
                        "pid": pid,
                        "processStart": process_start,
                        "runningForSec": 51.0,
                    }
                ],
            )
            self.assertIs(
                upgraded["onlineProfiles"][0].get("workDescriptionPolicyLoaded"),
                True,
            )

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

    def test_snapshot_rejects_a_hardlinked_profile_avatar(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            assets = hermes_root / "profiles" / "coder" / "assets"
            assets.mkdir(parents=True)
            outside = Path(tmp) / "outside.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate-image")
            os.link(outside, assets / "avatar.png")

            with mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}):
                snapshot = collector.build_snapshot(
                    root,
                    now=100.0,
                    online_sessions=[{"profile": "coder", "pid": 11, "runningForSec": 50.0}],
                )

            self.assertNotIn("avatarUrl", snapshot["onlineProfiles"][0])

    def test_avatar_parent_swap_cannot_redirect_the_validated_file(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            hermes_root = Path(tmp) / "hermes"
            assets = hermes_root / "profiles/coder/assets"
            assets.mkdir(parents=True)
            avatar = assets / "avatar.png"
            avatar.write_bytes(b"\x89PNG\r\n\x1a\nsafe")
            safe_inode = avatar.stat().st_ino
            outside = Path(tmp) / "outside-assets"
            outside.mkdir()
            (outside / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nprivate")
            moved = Path(tmp) / "moved-assets"
            original_stat = Path.stat
            swapped = False

            def swap_after_assets_check(path, *args, **kwargs):
                nonlocal swapped
                metadata = original_stat(path, *args, **kwargs)
                if path == assets and not swapped:
                    swapped = True
                    assets.rename(moved)
                    assets.symlink_to(outside, target_is_directory=True)
                return metadata

            with (
                mock.patch.dict(os.environ, {"HERMES_ROOT": str(hermes_root)}),
                mock.patch.object(Path, "stat", swap_after_assets_check),
            ):
                avatar_url = collector._profile_avatar_url(
                    "coder",
                    hermes_root=hermes_root,
                )

            self.assertIn(f"-{safe_inode}-", avatar_url)

    def test_avatar_swap_after_open_returns_the_descriptor_pinned_path(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            assets = hermes_root / "profiles/coder/assets"
            assets.mkdir(parents=True)
            (assets / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nsafe")
            outside = Path(tmp) / "outside-assets"
            outside.mkdir()
            (outside / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nprivate")
            moved = Path(tmp) / "moved-assets"
            original_read = collector.os.read
            swapped = False

            def swap_after_open(descriptor, size):
                nonlocal swapped
                payload = original_read(descriptor, size)
                if not swapped:
                    swapped = True
                    assets.rename(moved)
                    assets.symlink_to(outside, target_is_directory=True)
                return payload

            with mock.patch.object(collector.os, "read", side_effect=swap_after_open):
                avatar_url = collector._profile_avatar_url(
                    "coder",
                    hermes_root=hermes_root,
                )

            self.assertIn("/moved-assets/avatar.png", avatar_url)
            self.assertNotIn("/outside-assets/", avatar_url)

    def test_recent_session_database_is_opened_through_a_validated_descriptor(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            database_path = hermes_root / "state.db"
            hermes_root.mkdir()
            write_session_db(
                database_path,
                [
                    {
                        "id": "session-safe",
                        "title": "Safe session",
                        "source": "cli",
                        "started_at": 10,
                    }
                ],
            )
            real_connect = collector.sqlite3.connect
            with mock.patch.object(
                collector.sqlite3,
                "connect",
                wraps=real_connect,
            ) as connect:
                rows = collector._profile_recent_sessions(
                    "default",
                    hermes_root=hermes_root,
                    limit=6,
                )

            self.assertEqual([row["sessionId"] for row in rows], ["session-safe"])
            database_uri = connect.call_args.args[0]
            self.assertTrue(database_uri.startswith("file:///proc/self/fd/"))

    def test_recent_session_database_rejects_hardlinks_without_leaking_descriptors(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            hermes_root.mkdir()
            outside = Path(tmp) / "outside.db"
            write_session_db(
                outside,
                [
                    {
                        "id": "session-private",
                        "title": "Private session",
                        "source": "cli",
                        "started_at": 10,
                    }
                ],
            )
            os.link(outside, hermes_root / "state.db")
            descriptor_count = len(list(Path("/proc/self/fd").iterdir()))

            for _ in range(10):
                self.assertEqual(
                    collector._profile_recent_sessions(
                        "default",
                        hermes_root=hermes_root,
                        limit=6,
                    ),
                    [],
                )

            self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), descriptor_count)

    def test_recent_session_cache_revalidates_profile_directory_before_a_hit(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            profile_dir = hermes_root / "profiles/coder"
            profile_dir.mkdir(parents=True)
            write_session_db(
                profile_dir / "state.db",
                [
                    {
                        "id": "session-cached",
                        "title": "Cached session",
                        "source": "cli",
                        "started_at": 10,
                    }
                ],
            )
            cache = collector.SnapshotCache()
            self.assertTrue(
                collector._profile_recent_sessions(
                    "coder",
                    hermes_root=hermes_root,
                    limit=6,
                    cache=cache,
                )
            )
            moved = Path(tmp) / "moved-coder"
            profile_dir.rename(moved)
            profile_dir.symlink_to(moved, target_is_directory=True)

            rows = collector._profile_recent_sessions(
                "coder",
                hermes_root=hermes_root,
                limit=6,
                cache=cache,
            )

            self.assertEqual(rows, [])

    def test_recent_session_cache_keys_the_descriptor_opened_after_a_path_swap(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            hermes_root.mkdir()
            database_path = hermes_root / "state.db"
            write_session_db(
                database_path,
                [
                    {
                        "id": "session-old",
                        "title": "Old session",
                        "source": "cli",
                        "started_at": 10,
                    }
                ],
            )
            cache = collector.SnapshotCache()
            collector._profile_recent_sessions(
                "default",
                hermes_root=hermes_root,
                limit=6,
                cache=cache,
            )
            replacement = Path(tmp) / "replacement.db"
            write_session_db(
                replacement,
                [
                    {
                        "id": "session-new",
                        "title": "New session",
                        "source": "cli",
                        "started_at": 20,
                    }
                ],
            )

            original_open = collector.os.open

            def swap_before_open(path, flags, *args, **kwargs):
                if path == "state.db" and replacement.exists():
                    os.replace(replacement, database_path)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                collector.os,
                "open",
                side_effect=swap_before_open,
            ):
                rows = collector._profile_recent_sessions(
                    "default",
                    hermes_root=hermes_root,
                    limit=6,
                    cache=cache,
                )

            self.assertEqual([row["sessionId"] for row in rows], ["session-new"])

    def test_recent_session_parent_swap_cannot_redirect_database_open(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            profile_dir = hermes_root / "profiles/coder"
            profile_dir.mkdir(parents=True)
            write_session_db(
                profile_dir / "state.db",
                [
                    {
                        "id": "session-safe",
                        "title": "Safe session",
                        "source": "cli",
                        "started_at": 10,
                    }
                ],
            )
            outside = Path(tmp) / "outside"
            outside.mkdir()
            write_session_db(
                outside / "state.db",
                [
                    {
                        "id": "session-private",
                        "title": "Private session",
                        "source": "cli",
                        "started_at": 20,
                    }
                ],
            )
            moved = Path(tmp) / "moved-coder"
            original_stat = Path.stat
            swapped = False

            def swap_after_profile_check(path, *args, **kwargs):
                nonlocal swapped
                metadata = original_stat(path, *args, **kwargs)
                if path == profile_dir and not swapped:
                    swapped = True
                    profile_dir.rename(moved)
                    profile_dir.symlink_to(outside, target_is_directory=True)
                return metadata

            with mock.patch.object(Path, "stat", swap_after_profile_check):
                rows = collector._profile_recent_sessions(
                    "coder",
                    hermes_root=hermes_root,
                    limit=6,
                    avatar_url="",
                )

            self.assertEqual([row["sessionId"] for row in rows], ["session-safe"])

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

    def test_prune_removes_dead_observer_process_handshakes(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            process_dir = root / "processes/default"
            process_dir.mkdir(parents=True)

            paths = {}
            for pid in (10, 11):
                process_start = str(pid * 10)
                digest = hashlib.sha256(
                    f"default\0{pid}\0{process_start}".encode()
                ).hexdigest()
                path = process_dir / f"{digest}.json"
                path.write_text(json.dumps({
                    "schemaVersion": 1,
                    "profile": "default",
                    "writerPid": pid,
                    "writerProcessStart": process_start,
                    "registeredAt": 100.0,
                }))
                paths[pid] = path

            with mock.patch.object(
                collector,
                "_process_alive",
                side_effect=lambda pid, _start: pid == 11,
            ):
                collector.prune(root)

            self.assertFalse(paths[10].exists())
            self.assertTrue(paths[11].exists())

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

    def test_prune_bounds_terminal_history_by_age_as_well_as_count(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 4_000_000.0
            write_record(
                root,
                "coder",
                "expired",
                state="succeeded",
                finishedAt=now - 31 * 86400,
                durationSec=10.0,
            )
            write_record(
                root,
                "coder",
                "retained",
                state="succeeded",
                finishedAt=now - 86400,
                durationSec=10.0,
            )

            with mock.patch.object(collector.time, "time", return_value=now):
                deleted = collector.prune(
                    root,
                    keep_terminal=100,
                    max_age_sec=30 * 86400,
                )

            self.assertEqual(deleted, 1)
            remaining = {path.stem for path in (root / "events/coder").glob("*.json")}
            self.assertEqual(remaining, {"retained"})

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

    def test_notification_claim_prevents_replay_after_service_restart(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            write_record(
                root, "coder", "claimed", state="succeeded",
                startedAt=30.0, updatedAt=40.0, finishedAt=40.0, durationSec=10.0,
            )
            snapshot = collector.build_snapshot(root, now=60.0, process_alive=lambda *_: True)
            self.assertEqual(
                [item["eventId"] for item in collector.pending_notifications(root, snapshot, now=60.0)],
                ["claimed"],
            )

            self.assertTrue(collector.claim_notification(root, "claimed", now=60.0))

            restarted_collector = load_collector()
            self.assertEqual(
                restarted_collector.pending_notifications(root, snapshot, now=60.0),
                [],
            )
            self.assertFalse(
                restarted_collector.claim_notification(root, "claimed", now=60.0)
            )

    def test_existing_notification_claim_is_not_mistaken_for_delivery(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            self.assertTrue(collector.claim_notification(root, "still-running"))
            runner = mock.Mock(return_value=mock.Mock(returncode=0))

            delivered = collector.deliver_notification(
                root,
                "still-running",
                ["notify-send", "title", "body"],
                run=runner,
            )

            self.assertIsNone(delivered)
            self.assertNotIn("still-running", collector._acknowledged(root))
            runner.assert_not_called()

    def test_stale_notification_claim_can_be_retried(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)

            self.assertTrue(collector.claim_notification(root, "stale-claim", now=100.0))
            self.assertFalse(collector.claim_notification(root, "stale-claim", now=399.0))
            self.assertTrue(collector.claim_notification(root, "stale-claim", now=401.0))

    def test_future_notification_claim_does_not_block_recovery_after_clock_rollback(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)

            self.assertTrue(collector.claim_notification(root, "future-claim", now=500.0))
            self.assertTrue(collector.claim_notification(root, "future-claim", now=100.0))

    def test_notification_delivery_acknowledges_before_reporting_success(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            runner = mock.Mock(return_value=mock.Mock(returncode=0))

            delivered = collector.deliver_notification(
                root,
                "event-delivered",
                ["notify-send", "title", "body"],
                run=runner,
            )

            self.assertTrue(delivered)
            self.assertIn("event-delivered", load_collector()._acknowledged(root))
            self.assertNotIn("event-delivered", load_collector()._consumer_state(root)[1])
            runner.assert_called_once()

    def test_failed_notification_delivery_releases_its_persistent_claim(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            runner = mock.Mock(return_value=mock.Mock(returncode=1))

            delivered = collector.deliver_notification(
                root,
                "event-retry",
                ["notify-send", "title", "body"],
                run=runner,
            )

            self.assertFalse(delivered)
            self.assertNotIn("event-retry", collector._acknowledged(root))
            self.assertNotIn("event-retry", collector._consumer_state(root)[1])
            self.assertTrue(collector.claim_notification(root, "event-retry"))

    def test_hung_notification_process_is_terminated_and_released_for_retry(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector.initialize(root)
            started = time.monotonic()

            delivered = collector.deliver_notification(
                root,
                "event-timeout",
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_sec=0.05,
            )

            self.assertFalse(delivered)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(collector.claim_notification(root, "event-timeout"))

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
            self.assertNotIn("exitReason", snapshot["recent"][0])

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
