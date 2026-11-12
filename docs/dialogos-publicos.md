# Plan: «Diálogos» públicos en la top navbar

Estado: implementado en el repo, apagado por defecto (`dialog_feed_enabled = false`); falta
el ciclo de deploy. Este documento define el diseño para que los usuarios vean los diálogos de
los bots desde la navbar del juego, con un feed público filtrado (patrón panel-summary), y los
cambios de contrato que implica.

## Objetivo

Un botón «Diálogos» en la navbar superior del juego abre un panel con la conversación de
los bots (líneas habladas y eventos), en vivo o casi, servido same-origin desde `web`.
Los datos salen de la bitácora (`bots-dialog` PVC), ya activa en el Pi con
`bots_dialog_log_enabled 1`.

## Diseño elegido: sidecar en el pod `web` (patrón panel-summary)

`panel_summary.py` ya resuelve exactamente este problema para el panel de
infraestructura: un sidecar pod-local en el pod `web`, con nginx proxyeando una ruta
same-origin a `127.0.0.1:<puerto>`. Se replica el patrón para la bitácora:

```
[ bot pod: bots ] --escribe--> PVC bots-dialog <--lee (ro)--
[ web pod: nginx :8080 ] --/dialog/feed.json--> [ sidecar dialog-feed :8767 ]
        ^ navegador (same-origin, sin orígenes nuevos)
```

Descartado (por qué): un lector dentro del pod `bots` con proxy cross-pod estilo puffle
tocaría la imagen y el proceso de bots, añadiría ingress a un pod que hoy solo recibe del
juego, y agranda el radio de impacto. El sidecar en `web` no toca bots.

## Privacidad: qué sale y qué nunca sale

La bitácora tiene tres clases de datos. El feed es una **whitelist** de campos, no una
copia:

| Campo | ¿En el feed público? |
|---|---|
| `ts`, `room_id`, `speaker`, `kind`, `target_kind`, `target`, `text`, `source`, `scene_id`, `invite_id`, `reason`, `dest_room_id` | ✅ sí |
| `human_text` (mensaje del jugador que disparó la respuesta) | ❌ nunca |
| `player` (hash salado del jugador) | ❌ nunca |

Contrato nuevo (reemplaza el absoluto actual): la bitácora sigue **sin API de lectura
completa** y sigue sin ruta propia de Cloudflare; existe un único punto de lectura — el
sidecar — que solo publica la whitelist de arriba, con límites de tamaño.

## Cambios por pieza

### 1. `containers/runtime/dialog_feed.py` (nuevo) — el filtro

- `MODE=dialog` en `run.py`, HTTP en `127.0.0.1:8767` (solo localhost del pod).
- Monta `/var/lib/bots-dialog` (PVC, **read-only**) y `/etc/bots-dialog/enabled`
  (ConfigMap `bots-dialog-log`, read-only) para conocer el switch sin inventar otro canal.
- `GET /feed`: lee el archivo del día (y el anterior, opcional), conserva solo los
  campos de la whitelist, devuelve los últimos N eventos (p. ej. 200) más
  `{"enabled": <0|1>, "count": n, "dropped_lines": k}`.
- Tolerante a líneas corruptas (contador, nunca un 500 por una línea mala).
- Tope duro de tamaño de respuesta; no cachea nada en memoria más allá de un tail.
- `GET /healthz`.

### 2. `infra/cluster/web.tf` — infraestructura

- Sidecar `dialog-feed` en `kubernetes_deployment_v1.web`, imagen `runtime`,
  `MODE=dialog`, recursos mínimos, mismo security context que panel-summary
  (UID 10001, rootfs read-only, sin capabilities).
- Volúmenes nuevos en el pod `web` (ambos read-only):
  - PVC `bots-dialog` → `/var/lib/bots-dialog`.
  - ConfigMap `bots-dialog-log` → `/etc/bots-dialog`.
- `var.dialog_feed_enabled` (bool, **default false**): condición
  `!var.dialog_feed_enabled || (web_enabled && bots_enabled)`. Con `false` el sidecar no
  se despliega y nginx sirve 404 (el botón se oculta solo; ver frontend).
- `depends_on` del deployment `bots` (el PVC tiene que existir antes de montarlo).
- Nota de compatibilidad: el PVC es `ReadWriteOnce` + `local-path`. En k3s de un solo
  nodo (el Pi) web y bots comparten nodo y funciona. Si algún día hay más de un nodo,
  esto exige RWX o un diseño desacoplado — dejarlo escrito en la doc.

### 3. `containers/web/default.conf` — nginx

```nginx
location = /dialog/feed.json {
    proxy_pass http://127.0.0.1:8767/feed;
    add_header Cache-Control 'no-store';
}
location /dialog/ { return 404; }
```

Mismo estilo que `/infra/summary.json` (ruta exacta proxyeada, resto 404).

### 4. `containers/web/` — frontend

- Botón «Diálogos» en el `.dock` de la navbar (junto a Infraestructura, Spotify,
  Mi puffle), icono de burbuja de chat, `aria-haspopup="dialog"`.
- Panel `role="dialog"` (como `#infra-panel`: aside lateral, no interfiere con Ruffle ni
  con su WebSocket).
- Burbujas de chat: `speaker`, `text`, hora local, sala (`room_id`; con mapa
  room_id→nombre si se aprueba), etiqueta por `kind` (reply, greeting, scene, invite,
  accept, decline, travel).
- i18n ES/EN igual que el resto de la barra.
- Polling cada ~10 s **solo con el panel abierto**; `inert` al cerrar.
- Primer fetch: 404 ⇒ el botón se oculta; `enabled: 0` ⇒ panel muestra
  «Registro de diálogos apagado».
- `sw.js`: excluir `/dialog/feed.json` del caché del service worker (no-store).

### 5. Contratos y docs

- `docs/bots.md` (línea ~167 «The log is never exposed through Cloudflare Tunnel or any
  public Ingress») y `docs/operations.md` (líneas ~178-180): reemplazar por el contrato
  del feed: única lectura vía sidecar, whitelist de campos, sin `human_text`/`player`,
  sin ruta propia de Cloudflare.
- Nueva fila en la tabla de variables de `operations.md`: `dialog_feed_enabled`.

### 6. Tests y verificación

- Unit tests del feed (`scripts/test-bots.py` o suite del runtime): whitelist estricta,
  tolerancia a líneas corruptas, tope de N y de tamaño, switch off, `human_text`/`player`
  ausentes.
- **Test contractual**: la respuesta de `/feed` no puede contener las claves
  `human_text`, `player` ni `player_id` — falla el build si aparecen.
- `deploy.py verify`: nuevo check `[PASS] dialog-feed`: `/dialog/feed.json` responde 200,
  `enabled` correcto, y ninguna clave prohibida en el JSON.
- `test-containers.py --bots-tag`: la imagen check existente del dialog-log ya cubre la
  escritura; el feed se prueba contra un archivo JSONL de fixture.

## Ciclo de deploy (una vez aprobado el plan)

1. Cambios en el repo principal (runtime, web, tf, tests, docs) + CI verde.
2. Build de imágenes `web` y `runtime` con tags nuevos y manifest.
3. `plan` con `dialog_feed_enabled=true` → revisar (no debe borrar ni reemplazar nada;
   el deployment `web` se actualiza) → `apply` → `verify` (con el check nuevo).
4. Activar el switch si estuviera apagado (`patch configmap bots-dialog-log`) y probar
   un día con tráfico real de bots.
5. `record` + `cleanup` como siempre.

## Decisiones (fijadas 2026-11-12)

1. Se muestran todos los `kind`, con filtro Todo / Charlas (`reply`, `greeting`, `scene`) /
   Eventos (`invite`, `accept`, `decline`, `travel`) en el panel.
2. Mapa estático `room_id` → nombre (ES/EN) en `index.html`, copiado de
   `containers/bots/bots/data/rooms.json`; una sala sin nombre se muestra como «Sala N».
3. Hasta 200 eventos por respuesta (`?limit=` 0–200; la página sondea con `limit=0` al cargar).
4. Polling cada 10 s solo con el panel abierto y la pestaña visible.

## Detalles fijados al implementar

- Con el switch (`bots-dialog-log`) apagado, el feed responde `enabled: false` **sin eventos**:
  el mismo switch sirve para ocultar al instante la vista pública sin redeploy.
- Sin sidecar, nginx convierte el 502 en 404 y el botón no aparece (empieza `hidden` y solo se
  muestra si el sondeo responde 200).
- El sidecar corre como UID 10001 (dueño de los archivos `0600` que escribe bots), con el PVC
  `read_only` también a nivel de volumen y sin `fs_group` en el pod: el kubelet no re-asigna ni
  amplía permisos de la bitácora.
- Un objetivo `player` nunca se nombra, ni por hash: la página dice «un jugador».

## Esfuerzo estimado

- `dialog_feed.py` + tests: la mayor parte del trabajo (filtro, límites, tolerancia).
- Terraform + nginx: pequeño, patrón existente.
- Frontend: moderado (panel, i18n, polling).
- Docs + verify: pequeño pero obligatorio (es un cambio de contrato de privacidad).
