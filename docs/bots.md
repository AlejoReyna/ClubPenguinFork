# Pingüinos conectados en Glaciar

## Estado por capa (2026-11-04)

La migración de oficios y el cambio posterior de nombres y vestuario están
aplicados. La aceptación visual y conversacional humana sigue pendiente.

| Capa | Estado |
|---|---|
| Solicitado | Juanito en 110, DJ Cobra en 120 y Viterbos en 330 |
| Implementado y aplicado | Configuración y migración SQL de apariencia; conserva cuentas y contraseñas |
| Declarado en Terraform | tfvars privado fija `bots-002`, habilitado, `api`, 1 réplica; Secret externo. Cambio local pendiente: `fs_group_change_policy = OnRootMismatch` |
| Estado remoto | Plan revisado `run-local-202611040230004f37` aplicado; ConfigMap de release y Deployment actualizados; PVC conservado |
| Desplegado en el Pi | `bots-002`, 1 réplica lista, `Recreate`, `api`, nodo `alexis`; Juanito, DJ Cobra y Viterbos |
| Probado | 100 pruebas de bots; SQL en PostgreSQL aislado; `/readyz` 200, tres sesiones conectadas y roster AS2 con nombres y prendas correctos. Falta aceptación humana visual y de conversación |

El ledger del PVC volvió a tener permisos incompatibles al recrearse el pod:
`bots-002` entró en CrashLoopBackOff. Con autorización, se cambió únicamente
el fichero a 600 mediante un pod temporal sin descargas y se verificó que su
contenido no cambió. El pod de bots quedó listo con 4 reinicios del incidente.
El problema puede repetirse al recrear el pod; conservar el PVC y su ledger.
Se preparó en `bots.tf` `fs_group_change_policy = "OnRootMismatch"`: el directorio
del PVC observado tenía grupo 10001 y modo 2777, de modo que debería evitar la
recursión en ese estado. El cambio solo pasó `terraform validate` local; falta
un plan aprobado, su aplicación y comprobar un reinicio real. Si la raíz vuelve
a no coincidir, Kubernetes puede volver a cambiar permisos. No activar el
despliegue automático.

## Música (Spotify del jugador)

Los jugadores con Spotify conectado en «Mi puffle» pueden pedirle su propia
música a cualquier bot por su nombre («Cobra, pon despacito»). Está activado por
defecto en cada bot (`"music": false` lo apaga) y solo funciona si el proceso
tiene `BOTS_MUSIC_URL` y `BOTS_MUSIC_SECRET_FILE`. Ver
[puffle-sin-descarga.md](puffle-sin-descarga.md#música-con-los-bots-cadencedj-cobra-juanito-viterbos-y-los-que-vengan).


Las cuentas siguen siendo `baristaia`, `djia` y `pizzeroia`; sus nombres
visibles son Juanito, DJ Cobra y Viterbos. Los alias Barista, DJ y
Pizzero siguen disponibles; DJ Cobra también responde a Cobra. Prendas del
catálogo y archivos SWF existentes: delantal de café 262; audífonos 481; gorro
de chef 424 y delantal de pizza 263. La apariencia visual aún requiere revisión
con el cliente real.

`python3 scripts/prepare-bot-makeover.py` prepara un nuevo archivo privado en
`.local/bots/work-makeover.json` sin sobrescribir el original ni contactar el
Pi. `scripts/bot-makeover.sql` describe la actualización transaccional de
nombres, inventario y ropa y exige cuentas normales aprobadas y prendas válidas.
El SQL y el candidato se aplicaron en producción tras aprobación, con los bots
detenidos. El respaldo privado `.local/bots/makeover-backup.json` conserva el
Secret y los valores de nombres, prendas e inventario anteriores; no mostrarlo
ni versionarlo. El roster AS2 confirmó los nombres, el bit 8 y las prendas.
Falta comprobar el aspecto en el cliente y la conversación con consumo autorizado.
El aprovisionador histórico `create-bot-accounts.py --work-roles` todavía usa
los nombres anteriores. Su nueva guarda local rechaza cuentas renombradas antes
de escribir apodos o Secret; no es el procedimiento para esta migración.

## Zonas medidas

La posición que Houdini acepta no demuestra que sea transitable: `j#jr` entra
en sala, `j#grs` confirma el roster, `u#sp` difunde las coordenadas sin
validarlas y `u#h` mantiene la sesión. Los SWF están en
`vendor/legacy-media/media/play/v2/content/global/rooms/`.

**Coordenadas (2026-11-04)**, escenario 760x480, las que viajan en `j#jr`/`u#sp`.
El puesto es el centro:

| Rol | Sala | Zona x / y | Puesto | Fuente |
|---|---|---|---|---|
| BaristaIA | 110 | 300-360 / 290-350 | 330,320 | `coffee.swf`: suelo a la izquierda de la barra (barra x≥380, y 200-340) |
| Cadence (djia) | 120 | — | **322,168** | Anclada en cabina, sin baile automático (2026-11-05) |
| PizzeroIA | 330 | 365-430 / 258-290 | 397,274 | `pizza.swf`: delante del extremo derecho del mostrador, encima de las mesas; junto a `start_x/start_y` (400,278) |

Se obtuvieron de la máscara `block_mc` de cada SWF exportada con FFDec (relleno =
bloqueado, según `isPositionValid` de `engine.swf`) y se comprobaron en Ruffle con
un pingüino en cada esquina y en el centro. Están en
`bot_accounts.MEASURED_WORK_AREAS`; el generador las usa por defecto y crea una
configuración privada exclusivamente local, con credenciales nuevas, sin
sobrescribir un archivo existente. `--areas-file` permite sustituirlas por otro
JSON medido igual:

```bash
python3 scripts/prepare-work-bots.py
```

## Provisión comprobada en un banco desechable

`python3 scripts/test-work-provision.py` levanta Postgres, Redis, login, Glaciar,
Dash y web en una red Docker `--internal`, sin tirar de registros. El `kubectl`
del `PATH` es un shim: no abre `127.0.0.1:16443` ni lee
`~/.kube/clubpenguin-pi.yaml`. El kubeconfig del banco apunta a `127.0.0.1:1`.
Al terminar borra contenedores, credenciales y el Secret del shim.

Resultado de la corrida local (2026-11-04):

- La previsualización de `--work-roles` no crea cuentas ni Secret.
- `--execute` con el Deployment en 1 réplica termina sin cuentas y sin Secret.
- Con 0 réplicas crea 3 cuentas ordinarias. Nicknames visibles `BaristaIA`,
  `DJIA` y `PizzeroIA`. `approval_es` verdadero. El roster trae el bit 8
  (`Language.Es`) y el nombre exacto. El Secret del shim contiene esas tres
  identidades.
- Un segundo `--execute` crea 0 cuentas y conserva los mismos id.
- Tras reiniciar Glaciar, otro `--execute` reutiliza las mismas tres cuentas y
  el bit 8 sigue presente.
- La imagen local `clubpenguin/bots:bots-work-local`, con el código actual y
  proveedor simulado, caminó dentro de las zonas medidas:
  BaristaIA `(300,318)` y `(328,293)`; DJIA `(311,230)` y `(379,257)`;
  PizzeroIA `(415,260)` y `(385,274)`.
- Reiniciar ese proceso no creó cuentas. La red no tenía salida.

## Migración manual histórica a bots-002 (ya ejecutada)

Conservar el modo `api`, una réplica y `Recreate`. El nombre visible sale de
la cuenta del servidor. Sin `--work-roles` el aprovisionador sigue creando
GuiaIA, PescadorIA y ExploradorIA: no usar esa variante aquí.

Estos comandos son el registro histórico de la migración ya realizada. No repetirlos.
Sustituir `<CP_ACR_NAME>` por el valor privado de `deploy.env`. No imprimir el
Secret ni las contraseñas. No borrar el PVC `bots-budget`.

```bash
cd ~/theCode/clubpenguin-lab

# Publicar la imagen nueva. bots-001 queda para el rollback.
python3 scripts/build-images.py --components bots --tag bots-002 --platform linux/arm64
python3 scripts/test-containers.py --tag rt-origin-001 --component-tag database=lab-002 --web-tag lab-006 --bots-tag bots-002
python3 scripts/publish-images.py --components bots --tag bots-002 --registry "$CP_ACR_NAME"

python3 scripts/tunnel.py verify

# 1. Réplicas a 0 y esperar a que no quede ningún pod.
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin scale deployment/bots --replicas=0
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin rollout status deployment/bots --timeout=120s

# 2. Copia privada del Secret actual. No versionarla ni mostrarla.
umask 077
mkdir -p .local/bots
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin get secret bot-credentials -o yaml \
  > .local/bots/bot-credentials.bots-001.yaml
chmod 600 .local/bots/bot-credentials.bots-001.yaml

# 3. Cuentas nuevas y Secret nuevo. --execute exige 0 réplicas ya terminadas.
python3 scripts/prepare-work-bots.py
python3 scripts/create-bot-accounts.py --kubeconfig ~/.kube/clubpenguin-pi.yaml --expected-node alexis --work-roles
python3 scripts/create-bot-accounts.py --kubeconfig ~/.kube/clubpenguin-pi.yaml --expected-node alexis --work-roles --execute
```

`prepare-work-bots.py` no sobrescribe `.local/bots/work-credentials.json` si ya
existe. El `--execute` corrige los nicknames, entra a 110, 120 y 330, exige el
nombre exacto y el bit 8, y solo entonces hace el apply del Secret.

## Plan histórico de infraestructura (ya aplicado)

No hace falta editar `.tf`. El `terraform.tfvars` privado no declaraba los
bots. Con el default `bots_enabled = false`, el próximo plan intentaría quitar
el Deployment; el PVC tiene `prevent_destroy`, así que ese apply no es viable.
En el archivo privado, que git ignora, quedaron los cuatro valores que
coinciden con lo desplegado: `bots_enabled = true`, `bots_image_tag =
"bots-001"`, `bots_provider_mode = "api"`, `bots_replicas = 1`. Eso no se ha
aplicado ni se ha comparado con el estado remoto.

La migración, después de publicar y de bajar las réplicas, cambia solo
`bots_image_tag` a `"bots-002"` en ese archivo. El manifiesto que se pasa a
`deploy.py plan` tiene que fijar el resto de tags en lo que ya corre:
runtime `rt-origin-001`, database `lab-002`, web `lab-006`, bots `bots-002`.
`deploy.py plan` abre el túnel y lee el estado remoto; no se ha ejecutado.

Con el Deployment vivo a 0 y el estado todavía en 1, el plan refrescado debe
mostrar la imagen `bots-001` → `bots-002` y las réplicas 0 → 1. Puede actualizar
el ConfigMap `release-info` si el manifiesto trae otro commit. No debe
reemplazar el PVC `bots-budget`, ni tocar runtime, base de datos, web,
cloudflared, DNS, ACR ni `infra/cloud`. `--expect images-only` rechaza el
cambio de réplicas: esta migración no entra por el despliegue automático. El
Secret no está en Terraform.

```bash
# Después de publicar bots-002 y de dejar el Deployment a 0.
# El tfvars privado ya tiene enabled, api y replicas = 1; cambiar solo el tag.
python3 scripts/deploy.py plan --manifest .local/bots/manifest-bots-002.json
# Revisar la salida. Si el diff es el de arriba, el SHA-256 sale en el resumen.
python3 scripts/deploy.py apply --expect-sha256 <SHA256_DEL_PLAN>
```

Ejemplo de manifiesto local, no versionado. Los digest van después de publicar:

```json
{
  "schema": 1,
  "commit": "<SHA_DEL_COMMIT>",
  "created_utc": "<AAAAMMDDTHHMMSSZ>",
  "run": {"build_id": "manual-bots-002"},
  "components": {
    "runtime": {"tag": "rt-origin-001"},
    "database": {"tag": "lab-002"},
    "web": {"tag": "lab-006"},
    "bots": {"tag": "bots-002"}
  }
}
```

El apply de ese plan terminó correctamente; actualmente está desplegado `bots-002`.

## Rollback (no ejecutado)

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin scale deployment/bots --replicas=0
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin rollout status deployment/bots --timeout=120s
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin apply -f .local/bots/bot-credentials.bots-001.yaml
```

Devolver `bots_image_tag` a `bots-001`, dejar `bots_provider_mode = "api"` y
`bots_replicas = 1`, y aplicar el plan revisado. No borrar el PVC: el contador
de cuota vive ahí. Las cuentas nuevas pueden quedar en la base; no hace falta
borrarlas para volver a las anteriores.

## Aceptación en el Pi (después del despliegue; no ejecutada)

Con una sola réplica ya en `bots-002` y el Secret nuevo:

1. Entrar a Coffee (110), Night Club (120) y Pizza (330). El nombre visible
   debe ser BaristaIA, DJIA y PizzeroIA, cerca del puesto de su zona.
2. Escribir `Barista hola` en 110. Solo BaristaIA responde, en español y
   breve. Repetir con `DJ` en 120 y `Pizzero` en 330.
3. En 120, DJIA no debe afirmar que cambió la música. Un pedido de café o
   pizza se narra; no hay compra en el juego.
4. Cerrar el navegador. Las tres sesiones siguen. `/readyz` exige las tres en
   sala. `/metrics` no lleva texto de chat ni nombres humanos.
5. Reiniciar el pod solo con autorización. Vuelven las mismas cuentas a las
   mismas salas. El PVC `bots-budget` conserva el contador.
6. Una respuesta real de Kimi es otra prueba y gasta saldo. Esta aceptación
   puede usar el fallback local si el proveedor falla; eso no mide al modelo.

En la lectura del Pi se observó un Deployment de una réplica con `Recreate` y
modo `api`; conservar ese modo en cualquier plan. Una prueba local simulada no
evalúa la calidad del modelo ni autoriza gasto. La guía de instalación histórica
que sigue describe la versión anterior (`bots-001`) y no es un procedimiento
válido para activar los nuevos oficios.

La integración `scripts/test-containers.py --tag rt-origin-001 --component-tag
database=lab-002 --bots-tag bots-work-local` usa las zonas medidas y comprueba que cada
movimiento difundido cae dentro de la zona de su rol; no captura la apariencia
en Flash. También prueba un servidor HTTP falso en loopback, con retraso, errores
y cuota; durante el retraso verifica la lectura del chat y el movimiento. La red
interna no tiene salida y la clave es sintética. Verifica prompts
y rutas, no la calidad de las respuestas del modelo. La aceptación en el Pi debe entrar con un jugador a cada sala, comprobar el
nombre visible y el puesto, mencionar el alias de cada personaje y observar que
responde solo el destinatario. Cerrar el navegador y comprobar las tres sesiones;
reiniciar el proceso únicamente cuando esté autorizado y comprobar nuevamente
salas, identidades y métricas, incluyendo la estimación de humanos. La prueba
de respuestas reales de Kimi requiere autorización de consumo independiente.

La imagen desplegada `bots-002` mantiene las tres sesiones de oficios en
110, 120 y 330. Camina con lógica local y vuelve a autenticarse con la misma
cuenta si cae la conexión. La aceptación humana con el proveedor real sigue
pendiente y requiere autorización de consumo.

## Alcance y evidencia

`bots-002` se publicó y se aplicó al estado remoto. Las pruebas desechables
usaron el proveedor simulado; no acreditan la calidad de Kimi ni disponibilidad
permanente. Los nuevos nombres y prendas también se aplicaron; falta aceptación humana.

## Prueba local sin saldo

Desde la raíz del repositorio, con Docker del Mac encendido y las imágenes
`runtime:rt-origin-001`, `database:lab-002`, `web:lab-006` y Redis ya disponibles:

```bash
python3 scripts/build-images.py --components bots --tag bots-001 --platform linux/arm64
bash scripts/run-bot-tests.sh clubpenguin/runtime:rt-origin-001
python3 scripts/test-containers.py --tag rt-origin-001 --component-tag database=lab-002 --web-tag lab-006 --bots-tag bots-001
```

El arnés usa una red Docker interna sin salida, cuentas y almacenamiento
desechables y ninguna clave de proveedor. Arranca **otra imagen/proceso** para
los bots, verifica que sus fuentes coincidan con el árbol actual y elimina sólo
los recursos etiquetados por esa ejecución. La prueba espera un heartbeat real,
por lo que tarda más de un minuto. No consulta Kubernetes ni datos del Pi.

## Preparación y despliegue manual históricos de bots-001

1. Publicar la imagen ARM64 probada con `scripts/publish-images.py --components
   bots --tag bots-001 --registry NOMBRE_ACR`. Registrar su digest.
2. Revisar un plan de `infra/cloud` con `pull_repositories` que conserve
   `runtime`, `database`, `web` y añada `bots`. El token de descarga actual puede
   no tener permiso sobre el nuevo repositorio. Aprobar y aplicar únicamente el
   cambio revisado del scope map dedicado; no recrear ACR ni rotar contraseñas
   del registro para conceder ese permiso.
3. Abrir el túnel dedicado del Pi y verificar el nodo. Previsualizar con
   `python3 scripts/create-bot-accounts.py --expected-node NODO_REAL`.
   Añadir `--execute` sólo al decidir crear las tres cuentas y su Secret externo.
   El script guarda primero las contraseñas en `.local/bots/credentials.json`
   (0600), registra únicamente cuentas ausentes mediante Dash, verifica acceso
   normal y flags de moderación y crea `bot-credentials`. En ejecuciones
   posteriores reutiliza esas credenciales; si encuentra cuentas sin el archivo
   privado, se detiene sin restablecerlas ni adoptarlas. No activa cuentas.
4. Preparar y revisar un plan privado de `infra/cluster` con
   `bots_enabled=true`, `bots_image_tag="bots-001"` y
   `bots_provider_mode="simulated"`, conservando todos los valores efectivos
   actuales. Debe añadir recursos de bots y actualizar las reglas de Prometheus;
   no debe cambiar las imágenes del juego ni PostgreSQL. Aplicar el plan guardado
   únicamente después de revisarlo. No usar el antiguo tag compartido para bots.
5. Comprobar disponibilidad, digest ARM64, tres sesiones y métricas. Entrar al
   Pueblo y escribir `GuiaIA hola`. Repetir tras cerrar todos los navegadores.
   Esta primera fase sigue usando respuestas locales, sin Moonshot.

El Deployment usa una réplica y estrategia `Recreate` para no duplicar sesiones
de las mismas cuentas. `bots_replicas=0` permite detenerlo conservando el PVC de
cuotas. Ese PVC tiene `prevent_destroy`: deshabilitar recursos o borrarlo exige
revisar expresamente cómo conservar el contador de consumo.

## Prueba en el Pi (cast, dialogue, recall)

Pasos ordenados (Docker, publicación, tfvars, plan, clave Moonshot sin gasto
obligatorio): [bots-pi-prueba-vivos.md](bots-pi-prueba-vivos.md). Comprobación
previa: `bash scripts/pi-bots-vivos-ready.sh --expected-node alexis`.

## Dónde poner la API key

La clave va en el **Secret externo `moonshot-api`**, campo `MOONSHOT_API_KEY`.
No va en código, Terraform, argumentos de terminal ni conversaciones.
Con el túnel dedicado abierto, el siguiente script la solicita con entrada
oculta y la envía a Kubernetes por stdin, sin guardarla en un archivo local:

```bash
python3 scripts/set-moonshot-key.py --expected-node NODO_REAL --execute
```

Este comando modifica el Secret, no llama al proveedor y no cambia el modo de
los bots. Activar `bots_provider_mode="api"` necesita otro plan revisado y supone
autorizar consumo. La clave se inyecta sólo en ese modo; no se consulta desde
Terraform ni se incluye en la imagen. Los otros secretos históricos del
laboratorio pueden existir en sus estados privados; este diseño no los elimina.

## Inferencia y límites

La configuración usa `https://api.moonshot.ai/v1`, `kimi-k2.6`,
`thinking: {"type": "disabled"}` y `max_completion_tokens=120`. Consultado en la
[guía oficial de K2.6](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart) y la
[referencia de Chat Completions](https://platform.kimi.ai/docs/api/chat).
No se añaden temperatura ni herramientas. Sólo se toma texto del contenido de
la respuesta; no hay ejecución de código, shell, moderación ni herramientas
administrativas. El mensaje pasa por saneamiento de `%`, `|`, controles Unicode
y prefijos de comandos y se limita a 54 caracteres.

Por defecto: máximo global de **6 intentos HTTP/minuto y 100/día UTC**, contando
reintentos, dos peticiones simultáneas, timeout de 15 s y dos reintentos con
backoff para fallos transitorios. El contador sin chats ni secretos se guarda
en `/var/lib/bots/budget.json`, sobre un PVC de 64 MiB, y sobrevive a reinicios.
No es un límite monetario: el precio por token y otras aplicaciones que usen la
misma clave siguen influyendo en la factura. Si el contador no puede guardarse,
no se envía la petición. Se responde con una frase local al agotarse la cuota o
fallar el proveedor.

Cada bot tiene cola de ocho mensajes, seis segundos de cooldown por jugador,
dos segundos entre reservas y memoria de ocho turnos durante diez minutos,
con hasta 64 interlocutores. La memoria está en RAM; no se persisten chats.
El lector AS2 y los movimientos siguen trabajando durante una inferencia lenta.
No se responde a mensajes propios; conversaciones entre bots están desactivadas
por defecto y, si se habilitan, tienen intervalo y número de intercambios acotados.

## Operación y observabilidad

`/healthz` acredita el proceso; `/readyz` exige las tres sesiones dentro de sala.
`/metrics` expone conexión, sesiones, reconexiones, heartbeats, respuestas,
rechazos, errores, latencia y tokens reportados por el proveedor. Etiquetas
acotadas por índice de bot y causa; sin chats, credenciales ni nombres humanos.

Al habilitar bots, Terraform añade reglas de Prometheus para
`clubpenguin_bot_players` y `clubpenguin_human_players_estimated` además del total
`clubpenguin_players`. Los humanos son una **estimación por resta**, con desfase
entre scrapes; comprobar labels y frescura antes de usarla. Si falla el scrape
de bots, ambas reglas dejan de producir una muestra nueva; no se interpreta
un fallo como cero bots. No usar el
total como concurrencia humana probada: los bots también ocupan plazas del mundo.

El pod no monta token de ServiceAccount, corre como UID 10001, con filesystem
raíz de sólo lectura y sin capabilities. Egress permite DNS y login/mundo;
en modo API añade HTTPS público IPv4 excluyendo redes privadas. NetworkPolicy
no restringe por FQDN y sus permisos son aditivos. Debe comprobarse su efecto
en el clúster; esta declaración no demuestra aislamiento efectivo por sí sola.

Los archivos nuevos deben revisarse y versionarse para estar disponibles en un
checkout remoto. `release.py` incluye el componente opcional cuando su Dockerfile
está registrado en Git y añade la integración a las nuevas builds de bots.
No hay evidencia de ejecución remota del pipeline por haber pasado pruebas locales.
