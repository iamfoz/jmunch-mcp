#!/usr/bin/env bash
#
# Update the jmunch gateway to the latest `deploy` build and restart it.
#
# Reinstalls jmunch-mcp with pipx straight from the deploy branch, then
# restarts the gateway service. pipx keeps the app in its own isolated
# venv at a stable path, so the launchd / systemd service never breaks
# across updates — no more ad-hoc `pip install -e /tmp/...`.
#
# Settings are environment variables with sane defaults — see README.md.

set -euo pipefail

JMUNCH_REPO="${JMUNCH_REPO:-https://github.com/iamfoz/jmunch-mcp.git}"
JMUNCH_BRANCH="${JMUNCH_BRANCH:-deploy}"
JMUNCH_LABEL="${JMUNCH_LABEL:-sh.jmunch.gateway}"

if ! command -v pipx >/dev/null; then
  echo "error: pipx not found." >&2
  echo "  install it with: brew install pipx && pipx ensurepath" >&2
  echo "  (or run bootstrap-macos.sh in this directory for first-time setup)" >&2
  exit 1
fi

echo "==> reinstalling jmunch-mcp from '$JMUNCH_BRANCH'"
pipx install --force "jmunch-mcp[gateway] @ git+${JMUNCH_REPO}@${JMUNCH_BRANCH}"

echo "==> restarting gateway service ($JMUNCH_LABEL)"
if ! jmunch-mcp gateway restart --label "$JMUNCH_LABEL"; then
  echo "note: the gateway service is not installed yet — set it up once:" >&2
  echo "  jmunch-mcp gateway init       # then edit ~/.jmunch/gateway.toml" >&2
  echo "  jmunch-mcp gateway install" >&2
  exit 1
fi

jmunch-mcp gateway status --label "$JMUNCH_LABEL"
echo "==> done"
