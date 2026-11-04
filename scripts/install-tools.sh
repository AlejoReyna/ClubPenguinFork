#!/usr/bin/env bash
# Install the pinned validation tools into a directory (default: ./.local/tools).
# Checksums are pinned here, so a changed upstream file fails the build instead of
# being trusted. The directory is cache-friendly: nothing secret is ever written to it.
#
#   scripts/install-tools.sh [DIR]      then:  export PATH="DIR:DIR/ansible/bin:$PATH"
set -euo pipefail

TERRAFORM_VERSION=1.17.1
HELM_VERSION=3.19.0
ANSIBLE_SPEC='ansible-core==2.20.11'
PYYAML_SPEC='pyyaml==6.0.2'

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)
    tf_arch=linux_amd64; helm_arch=linux-amd64
    tf_sha=4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061
    helm_sha=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496 ;;
  Darwin-arm64)
    tf_arch=darwin_arm64; helm_arch=darwin-arm64
    tf_sha=b1ecb3fe0d0a0290c1ab6f83d967d43f25da05d0b671ee0fb355252268dce95a
    helm_sha=31513e1193da4eb4ae042eb5f98ef9aca7890cfa136f4707c8d4f70e2115bef6 ;;
  *) echo "Unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

dir="${1:-$(cd "$(dirname "$0")/.." && pwd)/.local/tools}"
mkdir -p "$dir"
cd "$dir"
verify() { # <expected-sha256> <file>
  local actual
  if command -v sha256sum >/dev/null; then actual="$(sha256sum "$2" | cut -d' ' -f1)"; else actual="$(shasum -a 256 "$2" | cut -d' ' -f1)"; fi
  [ "$actual" = "$1" ] || { echo "Checksum mismatch for $2" >&2; exit 1; }
}

if ! ./terraform version 2>/dev/null | head -1 | grep -q "v${TERRAFORM_VERSION}$"; then
  curl -fsSL -o terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_${tf_arch}.zip"
  verify "$tf_sha" terraform.zip
  unzip -o -q terraform.zip terraform && rm terraform.zip
fi
if ! ./helm version --short 2>/dev/null | grep -q "^v${HELM_VERSION}"; then
  curl -fsSL -o helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-${helm_arch}.tar.gz"
  verify "$helm_sha" helm.tgz
  tar -xzf helm.tgz && mv "${helm_arch}/helm" ./helm && rm -rf "${helm_arch}" helm.tgz
fi
if [ ! -x ansible/bin/ansible-playbook ]; then
  "${PYTHON:-python3}" -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,15), "Validation tools need Python 3.12–3.14"'
  "${PYTHON:-python3}" -m venv ansible
  ansible/bin/pip install --quiet "$ANSIBLE_SPEC" "$PYYAML_SPEC"
fi
echo "Tools ready in $dir"
./terraform version | head -1
./helm version --short
ansible/bin/ansible-playbook --version | head -1
