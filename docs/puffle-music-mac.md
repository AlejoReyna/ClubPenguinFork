# Música de macOS desde el puffle

Actualización, 4 de noviembre: Alexis confirmó que el puffle sí cambia la música
en el juego. Esa aceptación funcional queda conservada. La integración al sitio
público usa ahora el [acompañante por jugador](ci-cd/puffle-produccion.md); los
pendientes del banco descritos abajo son evidencia histórica.

La integración es una prueba local opcional. En el banco con `--provider relay --music-mac`, el puffle del dueño puede pedir a la app Música de esa Mac que pause, reanude o cambie de pista, o busque y reproduzca una canción del catálogo de Apple Music sin agregarla a la biblioteca. Las visitas no pueden controlar la app.

Las órdenes de música reconocidas se resuelven localmente, sin enviar la orden a ChatGPT. El chat normal sigue usando el proveedor configurado. La búsqueda usa la interfaz de Música mediante Automatización y Accesibilidad: necesita una sesión gráfica desbloqueada, permisos de macOS y una suscripción que permita reproducir el catálogo. Puede fallar si cambia la interfaz, si se interrumpe la automatización o si el resultado es ambiguo. El puffle confirma la reproducción solo cuando Música informa que está sonando la canción y el artista seleccionados.

El banco es temporal: al cerrarse se pierde la sesión del iglú. `--music-mac` es incompatible con `--spotify`, que permanece disponible como integración local independiente. Esto no está desplegado en producción ni habilita el control de Música para otros jugadores.

Validación: pruebas automáticas aisladas, compilación de AppleScript y búsqueda y reproducción reales de «Suzuki» de Fuerza Regida desde el catálogo. Falta verificar el recorrido completo desde el chat del juego después de actualizar el relay local.

## Apple Music en Safari, sin mover el cursor

Existe además un modo experimental local `--music-safari-window-id ID`, incompatible con los demás reproductores. El dueño inicia sesión en Apple Music en una ventana dedicada de Safari; se usa su primera pestaña y se comprueba que siga en `https://music.apple.com`. El modo requiere permitir JavaScript desde Apple Events. La búsqueda y los controles se ejecutan sobre esa página en segundo plano, sin cambiar de ventana ni mover el puntero. La sesión permanece en Safari; el relay no lee cookies, tokens ni datos de la cuenta.

La búsqueda usa la región indicada por la propia página. En la prueba, el catálogo de EE. UU. solo entregó muestras; al usar la página de México se confirmó la reproducción completa de «Suzuki» y también se probaron pausa y reanudación. El backend comprueba título, artista y reproducción, y distingue el servidor de muestras de iTunes para no anunciar una muestra como canción completa. No se ha validado para todas las regiones, canciones o cambios de interfaz. La pestaña debe permanecer abierta y la reproducción ocurre en Safari, no en Música. La función sigue limitada al dueño del banco desechable y no está desplegada.
