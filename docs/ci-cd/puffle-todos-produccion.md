# Puffle conversador para todos: preparación de producción

Estado: **preparación; no existe todavía un despliegue listo para aplicar**.
Alcance confirmado: todos los jugadores podrán conectar voluntariamente su
propia cuenta de ChatGPT para un puffle de su propio iglú. El cambio de música
en Spotify se desarrolla por separado y no es requisito de esta primera entrega.
La suite actual del puffle, incluido Spotify simulado, pasó 88 pruebas en
Docker sin red. El acceso real a Spotify y el aislamiento completo por jugador
siguen pendientes.

## Qué existe y qué falta

La integración actual funciona en un banco desechable con una sola cuenta y un
relay en la Mac. El motor ya admite proveedores que reciben el ID de jugador,
pero el proveedor real de ChatGPT del relay sigue usando una sola cuenta;
habilitarlo directamente para todos haría que los jugadores la compartieran.
No hay conexión fiable entre la sesión autenticada de
Houdini y la cuenta de ChatGPT de cada jugador, almacenamiento aislado,
revocación por jugador, ni una ruta de activación y consentimiento en el juego.
Los archivos de `tests/puffle_sim/fixture_world.py` son exclusivamente de prueba.

Para una aplicación alojada remotamente, la [documentación oficial de OpenAI](https://developers.openai.com/siwc/token-sharing-open-source)
remite a su formulario de interés. Está preparado el [borrador de consulta](consulta-openai-puffle.md);
no se ha enviado ni hay una respuesta que confirme esta modalidad. No almacenar
credenciales de terceros en el servidor hasta que la modalidad y sus requisitos
estén claros. El código y las pruebas pueden avanzar con cuentas sintéticas.

## Implementación necesaria antes del plan de infraestructura

1. Autenticación y consentimiento: vincular el ID de la conexión autenticada
   de Houdini a una autorización elegida por ese mismo jugador. Una visita, bot
   o cliente que envíe otro ID no puede activar, revocar ni usar la cuenta.
   El consentimiento debe poder retirarse y la desconexión invalidar sesiones.
2. Enrutamiento: seleccionar un proveedor propio para cada vínculo verificado,
   nunca un proveedor global único. Mantener credenciales y renovaciones
   aisladas y serializadas por cuenta. No entregar tokens al cliente Flash, a
   otras cuentas, a logs o a imágenes de contenedor.
3. Ciclo de vida y privacidad: capturar solo chat aceptado; cancelar al salir,
   desconectarse, revocar o cambiar de mascota; descartar resultados tardíos;
   borrar la memoria de la visita; revisar logs de servidor, proxy y proveedor.
   Establecer límites por jugador y globales, y comportamiento de cuota agotada.
4. Integración de aplicación: empaquetar el parche reproducible del cliente v6
   y el puente de Houdini en artefactos de producción, con activación explícita
   y deshabilitada por defecto. No reutilizar el entrypoint exclusivo del banco.
   Los caminos de acciones locales o Spotify no quedan habilitados por defecto.
5. Pruebas locales sin credenciales ni red: usuarios A y B con proveedores
   sintéticos distintos; A y B en iglús propios; visitas y bots; suplantación,
   revocación, desconexión y renovación concurrente; colas llenas, timeout,
   reinicio y borrado; protocolos AS2 y pruebas visuales en Ruffle, español y
   móvil. Comprobar que ni tokens ni mensajes aparecen en logs.

## Plan de release a preparar después

- Ya existe una candidata **solo local** `clubpenguin/web:puffle-v6-candidate-local`
  ARM64, construida sin red desde `clubpenguin/web:lab-006` con el SWF v6. En
  una red Docker interna arrancó y sirvió el SWF con SHA-256
  `41c6b81e19f970a381a7f85b478b8563a383ab27a22a4b75a8e18df5027735c5`.
  No se ha publicado ni se considera una release completa. También se verificó
  que las tres fuentes de Houdini dentro de `runtime:rt-origin-001` coinciden
  con las versiones fijadas por el generador de parches.
- Existe además `clubpenguin/runtime:puffle-staged-local`, ARM64 y sin publicar:
  contiene los parches de Houdini y una copia identificada por hashes de las
  fuentes del prototipo, pero conserva el entrypoint original y no activa el
  puffle. Pasó 53 pruebas aisladas y un banco con Houdini, PostgreSQL y Redis
  desechables, con proveedor simulado y red sin salida. Es un artefacto de
  preparación, todavía sin enrutamiento por jugador ni configuración de producción.
  El motor del árbol cambió después de construirla (trabajo concurrente de
  Spotify), por lo que esta imagen está desactualizada y no debe publicarse.
- Congelar el árbol exacto de fuentes, parche y SWF mediante hashes: el árbol
  actual está sucio, por lo que el HEAD por sí solo no lo identifica. No hacer
  commit, push ni PR sin una instrucción explícita.
- Construir imágenes ARM64 reproducibles con fuentes y dependencias ya locales;
  no instalar ni descargar sin autorización. Registrar tags y digests. Probar
  el runtime y web resultantes con PostgreSQL/Redis desechables y sin salida.
- Preparar configuración de infraestructura, política de red y almacenamiento
  necesarios, sin incluir credenciales en Terraform, manifiestos ni logs.
  Conservar los tags actuales de base de datos y bots; comprobar que ningún plan
  modifique DNS, AKS, runner, auto-deploy ni reemplace `postgres-data` o
  `bots-budget`. Revisar por separado la corrección local de permisos de bots.
- Producir el plan real de Terraform únicamente después de la autorización
  expresa requerida en el contexto. Usar exclusivamente el kubeconfig dedicado
  del Pi, revisar el diff y el hash, y presentar el plan terminado al usuario
  antes de cualquier apply o publicación de imágenes.
- Tras aprobación y despliegue: comprobar estado, logs saneados, sesiones y
  aislamiento de dos jugadores reales con consentimiento; probar la apariencia
  de dueño y visita. Cualquier inferencia usa su propia cuota y puede afectar
  créditos disponibles. Registrar los resultados sin contenido de chat.
- Rollback: desactivar la función, cancelar solicitudes, revertir imágenes y
  configuración al manifiesto anterior verificado, revocar o retirar accesos
  según la modalidad aprobada y comprobar juego y bots. Conservar ambos PVC.

No hay aún imágenes completas de este release, valores de configuración finales
ni un plan Terraform revisado; los puntos anteriores no son una autorización de apply.
