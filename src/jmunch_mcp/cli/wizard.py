"""Interactive setup for the jmunch gateway.

  jmunch-mcp gateway setup            — full first-time setup (Textual TUI)
  jmunch-mcp gateway add-upstream     — add an upstream (Textual modal)
  jmunch-mcp gateway remove-upstream  — remove an upstream by name

Reads/writes ~/.jmunch/gateway.toml and ~/.jmunch/env. Both files stay
hand-editable; the wizard just makes the common cases ergonomic.

The interactive flows live in `cli.wizard_ui` and depend on the optional
[setup] extra (textual). They are imported lazily — if textual is not
installed, only the non-interactive verbs (`remove-upstream`) work, and
the interactive ones print a clear "install textual" hint.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — py3.10
    import tomli as tomllib  # type: ignore[no-redef]

from . import service


_TEXTUAL_MISSING = (
    "error: the setup wizard needs Textual.\n"
    "  install it with:  pipx inject jmunch-mcp textual\n"
    "  or:              pipx install --force 'jmunch-mcp[gateway,setup]'\n"
    "  alternatively, edit ~/.jmunch/gateway.toml and ~/.jmunch/env by hand."
)


# --------------------------------------------------------------------------
# minimal TOML reader / writer for the gateway schema
# --------------------------------------------------------------------------

def _read_gateway_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"gateway": {}, "upstream": [], "interception": {}, "handles": {}}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_gateway_toml(path: Path, data: dict[str, Any]) -> None:
    """Serialize the gateway config dict to TOML. Only the keys jmunch
    understands are written — anything else is dropped (so a re-write
    won't preserve arbitrary user comments or unknown sections)."""
    lines: list[str] = [
        "# jmunch gateway configuration — managed by `gateway setup` /",
        "# `add-upstream` / `remove-upstream`. Hand-editable: run",
        "# `jmunch-mcp gateway install` to apply changes.",
        "",
        "[gateway]",
    ]
    g = data.get("gateway") or {}
    for k in ("listen", "default_upstream", "log_level"):
        if k in g:
            lines.append(f"{k} = {_toml_val(g[k])}")

    for u in data.get("upstream") or []:
        lines.append("")
        lines.append("[[upstream]]")
        for k in ("name", "kind", "base_url", "api_key_env"):
            if k in u and u[k] is not None:
                lines.append(f"{k} = {_toml_val(u[k])}")
        sp = u.get("scrub_params")
        if sp:
            lines.append("scrub_params = [" + ", ".join(_toml_val(p) for p in sp) + "]")

    inter = data.get("interception") or {}
    if inter:
        lines.append("")
        lines.append("[interception]")
        for k in ("threshold_tokens", "inject_tools"):
            if k in inter:
                lines.append(f"{k} = {_toml_val(inter[k])}")

    handles = data.get("handles") or {}
    if handles:
        lines.append("")
        lines.append("[handles]")
        for k in ("ttl_seconds", "store_path", "max_bytes"):
            if k in handles:
                lines.append(f"{k} = {_toml_val(handles[k])}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------

def _setup(*, force: bool) -> int:
    try:
        from .wizard_ui import run_setup
    except ImportError:
        print(_TEXTUAL_MISSING, file=sys.stderr)
        return 2
    return run_setup(force=force)


def _add_upstream() -> int:
    config_path = service._default_config()
    if not config_path.is_file():
        print(f"error: no gateway config at {config_path}", file=sys.stderr)
        print("  run `jmunch-mcp gateway setup` first.", file=sys.stderr)
        return 2
    try:
        from .wizard_ui import run_add_upstream
    except ImportError:
        print(_TEXTUAL_MISSING, file=sys.stderr)
        return 2
    return run_add_upstream()


def _remove_upstream(name: str) -> int:
    config_path = service._default_config()
    if not config_path.is_file():
        print(f"error: no gateway config at {config_path}", file=sys.stderr)
        return 2

    data = _read_gateway_toml(config_path)
    upstreams = data.get("upstream") or []
    remaining = [u for u in upstreams if u.get("name") != name]
    if len(remaining) == len(upstreams):
        names = ", ".join(u.get("name", "?") for u in upstreams) or "(none)"
        print(f"error: upstream '{name}' not found.", file=sys.stderr)
        print(f"  available: {names}", file=sys.stderr)
        return 1
    if not remaining:
        print(f"error: removing '{name}' would leave the gateway with no upstreams.",
              file=sys.stderr)
        return 1

    data["upstream"] = remaining
    g = data.setdefault("gateway", {})
    suffix = ""
    if g.get("default_upstream") == name:
        g["default_upstream"] = remaining[0]["name"]
        suffix = f"  Default upstream updated to '{remaining[0]['name']}'."
    _write_gateway_toml(config_path, data)
    print(f"Removed '{name}' from {config_path}.{suffix}")
    print("\nRestart to apply:  jmunch-mcp gateway restart")
    return 0


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def main(verb: str, argv: list[str]) -> int:
    if verb == "setup":
        parser = argparse.ArgumentParser(prog="jmunch-mcp gateway setup")
        parser.add_argument("--force", action="store_true",
                            help="overwrite an existing config without asking")
        args = parser.parse_args(argv)
        return _setup(force=args.force)

    if verb == "add-upstream":
        argparse.ArgumentParser(prog="jmunch-mcp gateway add-upstream").parse_args(argv)
        return _add_upstream()

    if verb == "remove-upstream":
        parser = argparse.ArgumentParser(prog="jmunch-mcp gateway remove-upstream")
        parser.add_argument("--name", required=True, help="upstream name to remove")
        args = parser.parse_args(argv)
        return _remove_upstream(args.name)

    print(f"error: unknown wizard verb: {verb}", file=sys.stderr)
    return 2
