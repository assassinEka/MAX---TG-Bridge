#!/usr/bin/env bash
# Запуск моста (Linux/macOS). Windows: python tools\console.py
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -u tools/console.py