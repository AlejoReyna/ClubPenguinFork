#!/usr/bin/env bash
# Pre-flight for Pi live-bot trials: tunnel, kubeconfig, node — no deploy, no secrets.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/clubpenguin-pi.yaml}"
NODE="${1:-}"
if [[ "${1:-}" == "--expected-node" ]]; then
  NODE="${2:?pass --expected-node NAME}"
fi
if [[ -z "$NODE" ]]; then
  echo "Usage: $0 --expected-node <node-name>" >&2
  exit 1
fi
cd "$ROOT"
echo "== Club Penguin Pi — pingüinos vivos (read-only checks)"
python3 scripts/tunnel.py verify
if [[ ! -f "$KUBECONFIG" ]]; then
  echo "Missing kubeconfig: $KUBECONFIG" >&2
  exit 1
fi
mode="$(stat -f '%OLp' "$KUBECONFIG" 2>/dev/null || stat -c '%a' "$KUBECONFIG")"
if [[ "$mode" != *600* && "$mode" != "600" ]]; then
  echo "Kubeconfig should be mode 0600 (got $mode)" >&2
  exit 1
fi
server="$(kubectl --kubeconfig "$KUBECONFIG" config view -o jsonpath='{.clusters[0].cluster.server}')"
if [[ "$server" != "https://127.0.0.1:16443" ]]; then
  echo "Unexpected API server: $server (use the Pi tunnel config)" >&2
  exit 1
fi
ready="$(kubectl --kubeconfig "$KUBECONFIG" get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
arch="$(kubectl --kubeconfig "$KUBECONFIG" get node "$NODE" -o jsonpath='{.status.nodeInfo.architecture}')"
if [[ "$ready" != "True" || "$arch" != "arm64" ]]; then
  echo "Node $NODE not Ready arm64 (ready=$ready arch=$arch)" >&2
  exit 1
fi
echo "OK: tunnel, kubeconfig and node $NODE"
echo ""
echo "Next steps (manual):"
echo "  1. Docker: test-containers.py --live-cast --recall (and --voices/--life if needed)"
echo "  2. publish-images.py --components bots --tag <TAG> --registry \$CP_ACR_NAME"
echo "  3. deploy.py plan/apply with bots_cast_enabled, bots_dialogue_enabled, bots_recall_enabled"
echo "  4. set-moonshot-key.py --expected-node $NODE --execute   # key only; keep simulated until authorized"
echo "  5. Separate plan to set bots_provider_mode=api when spend is authorized"
echo ""
echo "See docs/bots-pi-prueba-vivos.md"
