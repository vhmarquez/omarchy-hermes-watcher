"""Hermes Watcher observer plugin."""

from .omarchy_bot_status import (
    on_api_request,
    on_api_response,
    on_turn_end,
    on_turn_start,
    record_observer_loaded,
)


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_turn_start)
    ctx.register_hook("pre_api_request", on_api_request)
    ctx.register_hook("post_api_request", on_api_response)
    ctx.register_hook("on_session_end", on_turn_end)
    record_observer_loaded()
