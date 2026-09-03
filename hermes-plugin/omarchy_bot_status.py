"""Hermes lifecycle observer for Omarchy Hermes Watcher."""

from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any

def _load_local_helper(module_name: str):
    source = Path(__file__).with_name(f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(f"omarchy_bot_status_{module_name}", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from .hermes_proc import process_start_ticks
    from .secure_paths import ManagedTree
except ImportError:  # Direct file loading by tests and plugin doctor.
    process_start_ticks = _load_local_helper("hermes_proc").process_start_ticks
    ManagedTree = _load_local_helper("secure_paths").ManagedTree

SCHEMA_VERSION = 1
MAX_WORK_DESCRIPTION_CHARS = 160
_REASONING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_CONTEXT_MAX_CACHE: dict[tuple[str, str, str], int] = {}


def _state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "omarchy" / "hermes-bots"


def _profile_name() -> str:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    candidate = home.name if home.parent.name == "profiles" else "default"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate).strip("._-")[:80]
    return safe or "unknown"


def _process_start(pid: int) -> str:
    try:
        return process_start_ticks(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def _event_id(profile: str, session_id: str, turn_id: str) -> str:
    raw = "\0".join((profile, session_id, turn_id)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _event_path(profile: str, event_id: str) -> Path:
    root = _state_root()
    ManagedTree(root).ensure_directory(("events", profile))
    return root / "events" / profile / f"{event_id}.json"


def _work_description(value: Any) -> str:
    """Return a short, single-line excerpt of the current user request."""
    if not isinstance(value, str):
        return ""
    without_controls = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", value)
    return " ".join(without_controls.split())[:MAX_WORK_DESCRIPTION_CHARS].strip()


@contextmanager
def _profile_lock(profile: str):
    root = _state_root()
    with ManagedTree(root).lock(("events", profile, ".writer.lock")) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    root = _state_root()
    ManagedTree(root).atomic_json(path.relative_to(root).parts, data)


def _read_event(path: Path):
    root = _state_root()
    return ManagedTree(root).read_json(path.relative_to(root).parts)


def _write_turn_start(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    model: str = "",
    platform: str = "",
    user_message: Any = None,
    **_: Any,
) -> None:
    session_id = "" if session_id is None else str(session_id)
    turn_id = "" if turn_id is None else str(turn_id)
    if not session_id and not turn_id:
        return
    profile = _profile_name()
    event_id = _event_id(profile, session_id, turn_id)
    now = time.time()
    pid = os.getpid()
    work_description = _work_description(user_message)
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": event_id,
        "profile": profile,
        "sessionId": str(session_id),
        "turnId": str(turn_id),
        "taskId": str(task_id),
        "state": "running",
        "startedAt": now,
        "updatedAt": now,
        "model": str(model),
        "platform": str(platform),
        "writerPid": pid,
        "writerProcessStart": _process_start(pid),
    }
    if work_description:
        record["workDescription"] = work_description
    with _profile_lock(profile):
        path = _event_path(profile, event_id)
        try:
            existing = _read_event(path)
        except FileNotFoundError:
            existing = None
        except (ValueError, TypeError):
            existing = None
        if existing is not None:
            if isinstance(existing, dict) and existing.get("eventId") == event_id:
                if existing.get("state") != "running":
                    return
                existing.update(
                    {
                        "updatedAt": now,
                        "model": str(model),
                        "platform": str(platform),
                        "writerPid": pid,
                        "writerProcessStart": _process_start(pid),
                    }
                )
                if work_description:
                    existing["workDescription"] = work_description
                _atomic_json(path, existing)
                return
        _atomic_json(path, record)


def on_turn_start(**kwargs: Any) -> None:
    try:
        _write_turn_start(**kwargs)
    except Exception:
        logging.getLogger(__name__).warning("Hermes Watcher could not record turn start", exc_info=True)


def _resolve_context_max(model: str, provider: str, base_url: str) -> int:
    key = (str(model), str(provider), str(base_url))
    if key in _CONTEXT_MAX_CACHE:
        return _CONTEXT_MAX_CACHE[key]
    config_context_length = None
    custom_providers = None
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        try:
            from hermes_cli.config import get_compatible_custom_providers

            custom_providers = get_compatible_custom_providers(config)
        except (ImportError, AttributeError):
            pass
        model_config = config.get("model", {})
        if isinstance(model_config, dict):
            configured = model_config.get("context_length")
            if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
                config_context_length = configured
    except Exception:
        pass
    if custom_providers and base_url and model:
        try:
            from hermes_cli.config import get_custom_provider_context_length

            custom_context = get_custom_provider_context_length(
                model=str(model),
                base_url=str(base_url),
                custom_providers=custom_providers,
            )
            if (
                isinstance(custom_context, int)
                and not isinstance(custom_context, bool)
                and custom_context > 0
            ):
                _CONTEXT_MAX_CACHE[key] = custom_context
                return custom_context
        except (ImportError, AttributeError):
            pass
    from agent import model_metadata

    resolved = int(
        model_metadata.get_model_context_length(
            str(model),
            base_url=str(base_url),
            config_context_length=config_context_length,
            provider=str(provider),
            custom_providers=custom_providers,
        )
        or 0
    )
    fallback_key = (str(model), str(base_url) or "")
    if config_context_length is None and fallback_key in getattr(
        model_metadata, "_FALLBACK_WARNED", set()
    ):
        return 0
    if resolved > 0:
        _CONTEXT_MAX_CACHE[key] = resolved
    return resolved


def _sanitize_api_call_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000_000:
        return None
    return value


def _sanitize_api_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reasoning_level_from_request(request: Any) -> str:
    """Extract only the effective, non-sensitive reasoning label from a request."""
    if not isinstance(request, dict):
        return ""
    body = request.get("body")
    if not isinstance(body, dict):
        return ""

    def normalize(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        level = value.strip().lower()
        if level in {"none", "off", "disabled"}:
            return "off"
        return level if level in _REASONING_LEVELS else ""

    for key in ("reasoning_effort", "verbosity"):
        if level := normalize(body.get(key)):
            return level

    extra_body = body.get("extra_body")
    containers = [body]
    if isinstance(extra_body, dict):
        containers.append(extra_body)
    for container in containers:
        reasoning = container.get("reasoning")
        if isinstance(reasoning, dict):
            if reasoning.get("enabled") is False:
                return "off"
            for key in ("effort", "level"):
                if level := normalize(reasoning.get(key)):
                    return level
        thinking = container.get("thinking")
        if isinstance(thinking, dict):
            thinking_type = str(thinking.get("type", "")).strip().lower()
            if thinking_type == "disabled":
                return "off"
            if thinking_type == "enabled":
                return "on"
    return ""


def _write_api_request(
    session_id: str = "",
    turn_id: str = "",
    model: str = "",
    platform: str = "",
    provider: str = "",
    base_url: str = "",
    approx_input_tokens: int = 0,
    api_call_count: Any = None,
    api_request_id: Any = None,
    confirmed: bool = False,
    request: Any = None,
    **_: Any,
) -> None:
    session_id = "" if session_id is None else str(session_id)
    turn_id = "" if turn_id is None else str(turn_id)
    if not session_id and not turn_id:
        return
    if isinstance(approx_input_tokens, bool):
        return
    context_used = int(approx_input_tokens or 0)
    if context_used <= 0:
        return
    context_max = _resolve_context_max(str(model), str(provider), str(base_url))
    if context_max <= 0:
        return
    profile = _profile_name()
    event_id = _event_id(profile, session_id, turn_id)
    with _profile_lock(profile):
        path = _event_path(profile, event_id)
        record = _read_event(path)
        if not isinstance(record, dict) or record.get("state") != "running":
            return
        call_count = _sanitize_api_call_count(api_call_count)
        request_id = _sanitize_api_request_id(api_request_id)
        previous_count = _sanitize_api_call_count(record.get("contextApiCallCount"))
        previous_request_id = str(record.get("contextApiRequestId", ""))
        if previous_count is not None:
            if call_count is None or call_count < previous_count:
                return
            if (
                call_count == previous_count
                and record.get("contextConfirmed") is True
                and not confirmed
            ):
                return
        elif (
            request_id
            and request_id == previous_request_id
            and record.get("contextConfirmed") is True
            and not confirmed
        ):
            return
        now = time.time()
        record.update(
            {
                "updatedAt": now,
                "model": str(model),
                "platform": str(platform),
                "contextUsed": context_used,
                "contextMax": context_max,
                "contextPercent": max(
                    0, min(100, round(Fraction(context_used, context_max) * 100))
                ),
                "contextConfirmed": bool(confirmed),
            }
        )
        reasoning_level = _reasoning_level_from_request(request)
        if reasoning_level:
            record["reasoningLevel"] = reasoning_level
        if call_count is not None:
            record["contextApiCallCount"] = call_count
        if request_id:
            record["contextApiRequestId"] = request_id
        _atomic_json(path, record)


def on_api_request(**kwargs: Any) -> None:
    try:
        _write_api_request(**kwargs)
    except Exception:
        logging.getLogger(__name__).warning(
            "Hermes Watcher could not record context usage", exc_info=True
        )


def on_api_response(usage: Any = None, **kwargs: Any) -> None:
    try:
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens")
        if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, (int, float)):
            return
        _write_api_request(approx_input_tokens=int(prompt_tokens), confirmed=True, **kwargs)
    except Exception:
        logging.getLogger(__name__).warning(
            "Hermes Watcher could not record provider context usage", exc_info=True
        )


def _write_turn_end(
    session_id: str = "",
    turn_id: str = "",
    completed: bool = False,
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: str = "",
    **_: Any,
) -> None:
    session_id = "" if session_id is None else str(session_id)
    turn_id = "" if turn_id is None else str(turn_id)
    if not session_id and not turn_id:
        raise RuntimeError("turn end has no lifecycle identifier")
    profile = _profile_name()
    with _profile_lock(profile):
        if turn_id:
            event_id = _event_id(profile, str(session_id), str(turn_id))
            path = _event_path(profile, event_id)
            record = _read_event(path)
        else:
            running_matches: list[tuple[Path, dict[str, Any]]] = []
            stale_matches: list[tuple[Path, dict[str, Any]]] = []
            directory = _event_path(profile, "lookup").parent
            tree = ManagedTree(_state_root())
            for name in tree.list_regular_files(("events", profile), suffix=".json"):
                candidate = directory / name
                try:
                    value = _read_event(candidate)
                except (OSError, ValueError, TypeError):
                    continue
                if (
                    isinstance(value, dict)
                    and value.get("state") in {"running", "stale"}
                    and value.get("profile") == profile
                    and value.get("sessionId") == str(session_id)
                ):
                    target = running_matches if value.get("state") == "running" else stale_matches
                    target.append((candidate, value))
            if len(running_matches) == 1:
                path, record = running_matches[0]
            elif len(running_matches) > 1 or len(stale_matches) != 1:
                raise RuntimeError("turn end does not identify one running record")
            else:
                path, record = stale_matches[0]
        if not isinstance(record, dict) or record.get("state") not in {"running", "stale"}:
            raise RuntimeError("turn end does not identify an unfinished record")
        finished = time.time()
        state = "interrupted" if interrupted else ("failed" if failed else ("succeeded" if completed else "failed"))
        record.update(
            {
                "state": state,
                "updatedAt": finished,
                "finishedAt": finished,
                "durationSec": max(0.0, finished - float(record["startedAt"])),
                "exitReason": str(turn_exit_reason),
            }
        )
        _atomic_json(path, record)


def on_turn_end(**kwargs: Any) -> None:
    try:
        _write_turn_end(**kwargs)
    except Exception:
        logging.getLogger(__name__).warning("Hermes Watcher could not record turn end", exc_info=True)
