# Plan integral de producción — 4 de noviembre de 2026

Objetivo: llevar todos los cambios del árbol actual a una entrega versionada,
reproducible y operable en el Pi que sirve el sitio público, incluidos NPC,
puffle conversador y música. Este documento prepara el trabajo; no es un plan
binario de Terraform ni acredita que la entrega esté lista para aplicar.

**Aceptación incorporada:** Alexis confirmó que el puffle sí cambia la música.
Se da por aprobada esa funcionalidad en el entorno donde se probó. No hace
falta volver a demostrar su viabilidad. Falta integrar esa ruta en producción
y comprobarla después del despliegue. La confirmación no especifica qué backend
musical se usó y no acredita por sí sola Spotify Web API ni uso multiusuario.

## Inventario y alcance

La revisión inicial encontró **22 archivos versionados modificados y 123 nuevos**,
antes de añadir este plan. No había cambios preparados en el índice. El diff de
los versionados suma 1,177 líneas añadidas y 353 eliminadas; esas cifras no
incluyen los archivos nuevos. El HEAD de partida es `4406274`.

| Grupo | Cambios revisados | Trabajo para producción |
|---|---|---|
| Operación y release | `lablib.py`, `release.py`, `deploy.py`, `plan_gate.py`, `changed.py`, guardas, instalación y pruebas de scripts | Cerrar validación, incluir puffle en release y ensayar un despliegue controlado |
| Construcción y pruebas | `fetch-sources.py`, `build-images.py`, `publish-images.py`, `test-containers.py`, `ws_probe.py` | Construir artefactos finales ARM64, con medios completos y pruebas del puffle |
| Acceso al Pi | `tunnel.py`, modos ejecutables de scripts, `ansible/preflight.yml` | Conservar kubeconfig dedicado, identidad del nodo y propiedad del túnel por ejecución |
| Respaldos | `backup.py`, `restore_verify.py`, pruebas de restauración | Respaldo previo restaurado y comprobado; incluir presupuesto y configuración externa de bots |
| Cloud | Parámetros privados y lista configurable de repositorios permitidos para pull | Conservar permisos de runtime/database/web/bots; revisar cambios cloud por separado |
| Clúster | Tags por componente, estado cloud parametrizado, LAN, orígenes WebSocket, timeout web, `release-info` | Reconciliar configuración con estado real antes de aplicar |
| NPC | `containers/bots/`, provisión de cuentas, oficios, makeover SQL y pruebas | Conservar identidades ya migradas; resolver permisos y aceptación visual |
| Monitoreo | `monitoring/bots-rules.yaml`, valores condicionales de Prometheus | Verificar métricas y añadir señales operativas del puffle |
| Puffle | Motor, autoridad, puente Houdini, workers, relay, OAuth y proveedor en `experiments/puffle_sim/` | Convertir integración del banco en integración de producción |
| Cliente puffle | Generador de SWF fijado por hash, renderer AS2 y recurso visual | Integrar build reproducible en imagen web y renovación de caché |
| Música y acciones locales | Spotify, Música de macOS, catálogo, Safari y herramientas de lista fija | Emparejar dispositivo y jugador; empaquetar acompañante Mac cuando corresponda |
| CI/CD | GitHub Actions, Azure Pipelines, actionlint y pruebas de políticas | Incluir todas las suites; usar un solo escritor del clúster |
| Documentación y Git | README, guías CI/CD, bots y puffle, `.gitignore` | Unificar estado y publicar todo el código y pruebas, conservando datos privados fuera de Git |

Este alcance sustituye la exclusión del puffle de la antigua guía
`activacion-pipeline.md`: la entrega solicitada ahora sí lo incluye. Los bancos
y sus cuentas sintéticas se versionan como pruebas; los procesos de producción
necesitan entrypoints propios.

## Evidencia obtenida en esta revisión

Se ejecutaron verificaciones locales y contenedores desechables. No se consultó
el estado vivo de Azure, GitHub ni Kubernetes; las referencias a lo desplegado
proceden de las guías del proyecto y deben reconfirmarse antes de liberar.

| Verificación | Resultado |
|---|---|
| `git diff --check` | Correcto |
| Suite puffle con runtime `rt-origin-001`, Docker sin red, fuentes actuales montadas | 108 pruebas aprobadas |
| Suite bots, Docker sin red | 100 pruebas aprobadas |
| Suite scripts CI/CD | 76 casos: 73 aprobados inicialmente; los 3 de respaldo omitidos se ejecutaron aparte con `CP_TEST_DB_IMAGE=clubpenguin/database:lab-002` y también pasaron |
| Política de Azure Pipelines | Correcta, 5 etapas |
| Integración Docker runtime `rt-origin-001`, database `lab-002`, web `lab-006` | Correcta: aislamiento, registro, login, sala compartida, nginx, handshake WebSocket y rechazo de orígenes/rutas |
| `terraform validate` con proveedores locales, cloud y cluster | Ambas configuraciones válidas |
| Render Helm de Prometheus, incluidas reglas bots, y Grafana | Correcto |
| `bash scripts/validate.sh` | Falla al inicio: formato en `infra/cloud/main.tf` |
| Sintaxis Ansible con instalación actual del Mac | Bloqueada: Ansible falla al importar con Python 3.15, `dataclasses._is_type` ausente |

Las pruebas sobre imágenes existentes validan esas imágenes y las fuentes
montadas por cada suite. No sustituyen la prueba de imágenes finales recién
construidas. El Python predeterminado del Mac es 3.9.6; el runtime Docker usa
3.11 y permite ejecutar la suite actual del puffle.

## Bloqueos concretos

1. **El build estándar no contiene el puffle.** Los Dockerfiles actuales de
   runtime y web no copian el motor ni aplican sus parches. `run.py` tampoco
   inicia el puente. Las imágenes candidatas locales documentadas no constituyen
   una release final del árbol actual.
2. **Falta cobertura en CI.** `validate.sh` no valida `experiments/`; el runner
   de bots carga solo `tests/test_*.py`, excluyendo `tests/puffle_sim/`. Construir
   bots y ejecutar su suite no valida automáticamente el puffle.
3. **Las huellas de release no incluyen los experimentos.** `release.py`
   calcula hashes de archivos versionados bajo `containers/<componente>` y
   revisiones upstream. Añadir únicamente archivos de `experiments/` no cambia
   la etiqueta runtime/web. Además, bots es opcional hasta que su Dockerfile
   esté en Git; la resolución automática actual puede omitirlo.
4. **El relay y la conexión de cuentas son locales y de una sola cuenta.**
   `host_relay.py` selecciona un backend musical con clave `local` y
   `connect_server.py` escucha en loopback. Compartir esos procesos con todos
   los jugadores compartiría la cuenta/dispositivo del operador.
5. **La música de Mac requiere un componente en Mac.** Apple Events, Música y
   Safari no se ejecutan en Linux ARM64. Mover Python al Pi no mueve esa capacidad.
6. **La corrección de permisos de bots sigue pendiente según la documentación.**
   `OnRootMismatch` evita recursión únicamente cuando la raíz del volumen cumple
   las condiciones esperadas. Se necesita comprobar un reinicio real, ledger
   0600, contenido y reconexión de las tres cuentas.
7. **El auto deploy actual acepta solo imágenes.** Nuevos servicios, políticas,
   configuración del puffle o el campo de permisos fallarán con
   `--expect images-only`. El primer despliegue integral requiere un plan de
   infraestructura revisado; después podrán automatizarse actualizaciones de imágenes.
8. **Hay documentación desfasada.** README todavía presenta tags y nodo antiguos;
   otras guías reportan `rt-origin-001` y nodo `alexis`. Varias cifras de pruebas
   y pendientes musicales son históricas. No copiar sus valores al despliegue.

## Orden de ejecución

### 1. Fijar la base y dejar la validación en verde

- Inventariar los 145 archivos y revisar contenido nuevo, binarios y permisos
  ejecutables antes de preparar commits. Excluir `.local/`, credenciales OAuth,
  kubeconfigs, tfvars privados, estados, planes y medios upstream.
- Corregir el formato de `infra/cloud/main.tf`. Usar un entorno de herramientas
  con versión compatible y fijada de Python/Ansible; ejecutar de nuevo ambos
  chequeos de sintaxis. El error observado es del entorno, no demuestra un fallo
  del playbook.
- Unificar ejecución de las suites de scripts, bots y puffle con Python 3.11.
  Añadir compilación de experimentos y pruebas de puffle a ambos pipelines.
  Ejecutar las pruebas de respaldo con una imagen PostgreSQL desechable.
- Añadir integración completa de puffle a CI, con proveedor simulado y sin
  credenciales. Mantener separada la prueba nativa ARM64 de la prueba Ubuntu.
- Revisar README y guías para distinguir evidencia histórica de estado vigente.
  Conservar la aceptación musical aportada por Alexis.

**Salida:** validación completa verde y código/pruebas preparados para una
revisión de Git que incluya todos los componentes del alcance.

### 2. Terminar la integración de puffle, conversación y música

Arquitectura propuesta: el mundo Houdini valida dueño, iglú, mascota, consentimiento
y sesión; un servicio privado procesa conversación/órdenes y enruta por jugador;
la web incorpora el SWF y la conexión de cuentas. La música de macOS se ejecuta
en un acompañante instalado en la Mac emparejada con ese jugador.

- Crear un módulo y entrypoint de producción para el puente. Instalar los
  parches únicamente sobre fuentes con hashes esperados. Capturar chat en la
  rama aceptada y cancelar al salir, desconectarse, revocar o cambiar mascota.
  La caída del servicio puffle debe dejar operativo el juego.
- Implementar vínculo verificable entre sesión del juego, consentimiento,
  cuenta del proveedor y dispositivo. No aceptar identidad desde un ID libre
  enviado por el navegador. Separar cuentas A/B, tokens, renovaciones y cuotas.
- Implementar conexión/desconexión autenticada desde la web, protección CSRF,
  state de un solo uso, expiración y revocación. Mantener tokens fuera de Flash
  y de logs; revisar la implementación OAuth/validación criptográfica antes de
  alojar credenciales de terceros.
- Validar la modalidad vigente de acceso a ChatGPT y sus condiciones para este
  alojamiento antes de habilitarla a terceros. Las guías existentes registran
  una cuestión pendiente del proveedor; no se ha verificado externamente en
  esta revisión. La implementación puede avanzar con proveedores sintéticos.
- Incorporar la ruta musical que ya funcionó. Para Música/Safari: acompañante
  con conexión saliente autenticada al servicio, emparejamiento por jugador,
  órdenes acotadas, caducidad, protección contra repetición y cancelación. No
  publicar el puerto loopback del relay ni dar a visitantes control de la Mac.
- Empaquetar acompañante, arranque/parada y diagnóstico de permisos de macOS;
  gestionar pérdida de sesión gráfica, ventana Safari cerrada y Mac apagada.
  La música solo se anuncia como cambiada cuando el backend confirma el resultado.
- Mantener Spotify como adaptador independiente: sus pruebas HTTP simuladas no
  acreditan una conexión real. Verificar disponibilidad, callbacks, usuarios y
  requisitos actuales del proveedor antes de anunciarlo habilitado para todos.
- Las acciones locales de abrir aplicaciones también entran en el inventario:
  conservar lista fija, intención explícita y confirmación nativa; habilitarlas
  mediante capacidad independiente y solo en dispositivos emparejados. No son
  un requisito para que conversación o música funcionen.
- Acotar colas, concurrencia y tiempos por jugador/globalmente; memoria efímera,
  IPC autenticado y métricas sin texto de chat. Añadir interruptores de función
  globales y por jugador que cancelen trabajos al apagarse.

**Salida:** recorrido del sitio hasta Houdini y servicio final probado con dos
jugadores aislados; música integrada con el dispositivo correcto. Primero puede
habilitarse una lista pequeña en producción, conservando como objetivo final
el acceso voluntario de todos los jugadores con modalidad admitida.

### 3. Hacer reproducibles los artefactos de release

- Mover código desplegable a contextos de producción o ampliar explícitamente
  contextos y hashes del sistema de release. Incluir motor, puente, proveedor,
  trabajador, constructor SWF, renderer y revisiones fijadas en las huellas
  de los componentes que realmente los consumen.
- Construir el SWF desde fuente upstream y FFDec fijados; registrar su hash y
  copiarlo a la ruta exacta que carga el cliente. No depender de un SWF en `/tmp`
  ni de capas candidatas generadas manualmente.
- Añadir el nuevo servicio al manifiesto, tfvars, verificación de imágenes y
  registro de releases si se elige una imagen separada. Los helpers actuales
  conocen runtime/database/web/bots; hay que extenderlos de forma coherente.
- Versionar o invalidar el caché del cliente: `sw.js` conserva `glaciar-m1` y
  sirve medios desde caché mientras refresca en segundo plano. Verificar el
  SWF tanto en navegador nuevo como en uno con caché previa y al hacer rollback.
- Construir ARM64 con medios completos, registrar commit, hashes, tags y digests;
  rechazar fuentes sin versionar y artefactos web de prueba con medios stub.
- Conservar la imagen de base de datos y las identidades NPC salvo un cambio
  deliberado. El código de release permite fijar tags con `--pin`; usar los
  digests efectivos de la base validada.

**Salida:** conjunto completo de imágenes y acompañante local instalable,
probados desde un checkout identificable sin credenciales incorporadas.

### 4. Preparar infraestructura y recuperación

- Leer configuración y estado efectivos mediante kubeconfig dedicado; verificar
  nodo esperado y arquitectura, pods, PVC, espacio, recursos y tags/digests.
  Registrar manifiesto de la release anterior y accesos externos existentes.
- Comparar Cloudflare con estado Terraform; importar Deployment/política solo
  si aún están fuera del estado. No recrear conector ni cambiar DNS por accidente.
- Revisar `infra/cloud` separado del clúster: conservar acceso de pull a bots y
  componentes nuevos, cuenta de estado y respaldos. No retirar permisos por
  regresar al valor por defecto de tres repositorios. Planificar rotación del
  token antes de la fecha registrada en el proyecto: 1 de febrero de 2027.
- Declarar servicio/worker, recursos, probes, referencias a Secrets externos,
  red mínima, métricas y almacenamiento que requiera la arquitectura final.
  Elegir y probar protección/cifrado de credenciales; no guardar sus valores en
  tfvars ni manifiestos de release. Respaldar claves de recuperación de forma privada.
- Revisar permisos de bots en un plan pequeño previo o una etapa identificada
  del despliegue. Respaldar ledger y configuración; conservar `Recreate`, una
  réplica, modo del proveedor y PVC `bots-budget`.
- Antes del despliegue integral: dump PostgreSQL, restauración comprobada y
  manifiesto SHA-256; copias privadas recuperables de configuración/Secrets
  externos y ledger. El respaldo PostgreSQL no cubre por sí solo esos archivos.
- Preparar un plan guardado de `infra/cluster`, resumen saneado y SHA-256.
  Cualquier creación o cambio de configuración esperado se revisa explícitamente.
  Cero reemplazos/borrados de `postgres-data`, `bots-budget`, credenciales o
  conector; cero cambios a AKS o recursos ajenos al proyecto.

**Salida:** artefactos finales, respaldo restaurable, diff revisable, plan exacto
y procedimiento de reversión disponibles antes del apply.

### 5. Publicar y desplegar de forma controlada

1. Versionar todo el alcance por grupos revisables: operación, bots, puffle y
   música, integración/build, infraestructura y CI/documentación. Abrir PR con
   todos los checks, integrar en `main` y mantener auto deploy apagado.
2. Usar GitHub Actions como ruta principal propuesta, dado su workflow actual;
   conservar Azure Pipelines validado y sin aplicar en paralelo. Configurar o
   verificar runner ARM64, OIDC, permisos y environment restringido a `main`.
3. Publicar imágenes finales en ACR y guardar digests. Generar/revisar el plan
   con esas referencias; aplicar únicamente el archivo guardado identificado
   por su SHA-256. Si cambian fuentes, manifiesto o estado relevante, volver
   a preparar y revisar el plan.
4. Desplegar infraestructura y artefactos con funciones nuevas apagadas.
   Prever reconexión de jugadores al actualizar Glaciar: hay un solo mundo.
5. Validar juego público y NPC; activar puffle para una cuenta emparejada,
   después dos cuentas independientes y finalmente ampliar la habilitación.
   La prueba local musical ya aceptada pasa a ser una regresión del flujo instalado.
6. Registrar release y resultados saneados. Habilitar auto deploy solo después
   del primer despliegue integral exitoso, cuando los cambios posteriores quepan
   en la compuerta de imágenes. No ampliar esa compuerta para saltarse migraciones.

El respaldo de la etapa anterior debe renovarse si la publicación y revisión
demoran lo suficiente para que deje de representar el punto de recuperación.

### 6. Aceptación y operación

| Área | Criterio de aceptación |
|---|---|
| Juego | Registro/login público, selección de mundo, dos jugadores en sala, movimiento y heartbeat sin regresión |
| NPC | Juanito, DJ Cobra y Viterbos visibles, prendas y salas correctas, menciones al destinatario y tres sesiones estables |
| Presupuesto bots | Reinicio real conserva ledger 0600 y contenido; contador no se reinicia |
| Globo puffle | Dueño y visita lo ven en la mascota correcta; 48 caracteres, español, clic de cuidados, móvil y pantalla completa |
| Aislamiento | Dos jugadores usan sus propias cuentas/dispositivos; visita, bot e ID falsificado no pueden actuar |
| Ciclo de vida | Salida, desconexión, revocación y cambio de mascota cancelan trabajo; no aparece respuesta tardía |
| Música | Orden desde juego público llega al dispositivo emparejado y confirma resultado; dispositivo ausente produce mensaje veraz |
| Cuotas y errores | 429, expiración, renovación concurrente, timeout y cola llena quedan acotados sin bloquear Houdini |
| Desactivación | Interruptor detiene acciones nuevas y cancela pendientes; juego continúa con servicio puffle caído |
| Cliente actualizado | Navegador con caché previa obtiene el SWF correcto; reversión recupera el compatible |
| Observabilidad | Probes, imágenes/digests y métricas correctas; logs sin chat, códigos OAuth ni tokens |

El `deploy.py verify` actual comprueba infraestructura y conectividad, pero no
demuestra por sí solo esta matriz. Extenderlo con comprobaciones de puffle y
acompañante, exigir comprobaciones públicas y de Prometheus en producción, y
conservar aceptación visual separada. Una llamada real usa la cuota del titular;
limitarla a los casos de aceptación previstos y no dispararla desde CI.

Observar durante una ventana inicial y al menos un ciclo de reconexión/reinicio:
reinicios, memoria del Pi, disco, latencias, errores del proveedor, cola, sesiones
NPC y conexión del acompañante. Preparar alertas operativas y una programación
de respaldos con ejecución/restauración comprobadas; las reglas de Prometheus
actuales no prueban que existan notificaciones externas.

## Rollback

1. Apagar conversación, música y acciones; cancelar trabajos pendientes y cortar
   autorizaciones del acompañante de la release afectada.
2. Volver a imágenes/configuración anteriores con manifiesto y digests conocidos,
   revisar un plan nuevo y aplicar el archivo exacto. Revertir explícitamente
   recursos/configuración nuevos: un rollback `images-only` no deshace esos cambios.
3. Restaurar el cliente compatible e invalidar su caché; verificar login y salas.
4. Conservar PostgreSQL y `bots-budget`. No repetir provisión o makeover ya
   aplicados. Una restauración de datos requiere decidir qué datos recuperar;
   no restaurar toda la base por defecto para revertir una imagen.
5. Gestionar revocación/borrado de accesos por jugador cuando corresponda y
   comprobar NPC, ledger y aislamiento. Registrar la release revertida y motivo
   sin contenido de conversaciones.

La entrega completa termina cuando código y pruebas están versionados, CI pasa,
los artefactos finales están desplegados, conversación y música funcionan desde
el sitio con aislamiento por jugador, la operación tiene respaldo/observabilidad
y el rollback se ha ensayado. Publicar el árbol actual sin cerrar estos puntos
no cumple ese resultado.
