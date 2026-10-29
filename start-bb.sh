#!/bin/sh
# Launch from any working directory, preferring this checkout's environment.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python bb.py "$@"
fi
exec python3 bb.py "$@"
