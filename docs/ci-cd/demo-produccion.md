# Plan para la demo en producción

Estado: **NPC aplicados; puffle pendiente de implementación y plan de despliegue**. El sitio público usa el Pi;
no hay un segundo clúster de producción. Los cambios de NPC y puffle son
entregas distintas y la demo debe grabarse solo después de validarlas.

## NPC: nombres y vestuario

- Juanito: Coffee 110, delantal de café 262.
- DJ Cobra: Night Club 120, audífonos 481.
- Viterbos: Pizza 330, gorro de chef 424 y delantal de pizza 263.

La imagen `bots-002` ya es genérica: esta propuesta cambia filas de Houdini y
el Secret de configuración, sin necesitar publicar una imagen nueva. El
candidato privado `.local/bots/work-makeover.json` conserva las tres cuentas,
contraseñas, política, proveedor, salas y zonas del archivo anterior. Es una
preparación local: antes de usarlo hay que confirmar privadamente que el Secret
vigente coincide con el archivo original y detenerse si difieren.

Secuencia aprobada para los NPC, con `~/.kube/clubpenguin-pi.yaml` y nodo
`alexis` exclusivamente. Se ejecutaron los pasos 1 a 4 y la comprobación de
salud y roster del 5; faltan la inspección visual y el paso 6. El respaldo se
leyó y verificó, pero no se ha ensayado una restauración real:

1. Verificar destino, salud y configuración efectiva. Confirmar los tres ID,
   su condición de cuentas normales y aprobadas, la ausencia de colisiones de
   apodo y la existencia de las cuatro prendas en el catálogo de producción.
2. Guardar copias privadas verificadas del Secret vigente y de los valores
   anteriores de `nickname`, `head`, `body` y las relaciones de inventario de
   las cuatro prendas para cada una de las tres cuentas. No mostrar ni versionar
   credenciales. Comprobar que se puede restaurar el respaldo.
3. Bajar el Deployment de bots a cero y esperar a que spec y status sean cero.
   El juego seguirá disponible, pero los NPC estarán ausentes durante el cambio.
4. Aplicar `scripts/bot-makeover.sql` a Houdini y comprobar las tres filas y el
   inventario. Aplicar al Secret el candidato privado comprobado. Si cualquiera
   de los pasos falla, mantener los bots detenidos y restaurar los datos y el
   Secret de los respaldos antes de volver a levantarlos.
5. Volver a una réplica, comprobar `/readyz` y las tres sesiones, y mirar los
   nombres y las prendas en el cliente. Confirmar que los SWF se ven bien en
   las tres salas; su presencia en disco no demuestra todavía la apariencia.
6. Solo con autorización para consumir cuota, mencionar a cada personaje y
   comprobar respuesta breve, narración de pedidos y que DJ Cobra no afirma
   cambiar la música. Registrar el resultado y entonces grabar la demo.

Para revertir esta entrega, detener los bots, restaurar el Secret guardado y
los valores previos de nombre y ropa por ID, y restaurar exactamente la
presencia o ausencia anterior de las cuatro relaciones de inventario. Después,
levantar una réplica y verificar las sesiones. Este rollback es distinto del
histórico a `bots-001`; nunca elimina el PVC ni su ledger de cuota.

### Corrección pendiente del PVC de cuota

El reinicio de los bots volvió a ampliar los permisos del ledger y provocó un
CrashLoop. Ya se recuperó el servicio cambiando solo ese fichero a 600, con
autorización y comprobando que su contenido no cambió. El parche local de
`infra/cluster/bots.tf` propone `fs_group_change_policy = "OnRootMismatch"`;
la raíz del PVC observada cumple el grupo y modo esperados. El proveedor local
acepta el campo y `terraform validate` pasó, pero no hay plan ni aplicación.

Para revisar esta corrección hace falta autorizar una orden nueva que permita
generar el plan contra el estado remoto, sin aplicarlo. El diff esperado es
únicamente ese campo del Deployment; conservar tags, réplica, provider, Secret,
PVC y demás recursos. Revisar el plan y su hash antes de pedir aprobación para
aplicarlo. La aplicación recrearía el pod: después hay que verificar que el
ledger sigue en 600, el pod está listo y las tres sesiones se reconectan. Si el
directorio raíz no coincide con el grupo o los permisos esperados, Kubernetes
puede volver a recorrer el volumen y el cambio no garantiza conservar 600.

## Puffle conectado a ChatGPT

El relay actual vive en la Mac y solo conecta el banco desechable: todavía no
es una instalación para el sitio público. Una inferencia real autorizada ya
respondió en el banco y una visita recibió el paquete; falta capturar el globo
real y verificarlo visualmente en la visita. También faltan clasificación humana
y consentimiento desde una fuente confiable del servidor, cierre de sesión en
eventos reales, integrar el parche de registros de chat, límites y pruebas de
privacidad y saturación, además de la matriz de Ruffle y móvil. La opción de
alojar cuentas de terceros necesita aclaración del proveedor; no hay una
garantía documentada aquí de que el flujo excluya consumo de créditos.

El usuario autorizó continuar la implementación en `experiments/puffle_sim/`,
`tests/puffle_sim/` y `docs/puffle-conversacion.md`. Primero hay que cerrar
los puntos anteriores localmente y preparar imágenes ARM64, aislamiento de
credenciales por jugador, configuración de infraestructura y rollback. Solo
entonces se puede presentar un plan de despliegue concreto para aprobación; no
existe todavía uno listo para aplicar.

## Grabación propuesta

1. Entrar a Coffee, mostrar a Juanito y hacer un pedido narrado.
2. Visitar el Night Club y saludar a DJ Cobra; pasar a Pizza y pedir a Viterbos.
3. Entrar al iglú propio, escribir al puffle y mostrar su globo; mostrar también
   la vista de una visita para demostrar que ambos lo ven.
4. Grabar sin mostrar contraseñas, datos de conexión, herramientas internas ni
   pantallas de autorización. Publicar el video en LinkedIn es una acción
   posterior de Alexis; aquí se prepara la demostración.
