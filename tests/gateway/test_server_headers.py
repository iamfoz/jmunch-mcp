"""Server-layer tests: the `X-Jmunch-Gateway` response header (CHANGE 2) and
the `X-Jmunch-Handleify` per-request override (CHANGE 3).

Drives the real aiohttp app with a fake upstream — no network.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jmunch_mcp import __version__
from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.server import GatewayApp, build_aiohttp_app


class _FakeUpstream:
    """Serves a plain (no tool-call) reply over both complete() and stream()."""

    def __init__(self):
        self.calls: list[dict] = []
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")

    async def complete(self, request):
        self.calls.append(request)
        return {
            "id": "c1", "model": "gpt-4",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
        }

    async def list_models(self):
        # Present so this fake also satisfies the deploy line's real
        # /v1/models passthrough handler (feat/v1-models-passthrough).
        return {"object": "list", "data": []}

    async def stream(self, request):
        self.calls.append(request)
        yield (b'data: {"id":"c1","model":"gpt-4","choices":[{"index":0,'
               b'"delta":{"role":"assistant","content":"ok"}}]}\n\n')
        yield b'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'

    async def close(self):
        return None


def _fat_tool_result() -> str:
    # ~20 KB of tabular JSON — well over the test threshold. No "handle" key.
    return json.dumps([{"id": i, "name": f"row-{i}", "desc": "x" * 80} for i in range(200)])


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    # Redirect ~/.jmunch (tracker, registry, metrics) into tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "metrics.db"))
    cfg = GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )
    return GatewayApp(cfg)


def _run(gateway, coro_factory):
    async def main():
        fake = _FakeUpstream()
        gateway.upstream_factory = lambda spec: fake
        async with TestClient(TestServer(build_aiohttp_app(gateway))) as client:
            return await coro_factory(client, fake)
    return asyncio.run(main())


# ---------------------------------------------------------------------------
# CHANGE 2 — X-Jmunch-Gateway response header
# ---------------------------------------------------------------------------

def test_gateway_header_on_non_streaming_response(gateway):
    async def go(client, fake):
        r = await client.post("/v1/chat/completions", json={
            "model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status == 200
        assert r.headers.get("X-Jmunch-Gateway") == __version__
    _run(gateway, go)


def test_gateway_header_on_streaming_response(gateway):
    async def go(client, fake):
        r = await client.post("/v1/chat/completions", json={
            "model": "gpt-4", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status == 200
        assert r.headers.get("X-Jmunch-Gateway") == __version__
        await r.read()
    _run(gateway, go)


def test_gateway_header_on_health(gateway):
    async def go(client, fake):
        r = await client.get("/health")
        assert r.status == 200
        assert r.headers.get("X-Jmunch-Gateway") == __version__
    _run(gateway, go)


def test_gateway_header_on_models(gateway):
    async def go(client, fake):
        # /v1/models behaviour differs across the proxy line (stub) and the
        # deploy line (real passthrough); the gateway header must be present
        # either way.
        r = await client.get("/v1/models")
        assert r.headers.get("X-Jmunch-Gateway") == __version__
    _run(gateway, go)


def test_gateway_header_on_error_response(gateway):
    async def go(client, fake):
        r = await client.post("/v1/chat/completions", data="not json",
                               headers={"Content-Type": "application/json"})
        assert r.status == 400
        assert r.headers.get("X-Jmunch-Gateway") == __version__
    _run(gateway, go)


# ---------------------------------------------------------------------------
# CHANGE 3 — X-Jmunch-Handleify request header
# ---------------------------------------------------------------------------

def _handleify_body(fat: str) -> dict:
    return {
        "model": "gpt-4",
        "tools": [{"type": "function",
                   "function": {"name": "t", "description": "x", "parameters": {}}}],
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [
                {"id": "c0", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c0", "content": fat},
        ],
    }


def _forwarded_tool_content(fake: _FakeUpstream) -> str:
    msg = next(m for m in fake.calls[-1]["messages"] if m.get("role") == "tool")
    return msg["content"]


def test_handleify_happens_by_default(gateway):
    async def go(client, fake):
        fat = _fat_tool_result()
        r = await client.post("/v1/chat/completions", json=_handleify_body(fat))
        assert r.status == 200
        # Fat tool content was replaced with a handle envelope.
        content = _forwarded_tool_content(fake)
        assert content != fat
        assert json.loads(content)["result"]["handle"].startswith("h_")
    _run(gateway, go)


def test_handleify_header_false_passes_raw(gateway):
    async def go(client, fake):
        fat = _fat_tool_result()
        r = await client.post("/v1/chat/completions", json=_handleify_body(fat),
                               headers={"X-Jmunch-Handleify": "false"})
        assert r.status == 200
        # Handle-ification disabled for this call → upstream sees raw content.
        assert _forwarded_tool_content(fake) == fat
    _run(gateway, go)


def test_handleify_header_other_value_still_handleifies(gateway):
    async def go(client, fake):
        fat = _fat_tool_result()
        r = await client.post("/v1/chat/completions", json=_handleify_body(fat),
                               headers={"X-Jmunch-Handleify": "true"})
        assert r.status == 200
        # Only false/0/no disable it; any other value leaves it on.
        assert _forwarded_tool_content(fake) != fat
    _run(gateway, go)
