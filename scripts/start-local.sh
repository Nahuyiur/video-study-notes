#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
    exec uv run --python 3.12 --with pillow python scripts/video_notes.py serve --open "$@"
fi
exec python3 scripts/video_notes.py serve --open "$@"
