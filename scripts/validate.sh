#!/usr/bin/env bash
# Offline-safe checks of everything that does not need credentials. Run by PR CI and locally.
# Terraform uses a throwaway TF_DATA_DIR so an operator's initialized backends in infra/*/.terraform
# are never reconfigured by a validation run.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

tf_data="$(mktemp -d)"
trap 'rm -rf "$tf_data"' EXIT

echo "== Terraform"
terraform fmt -check -recursive infra
for stack in cloud cluster; do
  TF_DATA_DIR="$tf_data/$stack" terraform -chdir="infra/$stack" init -backend=false -input=false -no-color >/dev/null
  TF_DATA_DIR="$tf_data/$stack" terraform -chdir="infra/$stack" validate -no-color
done

echo "== Ansible syntax"
ansible-playbook -i ansible/inventory.ini ansible/preflight.yml --syntax-check
ansible-playbook -i ansible/inventory.ini ansible/configure-operator.yml --syntax-check

echo "== Python, shell and JSON"
python_dirs=(scripts containers/runtime experiments production tests)
[ -d containers/bots ] && python_dirs+=(containers/bots)
python3 -I -m compileall -q "${python_dirs[@]}"
for script in scripts/*.sh; do bash -n "$script"; done
python3 -I -m json.tool monitoring/dashboard.json > /dev/null

# PyYAML is needed by the pipeline policy checks and their tests; never skip them silently.
if python3 -I -c 'import yaml' 2>/dev/null; then
  yaml_python=python3
else
  venv="${CP_VALIDATE_VENV:-$project_root/.local/validate-venv}"
  [ -x "$venv/bin/python" ] || { python3 -m venv "$venv" && "$venv/bin/pip" install --quiet 'pyyaml==6.0.2'; }
  yaml_python="$venv/bin/python"
fi

echo "== Unit tests of the CI/CD scripts"
CP_TEST_QUIET=1 "$yaml_python" -I -m unittest discover -s scripts/tests -t scripts

echo "== Pipeline policy"
"$yaml_python" -I scripts/lint_pipeline.py

echo "== Helm render"
helm template prometheus-lab prometheus --version 29.40.3 \
  --repo https://prometheus-community.github.io/helm-charts \
  --namespace monitoring-lab -f monitoring/prometheus.yaml -f monitoring/bots-rules.yaml > /dev/null
helm template grafana-lab grafana --version 14.3.1 \
  --repo https://grafana-community.github.io/helm-charts \
  --namespace monitoring-lab -f monitoring/grafana.yaml > /dev/null
echo "All validations passed."
