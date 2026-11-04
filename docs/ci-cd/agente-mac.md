# Agente Mac de confianza y túnel al API privado

`Release`, `Apply` y `Cleanup` necesitan alcanzar la LAN y el API de Kubernetes del Pi
(`6443`, que **no** se abre a Internet). Un agente hospedado por Microsoft no puede.
Por eso hay un agente **autohospedado en un Mac de confianza**. Nunca ejecuta PR: las
etapas de PR usan agentes de Microsoft (ver
[azure-devops-y-github.md](azure-devops-y-github.md#qué-protege-el-agente-mac-y-qué-no)).

## Requisitos del Mac

| Herramienta | Versión | Para qué | Comprobación |
|---|---|---|---|
| macOS en Apple silicon | — | Build ARM64 nativo | `uname -m` → `arm64` |
| Docker Desktop (en ejecución, con el usuario del agente) + Buildx | Buildx ≥ 0.12 | Build y pruebas de imágenes | `docker buildx version` |
| Terraform | 1.17.x (`>= 1.11, < 2`) | Plan y apply de `infra/cluster` | `terraform version` |
| Azure CLI | reciente | Tareas `AzureCLI@2` | `az version` |
| kubectl | acorde a K3s 1.36 | Verificación y respaldos | `kubectl version --client` |
| Python | ≥ 3.9 (solo biblioteca estándar) | Scripts del repositorio | `python3 --version` |
| Ansible-core | 2.20.x sobre **Python 3.12–3.14** | Preflight del Pi | `ansible-playbook --version` |
| OpenSSH | — | Túnel y Ansible | `ssh -V` |

`scripts/install-tools.sh` instala Terraform, Helm y Ansible fijados (con checksum)
en una carpeta; en el Mac: `PYTHON=/opt/homebrew/bin/python3.14 bash
scripts/install-tools.sh ~/.clubpenguin-lab/tools` y añade esa ruta y `…/ansible/bin`
al `PATH` del agente (archivo `.path` del agente).

> **Ansible en este Mac:** `ansible-playbook` instalado con `pipx` falla (`module
> 'dataclasses' has no attribute '_is_type'`) porque su venv usa Python 3.15, que
> ansible-core 2.20 no soporta. Reinstala con `pipx reinstall ansible --python
> python3.14` o usa el instalador anterior.

## Usuario y aislamiento

Recomendado: una cuenta de macOS estándar dedicada (por ejemplo `azdo-agent`), no tu
cuenta personal. Así el agente solo ve su `~/.ssh`, su `~/.kube`, su Docker y su
`az`; no ve tus claves ni sesiones. Si usas tu propia cuenta, el aislamiento se reduce a
que `AzureCLI@2` use un directorio de configuración propio (lo hace por defecto: las
tareas de Azure no usan tu sesión `az`). Con cualquiera de las dos:

- Docker Desktop del usuario del agente en ejecución (`docker info` debe funcionar).
- Los scripts crean `~/.clubpenguin-lab/` con modo `0700` (planes, respaldos, caché).

## Instalar el agente

El registro del agente es la única operación que necesita un PAT, **una sola vez**, y no
se guarda: tras registrarse, el agente conserva sus propias credenciales.

1. Azure DevOps → User settings → *Personal access tokens* → New: alcance **Agent Pools
   (read & manage)**, caducidad de 1 día. Cópialo solo al pedirlo `config.sh`.
2. Organization settings → Agent pools → `clubpenguin-lab-mac` → *New agent* → macOS
   → descarga el paquete (`vsts-agent-osx-arm64-<ver>.tar.gz`).
3. Como el usuario del agente:

```bash
mkdir -p ~/azdo-agent && cd ~/azdo-agent
tar zxf ~/Downloads/vsts-agent-osx-arm64-*.tar.gz
./config.sh --url https://dev.azure.com/<organización> --auth pat \
            --pool clubpenguin-lab-mac --agent <nombre-del-agente> --acceptTeeEula
# pegar el PAT cuando lo pida; no pasarlo con --token ni dejarlo en el historial
./svc.sh install && ./svc.sh start
```

4. Revoca el PAT en Azure DevOps. Anota el nombre del agente: va en el parámetro
   `deploymentAgent` y fija **dónde** corren el plan y el apply.
5. Mantén el agente actualizado y el Mac con suspensión desactivada mientras haya
   despliegues.

## Acceso al Pi

```bash
# Como el usuario del agente
ssh-keygen -t ed25519 -f ~/.ssh/clubpenguin-pi -C azdo-agent
ssh-copy-id -i ~/.ssh/clubpenguin-pi.pub <usuario>@<host-del-pi>
ssh-keyscan -t ed25519 <host-del-pi> >> ~/.ssh/known_hosts   # y verifica la huella
python3 scripts/sync-kubeconfig.py --host <host-del-pi> --user <usuario>
```

Los scripts usan `StrictHostKeyChecking=yes`: la clave del Pi debe estar en
`known_hosts`. La clave SSH del agente da shell en el Pi (Ansible lo necesita para
el preflight), así que protégela con el sistema de archivos del usuario dedicado.

El kubeconfig dedicado (`~/.kube/clubpenguin-pi.yaml`, modo `0600`) apunta a
`https://127.0.0.1:16443`. **No** uses el contexto predeterminado de `kubectl`: en esta
máquina apunta al AKS anterior. Todos los scripts pasan `--kubeconfig` y
`--context default` explícitamente.

Configura `~/.config/clubpenguin-lab/deploy.env` ([ejemplo](deploy.env.example)).

### Si `~/.ssh/config` está roto

En este Mac, `~/.ssh/config` tiene líneas 20–22 con un comando `docker run … --token …`
pegado por error: `ssh` responde `Bad configuration option: docker` y **ningún** `ssh` lo
usa. Además, esas líneas contienen un token del túnel Cloudflare: quítalas y rota el
token (ver [acceso-publico.md](../acceso-publico.md#crear-o-rotar-el-token)). Mientras
tanto, `CP_SSH_CONFIG` apunta a un `ssh_config` mínimo con solo el `Host` del Pi; los
scripts lo pasan con `-F` a `ssh` y a Ansible.

## Túnel SSH al API de Kubernetes

El API del Pi (`127.0.0.1:6443` en el Pi) se alcanza por un reenvío local
`127.0.0.1:16443`. `scripts/tunnel.py`:

| Comando | Qué hace |
|---|---|
| `python3 scripts/tunnel.py status` | Dice si **este laboratorio** tiene su socket de control activo |
| `python3 scripts/tunnel.py start [--state-file F]` | Abre el túnel; **o reutiliza** uno que ya llegue al Pi (comprobado con el kubeconfig dedicado) |
| `python3 scripts/tunnel.py verify` | Comprueba el destino: servidor `https://127.0.0.1:16443`, un único nodo `penguin-pi`, ARM64, `Ready`, sin etiquetas de AKS, kubeconfig sin permisos de grupo/otros |
| `python3 scripts/tunnel.py stop [--state-file F]` | Cierra el túnel |

Para no interferir con otras sesiones:

- `start` no abre un segundo túnel si ya hay uno funcional (propio o ajeno); falla si
  el puerto `16443` lo ocupa otra cosa (`ExitOnForwardFailure`).
- Con `--state-file`, `start` anota si **esta ejecución** lo abrió y `stop` solo lo
  cierra en ese caso. El pipeline usa este modo, así que **nunca cierra el túnel que
  estés usando tú**. Sin `--state-file`, `stop` conserva el comportamiento original.
- El socket de control está en `.local/pi-ssh.sock` del repositorio; lo comparte
  quien use esta copia del repo.

Fallo observado (3 de noviembre de 2026): con el Pi apagado, `start` termina con
`ssh: connect to host … Operation timed out`, sin dejar archivo de estado ni procesos.
`verify` responde `the Kubernetes API is not reachable through the tunnel`.

## Qué se ejecuta en el agente

| Paso | Credenciales | Destino |
|---|---|---|
| `AzureCLI@2` (preflight, resolve, publish, plan, apply, record) | Login federado de la tarea, en su propio directorio de configuración; `addSpnToEnvironment: false` | Azure (ACR, Blob), API privado vía túnel |
| Build/pruebas ARM64 (`bash`) | Ninguna | Docker local, red `--internal` |
| Verificación y limpieza (`bash`) | Ninguna de Azure | Kubeconfig dedicado |

No ejecutes en este agente pipelines de otros repositorios ni PR no revisadas.
