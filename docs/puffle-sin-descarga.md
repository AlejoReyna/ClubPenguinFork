# Puffle + Spotify sin descarga (diseño, 5 de noviembre de 2026)

Objetivo: adoptar un puffle, conectar Spotify, hablarle en el iglú y pedir
música **solo desde el navegador**, sin `companion.zip`, sin Python en la Mac y
sin `connect_server` en `:1455`.

Estado: **MVP implementado en `glaciar-dev` (modo lab, 5 de noviembre de
2026)**. Los PR 0–4 de §8 están hechos; los PR 5 y 6 siguen pendientes. La
página `/puffle/` ya no pide descargas. El zip queda en «Modo avanzado» solo para
Música de la Mac y Safari.

## Cómo usarlo (camino feliz)

Una sola vez, como operador:

1. En https://developer.spotify.com/dashboard → tu app → *Redirect URIs*, agrega
   `http://127.0.0.1:8080/puffle/spotify/callback` y guarda.
   **Alternativa** si ya tienes registrado `http://127.0.0.1:1455/spotify/callback`
   (o si Spotify responde «redirect_uri: Not matching configuration»): agrega
   `--spotify-redirect http://127.0.0.1:1455/spotify/callback` al paso 2. Un
   contenedor `glaciar-dev-spotify-loopback` publica `127.0.0.1:1455` y solo
   reenvía ese callback a `/puffle/spotify/callback`. No corre ningún proceso en
   la Mac fuera de Docker y `connect_server` ya no hace falta (si estuviera
   corriendo, chocaría por el puerto).
2. Enciende el puffle en el stack dev (también vuelve a cargar los cambios del
   código): `python3 scripts/glaciar-dev-puffle.py --players 101`. El Client ID
   se lee de `PUFFLE_SPOTIFY_CLIENT_ID` o de `~/.config/puffle-lab/spotify-client-id`.

Como jugador, sin salir del juego (todo pasa en un globo bajo el ícono de Spotify):

1. Entra a http://127.0.0.1:8080 (si entras por `localhost`, la página te cambia
   sola a `127.0.0.1`). Adopta un puffle y déjalo en tu iglú. Tiene que estar en
   el iglú, **sin pasearlo**: si lo paseas, no escucha.
2. Pulsa el ícono de Spotify (o **Mi puffle**) → **Obtener código** y escribe en
   el chat de tu iglú `puffle conectar <código>`. El globo se abre solo con
   **¿Es tu pingüino?**; pulsa **Sí, es mi pingüino**.
3. Pulsa **Conectar Spotify** (con una cuenta Premium). Se abre una ventana
   emergente con el login de Spotify. Al aceptar, **esa misma ventana pasa a ser
   open.spotify.com**, el globo avisa «¡Listo!» y el ícono se pone verde. El
   juego no se mueve.
4. Pon algo unos segundos en el reproductor y escribe en tu iglú: «pon
   despacito», «pausa», «sigue», «siguiente».

`puffle desconectar` en el iglú, o el botón del globo, retira el vínculo.
**Desconectar Spotify** borra el token del servidor; quita también la app en
spotify.com/account/apps. La página `/puffle/` sigue disponible como alternativa
a pantalla completa.

**Cómo funciona el globo:** el estado se consulta con `POST
/puffle/api/session/status` cada 6 s (cada 2 s mientras hay algo pendiente o el
globo está abierto). La ventana de Spotify se abre dentro del clic, para que el
navegador no la bloquee, y después se navega a la URL de autorización. El
callback en modo ventana (state `p.…`) responde una página mínima que avisa a
la pestaña del juego con un evento `storage` del mismo origen (no depende de
`window.opener`) y, si salió bien, hace `location.replace('https://open.spotify.com/')`.

## Música con los bots (Cadence/DJ Cobra, Juanito, Viterbos y los que vengan)

Cualquier jugador con Spotify conectado puede pedirle **su propia** música a
cualquier bot, llamándolo por su nombre en la sala del bot: «Cobra, pon
despacito», «oye Juanito, siguiente», «Viterbos pausa». El bot contesta con el
resultado («Puse «Despacito».») y la música cambia en el Spotify de **quien lo
pidió**.

- **Regla de autoridad:** el bot reenvía al host el id que el mundo asignó a esa
  línea de chat y las palabras del jugador sin el nombre del bot
  (`POST puffle:9104/internal/music`, con un secreto propio de los bots). El host
  usa el mismo parser determinista y los mismos límites por jugador que el
  puffle. El bot nunca elige música por su cuenta ni actúa por otro jugador, y
  una visita no puede tocar el Spotify de nadie más.
- **Privacidad:** si la frase es una orden de música, no llega al modelo del bot.
  Lo demás («Cobra, ¿qué tal la fiesta?») sigue el camino normal.
- **Bots nuevos:** tienen música por defecto. Para quitársela a uno, pon
  `"music": false` en su entrada de `.local/bots/work-credentials.json`.
- **Si el host está caído,** el bot contesta normal (la orden no se ejecuta).
- **Encendido en `glaciar-dev`:** `scripts/glaciar-dev-puffle.py` crea el volumen
  `glaciar-dev-puffle-bots` y `scripts/provision-glaciar-dev-bots.py
  --skip-register` lo detecta y configura `BOTS_MUSIC_URL` y
  `BOTS_MUSIC_SECRET_FILE`.
- **Pruebas:** `tests/test_music.py` (bots) y
  `test_bots_change_only_the_speakers_own_music` (host).
- **Pendiente para producción:** NetworkPolicy `bots → puffle:9104` y el Secret
  en Kubernetes. El modo lab sigue limitado a `PUFFLE_ALLOWED_PLAYERS`.

## Implementación

| Pieza | Archivo |
|---|---|
| Dispositivo de servidor: sesiones, pairing, OAuth, cifrado y worker | `production/puffle/host.py` (arranca junto al broker con `PUFFLE_HOST_MODE=lab`) |
| `SpotifyAuth` con redirect fijo y almacenamiento enchufable | `experiments/puffle_sim/spotify_auth.py` (`:1455` sigue igual) |
| `ServiceStatus` (403 de revocación frente a una caída) | `production/puffle/http.py` |
| Rutas `/puffle/api/session/*` y `/puffle/spotify/*` → `puffle:9104` | `containers/web/default.conf` |
| Página sin descargas e ícono de Spotify en el mismo origen | `containers/web/puffle.html`, `containers/web/index.html` |
| Encendido en `glaciar-dev` (volúmenes, secreto y clave generados en Docker) | `scripts/glaciar-dev-puffle.py` |
| Pruebas (8): pairing, lista del lab, OAuth atado a la sesión, cifrado, música en servidor, revocación, reinicio, CSRF y cookie | `tests/production/test_host.py` |

**Cambio respecto al diseño:** `puffle-host` no es un contenedor aparte. Corre
en el mismo contenedor `puffle`, en el puerto 9104, y habla con el broker por
loopback. nginx resuelve `proxy_pass` al arrancar y ya depende del nombre
`puffle`, así que un nombre nuevo habría tumbado `web` en cualquier stack sin
ese servicio. El broker no cambió (solo su `main` arranca también el host).
Esto implica que en el laboratorio el broker y los tokens comparten contenedor.

**Verificado en `glaciar-dev`:** pairing completo vía nginx (con el paso del
iglú simulado con `/internal/pair`), worker `online`, «pon despacito» →
«Conecta tu Spotify en la página del puffle.», charla → respuesta simulada, URL
de Spotify con `redirect_uri=http://127.0.0.1:8080/puffle/spotify/callback` y
PKCE S256, callback con `state` falso → `sp_error`. No hay tokens ni códigos en
los logs. **Pendiente:** el login real en Spotify y «pon despacito» con
Premium; requieren que Alexis agregue el redirect en el dashboard.

## 1. Lo que hay hoy (verificado en el código y en `glaciar-dev`)

- El **broker** (`production/puffle/broker.py`) solo hace de cola. Guarda en
  SQLite el hash del token de cada dispositivo y mantiene los trabajos en memoria.
  `/internal/submit` responde 503 si ningún dispositivo hizo `poll` en los
  últimos 4 s.
- **No hay worker embebido.** En el contenedor `puffle` nada reclama trabajos.
  Siempre hace falta un cliente emparejado que haga poll (hoy el companion:
  `pair` → `poll` → `active` → `result`, y por cada trabajo un subproceso
  `device_worker` con `SpotifyAgent` + ChatGPT).
- **En `glaciar-dev` la función está apagada por completo:**
  `glaciar-dev-puffle` es `alpine:3.20` con `sleep infinity`, un stub para que
  nginx resuelva el alias `puffle`, y `glaciar-dev-glaciar` no tiene
  `PUFFLE_ENABLED`. Por eso `puffle conectar …` se trata como chat normal y
  `/puffle/api/*` devuelve 502.
- El ícono Spotify de `index.html` consulta
  `http://127.0.0.1:1455/api/local-status`. Solo funciona si `connect_server`
  corre en la Mac.
- La red `glaciar-dev-net` no es `internal`, así que un contenedor puede salir a
  `api.spotify.com`. La red de `scripts/test-containers.py` no tiene salida:
  las pruebas usan HTTP falso.
- La imagen runtime ya trae `cryptography` 50.x, que sirve para cifrar con
  AES-GCM sin dependencias nuevas.

## 2. Datos de Spotify que condicionan el diseño (documentación oficial)

- Redirect: HTTP solo con loopback **literal** (`127.0.0.1` o `[::1]`).
  `localhost` **no** se permite. En loopback el puerto puede ser dinámico, pero
  la ruta debe coincidir exactamente.
  → `http://127.0.0.1:8080/puffle/spotify/callback` es válido sin `:1455`.
  Un host no loopback (`club.example.com`) necesita HTTPS y su propio redirect
  registrado.
- Modo desarrollo (feb./mar. 2026): el dueño de la app necesita Premium, hay
  como máximo 5 usuarios en lista y `GET /search` acepta `limit` ≤ 10
  (`limit=1` sigue siendo válido). Los endpoints `/me/player*` no figuran entre
  los eliminados.
- Web Playback SDK: Chrome, Firefox, Safari y Edge, en escritorio y móvil (iOS
  con limitaciones). Requiere Premium y no puede usarse en proyectos
  comerciales sin aprobación.
- Spotify no ofrece revocación por API: hay que borrar el token y quitar la app
  en spotify.com/account/apps.

## 3. Opciones

### A. «Companion como servicio» en el servidor (recomendada)

Un servicio nuevo, `puffle-host` (dentro del contenedor `puffle`, puerto 9104;
ver «Cambio respecto al diseño»), **hace de dispositivo** frente al broker con el mismo protocolo
público que usa hoy el companion. El broker no cambia.

```
navegador ─/puffle/ (pairing, OAuth, estado)─► nginx ─► puffle-host ──poll/result──► broker ◄─internal─ mundo
                                                         │
                                                         └─► api.spotify.com (token del jugador)
```

- **Pairing:** la página pide `session/pair`. `puffle-host` llama a
  `/puffle/api/pair` del broker, guarda el token del dispositivo **en el
  servidor** y le da al navegador el código y una cookie de sesión. El usuario
  escribe `puffle conectar <código>` en su iglú. La página muestra «¿Eres
  *Nickname*?» y, con el botón **Sí**, `puffle-host` llama a `/confirm`. Se
  conserva la doble confirmación juego + dispositivo; el «dispositivo» es ahora
  esa sesión del navegador.
- **OAuth Spotify:** `POST /puffle/spotify/connect` responde 303 hacia
  `accounts.spotify.com`, con PKCE y un `state` de un solo uso atado al
  **player id del pairing**, nunca a un dato del cliente.
  `GET /puffle/spotify/callback` hace el canje y guarda el refresh token
  cifrado (AES-GCM con el player id como dato asociado y la clave en un archivo
  de secreto). No queda nada en el navegador.
- **Worker:** por cada jugador emparejado corre un bucle `poll`, que mantiene
  vivo el lease. Cada trabajo se ejecuta con `companion.run_job` (`Relay`:
  timeout y respuestas fijas) y un único `SpotifyAgent` en proceso, de modo que
  los límites por jugador persisten entre trabajos. No se usa el subproceso
  `device_worker`: el host no ejecuta herramientas locales que haya que matar.
- **Estado del ícono:** `POST /puffle/api/session/status` (mismo origen y con
  cookie) devuelve `{paired, nickname, spotify: conectado|desconectado|sin_app}`.
- **Pestaña abierta:** no hace falta. Solo tiene que haber un dispositivo de
  Spotify activo: open.spotify.com, la app del teléfono o, de forma opcional, la
  propia página `/puffle/` con el Web Playback SDK (PR 5).

| | |
|---|---|
| Privacidad | Los tokens de Spotify viven en el servidor. En el laboratorio, ese servidor es el Docker de la propia Mac. El chat pasa por el servidor, igual que hoy pasa por el broker. |
| Premium | Obligatorio para controlar la reproducción (403 `PREMIUM_REQUIRED` ya tiene mensaje fijo). |
| Multijugador | El diseño es por jugador. Lo limitan Spotify (5 usuarios en modo desarrollo) y el cifrado/KEK de producción, que sigue pendiente. |
| Costo | Un contenedor más. Sin cuotas propias. |
| Complejidad | Media: servicio nuevo, unas 300 líneas reutilizando `run_job`, `SpotifyAuth` y `SpotifyWebAPI`. El broker no cambia. |
| Seguridad | La cookie es HttpOnly y SameSite=Strict, con comprobación de `Origin` y un encabezado propio en los POST. Los tokens no salen nunca del servidor. |

### B. La pestaña del navegador como dispositivo

La página `/puffle/` empareja, hace poll al broker desde JavaScript y llama
directamente a `api.spotify.com` con PKCE en el navegador. El token queda en el
almacenamiento de esa pestaña. Con el Web Playback SDK, la pestaña también
reproduce.

- A favor: el servidor no guarda ningún token de Spotify.
- En contra: **la pestaña tiene que quedar abierta**, porque si no `submit`
  responde 503. Habría que portar a JavaScript el parser de seguridad
  (`parse_command`, `_scrub`, límites), con el riesgo de que se desincronice
  del de Python, salvo que el servidor mande la intención ya parseada. El
  token queda expuesto a un XSS en el mismo origen que el juego. ChatGPT
  queda descartado desde el navegador: el canje de SIWC y la Responses API no
  están documentados para CORS.
- Es útil como plan B si no se aceptan tokens en el servidor.

### C. Companion actual en un contenedor del stack (solo de transición)

`connect_server` y `companion run --spotify` dentro de un contenedor, publicado
en `127.0.0.1:1455`, con las credenciales en un volumen.

- A favor: casi no hay que tocar código y los redirects actuales (`:1455`) de
  Spotify y ChatGPT siguen funcionando.
- En contra: es de un solo dueño (clave `local`), el pairing es por CLI
  interactiva (`input()`) y habría que reescribirlo de todos modos, y no sirve
  fuera del laboratorio. Se descarta como meta. Sirve de idea para ChatGPT
  (ver §5).

### Recomendación

**A**, en **modo lab**. B queda como alternativa si la privacidad pesa más que
dejar una pestaña abierta.

## 4. Qué va en el navegador y qué en el servidor (opción A)

| Navegador (100 %) | Servidor (`puffle-host`) |
|---|---|
| Página `/puffle/`: código, confirmar nickname, botones Conectar/Desconectar Spotify | Token del dispositivo ante el broker y bucle `poll`/`active`/`result` |
| Poll de **estado** cada 2–4 s (no de trabajos) | PKCE: `state`, verifier, canje y refresh del token de Spotify |
| Ícono verde en `index.html` mediante `session/status` | Refresh token cifrado por jugador. El access token vive solo en memoria |
| Web Playback SDK (opcional) | `SpotifyAgent`: parser, límites, `_scrub`, verificación en `/me/player` |

**Cómo se reparte el trabajo del companion:**

| Antes (companion) | Ahora |
|---|---|
| `pair` + `input()` | Página `/puffle/` y botón **Sí, es mi pingüino** |
| `poll`/`active`/`result` | Bucle en `puffle-host` (reutiliza `run_job`) |
| `device_worker` con ChatGPT o Spotify | El mismo subproceso, con `--config-dir` por jugador |
| `connect_server` en :1455 | Rutas `/puffle/spotify/*` en el mismo origen |
| `disconnect` por CLI | Botón en la página o `puffle desconectar` en el iglú (ya existe `/internal/revoke`) |

## 5. ChatGPT sin proceso local

- Para la música **no hace falta**: `SpotifyAgent` resuelve «pon …», «pausa» y
  «siguiente» con el parser determinista y nunca llama al modelo. El MVP puede
  usar `SimulatedProcess` (o silencio) para el resto de la conversación.
- Sign in with ChatGPT (token sharing open source) está pensado para clientes
  locales o para la VM autohospedada del propio usuario. En modo lab, con Alexis
  como operador y único usuario en su propio Docker, entra en ese caso de uso.
  Para terceros **sigue bloqueado** hasta tener la aprobación de OpenAI
  (`puffle-conversacion.md`).
- Callback: el único que está probado es `http://127.0.0.1:1455/auth/callback`.
  Para no depender de un puerto que OpenAI quizá no acepte, `puffle-host`
  publicaría `127.0.0.1:1455` y serviría allí solo `/auth/callback`. Así no hay
  proceso en la Mac, pero choca con un `connect_server` viejo que siga
  corriendo. Habría que probar si `127.0.0.1:8080/puffle/chatgpt/callback`
  también funciona.
- Queda para el PR 6, después del MVP.

## 6. Modo lab y producción

- `PUFFLE_HOST_MODE=lab` (el único que se implementa): `puffle-host` no
  arranca si `PUFFLE_ALLOWED_PLAYERS` está vacío o tiene más de 5 ids. Se
  aceptan solo pairings de esos jugadores; el resto recibe 403 al confirmar.
  Los tokens se guardan cifrados con una clave local del volumen.
- Producción: sin cambios respecto a `puffle-spotify.md`. Faltan la KEK
  gestionada, la retención de 6 meses, el consentimiento escrito y HTTPS con su
  redirect registrado, y la cuota extendida de Spotify no es viable para una
  persona. **No se habilita.**

## 7. Riesgos

1. **`localhost` y `127.0.0.1`:** la cookie y el callback deben compartir
   origen. `/puffle/` redirige a `127.0.0.1` si se entra por `localhost`.
2. **Sin dispositivo activo:** Spotify responde `NO_ACTIVE_DEVICE` y el puffle
   dice «Abre Spotify en tu compu o el reproductor web.». Con el PR 5 (SDK) la
   propia página puede ser ese dispositivo.
3. **Límites:** en el companion, `device_worker` es un proceso nuevo por
   trabajo, así que el límite de `SpotifyAgent` se reinicia cada vez. En
   `puffle-host` el agente vive en proceso y el límite sí se aplica, además del
   que pone el broker.
6. **Log de errores de nginx:** si el host está caído, el `error_log` puede
   registrar la URL del callback con el `code`. El código es de un solo uso y no
   sirve sin el verifier PKCE, que solo existe en memoria del host.
7. **Kubernetes:** la NetworkPolicy debe permitir `web → puffle:9104` y la
   salida de `puffle` a `api.spotify.com` y `accounts.spotify.com`. No se tocó
   `infra/`, porque el modo lab es solo para `glaciar-dev`.
4. **Carga del poll:** 32 bucles × 2 req/s al broker, en red interna. Para el
   laboratorio basta. Long-poll queda como mejora futura.
5. **Términos:** el Web Playback SDK no se puede usar comercialmente, y se
   mantiene la regla de no mandar al modelo contenido de Spotify (`_scrub`).

## 8. Plan por PRs pequeños

| PR | Contenido | Prueba |
|---|---|---|
| 0 | Encender el puffle en `glaciar-dev`: `puffle` con imagen runtime `MODE=puffle`, archivo de secreto y `PUFFLE_ENABLED=true` en el mundo y el broker. El companion actual se mantiene. | `puffle conectar` responde. `socket_journey` en verde. |
| 1 | `production/puffle/host.py`: sesiones con cookie, pairing de servidor contra el broker y `session/status`. Rutas nginx `/puffle/api/session/*` → `puffle-host:9104`. | Unitarias con un broker en memoria. Cookie, `Origin` y CSRF. |
| 2 | Spotify OAuth en el mismo origen: `SpotifyAuth(config_dir, redirect)` parametrizado, cifrado AES-GCM y `/puffle/spotify/{connect,callback,disconnect}`. | HTTP falso: el `state` atado al jugador, el `state` reutilizado se rechaza, no hay tokens en logs. |
| 3 | Worker en `puffle-host`: bucle por jugador + `run_job` + `device_worker --spotify --config-dir`. | Banco de Docker con `SimulatedSpotify`: «pon X», «pausa», «siguiente», y una visita no puede ordenar nada. |
| 4 | UI: nuevo `puffle.html` sin pasos de descarga e ícono de `index.html` con `session/status`. El zip pasa a «modo avanzado». Se actualizan los docs. | Prueba manual con la cuenta Premium de Alexis y open.spotify.com. |
| 5 (opcional) | Web Playback SDK en `/puffle/` como dispositivo «Puffle Glaciar». Necesita los scopes `streaming` y un endpoint de access token de corta vida. | Safari o Chrome con Premium. |
| 6 (opcional) | ChatGPT en `puffle-host` (modo lab, callback en `:1455` publicado por Docker). | Inferencia real con la cuenta de Alexis. |

## 9. Pasos que hará Alexis una sola vez (camino feliz final)

1. En el dashboard de Spotify, añadir el redirect
   `http://127.0.0.1:8080/puffle/spotify/callback`.
2. Poner el Client ID en el entorno del stack dev (`PUFFLE_SPOTIFY_CLIENT_ID`) y
   su player id en `PUFFLE_ALLOWED_PLAYERS`.
3. En el juego: adoptar un puffle, abrir **Mi puffle**, escribir el código en el
   iglú, confirmar, pulsar **Conectar Spotify**, abrir open.spotify.com y decir
   «pon despacito».
