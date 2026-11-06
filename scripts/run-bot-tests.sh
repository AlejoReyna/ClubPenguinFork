#!/usr/bin/env bash
# Run the bot unit tests when present, with no network.
# `--network none` makes any accidental call to a paid provider (Moonshot) impossible,
# and no provider credential is passed in. Usage: run-bot-tests.sh <runtime-image>
set -euo pipefail
cd "$(dirname "$0")/.."
image="${1:?usage: run-bot-tests.sh <runtime-image>}"
if [ ! -f containers/bots/bots/__init__.py ] || [ ! -f scripts/test-bots.py ] || [ ! -d tests ]; then
  echo "Bot service/tests are absent; skipping (nothing to run)."
  exit 0
fi
docker run --rm --network none --read-only --tmpfs /tmp \
  --entrypoint python -v "$PWD":/src:ro -w /src "$image" \
  -I scripts/test-bots.py -v
