#!/usr/bin/env bash
# Run the banana-import test suite inside the running container (which already
# has pandas/pdfplumber + the baked tests). Usage: bash tests/run.sh [-q|-k ...]
set -e
C="${BANANA_IMPORT_CONTAINER:-banana-import-banana-import-1}"
docker exec "$C" pip install -q pytest==8.3.4 >/dev/null 2>&1 || true
docker exec -w /app "$C" python -m pytest tests "$@"
