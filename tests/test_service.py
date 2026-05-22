"""Gateway service-management CLI: unit-file rendering and platform
selection. Pure unit tests — no launchctl/systemctl calls."""
from __future__ import annotations

import plistlib
from pathlib import Path

from jmunch_mcp.cli import service


def test_launchd_plist_round_trips_with_expected_keys():
    data = service.render_launchd_plist(
        "sh.jmunch.gateway",
        Path("/etc/jmunch/gateway.toml"),
        Path("/logs/out"),
        Path("/logs/err"),
    )
    d = plistlib.loads(data)
    assert d["Label"] == "sh.jmunch.gateway"
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is True
    assert d["StandardOutPath"] == "/logs/out"
    assert d["StandardErrorPath"] == "/logs/err"
    args = d["ProgramArguments"]
    assert args[1:4] == ["-m", "jmunch_mcp", "gateway"]
    assert args[-2:] == ["--config", "/etc/jmunch/gateway.toml"]


def test_launchd_plist_handles_special_chars_in_path():
    # plistlib escapes XML for us — a path with & and spaces must round-trip.
    cfg = Path("/weird & path/my gateway.toml")
    d = plistlib.loads(service.render_launchd_plist("l", cfg, Path("/o"), Path("/e")))
    assert d["ProgramArguments"][-1] == str(cfg)


def test_systemd_unit_has_execstart_restart_and_logs():
    unit = service.render_systemd_unit(
        Path("/etc/jmunch/gateway.toml"), Path("/logs/out"), Path("/logs/err")
    )
    assert "ExecStart=" in unit
    assert "-m jmunch_mcp gateway --config" in unit
    assert "Restart=on-failure" in unit
    assert "StandardOutput=append:/logs/out" in unit
    assert "StandardError=append:/logs/err" in unit
    assert "WantedBy=default.target" in unit


def test_systemd_unit_quotes_paths_with_spaces():
    unit = service.render_systemd_unit(
        Path("/a b/gateway.toml"), Path("/o"), Path("/e")
    )
    assert '"/a b/gateway.toml"' in unit


def test_debug_dump_defaults_off_in_both_unit_kinds():
    cfg, out, err = Path("/c"), Path("/o"), Path("/e")
    plist = plistlib.loads(service.render_launchd_plist("l", cfg, out, err))
    assert plist["EnvironmentVariables"]["JMUNCH_DEBUG_DUMP"] == "0"
    unit = service.render_systemd_unit(cfg, out, err)
    assert "Environment=JMUNCH_DEBUG_DUMP=0" in unit


def test_debug_dump_on_when_requested():
    cfg, out, err = Path("/c"), Path("/o"), Path("/e")
    plist = plistlib.loads(
        service.render_launchd_plist("l", cfg, out, err, debug_dump=True)
    )
    assert plist["EnvironmentVariables"]["JMUNCH_DEBUG_DUMP"] == "1"
    unit = service.render_systemd_unit(cfg, out, err, debug_dump=True)
    assert "Environment=JMUNCH_DEBUG_DUMP=1" in unit


def test_backend_selection(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service._backend() == "launchd"
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service._backend() == "systemd"
    monkeypatch.setattr(service.sys, "platform", "win32")
    assert service._backend() is None


def test_main_rejects_unsupported_platform(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "win32")
    rc = service.main("status", [])
    assert rc == 2
    assert "not supported" in capsys.readouterr().err


def test_main_install_rejects_missing_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    rc = service.main("install", ["--config", str(tmp_path / "nope.toml")])
    assert rc == 2
    assert "config not found" in capsys.readouterr().err
