#!/usr/bin/env bash
# run.sh — automated before/after for the nanobot × jmunch-mcp demo.
#
# Runs the same prompt through nanobot twice:
#   1. jmunch gateway in passthrough mode (gateway-off.toml)
#   2. jmunch gateway in intercept mode  (gateway-on.toml)
#
# Both runs go through the proxy, so latency/plumbing are controlled. Only
# the content-aware interception differs. Emits a CSV with token totals and
# a one-line "saved %" summary suitable for a video title card.
#
# Preconditions:
#   - pip install 'jmunch-mcp[gateway,exact-tokens]' nanobot-ai
#   - uv / uvx installed (for `mcp-server-fetch`)
#   - ANTHROPIC_API_KEY exported
#   - bench/nanobot/nanobot.config.json copied to ~/.nanobot/config.json
#
# Usage: ./run.sh

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# On Git Bash / MSYS, `pwd` returns /c/... which Python on Windows can't open.
# Translate to native form for paths that Python reads.
if command -v cygpath >/dev/null 2>&1; then
  HERE_NATIVE="$(cygpath -m "${HERE}")"
else
  HERE_NATIVE="${HERE}"
fi
METRICS_DB="${HERE_NATIVE}/.metrics.db"
CSV="${HERE}/results.csv"
PROMPT="$(cat "${HERE}/prompt.txt")"

# `python3` on Linux/Mac; `python` on stock Windows + Git Bash.
PYTHON="$(command -v python3 || command -v python)"
if [[ -z "${PYTHON}" ]]; then
  echo "error: neither python3 nor python found on PATH." >&2
  exit 2
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "error: ANTHROPIC_API_KEY not set." >&2
  echo "  This demo hits Anthropic's OpenAI-compatible endpoint directly;" >&2
  echo "  ~\$0.02 of Claude API credit covers both runs." >&2
  exit 2
fi

# Isolate this bench from the user's real dashboard DB.
export JMUNCH_METRICS_DB="${METRICS_DB}"
rm -f "${METRICS_DB}"
rm -f ~/.jmunch/handles.db       # fair-start on handles

_wait_ready() {
  for _ in $(seq 1 20); do
    if curl -sf http://127.0.0.1:7879/health >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  echo "gateway never came up on 127.0.0.1:7879" >&2
  return 1
}

_run_one() {
  local label="$1"; local config="$2"
  echo
  echo "=============================================================="
  echo "Run: ${label}  (config: $(basename "${config}"))"
  echo "=============================================================="
  jmunch-mcp gateway --config "${config}" >"${HERE}/gateway-${label}.log" 2>&1 &
  local gw=$!
  trap "kill ${gw} 2>/dev/null || true" EXIT
  _wait_ready
  echo "--- nanobot turn ---"
  nanobot agent --message "${PROMPT}" --no-logs || true
  echo "--- end turn ---"
  sleep 3    # let the metrics row flush + WAL checkpoint
  kill "${gw}" 2>/dev/null || true
  wait "${gw}" 2>/dev/null || true
  sleep 1
  trap - EXIT
}

_run_one "off" "${HERE}/gateway-off.toml"
OFF_METRICS="$("${PYTHON}" -c "
import os; os.environ['JMUNCH_METRICS_DB']='${METRICS_DB}'
from jmunch_mcp import metrics
t = metrics.totals(surface='gateway', include_zero_savings=True)
print(f'{t[\"raw_bytes\"]},{t[\"response_bytes\"]},{t[\"saved_bytes\"]},{t[\"tokens_saved\"]},{t[\"tokens_saved_exact\"]}')
")"

# Reset for the second run so we measure it in isolation.
rm -f "${METRICS_DB}"
rm -f ~/.jmunch/handles.db

_run_one "on" "${HERE}/gateway-on.toml"
ON_METRICS="$("${PYTHON}" -c "
import os; os.environ['JMUNCH_METRICS_DB']='${METRICS_DB}'
from jmunch_mcp import metrics
t = metrics.totals(surface='gateway', include_zero_savings=True)
print(f'{t[\"raw_bytes\"]},{t[\"response_bytes\"]},{t[\"saved_bytes\"]},{t[\"tokens_saved\"]},{t[\"tokens_saved_exact\"]}')
")"

IFS=',' read OFF_RAW OFF_SENT OFF_SAVED OFF_TKSAVED OFF_TKEXACT <<< "${OFF_METRICS}"
IFS=',' read ON_RAW  ON_SENT  ON_SAVED  ON_TKSAVED  ON_TKEXACT  <<< "${ON_METRICS}"

# Headline metric: compare the total prompt tokens nanobot sent to the
# upstream between the two runs. That's the cost axis customers care about.
OFF_PROMPT_TOKENS=$("${PYTHON}" -c "print((${OFF_RAW}) // 4)")
ON_PROMPT_TOKENS=$("${PYTHON}" -c "print((${ON_SENT}) // 4)")
if [[ "${OFF_PROMPT_TOKENS}" -gt 0 ]]; then
  PCT=$("${PYTHON}" -c "print(f'{(1 - ${ON_PROMPT_TOKENS}/${OFF_PROMPT_TOKENS})*100:.1f}')")
else
  PCT="?"
fi

{
  echo "run,raw_bytes,sent_bytes,saved_bytes,tokens_saved_approx,tokens_saved_exact"
  echo "off,${OFF_RAW},${OFF_SENT},${OFF_SAVED},${OFF_TKSAVED},${OFF_TKEXACT}"
  echo "on,${ON_RAW},${ON_SENT},${ON_SAVED},${ON_TKSAVED},${ON_TKEXACT}"
} > "${CSV}"

echo
echo "=============================================================="
echo "Results → ${CSV}"
echo "--------------------------------------------------------------"
printf "  jmunch OFF : %'d prompt tokens sent to upstream\n" "${OFF_PROMPT_TOKENS}"
printf "  jmunch ON  : %'d prompt tokens sent to upstream\n" "${ON_PROMPT_TOKENS}"
echo   "  saved       : ${PCT}%"
echo "=============================================================="
