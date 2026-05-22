# Hermes-agent helpers

Optional helpers for running the jmunch gateway alongside the Hermes
agent. Not part of the core `jmunch-mcp` package — see [`../README.md`](../README.md).

## `update-jmunch.sh`

Updates the gateway to the latest `deploy` branch and restarts the
service. A safe replacement for ad-hoc `pip install -e /tmp/...`: the
checkout lives at a **stable** path (`~/.jmunch/src/jmunch-mcp` by
default), so the launchd / systemd gateway service never breaks when
`/tmp` is purged.

```
contrib/hermes-agent/update-jmunch.sh
```

Configure with environment variables (all optional):

| variable | default | meaning |
|---|---|---|
| `JMUNCH_REPO` | `https://github.com/iamfoz/jmunch-mcp.git` | repository to pull |
| `JMUNCH_BRANCH` | `deploy` | branch to track |
| `JMUNCH_SRC` | `~/.jmunch/src/jmunch-mcp` | stable checkout path |
| `JMUNCH_VENV` | `~/.jmunch/venv` | gateway venv |
| `JMUNCH_LABEL` | `sh.jmunch.gateway` | gateway service label |
| `HERMES_VENV` | _(unset)_ | if set, also install the **base** package here |

## Does Hermes need jmunch in its own venv?

Usually **no**. If Hermes reaches the gateway over HTTP — `OPENAI_BASE_URL`
/ `ANTHROPIC_BASE_URL` pointed at it — it imports nothing from jmunch. The
`jmunch_*` verbs are injected and resolved entirely inside the gateway, so
the gateway venv is the only one that needs the package.

The **only** case where Hermes needs it installed is if Hermes spawns
`jmunch-mcp` as a stdio **MCP server** in its own config. If so, set
`HERMES_VENV` — the script installs the **base** package only. The
`[gateway]` extra is just `aiohttp` for the HTTP server and is never
needed for the stdio MCP proxy.

## First-time setup

`update-jmunch.sh` expects the gateway venv and the service to exist
already. Bootstrap them once:

```
python3 -m venv ~/.jmunch/venv
JMUNCH_SRC=~/.jmunch/src/jmunch-mcp ./update-jmunch.sh   # clones + installs; restart will warn
~/.jmunch/venv/bin/jmunch-mcp gateway install --config ~/.jmunch/gateway.toml
```

After that, re-run `update-jmunch.sh` whenever you want the running
gateway moved to the latest `deploy`.
