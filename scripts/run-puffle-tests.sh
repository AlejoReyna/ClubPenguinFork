#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
image="${1:?usage: run-puffle-tests.sh <runtime-image>}"
docker run --rm --pull=never --network none --read-only --tmpfs /tmp \
  --entrypoint python -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/app/houdini:/app -e PUFFLE_UPSTREAM=/upstream \
  -v "$PWD/experiments/puffle_sim/run_houdini_fixture.py":/app/experiments/puffle_sim/run_houdini_fixture.py:ro \
  -v "$PWD/tests":/tests:ro -v "$PWD/vendor/houdini/houdini":/upstream:ro -w /app "$image" \
  -m unittest discover -s /tests/puffle_sim
docker run --rm --pull=never --network none --read-only --tmpfs /tmp \
  --entrypoint python -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app/houdini:/app \
  -v "$PWD/tests":/tests:ro -w /app "$image" \
  -m unittest discover -s /tests/production
