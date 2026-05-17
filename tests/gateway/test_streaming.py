"""Streaming SSE reassembly + verb short-circuit under streaming."""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.handleify import maybe_handleify
from jmunch_mcp.gateway.openai_route import stream_chat_completions
from jmunch_mcp.gateway.sse import (
    assemble_response_from_chunks,
    encode_as_sse,
    parse_sse_stream,
)
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


def _encode_chunk_bytes(chunk: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"


def _done_bytes() -> bytes:
    return b"data: [DONE]\n\n"


class FakeStreamingUpstream:
    """Serves a scripted SSE stream for stream() and scripted JSON for complete()."""

    def __init__(self, *, sse_script: list[list[bytes]], complete_script: list[dict[str, Any]]):
        self.sse_script = list(sse_script)
        self.complete_script = list(complete_script)
        self.stream_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")

    async def stream(self, request) -> AsyncIterator[bytes]:
        self.stream_calls.append(request)
        if not self.sse_script:
            raise AssertionError("stream() called more times than scripted")
        pieces = self.sse_script.pop(0)
        for p in pieces:
            yield p

    async def complete(self, request):
        self.complete_calls.append(request)
        if not self.complete_script:
            raise AssertionError("complete() called more times than scripted")
        return self.complete_script.pop(0)

    async def close(self):
        return None


def _config():
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )


def _make_tmp_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    return MetricsDB()


def _make_tracker(tmp_path):
    return SavingsTracker(path=tmp_path / "_savings.json")


# ---------------------------------------------------------------------------
# Pure SSE parsing unit tests
# ---------------------------------------------------------------------------

def test_sse_parse_basic():
    async def src():
        yield b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    events = asyncio.run(parse_sse_stream(src()))
    assert len(events) == 1
    assert events[0]["choices"][0]["delta"]["content"] == "hi"


def test_sse_parse_handles_chunk_splits():
    """Chunk boundaries that split in the middle of a line / JSON still work."""
    async def src():
        yield b'data: {"id":"1","choices":'
        yield b'[{"index":0,"delta":{"content":"hi"}}'
        yield b"]}\n\ndata: [DONE]\n\n"

    events = asyncio.run(parse_sse_stream(src()))
    assert len(events) == 1
    assert events[0]["choices"][0]["delta"]["content"] == "hi"


def test_assemble_folds_content_deltas():
    chunks = [
        {"id": "1", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
        {"id": "1", "model": "m", "choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {"id": "1", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    out = assemble_response_from_chunks(chunks)
    assert out["choices"][0]["message"]["content"] == "Hello"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_assemble_folds_tool_call_deltas():
    chunks = [
        {"id": "1", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "tc_1", "type": "function",
             "function": {"name": "jmunch_peek", "arguments": ""}}
        ]}}]},
        {"id": "1", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"handle":'}}
        ]}}]},
        {"id": "1", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"h_x","n":3}'}}
        ]}}]},
        {"id": "1", "choices": [{"index": 0, "finish_reason": "tool_calls"}]},
    ]
    out = assemble_response_from_chunks(chunks)
    tc = out["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "jmunch_peek"
    assert json.loads(tc["function"]["arguments"]) == {"handle": "h_x", "n": 3}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_encode_as_sse_roundtrip():
    resp = {
        "id": "c1", "model": "m", "created": 123,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
    }
    chunks = encode_as_sse(resp)
    assert chunks[-1] == b"data: [DONE]\n\n"
    # Parse our own output and confirm the content round-trips.
    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_sse_stream(src()))
    recovered = assemble_response_from_chunks(events)
    assert recovered["choices"][0]["message"]["content"] == "ok"


def test_assemble_preserves_trailing_usage_chunk():
    """OpenAI spec: when stream_options.include_usage=True the upstream
    emits a final chunk with choices=[] and a populated usage block."""
    chunks = [
        {"id": "1", "model": "m", "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ]},
        {"id": "1", "model": "m", "choices": [], "usage": {
            "prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49,
        }},
    ]
    out = assemble_response_from_chunks(chunks)
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["usage"] == {
        "prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49,
    }


def test_assemble_preserves_inline_usage():
    """Some upstreams (non-spec-compliant but common) attach usage to the
    final content-bearing chunk instead of a separate trailing chunk."""
    chunks = [
        {"id": "1", "model": "m", "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ], "usage": {
            "prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13,
        }},
    ]
    out = assemble_response_from_chunks(chunks)
    assert out["usage"]["total_tokens"] == 13


def test_assemble_usage_last_write_wins():
    """If multiple chunks carry usage, the latest one wins (the trailing
    spec-compliant frame should always be authoritative over any interim
    estimates a proxy might inject)."""
    chunks = [
        {"id": "1", "model": "m", "choices": [
            {"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}
        ], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        {"id": "1", "model": "m", "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        }},
    ]
    out = assemble_response_from_chunks(chunks)
    assert out["usage"]["total_tokens"] == 150


def test_assemble_no_usage_when_absent():
    """When no chunk carries usage, the assembled response must not
    invent one — downstream clients rely on absence as a signal."""
    chunks = [
        {"id": "1", "model": "m", "choices": [
            {"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}
        ]},
    ]
    out = assemble_response_from_chunks(chunks)
    assert "usage" not in out


def test_encode_emits_trailing_usage_chunk():
    """A response with usage must round-trip through encode→parse→assemble
    with its usage intact, matching the OpenAI streaming spec shape
    (separate trailing chunk with empty choices)."""
    resp = {
        "id": "c1", "model": "m", "created": 123,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    chunks = encode_as_sse(resp)
    assert chunks[-1] == b"data: [DONE]\n\n"
    # Three frames now: content chunk, usage chunk, [DONE].
    assert len(chunks) == 3
    # The usage chunk must follow the OpenAI spec shape: empty choices,
    # populated usage.
    usage_payload = json.loads(chunks[1].removeprefix(b"data: ").rstrip(b"\n\n"))
    assert usage_payload["choices"] == []
    assert usage_payload["usage"]["total_tokens"] == 6

    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_sse_stream(src()))
    recovered = assemble_response_from_chunks(events)
    assert recovered["choices"][0]["message"]["content"] == "ok"
    assert recovered["usage"]["total_tokens"] == 6


def test_encode_omits_usage_chunk_when_absent():
    """No usage in → no extra chunk out. Keeps the wire skinny for
    clients that didn't enable stream_options.include_usage."""
    resp = {
        "id": "c1", "model": "m", "created": 123,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
    }
    chunks = encode_as_sse(resp)
    assert len(chunks) == 2  # content + [DONE], no usage frame
    assert chunks[-1] == b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Streaming end-to-end: verb short-circuit under stream=true
# ---------------------------------------------------------------------------

def _fat_text() -> str:
    rows = [{"id": i, "name": f"r-{i}", "desc": "y" * 80} for i in range(200)]
    return json.dumps(rows)


def test_stream_verb_short_circuit(tmp_path, monkeypatch):
    """Stream from upstream contains a jmunch_peek tool_call across multiple
    chunks. The gateway reassembles, resolves locally, re-queries upstream
    (non-streaming for the follow-up), and re-emits the final assistant
    message as SSE. The client sees clean SSE with no jmunch tool_call."""
    registry = HandleRegistry()
    tracker = _make_tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_tmp_metrics(tmp_path, monkeypatch)

    # Prime a handle so the eventual jmunch_peek succeeds.
    env_text, handle_id = maybe_handleify(
        _fat_text(), registry=registry, tracker=tracker, threshold_tokens=100,
    )
    assert env_text and handle_id

    # First turn: streamed SSE, split across chunk boundaries.
    args_json = json.dumps({"handle": handle_id, "n": 2})
    sse_turn1 = [
        _encode_chunk_bytes({"id": "c1", "model": "gpt-4", "choices": [{"index": 0, "delta": {
            "role": "assistant",
            "tool_calls": [{"index": 0, "id": "tc_1", "type": "function",
                            "function": {"name": "jmunch_peek", "arguments": ""}}],
        }}]}),
        _encode_chunk_bytes({"id": "c1", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"arguments": args_json[:15]}}]
        }}]}),
        _encode_chunk_bytes({"id": "c1", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"arguments": args_json[15:]}}]
        }}]}),
        _encode_chunk_bytes({"id": "c1", "choices": [{"index": 0, "delta": {},
                                                      "finish_reason": "tool_calls"}]}),
        _done_bytes(),
    ]
    # After the gateway resolves jmunch_peek locally and posts the follow-up,
    # the upstream returns a normal assistant reply (non-streaming).
    complete_turn2 = {
        "id": "c2", "model": "gpt-4",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": "Peeked 2 rows."}}],
    }

    fake = FakeStreamingUpstream(
        sse_script=[sse_turn1],
        complete_script=[complete_turn2],
    )

    req = {
        "model": "gpt-4",
        "stream": True,
        "tools": [{"type": "function", "function": {
            "name": "noop", "description": "x", "parameters": {"type": "object"}}}],
        "messages": [{"role": "user", "content": "peek it"}],
    }

    status, chunks = asyncio.run(stream_chat_completions(
        req,
        upstream_override=None,
        config=_config(),
        upstream_factory=lambda spec: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))

    assert status == 200
    assert len(fake.stream_calls) == 1, "first turn must come from upstream streaming"
    assert len(fake.complete_calls) == 1, "follow-up turn uses non-streaming complete()"

    # The client SSE output should contain the final assistant message — no jmunch_peek.
    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_sse_stream(src()))
    final = assemble_response_from_chunks(events)
    assert final["choices"][0]["message"]["content"] == "Peeked 2 rows."
    assert "tool_calls" not in final["choices"][0]["message"]

    # Verify the follow-up request carried the synthesized tool_result.
    followup = fake.complete_calls[0]
    tool_results = [m for m in followup["messages"]
                    if m.get("role") == "tool" and m.get("tool_call_id") == "tc_1"]
    assert len(tool_results) == 1
    env = json.loads(tool_results[0]["content"])
    assert "_meta" in env
    assert isinstance(env["result"], list)
    assert len(env["result"]) == 2


def test_stream_no_tool_calls_passes_through(tmp_path, monkeypatch):
    """Streaming with no jmunch tool_calls in the first turn: gateway
    reassembles, verb loop is a no-op, and the final text is re-emitted."""
    registry = HandleRegistry()
    tracker = _make_tracker(tmp_path)
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = _make_tmp_metrics(tmp_path, monkeypatch)

    sse_turn1 = [
        _encode_chunk_bytes({"id": "c1", "model": "gpt-4", "choices": [{"index": 0, "delta": {
            "role": "assistant", "content": "Hello "}}]}),
        _encode_chunk_bytes({"id": "c1", "choices": [{"index": 0, "delta": {
            "content": "world"}}]}),
        _encode_chunk_bytes({"id": "c1", "choices": [{"index": 0, "delta": {},
                                                      "finish_reason": "stop"}]}),
        _done_bytes(),
    ]
    fake = FakeStreamingUpstream(sse_script=[sse_turn1], complete_script=[])
    req = {"model": "gpt-4", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    status, chunks = asyncio.run(stream_chat_completions(
        req, upstream_override=None, config=_config(),
        upstream_factory=lambda spec: fake,
        registry=registry, tracker=tracker,
        dispatcher=dispatcher, metrics=metrics,
    ))
    assert status == 200
    assert len(fake.stream_calls) == 1
    assert len(fake.complete_calls) == 0
    async def src():
        for c in chunks:
            yield c
    events = asyncio.run(parse_sse_stream(src()))
    final = assemble_response_from_chunks(events)
    assert final["choices"][0]["message"]["content"] == "Hello world"
