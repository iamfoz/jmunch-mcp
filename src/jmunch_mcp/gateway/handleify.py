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
from typing import Any

from ..backends.jsontree import JSONBackend
from ..backends.tabular import TabularBackend
from ..backends.text import TextBackend
from ..meta import BYTES_PER_TOKEN, SavingsTracker, envelope
from ..registry import Handle, HandleRegistry
from ..sniffer import Kind, classify, extract_rows
from .config import Interception
from .context_window import window_for

log = logging.getLogger("jmunch.gateway.handleify")


def request_is_eligible(
    request_bytes: int,
    model: str | None,
    *,
    interception: Interception,
) -> bool:
    """Context-aware gate for request-side handle-ification.

    With `context_fraction == 0` (default) every request is eligible — the
    previous behaviour. When it is set, a request is only handle-ified once
    its estimated token count reaches that fraction of the model's context
    window. The point: on a 262k-token model a request that comfortably
    fits should be forwarded verbatim — compressing it only sheds detail
    the model still has room to hold.
    """
    if interception.context_fraction <= 0.0:
        return True
    window = window_for(
        model,
        default_window=interception.default_context_window,
        overrides=interception.context_windows,
    )
    request_tokens = request_bytes // BYTES_PER_TOKEN
    return request_tokens >= interception.context_fraction * window


def recency_protected_count(total: int, recency_window: int) -> int:
    """How many of `total` ordered tool_results sit inside the recency
    window and must be left verbatim. Clamped to `[0, total]`."""
    if recency_window <= 0 or total <= 0:
        return 0
    return min(recency_window, total)


def maybe_handleify(
    text: str,
    *,
    registry: HandleRegistry,
    tracker: SavingsTracker,
    threshold_tokens: int,
) -> tuple[str, str] | None:
    """If `text` is over threshold and classifiable, register a handle and
    return `(envelope_json, handle_id)`. Otherwise return None (passthrough).

    The 2-tuple shape is preserved for backward compat with existing tests.
    For exact-token accounting, callers can hold on to the original `text`
    and compare with the returned envelope via `TokenCounter`.
    """
    threshold_bytes = threshold_tokens * 4
    if len(text) < threshold_bytes:
        return None

    # Content-addressed dedup: identical payloads share one handle (and one
    # backend), and the handle ID is derived from the hash, so the same
    # payload always gets the same ID, across processes and restarts too.
    digest = _content_hash(text)
    existing = registry.find_by_hash(digest)
    if existing is not None:
        return _handle_envelope(existing, raw_bytes=len(text), tracker=tracker), existing.id

    try:
        payload: Any = json.loads(text)
        kind = classify(payload)
    except json.JSONDecodeError:
        payload = text
        kind = Kind.TEXT

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
        backend, backend.size_bytes, backend.kind, source=source, content_hash=digest,
    )
    env_text = _handle_envelope(handle, raw_bytes=len(text), tracker=tracker)
    log.info(
        "gateway handle-ified %s payload: raw=%d detail=%s handle=%s",
        backend.kind, len(text), summary_detail, handle.id,
    )
    return env_text, handle.id


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _handle_envelope(handle: Handle, *, raw_bytes: int, tracker: SavingsTracker) -> str:
    """The envelope that replaces a tool_result in conversation history.

    Apps re-send the whole conversation every turn, so this must be
    byte-identical for the same payload: a content-derived handle ID and a
    `stable` envelope (no running totals or timing). Otherwise the upstream's
    prompt cache misses from the first handle-ified message onward, every turn.
    """
    handle_result = {
        "handle": handle.id,
        "kind": handle.kind,
        "summary": handle.backend.summary(),
        "_hint": (
            "This payload was large and has been replaced with a handle. "
            "Use the jmunch_peek, jmunch_slice, jmunch_search, or jmunch_describe "
            "tools (aggregate for tabular only, summarize for text only) to drill in."
        ),
    }
    # response_bytes omitted → the envelope self-measures and records the
    # true savings (passing 0 would over-credit the tracker by raw_bytes).
    env = envelope(result=handle_result, raw_bytes=raw_bytes, tracker=tracker, stable=True)
    return json.dumps(env, default=str)
