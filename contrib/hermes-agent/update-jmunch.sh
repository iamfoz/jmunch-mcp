#!/usr/bin/env bash
#
# Update the jmunch gateway to the latest `deploy` branch and restart it.
#
# A safe replacement for ad-hoc `pip install -e /tmp/...` flows: the
# checkout lives at a stable path, so the launchd / systemd gateway
# service never breaks when /tmp is purged.
#
# All settings are environment variables with sane defaults — see the
# README.md in this directory.

set -euo pipefail

JMUNCH_REPO="${JMUNCH_REPO:-https://github.com/iamfoz/jmunch-mcp.git}"
JMUNCH_BRANCH="${JMUNCH_BRANCH:-deploy}"
JMUNCH_SRC="${JMUNCH_SRC:-$HOME/.jmunch/src/jmunch-mcp}"
JMUNCH_VENV="${JMUNCH_VENV:-$HOME/.jmunch/venv}"
JMUNCH_LABEL="${JMUNCH_LABEL:-sh.jmunch.gateway}"

echo "==> source: $JMUNCH_SRC  (branch: $JMUNCH_BRANCH)"
if [ -d "$JMUNCH_SRC/.git" ]; then
  git -C "$JMUNCH_SRC" fetch --quiet origin "$JMUNCH_BRANCH"
  # `deploy` is force-rebuilt every release — point the mirror at origin
  # rather than `pull`, which would fail on the non-fast-forward update.
  git -C "$JMUNCH_SRC" checkout --quiet -B "$JMUNCH_BRANCH" "origin/$JMUNCH_BRANCH"
else
  mkdir -p "$(dirname "$JMUNCH_SRC")"
  git clone --quiet --branch "$JMUNCH_BRANCH" "$JMUNCH_REPO" "$JMUNCH_SRC"
fi

if [ ! -x "$JMUNCH_VENV/bin/pip" ]; then
  echo "error: no venv at $JMUNCH_VENV" >&2
  echo "  create one first: python3 -m venv $JMUNCH_VENV" >&2
  exit 1
fi

echo "==> installing into gateway venv: $JMUNCH_VENV"
"$JMUNCH_VENV/bin/pip" install --quiet --editable "${JMUNCH_SRC}[gateway]"

# The Hermes agent only needs the jmunch package if it spawns jmunch-mcp
# as a stdio MCP server. If it does, set HERMES_VENV — base package only;
# the [gateway] extra (aiohttp) is for the HTTP server, not needed there.
if [ -n "${HERMES_VENV:-}" ]; then
  echo "==> installing base jmunch-mcp into Hermes venv: $HERMES_VENV"
  "$HERMES_VENV/bin/pip" install --quiet --editable "$JMUNCH_SRC"
fi

echo "==> restarting gateway service ($JMUNCH_LABEL)"
if ! "$JMUNCH_VENV/bin/jmunch-mcp" gateway restart --label "$JMUNCH_LABEL"; then
  echo "note: the gateway service is not installed yet — install it once:" >&2
  echo "  $JMUNCH_VENV/bin/jmunch-mcp gateway install --config <gateway.toml>" >&2
  exit 1
fi

"$JMUNCH_VENV/bin/jmunch-mcp" gateway status --label "$JMUNCH_LABEL"
echo "==> done"
