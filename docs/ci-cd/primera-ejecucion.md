# Primera ejecución: CI y CD

Orden recomendado. Cada fase es independiente: puedes quedarte en el CI para siempre.

## Fase A: primer CI (solo piezas 1 y 5)

Requisitos: [pieza 1](azure-devops-y-github.md#1-conexión-con-github-sin-pat-en-el-repositorio)
(GitHub ↔ Azure Pipelines) y, para bloquear merges, la
[pieza 5](azure-devops-y-github.md#5-protección-de-main-y-validación-de-pr).
No hace falta agente, service connection ni environment: `Release`, `Apply` y `Cleanup`
no se ejecutan en PR.

1. Antes de abrir la PR, en local: `bash scripts/validate.sh` (necesita Terraform,
   Ansible y Helm fijados; ver [agente-mac.md](agente-mac.md)) y, con Docker,
   `python3 scripts/build-images.py --tag t --components runtime,database,web --web-media stub`
   seguido de `python3 scripts/test-containers.py --tag t --web-tag t`.
2. Abre una PR hacia `main`. Debe aparecer el *check* del pipeline en la PR.
3. Resultado esperado:
   - `Validate`: ~5–10 min la primera vez (descarga de herramientas y proveedores),
     menos con caché.
   - `Test`: construye imágenes nativas **amd64** con medios de relleno (sin descargar
     1,7 GiB), registra dos cuentas sintéticas, comprueba login y sala compartida, el proxy
     WebSocket y la ruta nginx → proxy → login. Si solo cambió documentación,
     infraestructura o monitoreo, el job de contenedores se omite y la PR queda en verde.
4. Si falla: el log de cada paso; `Test` imprime las últimas líneas de los contenedores
   (con secretos enmascarados) y siempre limpia **solo** lo que lleva la etiqueta de la
   ejecución.
5. Activa la protección de `main` con el nombre exacto del check (sección 5.2).

## Fase B: primer CD (piezas 2, 3 y 4)

### B0. Prerrequisitos externos

- [ ] Pi encendido y alcanzable (el 3 de noviembre de 2026 no respondía al ping).
- [ ] Token del túnel Cloudflare **rotado** y `~/.ssh/config` sin las líneas con el comando
      `docker run … --token`. Ver [acceso-publico.md](../acceso-publico.md).
- [ ] Service connection `clubpenguin-azure`, pool, agente y environment creados
      ([guía](azure-devops-y-github.md)); agente en línea.
- [ ] `~/.config/clubpenguin-lab/deploy.env` en el Mac del agente, con **todas** las
      claves, incluida `CP_LAN_CIDR` real.
- [ ] En el Pi: Secret `cloudflared-token` con contenido (longitud > 0).
- [ ] Kubeconfig dedicado, `tunnel.py verify` en verde y clave del Pi en `known_hosts`.
- [ ] Respaldo reciente verificado: `python3 scripts/backup.py --reason before-first-cd`.

### B1. Adoptar el conector (obligatorio antes del primer apply)

El plan real del 3 de noviembre de 2026 propone **crear** `cloudflared` y su política,
que ya existen a mano. Si no se importan, el pipeline lo detecta y **bloquea**. Sigue
[adopcion-cloudflared.md](adopcion-cloudflared.md).

### B2. Release sin aplicar

Ejecuta el pipeline manualmente sobre `main` con:

| Parámetro | Valor |
|---|---|
| `release` | `true` |
| `deploymentAgent` | nombre de tu agente |
| `pinTags` | `database=lab-002` (u otra etiqueta ya publicada de la base de datos) |

`pinTags` evita que la **primera** release (con etiquetas por contenido) reinicie
PostgreSQL solo por cambiar el esquema de nombres. Las etiquetas fijadas deben existir
en el ACR; si no, falla antes de construir.

Qué ocurre y qué mirar:

1. *Preflight*: identidad Azure, túnel, `verify` del destino, Ansible (`aarch64`, 7 GB+,
   20 GiB libres, k3s activo, nodo Ready).
2. *Resolve*: tabla `componente · etiqueta · computed|pinned · build|reuse`.
3. *Build*: solo lo que falta, para `linux/arm64`, con pruebas aisladas nativas.
4. *Publish*: sube lo nuevo y registra digests; rechaza imágenes que no sean arm64.
5. *Plan*: guardado en el agente (nunca publicado). Lee el **resumen de la ejecución**
   (pestaña *Extensions*/resumen) o el artefacto `release-evidence/plan-summary.md`:
   recursos cambiados, rutas de atributos y etiquetas de imagen, y si exige respaldo.
   Debe decir `Backup before apply: not required` solo para cambios de frontend/monitoreo.
6. Si hay `BLOCKED:`, **no se puede aplicar** (adopción pendiente, borrado de un recurso
   protegido, namespace ajeno…). Corrige la causa; no uses `allowDestructive` salvo que
   entiendas exactamente el efecto.

### B3. Despliegue

Misma ejecución manual, ahora con `deploy = true` (y los mismos parámetros).
`Release` rehace el plan; `Apply` queda esperando la aprobación del environment.

1. Revisa el plan y aprueba en el environment.
2. `Apply` verifica el SHA-256 del plan, vuelve a evaluar la política y, si el plan toca
   datos, **toma y verifica un respaldo antes** (restaurado en una base temporal y leído).
3. `terraform apply <plan>` aplica exactamente lo revisado; si el estado cambió entre
   plan y apply, Terraform lo rechaza (plan obsoleto) y no se aplica nada.
4. `verify`: rollout de cada carga, pods `Ready`, etiquetas **y digests** de las imágenes
   en ejecución contra el manifiesto, HTTP por el servicio `web`, sondas WebSocket
   (origen permitido sí, desconocido no), recorrido de dos cuentas por nginx hasta una
   sala compartida, señales de Prometheus y, si se puede, la URL pública.
5. `record`: guarda el manifiesto en `releases/` de Blob, solo tras verificar.
6. `Cleanup`: borra el plan y cierra el túnel si lo abrió esta ejecución.

Evidencia: artefactos `release-evidence` y `deploy-evidence` (ver
[observabilidad.md](observabilidad.md)).

Tras el primer despliegue verificado: opcionalmente crea `CP_RELEASE_ENABLED=true` para
que cada merge a `main` construya, publique y planifique (nunca aplica sin `deploy`).

## Pendientes externos

Nada de esto lo crea ni puede comprobar el repositorio:

| Pendiente | Dónde |
|---|---|
| Conexión GitHub ↔ Azure Pipelines y pipeline creado | GitHub / Azure DevOps |
| Service connection federada, roles y credencial | Entra ID / Azure DevOps |
| Pool, agente Mac registrado y herramientas instaladas | Mac del operador |
| Environment con aprobación, exclusión mutua y branch control | Azure DevOps |
| Protección de `main` con el nombre exacto del check | GitHub |
| `deploy.env` en el agente, kubeconfig dedicado, SSH al Pi | Mac del operador |
| Pi encendido; rotación del token del túnel; `~/.ssh/config` corregido | Operador |
| Importar el conector y su política al estado | Operador (`deploy.py adopt`) |
| Primer `Release` y primer `Apply` reales | Azure DevOps |
| Decidir qué hacer con `.github/workflows/ci.yml` (despliegue paralelo anterior) | Repositorio |
