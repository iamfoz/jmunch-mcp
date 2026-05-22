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
