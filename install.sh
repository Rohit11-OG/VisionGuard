#!/usr/bin/env bash
# VisionGuard — one-line installer for Linux / macOS
# Usage: curl -sSL https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.sh | bash
set -e

REPO="https://github.com/Rohit11-OG/VisionGuard.git"

echo "[visionguard] Installing from GitHub..."
pip install --upgrade "visionguard[full] @ git+${REPO}" 2>/dev/null \
    || pip install "git+${REPO}[full]" 2>/dev/null \
    || pip install "git+${REPO}"

echo ""
echo "[visionguard] Install complete."
echo ""
echo "  Usage (run from your CV project directory):"
echo "    visionguard bootstrap   # one-time setup"
echo "    visionguard scan        # scan for bugs now"
echo "    visionguard watch       # auto-scan on every file save"
echo "    visionguard report      # list recent reports"
