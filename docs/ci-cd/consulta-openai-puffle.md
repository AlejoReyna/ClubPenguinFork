# Borrador de consulta sobre Sign in with ChatGPT

Estado: borrador para revisión; no se ha enviado. La documentación de OpenAI
indica usar su [formulario de interés](https://openai.com/form/sign-in-with-chatgpt-interest/)
para aplicaciones alojadas remotamente que quieran usar el plan del usuario.

## Descripción propuesta

Club Penguin Lab es un laboratorio abierto y no oficial de un juego multijugador.
Queremos que cada jugador pueda autorizar voluntariamente su propia cuenta de
ChatGPT para que uno de sus puffles responda brevemente a mensajes aceptados en
el iglú del jugador. El texto se muestra a quienes están presentes. No buscamos
acceso a las conversaciones previas del usuario ni ejecutar herramientas.

El prototipo local usa Sign in with ChatGPT, un proveedor de texto y memoria
efímera por visita. El servidor del juego funciona en un Raspberry Pi accesible
por internet. La modalidad para terceros aún no está implementada; queremos
aclarar primero qué modalidad de registro, alojamiento y custodia se admite.

## Preguntas

1. ¿Qué integración y autorización corresponden a una aplicación remota y
   multijugador que permite a cada usuario consumir su propio plan?
2. ¿Se permite que el servidor del operador guarde credenciales renovables por
   jugador? En ese caso, ¿qué requisitos de aislamiento, almacenamiento,
   revocación, consentimiento y revisión se aplican?
3. ¿Existe un control documentado para impedir consumo de créditos adicionales
   y limitar el uso exclusivamente a la asignación del plan, sin activar ni
   comprar recargas?
4. ¿Qué condiciones aplican a una demostración privada hecha por el operador
   con su propia cuenta en su propio servidor, frente al uso abierto a otros
   jugadores?
5. ¿Qué modalidades de retención de datos aplican al texto enviado con
   `store: false`, incluida la retención por seguridad?

No incluir contraseñas, tokens, archivos de configuración privados ni datos de
otros jugadores en la consulta.
