#!/usr/bin/env bash
# Banco local para grabar el puffle controlando la app Música de esta Mac.
# Solo local: no toca el Pi, K3s, Terraform ni GitHub. Se cierra al borrar .local/hold.json (máx. 45 min).
set -euo pipefail
cd "$(dirname "$0")/.."
swf=.local/clubpenguin-puffle-engine-v6.swf
if [ ! -f "$swf" ]; then
  c=$(docker create clubpenguin/web:puffle-v6-candidate-local)
  docker cp "$c":/srv/media/play/v2/client/engine.swf "$swf"; docker rm "$c" >/dev/null
fi
echo "41c6b81e19f970a381a7f85b478b8563a383ab27a22a4b75a8e18df5027735c5  $swf" | shasum -a 256 -c -
rm -f .local/hold.json
nohup python3 experiments/puffle_sim/run_houdini_fixture.py --provider relay --music-mac \
  --visual-swf "$swf" --browser-python .local/browser-tools/bin/python --hold .local/hold.json \
  > .local/apple-music-bench.log 2>&1 &
echo "PID $! · log .local/apple-music-bench.log"
