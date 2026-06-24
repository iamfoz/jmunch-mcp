"""Optional debug dump of the exact outbound upstream request.

Gated behind the ``JMUNCH_DEBUG_DUMP`` env var (truthy = ``1``/``true``/
``yes``/``on``). Default off: when unset, :func:`dump_upstream_request`
returns immediately — no files, no log output, no behaviour change.

When on, every upstream call writes the complete request body actually
forwarded — model, full messages array, system field, tools, all
untruncated (handle envelopes included) — as pretty-printed JSON to a
timestamped file under ``~/.jmunch/debug/``.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("jmunch.gateway.debug")

_TRUTHY = ("1", "true", "yes", "on")

# Process-wide sequence — guarantees a unique, call-ordered filename even if
# two dumps land in the same microsecond.
_seq = itertools.count(1)


def enabled() -> bool:
    """True when ``JMUNCH_DEBUG_DUMP`` is set to a truthy value."""
    return os.environ.get("JMUNCH_DEBUG_DUMP", "").strip().lower() in _TRUTHY


def _dump_dir() -> Path:
    return Path.home() / ".jmunch" / "debug"


def dump_upstream_request(request: dict[str, Any], *, route: str, phase: str) -> None:
    """Dump ``request`` — the exact outbound upstream body, after tool and
    system-instruction injection and handle-ification — to a timestamped
    pretty-printed JSON file when ``JMUNCH_DEBUG_DUMP`` is truthy. No-op
    otherwise.

    ``route`` is ``"openai"`` | ``"anthropic"``; ``phase`` distinguishes the
    first turn (``"first"`` / ``"first-stream"``) from verb-loop iterations
    (``"verb-loop"``). Never raises — debug tooling must not break a live
    request.
    """
    if not enabled():
        return
    try:
        dump_dir = _dump_dir()
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        path = dump_dir / f"{stamp}_{next(_seq):06d}_{route}_{phase}.json"
        path.write_text(
            json.dumps(request, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("JMUNCH_DEBUG_DUMP: %s/%s upstream request -> %s", route, phase, path)
    except Exception as e:  # pragma: no cover - never break a live request
        log.warning("JMUNCH_DEBUG_DUMP: failed to write dump: %s", e)
