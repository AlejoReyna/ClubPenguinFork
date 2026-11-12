# Operations

How the lab is configured, released, deployed, backed up and rolled back. Everything here runs
from the operator Mac, or from the same Mac acting as the self-hosted GitHub Actions runner.

## Tooling

| Where | Needs |
|---|---|
| Operator Mac (Apple silicon) | Docker with Buildx, Terraform, Azure CLI, kubectl, OpenSSH, Python 3.9+ for the scripts (standard library only) |
| Validation tools | `bash scripts/install-tools.sh [DIR]` installs pinned Terraform, Helm, Ansible and ruff, checking binary checksums. Ansible needs Python 3.12–3.14 (`PYTHON=...`) |
| Raspberry Pi | ARM64, 8 GB RAM, 20 GiB free, K3s already installed (Ansible checks it and never reinstalls it) |

## Configuration

The repository is anonymized: real hostnames, addresses and Azure names live outside Git and
are resolved in this order:

1. `CP_*` environment variables (for example, CI variables);
2. `~/.config/clubpenguin-lab/deploy.env`, which must be owned by the user with mode `0600`
   (template: [`deploy.env.example`](../deploy.env.example));
3. safe defaults for everything that is not site-specific.

A missing value, or one still holding a placeholder such as `example.com`, stops the scripts
before they build or plan anything. The other private files have committed `.example`
templates: `infra/*/backend.hcl` (remote state location) and `infra/*/terraform.tfvars`.

| Key | Used for |
|---|---|
| `CP_PUBLIC_HOSTNAME` | Public hostname; also the WebSocket origin the proxy accepts |
| `CP_PI_HOST`, `CP_PI_USER` | SSH target for Ansible and the API tunnel |
| `CP_LAN_CIDR` | LAN range allowed to reach the game NodePorts (`game-ingress`) |
| `CP_ACR_NAME`, `CP_STATE_RESOURCE_GROUP`, `CP_STATE_STORAGE_ACCOUNT` | Registry and Terraform state storage |
| `CP_NODE_NAME` | Expected node name; the tunnel check refuses any other cluster |
| `CP_RELEASE_PINS` | Components to keep at a fixed tag, e.g. `database=lab-002` |
| `CP_PUFFLE_ENABLED`, `CP_PUFFLE_ALLOWED_PLAYERS` | Puffle switch and optional pilot allowlist |
| `CP_PROXY_EXTRA_HOSTS` | Extra origin hosts the WebSocket proxy accepts, such as a LAN address |

## First-time setup

```bash
cd ansible
ansible-playbook preflight.yml            # read-only: ARM64, RAM, disk, k3s active
ansible-playbook configure-operator.yml   # status script for the operator; no sudo, idempotent
cd ..
python3 scripts/sync-kubeconfig.py        # dedicated ~/.kube/clubpenguin-pi.yaml (mode 0600)
python3 scripts/tunnel.py start           # SSH tunnel to the API on 127.0.0.1:16443
python3 scripts/tunnel.py verify          # it is the expected node, ARM64 and Ready
```

The remote state container is created once, outside the state it will hold. After that the
operator applies `infra/cloud` (backup container and the Pi's read-only pull token) with their
own Azure permissions:

```bash
az storage container create --account-name "<state account>" --auth-mode login \
  --name tfstate-clubpenguin --public-access off
terraform -chdir=infra/cloud init -backend-config=backend.hcl
terraform -chdir=infra/cloud plan -out=cloud.tfplan && terraform -chdir=infra/cloud apply cloud.tfplan
```

## Releases

`scripts/release.py` builds content-addressed releases:

- Each component (`runtime`, `database`, `web`, `bots`) gets its own tag, `rt-…`, `db-…`, `web-…`
  or `bots-…`, derived from the SHA-256 of its tracked inputs and pinned upstream revisions. A
  web-only change produces a new `web-…` tag and nothing else, so PostgreSQL is never restarted
  for a frontend fix.
- A tag that already exists in the registry is reused, never rebuilt. `latest` is never used,
  images must be `linux/arm64`, and images built with stub media are refused.
- The manifest links commit, tags, digests and platform. After a verified deploy it is stored in
  Blob as `releases/<UTC>-<commit>.json`, which is what rollbacks read.

```bash
python3 scripts/release.py resolve --out release.json          # tags; queries the registry
python3 scripts/release.py build --release release.json        # ARM64 build + isolated tests
python3 scripts/release.py publish --release release.json --out release.json
python3 scripts/release.py manifest --release release.json --out manifest.json
```

## Deploys

`scripts/deploy.py` owns `infra/cluster`:

```bash
python3 scripts/deploy.py preflight                         # Azure identity, tunnel, node, Ansible
python3 scripts/deploy.py plan --manifest manifest.json     # saved plan, summary and SHA-256
python3 scripts/deploy.py apply --expect-sha256 <sha>       # backup if needed, then that exact plan
python3 scripts/deploy.py verify --manifest manifest.json   # images, rollouts, connectivity
python3 scripts/deploy.py record --manifest manifest.json   # store the manifest in Blob
python3 scripts/deploy.py cleanup                           # delete the plan, close this run's tunnel
```

Every saved plan goes through `scripts/plan_gate.py`, which blocks:

- resource types or namespaces outside the lab;
- deleting or replacing protected resources (database and bots volumes, credentials, the public
  tunnel) unless that exact address is passed with `--allow-destructive`;
- creating something that already exists in the cluster but not in the state (it must be
  imported instead, see `deploy.py adopt`);
- with `--expect images-only`, anything other than image updates and the `release-info` map.

The same gate decides whether a backup is required: any substantive change to a data-bearing
resource makes `apply` take and verify a backup first, and `--backup never` is then refused.

## Continuous integration and delivery

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

1. **Validate** (GitHub-hosted Ubuntu, no credentials): `scripts/validate.sh` checks Terraform
   format and validity, Ansible syntax, Python compilation, ruff, the script unit tests, the web
   tests, the Azure Pipelines policy, and both Helm renders.
2. **Build and test** (GitHub-hosted, no credentials): native images with stub media; the bots,
   puffle and production suites inside the runtime image with `--network none`; a backup and
   restore test against a disposable PostgreSQL; and an end-to-end run with a real Houdini,
   simulated bots and the puffle on an isolated network.
3. **Deploy** (only on `main`, only when the repository variable `CP_AUTO_DEPLOY_ENABLED` is
   `true`): runs on the self-hosted Mac in the `clubpenguin-lab` environment, authenticates to
   Azure with OIDC, refuses a commit that is no longer the tip of `main`, builds and publishes
   ARM64 images, then plans with `--expect images-only`, applies the gated plan by SHA-256,
   verifies the public site and records the release. One concurrency group serializes deploys
   and backups. A manual run is `gh workflow run ci.yml --ref main -f deploy=true`.

Changes beyond image updates (new resources, policies, settings) are planned and applied by the
operator with the commands above, then later releases flow through CD again.

`azure-pipelines.yml` describes the same flow as five Azure DevOps stages (validate, test,
release, apply behind an Azure DevOps environment, cleanup). CI checks it against a policy
(`scripts/lint_pipeline.py`), but GitHub Actions is the active path.

## Backups and restore

- `scripts/backup.py` runs `pg_dump` in `postgres-0`, restores the dump into a throwaway
  `verify_*` database, reads the accounts back, drops only that database, then uploads the dump
  and a SHA-256 manifest to Blob. A private local copy is kept under `~/.clubpenguin-lab/backups/`.
- `scripts/backup-operator-state.py` saves what the database dump does not cover: external
  Secrets, the bots spend ledger and a consistent SQLite snapshot of paired puffle devices.
  The dialog log (PVC `bots-dialog`) is not part of this backup and is not uploaded to Azure.
  It stays on the Pi. Losing the card loses the log; the spend ledger is still in the backup.
- `.github/workflows/backup.yml` runs both every day at 12:00 UTC on the self-hosted runner
  when `CP_BACKUP_ENABLED` is `true`; the Pi must be on for a backup to happen.
- `scripts/restore_verify.py` proves that a recorded backup restores (size and SHA-256 checked,
  restored into a throwaway `restore_*` database) without touching the live one.

A backup only counts as valid once it has been restored and read back.

## Rollback

Application rollback and data recovery are separate on purpose:

| | Application rollback | Data recovery |
|---|---|---|
| Fixes | A bad release (images or configuration) | Lost or corrupted data |
| How | Fetch an earlier manifest (`release.py fetch previous`), then plan, gate and apply it | Restore a verified dump |
| Automated | Yes, through the same plan and gate | Never; a manual, explicit procedure |
| Risk to data | None, nothing is written to the database | Replaces data |

Rolling back the application does not undo what newer code already wrote to the database.
Manifests from before the puffle shipped turn that feature off and scale its broker to zero,
while keeping its volume. The `postgres-data` and `bots-budget` volumes are never deleted.

## Day-to-day access

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  port-forward --address 127.0.0.1 svc/web 13080:8080              # game on localhost:13080
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n monitoring-lab \
  port-forward --address 127.0.0.1 svc/grafana-lab 13000:80        # Grafana on localhost:13000
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n monitoring-lab \
  get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -D
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  exec -i deploy/dash -- python - < scripts/game-smoke.py         # register two synthetic players
```

## Dialog log

The AI-penguin dialog log is a directory of JSONL files on the PVC `bots-dialog`, written only
by the bots pod at `/var/lib/bots-dialog`. There is no Ingress, no Cloudflare route of its own,
and no read API on port 9102. NetworkPolicies did not need a change.

The single exception is the public «Diálogos» panel, off by default. With
`dialog_feed_enabled = true` (Terraform, next to the `bots_*` variables; needs `web_enabled`
and `bots_enabled`), the web pod gets a `dialog-feed` sidecar (`MODE=dialog`, loopback port
8767, UID 10001) that mounts the PVC and the switch ConfigMap read-only, and nginx serves its
allow-listed output at `GET /dialog/feed.json` (no player hash, no human text; newest 200
events of today and yesterday, UTC; 5-second cache). Without the sidecar that path is a 404
and the page hides the button. `deploy.py verify` runs a `dialog-feed` check whenever the
sidecar is deployed: the feed must answer through web, follow the switch, and carry only the
allow-listed keys. The PVC is ReadWriteOnce on `local-path`, which works because web and bots
share the Pi's single node; a second node would need RWX storage or another reader.

To hide the public feed at once without a deploy, turn the switch off (below): the feed
answers `enabled: false` with no events, and the panel shows "Registro apagado". To remove
the button, set `dialog_feed_enabled = false` and deploy.

The switch defaults to off. It is the ConfigMap `bots-dialog-log` key `enabled`. The player
hash salt is the env var `BOTS_DIALOG_LOG_SALT` from the hand-created Secret `bots-dialog-salt`
key `salt`. Create that Secret before the first pod start, and do not paste the value anywhere:

```bash
umask 077
openssl rand -hex 32 | kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  create secret generic bots-dialog-salt --from-file=salt=/dev/stdin
```

If that kubectl cannot read `/dev/stdin`, write the bytes to a `mktemp` file, `--from-file=salt`
that path, and `rm` the file. The service strips one trailing newline, so the hash key is the
64 hex characters. The Secret is optional on the pod: without it the bots still start, and
`player` is stored as null. The env var is not hot-reloaded; a Secret created after the pod is
up needs `kubectl rollout restart deploy/bots`.

Turn the log on or off without a redeploy (allow a minute or two for the kubelet and the
process):

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  patch configmap bots-dialog-log --type merge -p '{"data":{"enabled":"true"}}'
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  patch configmap bots-dialog-log --type merge -p '{"data":{"enabled":"false"}}'
```

Day files older than 30 days are deleted by the bots process itself. Copy a day to the Mac
with either of:

```bash
# Reliable on the slim image: cat the file. kubectl cp needs tar inside the pod.
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  exec deploy/bots -- cat /var/lib/bots-dialog/2026-11-11.jsonl > 2026-11-11.jsonl

# Whole directory, when the image has tar:
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  cp deploy/bots:/var/lib/bots-dialog ./bots-dialog
```

Then `python3 scripts/bitacora.py --dir` that directory. See `docs/bots.md`.

Until `director.py`, `travel.py` and `life.py` call `emit`, a switch that is on still produces
no file: the writer is running and idle. A one-shot `emit` from `kubectl exec` is enough to
prove the disk; that line is real log data and ages out with everything else.

## Maintenance notes

- The Pi's registry pull token is time-limited (`infra/cloud`); rotate it before it expires.
- The Cloudflare connector and its policy were first created by hand. `deploy.py adopt` imports
  them into the state, and deploys always pass `cloudflared_enabled=true`, so a clean checkout can
  never remove the tunnel.
- Before powering the Pi off, take a backup and shut down cleanly. After boot, check that the
  node is Ready, volumes are mounted, pods are available and a test account can log in.
- Azure spend is limited to a Basic registry and blob storage. A managed cluster was deliberately
  avoided.
