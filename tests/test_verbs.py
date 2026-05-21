"""Dispatcher robustness: malformed model-supplied verb arguments must
surface as structured errors, never as unhandled exceptions (which would
kill the MCP proxy's pump loop or 500 the gateway)."""
from __future__ import annotations

from jmunch_mcp.backends.tabular import TabularBackend
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.verbs import Dispatcher


def _registry_with_handle():
    registry = HandleRegistry()
    backend = TabularBackend([{"id": i, "name": f"n{i}"} for i in range(10)])
    handle = registry.register(backend, backend.size_bytes, backend.kind)
    return registry, handle.id


def test_dispatch_unknown_tool_returns_error():
    registry, _ = _registry_with_handle()
    out = Dispatcher(registry).dispatch("jmunch_nonexistent", {})
    assert isinstance(out, dict) and out["code"] == "INVALID_ARGS"


def test_dispatch_survives_null_int_arg():
    """`n: null` would crash int(None) inside the handler."""
    registry, handle_id = _registry_with_handle()
    out = Dispatcher(registry).dispatch("jmunch_peek", {"handle": handle_id, "n": None})
    assert isinstance(out, dict) and out.get("code") == "INVALID_ARGS"


def test_dispatch_survives_non_numeric_int_arg():
    """`n: "abc"` would crash int("abc") inside the handler."""
    registry, handle_id = _registry_with_handle()
    out = Dispatcher(registry).dispatch("jmunch_peek", {"handle": handle_id, "n": "abc"})
    assert isinstance(out, dict) and out.get("code") == "INVALID_ARGS"


def test_dispatch_survives_bad_max_rows():
    registry, handle_id = _registry_with_handle()
    out = Dispatcher(registry).dispatch(
        "jmunch_slice", {"handle": handle_id, "selector": "id > 1", "max_rows": None}
    )
    assert isinstance(out, dict) and out.get("code") == "INVALID_ARGS"


def test_dispatch_valid_call_still_works():
    registry, handle_id = _registry_with_handle()
    out = Dispatcher(registry).dispatch("jmunch_peek", {"handle": handle_id, "n": 3})
    assert isinstance(out, list) and len(out) == 3
