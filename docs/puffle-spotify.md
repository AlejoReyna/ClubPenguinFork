# Puffle y Spotify del dueño

El dueño de un iglú le escribe a su puffle en el chat («pon Despacito»,
«siguiente», «pausa») y el puffle controla **el Spotify de ese dueño**. La
confirmación sale como globo `puffletalk` de 48 caracteres o menos.

## Estado (4 de noviembre de 2026)

| Pieza | Implementado | Declarado en Terraform | Desplegado | Probado funcionalmente |
|---|---|---|---|---|
| Parser determinista y validación de intención (`spotify.py`) | Sí | No | No | Sí, pruebas unitarias |
| `SpotifyAgent`: límites, bloqueo por jugador, cancelación, depuración del contexto | Sí | No | No | Sí, pruebas unitarias, de Houdini y banco simulado |
| `SpotifyWebAPI` (Web API real) | Sí | No | No | Solo con HTTP falso; **nunca contra Spotify** |
| PKCE local (`spotify_auth.py`) y botón en :1455 | Sí | No | No | Solo con HTTP falso y render local; **sin login real** |
| Relay con `--spotify` (un dueño, cuenta `local`) | Sí | No | No | No (requiere la app de Alexis) |
| Proveedor simulado en el banco (`--provider spotify-sim`) | Sí | No | No | Sí, banco Docker desechable |
| Vinculación jugador↔cuenta y credenciales cifradas por jugador | **No**, solo diseño | No | No | No |
| Pruebas visuales dueño/visita | No | No | No | No |

Nada de esto está en el clúster, la Pi/K3s ni AKS.

## Fuentes oficiales consultadas

- Iniciar o reanudar reproducción (Premium, `user-modify-playback-state`, 204/401/403/429): https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback
- Flujo PKCE: https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
- Redirect URIs (loopback con `127.0.0.1`, `localhost` no permitido): https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
- Modos de cuota (desarrollo: 5 usuarios en lista; extendido: solo organizaciones): https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Cambios de febrero de 2026 al modo desarrollo: https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide y https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- Términos para desarrolladores (incluyen restricciones sobre usar contenido de Spotify con modelos de IA): https://developer.spotify.com/terms
- Web Playback SDK y navegadores: https://developer.spotify.com/documentation/web-playback-sdk ; reproductor web: https://support.spotify.com/us/article/web-player-help/
- Sign in with ChatGPT, limitaciones: https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations

## Diseño

1. **Autoridad.** El puente solo pasa al motor el chat de un dueño habilitado,
   en su propio iglú, sin mute y con conexión legítima (`select_owned_puffle`).
   El chat de visitas, bots, comandos `!` y otras salas no llega al agente.
2. **Intención acotada.** `parse_command` reconoce `play`, `pause`,
   `resume`, `next` y `previous` en español. La búsqueda tiene un máximo de
   80 caracteres y se limpia. Se rechazan negaciones, frases largas sin
   palabra musical y órdenes incrustadas en otra frase. Al modelo **no** se le da una
   herramienta de Spotify: un texto inyectado no puede disparar acciones.
   `validate_intent` sirve si en el futuro el modelo propone una intención:
   solo se acepta si coincide con el último mensaje del dueño.
3. **Privacidad con el modelo.** `_scrub` cambia por `(acción de música)` las
   órdenes y confirmaciones antes de enviar el contexto al modelo. Así, los nombres de
   canciones no se mandan a OpenAI.
4. **Ejecución.** El agente aplica un intervalo mínimo (2 s) y un tope de 8 acciones por
   minuto por jugador, más un bloqueo por jugador. Justo antes de actuar
   comprueba que la sesión sigue activa; si el dueño salió, no hace nada.
5. **Web API.** El agente elige el dispositivo activo, o el único disponible. Para reproducir, busca
   con `limit=1` y luego llama a `PUT /me/player/play` con `uris`. Si recibe 401, renueva el token una
   vez. Después comprueba el resultado con `GET /me/player`. Si no puede confirmarlo,
   responde «Se lo pedí a Spotify, sin confirmar.».
6. **Errores con mensaje fijo:** sin conexión, sin Premium, sin dispositivo,
   429/cuota, sesión expirada, canción no encontrada, error y espera entre órdenes.

## Laboratorio local (lo que está implementado)

- Credenciales: `~/.config/puffle-lab/spotify-credentials.json`, modo 0600,
  fuera del repo. Se usa PKCE sin secreto de cliente.
- Client ID: `PUFFLE_SPOTIFY_CLIENT_ID` o `~/.config/puffle-lab/spotify-client-id`.
  No es secreto, pero tampoco se versiona.
- Redirect: `http://127.0.0.1:1455/spotify/callback`. Scopes:
  `user-modify-playback-state user-read-playback-state`.
- Desconectar borra el archivo local. Spotify no ofrece revocación por API, así que
  también hay que quitar la app en https://www.spotify.com/account/apps/.
- El relay es de un solo dueño (`--spotify`, clave `local`). No sirve para varios jugadores.
- Con `--spotify`, el banco manual habilita exclusivamente al dueño visual de
  prueba, incluso si no se activa `--allow-local-actions`. Esto evita que otro
  jugador sintético controle la cuenta local. La suite pasó 88 pruebas sin red.

### Pasos para Alexis (él mismo, con su cuenta)

1. Entra a https://developer.spotify.com/dashboard y crea una app («Puffle
   lab»). En *Redirect URIs* agrega `http://127.0.0.1:1455/spotify/callback`.
   En las APIs marca **Web API**. Acepta los términos.
2. Copia el **Client ID** (no el secreto) en
   `~/.config/puffle-lab/spotify-client-id`, una sola línea.
3. Abre https://open.spotify.com en Safari (con DRM activado) o en Brave,
   inicia sesión y reproduce algo unos segundos para que el navegador quede como dispositivo.
   Spotify de escritorio no está instalado en la Mac.
4. Reinicia `connect_server` y el relay con `--spotify`. Esto cierra el banco
   manual actual y se pierde la conversación efímera. Abre
   http://127.0.0.1:1455 y pulsa **Conectar mi Spotify**.
5. Lanza el banco manual con
   `--provider relay --spotify --visual-swf … --browser-python … --hold …`.
   En tu iglú escribe «pon Despacito» y luego «siguiente».

Requisito: la cuenta debe ser **Premium**.

## Producción (solo documentación; nada desplegado)

### Viabilidad
- En modo desarrollo solo hay 5 usuarios en lista y el dueño de la app necesita Premium.
  Por encima de eso, la API responde 429 o rechaza a los usuarios.
- La cuota extendida solo se concede a **organizaciones** (no a personas
  desde mayo de 2025), con unos 250 k MAU y una revisión de hasta 6 semanas. Para
  un proyecto personal, la producción abierta a terceros **no es viable**. La
  demo pública debe quedarse en modo desarrollo con usuarios en lista.
- Sign in with ChatGPT para terceros está en vista previa y requiere aprobación
  de OpenAI para usar la cuota del plan de cada usuario. El modo de
  terceros alojado no está habilitado para este proyecto.

### Vinculación y consentimiento
- La cuenta de Spotify se vincula al **id de jugador** del servidor, nunca a
  un dato que mande el cliente. El flujo se inicia desde la sesión web
  autenticada del jugador, con un `state` de un solo uso atado a ese id y con expiración de 10 min.
- La pantalla explica qué hace el puffle, qué ve la visita (solo el globo) y
  que el nombre de la canción no se manda al modelo. Desconectar borra la
  credencial y muestra el enlace a spotify.com/account/apps.
- El motor ya llama a `evaluate_for(binding.player, …)`: cada jugador usa
  solo su propio backend.

### Credenciales cifradas por jugador
- Se guarda solo el refresh token, cifrado con envelope encryption. Para cada jugador se genera una DEK aleatoria
  (AES-256-GCM, con el id de jugador como dato asociado), cifrada a su vez por una
  KEK en Azure Key Vault (AKS) o, en K3s, por una clave en un Secret de
  k8s montado solo en el servicio de puffles, con cifrado en reposo de etcd.
- El access token vive solo en memoria, con caducidad de una hora. No hay logs con tokens.
- Se borra al desconectar, al recibir `invalid_grant` o tras 6 meses sin uso.

### Límites y privacidad
- Por jugador: 2 s entre órdenes y 8 por minuto. A nivel global, el servicio respeta
  `Retry-After` y corta el tráfico al recibir 429 o `QUOTA_EXCEEDED`.
- No se almacenan el historial de reproducción ni las búsquedas. La conversación es efímera
  y se borra al salir. La visita ve el globo; eso se explica en el consentimiento.

### Pruebas pendientes antes de producción
- Prueba visual en Ruffle: el dueño pide una canción y ve el globo; la visita ve
  el mismo globo y no puede ordenar nada. Hay que capturarla con dos navegadores.
- Una cuenta real en lista (la de Alexis), con Premium y con el reproductor web como dispositivo.
- Casos reales a reproducir: sin Premium, sin dispositivo y token expirado.

### Plan de imagen
- Los archivos `spotify.py` y `spotify_auth.py` ya van en `experiments/puffle_sim` y no
  añaden dependencias, porque usan solo la biblioteca estándar. Hay que construir
  `clubpenguin/runtime:<sha>` en CI con las pruebas Docker del puffle como
  puerta de paso.

### Plan de despliegue (espera aprobación)
1. PR con el código y las pruebas. CI construye la imagen y la etiqueta por sha.
   El despliegue automático sigue apagado.
2. Declarar en Terraform el Secret o la referencia a Key Vault, y la
   variable `PUFFLE_SPOTIFY_ENABLED=false`. Revisar el `plan`. El `apply` lo
   ejecuta una persona.
3. Desplegar la imagen con la función apagada y verificar que el juego no cambia.
4. Encenderla solo para los jugadores en lista y hacer la prueba visual.
5. **Rollback:** poner `PUFFLE_SPOTIFY_ENABLED=false` (efecto inmediato), o volver a
   la etiqueta de imagen anterior. Las credenciales no se borran en el rollback.
