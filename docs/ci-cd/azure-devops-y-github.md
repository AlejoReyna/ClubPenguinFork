# Azure DevOps, GitHub e identidad

Escribir `azure-pipelines.yml` no crea nada de lo siguiente. Son **cinco piezas
independientes**, y el pipeline remoto no funciona hasta que existan todas las que le
tocan:

| # | Pieza | Para qué | Lo necesita |
|---|---|---|---|
| 1 | Conexión Azure Pipelines ↔ GitHub | Leer el repo y recibir eventos de push/PR | CI y CD |
| 2 | Service connection hacia Azure | Identidad federada de la release (ACR, Blob) | `Release`, `Apply` |
| 3 | Agente de despliegue (pool + agente Mac) | Alcanzar la LAN y el API privado del Pi | `Release`, `Apply`, `Cleanup` |
| 4 | Environment `clubpenguin-lab` | Aprobación, exclusión mutua y control de rama del despliegue | `Apply` |
| 5 | Protecciones de `main` y validación de PR | Que nada llegue a `main` sin pasar el CI | Todo |

La **parte de PR (piezas 1 y 5) es suficiente para el primer CI**. Las piezas 2–4 se
necesitan solo al activar `Release`/`Apply`.

## 1. Conexión con GitHub (sin PAT en el repositorio)

El repo está en `github.com/AlejoReyna/ClubPenguinFork`. Se conecta con la **app de
GitHub «Azure Pipelines»**, que da acceso solo a los repositorios elegidos y envía los
webhooks. No hay token en el repo ni en el YAML.

1. En GitHub: Marketplace → *Azure Pipelines* → Install → «Only select repositories» →
   `ClubPenguinFork`. (O desde Azure DevOps en el paso 3.)
2. En Azure DevOps crea un proyecto (privado). Pipelines → *New pipeline* → *GitHub* →
   autoriza la app → elige el repo → *Existing Azure Pipelines YAML file* →
   rama `main`, ruta `/azure-pipelines.yml` → *Save* (no *Run* todavía).
3. Pipeline → *Edit* → ⋮ → *Triggers* → *Pull request validation*: activo para `main`;
   **desactiva** «Build pull requests from forks», o si lo dejas, deja desactivado
   «Make secrets available to builds of forks» y activa «Require a team member's
   comment before building a pull request».
4. Project settings → Pipelines → *Settings*: activa «Limit job authorization scope to
   current project» y «Protect access to repositories in YAML pipelines».
5. Al ejecutarse, el pipeline aparece en GitHub como un *check run*. **Su nombre exacto**
   se ve en una PR después de la primera ejecución; no lo supongas (ver paso 5.2).

No pongas PAT en variables, en el YAML ni en `persistCredentials`: el pipeline usa
`persistCredentials: false` y el checkout lo autentica la app.

## 2. Service connection federada hacia Azure

Usa **federación de identidad (OIDC)**: no hay `client secret` que rotar ni guardar.

### 2.1 Crear la identidad en Entra ID

Ejecuta con una cuenta que pueda crear aplicaciones y asignar roles. Los nombres de
recurso son los del laboratorio; sustituye por los tuyos.

```bash
ACR=<nombre-del-acr>                 # el existente; no se crea otro
SA=<cuenta-de-storage>               # la de estado y respaldos
APP=sp-clubpenguin-azdo

APP_ID=$(az ad app create --display-name "$APP" --query appId -o tsv)
az ad sp create --id "$APP_ID" >/dev/null
echo "Application (client) ID: $APP_ID"; az account show --query tenantId -o tsv
```

### 2.2 Crear la conexión en Azure DevOps y su credencial federada

Project settings → *Service connections* → *New* → *Azure Resource Manager* →
**Workload identity federation (manual)**. Nombre: `clubpenguin-azure` (el valor por
defecto del parámetro). Azure DevOps muestra el **Issuer** y el **Subject identifier**
(`sc://<organización>/<proyecto>/clubpenguin-azure`). Con ellos:

```bash
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "azdo-clubpenguin",
  "issuer": "<Issuer mostrado por Azure DevOps>",
  "subject": "<Subject identifier mostrado por Azure DevOps>",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

Completa en la UI el *Tenant ID*, *Subscription ID/nombre* y *Client ID*; **no** marques
«Grant access permission to all pipelines». Después, en la conexión → *Security*:
autoriza solo este pipeline, y en *Approvals and checks* añade **Branch control**
con `refs/heads/main` (verificación de rama de origen activada).

### 2.3 Permisos mínimos

```bash
SA_ID=$(az storage account show -n "$SA" --query id -o tsv)

# Publicar y consultar imágenes del laboratorio (ver nota sobre el alcance)
az role assignment create --assignee "$APP_ID" --role AcrPush \
  --scope "$(az acr show -n "$ACR" --query id -o tsv)"

# Estados Terraform y lock (blob lease): solo ese contenedor
az role assignment create --assignee "$APP_ID" --role "Storage Blob Data Contributor" \
  --scope "$SA_ID/blobServices/default/containers/tfstate-clubpenguin"

# Respaldos de PostgreSQL y manifiestos de release: solo ese contenedor
az role assignment create --assignee "$APP_ID" --role "Storage Blob Data Contributor" \
  --scope "$SA_ID/blobServices/default/containers/clubpenguin-backups"
```

**Lo que NO recibe:** nada sobre el AKS anterior ni sus grupos de recursos, nada de
administración de Storage o del ACR, nada de la API de Kubernetes del Pi (esa va por el
túnel del agente), ni derechos sobre el contenedor `tfstate` original.

> **Alcance del ACR.** El registro del laboratorio (`CP_ACR_NAME`) está en modo
> `LegacyRegistryPermissions` (comprobado el 3 de noviembre de 2026, SKU Basic).
> En ese modo `AcrPush` es **de todo el registro**, no por repositorio: la identidad
> podría subir a otros repositorios (por ejemplo, los del AKS anterior). Limitar a
> `clubpenguin/*` requeriría cambiar el registro compartido al modo con permisos por
> repositorio (ABAC), lo que afecta al laboratorio anterior; no se hizo. El pipeline solo
> escribe en `clubpenguin/<componente>` y se mitiga con la rama protegida, Branch
> control en la conexión y el pool restringido. El Pi sigue descargando con el token de
> **solo lectura** limitado a `clubpenguin/{runtime,database,web}`.

> **Visibilidad de la suscripción.** `az login` con una identidad que solo tiene roles
> sobre recursos concretos puede no listar la suscripción, y entonces la verificación de
> la conexión falla. Si pasa, asigna `Reader` **solo** sobre el ACR y la cuenta de
> Storage (no sobre grupos de recursos del AKS) y vuelve a verificar. No lo he podido
> comprobar sin la cuenta real: verifícalo en el primer *Verify* de la conexión.

El primer `Release` confirmará si hacen falta más permisos de datos (por ejemplo, para
`az acr repository show`); añade solo el que falte, nunca `Contributor`.

## 3. Agente y pool

Ver [agente-mac.md](agente-mac.md). En Organization settings → Agent pools → *Add pool*
→ *Self-hosted* → `clubpenguin-lab-mac`. Desactiva «Auto-provision this agent pool in
all projects» y «Grant access permission to all pipelines». En el pool →
*Security*/*Approvals and checks*: autoriza solo este pipeline y añade **Branch control**
(`refs/heads/main`).

## 4. Environment `clubpenguin-lab`

Pipelines → Environments → *New environment* (recurso: *None*) → `clubpenguin-lab`.
Menú ⋮ → *Approvals and checks*:

1. **Approvals**: tú como revisor. Si trabajas solo, la aprobación sigue siendo una
   pausa deliberada para leer el resumen del plan; si hay más personas, desactiva
   «Allow approvers to approve their own runs».
2. **Exclusive lock**: un solo despliegue a la vez (complementa `lockBehavior:
   sequential` del YAML y el bloqueo del backend de Terraform).
3. **Branch control**: solo `refs/heads/main`.
4. En *Security* del environment: sin acceso para «todos los pipelines».

## 5. Protección de `main` y validación de PR

### 5.1 Reglas (no se crean con el YAML)

Settings → Branches → *Add branch protection rule* para `main`:

- Require a pull request before merging (al menos 1 aprobación si hay más de una
  persona; en un laboratorio individual puede ser 0, pero la PR sigue obligatoria).
- Require status checks to pass: el *check* del pipeline (ver 5.2).
- Require branches to be up to date; Require conversation resolution.
- Block force pushes y Restrict deletions; incluye a administradores.

Equivalente por línea de comandos (revísalo antes; es una acción sobre el remoto):

```bash
gh api -X PUT repos/AlejoReyna/ClubPenguinFork/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["<nombre exacto del check>"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0, "dismiss_stale_reviews": true},
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

### 5.2 Nombre exacto del check

Tras la primera ejecución sobre una PR:

```bash
gh api repos/AlejoReyna/ClubPenguinFork/commits/<sha-de-la-PR>/check-runs \
  --jq '.check_runs[] | [.app.slug, .name] | @tsv'
```

Usa ese texto en `contexts`. El pipeline **no** usa filtros `paths:` en los triggers a
propósito: un check requerido que se omite nunca reporta y bloquearía la PR. Las
omisiones (p. ej. cambios solo de documentación) se deciden dentro del pipeline con
`scripts/changed.py`, y el check sigue en verde.

## Qué protege el agente Mac (y qué no)

Una PR ejecuta **el YAML de la propia PR**. Si alguien con escritura al repo cambia el
YAML para apuntar al pool del Mac, lo que lo impide **no es el YAML** (que controla el
atacante) sino la configuración de Azure DevOps:

- el pool y la service connection exigen *Branch control* a `main` (una PR corre en
  `refs/pull/N/merge`);
- los pools/conexiones/environment no están abiertos a todos los pipelines;
- las PR de forks no reciben secretos ni se construyen sin aprobación;
- los jobs con credenciales tienen condición explícita que excluye PR (comprobada por
  `scripts/lint_pipeline.py`, como defensa en profundidad, no como frontera de seguridad).

Si no configuras esos controles, **el agente Mac no está protegido de una PR
maliciosa del propio repositorio**; hazlo antes de registrar el agente.

## Lista de comprobación

| ¿Existe? | Cómo comprobarlo |
|---|---|
| App de GitHub instalada solo en este repo | GitHub → Settings → Applications → Installed GitHub Apps |
| Pipeline creado desde `/azure-pipelines.yml` | Azure DevOps → Pipelines |
| Service connection verificada | Project settings → Service connections → *Verify* |
| Credencial federada | `az ad app federated-credential list --id <APP_ID>` |
| Roles mínimos | `az role assignment list --assignee <APP_ID> --all -o table` |
| Pool + agente en línea | Organization settings → Agent pools |
| Environment con aprobación, lock y branch control | Environments → `clubpenguin-lab` → Approvals and checks |
| Protección de `main` | `gh api repos/AlejoReyna/ClubPenguinFork/branches/main/protection` |
