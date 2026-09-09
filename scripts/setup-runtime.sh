#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
uv sync --locked
uv run python scripts/probe-vane.py
