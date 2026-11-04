# Observabilidad del despliegue

Prometheus y Grafana siguen siendo los de `monitoring/` (instalados por Helm desde
`infra/cluster/main.tf`). Este documento separa lo que **existe** de lo que **no**.

## Qué existe

- **Prometheus** (48 h, 3 GB, PVC de 5 GiB) con `kube-state-metrics`, `node-exporter` y
  el exportador del laboratorio (`clubpenguin_players{world="Glaciar"}`,
  `clubpenguin_redis_up`).
- **Grafana** con el dashboard «Raspberry Pi · Club Penguin Lab» (CPU, RAM, espacio,
  disponibilidad, reinicios, memoria por pod, jugadores, disponibilidad del exportador).
- **Dos reglas de alerta**, y solo dos (`monitoring/prometheus.yaml`):
  `RaspberryLowDiskSpace` (< 20 % libre durante 5 min) y `LabDeploymentUnavailable`
  (réplicas disponibles < deseadas durante 2 min en `clubpenguin`).
- **Alertmanager está desactivado**: las alertas son visibles en Prometheus
  (`/alerts`, métrica `ALERTS`) pero **nada las envía** a correo, chat ni teléfono.

## Qué no existe (no presentarlo como implementado)

- Notificaciones externas.
- SLO, presupuestos de error o alertas de latencia/errores del juego.
- Monitoreo del propio Pi cuando está apagado, ni de Cloudflare / la URL pública.
- Retención larga de métricas: 48 h.

## Señales que usa el despliegue

`scripts/deploy.py verify` (con tiempos límite) comprueba, en este orden:

| Comprobación | Fuente | Bloquea |
|---|---|---|
| Rollout de postgres, redis, login, glaciar, dash, exporter, web (+cloudflared si existe) | `kubectl rollout status --timeout=300s` | Sí |
| Todos los pods `Ready` y reinicios registrados | `kubectl wait`, `pods -o json` | Sí |
| Etiqueta y **digest** de cada contenedor = manifiesto de la release | `containerStatuses.imageID` | Sí |
| `/healthz`, `/`, `/servers.xml` por el servicio `web` | `GET` dentro de `deploy/dash` | Sí |
| WebSocket: origen público **sí**, origen desconocido **no** | `ws_probe.py` por nginx → proxy → login | Sí |
| Recorrido: registro por nginx, login, entrada a Glaciar, sala 100 compartida (2 cuentas) | `game-smoke.py` | Sí |
| `clubpenguin_redis_up == 1` y ninguna alerta `firing` | API de Prometheus vía el API de Kubernetes | Advertencia por defecto; bloquea con `--require-prometheus` |
| URL pública `/healthz` y WebSocket `wss://` | HTTPS desde el Mac | Advertencia; bloquea con `requirePublicEdge` |

Un HTTP 200 **no** prueba que el juego funcione; por eso el recorrido de juego y el
WebSocket son bloqueantes. Tampoco se prueba aquí «login desde otra red»: la prueba
pública atraviesa Cloudflare desde el Mac del agente.

## Dónde mirar cuando falla

| Qué | Dónde |
|---|---|
| Qué paso falló y su salida (con secretos enmascarados) | Registro de la ejecución en Azure DevOps |
| Plan evaluado (direcciones, acciones, rutas, etiquetas) | Resumen de la ejecución y `release-evidence/plan-summary.{md,json}` |
| Resultado de cada comprobación | `deploy-evidence/verify.json`, `pod-restarts.json`, `diagnostics.txt` (estado de pods y eventos; sin logs ni Secrets) |
| Respaldo previo | `deploy-evidence/backup-pre-deploy.json` (SHA-256, tamaño, restauración verificada) |
| Logs de la aplicación | `kubectl -n clubpenguin logs deploy/<login\|glaciar\|dash\|web>`; ningún log se publica como artefacto |
| Estado visual | Grafana por `port-forward` (README), panel de reinicios y disponibilidad |
| Alertas | Prometheus `/alerts` por `port-forward` |
| Bloqueo del estado | Blob `tfstate-clubpenguin` (lease); `terraform force-unlock` solo si no hay ejecución |

## Evidencia que conserva cada ejecución

| Artefacto | Contenido | Secretos |
|---|---|---|
| `release-evidence` | `release-manifest.json` (commit, etiquetas, digests, plataforma), `plan-summary.json/.md` (con SHA-256 del plan), `preflight.json` | No: valores nunca se copian; solo rutas de atributos y etiquetas de imagen |
| `deploy-evidence` | `apply.json`, `verify.json`, `pod-restarts.json`, `backup-pre-deploy.json`, `diagnostics.txt` si falló | No |
| Blob `releases/` | Manifiesto de cada release verificada | No |
| Blob `postgres/` | Dump y manifiesto de cada respaldo | **Sí** (datos de cuentas); contenedor privado |

`deploy.py evidence` rechaza publicar un archivo cuyo contenido parezca un token JWT,
un `Bearer`, una clave `sk-…`, y borra la carpeta de salida si lo detecta. El plan
guardado (`*.tfplan`), el estado, los kubeconfigs y los dumps **nunca** son artefactos.
