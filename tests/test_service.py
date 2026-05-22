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


def test_renderers_embed_env_dict_in_both_unit_kinds():
    cfg, out, err = Path("/c"), Path("/o"), Path("/e")
    env = {"JMUNCH_DEBUG_DUMP": "1", "OPENAI_API_KEY": "sk-test", "OTHER": "x"}
    plist = plistlib.loads(service.render_launchd_plist("l", cfg, out, err, env=env))
    assert plist["EnvironmentVariables"] == env
    unit = service.render_systemd_unit(cfg, out, err, env=env)
    for k, v in env.items():
        assert f"Environment={k}={v}" in unit


def test_renderers_without_env_omit_the_env_block():
    cfg, out, err = Path("/c"), Path("/o"), Path("/e")
    plist = plistlib.loads(service.render_launchd_plist("l", cfg, out, err))
    assert "EnvironmentVariables" not in plist
    unit = service.render_systemd_unit(cfg, out, err)
    assert "Environment=" not in unit


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
    err = capsys.readouterr().err
    assert "no gateway config" in err
    assert "gateway init" in err


def test_init_writes_a_loadable_config(tmp_path):
    from jmunch_mcp.gateway.config import load

    dest = tmp_path / "gateway.toml"
    rc = service.main("init", ["--config", str(dest)])
    assert rc == 0
    assert dest.is_file()
    # the generated template must itself be a valid gateway config
    cfg = load(dest)
    assert cfg.upstreams and cfg.upstreams[0].kind == "openai"
    assert cfg.interception.inject_tools == "auto"


def test_init_does_not_overwrite_without_force(tmp_path, capsys):
    dest = tmp_path / "gateway.toml"
    dest.write_text("custom = true\n", encoding="utf-8")
    rc = service.main("init", ["--config", str(dest)])
    assert rc == 0
    assert dest.read_text() == "custom = true\n"  # untouched
    assert "already exists" in capsys.readouterr().out


def test_init_force_overwrites(tmp_path):
    dest = tmp_path / "gateway.toml"
    dest.write_text("custom = true\n", encoding="utf-8")
    rc = service.main("init", ["--config", str(dest), "--force"])
    assert rc == 0
    assert "[[upstream]]" in dest.read_text()


def test_install_defaults_to_jmunch_home_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    # no --config, and ~/.jmunch/gateway.toml is absent → error pointing at setup/init
    rc = service.main("install", [])
    assert rc == 2
    assert "gateway init" in capsys.readouterr().err


def test_env_file_round_trip(tmp_path):
    p = tmp_path / "env"
    service._write_env_file(p, {"OPENAI_API_KEY": "sk-1", "ANTHROPIC_API_KEY": "sk-ant"})
    assert p.is_file()
    # mode 0600
    assert (p.stat().st_mode & 0o777) == 0o600
    out = service._read_env_file(p)
    assert out == {"OPENAI_API_KEY": "sk-1", "ANTHROPIC_API_KEY": "sk-ant"}


def test_env_file_parser_ignores_comments_and_blanks(tmp_path):
    p = tmp_path / "env"
    p.write_text("# a comment\n\nFOO=bar\n  BAZ = qux  \nbad-line-no-equals\n",
                 encoding="utf-8")
    assert service._read_env_file(p) == {"FOO": "bar", "BAZ": "qux"}


def test_env_file_missing_returns_empty(tmp_path):
    assert service._read_env_file(tmp_path / "no-such") == {}


def test_install_embeds_env_file_vars_in_unit(monkeypatch, tmp_path):
    """End-to-end: `_install` reads the env file and embeds its KEY=VALUEs
    in the rendered systemd unit alongside JMUNCH_DEBUG_DUMP."""
    import subprocess as _sp
    monkeypatch.setenv("HOME", str(tmp_path))
    # stub launchctl/systemctl so we don't depend on a real init system
    monkeypatch.setattr(service, "_systemctl",
                        lambda *a: _sp.CompletedProcess(a, 0, "", ""))
    config = tmp_path / "gw.toml"
    config.write_text(
        '[gateway]\ndefault_upstream="o"\n[[upstream]]\n'
        'name="o"\nkind="openai"\nbase_url="http://x"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / "env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nFOO=bar\n", encoding="utf-8")
    rc = service._install(service.DEFAULT_LABEL, config, "systemd",
                          debug_dump=False, env_file=env_file)
    assert rc == 0
    unit = service._systemd_unit_path(service.DEFAULT_LABEL).read_text()
    assert "Environment=JMUNCH_DEBUG_DUMP=0" in unit
    assert "Environment=OPENAI_API_KEY=sk-test" in unit
    assert "Environment=FOO=bar" in unit
