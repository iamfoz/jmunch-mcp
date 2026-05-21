"""Context-aware handle-ification.

Covers the context-window table, the request-size fraction gate, the
recency window, the `X-Jmunch-Handleify` master switch, the
`X-Jmunch-Gateway` response-header helper, and config loading/validation
of the new `[interception]` keys.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jmunch_mcp import __version__
from jmunch_mcp.gateway.anthropic_route import (
    _handleify_request_messages as anthropic_handleify,
)
from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.config import load as load_gateway
from jmunch_mcp.gateway.context_window import DEFAULT_WINDOW, window_for
from jmunch_mcp.gateway.handleify import recency_protected_count, request_is_eligible
from jmunch_mcp.gateway.openai_route import handle_chat_completions
from jmunch_mcp.gateway.openai_route import (
    _handleify_request_messages as openai_handleify,
)
from jmunch_mcp.gateway.server import (
    _config_for_request,
    _gw_headers,
    _header_is_false,
)
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fat(n: int = 300) -> str:
    """~34 KB of tabular JSON — comfortably over any test threshold."""
    return json.dumps(
        [{"id": i, "name": f"row-{i}", "desc": "x" * 80} for i in range(n)]
    )


def _is_handle_envelope(text: str) -> bool:
    try:
        env = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(env, dict)
        and isinstance(env.get("result"), dict)
        and isinstance(env["result"].get("handle"), str)
    )


def _openai_req(model: str, tool_contents: list[str]) -> dict[str, Any]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for i, content in enumerate(tool_contents):
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": content})
    return {"model": model, "messages": msgs}


def _tracker(tmp_path):
    return SavingsTracker(path=tmp_path / "_savings.json")


# ---------------------------------------------------------------------------
# context_window.window_for
# ---------------------------------------------------------------------------

def test_window_for_builtin_prefixes():
    assert window_for("gpt-4o-mini") == 128_000
    assert window_for("gpt-4-0613") == 8_192
    assert window_for("claude-opus-4-20250101") == 200_000
    assert window_for("qwen3.6-262k") == 262_144
    assert window_for("Qwen3-Coder") == 262_144  # case-insensitive


def test_window_for_longest_prefix_wins():
    # "gpt-4o" (6 chars) beats "gpt-4" (5) for a gpt-4o model.
    assert window_for("gpt-4o") == 128_000
    # "gpt-4-turbo" (11) beats "gpt-4" (5).
    assert window_for("gpt-4-turbo-preview") == 128_000


def test_window_for_unknown_falls_back():
    assert window_for("totally-unknown-model") == DEFAULT_WINDOW
    assert window_for("totally-unknown-model", default_window=999) == 999
    assert window_for(None) == DEFAULT_WINDOW
    assert window_for("") == DEFAULT_WINDOW


def test_window_for_overrides_win():
    overrides = {"qwen3.6-custom": 262_144, "my-finetune": 50_000}
    # prefix match on an override
    assert window_for("my-finetune-v2", overrides=overrides) == 50_000
    # an override beats the built-in table
    assert window_for("gpt-4", overrides={"gpt-4": 4242}) == 4242


# ---------------------------------------------------------------------------
# request_is_eligible / recency_protected_count
# ---------------------------------------------------------------------------

def test_request_eligible_gate_disabled():
    inter = Interception(context_fraction=0.0)
    # Gate off → always eligible, regardless of size.
    assert request_is_eligible(10, "gpt-4", interception=inter) is True
    assert request_is_eligible(10_000_000, "gpt-4", interception=inter) is True


def test_request_eligible_gate_on():
    inter = Interception(context_fraction=0.5)  # gpt-4 window = 8192 tokens
    # 0.5 * 8192 = 4096 tokens → 16384 bytes at the bytes/4 heuristic.
    assert request_is_eligible(16_384, "gpt-4", interception=inter) is True
    assert request_is_eligible(16_000, "gpt-4", interception=inter) is False


def test_request_eligible_big_window_skips():
    inter = Interception(context_fraction=0.5)
    # A 100 KB request on a 262k-token model is nowhere near half the window.
    assert request_is_eligible(100_000, "qwen3-max", interception=inter) is False


def test_recency_protected_count():
    assert recency_protected_count(10, 4) == 4
    assert recency_protected_count(2, 4) == 2    # clamped to total
    assert recency_protected_count(5, 0) == 0    # window disabled
    assert recency_protected_count(0, 4) == 0    # nothing to protect


# ---------------------------------------------------------------------------
# _handleify_request_messages — OpenAI shape
# ---------------------------------------------------------------------------

def test_openai_gate_skips_request_under_fraction(tmp_path):
    registry = HandleRegistry()
    inter = Interception(threshold_tokens=100, context_fraction=0.5)
    req = _openai_req("qwen3-max", [_fat()])  # ~34 KB, 262k-token window
    out, saved, pairs = openai_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    assert out is req and saved == 0 and pairs == []
    assert len(registry) == 0


def test_openai_gate_fires_request_over_fraction(tmp_path):
    registry = HandleRegistry()
    inter = Interception(threshold_tokens=100, context_fraction=0.5)
    req = _openai_req("gpt-4", [_fat()])  # ~34 KB on an 8192-token window
    out, saved, pairs = openai_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    assert saved > 0 and len(pairs) == 1
    assert len(registry) == 1
    tool_msg = next(m for m in out["messages"] if m["role"] == "tool")
    assert _is_handle_envelope(tool_msg["content"])


def test_openai_recency_window_protects_last_n(tmp_path):
    registry = HandleRegistry()
    inter = Interception(
        threshold_tokens=100, context_fraction=0.0, recency_window=2,
    )
    req = _openai_req("gpt-4", [_fat(), _fat(), _fat()])
    out, saved, pairs = openai_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    # Only the oldest tool message is compressed; the last two are verbatim.
    assert len(pairs) == 1
    assert len(registry) == 1
    tool_msgs = [m for m in out["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    assert _is_handle_envelope(tool_msgs[0]["content"])
    assert not _is_handle_envelope(tool_msgs[1]["content"])
    assert not _is_handle_envelope(tool_msgs[2]["content"])


def test_openai_handleify_master_switch_off(tmp_path):
    registry = HandleRegistry()
    inter = Interception(threshold_tokens=100, handleify_enabled=False)
    req = _openai_req("gpt-4", [_fat()])
    out, saved, pairs = openai_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    assert out is req and saved == 0 and pairs == []
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# _handleify_request_messages — Anthropic shape
# ---------------------------------------------------------------------------

def _anthropic_req(model: str, tool_contents: list[str]) -> dict[str, Any]:
    blocks = [
        {"type": "tool_result", "tool_use_id": f"tu_{i}", "content": c}
        for i, c in enumerate(tool_contents)
    ]
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "user", "content": blocks},
        ],
    }


def test_anthropic_recency_window_protects_last_block(tmp_path):
    registry = HandleRegistry()
    inter = Interception(
        threshold_tokens=100, context_fraction=0.0, recency_window=1,
    )
    req = _anthropic_req("claude-opus", [_fat(), _fat(), _fat()])
    out, saved, pairs = anthropic_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    # First two tool_result blocks compressed; the most recent left verbatim.
    assert len(pairs) == 2
    assert len(registry) == 2
    blocks = out["messages"][1]["content"]
    assert _is_handle_envelope(blocks[0]["content"])
    assert _is_handle_envelope(blocks[1]["content"])
    assert not _is_handle_envelope(blocks[2]["content"])


def test_anthropic_gate_skips_big_window(tmp_path):
    registry = HandleRegistry()
    inter = Interception(threshold_tokens=100, context_fraction=0.5)
    req = _anthropic_req("claude-opus", [_fat()])  # 200k-token window
    out, saved, pairs = anthropic_handleify(
        req, registry=registry, tracker=_tracker(tmp_path),
        interception=inter, request_bytes=len(json.dumps(req)),
    )
    assert out is req and saved == 0 and pairs == []
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# end-to-end through the OpenAI route — proves the wiring
# ---------------------------------------------------------------------------

class _FakeUpstream:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")

    async def complete(self, request):
        self.calls.append(request)
        return self.response

    async def close(self):
        return None


def _e2e_config(context_fraction: float) -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(
            threshold_tokens=100, inject_tools="auto",
            context_fraction=context_fraction,
        ),
    )


def test_route_skips_handleify_on_big_context_model(tmp_path, monkeypatch):
    """End-to-end: with the gate on, a request that fits a big-context
    model is forwarded with raw tool_result content — no envelope."""
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    registry = HandleRegistry()
    dispatcher = Dispatcher(registry, SessionStats())
    fake = _FakeUpstream({
        "id": "c1",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "done"}}],
    })
    req = _openai_req("qwen3-max", [_fat()])
    req["tools"] = [{"type": "function", "function": {
        "name": "t", "description": "x", "parameters": {"type": "object"}}}]

    status, resp = asyncio.run(handle_chat_completions(
        req, upstream_override=None, config=_e2e_config(context_fraction=0.5),
        upstream_factory=lambda spec: fake,
        registry=registry, tracker=_tracker(tmp_path),
        dispatcher=dispatcher, metrics=MetricsDB(),
    ))
    assert status == 200
    forwarded_tool = next(
        m for m in fake.calls[0]["messages"] if m.get("role") == "tool"
    )
    assert not _is_handle_envelope(forwarded_tool["content"])
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# server-side per-request header helpers
# ---------------------------------------------------------------------------

def test_gw_headers_carry_version():
    headers = _gw_headers()
    assert headers["X-Jmunch-Gateway"] == __version__
    merged = _gw_headers({"Content-Type": "text/event-stream"})
    assert merged["X-Jmunch-Gateway"] == __version__
    assert merged["Content-Type"] == "text/event-stream"


def test_header_is_false_recognises_falsey_values():
    for value in ("false", "FALSE", "0", "no", "off", " false "):
        assert _header_is_false(value) is True
    for value in ("true", "1", "yes", "", None):
        assert _header_is_false(value) is False


def test_config_for_request_handleify_header():
    base = GatewayConfig(
        upstreams=[UpstreamSpec(name="o", kind="openai", base_url="http://x")]
    )
    assert base.interception.handleify_enabled is True

    # No headers → original config object returned untouched.
    assert _config_for_request(base, {}) is base

    # X-Jmunch-Handleify: false → request-side handle-ification disabled.
    disabled = _config_for_request(base, {"X-Jmunch-Handleify": "false"})
    assert disabled.interception.handleify_enabled is False
    assert base.interception.handleify_enabled is True  # original untouched

    # X-Jmunch-Inject: false → verb injection disabled, handleify untouched.
    no_inject = _config_for_request(base, {"X-Jmunch-Inject": "false"})
    assert no_inject.interception.inject_tools == "never"
    assert no_inject.interception.handleify_enabled is True

    # Both headers compose.
    both = _config_for_request(
        base, {"X-Jmunch-Inject": "false", "X-Jmunch-Handleify": "0"}
    )
    assert both.interception.inject_tools == "never"
    assert both.interception.handleify_enabled is False


# ---------------------------------------------------------------------------
# config loading + validation of the new keys
# ---------------------------------------------------------------------------

_BASE_TOML = """
[gateway]
default_upstream = "o"

[[upstream]]
name = "o"
kind = "openai"
base_url = "http://x"
"""


def _write_cfg(tmp_path, extra: str):
    p = tmp_path / "gateway.toml"
    p.write_text(_BASE_TOML + extra, encoding="utf-8")
    return p


def test_config_loads_new_interception_keys(tmp_path):
    cfg = _write_cfg(tmp_path, """
[interception]
threshold_tokens = 500
context_fraction = 0.6
recency_window = 3
default_context_window = 200000
handleify = false

[interception.context_windows]
"qwen3.6" = 262144
""")
    g = load_gateway(cfg)
    assert g.interception.context_fraction == 0.6
    assert g.interception.recency_window == 3
    assert g.interception.default_context_window == 200_000
    assert g.interception.handleify_enabled is False
    assert g.interception.context_windows == {"qwen3.6": 262144}


def test_config_defaults_preserve_legacy_behaviour(tmp_path):
    g = load_gateway(_write_cfg(tmp_path, ""))
    assert g.interception.context_fraction == 0.0
    assert g.interception.recency_window == 0
    assert g.interception.default_context_window == 128_000
    assert g.interception.handleify_enabled is True
    assert g.interception.context_windows == {}


def test_config_rejects_bad_context_fraction(tmp_path):
    cfg = _write_cfg(tmp_path, "[interception]\ncontext_fraction = 1.5\n")
    with pytest.raises(ValueError, match="context_fraction"):
        load_gateway(cfg)


def test_config_rejects_negative_recency_window(tmp_path):
    cfg = _write_cfg(tmp_path, "[interception]\nrecency_window = -1\n")
    with pytest.raises(ValueError, match="recency_window"):
        load_gateway(cfg)
