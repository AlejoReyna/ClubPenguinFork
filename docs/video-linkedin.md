# Plan de grabación de Club Penguin Lab para LinkedIn

El corte dura **1:45**. La historia empieza con una pregunta simple: ¿cuándo vale la pena pagar para que hable un pingüino? Bruma y Lola muestran la decisión de diseño. El gráfico de respuestas y el plan de Terraform muestran cómo se opera el sistema en el Pi.

LinkedIn recomienda captar la atención en los primeros segundos, grabar vertical y usar subtítulos. Para demostrar habilidades de software y plataforma, abre con un resultado en pantalla y deja que el espectador vea las pruebas de cada afirmación. [Guía de video de LinkedIn](https://www.linkedin.com/business/talent/blog/talent-acquisition/how-create-videos-for-linkedin-feed) · [Investigación de LinkedIn sobre contratación por habilidades](https://www.linkedin.com/business/talent/blog/future-of-recruiting)

## Guion de 1 minuto y 45 segundos

| Tiempo | En pantalla | Locución |
| --- | --- | --- |
| 0:00–0:12 | Lola baila. Corte al Pi y al contador real de bots conectados. | «Monté Club Penguin en una Raspberry Pi. Tengo veintiséis pingüinos y una regla de oficina: si vas a hablar, dime cuánto cuesta.» |
| 0:12–0:33 | Dojo. Pregunta «Bruma, ¿eres una IA?». Deja leer su respuesta. Corte a «Bruma, ¿qué entrenamos hoy?» y al evento confirmado de Kimi. | «Bruma reconoce su nombre. Su respuesta de identidad sale de una regla local. Cuando le pregunto qué entrenamos, el director de sala puede darle el turno a Kimi.» |
| 0:33–0:48 | Discoteca. Escribe «Lola, vamos a la pizzería». Corte a Lola en la pizzería y a su viaje en Diálogos. | «Lola comprueba si hay cupo y si puede dejar la Discoteca. Se mueve con reglas locales. Cero llamadas al modelo. La pizza se cobra por separado.» |
| 0:48–1:07 | En el iglú, abre Mi Puffle / Spotify. Corte de vinculación a «pon Despacito». Deja leer la burbuja y muestra el título nuevo en Spotify. | «Mientras tanto, mi puffle aprendió a ser DJ. El servidor verifica que el iglú sea mío, un parser interpreta la orden y Spotify cambia de pista.» |
| 1:07–1:30 | Abre Infraestructura y recorta al panel. En Grafana, amplía el gráfico de respuestas locales y del modelo. Cierra con el SHA del plan de Terraform. | «Antes de llamar al modelo, reservo el costo. Cuando se aprieta el presupuesto, primero se callan las conversaciones entre bots. Para actualizar el Pi, el pipeline comprueba el SHA del plan aprobado.» |
| 1:30–1:45 | Toma real del móvil con desplazamiento lateral y CTA legible. | «También hice que la cámara siga al pingüino en el teléfono. Si buscas a alguien que construya software con IA y se quede a operarlo, hablemos.» |

## Ensayo y captura

- Confirma que el Pi sirve la release nueva, que los bots reconectaron y que Prometheus muestra bots, sesiones y presupuesto. Pasa por Dojo, Discoteca y Pizzería. Filma en sala para mostrar sombrero y bastón de Bruma, vestido de Lola y camiseta y patines de Peperina.
- Con una persona en la sala, genera una respuesta local y una de Kimi. Espera a que Prometheus tome la primera muestra y repite con las pausas de la conversación. Graba cuando Grafana ya muestre un incremento en ambas series y Diálogos conserve el evento `source=model`. El gráfico usa `source="llm"`. Graba el panel en otra toma si el ciclo de escritura, caché y sondeo lo retrasa. El p95 exige llamadas exitosas recientes y queda fuera de la locución.
- Para habilitar Mi Puffle / Spotify en el Pi, configura `CP_PUFFLE_WEB_PLAYERS` con los IDs de Houdini del piloto y `CP_PUFFLE_SPOTIFY_CLIENT_ID` con el ID público de su app en el archivo privado del operador. Registra `https://<dominio>/puffle/spotify/callback` en Spotify y comprueba que la cuenta esté en la lista de la app en modo desarrollo. Ensaya con un puffle clásico visible en el iglú propio, una pista distinta ya seleccionada y un reproductor Premium activo. Confirma burbuja y cambio de pista en tomas separadas si hace falta. Oculta el código de vinculación y los datos de la cuenta. [Redirect URI de Spotify](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri) · [Modo de desarrollo de Spotify](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
- Captura el escritorio con resolución completa y edita en **1080 × 1920**. Da medio segundo de contexto al abrir cada panel y corta a un recorte a pantalla completa. Usa una respuesta de chat por plano y subtítulos grandes. Revisa la exportación sin audio a 360 px de ancho. Mantén las operaciones de Spotify y las pausas del diálogo a velocidad natural.

## Texto para publicar

> Le pedí a Lola que dejara la discoteca y viniera a la pizzería. Primero comprobó que hubiera cupo y que su sala siguiera atendida. El viaje costó cero llamadas al modelo. La pizza todavía no entra en el presupuesto de IA.
>
> Lola es uno de los 26 personajes que conecté a Club Penguin en una Raspberry Pi 5. Las reglas locales se encargan del movimiento y de algunas respuestas. Para conversar, un director de sala decide quién tiene el turno y un ledger reserva el costo antes de llamar a Kimi.
>
> También me tocó operar lo que construí. Instrumenté las decisiones en Prometheus y Grafana y desplegué el lab sobre K3s con un pipeline que verifica el plan de Terraform antes de aplicarlo. Y sí, mi puffle controla Spotify.
>
> Busco un rol de Software Engineering, Applied AI o Platform/SRE. Si tu equipo busca a alguien que pueda construir una función, medirla y operarla, hablemos.
