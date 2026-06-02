#!/usr/bin/env bash
# =============================================================================
# VIP — Start backend + frontend
# =============================================================================
set -euo pipefail

VENV_DIR=".venv"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "❌  Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

# Activate venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Suppress HuggingFace tokenizers fork warning under uvicorn --reload.
export TOKENIZERS_PARALLELISM=false

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   VIP — Starting                         ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  API   → http://localhost:7474"
echo "  UI    → http://localhost:5173"
echo "  Docs  → http://localhost:7474/docs"
echo ""
echo "  Press Ctrl+C to stop both processes."
echo ""

# Start backend in background, capture PID
uvicorn backend.main:app --host 127.0.0.1 --port 7474 --reload &
BACKEND_PID=$!

# Start frontend in background
(cd frontend && npm run dev) &
FRONTEND_PID=$!

# Trap Ctrl+C / termination — kill both
cleanup() {
  echo ""
  echo "Stopping VIP..."
  kill "$BACKEND_PID" 2>/dev/null || true
  kill "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  wait "$FRONTEND_PID" 2>/dev/null || true
  echo "Stopped."
}
trap cleanup INT TERM

# Wait for either process to exit
wait -n "$BACKEND_PID" "$FRONTEND_PID"
EXIT_CODE=$?
cleanup
exit "$EXIT_CODE"
