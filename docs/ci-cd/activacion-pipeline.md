# Activación del pipeline de GitHub Actions (paso a paso)

Guía para el operador. Ningún comando de esta guía se ha ejecutado: cada uno
cambia estado remoto (Git, GitHub o Azure) y debe lanzarse a mano, revisando la
salida. Los valores entre `<...>` son privados o no están en el repositorio:
sustitúyelos en tu terminal, nunca en este archivo. El despliegue automático
queda **apagado** (`CP_AUTO_DEPLOY_ENABLED=false`) hasta el paso (e).

Requisitos que impone `.github/workflows/ci.yml`:

| Tipo | Valor |
|---|---|
| Runner del job `deploy` | `[self-hosted, macOS, ARM64, clubpenguin-lab]` (`validate` y `build-and-test` usan `ubuntu-24.04`) |
| Environment | `clubpenguin-lab` |
| Permisos | global `contents: read`; `deploy`: `contents: read`, `id-token: write` (OIDC) |
| Variables de repositorio | `CP_AUTO_DEPLOY_ENABLED`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `CP_NODE_NAME`, `CP_BOTS_PROVIDER_MODE` (`api` o `simulated`) |
| Secrets | ninguno (OIDC; el resto vive en `~/.config/clubpenguin-lab/deploy.env` del runner) |
| Condición de despliegue | `push` a `refs/heads/main` y `vars.CP_AUTO_DEPLOY_ENABLED == 'true'` |
| Concurrencia | grupo `clubpenguin-pi-deployment`, sin cancelar el job en curso |

## (a) Commits, push y PR

Los archivos del simulador de puffle (`experiments/puffle_sim/`,
`tests/puffle_sim/`, `docs/puffle-conversacion.md`) pertenecen a otro trabajo:
no se añaden. Usa siempre rutas explícitas, nunca `git add -A` ni `git add .`.

```bash
cd ~/theCode/clubpenguin-lab
git switch feature/glaciar-mobile
git status --short   # confirmar que la lista coincide con la de abajo

# 1. Scripts de despliegue y release, con sus pruebas
git add scripts/lablib.py scripts/deploy.py scripts/release.py scripts/plan_gate.py \
  scripts/changed.py scripts/restore_verify.py scripts/ws_probe.py scripts/lint_pipeline.py \
  scripts/install-tools.sh scripts/github_deploy_guard.py \
  scripts/backup.py scripts/build-images.py scripts/fetch-sources.py scripts/game-smoke.py \
  scripts/publish-images.py scripts/sync-kubeconfig.py scripts/test-containers.py \
  scripts/tunnel.py scripts/validate.sh scripts/tests/*.py ansible/preflight.yml
git commit -m "Scripts de release y despliegue con compuertas de plan y pruebas"

# 2. Infraestructura
git add infra/cloud/main.tf infra/cluster/main.tf infra/cluster/game.tf infra/cluster/web.tf \
  infra/cluster/terraform.tfvars.example infra/cluster/bots.tf infra/cluster/release.tf \
  monitoring/bots-rules.yaml infra/cloud/terraform.tfvars.example \
  infra/cloud/backend.hcl.example infra/cluster/backend.hcl.example .gitignore
git commit -m "Infraestructura del clúster: bots y mapa release-info"

# 3. Servicio de bots y sus pruebas (solo tests/ de primer nivel)
git add containers/bots scripts/bot_accounts.py scripts/create-bot-accounts.py \
  scripts/prepare-work-bots.py scripts/run-bot-tests.sh scripts/test-bots.py \
  scripts/test-work-provision.py scripts/bots_integration.py scripts/set-moonshot-key.py \
  tests/__init__.py tests/support.py tests/test_*.py docs/bots.md
git commit -m "Servicio de bots de Glaciar y pruebas sin red"

# 4. Pipelines y documentación
git add .github/workflows/ci.yml .github/actionlint.yaml azure-pipelines.yml \
  README.md docs/ci-cd
git commit -m "Pipelines de GitHub Actions y Azure DevOps con despliegue apagado"

git status --short   # solo deben quedar los archivos del puffle
git push -u origin feature/glaciar-mobile
gh pr create --repo AlejoReyna/ClubPenguinFork --base main --head feature/glaciar-mobile \
  --title "Pipeline de CI/CD y bots de Glaciar (despliegue automático apagado)" \
  --body "CI en Ubuntu hospedado; job deploy en runner propio, apagado por CP_AUTO_DEPLOY_ENABLED. Ver docs/ci-cd/activacion-pipeline.md."
```

Antes de cada `git commit`, revisa `git diff --cached --stat` y que no aparezca
ningún dato privado (IPs, dominios, IDs de suscripción).

## (b) Runner self-hosted macOS ARM64

Instálalo con un usuario dedicado del Mac. Requiere Docker, Azure CLI,
Terraform, Helm, Ansible, Python, `kubectl` y SSH al Pi (ver
[agente-mac.md](agente-mac.md)). Descarga la versión vigente que indique
GitHub en *Settings → Actions → Runners → New self-hosted runner* (macOS, ARM64).

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# Descargar y descomprimir el paquete osx-arm64 indicado por GitHub (verificar su SHA-256)
TOKEN="$(gh api -X POST repos/AlejoReyna/ClubPenguinFork/actions/runners/registration-token --jq .token)"
./config.sh --url https://github.com/AlejoReyna/ClubPenguinFork --token "$TOKEN" \
  --name clubpenguin-mac --labels clubpenguin-lab --work _work --unattended
unset TOKEN
./svc.sh install     # crea el LaunchAgent (launchd) del usuario actual
./svc.sh start
./svc.sh status
gh api repos/AlejoReyna/ClubPenguinFork/actions/runners --jq '.runners[]|[.name,.status,([.labels[].name]|join(","))]|@tsv'
```

`self-hosted`, `macOS` y `ARM64` se añaden solos. El repo es **público**: en
*Settings → Actions → General* exige aprobación para workflows de forks
("Require approval for all outside collaborators"). El workflow solo envía a
este runner el job `deploy`, condicionado a push en `main`.

## (c) OIDC con Azure

Recursos (nombres en `~/.config/clubpenguin-lab/deploy.env`; los valores por
defecto están en `infra/cloud/main.tf`): ACR `<CP_ACR_NAME>` en
`<RG_DEL_ACR>`; cuenta de estado `<CP_STATE_STORAGE_ACCOUNT>` en
`<CP_STATE_RESOURCE_GROUP>`, contenedores `tfstate-clubpenguin` y
`clubpenguin-backups` (valores por defecto de `deploy.env.example`).

```bash
SUB="<AZURE_SUBSCRIPTION_ID>"
az account set --subscription "$SUB"
APP_ID="$(az ad app create --display-name gh-clubpenguin-lab-deploy --query appId -o tsv)"
az ad sp create --id "$APP_ID"
SP_OID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-env-clubpenguin-lab",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:AlejoReyna/ClubPenguinFork:environment:clubpenguin-lab",
  "audiences": ["api://AzureADTokenExchange"]
}'

ACR_ID="$(az acr show -n "<CP_ACR_NAME>" -g "<RG_DEL_ACR>" --query id -o tsv)"
az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope "$ACR_ID"

ST_ID="$(az storage account show -n "<CP_STATE_STORAGE_ACCOUNT>" -g "<CP_STATE_RESOURCE_GROUP>" --query id -o tsv)"
for c in tfstate-clubpenguin clubpenguin-backups; do
  az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" --scope "$ST_ID/blobServices/default/containers/$c"
done
az ad sp show --id "$APP_ID" --query appOwnerOrganizationId -o tsv   # = AZURE_TENANT_ID
echo "$APP_ID"                                                        # = AZURE_CLIENT_ID
```

El backend usa `use_azuread_auth` (`scripts/lablib.py`) y blobs con
`--auth-mode login`, así que no hace falta acceso a claves de la cuenta. No se
concede nada sobre `infra/cloud` ni la suscripción. Si `release.py` necesita
leer el ACR con `az acr repository`, AcrPush ya incluye pull/lectura. Comprueba
en la primera ejecución controlada que no falte ningún permiso de lectura.

## (d) Environment y variables

```bash
R=repos/AlejoReyna/ClubPenguinFork
# Environment restringido a main, con revisor obligatorio
gh api -X PUT $R/environments/clubpenguin-lab --input - <<JSON
{"reviewers":[{"type":"User","id":$(gh api user --jq .id)}],
 "prevent_self_review":false,
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST $R/environments/clubpenguin-lab/deployment-branch-policies -f name=main -f type=branch

gh variable set CP_AUTO_DEPLOY_ENABLED --repo AlejoReyna/ClubPenguinFork --body false
gh variable set AZURE_CLIENT_ID       --repo AlejoReyna/ClubPenguinFork --body "<APP_ID>"
gh variable set AZURE_TENANT_ID       --repo AlejoReyna/ClubPenguinFork --body "<AZURE_TENANT_ID>"
gh variable set AZURE_SUBSCRIPTION_ID --repo AlejoReyna/ClubPenguinFork --body "<AZURE_SUBSCRIPTION_ID>"
gh variable set CP_NODE_NAME          --repo AlejoReyna/ClubPenguinFork --body "<NODO_REAL>"
gh variable set CP_BOTS_PROVIDER_MODE --repo AlejoReyna/ClubPenguinFork --body api
gh variable list --repo AlejoReyna/ClubPenguinFork
```

`CP_BOTS_PROVIDER_MODE` debe coincidir con el Deployment real (la última
lectura del Pi mostró `api`); `scripts/github_deploy_guard.py` falla si no.
Las variables son de repositorio, como lee el workflow; en un repo público son
visibles en los logs de Actions solo para quien vea los runs: no son secretos,
pero no deben aparecer en el repositorio. Protege `main` (PR obligatorio y los
checks `Validate configuration` y `Test isolated containers and bots`):

```bash
gh api -X PUT $R/branches/main/protection --input - <<'JSON'
{"required_status_checks":{"strict":true,"contexts":["Validate configuration","Test isolated containers and bots"]},
 "enforce_admins":false,"required_pull_request_reviews":{"required_approving_review_count":0},
 "restrictions":null}
JSON
```

### Nombres de recursos Azure (privados)

Los nombres reales del ACR, su grupo de recursos y la cuenta de estado ya no
están en el código. El job `deploy` los lee de `~/.config/clubpenguin-lab/deploy.env`
en el runner (`CP_ACR_NAME`, `CP_STATE_RESOURCE_GROUP`, `CP_STATE_STORAGE_ACCOUNT`,
opcionales `CP_STATE_CONTAINER`, `CP_CLUSTER_STATE_KEY`, `CP_CLOUD_STATE_KEY`,
`CP_BACKUP_CONTAINER`), así que **no hacen falta variables nuevas de GitHub**.
Si prefieres gestionarlos desde GitHub, `scripts/lablib.py` da prioridad a las
variables de entorno; tendrías que añadirlas también al bloque `env:` del job
`deploy` en `ci.yml`:

```bash
gh variable set CP_ACR_NAME              --repo AlejoReyna/ClubPenguinFork --body "<CP_ACR_NAME>"
gh variable set CP_STATE_RESOURCE_GROUP  --repo AlejoReyna/ClubPenguinFork --body "<CP_STATE_RESOURCE_GROUP>"
gh variable set CP_STATE_STORAGE_ACCOUNT --repo AlejoReyna/ClubPenguinFork --body "<CP_STATE_STORAGE_ACCOUNT>"
```

Para ejecuciones manuales de Terraform, los valores viven en archivos privados
ignorados por git: `infra/cloud/terraform.tfvars`, `infra/cloud/backend.hcl`,
`infra/cluster/backend.hcl` y `cloud_state` en `infra/cluster/terraform.tfvars`
(plantillas en los `*.example`). Usa `terraform init -backend-config=backend.hcl`.

## (e) Interruptor `CP_AUTO_DEPLOY_ENABLED`

Queda en `false`. Con `false`, un push a `main` solo ejecuta CI. Actívalo solo
cuando: la PR esté fusionada y la CI pase; el runner aparezca `online`; una
ejecución controlada de `python3 scripts/deploy.py preflight` y `plan --expect
images-only` en el Mac pase con OIDC; y la evolución de los NPC esté validada
según [bots.md](../bots.md) (zonas reales medidas, cuentas dedicadas
provisionadas y aceptación en el Pi). Entonces:

```bash
gh variable set CP_AUTO_DEPLOY_ENABLED --repo AlejoReyna/ClubPenguinFork --body true
```

Para volver a apagarlo: `--body false` (no detiene un job ya iniciado). No
activar a la vez el despliegue automático de `azure-pipelines.yml`.
