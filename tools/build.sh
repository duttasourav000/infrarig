#!/usr/bin/env bash
#
# Builds <output-dir>/rig: a single-file executable (Python zipapp)
# containing the infrarig compiler. It needs only python3 at runtime and
# can be copied anywhere, e.g. onto your PATH.
#
# Usage: tools/build.sh [output-dir]    (default: output/ in this checkout)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-${ROOT}/output}"
OUT="${OUT_DIR}/rig"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

# Stage just the runtime package (no tests, examples or caches).
mkdir -p "${STAGE}/infrarig"
cp "${ROOT}/__init__.py" "${STAGE}/infrarig/"
cp -R "${ROOT}/dsl" "${STAGE}/infrarig/"
find "${STAGE}" -name "__pycache__" -type d -prune -exec rm -rf {} +

# zipapp's -m flag drops main()'s return value, so supply our own entry
# point to keep the exit code.
cat > "${STAGE}/__main__.py" <<'PY'
from infrarig.dsl.cli import main

raise SystemExit(main())
PY

mkdir -p "${OUT_DIR}"
python3 -m zipapp "${STAGE}" -p "/usr/bin/env python3" -o "${OUT}"
chmod +x "${OUT}"
echo "built ${OUT}"
