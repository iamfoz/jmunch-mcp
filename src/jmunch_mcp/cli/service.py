"""Install and manage the jmunch gateway as a user-level background service.

  macOS  → a launchd agent     (~/Library/LaunchAgents/<label>.plist)
  Linux  → a systemd user unit (~/.config/systemd/user/<label>.service)

This is agent-agnostic: it manages the gateway HTTP service only. Nothing
here knows about any particular AI application or agent framework.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

DEFAULT_LABEL = "sh.jmunch.gateway"
SERVICE_VERBS = ("install", "start", "stop", "restart", "status", "uninstall")


def _jmunch_home() -> Path:
    return Path.home() / ".jmunch"


def _log_paths() -> tuple[Path, Path]:
    logs = _jmunch_home() / "logs"
    return logs / "gateway.out.log", logs / "gateway.err.log"


def _program_args(config: Path) -> list[str]:
    """Argv that runs the gateway with the interpreter that owns this install."""
    return [sys.executable, "-m", "jmunch_mcp", "gateway", "--config", str(config)]


def _backend() -> str | None:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


# --------------------------------------------------------------------------
# unit-file rendering (pure — unit-tested without touching launchctl/systemctl)
# --------------------------------------------------------------------------

def render_launchd_plist(
    label: str, config: Path, out_log: Path, err_log: Path,
    *, debug_dump: bool = False,
) -> bytes:
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": _program_args(config),
        "EnvironmentVariables": {"JMUNCH_DEBUG_DUMP": "1" if debug_dump else "0"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
        "WorkingDirectory": str(Path.home()),
    })


def _sd_quote(arg: str) -> str:
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def render_systemd_unit(
    config: Path, out_log: Path, err_log: Path, *, debug_dump: bool = False
) -> str:
    exec_start = " ".join(_sd_quote(a) for a in _program_args(config))
    return (
        "[Unit]\n"
        "Description=jmunch gateway — token-saving OpenAI/Anthropic proxy\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"Environment=JMUNCH_DEBUG_DUMP={'1' if debug_dump else '0'}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        f"StandardOutput=append:{out_log}\n"
        f"StandardError=append:{err_log}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _systemd_unit_path(label: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{label}.service"


# --------------------------------------------------------------------------
# launchctl / systemctl wrappers
# --------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["launchctl", *args])


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------

def _install(label: str, config: Path, backend: str, *, debug_dump: bool) -> int:
    out_log, err_log = _log_paths()
    out_log.parent.mkdir(parents=True, exist_ok=True)

    if backend == "launchd":
        plist = _launchd_plist_path(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(
            render_launchd_plist(label, config, out_log, err_log, debug_dump=debug_dump)
        )
        uid = os.getuid()
        _launchctl("bootout", f"gui/{uid}/{label}")  # ignore: may not be loaded
        r = _launchctl("bootstrap", f"gui/{uid}", str(plist))
        if r.returncode != 0:
            print(f"error: launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"installed launchd agent: {plist}")
    else:
        unit = _systemd_unit_path(label)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            render_systemd_unit(config, out_log, err_log, debug_dump=debug_dump),
            encoding="utf-8",
        )
        _systemctl("daemon-reload")
        r = _systemctl("enable", "--now", unit.name)
        if r.returncode != 0:
            print(f"error: systemctl enable failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"installed systemd user unit: {unit}")

    print(f"  config:     {config}")
    print(f"  logs:       {out_log}")
    print(f"  debug dump: {'on' if debug_dump else 'off'} (JMUNCH_DEBUG_DUMP)")
    return 0


def _control(verb: str, label: str, backend: str) -> int:
    if backend == "launchd":
        return _launchd_control(verb, label)
    return _systemd_control(verb, label)


def _launchd_control(verb: str, label: str) -> int:
    plist = _launchd_plist_path(label)
    uid = os.getuid()
    target = f"gui/{uid}/{label}"

    if verb == "status":
        r = _launchctl("list", label)
        if r.returncode != 0:
            print(f"{label}: not loaded")
            return 0
        pid = next(
            (ln.split("=")[1].strip().rstrip(";")
             for ln in r.stdout.splitlines() if ln.strip().startswith('"PID"')),
            None,
        )
        print(f"{label}: running (pid {pid})" if pid else f"{label}: loaded, not running")
        return 0

    if not plist.is_file():
        print(f"error: {label} is not installed (run `jmunch-mcp gateway install`)",
              file=sys.stderr)
        return 1

    if verb == "start":
        r = _launchctl("bootstrap", f"gui/{uid}", str(plist))
    elif verb == "stop":
        r = _launchctl("bootout", target)
    elif verb == "restart":
        r = _launchctl("kickstart", "-k", target)
    elif verb == "uninstall":
        _launchctl("bootout", target)  # ignore: may already be stopped
        plist.unlink(missing_ok=True)
        print(f"removed {plist}")
        return 0
    else:  # pragma: no cover - guarded by caller
        print(f"error: unknown verb {verb}", file=sys.stderr)
        return 2

    if r.returncode != 0:
        print(f"error: launchctl {verb} failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"{label}: {verb} ok")
    return 0


def _systemd_control(verb: str, label: str) -> int:
    unit = _systemd_unit_path(label)
    name = unit.name

    if verb == "status":
        if not unit.is_file():
            print(f"{name}: not installed")
            return 0
        active = _systemctl("is-active", name).stdout.strip() or "unknown"
        pid = _systemctl("show", "--property=MainPID", "--value", name).stdout.strip()
        suffix = f" (pid {pid})" if pid and pid != "0" else ""
        print(f"{name}: {active}{suffix}")
        return 0

    if not unit.is_file():
        print(f"error: {name} is not installed (run `jmunch-mcp gateway install`)",
              file=sys.stderr)
        return 1

    if verb == "uninstall":
        _systemctl("disable", "--now", name)  # ignore: may already be stopped
        unit.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        print(f"removed {unit}")
        return 0

    r = _systemctl(verb, name)
    if r.returncode != 0:
        print(f"error: systemctl {verb} failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"{name}: {verb} ok")
    return 0


def main(verb: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"jmunch-mcp gateway {verb}")
    parser.add_argument("--label", default=DEFAULT_LABEL,
                        help="service label (default: %(default)s)")
    if verb == "install":
        parser.add_argument("--config", required=True, help="path to gateway.toml")
        parser.add_argument(
            "--debug-dump", action="store_true",
            help="set JMUNCH_DEBUG_DUMP=1 in the service environment (default: 0)",
        )
    args = parser.parse_args(argv)

    backend = _backend()
    if backend is None:
        print(
            f"error: gateway service management is not supported on {sys.platform} "
            "(macOS/launchd and Linux/systemd only). Run "
            "`jmunch-mcp gateway --config <toml>` in the foreground instead.",
            file=sys.stderr,
        )
        return 2

    if verb == "install":
        config = Path(args.config).expanduser().resolve()
        if not config.is_file():
            print(f"error: config not found: {config}", file=sys.stderr)
            return 2
        return _install(args.label, config, backend, debug_dump=args.debug_dump)

    return _control(verb, args.label, backend)
