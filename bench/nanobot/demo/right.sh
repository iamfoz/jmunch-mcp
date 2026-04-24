#!/usr/bin/env bash
# RIGHT terminal — jmunch ON (interception).
# Starts the gateway in intercept mode, runs nanobot, prints token totals.
SIDE="right"
source "$(dirname "$0")/_common.sh"

# Clean previous run's metrics + handles so counts start at zero.
rm -f "${DB_PATH}" "${DB_PATH}-wal" "${DB_PATH}-shm"
rm -f "${HANDLES_DB}" "${HANDLES_DB}-wal" "${HANDLES_DB}-shm" 2>/dev/null || true

banner "RIGHT terminal — ${LABEL}  (port ${PORT})"
echo "  Starting gateway (intercept + verb injection)..."
jmunch-mcp gateway --config "${GW_TOML}" >"${HERE}/right.gateway.log" 2>&1 &
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
