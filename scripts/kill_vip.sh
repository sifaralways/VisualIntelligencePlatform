#!/usr/bin/env bash
# =============================================================================
# kill_vip.sh — Kill all stale VIP processes after a crash.
#
# Targets:
#   • uvicorn     (backend API, port 7474)
#   • vite / npm run dev   (frontend dev server, port 5173)
#   • exiftool    (may be left running mid-batch if backend died)
#   • python      processes in this project's .venv (pipeline workers)
#
# Usage:
#   ./scripts/kill_vip.sh          # dry-run: shows what would be killed
#   ./scripts/kill_vip.sh --kill   # actually send SIGTERM (then SIGKILL)
# =============================================================================

set -euo pipefail

DRY_RUN=true
if [[ "${1:-}" == "--kill" ]]; then
  DRY_RUN=false
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

# Colours
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
RST='\033[0m'

found=0

kill_pids() {
  local label="$1"; shift
  local pids=("$@")

  if [[ ${#pids[@]} -eq 0 ]]; then
    return
  fi

  for pid in "${pids[@]}"; do
    local cmd
    cmd=$(ps -p "$pid" -o args= 2>/dev/null || echo "(already gone)")

    if $DRY_RUN; then
      echo -e "  ${YLW}[dry-run]${RST} Would kill PID $pid ($label): $cmd"
    else
      echo -e "  ${RED}[kill]${RST}    Sending SIGTERM to PID $pid ($label): $cmd"
      kill -TERM "$pid" 2>/dev/null || true
    fi
    (( found++ )) || true
  done

  if ! $DRY_RUN; then
    sleep 3
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        echo -e "  ${RED}[kill]${RST}    PID $pid still alive — sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  fi
}

collect_pids() {
  # Usage: collect_pids <command...>
  # Runs the command and returns stdout lines as a bash array stored in $REPLY_PIDS
  REPLY_PIDS=()
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] && REPLY_PIDS+=("$line")
  done < <("$@" 2>/dev/null || true)
}

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   VIP — Stale Process Cleanup            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if $DRY_RUN; then
  echo -e "  ${YLW}Dry-run mode${RST} — pass --kill to actually terminate processes"
  echo ""
fi

# ── 1. uvicorn on port 7474 ────────────────────────────────────────────────
collect_pids lsof -ti tcp:7474
kill_pids "uvicorn / backend :7474" "${REPLY_PIDS[@]+"${REPLY_PIDS[@]}"}"

# ── 2. Vite / npm dev server on port 5173 ─────────────────────────────────
collect_pids lsof -ti tcp:5173
kill_pids "vite / frontend :5173" "${REPLY_PIDS[@]+"${REPLY_PIDS[@]}"}"

# ── 3. exiftool processes (may be mid-batch) ───────────────────────────────
collect_pids pgrep -x exiftool
kill_pids "exiftool" "${REPLY_PIDS[@]+"${REPLY_PIDS[@]}"}"

# ── 4. Python workers running from this project's venv ────────────────────
if [[ -d "$VENV_DIR" ]]; then
  collect_pids pgrep -f "$VENV_DIR/bin/python"
  kill_pids "python (.venv)" "${REPLY_PIDS[@]+"${REPLY_PIDS[@]}"}"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
if [[ $found -eq 0 ]]; then
  echo -e "  ${GRN}No stale VIP processes found — nothing to do.${RST}"
else
  if $DRY_RUN; then
    echo -e "  ${YLW}Found $found process(es). Run with --kill to terminate them.${RST}"
  else
    echo -e "  ${GRN}Sent termination signals to $found process(es).${RST}"
  fi
fi
echo ""
