"""Wizard utilities — TOML reader/writer, remove-upstream, and the
'Textual missing' hint path. The Textual TUI itself is not unit-tested
(it would need a full terminal); these tests exercise the non-Textual
pieces that the TUI sits on top of."""
from __future__ import annotations

from pathlib import Path

import pytest

from jmunch_mcp.cli import service, wizard


def _toml(tmp_path) -> Path:
    return tmp_path / "gateway.toml"


# --------------------------------------------------------------------------
# TOML writer round-trips through the gateway config loader
# --------------------------------------------------------------------------

def test_write_gateway_toml_round_trips_through_loader(tmp_path):
    from jmunch_mcp.gateway.config import load

    p = _toml(tmp_path)
    data = {
        "gateway": {"listen": "0.0.0.0:7879", "default_upstream": "openai",
                    "log_level": "INFO"},
        "upstream": [
            {"name": "openai", "kind": "openai", "base_url": "https://api.openai.com"},
            {"name": "anthropic", "kind": "anthropic",
             "base_url": "https://api.anthropic.com",
             "api_key_env": "MY_ANT_KEY"},
        ],
        "interception": {"threshold_tokens": 1500, "inject_tools": "always"},
        "handles": {"ttl_seconds": 7200,
                    "store_path": "~/.jmunch/handles.db",
                    "max_bytes": 1_000_000_000},
    }
    wizard._write_gateway_toml(p, data)

    cfg = load(p)
    assert cfg.listen == "0.0.0.0:7879"
    assert cfg.default_upstream == "openai"
    assert [u.name for u in cfg.upstreams] == ["openai", "anthropic"]
    assert cfg.upstreams[1].api_key_env == "MY_ANT_KEY"
    assert cfg.interception.threshold_tokens == 1500
    assert cfg.interception.inject_tools == "always"
    assert cfg.handles.ttl_seconds == 7200


def test_write_gateway_toml_quotes_special_chars(tmp_path):
    p = _toml(tmp_path)
    wizard._write_gateway_toml(p, {
        "gateway": {"listen": "127.0.0.1:7879", "default_upstream": "x"},
        "upstream": [{"name": "x", "kind": "openai",
                      "base_url": 'https://api.example.com/"weird"/path'}],
    })
    # the loader must still accept it
    from jmunch_mcp.gateway.config import load
    cfg = load(p)
    assert cfg.upstreams[0].base_url == 'https://api.example.com/"weird"/path'


# --------------------------------------------------------------------------
# remove-upstream — non-interactive
# --------------------------------------------------------------------------

def _write_two_upstream_config(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = service._default_config()
    p.parent.mkdir(parents=True, exist_ok=True)
    wizard._write_gateway_toml(p, {
        "gateway": {"listen": "127.0.0.1:7879", "default_upstream": "openai"},
        "upstream": [
            {"name": "openai", "kind": "openai", "base_url": "https://api.openai.com"},
            {"name": "anthropic", "kind": "anthropic",
             "base_url": "https://api.anthropic.com"},
        ],
    })
    return p


def test_remove_upstream_removes_and_keeps_default(tmp_path, monkeypatch, capsys):
    p = _write_two_upstream_config(tmp_path, monkeypatch)
    rc = wizard._remove_upstream("anthropic")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed 'anthropic'" in out
    data = wizard._read_gateway_toml(p)
    assert [u["name"] for u in data["upstream"]] == ["openai"]
    assert data["gateway"]["default_upstream"] == "openai"   # untouched


def test_remove_upstream_updates_default_when_removed(tmp_path, monkeypatch, capsys):
    p = _write_two_upstream_config(tmp_path, monkeypatch)
    rc = wizard._remove_upstream("openai")   # the current default
    assert rc == 0
    assert "Default upstream updated to 'anthropic'" in capsys.readouterr().out
    data = wizard._read_gateway_toml(p)
    assert [u["name"] for u in data["upstream"]] == ["anthropic"]
    assert data["gateway"]["default_upstream"] == "anthropic"


def test_remove_upstream_unknown_name(tmp_path, monkeypatch, capsys):
    _write_two_upstream_config(tmp_path, monkeypatch)
    rc = wizard._remove_upstream("nope")
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "available: openai, anthropic" in err


def test_remove_upstream_refuses_to_empty_the_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = service._default_config()
    p.parent.mkdir(parents=True, exist_ok=True)
    wizard._write_gateway_toml(p, {
        "gateway": {"listen": "127.0.0.1:7879", "default_upstream": "only"},
        "upstream": [{"name": "only", "kind": "openai",
                      "base_url": "https://api.openai.com"}],
    })
    rc = wizard._remove_upstream("only")
    assert rc == 1
    assert "no upstreams" in capsys.readouterr().err


def test_remove_upstream_no_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = wizard._remove_upstream("x")
    assert rc == 2
    assert "no gateway config" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Textual-missing hint path — assumes textual is NOT installed in the test env
# --------------------------------------------------------------------------

def _textual_unavailable() -> bool:
    try:
        import textual  # noqa: F401
        return False
    except ImportError:
        return True


@pytest.mark.skipif(not _textual_unavailable(),
                    reason="textual is installed; the missing-hint test only "
                           "applies when it's not")
def test_setup_without_textual_prints_install_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    # --force so it doesn't ask about overwriting a (nonexistent) config
    rc = wizard._setup(force=True)
    err = capsys.readouterr().err
    assert rc == 2
    assert "Textual" in err
    assert "pipx inject" in err


@pytest.mark.skipif(not _textual_unavailable(),
                    reason="textual is installed; the missing-hint test only "
                           "applies when it's not")
def test_add_upstream_without_textual_prints_install_hint(tmp_path, monkeypatch, capsys):
    p = _write_two_upstream_config(tmp_path, monkeypatch)
    assert p.is_file()                # precondition: config exists
    rc = wizard._add_upstream()
    err = capsys.readouterr().err
    assert rc == 2
    assert "Textual" in err


def test_add_upstream_without_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = wizard._add_upstream()
    assert rc == 2
    err = capsys.readouterr().err
    assert "no gateway config" in err
    assert "gateway setup" in err


# --------------------------------------------------------------------------
# _env_var_for — derives a per-upstream env var name so two upstreams of
# the same kind don't collide on OPENAI_API_KEY / ANTHROPIC_API_KEY.
# --------------------------------------------------------------------------

def test_env_var_for_canonical_names():
    assert wizard._env_var_for("openai") == "OPENAI_API_KEY"
    assert wizard._env_var_for("anthropic") == "ANTHROPIC_API_KEY"
    assert wizard._env_var_for("airouter") == "AIROUTER_API_KEY"
    assert wizard._env_var_for("deepseek") == "DEEPSEEK_API_KEY"


def test_env_var_for_sanitises_unsafe_chars():
    assert wizard._env_var_for("my-finetune") == "MY_FINETUNE_API_KEY"
    assert wizard._env_var_for("openai.eu") == "OPENAI_EU_API_KEY"
    assert wizard._env_var_for("a b c") == "A_B_C_API_KEY"


def test_env_var_for_strips_edge_underscores_and_handles_empty():
    assert wizard._env_var_for("-weird-") == "WEIRD_API_KEY"
    assert wizard._env_var_for("") == "UPSTREAM_API_KEY"
    assert wizard._env_var_for("---") == "UPSTREAM_API_KEY"


# --------------------------------------------------------------------------
# _test_upstream — connection probe used by the Test button (and re-usable
# from a future CLI). urllib is mocked so we don't hit the network.
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, handler):
    """Replace urllib.request.urlopen with `handler(req, timeout=10)`."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", handler)


def test_test_upstream_openai_ok(monkeypatch):
    import json
    captured: dict = {}

    def fake(req, timeout=10):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(200, json.dumps(
            {"data": [{"id": "gpt-4"}, {"id": "gpt-4o"}, {"id": "o1"}]}
        ).encode())

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("https://api.openai.com/", "openai", "sk-test")
    assert ok is True
    assert "OK" in msg and "3 models" in msg
    assert captured["url"] == "https://api.openai.com/v1/models"
    # urllib title-cases header names, so check case-insensitively
    auth = {k.lower(): v for k, v in captured["headers"].items()}.get("authorization")
    assert auth == "Bearer sk-test"


def test_test_upstream_anthropic_uses_anthropic_headers(monkeypatch):
    import json
    captured: dict = {}

    def fake(req, timeout=10):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResponse(200, json.dumps({"data": [{"id": "claude-opus"}]}).encode())

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("https://api.anthropic.com", "anthropic", "sk-ant-x")
    assert ok is True and "1 model" in msg
    assert captured["headers"]["x-api-key"] == "sk-ant-x"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in captured["headers"]


def test_test_upstream_401(monkeypatch):
    import urllib.error

    def fake(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                     hdrs=None, fp=None)

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("https://api.openai.com", "openai", "sk-bad")
    assert ok is False
    assert "401" in msg
    assert "rejected" in msg


def test_test_upstream_401_without_key_suggests_adding_one(monkeypatch):
    import urllib.error

    def fake(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                     hdrs=None, fp=None)

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("https://api.openai.com", "openai", "")
    assert ok is False
    assert "requires an API key" in msg


def test_test_upstream_network_error(monkeypatch):
    import urllib.error

    def fake(req, timeout=10):
        raise urllib.error.URLError("Name or service not known")

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("https://does-not-exist.invalid", "openai", "k")
    assert ok is False
    assert "could not reach" in msg


def test_test_upstream_local_server_no_key(monkeypatch):
    """Ollama / LM Studio etc.: kind=openai, no key, returns 200."""
    import json
    captured: dict = {}

    def fake(req, timeout=10):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResponse(200, json.dumps({"data": [{"id": "llama-3"}]}).encode())

    _patch_urlopen(monkeypatch, fake)
    ok, msg = wizard._test_upstream("http://127.0.0.1:11434", "openai", "")
    assert ok is True
    # no Authorization header sent when key is empty
    assert "authorization" not in captured["headers"]


def test_test_upstream_rejects_unknown_kind(monkeypatch):
    ok, msg = wizard._test_upstream("https://x", "weird", "k")
    assert ok is False
    assert "unknown upstream kind" in msg


def test_test_upstream_rejects_blank_base_url():
    ok, msg = wizard._test_upstream("", "openai", "k")
    assert ok is False
    assert "base URL is required" in msg
