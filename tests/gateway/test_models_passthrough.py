"""Tests for /v1/models passthrough on the gateway HTTP layer.

Uses a FakeUpstream + monkeypatched upstream_factory so we exercise
the handler logic (resolution, caching, error forwarding, anthropic
fallback) without touching the network.
"""
from __future__ import annotations

from typing import Any

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from jmunch_mcp.gateway.config import (  # noqa: E402
    GatewayConfig,
    Interception,
    UpstreamSpec,
)
from jmunch_mcp.gateway.server import GatewayApp, build_aiohttp_app  # noqa: E402
from jmunch_mcp.gateway.upstreams import UpstreamError  # noqa: E402


class FakeOpenAIUpstream:
    def __init__(self, spec: UpstreamSpec, body: dict[str, Any] | None = None,
                 raises: Exception | None = None) -> None:
        self.spec = spec
        self._body = body or {"object": "list", "data": [
            {"id": "gpt-4o-mini", "context_length": 128000},
            {"id": "meta-llama/llama-3.3-70b-instruct:free", "context_length": 131072},
        ]}
        self._raises = raises
        self.list_models_calls = 0
        self.bytes_sent_upstream = 0
        self.bytes_received_upstream = 0
        self.upstream_calls = 0

    async def list_models(self) -> dict[str, Any]:
        self.list_models_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._body

    async def complete(self, request): ...  # not used here
    async def stream(self, request): ...    # not used here

    async def close(self) -> None:
        return None


class FakeAnthropicUpstream(FakeOpenAIUpstream):
    async def list_models(self) -> dict[str, Any]:
        self.list_models_calls += 1
        raise NotImplementedError("anthropic")


def _config(**kw) -> GatewayConfig:
    upstreams = kw.pop("upstreams", [
        UpstreamSpec(name="openai", kind="openai", base_url="http://fake-openai"),
    ])
    default_upstream = kw.pop("default_upstream", upstreams[0].name)
    return GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream=default_upstream,
        upstreams=upstreams,
        interception=Interception(threshold_tokens=100),
        models_cache_ttl_seconds=kw.pop("models_cache_ttl_seconds", 60),
    )


def _make_app(config: GatewayConfig, fakes: dict[str, FakeOpenAIUpstream], tmp_path):
    config.handles.store_path = str(tmp_path / "handles.db")
    gateway = GatewayApp(config)

    def factory(spec: UpstreamSpec):
        return fakes[spec.name]

    gateway.upstream_factory = factory  # type: ignore[assignment]
    return build_aiohttp_app(gateway), gateway


@pytest.mark.asyncio
async def test_passthrough_returns_upstream_catalog(tmp_path):
    spec = UpstreamSpec(name="openai", kind="openai", base_url="http://fake")
    fake = FakeOpenAIUpstream(spec)
    app, _ = _make_app(_config(upstreams=[spec]), {"openai": fake}, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")
        assert resp.status == 200
        body = await resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "gpt-4o-mini" in ids
    assert fake.list_models_calls == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_upstream(tmp_path):
    spec = UpstreamSpec(name="openai", kind="openai", base_url="http://fake")
    fake = FakeOpenAIUpstream(spec)
    app, _ = _make_app(
        _config(upstreams=[spec], models_cache_ttl_seconds=60),
        {"openai": fake},
        tmp_path,
    )
    async with TestClient(TestServer(app)) as client:
        await client.get("/v1/models")
        await client.get("/v1/models")
        await client.get("/v1/models")
    assert fake.list_models_calls == 1


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache(tmp_path):
    spec = UpstreamSpec(name="openai", kind="openai", base_url="http://fake")
    fake = FakeOpenAIUpstream(spec)
    app, _ = _make_app(
        _config(upstreams=[spec], models_cache_ttl_seconds=0),
        {"openai": fake},
        tmp_path,
    )
    async with TestClient(TestServer(app)) as client:
        await client.get("/v1/models")
        await client.get("/v1/models")
    assert fake.list_models_calls == 2


@pytest.mark.asyncio
async def test_upstream_error_forwarded(tmp_path):
    spec = UpstreamSpec(name="openai", kind="openai", base_url="http://fake")
    fake = FakeOpenAIUpstream(spec, raises=UpstreamError(401, '{"error":{"message":"bad key"}}'))
    app, _ = _make_app(_config(upstreams=[spec]), {"openai": fake}, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")
        assert resp.status == 401
        body = await resp.json()
    assert body["error"]["message"] == "bad key"


@pytest.mark.asyncio
async def test_transport_error_returns_502(tmp_path):
    spec = UpstreamSpec(name="openai", kind="openai", base_url="http://fake")
    fake = FakeOpenAIUpstream(spec, raises=ConnectionError("dns fail"))
    app, _ = _make_app(_config(upstreams=[spec]), {"openai": fake}, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")
        assert resp.status == 502
        body = await resp.json()
    assert "unreachable" in body["error"]["message"]


@pytest.mark.asyncio
async def test_anthropic_kind_falls_back_to_empty_stub(tmp_path):
    spec = UpstreamSpec(name="anthropic", kind="anthropic", base_url="http://fake")
    fake = FakeAnthropicUpstream(spec)
    app, _ = _make_app(_config(upstreams=[spec]), {"anthropic": fake}, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")
        assert resp.status == 200
        body = await resp.json()
    assert body == {"object": "list", "data": []}
    # We never even call the upstream when it's anthropic-kind
    assert fake.list_models_calls == 0


@pytest.mark.asyncio
async def test_explicit_upstream_header_routes_correctly(tmp_path):
    spec_a = UpstreamSpec(name="primary", kind="openai", base_url="http://a")
    spec_b = UpstreamSpec(name="secondary", kind="openai", base_url="http://b")
    fake_a = FakeOpenAIUpstream(spec_a, body={"object": "list", "data": [{"id": "from-a"}]})
    fake_b = FakeOpenAIUpstream(spec_b, body={"object": "list", "data": [{"id": "from-b"}]})
    app, _ = _make_app(
        _config(upstreams=[spec_a, spec_b], default_upstream="primary"),
        {"primary": fake_a, "secondary": fake_b},
        tmp_path,
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")
        assert (await resp.json())["data"][0]["id"] == "from-a"

        resp = await client.get("/v1/models", headers={"X-Jmunch-Upstream": "secondary"})
        assert (await resp.json())["data"][0]["id"] == "from-b"
    assert fake_a.list_models_calls == 1
    assert fake_b.list_models_calls == 1
