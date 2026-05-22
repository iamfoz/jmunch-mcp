"""JMUNCH_DEBUG_DUMP — debug dump of the exact outbound upstream request."""
from __future__ import annotations

import asyncio
import json

import pytest

from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.debug_dump import dump_upstream_request, enabled
from jmunch_mcp.gateway.anthropic_route import handle_messages
from jmunch_mcp.gateway.openai_route import handle_chat_completions
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


def _dumps(tmp_path):
    return sorted((tmp_path / ".jmunch" / "debug").glob("*.json"))


# ---------------------------------------------------------------------------
# helper unit tests
# ---------------------------------------------------------------------------

def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JMUNCH_DEBUG_DUMP", raising=False)
    assert enabled() is False
    dump_upstream_request({"model": "gpt-4"}, route="openai", phase="first")
    assert not (tmp_path / ".jmunch" / "debug").exists()  # nothing written


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " yes "])
def test_truthy_values_enable(value, monkeypatch):
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", value)
    assert enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_non_truthy_values_disable(value, monkeypatch):
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", value)
    assert enabled() is False


def test_enabled_writes_complete_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", "1")
    request = {
        "model": "claude-opus",
        "system": "you are helpful",
        "tools": [{"name": "jmunch_peek"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": json.dumps({"handle": "h_x", "kind": "json"})},
        ],
    }
    dump_upstream_request(request, route="anthropic", phase="verb-loop")

    dumps = _dumps(tmp_path)
    assert len(dumps) == 1
    assert dumps[0].name.endswith("_anthropic_verb-loop.json")
    text = dumps[0].read_text()
    assert json.loads(text) == request          # complete + untruncated
    assert "\n  " in text                       # pretty-printed


def test_each_call_writes_a_separate_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", "1")
    for i in range(3):
        dump_upstream_request({"model": "gpt-4", "n": i}, route="openai", phase="first")
    assert len(_dumps(tmp_path)) == 3


def test_never_raises_on_unserialisable_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", "1")
    # default=str must keep a non-JSON value from blowing up a live request.
    dump_upstream_request({"model": "gpt-4", "weird": object()},
                          route="openai", phase="first")
    assert len(_dumps(tmp_path)) == 1


# ---------------------------------------------------------------------------
# route wiring — the dump fires right before each upstream call
# ---------------------------------------------------------------------------

class _FakeOpenAIUpstream:
    def __init__(self, script):
        self.script = list(script)
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")

    async def complete(self, request):
        return self.script.pop(0)

    async def close(self):
        return None


class _FakeAnthropicUpstream:
    def __init__(self, script):
        self.script = list(script)
        self.spec = UpstreamSpec(name="fake-a", kind="anthropic", base_url="http://fake")

    async def complete(self, request):
        return self.script.pop(0)

    async def close(self):
        return None


def _core(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", "1")
    registry = HandleRegistry()
    return dict(
        registry=registry,
        tracker=SavingsTracker(path=tmp_path / "_savings.json"),
        dispatcher=Dispatcher(registry, SessionStats()),
        metrics=MetricsDB(),
    )


def test_openai_route_dumps_first_and_verb_loop(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    # turn 1: model calls a jmunch verb → triggers the verb loop; turn 2: plain.
    turn1 = {"id": "c1", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "jmunch_list_handles", "arguments": "{}"}}]}}]}
    turn2 = {"id": "c2", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "done"}}]}
    fake = _FakeOpenAIUpstream([turn1, turn2])
    req = {
        "model": "gpt-4",
        "tools": [{"type": "function",
                   "function": {"name": "app_tool", "description": "d", "parameters": {}}}],
        "messages": [{"role": "user", "content": "go"}],
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

    dumps = _dumps(tmp_path)
    names = [d.name for d in dumps]
    assert any(n.endswith("_openai_first.json") for n in names)
    assert any(n.endswith("_openai_verb-loop.json") for n in names)
    # The "first" dump is the real forwarded body. No handle envelope is
    # present on this turn, so envelope-aware `auto` does not inject the
    # jmunch verbs — only the app's own tool is forwarded.
    first = json.loads(next(d for d in dumps if d.name.endswith("_openai_first.json")).read_text())
    assert first["model"] == "gpt-4"
    tool_names = [t["function"]["name"] for t in first["tools"]]
    assert "app_tool" in tool_names


def test_anthropic_route_dumps_first_turn(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    final = {"id": "m1", "type": "message", "role": "assistant",
             "content": [{"type": "text", "text": "hi"}],
             "stop_reason": "end_turn", "model": "claude-opus"}
    fake = _FakeAnthropicUpstream([final])
    req = {"model": "claude-opus", "max_tokens": 256,
           "messages": [{"role": "user", "content": "hi"}]}
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

    dumps = _dumps(tmp_path)
    assert len(dumps) == 1
    assert dumps[0].name.endswith("_anthropic_first.json")
    assert json.loads(dumps[0].read_text())["model"] == "claude-opus"


def test_route_does_not_dump_when_disabled(tmp_path, monkeypatch):
    core = _core(tmp_path, monkeypatch)
    monkeypatch.setenv("JMUNCH_DEBUG_DUMP", "0")  # override the _core default
    final = {"id": "m1", "type": "message", "role": "assistant",
             "content": [{"type": "text", "text": "hi"}],
             "stop_reason": "end_turn", "model": "claude-opus"}
    fake = _FakeAnthropicUpstream([final])
    config = GatewayConfig(
        default_upstream="fake-a",
        upstreams=[UpstreamSpec(name="fake-a", kind="anthropic", base_url="http://fake")],
        interception=Interception(),
    )
    asyncio.run(handle_messages(
        {"model": "claude-opus", "max_tokens": 256,
         "messages": [{"role": "user", "content": "hi"}]},
        upstream_override=None, config=config,
        upstream_factory=lambda spec: fake, **core,
    ))
    assert not (tmp_path / ".jmunch" / "debug").exists()
