import json
from pathlib import Path

from jmunch_mcp.config import Config, UpstreamConfig
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.proxy import Proxy


def _make(tmp_path: Path, threshold: int = 100) -> Proxy:
    p = Proxy(Config(upstream=UpstreamConfig(command="noop"), threshold_tokens=threshold))
    p.tracker = SavingsTracker(path=tmp_path / "_savings.json")
    p.stats.tokens_saved_at_start = p.tracker.total
    return p


def _tool_call(text: str, msg_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def test_stats_track_handle_and_reuse(tmp_path):
    p = _make(tmp_path)
    p._pending[2] = "tools/call"
    rows = [{"id": i, "name": f"n{i}"} for i in range(20)]
    out = p._maybe_rewrite_response(_tool_call(json.dumps(rows)))
    handle_id = json.loads(out["result"]["content"][0]["text"])["result"]["handle"]

    assert p.stats.handles_created == 1
    assert p.stats.handles_by_kind["tabular"] == 1

    p.dispatcher.dispatch("jmunch_peek", {"handle": handle_id, "n": 3})
    p.dispatcher.dispatch("jmunch_describe", {"handle": handle_id})
    assert p.stats.handle_reuses == 2


def test_stats_track_passthrough(tmp_path):
    p = _make(tmp_path, threshold=1000)
    p._pending[2] = "tools/call"
    p._maybe_rewrite_response(_tool_call(json.dumps([{"a": 1}])))
    assert p.stats.passthroughs == 1
    assert p.stats.handles_created == 0


def test_list_handles_does_not_count_as_reuse(tmp_path):
    p = _make(tmp_path)
    p.dispatcher.dispatch("jmunch_list_handles", {})
    assert p.stats.handle_reuses == 0


def test_expired_handle_does_not_count_as_reuse(tmp_path):
    p = _make(tmp_path)
    p.dispatcher.dispatch("jmunch_peek", {"handle": "h_nope"})
    assert p.stats.handle_reuses == 0


def test_report_renders_required_fields(tmp_path):
    p = _make(tmp_path)
    p._pending[2] = "tools/call"
    rows = [{"id": i, "title": f"issue number {i} with padding"} for i in range(30)]
    p._maybe_rewrite_response(_tool_call(json.dumps(rows)))
    p.stats.finalize(p.tracker.total)
    output = p.stats.render()
    assert "session tokens saved" in output
    assert "handles created" in output
    assert "handle reuses" in output
    assert "backend distribution" in output
    assert "tabular=1" in output


def test_session_tokens_saved_is_delta(tmp_path):
    # Prime a saved total, then start a fresh proxy.
    path = tmp_path / "_savings.json"
    SavingsTracker(path=path).record(5000)

    p = Proxy(Config(upstream=UpstreamConfig(command="noop"), threshold_tokens=100))
    p.tracker = SavingsTracker(path=path)
    p.stats.tokens_saved_at_start = p.tracker.total  # 5000

    p._pending[2] = "tools/call"
    # A genuinely fat payload — the handle envelope must be much smaller than
    # the raw rows for there to be real savings to measure.
    rows = [{"id": i, "name": f"user_{i}", "bio": "x" * 100} for i in range(200)]
    p._maybe_rewrite_response(_tool_call(json.dumps(rows)))
    p.stats.finalize(p.tracker.total)

    # Session delta reflects only the current session's savings.
    assert p.stats.session_tokens_saved > 0
    assert p.stats.session_tokens_saved < p.tracker.total
