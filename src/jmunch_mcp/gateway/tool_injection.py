"""Convert jmunch MCP tool schemas into the OpenAI / Anthropic request shapes.

MCP names already use the underscore form (`jmunch_peek`) to satisfy the
Anthropic API regex `^[a-zA-Z0-9_-]{1,64}$`, so the gateway-side name is
identical. This mapping is kept as an indirection in case the wire shapes
ever diverge again.
"""
from __future__ import annotations

import copy
import json
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


def should_inject(mode: str, *, has_handle: bool) -> bool:
    """Verb-injection policy:

      always — inject unconditionally.
      never  — never inject.
      auto   — inject only when the (already handle-ified) request carries a
               handle envelope. That is exactly when the verbs are needed: a
               handle exists to drill into. No handle → the verbs would have
               nothing to operate on, so injecting them is pure overhead.

    `auto` keys off the handle envelope rather than the request's `tools`
    array: the verbs are useful precisely when jmunch handle-ified something,
    regardless of whether — or how — the app declared its own tools.
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    return has_handle  # auto


def _looks_like_handle_envelope(text: str) -> bool:
    """True if `text` is a jMRI handle envelope — a JSON object whose
    `result` carries a string `handle`."""
    try:
        env = json.loads(text)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(env, dict)
        and isinstance(env.get("result"), dict)
        and isinstance(env["result"].get("handle"), str)
    )


def _openai_request_has_handle(req: dict[str, Any]) -> bool:
    """True if any `role: "tool"` message in `req` is a handle envelope."""
    for m in req.get("messages") or []:
        if (isinstance(m, dict) and m.get("role") == "tool"
                and isinstance(m.get("content"), str)
                and _looks_like_handle_envelope(m["content"])):
            return True
    return False


def _anthropic_request_has_handle(req: dict[str, Any]) -> bool:
    """True if any `tool_result` block in `req` is a handle envelope."""
    for m in req.get("messages") or []:
        if not (isinstance(m, dict) and isinstance(m.get("content"), list)):
            continue
        for block in m["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            content = block.get("content")
            candidates: list[str] = []
            if isinstance(content, str):
                candidates.append(content)
            elif isinstance(content, list):
                candidates.extend(
                    c["text"] for c in content
                    if isinstance(c, dict) and isinstance(c.get("text"), str)
                )
            if any(_looks_like_handle_envelope(t) for t in candidates):
                return True
    return False


# Static system instruction explaining handle envelopes. Injected into every
# forwarded request whenever verb injection is active (see inject_into_*), so
# the model recognises a handle envelope sitting in conversation history even
# on an ordinary turn — not only during the internal verb loop. Kept static
# and in a stable leading position so it does not bust upstream prompt caching.
# Keep the verb list consistent with openai_route._DRILL_IN_SYSTEM.
HANDLE_ENVELOPE_SYSTEM = (
    "Handle envelopes: a tool result in this conversation may be replaced by "
    "a compact \"handle envelope\" — a JSON object with the keys handle, kind, "
    "summary, _hint and _meta. That envelope is the jmunch gateway compressing "
    "a large tool output; it is NOT a file or data payload the user attached. "
    "When the envelope's summary already answers the user, just answer the "
    "user. To inspect more of the underlying data, call jmunch_peek, "
    "jmunch_slice, jmunch_search, jmunch_describe, jmunch_summarize or "
    "jmunch_aggregate with the handle. Never greet the user or ask what they "
    "would like to do with the payload merely because a handle envelope "
    "appears."
)


def _openai_messages_with_system(messages: Any) -> list[Any]:
    """Return `messages` with `HANDLE_ENVELOPE_SYSTEM` present as a role:system
    message — merged onto the front of the leading system message when there is
    one, otherwise prepended as a fresh system message. Idempotent."""
    msgs = list(messages) if isinstance(messages, list) else []
    for m in msgs:
        if (isinstance(m, dict) and m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and HANDLE_ENVELOPE_SYSTEM in m["content"]):
            return msgs
    if (msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system"
            and isinstance(msgs[0].get("content"), str)):
        head = dict(msgs[0])
        head["content"] = HANDLE_ENVELOPE_SYSTEM + "\n\n" + head["content"]
        return [head, *msgs[1:]]
    return [{"role": "system", "content": HANDLE_ENVELOPE_SYSTEM}, *msgs]


def _anthropic_system_with_instruction(system: Any) -> Any:
    """Return the Anthropic top-level `system` field with `HANDLE_ENVELOPE_SYSTEM`
    prepended. Handles both the plain-string and content-block-list shapes.
    Idempotent."""
    if not system:
        return HANDLE_ENVELOPE_SYSTEM
    if isinstance(system, str):
        if HANDLE_ENVELOPE_SYSTEM in system:
            return system
        return HANDLE_ENVELOPE_SYSTEM + "\n\n" + system
    if isinstance(system, list):
        for block in system:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and HANDLE_ENVELOPE_SYSTEM in block["text"]):
                return system
        return [{"type": "text", "text": HANDLE_ENVELOPE_SYSTEM}, *system]
    return system


def inject_into_openai_request(
    req: dict[str, Any], *, mode: str = "auto", default_model: str = "",
) -> dict[str, Any]:
    """Return a shallow copy of the request with jmunch verb tools appended to
    `tools` and the handle-envelope system instruction merged into `messages`.

    Idempotent: a tool whose name already exists is not duplicated, and the
    system instruction is added at most once.

    `default_model` substitutes into `req["model"]` when the inbound request
    omitted the field, so the forwarded request always has a valid model. Pass
    the second element of `GatewayConfig.resolve_upstream()`'s return tuple.
    The model substitution applies even when verb injection is skipped.
    """
    out = dict(req)
    if not out.get("model") and default_model:
        out["model"] = default_model

    if not should_inject(mode, has_handle=_openai_request_has_handle(out)):
        return out

    existing = out.get("tools")
    merged = list(existing) if isinstance(existing, list) else []
    have_names = {
        t.get("function", {}).get("name") for t in merged if isinstance(t, dict)
    }
    for jt in openai_tools():
        if jt["function"]["name"] not in have_names:
            merged.append(jt)

    out["tools"] = merged
    out["messages"] = _openai_messages_with_system(out.get("messages"))
    return out


def inject_into_anthropic_request(
    req: dict[str, Any], *, mode: str = "auto", default_model: str = "",
) -> dict[str, Any]:
    """Return a shallow copy of the request with jmunch verb tools appended to
    `tools` and the handle-envelope system instruction prepended to the
    top-level `system` field. Idempotent.

    `default_model` substitutes into `req["model"]` when the inbound request
    omitted the field; this applies even when verb injection is skipped.
    """
    out = dict(req)
    if not out.get("model") and default_model:
        out["model"] = default_model

    if not should_inject(mode, has_handle=_anthropic_request_has_handle(out)):
        return out

    existing = out.get("tools")
    merged = list(existing) if isinstance(existing, list) else []
    have_names = {t.get("name") for t in merged if isinstance(t, dict)}
    for jt in anthropic_tools():
        if jt["name"] not in have_names:
            merged.append(jt)

    out["tools"] = merged
    out["system"] = _anthropic_system_with_instruction(out.get("system"))
    return out
