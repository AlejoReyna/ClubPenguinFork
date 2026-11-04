# Configuración privada, variables y parámetros

El repositorio está anonimizado: `example.com`, `192.168.0.10`, `192.168.0.0/24` y el
usuario `pi` **no** representan la red real. Los valores de despliegue viven fuera del
código y se resuelven así (de mayor a menor prioridad):

1. Variables de entorno `CP_*` (por ejemplo, variables del pipeline).
2. El archivo local del agente `~/.config/clubpenguin-lab/deploy.env`
   ([ejemplo](deploy.env.example)). Debe ser de tu usuario y con modo `0600`.
3. Valores por defecto seguros (nombres de contenedores, clave de estado, etc.).

Si falta un valor requerido, o conserva un valor de ejemplo de la anonimización, el
script **falla antes de construir o planear** y dice qué clave falta. Se puede forzar
`CP_ALLOW_SAMPLE_VALUES=1` solo para pruebas.

**Recomendación:** poner todo en `deploy.env` del agente. Los trabajos de PR corren en
agentes de Microsoft y nunca lo ven. Si prefieres variables del pipeline, no las marques
como secretas (no hace falta) pero recuerda que cualquier persona con permiso para
editar o ejecutar el pipeline puede mostrarlas.

## Claves

| Clave | Necesaria en | Descripción |
|---|---|---|
| `CP_PUBLIC_HOSTNAME` | plan | Dominio público; se pasa como `public_hostname` (origen permitido del WebSocket) |
| `CP_PI_HOST`, `CP_PI_USER` | túnel, Ansible | Dirección y usuario SSH del Pi |
| `CP_LAN_CIDR` | plan | LAN con acceso a los puertos del juego. **Obligatoria**: el valor por defecto del HCL es el de ejemplo y sobrescribiría la política viva |
| `CP_ACR_NAME` | publicar, plan | Nombre del ACR existente |
| `CP_STATE_RESOURCE_GROUP`, `CP_STATE_STORAGE_ACCOUNT` | plan, respaldos | Cuenta de estado Terraform y respaldos |
| `CP_STATE_CONTAINER` (`tfstate-clubpenguin`), `CP_CLUSTER_STATE_KEY` (`cluster.tfstate`), `CP_CLOUD_STATE_KEY` (`cloud.tfstate`) | plan | Ubicación de los estados |
| `CP_BACKUP_CONTAINER` (`clubpenguin-backups`) | respaldos, releases | Contenedor de dumps y `releases/` |
| `CP_KUBECONFIG` (`~/.kube/clubpenguin-pi.yaml`) | todo el CD | Kubeconfig **dedicado** al Pi; nunca el predeterminado |
| `CP_NODE_NAME` (`penguin-pi`) | verificación del destino | Nombre esperado del único nodo |
| `CP_SSH_CONFIG` | túnel, Ansible | `ssh_config` alternativo (ver [agente-mac.md](agente-mac.md)) |
| `CP_AZURE_SUBSCRIPTION_ID` | opcional | Si se define, el login de Azure debe ser esa suscripción |

No hay claves para el token de Cloudflare ni para Moonshot/Kimi: el token del túnel es
un Secret de Kubernetes creado a mano (`cloudflared-token`), y ninguna prueba ni
etapa de CI recibe credenciales de proveedores de IA.

## Variables del pipeline (interfaz de Azure DevOps)

| Variable | Valor | Efecto |
|---|---|---|
| `CP_RELEASE_ENABLED` | `true` | En cada push a `main`, ejecuta `Release` (build ARM64, publicación, plan). No aplica. Si no existe, `Release` solo corre con `release`/`deploy` manuales |

## Parámetros de ejecución (manual)

| Parámetro | Por defecto | Uso |
|---|---|---|
| `deploy` | `false` | Añade `Apply` (aprobación del environment, plan exacto, verificación) |
| `release` | `false` | Fuerza `Release` sin aplicar |
| `rollbackTo` | vacío | `previous` o `releases/<UTC>-<commit12>.json`: no construye; planifica esa release (solo imágenes) |
| `azureServiceConnection` | `clubpenguin-azure` | Nombre de la service connection federada |
| `deploymentPool`, `deploymentAgent` | `clubpenguin-lab-mac`, vacío | Pool y **nombre del agente** (obligatorio: plan y apply deben ejecutarse en la misma máquina) |
| `backupMode` | `auto` | `auto` respalda solo si el plan toca datos; `always` siempre. No existe `never` |
| `pinTags` | vacío | `database=lab-002,web=lab-006`: reutiliza etiquetas ya publicadas (migración) |
| `rebuildSalt` | vacío | Cambiarlo fuerza etiquetas nuevas (p. ej. imagen base actualizada) |
| `allowDestructive` | vacío | Direcciones exactas de recursos que pueden borrarse/reemplazarse. Dejar vacío |
| `requirePublicEdge` | `false` | Si es `true`, falla el despliegue cuando la URL pública no responde |

## Dónde se guarda cada cosa

| Dato | Ubicación | Publicado |
|---|---|---|
| Plan guardado | `~/.clubpenguin-lab/runs/run-<BuildId>-r/plan/` (0700), solo en el agente | **Nunca** (puede contener secretos) |
| Datos de Terraform (`TF_DATA_DIR`) | misma carpeta | No |
| Backend y variables de la ejecución | misma carpeta (`config/`, 0600) | No |
| Evidencia (resúmenes, hashes, resultados) | misma carpeta (`evidence/`) → artefactos `release-evidence`, `deploy-evidence` | Sí, tras un filtro de secretos |
| Dump de PostgreSQL | `~/.clubpenguin-lab/backups/` (0700) y Blob `postgres/` | No como artefacto |
| Manifiesto de release | artefacto + Blob `releases/` | Sí (sin secretos) |
| Caché de proveedores Terraform | `~/.clubpenguin-lab/plugin-cache` | No (binarios públicos) |
