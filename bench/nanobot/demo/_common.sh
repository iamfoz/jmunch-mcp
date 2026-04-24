#!/usr/bin/env bash
# Shared setup for left.sh / right.sh. Sourced, not executed directly.
# Exports: HERE, BENCH, PROMPT, PYTHON, SIDE, PORT, GW_TOML, NB_CFG, DB_PATH

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="$(cd "${HERE}/.." && pwd)"

if command -v cygpath >/dev/null 2>&1; then
  BENCH_NATIVE="$(cygpath -m "${BENCH}")"
  HOME_NATIVE="$(cygpath -m "${HOME}")"
else
  BENCH_NATIVE="${BENCH}"
  HOME_NATIVE="${HOME}"
fi

PYTHON="$(command -v python3 || command -v python)"
if [[ -z "${PYTHON}" ]]; then
  echo "error: python not found on PATH" >&2
  exit 2
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "error: ANTHROPIC_API_KEY is not set in this terminal." >&2
  echo "  run:  export ANTHROPIC_API_KEY=sk-ant-..." >&2
  exit 2
fi

# SIDE is set by left.sh / right.sh before sourcing this file.
if [[ "${SIDE}" == "left" ]]; then
  PORT=7879
  SRC_TOML="${BENCH}/gateway-off.toml"
  LABEL="jmunch OFF (baseline)"
else
  PORT=7880
  SRC_TOML="${BENCH}/gateway-on.toml"
  LABEL="jmunch ON (interception)"
fi

# Per-side generated config: own port, own handles DB so the two sides can run
# concurrently without fighting over file locks.
GW_TOML="${BENCH}/demo/gateway-${SIDE}.toml"
HANDLES_DB="${HOME_NATIVE}/.jmunch/handles-${SIDE}.db"
sed -e "s|127.0.0.1:7879|127.0.0.1:${PORT}|" \
    -e "s|~/.jmunch/handles.db|${HANDLES_DB}|" \
    "${SRC_TOML}" > "${GW_TOML}"

NB_CFG="${HOME}/.nanobot/config-${SIDE}.json"
# Always regenerate from the canonical template so edits to
# bench/nanobot/nanobot.config.json (model, MCP servers) propagate.
mkdir -p "${HOME}/.nanobot"
sed "s|127.0.0.1:7879|127.0.0.1:${PORT}|" "${BENCH}/nanobot.config.json" > "${NB_CFG}"

# Fresh workspace per run so nanobot can't answer from a prior session's memory.
WORKSPACE="${HOME_NATIVE}/.nanobot/demo-ws-${SIDE}-$$"
rm -rf "${WORKSPACE}"
mkdir -p "${WORKSPACE}"
cleanup_workspace() { rm -rf "${WORKSPACE}" 2>/dev/null || true; }

# Each side writes to its own metrics DB so the two sides never interfere.
DB_PATH="${BENCH_NATIVE}/.metrics-${SIDE}.db"
export JMUNCH_METRICS_DB="${DB_PATH}"

PROMPT="$(cat "${BENCH}/prompt.txt")"

banner() {
  local msg="$1"
  echo
  echo "=============================================================="
  echo "  ${msg}"
  echo "=============================================================="
}

wait_for_gateway() {
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "gateway never came up on port ${PORT}" >&2
  return 1
}

print_token_report() {
  "${PYTHON}" - <<PYEOF
import os
os.environ['JMUNCH_METRICS_DB'] = r"${DB_PATH}"
from jmunch_mcp import metrics
t = metrics.totals(surface='gateway', include_zero_savings=True)

# Prefer true upstream-side bytes (measured in the gateway-to-Anthropic HTTP
# layer). Fall back to request-side accounting on databases written by
# pre-instrumentation versions of the gateway.
upstream_sent = t.get('upstream_bytes_sent') or 0
upstream_calls = t.get('upstream_calls') or 0
if upstream_sent:
    bytes_to_claude = upstream_sent
    trips = upstream_calls
else:
    raw = t['raw_bytes']; saved = t['saved_bytes']
    bytes_to_claude = max(0, raw - saved)
    trips = t['calls']

tokens = bytes_to_claude // 4
print()
print('==============================================================')
print('  ${LABEL}')
print('==============================================================')
print()
print(f'    round trips to Claude:   {trips:>6,}')
print(f'    tokens to Claude:        {tokens:>6,}')
print()
print('==============================================================')
PYEOF
}
