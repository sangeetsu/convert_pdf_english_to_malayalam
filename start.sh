#!/usr/bin/env bash
# ============================================================
#  English to Malayalam PDF Translator — Ubuntu/Linux launcher
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo ""
echo " ============================================================"
echo "  English to Malayalam PDF Translator"
echo " ============================================================"
echo ""

# ── 1. Check for Python 3 ─────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo " ERROR: python3 is not installed."
    echo ""
    echo " Please install it by running:"
    echo "   sudo apt update && sudo apt install -y python3 python3-pip python3-venv"
    echo ""
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo " Found Python $PYTHON_VERSION"

# ── 2. Create a virtual environment (first run only) ──────────
#  This avoids the "externally managed environment" error on
#  Ubuntu 23.04+ where system pip refuses to install packages
#  outside a venv.
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    # Remove any broken/incomplete venv directory before recreating
    [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
    echo " Creating virtual environment (first run only)…"
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        echo ""
        echo " ERROR: Could not create a virtual environment."
        echo " Please install python3-venv and try again:"
        echo "   sudo apt update && sudo apt install -y python3-venv"
        echo ""
        exit 1
    fi
fi

# ── 3. Activate the virtual environment ───────────────────────
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ── 4. Install / update dependencies ─────────────────────────
echo " Installing required packages (first run may take a minute)…"
if ! pip install -q -r "$SCRIPT_DIR/requirements.txt"; then
    echo ""
    echo " ERROR: Could not install packages."
    echo " Please make sure you have an internet connection and try again."
    echo ""
    exit 1
fi

# ── 5. Start the app ──────────────────────────────────────────
echo ""
echo " Starting the translator app…"
echo ""
echo " ============================================================"
echo "  Open your web browser and go to:"
echo ""
echo "      http://localhost:5000"
echo ""
echo "  Keep this terminal open while you are using the translator."
echo "  Press Ctrl+C to stop the app."
echo " ============================================================"
echo ""

cd "$SCRIPT_DIR"
python app.py
