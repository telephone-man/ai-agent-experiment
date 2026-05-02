#!/bin/bash
set -euo pipefail

# Optional env/args:
#   WATCH=1            Loop forever (Ctrl+C to stop)
#   INTERVAL=1         Seconds between loops
#   QUIET_WHEN_EMPTY=1 Suppress "no sessions" noise (default on)
#   Pass --watch as first arg to enable watch mode.

WATCH=${WATCH:-0}
if [ "${1:-}" = "--watch" ]; then
  WATCH=1
  shift
fi
INTERVAL=${INTERVAL:-1}
QUIET_WHEN_EMPTY=${QUIET_WHEN_EMPTY:-1}

dump_sessions() {
  local call_ids
  call_ids=$(rtpengine-ctl list sessions all 2>/dev/null | awk '/^ID:/ {print $2}')

  if [ -z "${call_ids}" ]; then
    [ "${QUIET_WHEN_EMPTY}" = "1" ] && return 1
    echo "No active RTPengine sessions."
    return 1
  fi

  echo "=== $(date -Is) ==="
  for callid in ${call_ids}; do
    echo "Fetching info for call ID: ${callid}"
    rtpengine-ctl call "${callid}" info
  done
  echo
}

if [ "${WATCH}" = "1" ]; then
  while :; do
    dump_sessions || true
    sleep "${INTERVAL}"
  done
else
  dump_sessions
fi
