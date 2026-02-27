#!/usr/bin/env bash
# =============================================================================
# VIP — Visual Intelligence Platform
# One-command setup for Apple Silicon macOS
# =============================================================================
set -euo pipefail

PYTHON="${PYTHON:-python3.11}"
VENV_DIR=".venv"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   VIP — Visual Intelligence Platform     ║"
echo "║   Setup — Apple Silicon macOS            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------------------
# 0. Preflight checks
# ---------------------------------------------------------------------------
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "❌  This application requires Apple Silicon (arm64). Aborting."
  exit 1
fi

if ! command -v brew &>/dev/null; then
  echo "❌  Homebrew not found. Install it from https://brew.sh then re-run."
  exit 1
fi

if ! $PYTHON --version &>/dev/null; then
  echo "❌  $PYTHON not found. Install via: brew install python@3.11"
  exit 1
fi

if ! command -v node &>/dev/null; then
  echo "❌  Node.js not found. Install via: brew install node"
  exit 1
fi

echo "✅  Platform checks passed (Apple Silicon, Homebrew, Python, Node)"
echo ""

# ---------------------------------------------------------------------------
# 1. System dependencies via Homebrew
# ---------------------------------------------------------------------------
echo "── Step 1/5: Installing system dependencies via Homebrew ──"
brew install exiftool ffmpeg 2>&1 | grep -E "(Installing|Pouring|already installed|Error)" || true
echo "✅  exiftool + ffmpeg ready"
echo ""

# ---------------------------------------------------------------------------
# 2. Python virtual environment
# ---------------------------------------------------------------------------
echo "── Step 2/5: Creating Python virtual environment ──"
if [[ ! -d "$VENV_DIR" ]]; then
  $PYTHON -m venv "$VENV_DIR"
  echo "✅  Created .venv"
else
  echo "ℹ️   .venv already exists, skipping creation"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "── Step 3/5: Installing Python packages ──"
pip install --upgrade pip --quiet

pip install \
  mlx \
  insightface \
  fastapi \
  "uvicorn[standard]" \
  aiosqlite \
  faiss-cpu \
  hdbscan \
  scikit-learn \
  rawpy \
  Pillow \
  numpy \
  pydantic \
  "pydantic-settings" \
  python-multipart \
  httpx \
  --quiet

echo "✅  Python packages installed"
echo ""

# ---------------------------------------------------------------------------
# 3. Download InsightFace Buffalo_L model weights (one-time, ~300MB)
# ---------------------------------------------------------------------------
echo "── Step 4/5: Downloading InsightFace Buffalo_L model ──"
python - <<'EOF'
import sys
try:
    import insightface
    from insightface.app import FaceAnalysis
    print("  Preparing InsightFace Buffalo_L (download on first run)...")
    app = FaceAnalysis(name='buffalo_l', providers=['CoreMLExecutionProvider', 'CPUExecutionProvider'])
    app.prepare(ctx_id=0, det_size=(640, 640))
    print("  ✅  Buffalo_L model ready")
except Exception as e:
    print(f"  ⚠️   InsightFace init warning: {e}")
    print("  Model will be downloaded on first pipeline run.")
EOF
echo ""

# ---------------------------------------------------------------------------
# 4. Frontend dependencies
# ---------------------------------------------------------------------------
echo "── Step 5/5: Installing frontend dependencies ──"
if [[ -d "frontend" ]]; then
  (cd frontend && npm install --silent)
  echo "✅  Frontend dependencies installed"
else
  echo "⚠️   frontend/ directory not found — skipping npm install"
fi
echo ""

# ---------------------------------------------------------------------------
# 5. Initialise application support directory + database
# ---------------------------------------------------------------------------
echo "── Initialising database ──"
python -m backend.database.db --init
echo ""

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo "╔══════════════════════════════════════════╗"
echo "║   ✅  Setup complete!                    ║"
echo "║                                          ║"
echo "║   Start the app:  ./start.sh             ║"
echo "║   Open browser:   http://localhost:5173  ║"
echo "╚══════════════════════════════════════════╝"
echo ""
