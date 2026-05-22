"""Tool injection converts MCP jmunch.* schemas into OpenAI / Anthropic shapes
and respects the auto / always / never policy. Pure unit tests — no network,
no aiohttp."""
from __future__ import annotations

from jmunch_mcp.gateway.tool_injection import (
    HANDLE_ENVELOPE_SYSTEM,
    anthropic_tools,
    inject_into_anthropic_request,
    inject_into_openai_request,
    is_jmunch_gateway_tool,
    openai_tools,
    should_inject,
    to_mcp_name,
)
from jmunch_mcp.verbs import TOOL_SCHEMAS


def test_openai_tools_match_mcp_verb_set():
    names = {t["function"]["name"] for t in openai_tools()}
    expected = {s["name"].replace(".", "_") for s in TOOL_SCHEMAS}
    assert names == expected


def test_anthropic_tools_match_mcp_verb_set():
    names = {t["name"] for t in anthropic_tools()}
    expected = {s["name"].replace(".", "_") for s in TOOL_SCHEMAS}
    assert names == expected


def test_name_roundtrip():
    for s in TOOL_SCHEMAS:
        gw = s["name"].replace(".", "_")
        assert is_jmunch_gateway_tool(gw)
        assert to_mcp_name(gw) == s["name"]


def test_should_inject_policy():
    # auto: only when tools present
    assert should_inject([{"type": "function"}], "auto") is True
    assert should_inject([], "auto") is False
    assert should_inject(None, "auto") is False
    # always: always
    assert should_inject([], "always") is True
    assert should_inject(None, "always") is True
    # never: never
    assert should_inject([{"type": "function"}], "never") is False


def test_openai_injection_auto_no_existing_tools_skips():
    req = {"model": "gpt-4", "messages": []}
    out = inject_into_openai_request(req, mode="auto")
    assert "tools" not in out


def test_openai_injection_auto_appends():
    app_tool = {"type": "function", "function": {"name": "my_tool", "description": "x", "parameters": {}}}
    req = {"model": "gpt-4", "messages": [], "tools": [app_tool]}
    out = inject_into_openai_request(req, mode="auto")
    names = [t["function"]["name"] for t in out["tools"]]
    assert "my_tool" in names
    assert "jmunch_peek" in names
    assert len(out["tools"]) == 1 + len(TOOL_SCHEMAS)


def test_openai_injection_idempotent():
    req = {"model": "gpt-4", "messages": [], "tools": []}
    once = inject_into_openai_request(req, mode="always")
    twice = inject_into_openai_request(once, mode="always")
    assert len(once["tools"]) == len(twice["tools"])


def test_anthropic_injection_always():
    req = {"model": "claude-opus", "messages": []}
    out = inject_into_anthropic_request(req, mode="always")
    names = [t["name"] for t in out["tools"]]
    assert "jmunch_slice" in names


def test_openai_tool_name_shape_is_api_safe():
    # OpenAI allows ^[a-zA-Z0-9_-]{1,64}$ — no dots, no spaces.
    import re
    pat = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    for t in openai_tools():
        assert pat.match(t["function"]["name"]), t["function"]["name"]


# ---------------------------------------------------------------------------
# Always-on handle-envelope system instruction
# ---------------------------------------------------------------------------

def _openai_systems(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]


def test_openai_injection_adds_handle_envelope_system():
    req = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "t", "description": "x", "parameters": {}}}],
    }
    out = inject_into_openai_request(req, mode="auto")
    systems = _openai_systems(out["messages"])
    assert len(systems) == 1
    assert HANDLE_ENVELOPE_SYSTEM in systems[0]["content"]


def test_openai_injection_merges_into_existing_system():
    req = {
        "model": "gpt-4",
        "messages": [{"role": "system", "content": "APP RULES"},
                     {"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "t", "description": "x", "parameters": {}}}],
    }
    out = inject_into_openai_request(req, mode="auto")
    systems = _openai_systems(out["messages"])
    assert len(systems) == 1  # merged, not a second system message
    assert "APP RULES" in systems[0]["content"]
    # Our static text leads, so it stays a stable cacheable prefix.
    assert systems[0]["content"].startswith(HANDLE_ENVELOPE_SYSTEM)


def test_openai_system_injection_idempotent():
    req = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "tools": []}
    once = inject_into_openai_request(req, mode="always")
    twice = inject_into_openai_request(once, mode="always")
    assert len(_openai_systems(twice["messages"])) == 1


def test_openai_no_system_when_injection_skipped():
    # auto mode + no tools → injection is inactive → no system instruction.
    req = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    out = inject_into_openai_request(req, mode="auto")
    assert "tools" not in out
    assert _openai_systems(out["messages"]) == []


def test_anthropic_injection_adds_system_when_absent():
    req = {"model": "claude-opus", "messages": [],
           "tools": [{"name": "t", "description": "x", "input_schema": {"type": "object"}}]}
    out = inject_into_anthropic_request(req, mode="auto")
    assert out["system"] == HANDLE_ENVELOPE_SYSTEM


def test_anthropic_injection_prepends_to_string_system():
    req = {"model": "claude-opus", "messages": [], "system": "APP RULES", "tools": []}
    out = inject_into_anthropic_request(req, mode="always")
    assert out["system"].startswith(HANDLE_ENVELOPE_SYSTEM)
    assert "APP RULES" in out["system"]


def test_anthropic_injection_prepends_to_block_list_system():
    req = {"model": "claude-opus", "messages": [],
           "system": [{"type": "text", "text": "APP RULES"}], "tools": []}
    out = inject_into_anthropic_request(req, mode="always")
    assert isinstance(out["system"], list)
    assert out["system"][0] == {"type": "text", "text": HANDLE_ENVELOPE_SYSTEM}
    assert any(b.get("text") == "APP RULES" for b in out["system"])


def test_anthropic_system_injection_idempotent():
    req = {"model": "claude-opus", "messages": [], "tools": []}
    once = inject_into_anthropic_request(req, mode="always")
    twice = inject_into_anthropic_request(once, mode="always")
    assert twice["system"].count(HANDLE_ENVELOPE_SYSTEM) == 1


# ---------------------------------------------------------------------------
# default_model substitution (from feat/default-model-fallback)
# ---------------------------------------------------------------------------

def test_openai_injection_substitutes_default_model():
    req = {"messages": [{"role": "user", "content": "hi"}]}  # no model field
    out = inject_into_openai_request(req, mode="auto", default_model="gpt-4o")
    assert out["model"] == "gpt-4o"


def test_openai_injection_keeps_explicit_model():
    req = {"model": "gpt-4", "messages": []}
    out = inject_into_openai_request(req, mode="auto", default_model="gpt-4o")
    assert out["model"] == "gpt-4"


def test_anthropic_injection_substitutes_default_model_even_when_skipped():
    # mode="never" → no verb injection, but the model substitution still applies.
    req = {"messages": []}  # no model field
    out = inject_into_anthropic_request(req, mode="never", default_model="claude-x")
    assert out["model"] == "claude-x"
    assert "tools" not in out
