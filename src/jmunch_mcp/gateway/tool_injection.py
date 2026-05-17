"""Convert jmunch MCP tool schemas into the OpenAI / Anthropic request shapes.

MCP names already use the underscore form (`jmunch_peek`) to satisfy the
Anthropic API regex `^[a-zA-Z0-9_-]{1,64}$`, so the gateway-side name is
identical. This mapping is kept as an indirection in case the wire shapes
ever diverge again.
"""
from __future__ import annotations

import copy
from typing import Any

from ..verbs import TOOL_SCHEMAS


def _gateway_name(mcp_name: str) -> str:
    return mcp_name.replace(".", "_")


_GATEWAY_TO_MCP: dict[str, str] = {_gateway_name(s["name"]): s["name"] for s in TOOL_SCHEMAS}
_GATEWAY_TOOL_NAMES = frozenset(_GATEWAY_TO_MCP.keys())


def is_jmunch_gateway_tool(name: str) -> bool:
    return name in _GATEWAY_TOOL_NAMES


def to_mcp_name(gateway_name: str) -> str | None:
    return _GATEWAY_TO_MCP.get(gateway_name)


def openai_tools() -> list[dict[str, Any]]:
    """jmunch verb schemas in OpenAI's `{type:"function", function:{...}}` shape."""
    out: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        out.append({
            "type": "function",
            "function": {
                "name": _gateway_name(schema["name"]),
                "description": schema["description"],
                "parameters": copy.deepcopy(schema["inputSchema"]),
            },
        })
    return out


def anthropic_tools() -> list[dict[str, Any]]:
    """jmunch verb schemas in Anthropic's flat `{name, description, input_schema}` shape."""
    out: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        out.append({
            "name": _gateway_name(schema["name"]),
            "description": schema["description"],
            "input_schema": copy.deepcopy(schema["inputSchema"]),
        })
    return out


def should_inject(request_tools: list[Any] | None, mode: str) -> bool:
    """Policy: 'always' always injects, 'never' never, 'auto' only when the
    request already declares a non-empty tools array (the app is doing
    tool-calling — safe to add more)."""
    if mode == "never":
        return False
    if mode == "always":
        return True
    # auto
    return bool(request_tools)


def inject_into_openai_request(req: dict[str, Any], *, mode: str = "auto", default_model: str = "") -> dict[str, Any]:
    """Return a shallow copy of the request with jmunch tools appended to `tools`.

    Idempotent: a tool whose name already exists in the request is not duplicated.

    `default_model` substitutes into `req["model"]` when the inbound request
    omitted the field, so the forwarded request always has a valid model. Pass
    the second element of `GatewayConfig.resolve_upstream()`'s return tuple.
    """
    out = dict(req)
    if not out.get("model") and default_model:
        out["model"] = default_model

    existing = out.get("tools")
    if not should_inject(existing if isinstance(existing, list) else None, mode):
        return out

    merged = list(existing) if isinstance(existing, list) else []
    have_names = {
        t.get("function", {}).get("name") for t in merged if isinstance(t, dict)
    }
    for jt in openai_tools():
        if jt["function"]["name"] not in have_names:
            merged.append(jt)

    out["tools"] = merged
    return out


def inject_into_anthropic_request(req: dict[str, Any], *, mode: str = "auto", default_model: str = "") -> dict[str, Any]:
    """`default_model` substitutes into `req["model"]` when the inbound request
    omitted the field."""
    out = dict(req)
    if not out.get("model") and default_model:
        out["model"] = default_model

    existing = out.get("tools")
    if not should_inject(existing if isinstance(existing, list) else None, mode):
        return out

    merged = list(existing) if isinstance(existing, list) else []
    have_names = {t.get("name") for t in merged if isinstance(t, dict)}
    for jt in anthropic_tools():
        if jt["name"] not in have_names:
            merged.append(jt)

    out["tools"] = merged
    return out
