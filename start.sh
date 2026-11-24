#!/bin/sh
set -eu
cd -- "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 main.py
fi
exec python3 main.py
