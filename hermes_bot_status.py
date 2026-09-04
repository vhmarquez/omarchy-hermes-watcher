#!/usr/bin/env python3
"""Read Hermes Watcher lifecycle records for the Omarchy shell plugin."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Callable

from hermes_proc import process_start_ticks
from secure_paths import ManagedTree

TERMINAL_STATES = {"succeeded", "failed", "interrupted", "stale"}
MAX_CLOCK_SKEW_SEC = 5.0
MAX_JSON_SAFE_INTEGER = (1 << 53) - 1
NOTIFICATION_CLAIM_TTL_SEC = 300.0
_THREAD_LOCK = threading.Lock()
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_HERMES_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HERMES_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HERMES_RESERVED_PROFILE_IDS = frozenset({"hermes", "test", "tmp", "root", "sudo"})
_REASONING_LEVELS = frozenset({"off", "on", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_RECORD_KEYS = {
    "schemaVersion", "eventId", "profile", "sessionId", "turnId", "taskId",
    "state", "startedAt", "updatedAt", "finishedAt", "durationSec", "model",
    "platform", "reasoningLevel", "writerPid", "writerProcessStart", "exitReason",
    "workDescription",
}


@contextmanager
def _consumer_lock(root: Path):
    with _THREAD_LOCK, ManagedTree(root).lock((".consumer.lock",)) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _event_lock(root: Path, path: Path):
    relative = path.relative_to(root).parts
    with ManagedTree(root).lock(relative[:-1] + (".writer.lock",)) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/hermes-bots"


def _configured_hermes_root() -> Path:
    configured = os.environ.get("HERMES_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.absolute()


def _profile_avatar_url(profile: str, *, hermes_root: Path | None = None) -> str:
    if not _SAFE_PROFILE.fullmatch(profile):
        return ""
    base = hermes_root or _configured_hermes_root()
    profile_dir = base if profile == "default" else base / "profiles" / profile
    assets_dir = profile_dir / "assets"
    try:
        for directory in (profile_dir, assets_dir):
            metadata = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return ""
    except OSError:
        return ""

    signatures = {
        "png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    for extension, matches in signatures.items():
        avatar = assets_dir / f"avatar.{extension}"
        descriptor = None
        try:
            descriptor = os.open(
                avatar,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            metadata = os.fstat(descriptor)
            header = os.read(descriptor, 12)
            if (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and 0 < metadata.st_size <= 2_000_000
                and matches(header)
            ):
                return f"{avatar.absolute().as_uri()}?v={metadata.st_mtime_ns}-{metadata.st_size}"
        except OSError:
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return ""


def _available_profiles(
    *,
    hermes_root: Path | None = None,
    profile_filter: set[str] | None = None,
) -> list[dict]:
    """List local Hermes profile IDs without reading profile configuration."""
    base = hermes_root or _configured_hermes_root()
    names = ["default"]
    profiles_dir = base / "profiles"
    deleted_dir = profiles_dir / ".deleted"
    try:
        metadata = profiles_dir.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid():
            for entry in sorted(profiles_dir.iterdir(), key=lambda item: item.name):
                name = entry.name
                if (
                    name == "default"
                    or not _HERMES_PROFILE_ID.fullmatch(name)
                    or name in _HERMES_RESERVED_PROFILE_IDS
                ):
                    continue
                try:
                    entry_metadata = entry.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(entry_metadata.st_mode)
                        or entry_metadata.st_uid != os.geteuid()
                    ):
                        continue
                    (deleted_dir / name).stat(follow_symlinks=False)
                except FileNotFoundError:
                    names.append(name)
                except OSError:
                    continue
    except OSError:
        pass

    if profile_filter:
        names = [name for name in names if name in profile_filter]

    profiles = []
    for name in names:
        avatar_url = _profile_avatar_url(name, hermes_root=base)
        profiles.append(
            {"profile": name, **({"avatarUrl": avatar_url} if avatar_url else {})}
        )
    return profiles


def _recent_session_description(value) -> str:
    description = " ".join(str(value or "").split())
    if not description or not all(character.isprintable() for character in description):
        return ""
    return description


def _valid_recent_session_id(value) -> bool:
    return isinstance(value, str) and _HERMES_SESSION_ID.fullmatch(value) is not None


def _profile_recent_sessions(
    profile: str,
    *,
    hermes_root: Path,
    limit: int,
) -> list[dict]:
    profile_dir = hermes_root if profile == "default" else hermes_root / "profiles" / profile
    database_path = profile_dir / "state.db"
    try:
        for directory in (hermes_root, profile_dir):
            metadata = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return []
        metadata = database_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return []
    except OSError:
        return []

    try:
        # Immutable mode is the only SQLite read path that cannot create or
        # update WAL/SHM coordination files. It intentionally reads the latest
        # checkpoint; a later poll sees activity after Hermes checkpoints it.
        with closing(
            sqlite3.connect(
                database_path.as_uri() + "?mode=ro&immutable=1",
                uri=True,
                timeout=0.1,
            )
        ) as database:
            database.row_factory = sqlite3.Row
            database.create_function(
                "watcher_valid_session_title",
                1,
                lambda value: int(bool(_recent_session_description(value))),
                deterministic=True,
            )
            database.create_function(
                "watcher_valid_session_id",
                1,
                lambda value: int(_valid_recent_session_id(value)),
                deterministic=True,
            )
            database.execute("PRAGMA query_only = ON")
            database.execute("PRAGMA busy_timeout = 100")
            columns = {
                str(row["name"])
                for row in database.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if not {"id", "title", "source", "started_at"}.issubset(columns):
                return []
            recency_columns = [
                f"s.{name}"
                for name in ("last_activity_at", "ended_at", "started_at")
                if name in columns
            ]
            recency_expression = (
                recency_columns[0]
                if len(recency_columns) == 1
                else f"COALESCE({', '.join(recency_columns)})"
            )
            conditions = [
                "s.source IN ('cli', 'tui', 'desktop')",
                "watcher_valid_session_id(s.id) = 1",
                "watcher_valid_session_title(s.title) = 1",
            ]
            if "archived" in columns:
                conditions.append("s.archived = 0")
            if "hidden" in columns:
                conditions.append("s.hidden = 0")
            if "message_count" in columns:
                conditions.append("s.message_count > 0")
            listable_children = ["s.parent_session_id IS NULL"]
            if "model_config" in columns:
                valid_model_config = "json_valid(COALESCE(s.model_config, '{}')) = 1"
                safe_model_config = (
                    "CASE WHEN json_valid(COALESCE(s.model_config, '{}')) "
                    "THEN COALESCE(s.model_config, '{}') ELSE '{}' END"
                )
                conditions.append(valid_model_config)
                conditions.append(
                    f"json_extract({safe_model_config}, '$._delegate_from') IS NULL"
                )
                if "parent_session_id" in columns:
                    listable_children.extend(
                        [
                            f"json_extract({safe_model_config}, '$._branched_from') IS NOT NULL",
                            f"json_extract({safe_model_config}, '$._reset_from') IS NOT NULL",
                        ]
                    )
            if "parent_session_id" in columns:
                if "end_reason" in columns:
                    child_recency_columns = [
                        f"child.{name}"
                        for name in ("last_activity_at", "ended_at", "started_at")
                        if name in columns
                    ]
                    child_recency = (
                        child_recency_columns[0]
                        if len(child_recency_columns) == 1
                        else f"COALESCE({', '.join(child_recency_columns)})"
                    )
                    child_filters = [
                        "child.parent_session_id = s.parent_session_id",
                        "child.source IN ('cli', 'tui', 'desktop')",
                        "watcher_valid_session_id(child.id) = 1",
                        f"typeof({child_recency}) IN ('integer', 'real')",
                        f"{child_recency} >= 0",
                        f"{child_recency} <= {MAX_JSON_SAFE_INTEGER}",
                        "typeof(child.started_at) IN ('integer', 'real')",
                        "child.started_at >= 0",
                        f"child.started_at <= {MAX_JSON_SAFE_INTEGER}",
                        "watcher_valid_session_title(child.title) = 1",
                    ]
                    if "archived" in columns:
                        child_filters.append("child.archived = 0")
                    if "hidden" in columns:
                        child_filters.append("child.hidden = 0")
                    if "message_count" in columns:
                        child_filters.append("child.message_count > 0")
                    if "model_config" in columns:
                        safe_child_model_config = (
                            "CASE WHEN json_valid(COALESCE(child.model_config, '{}')) "
                            "THEN COALESCE(child.model_config, '{}') ELSE '{}' END"
                        )
                        child_filters.extend(
                            [
                                "json_valid(COALESCE(child.model_config, '{}')) = 1",
                                f"COALESCE(json_extract({safe_child_model_config}, "
                                "'$._branched_from'), '') != s.parent_session_id",
                                f"json_extract({safe_child_model_config}, "
                                "'$._delegate_from') IS NULL",
                            ]
                        )
                    child_priority = (
                        "CASE WHEN child.end_reason = 'compression' THEN 0 "
                        "WHEN child.ended_at IS NULL THEN 1 ELSE 2 END"
                        if "ended_at" in columns
                        else "CASE WHEN child.end_reason = 'compression' THEN 0 ELSE 1 END"
                    )
                    listable_children.append(
                        "(EXISTS (SELECT 1 FROM sessions p "
                        "WHERE p.id = s.parent_session_id "
                        "AND p.end_reason = 'compression') "
                        "AND s.id = (SELECT child.id FROM sessions child "
                        f"WHERE {' AND '.join(child_filters)} "
                        f"ORDER BY {child_priority}, {child_recency} DESC, "
                        "child.started_at DESC, child.id DESC LIMIT 1))"
                    )
                    root_child_filters = [
                        item.replace("s.parent_session_id", "s.id")
                        for item in child_filters
                    ]
                    conditions.append(
                        "(COALESCE(s.end_reason, '') != 'compression' OR "
                        "NOT EXISTS (SELECT 1 FROM sessions child "
                        f"WHERE {' AND '.join(root_child_filters)}))"
                    )
                if {"end_reason", "ended_at"}.issubset(columns):
                    listable_children.append(
                        "EXISTS (SELECT 1 FROM sessions p "
                        "WHERE p.id = s.parent_session_id "
                        "AND p.end_reason = 'branched' "
                        "AND s.started_at >= p.ended_at)"
                    )
                if {"end_reason", "session_key"}.issubset(columns):
                    listable_children.append(
                        "EXISTS (SELECT 1 FROM sessions p "
                        "WHERE p.id = s.parent_session_id "
                        "AND p.end_reason IN ('session_reset', 'session_switch', "
                        "'idle', 'daily', 'suspended', 'resume_pending_expired') "
                        "AND s.session_key IS NOT NULL AND s.session_key != '' "
                        "AND s.session_key = p.session_key)"
                    )
                conditions.append(f"({' OR '.join(listable_children)})")
            conditions.extend(
                [
                    f"typeof({recency_expression}) IN ('integer', 'real')",
                    f"{recency_expression} >= 0",
                    f"{recency_expression} <= ?",
                    "typeof(s.started_at) IN ('integer', 'real')",
                    "s.started_at >= 0",
                    "s.started_at <= ?",
                ]
            )
            rows = database.execute(
                f"""
                SELECT s.id, s.title, s.source, s.started_at,
                       {recency_expression} AS recent_at
                FROM sessions s
                WHERE {' AND '.join(conditions)}
                ORDER BY recent_at DESC, s.started_at DESC, s.id DESC
                LIMIT ?
                """,
                (MAX_JSON_SAFE_INTEGER, MAX_JSON_SAFE_INTEGER, limit),
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return []

    avatar_url = _profile_avatar_url(profile, hermes_root=hermes_root)
    recent = []
    for row in rows:
        try:
            session_id = row["id"]
            description = _recent_session_description(row["title"])
            recent_at = row["recent_at"]
            started_at = row["started_at"]
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if (
            not _valid_recent_session_id(session_id)
            or not description
            or not _finite_number(recent_at)
            or not _finite_number(started_at)
        ):
            continue
        if len(description) > 160:
            description = description[:159].rstrip() + "…"
        recent.append(
            {
                "profile": profile,
                "sessionId": session_id,
                "description": description,
                "source": str(row["source"] or ""),
                "recentAt": float(recent_at),
                "_startedAt": float(started_at),
                **({"avatarUrl": avatar_url} if avatar_url else {}),
            }
        )
    return recent


def _recent_sessions(
    available_profiles: list[dict],
    *,
    hermes_root: Path,
    limit: int,
) -> list[dict]:
    bounded_limit = min(6, max(1, int(limit)))
    sessions = []
    for available in available_profiles:
        profile = str(available.get("profile", ""))
        sessions.extend(
            _profile_recent_sessions(
                profile,
                hermes_root=hermes_root,
                limit=bounded_limit,
            )
        )
    sessions.sort(
        key=lambda row: (
            float(row["recentAt"]),
            float(row["_startedAt"]),
            row["sessionId"],
            row["profile"],
        ),
        reverse=True,
    )
    bounded = sessions[:bounded_limit]
    for row in bounded:
        row.pop("_startedAt", None)
    return bounded


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_event_id(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def _normalize_record(
    value, *, containing_profile: str, containing_event_id: str, now: float
) -> dict | None:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        return None
    state = value.get("state")
    profile = value.get("profile")
    event_id = value.get("eventId")
    if state not in TERMINAL_STATES | {"running"}:
        return None
    if (
        not isinstance(profile, str)
        or not _SAFE_PROFILE.fullmatch(profile)
        or profile != containing_profile
    ):
        return None
    if not _valid_event_id(event_id) or event_id != containing_event_id:
        return None
    if not _finite_number(value.get("startedAt")):
        return None
    if float(value["startedAt"]) > now + MAX_CLOCK_SKEW_SEC:
        return None
    if state == "running" and not _finite_number(value.get("updatedAt", value.get("startedAt"))):
        return None
    if state == "running" and float(value.get("updatedAt", value["startedAt"])) > now + MAX_CLOCK_SKEW_SEC:
        return None
    if state in TERMINAL_STATES and (
        not _finite_number(value.get("finishedAt"))
        or not _finite_number(value.get("durationSec"))
        or float(value["finishedAt"]) > now + MAX_CLOCK_SKEW_SEC
    ):
        return None
    normalized = {key: value[key] for key in _RECORD_KEYS if key in value}
    work_description = value.get("workDescription")
    if not (
        isinstance(work_description, str)
        and 0 < len(work_description) <= 160
        and work_description == work_description.strip()
        and all(character.isprintable() for character in work_description)
    ):
        normalized.pop("workDescription", None)
    reasoning_level = value.get("reasoningLevel")
    if not isinstance(reasoning_level, str) or reasoning_level not in _REASONING_LEVELS:
        normalized.pop("reasoningLevel", None)
    normalized["startedAt"] = float(value["startedAt"])
    if state == "running":
        writer_pid = value.get("writerPid")
        if isinstance(writer_pid, bool) or not isinstance(writer_pid, int):
            return None
        if writer_pid <= 0:
            return None
        normalized["updatedAt"] = float(value.get("updatedAt", value["startedAt"]))
        normalized["writerPid"] = writer_pid
        normalized["writerProcessStart"] = str(value.get("writerProcessStart", ""))
    else:
        normalized["finishedAt"] = float(value["finishedAt"])
        normalized["updatedAt"] = normalized["finishedAt"]
        normalized["durationSec"] = max(0.0, float(value["durationSec"]))
    context_used = value.get("contextUsed")
    context_max = value.get("contextMax")
    if (
        isinstance(context_used, int)
        and not isinstance(context_used, bool)
        and isinstance(context_max, int)
        and not isinstance(context_max, bool)
        and context_used > 0
        and context_max > 0
        and context_used <= MAX_JSON_SAFE_INTEGER
        and context_max <= MAX_JSON_SAFE_INTEGER
    ):
        normalized["contextUsed"] = context_used
        normalized["contextMax"] = context_max
        normalized["contextPercent"] = max(
            0, min(100, round(Fraction(context_used, context_max) * 100))
        )
        if isinstance(value.get("contextConfirmed"), bool):
            normalized["contextConfirmed"] = value["contextConfirmed"]
    return normalized


def _event_json_paths(root: Path) -> list[Path]:
    tree = ManagedTree(root)
    paths: list[Path] = []
    for profile in tree.list_directories(("events",)):
        for name in tree.list_regular_files(("events", profile), suffix=".json"):
            paths.append(root / "events" / profile / name)
    return paths


def _observer_process_files(root: Path) -> list[tuple[Path, tuple[str, int, str]]]:
    tree = ManagedTree(root)
    registrations: list[tuple[Path, tuple[str, int, str]]] = []
    for profile in tree.list_directories(("processes",)):
        if not _SAFE_PROFILE.fullmatch(profile):
            continue
        for name in tree.list_regular_files(("processes", profile), suffix=".json"):
            try:
                value = tree.read_json(("processes", profile, name))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(value, dict) or value.get("schemaVersion") != 1:
                continue
            writer_pid = value.get("writerPid")
            process_start = value.get("writerProcessStart")
            if (
                value.get("profile") != profile
                or isinstance(writer_pid, bool)
                or not isinstance(writer_pid, int)
                or writer_pid <= 0
                or not isinstance(process_start, str)
                or not 0 < len(process_start) <= 64
                or not process_start.isdigit()
            ):
                continue
            expected = hashlib.sha256(
                "\0".join((profile, str(writer_pid), process_start)).encode("utf-8")
            ).hexdigest()
            if name != f"{expected}.json":
                continue
            path = root / "processes" / profile / name
            registrations.append((path, (profile, writer_pid, process_start)))
    return registrations


def _observer_process_identities(root: Path) -> set[tuple[str, int, str]]:
    return {identity for _, identity in _observer_process_files(root)}


def _read_json(root: Path, path: Path):
    return ManagedTree(root).read_json(path.relative_to(root).parts)


def _record_files(root: Path, *, now: float | None = None) -> list[tuple[Path, dict]]:
    current = time.time() if now is None else now
    records = []
    for path in _event_json_paths(root):
        try:
            value = _normalize_record(
                _read_json(root, path),
                containing_profile=path.parent.name,
                containing_event_id=path.stem,
                now=current,
            )
            if value is not None:
                records.append((path, value))
        except (OSError, ValueError, TypeError):
            continue
    return records


def _records(root: Path) -> list[dict]:
    return [record for _, record in _record_files(root)]


def _atomic_json(path: Path, value: dict) -> None:
    if path.parent.parent.name == "events":
        root = path.parents[2]
    else:
        root = path.parent
    ManagedTree(root).atomic_json(path.relative_to(root).parts, value)


def _consumer_state(root: Path) -> tuple[set[str], dict[str, float]]:
    try:
        value = ManagedTree(root).read_json(("consumer.json",))
    except FileNotFoundError:
        return set(), {}
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("invalid consumer state")
    acknowledged = value.get("acknowledged")
    claimed = value.get("claimed", {})
    if not isinstance(acknowledged, list) or any(
        not _valid_event_id(item) for item in acknowledged
    ):
        raise ValueError("invalid consumer acknowledgements")
    acknowledged_ids = set(acknowledged)
    if isinstance(claimed, list):
        if any(not _valid_event_id(item) for item in claimed):
            raise ValueError("invalid consumer claims")
        claim_times = {item: 0.0 for item in claimed}
    elif isinstance(claimed, dict):
        if any(
            not _valid_event_id(event_id) or not _finite_number(claimed_at)
            for event_id, claimed_at in claimed.items()
        ):
            raise ValueError("invalid consumer claims")
        claim_times = {event_id: float(claimed_at) for event_id, claimed_at in claimed.items()}
    else:
        raise ValueError("invalid consumer claims")
    return acknowledged_ids, {
        event_id: claimed_at
        for event_id, claimed_at in claim_times.items()
        if event_id not in acknowledged_ids
    }


def _acknowledged(root: Path) -> set[str]:
    return _consumer_state(root)[0]


def _write_consumer(root: Path, acknowledged: set[str], claimed: dict[str, float]) -> None:
    _atomic_json(
        root / "consumer.json",
        {
            "schemaVersion": 1,
            "acknowledged": sorted(acknowledged),
            "claimed": {
                event_id: claimed[event_id]
                for event_id in sorted(claimed)
                if event_id not in acknowledged
            },
        },
    )


def _write_acknowledged(root: Path, event_ids: set[str]) -> None:
    _, claimed = _consumer_state(root)
    _write_consumer(root, event_ids, claimed)


def initialize(root: Path) -> None:
    terminal_ids = {str(record["eventId"]) for record in _records(root) if record["state"] in TERMINAL_STATES}
    with _consumer_lock(root):
        _write_acknowledged(root, _acknowledged(root) | terminal_ids)


def repair_consumer(root: Path) -> None:
    terminal_ids = {
        str(record["eventId"])
        for record in _records(root)
        if record["state"] in TERMINAL_STATES
    }
    with _consumer_lock(root):
        _write_consumer(root, terminal_ids, {})


def claim_notification(root: Path, event_id: str, *, now: float | None = None) -> bool:
    if not _valid_event_id(event_id):
        raise ValueError("invalid event ID")
    with _consumer_lock(root):
        acknowledged, claimed = _consumer_state(root)
        current = time.time() if now is None else now
        claimed_at = claimed.get(event_id)
        claim_age = current - claimed_at if claimed_at is not None else None
        if event_id in acknowledged or (
            claim_age is not None and 0 <= claim_age <= NOTIFICATION_CLAIM_TTL_SEC
        ):
            return False
        claimed[event_id] = current
        _write_consumer(root, acknowledged, claimed)
        return True


def release_notification(root: Path, event_id: str) -> None:
    if not _valid_event_id(event_id):
        raise ValueError("invalid event ID")
    with _consumer_lock(root):
        acknowledged, claimed = _consumer_state(root)
        claimed.pop(event_id, None)
        _write_consumer(root, acknowledged, claimed)


def deliver_notification(
    root: Path,
    event_id: str,
    command: list[str],
    *,
    run: Callable[..., object] | None = None,
    timeout_sec: float = 10.0,
) -> bool | None:
    if not claim_notification(root, event_id):
        return None
    runner = subprocess.run if run is None else run
    try:
        result = runner(command, check=False, timeout=max(0.01, timeout_sec))
    except (OSError, ValueError, subprocess.SubprocessError):
        release_notification(root, event_id)
        return False
    if int(getattr(result, "returncode", 1)) != 0:
        release_notification(root, event_id)
        return False
    acknowledge(root, event_id)
    return True


def acknowledge(root: Path, event_id: str) -> None:
    if not _valid_event_id(event_id):
        raise ValueError("invalid event ID")
    with _consumer_lock(root):
        acknowledged, claimed = _consumer_state(root)
        acknowledged.add(event_id)
        claimed.pop(event_id, None)
        _write_consumer(root, acknowledged, claimed)


def pending_notifications(
    root: Path,
    snapshot: dict,
    *,
    now: float | None = None,
    min_duration_sec: float = 5,
    max_catchup_age_sec: float = 3600,
    enabled_states: set[str] | None = None,
) -> list[dict]:
    current = time.time() if now is None else now
    enabled = {"succeeded", "failed"} if enabled_states is None else enabled_states
    acknowledged, claimed = _consumer_state(root)
    active_claims = {
        event_id
        for event_id, claimed_at in claimed.items()
        if 0 <= current - claimed_at <= NOTIFICATION_CLAIM_TTL_SEC
    }
    return [
        record
        for record in snapshot.get("_notificationCandidates", snapshot.get("recent", []))
        if record.get("state") in enabled
        and record.get("eventId") not in acknowledged
        and record.get("eventId") not in active_claims
        and float(record.get("durationSec", 0)) >= min_duration_sec
        and current - float(record.get("finishedAt", 0)) <= max_catchup_age_sec
    ]


def prune(root: Path, *, keep_terminal: int = 100) -> int:
    current = time.time()
    terminal = [
        (float(record["finishedAt"]), path, record)
        for path, record in _record_files(root, now=current)
        if record["state"] in TERMINAL_STATES
    ]
    terminal.sort(key=lambda item: item[0], reverse=True)
    deleted = 0
    for _, path, selected in terminal[max(0, keep_terminal):]:
        with _event_lock(root, path):
            try:
                latest = _normalize_record(
                    _read_json(root, path),
                    containing_profile=path.parent.name,
                    containing_event_id=path.stem,
                    now=current,
                )
                if latest is None or latest["state"] not in TERMINAL_STATES or latest != selected:
                    continue
                ManagedTree(root).unlink_regular(path.relative_to(root).parts)
                deleted += 1
            except (FileNotFoundError, ValueError, TypeError):
                pass
    with _consumer_lock(root):
        retained_ids = {str(record["eventId"]) for record in _records(root)}
        acknowledged, claimed = _consumer_state(root)
        _write_consumer(
            root,
            acknowledged & retained_ids,
            {
                event_id: claimed_at
                for event_id, claimed_at in claimed.items()
                if event_id in retained_ids
            },
        )
    for path, (_, writer_pid, process_start) in _observer_process_files(root):
        if _process_alive(writer_pid, process_start):
            continue
        try:
            ManagedTree(root).unlink_regular(path.relative_to(root).parts)
            deleted += 1
        except (FileNotFoundError, ValueError, TypeError):
            pass
    return deleted


def _process_alive(pid: int, expected_start: str) -> bool:
    try:
        actual = process_start_ticks(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
        return bool(expected_start) and actual == expected_start
    except (OSError, ValueError):
        return False


_HERMES_EXECUTION_MARKERS = {
    "chat", "-z", "--oneshot", "-r", "--resume", "-c", "--continue", "--tui", "--cli",
}
_HERMES_OPTIONS_WITH_VALUE = {
    "-p", "--profile", "-m", "--model", "--provider", "--reasoning", "-t", "--toolsets",
    "--usage-file", "--in", "-s", "--skills",
}
_HERMES_VALUELESS_FLAGS = {
    "-w", "--no-restore-cwd", "--worktree", "--accept-hooks", "--yolo", "--pass-session-id",
    "--ignore-user-config", "--ignore-rules", "--safe-mode", "--dev",
}


def _partial_can_be_classification_token(partial: str) -> bool:
    exact_tokens = _HERMES_EXECUTION_MARKERS | _HERMES_OPTIONS_WITH_VALUE | _HERMES_VALUELESS_FLAGS
    if any(token.startswith(partial) for token in exact_tokens):
        return True
    suffix_prefixes = {
        "--profile=", "-p", "-m", "-t", "-s",
        *(f"{option}=" for option in _HERMES_OPTIONS_WITH_VALUE if option not in {"-p", "--profile"}),
        "--oneshot=", "--resume=", "--continue=", "-r=", "-c=",
    }
    return any(partial.startswith(prefix) or prefix.startswith(partial) for prefix in suffix_prefixes)


def _read_hermes_cmdline_prefix(path: Path, *, dir_fd: int | None = None) -> list[str]:
    """Read only enough argv tokens to classify Hermes, never prompt text."""
    args: list[str] = []
    token = bytearray()
    found_launcher = False
    option_value_pending = ""
    try:
        if dir_fd is None:
            opened = path.open("rb", buffering=0)
        else:
            fd = os.open(
                os.fspath(path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=dir_fd,
            )
            opened = os.fdopen(fd, "rb", buffering=0)
        with opened as handle:
            for _ in range(4096):
                byte = handle.read(1)
                if not byte:
                    if token:
                        return []
                    break
                if byte != b"\0":
                    token.extend(byte)
                    if len(token) > 1024:
                        return []
                    if found_launcher and not option_value_pending:
                        partial = token.decode("utf-8", errors="replace")
                        immediate_markers = {
                            "-z": "-z",
                            "-r": "-r",
                            "-c": "-c",
                            "--oneshot=": "--oneshot",
                            "--resume=": "--resume",
                            "--continue=": "--continue",
                        }
                        marker = immediate_markers.get(partial)
                        if marker is not None:
                            args.append(marker)
                            return args
                        if not _partial_can_be_classification_token(partial):
                            return []
                    continue

                value = token.decode("utf-8", errors="replace")
                token.clear()
                args.append(value)
                if not found_launcher:
                    if len(args) == 1:
                        if Path(value).name == "hermes":
                            found_launcher = True
                        elif Path(value).name.startswith("python"):
                            continue
                        else:
                            return []
                    elif len(args) == 2:
                        if Path(args[0]).name.startswith("python") and Path(value).name == "hermes":
                            found_launcher = True
                        else:
                            return []
                    continue
                if option_value_pending:
                    if option_value_pending != "profile":
                        args.pop()
                    option_value_pending = ""
                    continue
                if value in _HERMES_EXECUTION_MARKERS:
                    break
                if value in _HERMES_OPTIONS_WITH_VALUE:
                    if value in {"-p", "--profile"}:
                        option_value_pending = "profile"
                    else:
                        args.pop()
                        option_value_pending = "discard"
                    continue
                attached_profile = next(
                    (prefix for prefix in ("--profile=", "-p=") if value.startswith(prefix)),
                    None,
                )
                if attached_profile is not None:
                    profile = value[len(attached_profile):]
                    args[-1:] = ["--profile", profile]
                    continue
                if value.startswith("-p") and len(value) > 2:
                    args[-1:] = ["--profile", value[2:]]
                    continue
                if any(value.startswith(option) and len(value) > len(option) for option in ("-m", "-t", "-s")):
                    args.pop()
                    continue
                attached_execution = next(
                    (
                        prefix
                        for prefix in ("--resume=", "-r=", "--continue=", "-c=")
                        if value.startswith(prefix)
                    ),
                    None,
                )
                if attached_execution is not None:
                    args[-1] = attached_execution[:-1]
                    break
                attached_discard = next(
                    (
                        option
                        for option in _HERMES_OPTIONS_WITH_VALUE - {"-p", "--profile"}
                        if value.startswith(f"{option}=")
                    ),
                    None,
                )
                if attached_discard is not None:
                    args.pop()
                    continue
                if value in _HERMES_VALUELESS_FLAGS:
                    args.pop()
                    continue
                if value.startswith("-"):
                    return []
                break
            else:
                return []
    except (OSError, ValueError):
        return []
    return args


def _is_interactive_hermes_args(args: list[str]) -> bool:
    if not args or any(token in _HERMES_EXECUTION_MARKERS for token in args):
        return True
    index = 0
    while index < len(args):
        token = args[index]
        if token in _HERMES_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return False
    return True


def _read_proc_text(directory_fd: int, name: str, *, limit: int = 4096) -> str:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise OSError("unsafe proc file")
        payload = bytearray()
        while len(payload) <= limit:
            block = os.read(fd, min(1024, limit + 1 - len(payload)))
            if not block:
                return payload.decode("utf-8")
            payload.extend(block)
        raise OSError("proc file exceeds read limit")
    finally:
        os.close(fd)


def _discover_hermes_sessions(
    *,
    proc_root: Path = Path("/proc"),
    clock_ticks: int | None = None,
) -> list[dict]:
    """Return sanitized metadata for open interactive Hermes processes."""
    try:
        uptime = float((proc_root / "uptime").read_text(encoding="utf-8").split()[0])
        ticks_per_second = int(clock_ticks or os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return []
    if ticks_per_second <= 0:
        return []

    sessions = []
    effective_uid = os.geteuid()

    try:
        process_dirs = list(proc_root.iterdir())
    except OSError:
        return []
    for directory in process_dirs:
        if not directory.name.isdigit():
            continue
        process_fd = -1
        try:
            process_fd = os.open(
                directory,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(process_fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != effective_uid:
                continue
            initial_start = process_start_ticks(_read_proc_text(process_fd, "stat"))
            try:
                comm = _read_proc_text(process_fd, "comm").strip()
            except OSError:
                comm = ""
            if comm and comm != "hermes" and not comm.startswith("python"):
                continue
            argv = _read_hermes_cmdline_prefix(Path("cmdline"), dir_fd=process_fd)
            if not argv:
                continue
            final_start = process_start_ticks(_read_proc_text(process_fd, "stat"))
            current = os.stat(directory, follow_symlinks=False)
            if (
                initial_start != final_start
                or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
            ):
                continue
            if Path(argv[0]).name == "hermes":
                launcher_index = 0
            elif (
                len(argv) > 1
                and Path(argv[0]).name.startswith("python")
                and Path(argv[1]).name == "hermes"
            ):
                launcher_index = 1
            else:
                continue
            args = argv[launcher_index + 1:]
            profile = "default"
            for index, token in enumerate(args):
                if token in {"-p", "--profile"} and index + 1 < len(args):
                    profile = args[index + 1]
                    break
                if token.startswith("--profile="):
                    profile = token.split("=", 1)[1]
                    break
            if not _SAFE_PROFILE.fullmatch(profile):
                continue
            if not _is_interactive_hermes_args(args):
                continue
            started_ticks = int(final_start)
            running_for = max(0.0, uptime - started_ticks / ticks_per_second)
            sessions.append(
                {
                    "profile": profile,
                    "pid": int(directory.name),
                    "processStart": str(final_start),
                    "runningForSec": running_for,
                }
            )
        except (OSError, ValueError, IndexError, StopIteration):
            continue
        finally:
            if process_fd >= 0:
                os.close(process_fd)
    sessions.sort(key=lambda row: (row["profile"], row["pid"]))
    return sessions


def build_snapshot(
    root: Path,
    *,
    now: float | None = None,
    process_alive: Callable[[int, str], bool] | None = None,
    history_limit: int = 20,
    stale_grace_sec: int = 30,
    profile_filter: set[str] | None = None,
    online_sessions: list[dict] | None = None,
) -> dict:
    current = time.time() if now is None else now
    alive = _process_alive if process_alive is None else process_alive
    detected_sessions = _discover_hermes_sessions() if online_sessions is None else online_sessions
    loaded_observers = _observer_process_identities(root)
    if profile_filter:
        detected_sessions = [
            session for session in detected_sessions
            if str(session.get("profile", "")) in profile_filter
        ]
    record_files = _record_files(root, now=current)
    if profile_filter:
        record_files = [
            (path, record)
            for path, record in record_files
            if str(record.get("profile", "")) in profile_filter
        ]
    for index, (path, record) in enumerate(record_files):
        if record["state"] != "running":
            continue
        with _event_lock(root, path):
            try:
                latest = _normalize_record(
                    _read_json(root, path),
                    containing_profile=path.parent.name,
                    containing_event_id=path.stem,
                    now=current,
                )
            except (OSError, ValueError, TypeError):
                continue
            if latest is None:
                continue
            record_files[index] = (path, latest)
            if latest.get("state") != "running":
                continue
            age = current - float(latest.get("updatedAt", latest.get("startedAt", current)))
            if age < stale_grace_sec or alive(
                int(latest.get("writerPid", 0)), str(latest.get("writerProcessStart", ""))
            ):
                continue
            stale = dict(latest)
            stale.update(
                state="stale",
                finishedAt=current,
                updatedAt=current,
                durationSec=max(0.0, current - float(latest.get("startedAt", current))),
                exitReason="writer_process_exited",
            )
            _atomic_json(path, stale)
            record_files[index] = (path, stale)
    records = [record for _, record in record_files]
    active = [record for record in records if record["state"] == "running"]
    last_confirmed_context: dict[str, dict] = {}
    last_confirmed_context_by_process: dict[tuple[str, int, str], dict] = {}

    def remember_latest(mapping: dict, key, record: dict) -> None:
        previous = mapping.get(key)
        current_key = (float(record["finishedAt"]), str(record.get("eventId", "")))
        previous_key = (
            (float(previous["finishedAt"]), str(previous.get("eventId", "")))
            if previous is not None
            else None
        )
        if previous_key is None or current_key > previous_key:
            mapping[key] = record

    for record in records:
        if (
            record["state"] in TERMINAL_STATES
            and record.get("contextConfirmed") is True
            and all(key in record for key in ("contextUsed", "contextMax", "contextPercent"))
        ):
            profile = str(record["profile"])
            remember_latest(last_confirmed_context, profile, record)
            writer_pid = record.get("writerPid")
            writer_start = record.get("writerProcessStart")
            if (
                isinstance(writer_pid, int)
                and not isinstance(writer_pid, bool)
                and writer_pid > 0
                and isinstance(writer_start, str)
                and 0 < len(writer_start) <= 64
                and writer_start.isdigit()
            ):
                remember_latest(
                    last_confirmed_context_by_process,
                    (profile, writer_pid, writer_start),
                    record,
                )
    by_profile: dict[str, list[dict]] = {}
    for record in active:
        by_profile.setdefault(str(record["profile"]), []).append(record)
    profiles = []
    for profile, turns in by_profile.items():
        oldest = min(float(turn["startedAt"]) for turn in turns)
        latest = max(turns, key=lambda turn: float(turn.get("updatedAt", turn["startedAt"])))
        row = {
            "profile": profile,
            "activeTurnCount": len(turns),
            "runningForSec": max(0.0, current - oldest),
            "model": str(latest.get("model", "")),
            "platform": str(latest.get("platform", "")),
            "reasoningLevel": str(latest.get("reasoningLevel", "")),
        }
        if latest.get("workDescription"):
            row["workDescription"] = str(latest["workDescription"])
        context_turns = [
            turn
            for turn in turns
            if all(key in turn for key in ("contextUsed", "contextMax", "contextPercent"))
        ]
        if context_turns:
            context_turn = max(
                context_turns,
                key=lambda turn: Fraction(
                    int(turn["contextUsed"]), int(turn["contextMax"])
                ),
            )
            row.update(
                {
                    "model": str(context_turn.get("model", "")),
                    "platform": str(context_turn.get("platform", "")),
                    "reasoningLevel": str(context_turn.get("reasoningLevel", "")),
                    "contextUsed": int(context_turn["contextUsed"]),
                    "contextMax": int(context_turn["contextMax"]),
                    "contextPercent": int(context_turn["contextPercent"]),
                }
            )
        profiles.append(row)
    profiles.sort(key=lambda row: row["profile"])

    # Agent cards represent Hermes sessions, not profile aggregates. A profile
    # may own several concurrent sessions, each with its own task and context.
    active_by_session: dict[tuple[str, str, int, str], list[dict]] = {}
    for record in active:
        profile = str(record["profile"])
        session_id = str(record.get("sessionId", ""))
        writer_pid = int(record.get("writerPid", 0))
        writer_start = str(record.get("writerProcessStart", ""))
        session_key = (profile, session_id, writer_pid, writer_start)
        active_by_session.setdefault(session_key, []).append(record)

    active_session_rows = []
    for (profile, session_id, writer_pid, writer_start), turns in active_by_session.items():
        oldest = min(float(turn["startedAt"]) for turn in turns)
        latest = max(turns, key=lambda turn: float(turn.get("updatedAt", turn["startedAt"])))
        row = {
            "profile": profile,
            "activeTurnCount": len(turns),
            "runningForSec": max(0.0, current - oldest),
            "model": str(latest.get("model", "")),
            "platform": str(latest.get("platform", "")),
            "reasoningLevel": str(latest.get("reasoningLevel", "")),
            "_sessionId": session_id,
            "_writerPid": writer_pid,
            "_writerProcessStart": writer_start,
            "_updatedAt": float(latest.get("updatedAt", latest["startedAt"])),
        }
        if latest.get("workDescription"):
            row["workDescription"] = str(latest["workDescription"])
        context_turns = [
            turn
            for turn in turns
            if all(key in turn for key in ("contextUsed", "contextMax", "contextPercent"))
        ]
        if context_turns:
            context_turn = max(
                context_turns,
                key=lambda turn: Fraction(
                    int(turn["contextUsed"]), int(turn["contextMax"])
                ),
            )
            row.update(
                {
                    "model": str(context_turn.get("model", "")),
                    "platform": str(context_turn.get("platform", "")),
                    "reasoningLevel": str(context_turn.get("reasoningLevel", "")),
                    "contextUsed": int(context_turn["contextUsed"]),
                    "contextMax": int(context_turn["contextMax"]),
                    "contextPercent": int(context_turn["contextPercent"]),
                }
            )
        active_session_rows.append(row)
    active_session_rows.sort(
        key=lambda row: (row["profile"], row["_writerPid"], row["_sessionId"])
    )

    sessions_by_profile: dict[str, list[dict]] = {}
    for session in detected_sessions:
        if not isinstance(session, dict):
            continue
        profile = session.get("profile")
        pid = session.get("pid")
        running_for = session.get("runningForSec")
        process_start = session.get("processStart", "")
        if (
            not isinstance(profile, str)
            or not _SAFE_PROFILE.fullmatch(profile)
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not _finite_number(running_for)
            or not isinstance(process_start, str)
            or len(process_start) > 64
            or (process_start and not process_start.isdigit())
        ):
            continue
        sessions_by_profile.setdefault(profile, []).append(
            {
                "pid": pid,
                "processStart": process_start,
                "runningForSec": max(0.0, float(str(running_for))),
            }
        )

    def public_session_row(profile: str, session: dict | None, active_row: dict) -> dict:
        active_has_context = all(
            key in active_row for key in ("contextUsed", "contextMax", "contextPercent")
        )
        if active_has_context:
            context_row = active_row
        elif active_row:
            context_row = {}
        elif session is not None and session["processStart"]:
            context_row = last_confirmed_context_by_process.get(
                (profile, session["pid"], session["processStart"]), {}
            )
        else:
            # Compatibility fallback for callers that cannot supply a process
            # start identity. Live /proc discovery always supplies one.
            context_row = last_confirmed_context.get(profile, {})
        display_row = active_row or context_row
        avatar_url = _profile_avatar_url(profile)
        if session is not None:
            session_identity = (
                "process",
                profile,
                str(session["pid"]),
                str(session["processStart"]),
            )
        elif active_row.get("_sessionId"):
            session_identity = ("session", profile, str(active_row["_sessionId"]))
        else:
            session_identity = (
                "process",
                profile,
                str(active_row.get("_writerPid", "")),
                str(active_row.get("_writerProcessStart", "")),
            )
        session_key = hashlib.sha256("\0".join(session_identity).encode("utf-8")).hexdigest()
        observer_identity = (
            profile,
            int(session["pid"]) if session is not None else int(active_row.get("_writerPid", 0)),
            str(
                session["processStart"]
                if session is not None
                else active_row.get("_writerProcessStart", "")
            ),
        )
        return {
            "sessionKey": session_key,
            "profile": profile,
            "activeTurnCount": int(active_row.get("activeTurnCount", 0)),
            "observerLoaded": bool(active_row) or observer_identity in loaded_observers,
            "runningForSec": (
                float(session["runningForSec"])
                if session is not None
                else float(active_row.get("runningForSec", 0.0))
            ),
            "model": str(display_row.get("model", "")),
            "platform": str(display_row.get("platform", "")),
            "reasoningLevel": str(display_row.get("reasoningLevel", "")),
            **(
                {"workDescription": str(active_row["workDescription"])}
                if active_row.get("workDescription")
                else {}
            ),
            **({"avatarUrl": avatar_url} if avatar_url else {}),
            **(
                {
                    "contextUsed": context_row["contextUsed"],
                    "contextMax": context_row["contextMax"],
                    "contextPercent": context_row["contextPercent"],
                    "contextIsLastKnown": not active_has_context,
                }
                if context_row
                else {}
            ),
        }

    unmatched_active = list(active_session_rows)
    online_profiles = []
    for profile in sorted(sessions_by_profile):
        sessions = sorted(sessions_by_profile[profile], key=lambda row: row["pid"])
        for session in sessions:
            matched_index = next(
                (
                    index
                    for index, row in enumerate(unmatched_active)
                    if row["profile"] == profile
                    and row["_writerPid"] == session["pid"]
                    and (
                        not session["processStart"]
                        or not row["_writerProcessStart"]
                        or row["_writerProcessStart"] == session["processStart"]
                    )
                ),
                None,
            )
            active_row = unmatched_active.pop(matched_index) if matched_index is not None else {}
            online_profiles.append(public_session_row(profile, session, active_row))

    for active_row in unmatched_active:
        online_profiles.append(public_session_row(active_row["profile"], None, active_row))
    online_profiles.sort(
        key=lambda row: (row["profile"], -int(row["activeTurnCount"]), -float(row["runningForSec"]))
    )

    recent = [record for record in records if record["state"] in TERMINAL_STATES]
    recent.sort(key=lambda record: float(record.get("finishedAt", 0)), reverse=True)
    hermes_root = _configured_hermes_root()
    available_profiles = _available_profiles(
        hermes_root=hermes_root,
        profile_filter=profile_filter,
    )
    return {
        "schemaVersion": 1,
        "generatedAt": current,
        "hermesRoot": str(hermes_root),
        "activeBotCount": len(active_session_rows),
        "activeTurnCount": len(active),
        "onlineBotCount": len(online_profiles),
        "onlineSessionCount": len(online_profiles),
        "onlineProfiles": online_profiles,
        "availableProfiles": available_profiles,
        "profiles": profiles,
        "recent": recent[:history_limit],
        "recentSessions": _recent_sessions(
            available_profiles,
            hermes_root=hermes_root,
            limit=history_limit,
        ),
        "_notificationCandidates": recent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--now", type=float)
    snapshot_parser.add_argument("--history-limit", type=int, default=20)
    snapshot_parser.add_argument("--stale-grace-sec", type=int, default=30)
    snapshot_parser.add_argument("--min-duration-sec", type=float, default=5)
    snapshot_parser.add_argument("--max-catchup-age-sec", type=float, default=3600)
    snapshot_parser.add_argument("--notify-states", default="succeeded,failed")
    snapshot_parser.add_argument("--profile-filter", default="")
    acknowledge_parser = subparsers.add_parser("acknowledge")
    acknowledge_parser.add_argument("event_id")
    deliver_parser = subparsers.add_parser("deliver-notification")
    deliver_parser.add_argument("event_id")
    deliver_parser.add_argument("--icon", required=True)
    deliver_parser.add_argument("--title", required=True)
    deliver_parser.add_argument("--body", required=True)
    subparsers.add_parser("initialize")
    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--keep-terminal", type=int, default=100)
    args = parser.parse_args(argv)
    root = state_root()
    if args.command == "snapshot":
        snapshot = build_snapshot(
            root,
            now=args.now,
            history_limit=max(1, args.history_limit),
            stale_grace_sec=max(1, args.stale_grace_sec),
            profile_filter={item.strip() for item in args.profile_filter.split(",") if item.strip()},
        )
        try:
            snapshot["pendingNotifications"] = pending_notifications(
                root,
                snapshot,
                now=args.now,
                min_duration_sec=max(0, args.min_duration_sec),
                max_catchup_age_sec=max(0, args.max_catchup_age_sec),
                enabled_states={item for item in args.notify_states.split(",") if item},
            )
        except (TypeError, ValueError):
            repair_consumer(root)
            snapshot["pendingNotifications"] = []
            snapshot["notificationError"] = "Notification history was repaired"
        snapshot.pop("_notificationCandidates", None)
        print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    elif args.command == "acknowledge":
        acknowledge(root, args.event_id)
    elif args.command == "deliver-notification":
        delivered = deliver_notification(
            root,
            args.event_id,
            [
                "notify-send",
                "--app-name=Hermes Watcher",
                "--urgency=normal",
                f"--icon={args.icon}",
                args.title,
                args.body,
            ],
        )
        if delivered is None:
            return 75
        if not delivered:
            return 1
    elif args.command == "initialize":
        initialize(root)
    elif args.command == "prune":
        print(prune(root, keep_terminal=max(0, args.keep_terminal)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
