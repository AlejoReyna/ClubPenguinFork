# Puffle conversador: investigación y prototipo sin red

Estado: **integración experimental en un banco desechable; no lista para producción**. Alexis conectó su cuenta mediante OAuth. Se hizo una única inferencia real autorizada desde el iglú de una cuenta sintética: el relay informó respuesta y una visita recibió el paquete `puffletalk`. Autorizó continuar el trabajo y el despliegue posterior; el plan concreto para instalar el puffle en el Pi todavía no está listo ni aprobado.

## Conexión oficial candidata

La opción preferida para texto es [Sign in with ChatGPT, uso del plan](https://developers.openai.com/siwc/token-sharing-open-source) y la [Responses API pública](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), directamente y sin herramientas. No hay que iniciar un agente con shell para obtener texto. OpenAI documenta que usuarios **Plus elegibles** pueden utilizar el plan en aplicaciones participantes; identidad y permiso de usar el plan son cosas distintas ([inicio rápido](https://developers.openai.com/siwc/quickstart)). La conexión de la cuenta de Alexis y una inferencia real en el banco se completaron; esto no valida disponibilidad futura ni el alojamiento de cuentas de terceros.

La documentación cubre clientes abiertos/locales y describe el caso de una [VM autohospedada del usuario](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms), donde OAuth se completa localmente y después se transfieren credenciales protegidas. Esta guía no demuestra que sea admisible centralizar cuentas de todos los jugadores en el servidor del operador. Para aplicaciones remotas o de pago, OpenAI indica solicitar acceso mediante su [formulario de interés](https://openai.com/form/sign-in-with-chatgpt-interest/). Antes de ofrecer el servicio a terceros hace falta una respuesta del proveedor que cubra específicamente este alojamiento y uso. No se ha enviado el formulario.

El registro abierto utiliza un host ID opaco estable, registro dinámico propio, navegador del sistema, state, nonce y PKCE, callback de loopback, validación del ID token y alcance explícito `chatgpt.tokens.use.direct`. Cada cuenta conserva su propio client ID y conjunto de credenciales; renovación serializada y revocación siguen el [procedimiento oficial](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions). El cliente Flash no debe recibir credenciales. El alojamiento seguro por jugador, su revocación y el vínculo con la cuenta del juego siguen sin implementar.

Las peticiones HTTP de este flujo requieren `store: false`, `stream: true` e historial explícito; existen otras [limitaciones de la vista previa](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations). Esto no demuestra por sí solo ausencia de toda retención de seguridad del proveedor: habrá que verificarla expresamente. El historial local debe desaparecer al finalizar la visita y no usar un SDK que lo escriba en disco. No se solicitarán herramientas en la petición. Plus comparte su límite de cinco horas entre aplicaciones, según la documentación de cuentas. La documentación también menciona uso de créditos disponibles, por lo que la garantía de no consumir saldo o activar gastos debe confirmarse y verificarse antes de cualquier prueba real. Al agotarse la cuota se pausará la inferencia y se ofrecerá administrar el uso, sin compra, cambio de cuenta ni recarga automática.

Como comparación, en el código de T3 Code, revisión `647bd148b3fbd7c8b57a10fd41ae6d5fc7b35cc1`, `apps/server/src/provider/CodexChatGptAuth.ts` implementa registro propio y solicita el alcance del plan; `CodexManagedRuntime.ts` configura Codex App Server con el endpoint público Responses y un comando de autenticación. El [código de T3](https://github.com/pingdotgg/t3code/tree/647bd148b3fbd7c8b57a10fd41ae6d5fc7b35cc1/apps/server/src/provider) y la [documentación de OpenAI para App Server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) describen una alternativa oficial de agente, pero no acreditan las condiciones de este servidor multiusuario. App Server introduce herramientas e historial que exigirían aislamiento y pruebas adicionales; no se ha demostrado cómo deshabilitarlos completamente en este laboratorio. La API convencional con clave y facturación es una alternativa distinta, no seleccionada.

El servidor observado es Linux ARM64 y las imágenes de runtime existentes corren en Docker ARM64 local. Un cliente HTTP directo no requiere binario Codex específico, pero ni el adaptador de OpenAI ni OAuth se han implementado o probado en el Pi. Compatibilidad funcional, límites, renovación y revocación reales siguen pendientes.

## Hallazgos de Houdini y cliente

- `PenguinPuffle` tiene ID de instancia, dueño y tipo. La lista `p.puffles` representa mascotas del pingüino; las mascotas del patio o que esté paseando necesitan una comprobación explícita de visibilidad antes de seleccionarse.
- El manejador `m#sm` comprueba identidad, mute y filtro, procesa comandos y envía `sm` con el ID del pingüino. Un listener posterior por sí solo no demuestra que el mensaje haya superado esas ramas. Hace falta integrar la captura **en la rama aceptada**, después de las reglas, sin esperar la inferencia. El manejador actual registra los textos y el socket puede registrarlos en nivel debug; deben revisarse antes de habilitar la función.
- El manejador `p#pm` retransmite movimiento, pero no mantiene una posición autoritativa persistente para cada puffle. La salida debe referirse a la instancia y el cliente debe encontrar su objeto visual actual; una posición calculada una vez en el servidor no basta.
- El contenido disponible incluye `engine.swf` e `igloo.swf`; no se encontraron `.as` ni `.fla`. Al descompilar temporalmente el `engine.swf` con FFDec 26.4.0 se observó que `PuffleManager` activa el clic en el iglú propio y `handlePuffleClick` llama a `showPuffleWidget(puffle.id)`. También mantiene objetos por ID, cada uno con su `clip`.
- Los bots actuales son clientes pingüino y no son identidad de mascota. El 3 de noviembre se observó una réplica lista, modo API y tres métricas de conexión activas. No se ha enviado una petición mediante ellos.

## Prototipo y límites

`experiments/puffle_sim` contiene un coordinador en memoria y un simulador de proceso separado. En el banco desechable se conectaron a Houdini y Ruffle. La cuenta real se conectó localmente al relay y completó una inferencia autorizada en el banco. La selección toma la instancia visible preferida si es válida; si no, la de menor ID. El coordinador agrupa mensajes, limita sesiones, trabajos paralelos, cola, contexto y tiempo absoluto, mantiene un único trabajo por sesión, descarta resultados tras invalidar la vinculación y permite silencio. El simulador solo responde a mensajes con signo de interrogación. El puente de prueba debe reemplazarse o endurecerse antes de usarse con jugadores reales.

`authority.py` y `bridge.py` son comprobaciones experimentales conectadas únicamente al mundo desechable. Utilizan los modelos reales de Houdini para verificar propietario, sala, conexión, mute y mascota compatible, y excluyen mascotas en el patio o que estén siendo paseadas. Dependen de una clasificación humana y una habilitación confiables suministradas por el servidor: para producción esas piezas aún no existen y no se deducen del nombre de cuenta. El banco solo autoriza los IDs sintéticos asignados por el propio test.

`experiments/puffle_sim/client` contiene una primera implementación experimental de globo AS2 y un constructor que modifica una **copia** del SWF fijado, sin tocar el original. El paquete candidato es `puffletalk` y lleva iglú, instancia de mascota y texto; el globo se crea como hijo del `clip` de esa mascota y utiliza un campo de texto sin HTML. El constructor verifica los hashes esperados y rehúsa sobrescribir archivos. Se ha construido y vuelto a descompilar la copia: aparecen el nuevo manejador y el clic de cuidados original. Esto solo comprueba la compilación, no su comportamiento visual, interacciones, transporte desde Houdini, ni compatibilidad de Ruffle. La posición, duración y longitud son provisionales.

La respuesta se limita a 48 caracteres (y 120 bytes UTF-8 como cota de transporte), el mismo máximo que el campo de chat del pingüino (`chat_input` de interface.swf, maxChars 48); medido en Ruffle, ver «Globo del puffle». Con la cola llena, el prototipo conserva los mensajes más recientes en memoria acotada y descarta contexto antiguo; esto necesita métricas y pruebas de saturación adicionales. Tampoco maneja aún una pausa de cuota persistente entre visitas, clasificación de bots y consentimiento reales, límite global a largo plazo, métricas ni IPC autenticado. En el banco, el parche experimental cancela al salir o desconectarse y el puente invalida al cambiar la mascota; hay que verificar y endurecer las carreras antes de usarlo con jugadores. El aislamiento de red de la prueba se aplica al contenedor completo; el proceso simulado no es una frontera de seguridad de producción.

Prueba reproducible con la imagen local existente, sin red y sin montar credenciales:

```sh
docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python \
  -v "$PWD/experiments/puffle_sim":/src/experiments/puffle_sim:ro \
  -v "$PWD/tests/puffle_sim":/src/puffle_tests:ro -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app/houdini:/src \
  clubpenguin/runtime:lab-mobile \
  -m unittest discover -s /src/puffle_tests -v
```

Resultado observado: 23 pruebas aprobadas. Dos usan el serializador real de Houdini para comprobar el encuadre del paquete candidato y cuatro usan modelos reales en memoria; otras cuatro ejercitan el handler real con conexiones sintéticas.

Además, `python3 experiments/puffle_sim/run_houdini_fixture.py` pasó en una red Docker interna sin salida, con Houdini, PostgreSQL, Redis, tres conexiones AS2 sintéticas y el trabajador simulado en otro proceso. Verificó dueño, visitante, bot, selección del menor ID entre dos mascotas, destinatarios, movimiento y heartbeat durante inferencia lenta, cancelación al salir y un segundo jugador en su iglú. Esta prueba recibe el paquete por sockets AS2; **no usa Ruffle ni prueba la apariencia o legibilidad del globo**. La prueba existente `python3 scripts/test-containers.py --tag lab-mobile --web-tag lab-mobile` también pasó para nginx y el proxy, sin cargar el prototipo.

Para reproducir únicamente la compilación del SWF, proporcionar una instalación local de FFDec 26.4.0 y ejecutar el constructor con `--ffdec` apuntando a `ffdec.jar`, `--engine` al `engine.swf` original y `--output` a un archivo nuevo. Se compilaron dos copias con el mismo hash; el constructor rechazó un origen no reconocido y un destino existente. No se ha generado una imagen web con esa copia.

## Globo del puffle (v6)

El cliente dibuja los globos de chat de pingüino con `com.clubpenguin.ui.balloons.BalloonManager` de interface.swf (instancia `balloons_mc`): adjunta el clip de librería `DefaultBalloon` (`balloon_mc` de fondo, `pointer_mc` como cola, `message_txt`), con formato Burbank Small Medium sin interlineado, ancho máximo 128 px, alto ajustado al texto, 125 fotogramas de vida y reemplazo del globo anterior del mismo emisor. `renderer.as` reutiliza ese mismo administrador: crea el clip con la clave `puffle<id>` (nunca un ID de pingüino), profundidad propia y linkage explícito para no pasar por `findPlayerDepth` ni por la búsqueda de amigo/mascota, llama a `showTextBalloon` y un clip auxiliar dentro del puffle reposiciona el globo cada fotograma (punto local 0,18 del puffle convertido a coordenadas de `balloons_mc`). `reset()` del administrador lo borra al cambiar de sala.

Medición en Ruffle con el banco (dueña cuenta 3, modo `largo N?`): el globo nunca corta texto, crece en alto; 48 caracteres ocupan 3 líneas como un mensaje máximo de pingüino, 90 bytes ocupan 6 y 120 bytes 8 líneas, tapando la escena. Se fijó 48 caracteres en `engine.py` (`speech_text`), `protocol.py` y `renderer.as`. Copia: `/tmp/clubpenguin-puffle-engine-v6.swf`, sha256 `41c6b81e19f970a381a7f85b478b8563a383ab27a22a4b75a8e18df5027735c5`; capturas en `.local/pv/v6a` y `.local/pv/v6b`. 41 pruebas aprobadas.

## Trabajo pendiente para fase 2

1. Endurecer y revisar el parche reproducible sobre la revisión fijada de Houdini: el banco ya captura `sm` aceptado y conecta salida y desconexión, pero faltan clasificación humana y consentimiento reales, controles de logging de todas las capas y transporte local autenticado y acotado hacia el trabajador.
2. Integrar y validar la ruta experimental de paquete y representación para el puffle, incluyendo destinatarios y revalidación inmediata. Probar posición, visibilidad, clic de cuidados y longitud real en Ruffle. El globo v6 ya se vio en Ruffle para la dueña; falta comprobar que la visita también lo vea y completar teclado, móvil y pantalla completa.
3. Conectar el simulador al juego y ejecutar la matriz solicitada con Houdini real, PostgreSQL y Redis desechables, sin salida al proveedor. Cubrir movimiento, heartbeat, mute/filtro, visitantes y bots, orden, saturación, caducidad, múltiples jugadores, salida, cambio de mascota, inyección y borrado. Comprobar teclado, español, móvil y pantalla completa.
4. Completar los límites operativos y privacidad, comprobar el historial de todas las capas y demostrar que el trabajador no puede acceder a shell, administración ni herramientas. Preparar integración oficial únicamente para una modalidad admitida por OpenAI.

## Fases posteriores y rollback

La prueba mínima real debe esperar a que la fase 2 y la modalidad oficial estén listas. La autorización del usuario ya consta en la conversación; antes de ejecutarla hay que informar sobre el texto sintético, el consumo y la incertidumbre sobre créditos, respetando su prohibición de gastos. No se solicitarán secretos en el chat.

Para desplegar: construir imágenes ARM64 etiquetadas de runtime, web y trabajador a partir de fuentes y parche fijados; probarlas localmente, preparar Terraform y plan revisable, verificar de nuevo el nodo y usar siempre `~/.kube/clubpenguin-pi.yaml`. El usuario ya autorizó el despliegue, pero aún no existe un plan terminado ni se cumplen los requisitos previos. Nada de esto está declarado todavía para el puffle.

Rollback del estado actual: retirar únicamente `experiments/puffle_sim`, `tests/puffle_sim` y este documento. Son archivos nuevos y no afectan al juego. Un rollback futuro deberá desactivar primero la función, cancelar trabajos, revocar o retirar las credenciales correspondientes con el flujo autorizado y volver a las imágenes y configuración verificadas anteriores; no se han generado aún imágenes de esta función.

## Botón «Conectar mi ChatGPT» (Puffle Lab, no oficial)

Página local con diseño propio del laboratorio (sin logo, colores ni textos de
OpenAI) que conecta el puffle a tu cuenta ChatGPT con el flujo oficial Sign in
with ChatGPT para herramientas open source. Solo stdlib: el ID token se valida
con RS256 implementado a mano (sin PyJWT/cryptography).

Archivos: `experiments/puffle_sim/chatgpt_auth.py` (OAuth + PKCE, validación de
ID token, credenciales, refresh, revocación), `chatgpt_provider.py` (proveedor
real con la interfaz `evaluate(context)`), `connect_server.py` (página) y
`tests/puffle_sim/test_chatgpt.py` (pruebas sin red).

Arranque, desde la raíz del repo:

    python3 -m experiments.puffle_sim.connect_server --port 1455

Abre http://127.0.0.1:1455/ (se abre solo; `--no-browser` lo evita). Al picarle
«Conectar mi ChatGPT» se te manda a `auth.openai.com`, apruebas la app «Puffle
Lab» y vuelves a `http://127.0.0.1:1455/auth/callback`. El servidor valida
state, canjea el código, valida el ID token y exige
`chatgpt.tokens.use.direct`; si falta, muestra «No concedió uso del plan» y no
guarda nada. Las credenciales quedan en `~/.config/puffle-lab/credentials.json`
(0600, carpeta 0700); el navegador solo ve el estado (Desconectado, Conectado o
Pausado por cuota). «Desconectar» revoca el refresh token y borra el archivo;
«Reanudar» quita la pausa por cuota.

Para que el engine use la cuenta en lugar del simulador hay que pedirlo
explícitamente: `provider.make_provider('chatgpt')` (por defecto,
`make_provider()` sigue siendo el simulador). Un 429
`subscription_sharing_usage_limit_exceeded` pausa el puffle y responde «Necesito
descansar un rato.»; un 503 se reintenta dos veces con espera de 1 s y 2 s.

Pruebas: `.local/browser-tools/bin/python -m unittest tests.puffle_sim.test_chatgpt`
(requiere Python 3.10+; el `python3` 3.9 del sistema no carga `engine.py`).


## Relay local y privacidad (estado actual)

El relay `host_relay.py` corre en loopback de la Mac y mantiene las credenciales
fuera del contenedor del banco. El mundo desechable llama a un reenviador sin
credenciales en su red interna; la única salida de esa red es hacia el relay.
Cada ejecución usa un secreto nuevo, solicitudes de hasta 16 KiB y 32 mensajes,
una inferencia concurrente y espera máxima de 25 s. El proveedor del relay
se ejecuta en un proceso hijo por petición; la desconexión del cliente cancela
y termina el proceso antes de liberar la exclusión. El registro contiene solo
eventos y clases de error, no chat ni tokens. Este diseño es para el banco de
una sola cuenta: no implementa el vínculo seguro entre cada jugador público y
su propia cuenta ni constituye un relay de producción.

El parche fijado de Houdini elimina tanto los logs de texto del manejador de
chat como los logs de paquetes completos de entrada y salida, incluso con
`debug` activo. Queda revisar todas las demás capas y sus registros antes del
despliegue. La clasificación humana y la habilitación confiables aún deben
integrarse desde el servidor; no se deducen del nombre del jugador.

El registro dinámico de OAuth ahora se guarda solo después de validar identidad
y permiso. Un `invalid_grant` conserva el cliente emitido únicamente en memoria
para reintentar, sin sustituir el registro validado. Los registros históricos
sin identidad validada se ignoran si no existe una sesión; al desconectar una
sesión válida se conserva su asociación verificada. Esto no implementa todavía
un selector o almacenamiento aislado para varias cuentas. Las pruebas automatizadas usan
HTTP simulado. Además se ejecutó una sola inferencia real autorizada, sin
compra de saldo ni activación de recargas.

La arquitectura y habilitación para terceros siguen pendientes de confirmación
con OpenAI. La documentación oficial distingue el registro open source local
y otras modalidades; no hay aquí una garantía de que el uso excluya créditos.
No se debe grabar como funcionalidad pública hasta comprobar su flujo real,
el aislamiento por jugador y el comportamiento de cancelación.

Al salir, el coordinador borra su historial y descarta respuestas tardías. En
el relay local, el cierre de la conexión cancela el proceso hijo de inferencia;
eso libera su memoria y cierra sus sockets, pero no puede retirar datos ya
enviados al proveedor ni garantizar que no hayan consumido cuota. La ruta
directa `ChatGPTProvider` sigue usando `urllib` en un hilo; su exclusión evita
solapamientos, pero por sí sola no cancela ese hilo. Antes de producción faltan
validar la cancelación de extremo a extremo con el servidor real y resolver
carreras de renovación de credenciales al terminar un proceso.

## Primera inferencia real (banco desechable)

Con autorización explícita para una respuesta y con conocimiento de que la
cuota podía incluir créditos disponibles, se entró al iglú con una dueña
sintética en Ruffle y una visita sintética por AS2. Tras un solo mensaje de la
dueña, el relay registró `ok: reply` y la visita recibió `puffletalk`. El
worker del relay usa cero reintentos de inferencia para que ese mensaje no
lance solicitudes adicionales. El banco se cerró después.

No se capturó el globo durante su duración; la captura visual del resultado
real y la vista Ruffle de la visita siguen pendientes. La prueba no demuestra
calidad, aislamiento de cuentas de terceros, ausencia de consumo de créditos
ni que una solicitud ya enviada pueda retirarse del proveedor.

## Abrir aplicaciones en la Mac: prueba local opcional

El banco con `--allow-local-actions` permite que el puffle solicite abrir una
aplicación de una lista fija: Calculadora, Notas, Safari, TextEdit, Música o
Brave. El modelo propone la función; el relay comprueba que el último mensaje
del dueño pidió explícitamente esa misma aplicación. La Mac muestra una
confirmación nativa y solo la abre si se elige «Abrir». La respuesta indica el
resultado. No se ejecutan comandos arbitrarios ni se ofrecen archivos o shell.

Este modo está apagado por defecto y solo funciona en el banco manual con relay;
la clasificación de ese banco autoriza únicamente al dueño visual sintético.
No es una implementación de control de computadora ni de autorización
multiusuario para producción. La conexión de Sign in with ChatGPT admite
funciones locales agrupadas en un espacio de nombres, pero no ofrece la
herramienta nativa de control de computadora en esta modalidad ([limitaciones
oficiales](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)).

## Música: Spotify del dueño

El dueño puede pedirle a su puffle, desde su iglú, que controle **su**
Spotify («pon Despacito», «siguiente», «pausa»). Las órdenes las reconoce un
parser determinista. Nunca llegan al modelo, que tampoco recibe nombres de canciones. Las
visitas no pueden ordenar nada. El banco simulado se corre con
`--provider spotify-sim`. La conexión real es local, con PKCE desde :1455 y el relay con
`--spotify`. Diseño, estado y plan de producción:
[puffle-spotify.md](puffle-spotify.md).
