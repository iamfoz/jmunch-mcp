#!/usr/bin/env bash
# LEFT terminal — jmunch OFF (baseline).
# Starts the gateway in passthrough mode, runs nanobot, prints token totals.
SIDE="left"
source "$(dirname "$0")/_common.sh"

# Clean previous run's metrics so counts start at zero.
rm -f "${DB_PATH}" "${DB_PATH}-wal" "${DB_PATH}-shm"

banner "LEFT terminal — ${LABEL}  (port ${PORT})"
echo "  Starting gateway (passthrough)..."
jmunch-mcp gateway --config "${GW_TOML}" >"${HERE}/left.gateway.log" 2>&1 &
GW=$!
trap 'kill ${GW} 2>/dev/null || true' EXIT
wait_for_gateway
echo "  Gateway up. Running nanobot with the demo prompt..."
echo

nanobot agent --config "${NB_CFG}" --workspace "${WORKSPACE}" \
  --session "demo-$$" \
  --message "${PROMPT}" --no-logs --no-markdown || true

sleep 2
kill "${GW}" 2>/dev/null || true
wait "${GW}" 2>/dev/null || true
sleep 1
cleanup_workspace
trap - EXIT

print_token_report
