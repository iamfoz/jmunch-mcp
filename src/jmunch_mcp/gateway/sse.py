"""SSE parsing + encoding helpers for OpenAI-style streaming.

OpenAI's `/v1/chat/completions` with `stream=true` emits lines of the form:
    data: {"id":"...","choices":[{"delta":{"content":"..."},"index":0}]}\n\n
    ...
    data: [DONE]\n\n

Each `data:` line is a `ChatCompletionChunk`. We parse them, accumulate
deltas across chunks into a full non-streaming response shape, then —
after the jmunch verb-resolution loop — re-encode the final response as
SSE so the client can consume it as streaming.

Buffer-then-replay trades first-token-latency for correctness. The plan
accepts this for Phase 2; streaming through live until a tool_call is
detected is a future optimization.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable


async def parse_sse_stream(chunks: AsyncIterator[bytes]) -> list[dict[str, Any]]:
    """Consume an SSE byte stream, return the list of decoded `data:` events
    (minus the terminal `[DONE]`). Lines without `data:` prefix are ignored."""
    events: list[dict[str, Any]] = []
    buffer = b""
    async for piece in chunks:
        buffer += piece
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                continue
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].lstrip()
            if payload == b"[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
    # Flush any trailing line without a newline terminator.
    tail = buffer.strip()
    if tail.startswith(b"data:"):
        payload = tail[5:].lstrip()
        if payload != b"[DONE]":
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def assemble_response_from_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold streaming ChatCompletionChunks into a single ChatCompletion-shaped
    response. We reconstruct `choices[i].message` from the `delta` updates.

    Enough fidelity for the verb-short-circuit decision + for re-emitting
    back as SSE. Chunk boundaries are lost, but `usage` is preserved from
    whichever chunk carries it (per OpenAI's spec, that's a trailing chunk
    with `choices: []`; some upstreams attach it inline on the final
    content chunk instead) so downstream clients can do token accounting.
    """
    if not chunks:
        return {"choices": [], "id": "", "object": "chat.completion"}

    # Take id/model/object from the first chunk; finish_reason from the last
    # chunk that sets it.
    base = chunks[0]
    out: dict[str, Any] = {
        "id": base.get("id", ""),
        "object": "chat.completion",
        "created": base.get("created"),
        "model": base.get("model"),
        "choices": [],
    }

    # Preserve usage from any chunk that carries it. Last write wins so
    # the trailing usage frame (the authoritative one) overrides any
    # interim values some upstreams emit.
    for chunk in chunks:
        u = chunk.get("usage")
        if u:
            out["usage"] = u

    # Fold deltas per choice index.
    choices_by_idx: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for ch in chunk.get("choices") or []:
            idx = ch.get("index", 0)
            slot = choices_by_idx.setdefault(idx, {
                "index": idx,
                "message": {"role": "assistant", "content": None},
                "finish_reason": None,
            })
            if ch.get("finish_reason"):
                slot["finish_reason"] = ch["finish_reason"]
            delta = ch.get("delta") or {}
            msg = slot["message"]
            if "role" in delta:
                msg["role"] = delta["role"]
            if "content" in delta and delta["content"] is not None:
                msg["content"] = (msg.get("content") or "") + delta["content"]
            if "tool_calls" in delta and delta["tool_calls"]:
                _merge_tool_call_deltas(msg, delta["tool_calls"])

    out["choices"] = [choices_by_idx[i] for i in sorted(choices_by_idx)]
    return out


def _merge_tool_call_deltas(msg: dict[str, Any], deltas: list[dict[str, Any]]) -> None:
    tool_calls = msg.setdefault("tool_calls", [])
    for d in deltas:
        idx = d.get("index", 0)
        while len(tool_calls) <= idx:
            tool_calls.append({
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        slot = tool_calls[idx]
        if d.get("id"):
            slot["id"] = d["id"]
        if d.get("type"):
            slot["type"] = d["type"]
        fn_delta = d.get("function") or {}
        if fn_delta.get("name"):
            slot["function"]["name"] = (
                slot["function"].get("name", "") + fn_delta["name"]
            )
        if fn_delta.get("arguments"):
            slot["function"]["arguments"] = (
                slot["function"].get("arguments", "") + fn_delta["arguments"]
            )


def encode_as_sse(response: dict[str, Any]) -> list[bytes]:
    """Encode a non-streaming response as a minimal SSE sequence:
    one data-chunk with the full message as a delta, then [DONE].

    Clients that properly aggregate chunks (LangChain, OpenAI SDK) handle
    this fine. Progressive rendering is lost — a known tradeoff of
    buffer-then-replay.
    """
    chunk_id = response.get("id") or "jmunch-gw"
    model = response.get("model", "")
    choices: list[dict[str, Any]] = []
    for ch in response.get("choices") or []:
        msg = ch.get("message") or {}
        delta: dict[str, Any] = {"role": msg.get("role", "assistant")}
        if msg.get("content") is not None:
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = msg["tool_calls"]
        choices.append({
            "index": ch.get("index", 0),
            "delta": delta,
            "finish_reason": ch.get("finish_reason"),
        })
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": response.get("created") or 0,
        "model": model,
        "choices": choices,
    }
    lines = [b"data: " + json.dumps(chunk, default=str).encode("utf-8") + b"\n\n"]

    # When the upstream reported usage, replay it as a separate trailing
    # chunk with `choices: []` — that's OpenAI's spec shape for the
    # final usage frame when `stream_options.include_usage=True`.
    # Clients that didn't request usage will simply see an extra empty
    # chunk; the OpenAI/LangChain SDKs ignore it gracefully.
    usage = response.get("usage")
    if usage:
        usage_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": response.get("created") or 0,
            "model": model,
            "choices": [],
            "usage": usage,
        }
        lines.append(
            b"data: " + json.dumps(usage_chunk, default=str).encode("utf-8") + b"\n\n"
        )

    lines.append(b"data: [DONE]\n\n")
    return lines


def iter_as_sse(response: dict[str, Any]) -> Iterable[bytes]:
    return iter(encode_as_sse(response))
