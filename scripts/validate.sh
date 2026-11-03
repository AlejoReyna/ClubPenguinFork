#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
terraform fmt -check -recursive infra
for stack in cloud cluster; do
  terraform -chdir="infra/$stack" init -backend=false -input=false -no-color
  terraform -chdir="infra/$stack" validate -no-color
done
ansible-playbook -i ansible/inventory.ini ansible/preflight.yml --syntax-check
ansible-playbook -i ansible/inventory.ini ansible/configure-operator.yml --syntax-check
python3 -m compileall -q scripts containers/runtime
python3 -m json.tool monitoring/dashboard.json > /dev/null
helm template prometheus-lab prometheus --version 29.40.3 \
  --repo https://prometheus-community.github.io/helm-charts \
  --namespace monitoring-lab -f monitoring/prometheus.yaml > /dev/null
helm template grafana-lab grafana --version 14.3.1 \
  --repo https://grafana-community.github.io/helm-charts \
  --namespace monitoring-lab -f monitoring/grafana.yaml > /dev/null
