"""Upstream HTTP adapters.

Each adapter forwards a parsed request dict to a real LLM provider and
returns the parsed response (non-streaming) or an async iterator of SSE
chunks (streaming — Phase 2).

Phase 1 ships `complete()` only. The `stream()` method is defined on the
protocol so Phase 2 can fill it in without touching call sites that don't
need streaming.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Protocol

from .config import UpstreamSpec

log = logging.getLogger("jmunch.gateway.upstream")


class UpstreamError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"upstream returned {status}: {body[:500]}")
        self.status = status
        self.body = body


class Upstream(Protocol):
    spec: UpstreamSpec
    bytes_sent_upstream: int
    bytes_received_upstream: int
    upstream_calls: int

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]: ...  # Phase 2

    async def close(self) -> None: ...


class _BaseHTTPUpstream:
    """Shared aiohttp session management. aiohttp is imported lazily so the
    package still imports without the [gateway] extra installed.

    Each instance tracks cumulative bytes POSTed to and received from the
    real upstream. These counters span the whole lifetime of the Upstream
    instance (which the gateway creates per app-side request), so summing
    them gives the true cost of handling one app-side request — including
    every verb-loop iteration.
    """

    def __init__(self, spec: UpstreamSpec) -> None:
        self.spec = spec
        self._session: Any = None  # aiohttp.ClientSession, lazy
        self._aiohttp: Any = None
        self.bytes_sent_upstream = 0      # total body bytes POSTed to upstream
        self.bytes_received_upstream = 0  # total body bytes received from upstream
        self.upstream_calls = 0           # POST count (for diagnostics)

    def _ensure_session(self) -> Any:
        if self._session is None:
            try:
                import aiohttp  # noqa
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "aiohttp is required for the gateway. "
                    "Install with: pip install 'jmunch-mcp[gateway]'"
                ) from e
            self._aiohttp = aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _scrub(body: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    if not keys:
        return body
    for k in keys:
        body.pop(k, None)
    return body


class OpenAIUpstream(_BaseHTTPUpstream):
    """Speaks OpenAI's `/v1/chat/completions`. Works for the real OpenAI API,
    Ollama, OpenRouter, LM Studio, and anything else OpenAI-compatible."""

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self._ensure_session()
        url = f"{self.spec.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"

        # Always non-streaming in Phase 1 regardless of what the app requested.
        body = dict(request)
        body = _scrub(body, self.spec.scrub_params)
        body["stream"] = False
        # `stream_options` is only valid alongside `stream=true`. If the app
        # sent streaming opts but we're forcing non-streaming, drop them so
        # strict upstreams (e.g. Anthropic's OpenAI-compat) don't 400.
        body.pop("stream_options", None)

        payload = json.dumps(body).encode("utf-8")
        self.bytes_sent_upstream += len(payload)
        self.upstream_calls += 1
        async with session.post(url, data=payload, headers=headers) as resp:
            text = await resp.text()
            self.bytes_received_upstream += len(text.encode("utf-8"))
            if resp.status >= 400:
                raise UpstreamError(resp.status, text)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise UpstreamError(resp.status, text) from e

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        session = self._ensure_session()
        url = f"{self.spec.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        body = dict(request)
        body = _scrub(body, self.spec.scrub_params)
        body["stream"] = True

        payload = json.dumps(body).encode("utf-8")
        self.bytes_sent_upstream += len(payload)
        self.upstream_calls += 1
        async with session.post(url, data=payload, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                self.bytes_received_upstream += len(text.encode("utf-8"))
                raise UpstreamError(resp.status, text)
            async for piece in resp.content.iter_any():
                self.bytes_received_upstream += len(piece)
                yield piece


class AnthropicUpstream(_BaseHTTPUpstream):
    """Speaks Anthropic's `/v1/messages`. Phase 3 wires this into the router."""

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self._ensure_session()
        url = f"{self.spec.base_url}/v1/messages"
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.spec.api_key:
            headers["x-api-key"] = self.spec.api_key

        body = dict(request)
        body = _scrub(body, self.spec.scrub_params)
        body["stream"] = False

        payload = json.dumps(body).encode("utf-8")
        self.bytes_sent_upstream += len(payload)
        self.upstream_calls += 1
        async with session.post(url, data=payload, headers=headers) as resp:
            text = await resp.text()
            self.bytes_received_upstream += len(text.encode("utf-8"))
            if resp.status >= 400:
                raise UpstreamError(resp.status, text)
            return json.loads(text)

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        session = self._ensure_session()
        url = f"{self.spec.base_url}/v1/messages"
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                   "Accept": "text/event-stream"}
        if self.spec.api_key:
            headers["x-api-key"] = self.spec.api_key
        body = dict(request)
        body = _scrub(body, self.spec.scrub_params)
        body["stream"] = True

        payload = json.dumps(body).encode("utf-8")
        self.bytes_sent_upstream += len(payload)
        self.upstream_calls += 1
        async with session.post(url, data=payload, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                self.bytes_received_upstream += len(text.encode("utf-8"))
                raise UpstreamError(resp.status, text)
            async for piece in resp.content.iter_any():
                self.bytes_received_upstream += len(piece)
                yield piece


def build(spec: UpstreamSpec) -> Upstream:
    if spec.kind == "openai":
        return OpenAIUpstream(spec)
    if spec.kind == "anthropic":
        return AnthropicUpstream(spec)
    raise ValueError(f"unknown upstream kind: {spec.kind}")
