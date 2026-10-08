#!/usr/bin/env bash
# Install the `t4` command. macOS and Linux.
#
#   ./install.sh                 # install into ./.venv and link onto PATH
#   ./install.sh --prefix ~/.local
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${HOME}/.local"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)'; then
      PY="$candidate"; break
    fi
  fi
done
[ -n "$PY" ] || { echo "Python 3.8+ is required but was not found." >&2; exit 1; }

echo "Using $($PY --version)"
"$PY" -m venv "$HERE/.venv"
VENV_PY="$HERE/.venv/bin/python"

# Editable installs from pyproject.toml need pip >= 21.3 (PEP 660). Many system
# Pythons ship something older, so try to upgrade -- but do not fail the
# install if there is no network, because a plain install works regardless.
"$VENV_PY" -m pip install --quiet --upgrade pip setuptools >/dev/null 2>&1 || \
  echo "note: could not upgrade pip (offline?); continuing"

if ! "$VENV_PY" -m pip install --quiet -e "$HERE" 2>/dev/null; then
  echo "note: editable install unavailable; installing a normal copy."
  echo "      re-run ./install.sh after pulling changes."
  "$VENV_PY" -m pip install --quiet "$HERE"
fi

mkdir -p "$PREFIX/bin"
ln -sf "$HERE/.venv/bin/t4" "$PREFIX/bin/t4"

echo
echo "Installed: $PREFIX/bin/t4"
if ! command -v t4 >/dev/null 2>&1; then
  echo
  echo "$PREFIX/bin is not on your PATH. Add it:"
  echo "  echo 'export PATH=\"$PREFIX/bin:\$PATH\"' >> ~/.zshrc   # or ~/.bashrc"
  echo "  exec \$SHELL -l"
fi
echo
echo "Then, in your project directory:"
echo "  t4 init"
