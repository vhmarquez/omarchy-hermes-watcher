import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes-plugin" / "omarchy_bot_status.py"


def load_observer():
    spec = importlib.util.spec_from_file_location("omarchy_bot_status", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_observer_plugin():
    path = ROOT / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("omarchy_bot_status_plugin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class ObserverLifecycleTests(unittest.TestCase):
    def test_privacy_disable_linearizes_with_an_inflight_turn_start(self):
        from tests.test_collector import load_collector

        observer = load_observer()
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "omarchy/hermes-bots"
            policy_read = threading.Event()
            continue_write = threading.Event()
            shared_lock = threading.Lock()
            real_policy_read = observer._show_work_description

            def delayed_policy_read():
                selected = real_policy_read()
                policy_read.set()
                continue_write.wait(timeout=2)
                return selected

            @contextmanager
            def synchronized_lock(*_args, **_kwargs):
                with shared_lock:
                    yield

            def start_turn():
                with mock.patch.dict(
                    os.environ,
                    {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
                ), mock.patch.object(
                    observer, "_show_work_description", side_effect=delayed_policy_read
                ), mock.patch.object(observer, "_privacy_lock", synchronized_lock):
                    observer.on_turn_start(
                        session_id="session-race",
                        turn_id="turn-race",
                        user_message="Private race description",
                    )

            thread = threading.Thread(target=start_turn)
            thread.start()
            self.assertTrue(policy_read.wait(timeout=2))
            with mock.patch.object(collector, "_privacy_policy_lock", synchronized_lock):
                privacy_thread = threading.Thread(
                    target=collector.configure_privacy,
                    args=(root,),
                    kwargs={"show_work_description": False},
                )
                privacy_thread.start()
                continue_write.set()
                privacy_thread.join(timeout=2)
                self.assertFalse(privacy_thread.is_alive())
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

            record = json.loads(
                next((root / "events/coder").glob("*.json")).read_text()
            )
            self.assertNotIn("workDescription", record)

    def test_observer_bounds_all_persisted_hook_strings(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            long_value = "x" * 5000
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(
                    session_id=long_value,
                    turn_id=long_value,
                    task_id=long_value,
                    model=long_value,
                    platform=long_value,
                )
                observer.on_turn_end(
                    session_id=long_value,
                    turn_id=long_value,
                    failed=True,
                    turn_exit_reason=long_value,
                )

            record = json.loads(
                next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json")).read_text()
            )
            self.assertLessEqual(len(record["sessionId"]), 128)
            self.assertLessEqual(len(record["turnId"]), 128)
            self.assertLessEqual(len(record["taskId"]), 128)
            self.assertLessEqual(len(record["model"]), 200)
            self.assertLessEqual(len(record["platform"]), 100)
            self.assertLessEqual(len(record["exitReason"]), 200)

    def test_observer_registration_writes_process_handshake(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer.os, "getpid", return_value=123), mock.patch.object(
                observer, "_process_start", return_value="456"
            ):
                observer.record_observer_loaded()

            records = list((Path(tmp) / "omarchy/hermes-bots/processes/coder").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text())
            self.assertEqual(
                record,
                {
                    "schemaVersion": 1,
                    "profile": "coder",
                    "writerPid": 123,
                    "writerProcessStart": "456",
                    "capabilities": ["work-description-policy-v1"],
                },
            )

    def test_context_resolver_rejects_hermes_unknown_model_fallback(self):
        observer = load_observer()
        fake_agent = types.ModuleType("agent")
        fake_metadata = types.ModuleType("agent.model_metadata")
        setattr(fake_metadata, "_FALLBACK_WARNED", set())

        def resolve(model, *, base_url="", **_kwargs):
            getattr(fake_metadata, "_FALLBACK_WARNED").add((model, base_url))
            return 256_000

        setattr(fake_metadata, "get_model_context_length", resolve)
        observer._CONTEXT_MAX_CACHE.clear()
        with mock.patch.dict(
            sys.modules,
            {"agent": fake_agent, "agent.model_metadata": fake_metadata},
        ):
            self.assertEqual(
                observer._resolve_context_max("unknown-model", "unknown", ""), 0
            )

    def test_context_resolver_prefers_explicit_config_over_stale_fallback_warning(self):
        observer = load_observer()
        fake_agent = types.ModuleType("agent")
        fake_metadata = types.ModuleType("agent.model_metadata")
        setattr(fake_metadata, "_FALLBACK_WARNED", {("unknown-model", "")})
        setattr(
            fake_metadata,
            "get_model_context_length",
            lambda _model, *, config_context_length=None, **_kwargs: config_context_length,
        )
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_config = types.ModuleType("hermes_cli.config")
        setattr(fake_config, "load_config", lambda: {"model": {"context_length": 131_072}})
        observer._CONTEXT_MAX_CACHE.clear()

        with mock.patch.dict(
            sys.modules,
            {
                "agent": fake_agent,
                "agent.model_metadata": fake_metadata,
                "hermes_cli": fake_hermes_cli,
                "hermes_cli.config": fake_config,
            },
        ):
            self.assertEqual(
                observer._resolve_context_max("unknown-model", "unknown", ""), 131_072
            )

    def test_context_resolver_honors_compatible_custom_provider_metadata(self):
        observer = load_observer()
        fake_agent = types.ModuleType("agent")
        fake_metadata = types.ModuleType("agent.model_metadata")
        base_url = "http://127.0.0.1:9000/v1"
        fallback_warned = {("custom-model", base_url)}
        setattr(fake_metadata, "_FALLBACK_WARNED", fallback_warned)
        expected_providers = [
            {
                "name": "local-provider",
                "models": [{"id": "custom-model", "context_length": 98_304}],
            }
        ]

        def resolve(model, *, base_url="", custom_providers=None, **_kwargs):
            fallback_warned.add((model, base_url))
            return 256_000

        setattr(fake_metadata, "get_model_context_length", resolve)
        fake_hermes_cli = types.ModuleType("hermes_cli")
        fake_config = types.ModuleType("hermes_cli.config")
        config = {"providers": {"local-provider": {"enabled": True}}}
        setattr(fake_config, "load_config", lambda: config)
        setattr(
            fake_config,
            "get_compatible_custom_providers",
            lambda loaded: expected_providers if loaded is config else [],
        )
        setattr(
            fake_config,
            "get_custom_provider_context_length",
            lambda *, model, base_url, custom_providers: (
                98_304
                if model == "custom-model"
                and base_url == "http://127.0.0.1:9000/v1"
                and custom_providers == expected_providers
                else None
            ),
        )
        observer._CONTEXT_MAX_CACHE.clear()

        with mock.patch.dict(
            sys.modules,
            {
                "agent": fake_agent,
                "agent.model_metadata": fake_metadata,
                "hermes_cli": fake_hermes_cli,
                "hermes_cli.config": fake_config,
            },
        ):
            self.assertEqual(
                observer._resolve_context_max("custom-model", "local-provider", base_url),
                98_304,
            )

    def test_plugin_helper_copies_match_collector_helpers(self):
        for name in ("hermes_proc.py", "secure_paths.py"):
            self.assertEqual(
                (ROOT / name).read_bytes(),
                (ROOT / "hermes-plugin" / name).read_bytes(),
            )

    def test_process_start_handles_spaces_and_parentheses_in_comm(self):
        observer = load_observer()
        stat = "101 (hermes worker (child)) S " + " ".join(["0"] * 18) + " 25000\n"

        with mock.patch.object(observer.Path, "read_text", return_value=stat):
            self.assertEqual(observer._process_start(101), "25000")

    def test_plugin_manifest_declares_only_observer_hooks(self):
        manifest = (ROOT / "hermes-plugin/plugin.yaml").read_text()
        self.assertIn("name: omarchy-bot-status", manifest)
        self.assertIn("provides_hooks:", manifest)
        self.assertIn("- pre_llm_call", manifest)
        self.assertIn("- pre_api_request", manifest)
        self.assertIn("- post_api_request", manifest)
        self.assertIn("- on_session_end", manifest)
        self.assertNotIn("tools:", manifest)

    def test_plugin_registers_documented_turn_hooks(self):
        plugin = load_observer_plugin()
        context = FakeContext()
        plugin.register(context)
        self.assertEqual(
            set(context.hooks),
            {"pre_llm_call", "pre_api_request", "post_api_request", "on_session_end"},
        )
        self.assertEqual(context.hooks["pre_llm_call"].__name__, "on_turn_start")
        self.assertEqual(context.hooks["pre_api_request"].__name__, "on_api_request")
        self.assertEqual(context.hooks["post_api_request"].__name__, "on_api_response")
        self.assertEqual(context.hooks["on_session_end"].__name__, "on_turn_end")

    def test_api_request_updates_running_record_with_context_occupancy(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=272_000):
                observer.on_turn_start(
                    session_id="session-1", turn_id="turn-1", model="gpt-5.6-sol"
                )
                observer.on_api_request(
                    session_id="session-1",
                    turn_id="turn-1",
                    model="gpt-5.6-sol",
                    provider="openai-codex",
                    base_url="https://chatgpt.com/backend-api/codex",
                    approx_input_tokens=186_000,
                    request={"messages": [{"content": "SECRET PROMPT"}]},
                )

                path = next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json"))
                record = json.loads(path.read_text())

            self.assertEqual(record["contextUsed"], 186_000)
            self.assertEqual(record["contextMax"], 272_000)
            self.assertEqual(record["contextPercent"], 68)
            self.assertNotIn("SECRET PROMPT", json.dumps(record))

    def test_api_request_records_only_the_sanitized_runtime_reasoning_level(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=900_000):
                observer.on_turn_start(
                    session_id="session-1", turn_id="turn-1", model="gpt-5.6-sol-900k"
                )
                observer.on_api_request(
                    session_id="session-1",
                    turn_id="turn-1",
                    model="gpt-5.6-sol-900k",
                    provider="openai-codex",
                    approx_input_tokens=186_000,
                    request={
                        "method": "POST",
                        "body": {
                            "reasoning": {"effort": "max", "summary": "auto"},
                            "input": [{"content": "SECRET PROMPT"}],
                        },
                    },
                )

                path = next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json"))
                record = json.loads(path.read_text())

            self.assertEqual(record["reasoningLevel"], "max")
            self.assertNotIn("SECRET PROMPT", json.dumps(record))
            self.assertNotIn("request", record)

    def test_api_request_replaces_turn_start_model_and_platform_with_request_labels(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=128_000):
                observer.on_turn_start(
                    session_id="session-1",
                    turn_id="turn-1",
                    model="stale-model",
                    platform="stale-platform",
                )
                observer.on_api_request(
                    session_id="session-1",
                    turn_id="turn-1",
                    model="fallback-model",
                    platform="fallback-platform",
                    provider="private-provider",
                    base_url="https://private.example/v1",
                    approx_input_tokens=64_000,
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["model"], "fallback-model")
            self.assertEqual(record["platform"], "fallback-platform")
            self.assertEqual(record["contextUsed"], 64_000)
            self.assertEqual(record["contextMax"], 128_000)
            self.assertNotIn("provider", record)
            self.assertNotIn("base_url", record)

    def test_api_response_replaces_estimate_with_provider_prompt_usage(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=272_000):
                observer.on_turn_start(session_id="s", turn_id="t", model="model")
                observer.on_api_request(
                    session_id="s", turn_id="t", model="model", approx_input_tokens=20_000
                )
                observer.on_api_response(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    usage={"prompt_tokens": 21_500, "completion_tokens": 400},
                    response={"output": "SECRET RESPONSE"},
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["contextUsed"], 21_500)
            self.assertEqual(record["contextPercent"], 8)
            self.assertNotIn("SECRET RESPONSE", json.dumps(record))

    def test_late_api_request_estimate_cannot_replace_confirmed_usage_for_same_call(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=272_000):
                observer.on_turn_start(session_id="s", turn_id="t", model="model")
                observer.on_api_response(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    api_call_count=7,
                    api_request_id="request-7",
                    usage={"prompt_tokens": 21_500},
                )
                observer.on_api_request(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    api_call_count=7,
                    api_request_id="request-7",
                    approx_input_tokens=20_000,
                    request={"messages": [{"content": "LATE SECRET PROMPT"}]},
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["contextUsed"], 21_500)
            self.assertNotIn("LATE SECRET PROMPT", json.dumps(record))

    def test_older_api_call_cannot_replace_newer_confirmed_usage(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=272_000):
                observer.on_turn_start(session_id="s", turn_id="t", model="model")
                observer.on_api_response(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    api_call_count=8,
                    api_request_id="request-8",
                    usage={"prompt_tokens": 30_000},
                )
                observer.on_api_response(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    api_call_count=7,
                    api_request_id="request-7",
                    usage={"prompt_tokens": 21_500},
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["contextUsed"], 30_000)

    def test_unsequenced_estimate_cannot_replace_sequenced_confirmed_usage(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=272_000):
                observer.on_turn_start(
                    session_id="s", turn_id="t", model="confirmed-model", platform="cli"
                )
                observer.on_api_response(
                    session_id="s",
                    turn_id="t",
                    model="confirmed-model",
                    platform="cli",
                    api_call_count=8,
                    api_request_id="request-8",
                    usage={"prompt_tokens": 30_000},
                )
                observer.on_api_request(
                    session_id="s",
                    turn_id="t",
                    model="stale-model",
                    platform="tui",
                    approx_input_tokens=20_000,
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["contextUsed"], 30_000)
            self.assertTrue(record["contextConfirmed"])
            self.assertEqual(record["contextApiCallCount"], 8)
            self.assertEqual(record["model"], "confirmed-model")
            self.assertEqual(record["platform"], "cli")

    def test_api_request_clamps_extreme_context_without_float_overflow(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer, "_resolve_context_max", return_value=1):
                observer.on_turn_start(session_id="s", turn_id="t", model="model")
                observer.on_api_request(
                    session_id="s",
                    turn_id="t",
                    model="model",
                    api_call_count=1,
                    approx_input_tokens=10**1000,
                )

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())

            self.assertEqual(record["contextPercent"], 100)

    def test_end_hook_missing_start_record_does_not_escape_into_hermes(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                with self.assertLogs("omarchy_bot_status", level="WARNING"):
                    observer.on_turn_end(session_id="missing", turn_id="missing", completed=True)

    def test_hook_write_failure_does_not_escape_into_hermes(self):
        observer = load_observer()
        with mock.patch.object(observer, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertLogs("omarchy_bot_status", level="WARNING"):
                observer.on_turn_start(session_id="s", turn_id="t")

    def test_turn_start_without_session_or_turn_id_is_ignored(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start()
                observer.on_turn_start()

                self.assertEqual(list(Path(tmp).rglob("*.json")), [])

    def test_turn_start_with_null_session_and_turn_id_is_ignored(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id=None, turn_id=None)

                self.assertEqual(list(Path(tmp).rglob("*.json")), [])

    def test_null_session_with_turn_id_can_be_finalized(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id=None, turn_id="turn-only")
                observer.on_turn_end(session_id=None, turn_id="turn-only", completed=True)

                records = [json.loads(path.read_text()) for path in Path(tmp).rglob("*.json")]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["sessionId"], "")
                self.assertEqual(records[0]["state"], "succeeded")

    def test_end_without_any_identifier_does_not_finalize_turn_only_record(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id=None, turn_id="turn-only")
                with self.assertLogs("omarchy_bot_status", level="WARNING"):
                    observer.on_turn_end(session_id=None, turn_id=None, completed=True)

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())
                self.assertEqual(record["state"], "running")

    def test_terminal_record_is_not_rewritten_by_conflicting_second_end(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ), mock.patch.object(observer.time, "time", side_effect=[10.0, 20.0, 30.0]):
                observer.on_turn_start(session_id="session", turn_id="turn")
                observer.on_turn_end(session_id="session", turn_id="turn", completed=True)
                with self.assertLogs("omarchy_bot_status", level="WARNING"):
                    observer.on_turn_end(session_id="session", turn_id="turn", failed=True)

                record = json.loads(next(Path(tmp).rglob("*.json")).read_text())
                self.assertEqual(record["state"], "succeeded")
                self.assertEqual(record["finishedAt"], 20.0)

    def test_turn_end_without_turn_id_finalizes_unique_running_session_record(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id="target", turn_id="turn-1")
                observer.on_turn_start(session_id="other", turn_id="turn-2")
                observer.on_turn_end(
                    session_id="target", completed=True, assistant_response="SECRET RESPONSE"
                )

                records = [
                    json.loads(path.read_text())
                    for path in (Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json")
                ]

            target = next(record for record in records if record["sessionId"] == "target")
            other = next(record for record in records if record["sessionId"] == "other")
            self.assertEqual(target["state"], "succeeded")
            self.assertEqual(other["state"], "running")
            self.assertNotIn("SECRET RESPONSE", json.dumps(target))

    def test_end_without_turn_id_can_replace_matching_stale_record(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id="target", turn_id="turn-1")
                path = next(Path(tmp).rglob("*.json"))
                record = json.loads(path.read_text())
                record["state"] = "stale"
                record["finishedAt"] = record["startedAt"] + 1
                record["durationSec"] = 1.0
                path.write_text(json.dumps(record))

                observer.on_turn_end(session_id="target", turn_id=None, completed=True)

                self.assertEqual(json.loads(path.read_text())["state"], "succeeded")

    def test_end_without_turn_id_prefers_unique_running_over_older_stale_record(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id="target", turn_id="turn-1")
                directory = Path(tmp) / "omarchy/hermes-bots/events/coder"
                first_path = next(directory.glob("*.json"))
                first = json.loads(first_path.read_text())
                first["state"] = "stale"
                first["finishedAt"] = first["startedAt"] + 1
                first["durationSec"] = 1.0
                first_path.write_text(json.dumps(first))
                observer.on_turn_start(session_id="target", turn_id="turn-2")

                observer.on_turn_end(session_id="target", turn_id=None, completed=True)

                records = [json.loads(path.read_text()) for path in directory.glob("*.json")]
                states = {record["turnId"]: record["state"] for record in records}
                self.assertEqual(states, {"turn-1": "stale", "turn-2": "succeeded"})

    def test_turn_end_without_turn_id_fails_closed_for_ambiguous_running_session(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(session_id="same", turn_id="turn-1")
                observer.on_turn_start(session_id="same", turn_id="turn-2")
                with self.assertLogs("omarchy_bot_status", level="WARNING"):
                    observer.on_turn_end(session_id="same", completed=True)
                states = [
                    json.loads(path.read_text())["state"]
                    for path in (Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json")
                ]

            self.assertEqual(states, ["running", "running"])

    def test_turn_end_distinguishes_failure_and_interruption(self):
        observer = load_observer()
        cases = [
            ({"failed": True, "interrupted": False}, "failed"),
            ({"failed": False, "interrupted": True}, "interrupted"),
        ]
        for flags, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(
                    os.environ,
                    {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
                ):
                    observer.on_turn_start(session_id="s", turn_id="t")
                    observer.on_turn_end(session_id="s", turn_id="t", completed=False, **flags)
                    record = next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json"))
                    self.assertEqual(json.loads(record.read_text())["state"], expected)

    def test_turn_end_replaces_running_record_with_success(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/researcher"},
            ):
                observer.on_turn_start(session_id="session-1", turn_id="turn-1")
                observer.on_turn_end(
                    session_id="session-1",
                    turn_id="turn-1",
                    completed=True,
                    failed=False,
                    interrupted=False,
                    turn_exit_reason="completed",
                    assistant_response="SECRET RESPONSE",
                )
                record = next((Path(tmp) / "omarchy/hermes-bots/events/researcher").glob("*.json"))
                data = json.loads(record.read_text())
                self.assertEqual(data["state"], "succeeded")
                self.assertEqual(data["exitReason"], "completed")
                self.assertGreaterEqual(data["finishedAt"], data["startedAt"])
                self.assertNotIn("SECRET RESPONSE", record.read_text())

    def test_turn_end_removes_the_work_description_from_terminal_history(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(
                    session_id="session-terminal",
                    turn_id="turn-terminal",
                    user_message="Temporary work description",
                )
                observer.on_turn_end(
                    session_id="session-terminal",
                    turn_id="turn-terminal",
                    completed=True,
                )

            record = json.loads(
                next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json")).read_text()
            )
            self.assertNotIn("workDescription", record)

    def test_turn_start_writes_bounded_work_description_without_history(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            old_state = os.environ.get("XDG_STATE_HOME")
            old_home = os.environ.get("HERMES_HOME")
            os.environ["XDG_STATE_HOME"] = tmp
            os.environ["HERMES_HOME"] = "/tmp/hermes/profiles/researcher"
            try:
                observer.on_turn_start(
                    session_id="session/unsafe",
                    task_id="task-1",
                    turn_id="turn-1",
                    model="model-x",
                    platform="cli",
                    user_message="  Investigate\ncache   misses  ",
                    conversation_history=[{"content": "SECRET HISTORY"}],
                )
                records = list((Path(tmp) / "omarchy/hermes-bots/events/researcher").glob("*.json"))
                self.assertEqual(len(records), 1)
                data = json.loads(records[0].read_text())
                self.assertEqual(data["state"], "running")
                self.assertEqual(data["profile"], "researcher")
                self.assertEqual(data["sessionId"], "session/unsafe")
                self.assertEqual(data["turnId"], "turn-1")
                self.assertEqual(data["workDescription"], "Investigate cache misses")
                self.assertNotIn("SECRET HISTORY", records[0].read_text())
                self.assertNotIn("session/unsafe", records[0].name)
                self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(records[0].parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(records[0].parent.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(records[0].parent.parent.parent.stat().st_mode & 0o777, 0o700)
            finally:
                if old_state is None:
                    os.environ.pop("XDG_STATE_HOME", None)
                else:
                    os.environ["XDG_STATE_HOME"] = old_state
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home

    def test_disabled_work_description_policy_prevents_prompt_persistence(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "omarchy/hermes-bots"
            state_root.mkdir(parents=True)
            (state_root / "privacy.json").write_text(
                json.dumps({"schemaVersion": 1, "showWorkDescription": False})
            )
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(
                    session_id="session-private",
                    turn_id="turn-private",
                    user_message="Private request text",
                )

            record = json.loads(
                next((state_root / "events/coder").glob("*.json")).read_text()
            )
            self.assertNotIn("workDescription", record)
            record_path = next((state_root / "events/coder").glob("*.json"))
            record["workDescription"] = "Legacy description"
            record_path.write_text(json.dumps(record))
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/coder"},
            ):
                observer.on_turn_start(
                    session_id="session-private",
                    turn_id="turn-private",
                    user_message="Another private request",
                )
            self.assertNotIn(
                "workDescription",
                json.loads(record_path.read_text()),
            )

    def test_work_description_is_plain_text_and_bounded(self):
        observer = load_observer()

        self.assertEqual(
            observer._work_description("Build\x00 the\tstatus\nwidget"),
            "Build the status widget",
        )
        self.assertEqual(len(observer._work_description("x" * 500)), 160)
        self.assertEqual(observer._work_description({"prompt": "private"}), "")
        sensitive_value = "credential-" + "sentinel"
        filtered = observer._work_description(f"Deploy password={sensitive_value}")
        self.assertNotIn(sensitive_value, filtered)
        self.assertEqual(filtered, "Deploy password=[REDACTED]")
        authorization = observer._work_description(
            f"Call Authorization: Basic {sensitive_value} now"
        )
        self.assertNotIn(sensitive_value, authorization)
        self.assertEqual(authorization, "Call Authorization:[REDACTED]")
        spaced_secret = observer._work_description(
            f"Deploy password={sensitive_value} second-word; continue"
        )
        self.assertNotIn("second-word", spaced_secret)
        self.assertEqual(spaced_secret, "Deploy password=[REDACTED]")
        digest = observer._work_description(
            f"Call Authorization: Digest username={sensitive_value}, nonce=second-word"
        )
        self.assertNotIn(sensitive_value, digest)
        self.assertNotIn("second-word", digest)
        self.assertEqual(digest, "Call Authorization:[REDACTED]")
        quoted = observer._work_description(
            f'Deploy password="{sensitive_value},second-word"; continue'
        )
        self.assertNotIn(sensitive_value, quoted)
        self.assertNotIn("second-word", quoted)
        self.assertEqual(quoted, "Deploy password=[REDACTED]")
        semicolon_secret = observer._work_description(
            f'Deploy password="{sensitive_value};second-word"'
        )
        self.assertNotIn(sensitive_value, semicolon_secret)
        self.assertNotIn("second-word", semicolon_secret)
        for key in (
            "OPENAI_API_KEY",
            "CLIENT_SECRET",
            "REFRESH_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
        ):
            with self.subTest(key=key):
                filtered_name = observer._work_description(
                    f"Deploy {key}={sensitive_value}"
                )
                self.assertNotIn(sensitive_value, filtered_name)

    def test_repeated_turn_start_preserves_the_original_start_time(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            previous_state = os.environ.get("XDG_STATE_HOME")
            previous_home = os.environ.get("HERMES_HOME")
            os.environ["XDG_STATE_HOME"] = tmp
            os.environ["HERMES_HOME"] = "/tmp/hermes/profiles/coder"
            try:
                with mock.patch.object(observer.time, "time", side_effect=[100.0, 200.0]):
                    observer.on_turn_start(
                        session_id="session-1", turn_id="turn-1", task_id="task-1"
                    )
                    observer.on_turn_start(
                        session_id="session-1", turn_id="turn-1", task_id="task-1"
                    )
                record = json.loads(next((Path(tmp) / "omarchy/hermes-bots/events/coder").glob("*.json")).read_text())
                self.assertEqual(record["startedAt"], 100.0)
                self.assertEqual(record["updatedAt"], 200.0)
            finally:
                if previous_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous_home
                if previous_state is None:
                    os.environ.pop("XDG_STATE_HOME", None)
                else:
                    os.environ["XDG_STATE_HOME"] = previous_state

    def test_profile_name_cannot_escape_the_events_directory(self):
        observer = load_observer()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": tmp, "HERMES_HOME": "/tmp/hermes/profiles/.."},
            ):
                observer.on_turn_start(session_id="s", turn_id="t")

            records = list((Path(tmp) / "omarchy/hermes-bots/events/unknown").glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["profile"], "unknown")

    def test_turn_start_rejects_symlinks_in_managed_state_hierarchy(self):
        observer = load_observer()
        for managed_path in ("omarchy", "omarchy/hermes-bots/events/coder/.writer.lock"):
            with self.subTest(managed_path=managed_path), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp) / "state"
                victim = Path(tmp) / "victim"
                if managed_path == "omarchy":
                    base.mkdir()
                    victim.mkdir()
                    (base / managed_path).symlink_to(victim, target_is_directory=True)
                else:
                    lock = base / managed_path
                    lock.parent.mkdir(parents=True)
                    victim.write_text("do not change")
                    lock.symlink_to(victim)

                with mock.patch.dict(
                    os.environ,
                    {"XDG_STATE_HOME": str(base), "HERMES_HOME": "/tmp/hermes/profiles/coder"},
                ):
                    with self.assertLogs("omarchy_bot_status", level="WARNING"):
                        observer.on_turn_start(session_id="s", turn_id="t")

                if victim.is_file():
                    self.assertEqual(victim.read_text(), "do not change")
                else:
                    self.assertEqual(list(victim.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
