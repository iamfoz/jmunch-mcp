# Hermes-agent helpers

Optional helpers for running the jmunch gateway alongside the Hermes
agent. Not part of the core `jmunch-mcp` package — see [`../README.md`](../README.md).

The Hermes agent reaches jmunch through the **gateway**: point Hermes'
`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` at it. Nothing is installed inside
the Hermes environment — the gateway does all the work. These scripts just
install the gateway itself and keep it current.

## `bootstrap-macos.sh` — first-time setup

One-shot macOS install. It installs jmunch-mcp with
[pipx](https://pipx.pypa.io) — its own isolated venv on a modern Python,
which sidesteps the locked-down macOS system Python — then scaffolds
`~/.jmunch/gateway.toml` and registers the launchd service.

```
contrib/hermes-agent/bootstrap-macos.sh
```

Homebrew must already be installed (the script won't install it — that
needs sudo). On a fresh machine it installs pipx via brew, `pipx install`s
the deploy build, runs `jmunch-mcp gateway init`, and stops so you can
fill in the `[[upstream]]` block of `~/.jmunch/gateway.toml`. Run
`jmunch-mcp gateway install` once you have. Re-running the script with the
config already in place installs/refreshes the service.

## `update-jmunch.sh` — update an existing install

Reinstalls jmunch-mcp from the latest `deploy` build and restarts the
gateway service:

```
contrib/hermes-agent/update-jmunch.sh
```

pipx keeps the app in a venv at a stable path, so the service never breaks
across updates — a safe replacement for ad-hoc `pip install -e /tmp/...`.

## Settings

Both scripts read these environment variables (all optional):

| variable | default | meaning |
|---|---|---|
| `JMUNCH_REPO` | `https://github.com/iamfoz/jmunch-mcp.git` | repository to install from |
| `JMUNCH_BRANCH` | `deploy` | branch to install |
| `JMUNCH_EXTRAS` | `gateway,setup` | optional-deps to install (e.g. `gateway` to skip the Textual wizard) |
| `JMUNCH_LABEL` | `sh.jmunch.gateway` | gateway service label (`update-jmunch.sh` only) |

## Does Hermes need jmunch in its own environment?

**No** — as long as Hermes reaches the gateway over HTTP. The `jmunch_*`
verbs are injected and resolved entirely inside the gateway; Hermes
imports nothing from jmunch.

The only exception is if Hermes spawns `jmunch-mcp` as a stdio **MCP
server** in its own config — a different mode entirely (see the two modes
in [`../../README.md`](../../README.md)). In that case install the **base**
package into Hermes' own environment separately — `pipx install jmunch-mcp`,
no `[gateway]` extra needed.
