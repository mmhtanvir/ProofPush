#!/usr/bin/env bash
# ProofPush One-Command Installer
# Usage: git clone https://github.com/mmhtanvir/ProofPush.git && cd ProofPush && ./install.sh

set -e

echo "=================================================="
echo "          Installing ProofPush Auditor"
echo "=================================================="

# Check Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required but not found in PATH." >&2
    exit 1
fi

echo "1. Installing Python package and dependencies..."
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install -e .

echo "2. Installing Playwright browser dependencies (Chromium)..."
python3 -m playwright install chromium

echo "3. Running diagnostics..."
proofpush doctor

echo ""
echo "=================================================="
echo "  ProofPush installation completed successfully!  "
echo "=================================================="
echo ""
echo "To enable ProofPush in your Git repository, run:"
echo "  cd /path/to/your/project"
echo "  proofpush install"
echo ""
