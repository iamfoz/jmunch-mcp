"""CLI entrypoint.

Default (no subcommand): run the proxy.
    jmunch-mcp --config path/to/config.toml [--report]

Subcommands:
    jmunch-mcp init  [...]            Scan + generate wrapper configs.
    jmunch-mcp dashboard [...]        Local metrics web UI.
    jmunch-mcp gateway --config ...   Run the HTTP gateway (foreground).
    jmunch-mcp gateway init           Write a starter ~/.jmunch/gateway.toml.
    jmunch-mcp gateway install|start|stop|restart|status|uninstall
                                      Manage the gateway as a background
                                      service (launchd on macOS, systemd
                                      user unit on Linux).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load
from .proxy import Proxy


def _run_serve(args: argparse.Namespace) -> int:
    config = load(args.config)
    level = args.log_level or config.log_level
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    want_report = args.report or config.report
    upstream_name = Path(args.config).stem or "upstream"
    proxy = Proxy(config, upstream_name=upstream_name)
    rc = 0
    try:
        rc = asyncio.run(proxy.run())
    except KeyboardInterrupt:
        rc = 130
    finally:
        if want_report:
            proxy.stats.finalize(proxy.tracker.total)
            print(proxy.stats.render(), file=sys.stderr, flush=True)
    return rc


def _run_gateway(argv: list[str]) -> int:
    from .cli.service import GATEWAY_VERBS

    if argv and argv[0] in GATEWAY_VERBS:
        from .cli.service import main as service_main
        return service_main(argv[0], argv[1:])

    parser = argparse.ArgumentParser(prog="jmunch-mcp gateway")
    parser.add_argument("--config", required=True, help="Path to gateway.toml")
    parser.add_argument("--log-level", default=None, help="Override config log_level")
    args = parser.parse_args(argv)

    from .gateway.config import load as load_gateway
    from .gateway.server import serve

    config = load_gateway(args.config)
    level = args.log_level or config.log_level
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return serve(config)


def main() -> int:
    # Route `init` / `dashboard` / `gateway` subcommands; default is the proxy.
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from .cli.init import main as init_main
        return init_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
        from .cli.dashboard import main as dashboard_main
        return dashboard_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "gateway":
        return _run_gateway(sys.argv[2:])

    parser = argparse.ArgumentParser(prog="jmunch-mcp")
    parser.add_argument("--config", required=True, help="Path to config.toml / config.json")
    parser.add_argument("--log-level", default=None, help="Override config log_level")
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print session summary to stderr on shutdown",
    )
    args = parser.parse_args()
    return _run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
