"""Shared handle-ification helper for the gateway.

Takes a raw text payload (an OpenAI tool message `content`, or an Anthropic
`tool_result` block's content), classifies it via the sniffer, registers a
backend, and returns the jMRI envelope-wrapped replacement content.

Intentionally parallel to `proxy._maybe_handle_ify` — not DRY'd into a shared
helper with the MCP proxy per the refined plan (no churn in proxy.py). The
slice of logic is small and the two call sites have slightly different shapes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from ..backends.jsontree import JSONBackend
from ..backends.tabular import TabularBackend
from ..backends.text import TextBackend
from ..meta import SavingsTracker, envelope, timer_ms
from ..registry import Handle, HandleRegistry
from ..sniffer import Kind, classify, extract_rows

log = logging.getLogger("jmunch.gateway.handleify")

_HINT = (
    "This payload was large and has been replaced with a handle. "
    "Use the jmunch_peek, jmunch_slice, jmunch_search, or jmunch_describe "
    "tools (aggregate for tabular only, summarize for text only) to drill in."
)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_handle_envelope(
    handle: Handle, *,
    raw_bytes: int,
    tracker: SavingsTracker,
    started_ns: int,
) -> str:
    """Build the jMRI envelope JSON wrapping a handle reference. Used both
    on first-ingest and on content-cache hit so the envelope's _meta
    reflects THIS request's savings, not the original ingest's."""
    handle_result = {
        "handle": handle.id,
        "kind": handle.kind,
        "summary": handle.backend.summary(),
        "_hint": _HINT,
    }
    env = envelope(
        result=handle_result,
        raw_bytes=raw_bytes,
        response_bytes=0,
        tracker=tracker,
        timing_ms=timer_ms(started_ns),
    )
    env_text = json.dumps(env, default=str)
    env["_meta"]["response_tokens"] = len(env_text) // 4
    env["_meta"]["tokens_saved"] = max(0, (raw_bytes - len(env_text)) // 4)
    return json.dumps(env, default=str)


def maybe_handleify(
    text: str,
    *,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    threshold_tokens: int,
) -> tuple[str, str] | None:
    """If `text` is over threshold and classifiable, register a handle and
    return `(envelope_json, handle_id)`. Otherwise return None (passthrough).

    Content-addressed dedup: an identical payload (by SHA-256) reuses the
    existing handle instead of allocating a new backend. The envelope is
    rebuilt fresh on every call so per-request savings stay accurate.
    """
    threshold_bytes = threshold_tokens * 4
    if len(text) < threshold_bytes:
        return None

    # Content-addressed dedup — if we've seen this exact payload, reuse the
    # handle. Cheap (a SHA-256 of the inbound text) and worth it: identical
    # tool results across sessions / agents collapse to one backend.
    digest = _content_hash(text)
    existing = registry.find_by_hash(digest)
    if existing is not None:
        started = time.perf_counter_ns()
        env_text = _build_handle_envelope(
            existing, raw_bytes=len(text), tracker=tracker, started_ns=started,
        )
        log.info(
            "gateway handle dedup HIT: handle=%s raw=%d hash=%s…",
            existing.id, len(text), digest[:12],
        )
        return env_text, existing.id

    try:
        payload: Any = json.loads(text)
        kind = classify(payload)
    except json.JSONDecodeError:
        payload = text
        kind = Kind.TEXT

    started = time.perf_counter_ns()
    backend: Any
    summary_detail: dict[str, Any] = {}

    source: Any = None
    if kind is Kind.TEXT:
        text_payload = payload if isinstance(payload, str) else text
        try:
            backend = TextBackend(text_payload)
        except Exception as e:
            log.warning("text ingest failed, passthrough: %s", e)
            return None
        summary_detail = {"lines": len(backend._lines)}
        source = text_payload
    elif kind is Kind.TABULAR:
        rows = extract_rows(payload)
        if rows is None:
            return None
        try:
            backend = TabularBackend(rows)
        except Exception as e:
            log.warning("tabular ingest failed, passthrough: %s", e)
            return None
        summary_detail = {"rows": len(rows)}
        source = rows
    elif kind is Kind.JSON:
        try:
            backend = JSONBackend(payload)
        except Exception as e:
            log.warning("json ingest failed, passthrough: %s", e)
            return None
        summary_detail = {"nodes": backend._node_count}
        source = payload
    else:
        return None

    handle = registry.register(
        backend, backend.size_bytes, backend.kind,
        source=source, content_hash=digest,
    )
    raw_bytes = len(text)
    env_text = _build_handle_envelope(
        handle, raw_bytes=raw_bytes, tracker=tracker, started_ns=started,
    )
    log.info(
        "gateway handle-ified %s payload: raw=%d detail=%s handle=%s",
        backend.kind, raw_bytes, summary_detail, handle.id,
    )
    return env_text, handle.id
