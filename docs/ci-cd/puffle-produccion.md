# Puffle y música: instalación de producción

El runtime integra el puente en Glaciar; la web construye el SWF reproducible y
sirve `/puffle/`, el acompañante descargable y sus hashes. `MODE=puffle` ejecuta
el broker privado con una réplica y un PVC de hashes de emparejamiento. La opción
`puffle_enabled` controla la función. La base de datos del juego no cambia de imagen.

Las credenciales de ChatGPT y Spotify permanecen en la computadora del jugador.
El emparejamiento tiene dos pasos: código privado desde el iglú autenticado y
confirmación del pingüino en esa computadora. El broker no acepta un ID elegido
por el navegador como prueba de identidad. Los NPC conocidos no pueden emparejarse.
Se mantiene un trabajo por jugador, máximo 32 en total, 8 acciones/evaluaciones
por minuto y al menos 2 segundos entre ellas. El trabajo vence a los 25 segundos.

La [documentación oficial de OpenAI](https://developers.openai.com/siwc/token-sharing-open-source)
describe el uso del plan en aplicaciones locales/open source; este acompañante
ejecuta la inferencia localmente. El servidor no aloja cuentas OAuth de terceros.
El uso sigue sujeto a elegibilidad y límites del titular, y puede afectar sus
créditos disponibles. No se ejecutan inferencias reales desde CI.

## Operador

1. Usar Python 3.12–3.14 para las herramientas de validación y el entorno
   `.local/tools/ansible/bin`; el juego y el acompañante requieren Python 3.11+.
2. Verificar túnel y nodo con los scripts existentes. Preparar una sola vez el
   Secret externo mediante `python3 scripts/prepare-puffle-secret.py`; no rota
   un Secret existente ni imprime su valor.
3. Fijar privadamente `CP_PUFFLE_ENABLED=false` para la primera instalación;
   `CP_PUFFLE_ALLOWED_PLAYERS` permite una lista de IDs piloto, separada por comas.
   Vacío permite a cada jugador humano emparejar voluntariamente su dispositivo.
   `CP_RELEASE_PINS=database=lab-002` conserva la base actual al resolver releases
   automáticas; cambiar ese pin requiere una actualización deliberada y revisada.
   Fijar `CP_PROXY_EXTRA_HOSTS` con los mismos hosts adicionales del proxy vivo:
   los checkouts limpios del runner no tienen el `terraform.tfvars` del operador.
4. Construir y probar runtime/web ARM64. El build completo descarga FFDec 26.4.0
   si falta y verifica tanto su distribución como el JAR por SHA-256. Solo se
   parchea una copia de las fuentes upstream fijadas y del SWF original.
5. Respaldar PostgreSQL con restauración comprobada y configuración externa con
   `backup-operator-state.py`. El segundo script protege Secrets, ledger y una
   instantánea consistente SQLite de dispositivos cuando existe.
6. Publicar imágenes, generar el manifiesto y revisar `deploy.py plan` sin la
   restricción `images-only` para la primera instalación. Aplicar por SHA-256.
   Comprobar `deploy.py verify --require-public --require-prometheus`.
7. Habilitar `CP_PUFFLE_ENABLED=true` mediante otro plan revisado y repetir la
   aceptación desde el sitio. Cambiar ese valor no debe volver a cambiar imágenes.

`puffle_enabled=false` retiene servicio, PVC y vínculos pero rechaza nuevos
trabajos. La modificación recrea el mundo/broker y cancela el trabajo pendiente.
El jugador puede desconectar desde el juego o desde el acompañante. Ctrl+C detiene
el acompañante y el lease del dispositivo vence en cuatro segundos.

## Jugador

En `/puffle/` están la descarga y los pasos de instalación. Conecta ChatGPT o
Spotify desde la página local existente. Música/Safari requieren una sesión
gráfica de Mac con sus permisos; Spotify utiliza su adaptador independiente.
Inicia el emparejamiento desde esa computadora, escribe el código en tu propio
iglú y confirma el pingüino. Solo después inicia `companion run`, seleccionando
un reproductor. Las visitas ven los globos y no pueden controlar el dispositivo.
Las acciones de abrir aplicaciones permanecen desactivadas salvo la opción
explícita `--allow-local-actions` y requieren confirmación nativa por acción.

## Pruebas y recuperación

`run-puffle-tests.sh` prueba el código instalado dentro de la imagen, sin montarle
fuentes que puedan ocultar una imagen obsoleta. `test-containers.py --puffle`
prueba el entrypoint real de producción con Houdini/PostgreSQL/Redis desechables,
dos acompañantes y una visita. Todos los datos y las respuestas son sintéticos;
la red no tiene salida. Ambas rutas de CI las ejecutan.

El proceso local de cada trabajo se aísla en un grupo cancelable para terminar
también herramientas hijas y solicitudes bloqueadas. Esto no revierte una acción
ya ejecutada ni retira una solicitud que el proveedor ya recibió.

En un rollback de aplicación se conserva `puffle-devices`, se apaga la función y
se restaura el manifiesto anterior. Mantener los recursos del broker al revertir
imágenes evita borrar el PVC; si se vuelve a una imagen sin `MODE=puffle`, escalar
solo el broker a cero en el plan de reversión o fijarlo a la última imagen que lo
soporte. Los manifiestos históricos sin `features.puffle=true` fuerzan la función
apagada y el broker a cero réplicas automáticamente, conservando el PVC; ese
primer rollback requiere un plan completo, no `--expect images-only`.
Conservar también `postgres-data` y `bots-budget`. La web evita cachear
`engine.swf`; sus hashes se verifican antes de registrar la release.

Prometheus incorpora reglas del puffle sin reemplazar las alertas existentes.
El workflow de respaldo diario queda controlado por `CP_BACKUP_ENABLED`; requiere
runner disponible, su environment de backup y OIDC. El despliegue automático
continúa limitado a cambios de imágenes y metadatos de release.
La primera ejecución controlada puede iniciarse con `gh workflow run ci.yml
--ref main -f deploy=true`, después de pasar CI y habilitar explícitamente
`CP_AUTO_DEPLOY_ENABLED`. Conserva la misma compuerta, SHA-256 y verificación.
