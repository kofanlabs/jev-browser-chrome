#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if ! command -v uv >/dev/null 2>&1; then
  print -u2 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
  exit 1
fi
uv sync --locked
uv run python scripts/setup_macos.py
