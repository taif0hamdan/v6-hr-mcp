#!/usr/bin/env bash
# Run this on a machine WITH internet access to pre-fetch every dependency
# (and its transitive dependencies) as wheels, so the Docker image can later
# be built on an air-gapped host with a plain:
#   docker build .
# The Dockerfile auto-detects a non-empty ./wheels/ directory and installs
# from it with --no-index instead of reaching out to PyPI.
#
# Usage:
#   ./scripts/download_wheels.sh [path-to-requirements.txt]

set -euo pipefail

REQUIREMENTS_FILE="${1:-requirements.txt}"
WHEELS_DIR="wheels"

if [ ! -f "$REQUIREMENTS_FILE" ]; then
  echo "Requirements file not found: $REQUIREMENTS_FILE" >&2
  exit 1
fi

mkdir -p "$WHEELS_DIR"

echo "Downloading wheels for $REQUIREMENTS_FILE into $WHEELS_DIR/ ..."
python3 -m pip download \
  --dest "$WHEELS_DIR" \
  --requirement "$REQUIREMENTS_FILE"

echo "Done. Copy (or keep) the $WHEELS_DIR/ directory alongside the repo"
echo "before building on the air-gapped host - docker build will use it"
echo "automatically when present (see Dockerfile)."
