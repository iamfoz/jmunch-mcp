"""Anthropic prompt-cache markers: when `cache_optimize` is on, the
gateway attaches `cache_control: {type: "ephemeral"}` to the stable
prefix (system + tools tail) so successive turns hit the provider's
prompt cache."""
from __future__ import annotations

from jmunch_mcp.gateway.anthropic_route import attach_cache_control


def test_string_system_becomes_marked_text_block():
    out = attach_cache_control({"system": "You are helpful."})
    assert out["system"] == [{
        "type": "text", "text": "You are helpful.",
        "cache_control": {"type": "ephemeral"},
    }]


def test_list_system_marks_only_the_last_block():
    sys = [{"type": "text", "text": "preamble"},
           {"type": "text", "text": "rules"}]
    out = attach_cache_control({"system": sys})
    assert out["system"][0] == {"type": "text", "text": "preamble"}        # untouched
    assert out["system"][1]["cache_control"] == {"type": "ephemeral"}      # marked
    # the original list and dicts must not have been mutated
    assert sys[0] == {"type": "text", "text": "preamble"}
    assert "cache_control" not in sys[1]


def test_tools_array_marks_only_the_last_tool():
    tools = [
        {"name": "t1", "description": "x", "input_schema": {}},
        {"name": "t2", "description": "y", "input_schema": {}},
        {"name": "t3", "description": "z", "input_schema": {}},
    ]
    out = attach_cache_control({"tools": tools})
    assert "cache_control" not in out["tools"][0]
    assert "cache_control" not in out["tools"][1]
    assert out["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # original untouched
    assert "cache_control" not in tools[-1]


def test_no_system_or_tools_returns_a_clean_copy():
    req = {"model": "claude-opus", "messages": [{"role": "user", "content": "hi"}]}
    out = attach_cache_control(req)
    assert out == req
    assert out is not req     # new dict


def test_empty_system_string_is_skipped():
    out = attach_cache_control({"system": ""})
    # empty string → leave alone; don't wrap into a list block of nothing
    assert out["system"] == ""


def test_empty_tools_list_is_skipped():
    out = attach_cache_control({"tools": []})
    assert out["tools"] == []


def test_attach_cache_control_does_not_touch_messages():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = attach_cache_control({"system": "s", "messages": msgs})
    assert out["messages"] is msgs   # messages are not the stable prefix; left alone


def test_disabled_path_via_handle_messages(tmp_path, monkeypatch):
    """End-to-end: cache_optimize=False → no cache_control markers go
    to the upstream (regression — make sure the flag is honoured)."""
    import asyncio
    import copy as _copy
    from jmunch_mcp.gateway.anthropic_route import handle_messages
    from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
    from jmunch_mcp.meta import SavingsTracker
    from jmunch_mcp.metrics import MetricsDB
    from jmunch_mcp.registry import HandleRegistry
    from jmunch_mcp.stats import SessionStats
    from jmunch_mcp.verbs import Dispatcher

    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))

    class _Fake:
        def __init__(self):
            self.calls = []
            self.spec = UpstreamSpec(name="fake", kind="anthropic", base_url="http://fake")
        async def complete(self, req):
            self.calls.append(_copy.deepcopy(req))
            return {"id": "m1", "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "model": "claude-opus"}
        async def close(self): pass

    registry = HandleRegistry()
    fake = _Fake()
    req = {
        "model": "claude-opus", "max_tokens": 16,
        "system": "you are helpful",
        "messages": [{"role": "user", "content": "hi"}],
    }
    cfg = GatewayConfig(
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="anthropic", base_url="http://fake")],
        interception=Interception(cache_optimize=False),
    )
    asyncio.run(handle_messages(
        req, upstream_override=None, config=cfg,
        upstream_factory=lambda spec: fake,
        registry=registry,
        tracker=SavingsTracker(path=tmp_path / "_savings.json"),
        dispatcher=Dispatcher(registry, SessionStats()),
        metrics=MetricsDB(),
    ))
    sent = fake.calls[0]
    # With the flag off the system field stays a plain string — no rewrap.
    assert sent["system"] == "you are helpful"


def test_enabled_path_via_handle_messages(tmp_path, monkeypatch):
    """End-to-end: cache_optimize=True → the system field arrives at the
    upstream as a content-block list with a cache_control marker."""
    import asyncio
    import copy as _copy
    from jmunch_mcp.gateway.anthropic_route import handle_messages
    from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
    from jmunch_mcp.meta import SavingsTracker
    from jmunch_mcp.metrics import MetricsDB
    from jmunch_mcp.registry import HandleRegistry
    from jmunch_mcp.stats import SessionStats
    from jmunch_mcp.verbs import Dispatcher

    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))

    class _Fake:
        def __init__(self):
            self.calls = []
            self.spec = UpstreamSpec(name="fake", kind="anthropic", base_url="http://fake")
        async def complete(self, req):
            self.calls.append(_copy.deepcopy(req))
            return {"id": "m1", "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "model": "claude-opus"}
        async def close(self): pass

    registry = HandleRegistry()
    fake = _Fake()
    req = {
        "model": "claude-opus", "max_tokens": 16,
        "system": "you are helpful",
        "messages": [{"role": "user", "content": "hi"}],
    }
    cfg = GatewayConfig(
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="anthropic", base_url="http://fake")],
        interception=Interception(cache_optimize=True),
    )
    asyncio.run(handle_messages(
        req, upstream_override=None, config=cfg,
        upstream_factory=lambda spec: fake,
        registry=registry,
        tracker=SavingsTracker(path=tmp_path / "_savings.json"),
        dispatcher=Dispatcher(registry, SessionStats()),
        metrics=MetricsDB(),
    ))
    sent = fake.calls[0]
    assert isinstance(sent["system"], list)
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}
