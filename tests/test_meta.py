from pathlib import Path

from jmunch_mcp.meta import SavingsTracker, envelope, estimate_savings


def test_savings_calc_matches_jmri_spec():
    assert estimate_savings(4000, 400) == 900  # (4000-400)//4
    assert estimate_savings(100, 500) == 0  # no negative


def test_envelope_shape(tmp_path: Path):
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(result={"hello": "world"}, raw_bytes=4000, response_bytes=400, tracker=tracker)
    assert "result" in env
    assert "error" not in env
    meta = env["_meta"]
    assert meta["tokens_saved"] == 900
    assert meta["total_tokens_saved"] == 900
    assert meta["response_tokens"] == 100
    assert meta["naive_tokens"] == 1000
    assert meta["retrieval_engine"] == "jmunch"
    assert meta["retrieval_version"] == "1.0"
    assert "powered_by" in meta


def test_envelope_error_shape(tmp_path: Path):
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(
        error={"code": "NOT_FOUND", "message": "nope"},
        raw_bytes=0,
        response_bytes=0,
        tracker=tracker,
    )
    assert "error" in env
    assert env["error"]["code"] == "NOT_FOUND"
    assert env["_meta"]["tokens_saved"] == 0


def test_envelope_self_measures_when_response_bytes_omitted(tmp_path: Path):
    """Handle-ification omits response_bytes so the envelope sizes itself.

    Regression: passing response_bytes=0 used to record raw_bytes/4 of
    savings into the tracker — far more than the handle envelope actually
    saved. The tracker total must match the displayed savings exactly.
    """
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(
        result={"handle": "h_x", "summary": {"rows": 100}},
        raw_bytes=40_000,
        tracker=tracker,
    )
    meta = env["_meta"]
    # Savings is raw minus the envelope's own size — strictly less than raw/4.
    assert 0 < meta["tokens_saved"] < 40_000 // 4
    assert meta["response_tokens"] > 0
    # The tracker recorded exactly the displayed savings — no over-count.
    assert tracker.total == meta["tokens_saved"] == meta["total_tokens_saved"]


def test_envelope_self_measure_records_once(tmp_path: Path):
    """Self-measuring path must call tracker.record exactly once."""
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    env = envelope(result={"handle": "h_y"}, raw_bytes=20_000, tracker=tracker)
    assert tracker.total == env["_meta"]["tokens_saved"]


def test_tracker_persists(tmp_path: Path):
    path = tmp_path / "_savings.json"
    t1 = SavingsTracker(path=path)
    t1.record(1000)
    t1.record(500)
    assert t1.total == 1500

    t2 = SavingsTracker(path=path)
    assert t2.total == 1500


def test_record_is_cross_process_safe(tmp_path: Path):
    """Two trackers constructed before either records — stands in for two
    proxy processes that both read the (empty) savings file at startup.

    Regression: each record() held only an in-process lock and wrote its
    own stale in-memory total, so concurrent processes silently lost each
    other's increments. record() now re-reads the on-disk total under a
    cross-process lock, so the increments accumulate.
    """
    path = tmp_path / "_savings.json"
    t1 = SavingsTracker(path=path)
    t2 = SavingsTracker(path=path)

    t1.record(100)
    t2.record(50)   # must accumulate onto t1's 100, not overwrite with 50
    t1.record(25)

    assert SavingsTracker(path=path).total == 175
    # The recording tracker also sees the merged total it just wrote.
    assert t1.total == 175
