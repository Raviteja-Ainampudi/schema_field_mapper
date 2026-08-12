#!/usr/bin/env bash
# Create or refresh the project venv on Linux / WSL / macOS.
# A Windows .venv cannot be reused here — recreate when switching OS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MIN_MINOR=12
VENV_DIR="${VENV_DIR:-.venv}"

init_pyenv() {
  local pyenv_root="${PYENV_ROOT:-$HOME/.pyenv}"
  if [[ ! -d "$pyenv_root" ]]; then
    return 0
  fi
  export PYENV_ROOT="$pyenv_root"
  export PATH="$PYENV_ROOT/bin:$PATH"
  if command -v pyenv >/dev/null 2>&1; then
    # pyenv shims are normally added in interactive shells only.
    eval "$(pyenv init -)"
  fi
}

pick_python() {
  local candidates=(python3.12 python3)
  for cmd in "${candidates[@]}"; do
    if command -v "$cmd" >/dev/null 2>&1; then
      local ver minor
      ver="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      minor="${ver#*.}"
      if [[ "${ver%%.*}" -eq 3 && "$minor" -ge "$MIN_MINOR" ]]; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

init_pyenv

if ! PYTHON="$(pick_python)"; then
  cat >&2 <<'EOF'
Python 3.12+ not found.

WSL Ubuntu 20.04 (install once):
  sudo apt update
  sudo apt install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt update
  sudo apt install -y python3.12 python3.12-venv python3.12-dev

Ubuntu 22.04+ / Debian (often available directly):
  sudo apt update
  sudo apt install -y python3.12 python3.12-venv

Or install with pyenv (honors .python-version in this repo):
  pyenv install 3.12
  pyenv local 3.12

Then re-run:  bash scripts/setup_venv.sh
EOF
  exit 1
fi

echo "Using $("$PYTHON" --version) ($PYTHON)"

if [[ -d "$VENV_DIR" ]]; then
  if [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
    echo "Removing Windows .venv (not usable on Linux/WSL)..."
    rm -rf "$VENV_DIR"
  elif [[ -x "$VENV_DIR/bin/python" ]]; then
    echo "Refreshing existing Linux/macOS venv..."
    rm -rf "$VENV_DIR"
  else
    echo "Removing incomplete .venv..."
    rm -rf "$VENV_DIR"
  fi
fi

"$PYTHON" -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

echo
echo "Done. Activate with:"
echo "  source $VENV_DIR/bin/activate"
