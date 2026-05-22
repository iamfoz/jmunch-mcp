#!/usr/bin/env bash
#
# One-shot macOS setup for the jmunch gateway.
#
# Installs jmunch-mcp (the deploy build) with pipx, scaffolds a gateway
# config, and — once the config is filled in — registers the launchd
# service. Safe to re-run.
#
# Settings are environment variables with sane defaults — see README.md.

set -euo pipefail

JMUNCH_REPO="${JMUNCH_REPO:-https://github.com/iamfoz/jmunch-mcp.git}"
JMUNCH_BRANCH="${JMUNCH_BRANCH:-deploy}"
CONFIG="$HOME/.jmunch/gateway.toml"

# 1. Homebrew — required. We don't install it for you (it needs sudo).
if ! command -v brew >/dev/null; then
  echo "error: Homebrew not found. Install it from https://brew.sh, then re-run." >&2
  exit 1
fi

# 2. pipx — the standard, locked-down-Python-safe way to install a CLI app.
if ! command -v pipx >/dev/null; then
  echo "==> installing pipx"
  brew install pipx
fi
pipx ensurepath >/dev/null

# 3. jmunch-mcp (deploy build) into its own isolated pipx venv.
echo "==> installing jmunch-mcp from '$JMUNCH_BRANCH'"
pipx install --force "jmunch-mcp[gateway] @ git+${JMUNCH_REPO}@${JMUNCH_BRANCH}"

# pipx drops the command in ~/.local/bin — call it by path in case this
# shell has not yet picked up the PATH change `pipx ensurepath` just made.
JMUNCH="$HOME/.local/bin/jmunch-mcp"

# 4. Config, then service.
if [ -f "$CONFIG" ]; then
  echo "==> config present ($CONFIG) — installing the gateway service"
  "$JMUNCH" gateway install
  echo ""
  echo "════ done ════"
  "$JMUNCH" gateway status
else
  "$JMUNCH" gateway init
  echo ""
  echo "════ almost done ════"
  echo "  1. Edit $CONFIG — set the [[upstream]] block for your provider."
  echo "  2. Run:  jmunch-mcp gateway install"
fi

echo ""
echo "  Keep it current later with: contrib/hermes-agent/update-jmunch.sh"
