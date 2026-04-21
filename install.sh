#!/usr/bin/env bash
# install.sh — set up tt-model-runner-gui dependencies
#
# Creates ~/.tenstorrent-venv with --system-site-packages so that the system
# PyGObject (python3-gi) is visible inside the venv.  PyGObject cannot be
# reliably pip-installed without system dev headers; the system package is the
# most reliable source.
#
# Usage:
#   ./install.sh          — install into ~/.tenstorrent-venv
#   VENV=~/my-venv ./install.sh  — install into a custom path
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-${HOME}/.tenstorrent-venv}"
REQ="${SCRIPT_DIR}/requirements.txt"

echo "==> Checking system Python…"
SYSPY=$(command -v python3 2>/dev/null || true)
if [[ -z "${SYSPY}" ]]; then
    echo "ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi
PY_VER=$("${SYSPY}" -c "import sys; print(sys.version_info[:2])")
echo "    Found: ${SYSPY}  (${PY_VER})"

echo "==> Checking for python3-gi (PyGObject / GTK4 bindings)…"
# Must use the system Python — not a pyenv shim — because python3-gi is a
# system package whose .so file links against the system Python ABI.
if ! "${SYSPY}" -c "import gi" 2>/dev/null; then
    echo ""
    echo "ERROR: python3-gi not found on the system Python."
    echo "Install it with your package manager, then re-run this script:"
    echo ""
    echo "  Ubuntu / Debian:"
    echo "    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0"
    echo ""
    echo "  Fedora / RHEL:"
    echo "    sudo dnf install python3-gobject gtk4"
    echo ""
    echo "  Arch Linux:"
    echo "    sudo pacman -S python-gobject gtk4"
    echo ""
    exit 1
fi
echo "    python3-gi found ✓"

echo "==> Creating venv at ${VENV}…"
# --system-site-packages makes the system gi package visible inside the venv.
"${SYSPY}" -m venv "${VENV}" --system-site-packages

echo "==> Installing pip dependencies…"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${REQ}"

echo ""
echo "Done!  Run the app with:"
echo "    ./run           — GTK4 GUI"
echo "    ./run --tui     — Textual TUI"
echo ""
echo "The venv is at: ${VENV}"
echo "To update dependencies later: ${VENV}/bin/pip install -r requirements.txt"
