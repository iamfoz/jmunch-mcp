"""Regression: the verb loop must continue the REAL conversation.

Before the fix, _verb_loop rebuilt a synthetic context via
_compact_base_messages()/_extract_user_and_handle() — it dropped all history
and kept the *first* user message. Deep in a conversation the model was
handed the opening turn ("Hello") as the whole request and lost the thread.
"""
from __future__ import annotations

import asyncio
import copy
import json

from jmunch_mcp.gateway.anthropic_route import _DRILL_IN_SYSTEM as _ANTH_DRILL
from jmunch_mcp.gateway.anthropic_route import handle_messages
from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.openai_route import _DRILL_IN_SYSTEM as _OPENAI_DRILL
from jmunch_mcp.gateway.openai_route import handle_chat_completions
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


class _FakeUpstream:
    def __init__(self, script, kind):
        self.script = list(script)
        self.calls: list[dict] = []
        self.spec = UpstreamSpec(name="fake", kind=kind, base_url="http://fake")

    async def complete(self, request):
        self.calls.append(copy.deepcopy(request))  # snapshot — loop reuses dicts
        return self.script.pop(0)

    async def close(self):
        return None


def _core(tmp_path, monkeypatch):
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    registry = HandleRegistry()
    return dict(
        registry=registry,
        tracker=SavingsTracker(path=tmp_path / "_savings.json"),
        dispatcher=Dispatcher(registry, SessionStats()),
        metrics=MetricsDB(),
    )


# ---------------------------------------------------------------------------
# OpenAI route — the route with the confirmed bug
# ---------------------------------------------------------------------------

def test_openai_verb_loop_preserves_full_conversation(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    # turn 1: model calls a verb → verb loop runs. turn 2: plain final answer.
    turn1 = {"id": "c1", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "jmunch_list_handles", "arguments": "{}"}}]}}]}
    turn2 = {"id": "c2", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "The total is 42."}}]}
    fake = _FakeUpstream([turn1, turn2], kind="openai")

    current_request = "Summarize the records and give me the total."
    req = {
        "model": "gpt-4",
        "tools": [{"type": "function",
                   "function": {"name": "app_tool", "description": "d", "parameters": {}}}],
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},                 # first user msg
            {"role": "assistant", "content": "Hi! How can I help?"},
            {"role": "user", "content": current_request},          # the CURRENT turn
        ],
    }
    config = GatewayConfig(
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(),
    )
    status, _ = asyncio.run(handle_chat_completions(
        req, upstream_override=None, config=config,
        upstream_factory=lambda spec: fake, **core,
    ))
    assert status == 200
    assert len(fake.calls) == 2

    follow_up = fake.calls[1]["messages"]
    # The real conversation, not a 4-message synthetic reconstruction.
    assert [m["role"] for m in follow_up] == [
        "system", "user", "assistant", "user", "assistant", "tool"]

    user_texts = [m["content"] for m in follow_up if m.get("role") == "user"]
    assert user_texts == ["Hello", current_request]   # both turns, in order
    assert user_texts[-1] == current_request          # model works from the CURRENT turn

    systems = [m for m in follow_up if m.get("role") == "system"]
    assert len(systems) == 1
    assert "You are a helpful assistant." in systems[0]["content"]   # real prompt kept
    assert _OPENAI_DRILL in systems[0]["content"]                    # drill-in merged in

    # The synthetic-context defect must be gone entirely.
    assert "The user's request was" not in json.dumps(follow_up)


def test_openai_verb_loop_preserves_full_tools_array(tmp_path, monkeypatch):
    """Regression: the follow-up call must carry the app's REAL tools
    (not just `jmunch_*` verbs). Stripping to jmunch-only saved a few
    KB but caused the model to wrongly conclude its tools were gone
    mid-drill-in ('I do not have access to a bash/terminal tool')."""
    core = _core(tmp_path, monkeypatch)
    turn1 = {"id": "c1", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "jmunch_list_handles", "arguments": "{}"}}]}}]}
    turn2 = {"id": "c2", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "done"}}]}
    fake = _FakeUpstream([turn1, turn2], kind="openai")

    app_tools = [
        {"type": "function", "function": {"name": "shell_exec",
                                          "description": "Run a shell command",
                                          "parameters": {}}},
        {"type": "function", "function": {"name": "read_file",
                                          "description": "Read a file",
                                          "parameters": {}}},
    ]
    req = {
        "model": "gpt-4",
        "tools": list(app_tools),
        "messages": [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "run something for me"},
        ],
    }
    config = GatewayConfig(
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(),
    )
    status, _ = asyncio.run(handle_chat_completions(
        req, upstream_override=None, config=config,
        upstream_factory=lambda spec: fake, **core,
    ))
    assert status == 200
    assert len(fake.calls) == 2

    follow_up_names = {
        t["function"]["name"]
        for t in (fake.calls[1].get("tools") or [])
        if isinstance(t, dict) and t.get("type") == "function"
    }
    # The app's real tools MUST survive into the drill-in iteration.
    assert "shell_exec" in follow_up_names
    assert "read_file" in follow_up_names


def test_openai_verb_loop_no_system_message_still_keeps_history(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    turn1 = {"id": "c1", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "jmunch_list_handles", "arguments": "{}"}}]}}]}
    turn2 = {"id": "c2", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "done"}}]}
    fake = _FakeUpstream([turn1, turn2], kind="openai")
    req = {
        "model": "gpt-4",
        "tools": [{"type": "function",
                   "function": {"name": "t", "description": "d", "parameters": {}}}],
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "the real question"},
        ],
    }
    config = GatewayConfig(
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(),
    )
    asyncio.run(handle_chat_completions(
        req, upstream_override=None, config=config,
        upstream_factory=lambda spec: fake, **core,
    ))
    follow_up = fake.calls[1]["messages"]
    # No system message in the request → the verb loop still leads with a
    # system message carrying the drill-in guidance, and the full
    # conversation is preserved underneath it. (`in`, not `==`: other
    # gateway features may also contribute system text.)
    assert follow_up[0]["role"] == "system"
    assert _OPENAI_DRILL in follow_up[0]["content"]
    assert [m["content"] for m in follow_up if m.get("role") == "user"] == [
        "Hello", "the real question"]


# ---------------------------------------------------------------------------
# Anthropic route — loop already continues the real conversation; verify that,
# plus the drill-in guidance now lands in the top-level `system` field.
# ---------------------------------------------------------------------------

def test_anthropic_verb_loop_preserves_conversation_and_system(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    turn1 = {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus",
        "content": [{"type": "tool_use", "id": "tu1",
                     "name": "jmunch_list_handles", "input": {}}],
        "stop_reason": "tool_use"}
    turn2 = {"id": "m2", "type": "message", "role": "assistant", "model": "claude-opus",
        "content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}
    fake = _FakeUpstream([turn1, turn2], kind="anthropic")

    req = {
        "model": "claude-opus", "max_tokens": 256,
        "system": "You are a helpful assistant.",
        "tools": [{"name": "app_tool", "description": "d",
                   "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]},
            {"role": "user", "content": "Summarize and give the total."},
        ],
    }
    config = GatewayConfig(
        default_upstream="fake-a",
        upstreams=[UpstreamSpec(name="fake-a", kind="anthropic", base_url="http://fake")],
        interception=Interception(),
    )
    status, _ = asyncio.run(handle_messages(
        req, upstream_override=None, config=config,
        upstream_factory=lambda spec: fake, **core,
    ))
    assert status == 200
    assert len(fake.calls) == 2

    follow_up = fake.calls[1]
    msgs = follow_up["messages"]
    # Original 3 turns + assistant(tool_use) + user(tool_result).
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    str_user_turns = [m["content"] for m in msgs
                      if m.get("role") == "user" and isinstance(m.get("content"), str)]
    assert str_user_turns == ["Hello", "Summarize and give the total."]

    # Drill-in guidance merged into the top-level `system` field; real prompt kept.
    assert "You are a helpful assistant." in follow_up["system"]
    assert _ANTH_DRILL in follow_up["system"]
