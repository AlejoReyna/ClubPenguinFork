# Modo desarrollo local (`glaciar-dev`)

Stack Docker en tu Mac: juego en el navegador (Ruffle), tres bots de trabajo con
LLM simulado por defecto, panel **Diálogos** y montajes del árbol de código para
iterar sin reconstruir imágenes en cada cambio.

## Requisitos

- Docker Desktop
- Imágenes ARM64 locales (en Apple silicon son nativas):

```bash
python3 scripts/fetch-sources.py --only houdini,dash,ruffle
python3 scripts/build-images.py --tag glaciar-dev --components runtime,database,web,bots
```

La primera build con media completa tarda; usa el caché de `vendor/vanilla-media`
si ya lo tienes.

`glaciar-dev.py up` monta `vendor/vanilla-media/media` en el contenedor web (incluye
`club_penguin.swf`). Los SWF móviles parcheados (`engine`, `login`, `create`) se
conservan desde `.local/glaciar-dev/patched-swfs/`. Sin vendor, Ruffle falla al cargar.

## Arranque rápido

```bash
python3 scripts/glaciar-dev.py up
python3 scripts/glaciar-dev.py bots          # Juanito, Cadence, Viterbos (simulated)
```

Abre **http://127.0.0.1:8080** (Spotify y OAuth exigen `127.0.0.1`, no `localhost`).

Comandos útiles:

| Comando | Qué hace |
|---------|----------|
| `glaciar-dev.py status` | Estado de contenedores |
| `glaciar-dev.py down` | Para todo el prefijo |
| `glaciar-dev.py bots --skip-register` | Reinicia bots sin volver a registrar cuentas |
| `glaciar-dev.py watch` | Hot-reload (otra terminal) |

## Pingüinos IA

### Tres bots de trabajo (por defecto)

`prepare-work-bots.py` crea `.local/bots/work-credentials.json`. `bots` los registra
en Dash y levanta el servicio en modo **`simulated`** (sin Moonshot).

### Cast completo (23 fichas)

```bash
python3 scripts/prepare-work-bots.py --cast containers/bots/cast \
  --output .local/bots/cast-credentials.json
python3 scripts/glaciar-dev.py bots --cast
```

La primera vez registra muchas cuentas; las siguientes: `--skip-register`.

### API real (Moonshot)

```bash
export MOONSHOT_API_KEY='...'
python3 scripts/glaciar-dev.py bots --provider api
```

Respeta el ledger de gasto en `BOTS_BUDGET_PATH` dentro del contenedor.

## Hot reload

No hay recarga en caliente dentro del proceso Python de Houdini ni del cliente
Flash; el servidor de bots tampoco recarga módulos solos.

| Qué editas | Qué pasa con `glaciar-dev.py watch` |
|------------|-------------------------------------|
| `containers/web/index.html`, `puffle.html`, SW | Solo refresca el navegador (bind mount) |
| `containers/web/default.conf` | `nginx -s reload` en el contenedor web |
| `containers/bots/bots/*.py` | `docker restart glaciar-dev-bots` (~2–5 s) |
| `containers/runtime/socket_proxy.py` | Reinicia `glaciar-dev-webproxy` |
| `containers/runtime/dialog_feed.py` | Reinicia `glaciar-dev-webdialog` |
| Código Houdini / parches en runtime | Rebuild de imagen `runtime` o reinicio del mundo |
| SWF / media del juego | Rebuild de imagen `web` con media completa |

**Plan si quisieras recarga más fina (no implementado):**

1. **Bots:** `watchfiles` + `exec` dentro del contenedor, o un modo `BOTS_RELOAD=1`
   con `importlib.reload` limitado a `brain`/`director` (frágil con asyncio).
2. **Web:** ya está resuelto con montajes + `watch`.
3. **Runtime / mundo:** volumen con `/app/houdini` parcheado y reinicio solo de
   `glaciar-dev-glaciar` (más lento; rompe sesiones conectadas).
4. **Cliente Ruffle:** solo refresco del navegador; parches AS2 pasan por
   `build-puffle-client.py` y rebuild de `web`.

Para la mayoría de cambios en IA, `watch` + reinicio de bots es suficiente.

## Puffle y Spotify

Opcional, mismo stack:

```bash
python3 scripts/glaciar-dev-puffle.py --players <tu_penguin_id>
```

Ver el docstring de `scripts/glaciar-dev-puffle.py` (Spotify, pairing, música con bots).

## Comando `/party` (solo desarrollo)

Con `LAB_PARTY_ENABLED=true` (activado por defecto en `glaciar-dev.py`), un jugador
autorizado en la **disco (sala 120)** escribe `/party` en el chat. El servidor valida
cupo (6 bots en la pista) y publica la escena en el volumen compartido
`/var/lib/lab-party/scene.json`. El servicio de bots ejecuta la coreografía: cinco
invitadas con bikini y peluca, coqueteo por turnos, círculo al bailar el invocador y
emotes de cariño escalonados (`u#se` 1, 3 y 4).

**Primera vez (cuentas e inventario):**

```bash
python3 scripts/prepare-work-bots.py
python3 scripts/prepare-party-guests.py
python3 scripts/glaciar-dev.py up
python3 scripts/glaciar-dev.py bots
# Registrar las dos cuentas nuevas y aplicar SQL (ver provision-glaciar-dev-bots)
docker exec -i glaciar-dev-postgres psql -U houdini -d houdini < scripts/party-guests.sql
```

Variables útiles: `LAB_PARTY_PLAYERS` (lista de IDs permitidos, vacío = cualquier humano),
`LAB_PARTY_BUS_PATH` (ruta del JSON compartido).

## Panel Diálogos

`glaciar-dev.py up` crea el sidecar `webdialog` y el volumen `glaciar-dev-bots-dialog`.
El switch está en `.local/glaciar-dev/dialog-enabled` (`true` por defecto). Los bots
escriben el JSONL; nginx expone `/dialog/feed.json`.

## Persistencia

- Contraseña de Postgres estable en `.local/glaciar-dev/runtime.env`
- Datos del juego en el volumen `glaciar-dev-postgres-data` (contenedores nuevos
  creados con `up --recreate` sin borrar volúmenes)
- El stack que ya tenías en Docker conserva su Postgres interno hasta que migres
  al volumen nombrado

## Relación con CI

`scripts/test-containers.py` levanta una red **aislada** sin Internet (seguro para
CI). `glaciar-dev` usa red normal para OAuth/Spotify y desarrollo interactivo; no
lo uses para pruebas que deban evitar APIs de pago.