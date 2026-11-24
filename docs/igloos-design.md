# Iglús: cómo funcionan de verdad, y un ciclo para los pingüinos IA

Investigación para que un pingüino IA pueda ir a su iglú, invitar a alguien que esté
en la sala (bot o humano) y que el invitado acepte o rechace con reglas locales.
El modelo, si llega a hablar, solo escribe la frase. Este documento no implementa
el ciclo.

Fuentes de verdad del lab: [bots.md](bots.md), [architecture.md](architecture.md),
[operations.md](operations.md). El código de juego es el pin de `sources.json`:

- Houdini `36eaa5cbd342d743c40a8c47fe140abc710eb069`
- Dash `89ccac34883088f176c594ae55e0e93a29dac265`
- legacy-media `b3a86b70689a38fdf8773c250aa576551acb65b8`

El cliente AS2 se leyó con FFDec 26.4.0 (el mismo jar que usa
`scripts/build-puffle-client.py`) sobre los SWF de ese pin de legacy-media. Esos
SWF no viven en Git: `scripts/fetch-sources.py` los baja a `vendor/`. La
decompilación se hizo en `/tmp` y no se commitea. Las líneas de ActionScript de
abajo son las del export de FFDec (`frame_1/DoAction.as` de cada SWF).

El contrato de eventos de diálogo que quede al final lo escribe Dev Bitácora en
`docs/bots.md`. Aquí solo hay una propuesta.

## 1. Qué está verificado

### 1.1 Identificadores de sala

Hay tres números distintos y no son intercambiables.

| Qué | Valor | Dónde |
|---|---|---|
| Lo que el cliente AS2 manda en `j#jp` | `player_id + 1000` | shell, `joinPlayerIgloo` |
| Lo que Houdini resta para obtener el dueño | ese argumento menos 1000 | `handle_join_player_room_legacy` |
| Lo que Houdini pone en el `jr` (external id) | `penguin_id + 2000` | `PenguinIglooRoom.external_id` |
| Lo que el cliente guarda como sala actual | el primer id del `jr` menos 1000, o sea `player_id + 1000` | shell, `handleJoinPlayerIgloo` |
| La fila `penguin_igloo_room.id` | un serial, no es un room id de juego | `houdini.sql` |

Houdini, cliente legacy (el que usan los bots, `as2.login_legacy`):

```176:181:vendor/houdini/houdini/handlers/play/navigation.py
@handlers.handler(XTPacket('j', 'jp'), client=ClientType.Legacy)
@handlers.cooldown(1)
async def handle_join_player_room_legacy(p, penguin_id: int):
    penguin_id -= 1000
    igloo = await create_temporary_room(p, penguin_id)
    await p.join_room(igloo)
```

```181:193:vendor/houdini/houdini/data/room.py
    @property
    def external_id(self):
        return self.penguin_id + PenguinIglooRoom.internal_id

    async def add_penguin(self, p):
        ...
        await p.send_xt('jr', self.external_id, await self.get_string(f=stealth_mod_filter(p.id)))
        if not p.stealth_moderator:
            await self.send_xt('ap', await p.string)
```

`internal_id = 2000` está en `room.py:167`. El fixture del puffle ya depende de
esto: manda `%xt%s%j#jp%-1%{1000+owner}%` y espera un `jr` cuyo primer argumento
es `2000+owner` (`tests/puffle_sim/fixture_clients.py:43-48`).

El shell AS2 (`media/play/v2/client/shell.swf`):

- `sendJoinPlayerIgloo(player_id)` (línea 5470) pide antes los detalles con
  `g#gm` y, cuando llegan, llama a `joinPlayerIgloo`.
- `joinPlayerIgloo` (línea 5600) manda `j#jp` con `player_id + 1000`.
- `handleJoinPlayerIgloo` (línea 5621) hace `setCurrentRoomId(segundo_id - 1000)`.
- `handleJoinRoom` (línea 3043) trata la sala como iglú si el id del `jr` es
  `>= PLAYER_ROOM_SMART_ID_CUTOFF`. Esa constante vale 1000 (línea 9528). Un
  external id `2000+player_id` cae en la rama de iglú, y `2000+id-1000` vuelve
  a ser `1000+id`, que es la sala que el shell recuerda.

Los nombres de paquete están en `airtower.swf`, clase `Airtower`:

| Constante | Valor | Paquete |
|---|---|---|
| `NAVIGATION` | `j` | |
| `IGLOO_HANDLER` | `g` | |
| `JOIN_PLAYER_IGLOO` | `jp` | `j#jp` |
| `JOIN_ROOM` | el `jr` de navegación, no el de iglú | `j#jr` |
| `GET_IGLOO_DETAILS` | `gm` | `g#gm` |
| `GET_IGLOO_LIST` | `gr` | `g#gr` |
| `UNLOCK_IGLOO` | `or` | `g#or` |
| `LOCK_IGLOO` | `cr` | `g#cr` |
| `SAVE_IGLOO_FURNITURE` | `ur` | `g#ur` |
| `ACTIVATE_IGLOO` | `ao` | `g#ao` |
| `GET_OWNED_IGLOOS` | `go` | `g#go` |

`j#jr` no entra a un iglú. `RoomConverter` solo conoce filas de la tabla `room`.
Si la sala no existe, el handler hace `room.tables` sobre `None`
(`navigation.py:133-137`). El `change_room` de los bots manda `j#jr`
(`containers/bots/bots/as2.py:267-269`, `session.py:393-420`). Para un iglú hace
falta un camino nuevo que mande `j#jp` y espere el `jr` con external id
`2000+dueño`.

### 1.2 Qué pide y qué devuelve el servidor

Entrar (legacy):

1. El cliente manda `g#gm` con el `player_id` del dueño (`shell.swf`,
   `getPlayerIglooDetails`, línea 5491).
2. `handle_get_igloo_details` (`igloo.py:168-177`) llama a `create_first_igloo`
   y responde `gm` con el string legacy
   `{type}%{music}%{flooring}%{furniture}` (`get_legacy_igloo_string`,
   `igloo.py:49-54`). El cliente lo parsea en `handleGetPlayerIgloo` (shell,
   línea 5503): tipo, música, suelo, y muebles `id|x|y|rotación|frame`.
3. El cliente manda `j#jp` con `player_id+1000`.
4. El servidor responde `jr` con external id `2000+player_id` y el string de
   cada pingüino (`id|nickname|...|x|y|frame|...`, compilado en
   `houdini/__init__.py:125-147`). A los que ya estaban les manda `ap` con el
   mismo string.

`create_temporary_room` (`navigation.py:147-158`) resuelve el iglú así:

- si el dueño está online, usa `igloo_rooms[dueño.igloo]` (la fila activa);
- si no, carga la fila cuyo id es `penguin.igloo`.

Si no hay fila, devuelve `None` y `join_room(None)` revienta. Un `KeyError` o
`AttributeError` en un handler XT no corta el socket: el `except BaseException`
de `spheniscidae.py:186-187` lo loguea y el bucle sigue. El cliente se queda
esperando un `jr` que no llega. Por eso la fila tiene que existir antes del
`j#jp`, no como efecto colateral de un error.

`g#gm` sí crea la fila si falta (`create_first_igloo`, `igloo.py:79-89`):
`type=1` (Basic Igloo), `flooring=0`, `location=1`, y `UPDATE penguin SET
igloo=...`. El locked queda en el default de la tabla, `true`. Eso escribe en
Postgres la primera vez que alguien pide los detalles. Un bot que solo manda
`j#jp` no pasa por ahí.

El `jr` de una sala normal incluye las coordenadas que el cliente mandó en
`j#jr`, con un jitter. El `j#jp` legacy no recibe x,y
(`handle_join_player_room_legacy` solo tiene `penguin_id`). El jitter de
`RoomMixin.add_penguin` (`room.py:37-42`) se aplica igual, alrededor de la
posición anterior del pingüino: `max_users` del iglú es 80 (`room.py:170`), así
que el rango es ±20 px (`max_users // 4`). Esas coordenadas viajan en el string
del `jr`. El cliente de un iglú no las usa para dibujar (sección 1.5).

`j#grs` dentro de un iglú no devuelve el external id. `PenguinIglooRoom` no
redefine `refresh`, así que usa `RoomMixin.refresh` (`room.py:59-60`) y manda
`grs` con `self.id`, el serial de `penguin_igloo_room`. El `join_room` de los
bots exige que el `grs` repita el room id pedido (`as2.py:275-284`). En un iglú
ese id no coincide con `2000+dueño`. El camino nuevo no puede reutilizar esa
barrera tal cual.

Salir es un `j#jr` a una sala normal. El cliente, al estar en su propio iglú,
también puede caminar hacia la puerta: en `igloos/1.swf` el click de la puerta
llama a `ENGINE.sendPlayerMove(250, 270)` (frame script, línea 18). Eso es un
`u#sp`, no un cambio de sala.

### 1.3 `ap`, `rp`, `jr`. Qué son `ir` y `aj`

En este Houdini no existe un paquete `aj`.

| Paquete | Qué es aquí |
|---|---|
| `jr` | la sala te acepta y te manda el roster. En un iglú el primer argumento es el external id `2000+dueño` |
| `ap` | add player: alguien entró. El argumento es el string del pingüino |
| `rp` | remove player: alguien salió (`room.py:45-47`) |
| `p#ir` | interacción de puffle (descansar), no un iglú. `pet.py:564-570`, solo cliente legacy |

El comentario de `experiments/puffle_sim/client/renderer.as:1-6` encaja con el
shell: el servidor manda el external id `2000+id` y el shell guarda
`external_id - 1000`.

### 1.4 Abierto, cerrado y mapa

Son dos estados distintos.

La columna `penguin_igloo_room.locked` nace en `true` (`houdini.sql:966`). El
cliente vanilla la escribe con `g#uiss` (`igloo.py:324-365`) y, al entrar al
mundo, si `locked` es false, mete el iglú en el mapa
(`handle_add_igloo_map`, `igloo.py:368-374`).

El cliente legacy no toca esa columna:

- `g#or` (`handle_unlock_igloo`, `igloo.py:486-489`) mete el iglú en
  `open_igloos_by_penguin_id`. No hace `UPDATE`.
- `g#cr` (`handle_lock_igloo`, `igloo.py:492-494`) lo saca del dict con `del`.
  Si no estaba, es un `KeyError` que se loguea y la conexión sigue.
- Al desconectarse, `handle_remove_igloo_map` (`igloo.py:377-381`) lo saca del
  dict. El mapa es memoria del proceso world. Un reinicio lo vacía.

`g#gr` legacy (`igloo.py:473-483`) lista solo ese dict, como `player_id|nombre`.
Si está vacío manda `%xt%gr%-1%`.

El `j#jp` legacy no mira `locked` ni el mapa. Quien mande el paquete entra
igual. Quien usa el cliente de verdad no llega a mandarlo si el iglú no está
en la lista:

- `igloo_map.swf` pide `g#gr` al abrir (`loadPlayerIglooList`, línea 206) y
  cada iglú del mapa llama a `sendJoinPlayerIgloo(player_id)` (líneas 41 y 137).
  El botón del propio iglú (línea 202) entra sin estar en la lista.
- La ficha de jugador (`interface.swf`, `showPlayerWidget`, línea 3385) pide la
  misma lista. `handlePlayerIglooList` (línea 3399) solo enseña el botón de
  visitar si el id está en ella. El dock "home" (línea 1459) entra al iglú
  propio sin esa lista.

El candado del editor (`igloo.swf`, `openPlayerRoom` / `closePlayerRoom`,
líneas 109-116) llama a `SHELL.unlockIgloo` / `lockIgloo`. El botón pasa por
`checkMember()` (línea 1542). El handler del servidor no comprueba membresía:
un bot no miembro puede mandar `g#or` y aparecer en el mapa.

Para que un humano del cliente AS2 acepte una invitación hace falta que el
dueño haya mandado `g#or` mientras sigue conectado. Si el proceso muere, el
disconnect saca el iglú del mapa. `locked` en la base sigue en true, así que
un cliente vanilla tampoco lo reabre solo.

### 1.5 Dónde aparece el pingüino

El servidor, al añadirte, elige un punto al azar a ±20 px de tu posición
anterior y lo mete en el string (`room.py:37-42`, `penguin.py:38` arranca en
0,0). Un `j#jr` normal pisa x,y con lo que mandó el cliente antes de ese jitter
(`navigation.py:136`). Un `j#jp` no.

El engine (`engine.swf`, `onPlayerLoadInit`, líneas 805-815) hace otra cosa si
`SHELL.getIsRoomIgloo()` es true, o si el punto del servidor no pasa
`isPositionValid`: coloca al pingüino en un punto aleatorio cerca de
`start_x`/`start_y` del SWF de la sala. `RANDOM_RANGE_FOR_MAP_ENTRY` vale 80
(línea 2008): `findRandomLocation` (línea 1303) suma `random(80) - 40`. Prueba
hasta 25 veces un punto que `isPositionValid` acepte y, si ninguna vale, se
queda en el `start_x`/`start_y`. `updatePlayerPosition` (línea 1150) solo mueve
el clip local. No manda `u#sp`. Cada cliente dibuja un punto distinto hasta que
alguien camina de verdad.

`isPositionValid` (línea 848) es el hit-test de `block_mc`: un pixel relleno
está bloqueado. Es el mismo criterio que `docs/bots.md` ("Zonas medidas").

El iglú por defecto que crea `create_first_igloo` es el tipo 1, Basic Igloo
(`houdini.sql:8802`, `legacy_inventory` true). Su SWF es
`media/play/v2/content/global/igloo/igloos/1.swf`. El frame script, leído con
FFDec, dice:

```
var start_x = 380;
var start_y = 320;
var floor_x = 380;
var floor_y = 380;
var wall_x = 375;
var wall_y = 160;
```

La puerta, al soltar el click, manda `sendPlayerMove(250, 270)`.

Se exportó el `block_mc` con FFDec (`-swf2xml` y sprite PNG). PlaceObject
`block_mc`, characterId 21, sin escala, translate 0,0. El PNG mide 760×480.
Con el criterio del lab (alpha > 0 = bloqueado):

| Punto | Qué es | Máscara |
|---|---|---|
| (380, 320) | `start_x`/`start_y` del diseñador | suelo |
| (250, 270) | click de la puerta | suelo |
| (380, 380) | variable `floor_x`/`floor_y` del SWF | suelo |
| (375, 160) | variable `wall_x`/`wall_y` | bloqueado |

Eso no es una zona medida. No hay rectángulos en `rooms.json`, no se puso un
pingüino en Ruffle en las esquinas, y un pixel libre no dice que el sprite
quepa. El punto fijo que se propone para la primera versión es **(380, 320)**:
es el spawn del diseñador, el mismo tipo de dato que `spawn_source` de las
salas sin medir, y la máscara lo deja en suelo. Hasta el pase de Ruffle el
cuerpo se queda ahí. No se pasea por el iglú.

Cómo medirlo en el banco aislado, cuando toque implementarlo:

1. Correr el mismo método que `scripts/measure-room-masks.py` (grilla de 4 px,
   margen, rectángulos) sobre `igloos/1.swf`. El script hoy solo mira
   `vendor/legacy-media/.../rooms/` y el catálogo; hay que apuntarlo a este SWF
   sin cambiar el catálogo de producción.
2. En `scripts/test-containers.py` (red Docker `--internal`, Houdini desechable,
   provider simulado) un cliente sintético entra con `j#jp`, manda `u#sp` a
   (380, 320) y comprueba que el `sp` vuelve. Houdini reenvía cualquier
   coordenada (`docs/bots.md`), así que el eco no demuestra que sea caminable.
3. El pase de Ruffle (pingüino en el centro y en las esquinas del rectángulo)
   es el mismo paso manual que ya documenta `docs/bots.md`. El banco aislado
   no abre un navegador. Sin ese pase, `measured` sigue en false y el bot no
   camina.

Otros tipos de iglú tienen su propio SWF (`igloos/{id}.swf`) y su propio
`start_x`. No se decompilaron. La primera versión se queda en el tipo 1.

### 1.6 Tablas que hacen falta

Esquema en `vendor/houdini/houdini.sql` (el pin, no un dump del Pi).

| Tabla | Para qué | ¿Hace falta para estar dentro? |
|---|---|---|
| `penguin.igloo` | FK a la fila activa (`sql:536`, `sql:975`) | sí, o `create_temporary_room` no encuentra sala |
| `penguin_igloo_room` | tipo, suelo, música, location, locked (`sql:958-973`) | sí, una fila |
| `igloo` | catálogo. El id 1 es Basic Igloo, `legacy_inventory` true (`sql:8800-8802`) | la fila de catálogo ya viene en el seed |
| `flooring` | el id 0 existe ("Floor Removal") y no es inventario por defecto (`sql:7388-7389`) | el FK del room lo exige; el jugador no necesita poseerlo |
| `location` | el id 1 es Default, `legacy_inventory` true (`sql:8898-8899`) | igual |
| `penguin_igloo` | tipos que el pingüino posee (`sql:1046`) | no para entrar. El login los rellena |
| `penguin_location` | locations poseídas (`sql:1008`) | no para entrar |
| `penguin_flooring` | suelos poseídos (`sql:944`) | no. Ningún suelo tiene `legacy_inventory` |
| `penguin_furniture` | muebles poseídos (`sql:928`) | no. Ningún mueble del seed tiene `legacy_inventory` (1384 filas, 0 en true) |
| `igloo_furniture` | muebles colocados (`sql:1022`) | no. Un iglú vacío es un string de muebles vacío |
| `igloo_like` | likes, cliente vanilla (`sql:987`) | no |

Dash, al registrar, crea `penguin`, un `penguin_item` del color y la postal
125. No toca iglús (`vendor/dash/dash/routes/create/legacy.py:164-175`).
`scripts/create-bot-accounts.py` registra por Dash y, en modo cast o work,
corrige el nickname. No inserta `penguin_igloo_room`.

En el primer login al world, `load_igloo_inventory` (`igloo.py:134-165`,
enganchado al XML `login`) inserta el inventario por defecto del cliente
legacy: el iglú 1 y la location 1. No crea la fila de sala ni rellena
`penguin.igloo`.

El puffle del lab inserta la sala a mano porque el registro no la trae
(`scripts/puffle_integration.py:23-25` y el mismo SQL en
`experiments/puffle_sim/run_houdini_fixture.py:156-158`).

Los mascots del seed (`houdini.sql:11626`) tampoco traen columna `igloo`.

Conclusión de código: una cuenta recién registrada no tiene iglú. La fila
aparece cuando alguien manda `g#gm` de ese id, o cuando un script la inserta.
No se consultó la base del Pi (queda en "no verificado").

### 1.7 Cómo se mueven hoy `life`, `travel` y `director`

El cuerpo es local. `Life` elige actividad cada paso con un
`random.Random(life_seed ^ crc32(username))` (`life.py:31-33`). El horario de
la ficha solo aplica si el bloque cubre la hora y la sala actual
(`life.py:68-78`). Energía bajo 0.2 obliga a `rest` (`life.py:79`). Hambre
sobre 0.85 obliga a `eat`, y si 330 está en `wander.favorites` el viaje pide
la pizzería (`travel.py:311-312`). `work` no está en `ROAMING`
(`actions.py:41-42`): en un bloque de trabajo el cuerpo se queda en el puesto.

`Life.run` llama a `travel.consider` en cada paso (`life.py:227-232`).
`consider` solo corre si `life.autonomous` es true (`travel.py:321-334`).

`Travel` guarda tres dicts en RAM: `reservations`, `leases`, `jobs`
(`travel.py:79`). Nada de eso se escribe a disco. Al cerrar el director se
cancelan y se vacían (`travel.py:386-394`). Un reinicio del pod entra otra vez
por `_enter_world`, que hace `j#jr` a `bot.room` (`session.py:227-241`). Nadie
queda en un iglú del lado del bot. Del lado del world, el disconnect saca al
pingüino de la sala (`navigation.py:189-194`) y del mapa de iglús.

Reservas (`travel.py:_check`, líneas 148-193):

- sin ficha, o `wander_home_share >= 1`: `fixed`, no viaja;
- la sala destino tiene que estar en el catálogo, con `spawn` y `spawn_source`;
- un evento del calendario que tenga al pingüino `pinned` lo retiene
  (`events.py:35-47`, y ahí sí hay nombres escritos: dontelon, divaperla,
  bruno, kiki, bruma, tormenta, avalancha);
- si habló con un humano distinto del que pide el viaje, en los últimos
  `conversation_seconds` (default 180): `conversation`
  (`world.py:99-101`);
- de una sala del plan no se bajan del mínimo. El mínimo es `max(1, residentes-1)`.
  Con un solo residente y un humano dentro, no se va (`travel.py:179-183`).
  Una reserva en vuelo cuenta como salida, así que dos no se van a la vez;
- el destino no puede pasar de `min(bot_cap, residentes+2)` contando a los
  residentes que todavía no han vuelto (`travel.py:185-192`);
- el tiempo fuera lo limita el crédito de casa (`travel.py:93-108`,
  `visit_seconds` default 180, tope 900). Menos de 30 s de crédito es
  `budget`.

Una invitación humana ("vamos a la pizzería", "sigueme") la detecta
`invitation()` con alias de sala (`travel.py:24-38`). El director la ofrece
antes de llamar al modelo y, si la reserva falla, dice una frase local
(`director.py:653-678`). El modelo puede proponer `follow` o `go:N`, y el
código vuelve a pasar por `offer` (`director.py:719-727`). Un iglú no está en
`ROOM_ALIASES` ni en el catálogo: hoy esa frase no lleva a ningún sitio, y un
`reserve` hacia `2000+id` caería en `unknown`.

El director ignora el chat de una sala que no está en el catálogo
(`director.py:350-354`). Un iglú, con room id `2000+dueño`, hoy es invisible
para saludos, respuestas y escenas. El lector de la sesión sí actualiza el
world model cuando llega un `jr` (`session.py:319-322`).

Escenas bot-bot: `scene()` exige un humano en la sala y al menos dos bots
(`director.py:787-795`). Una sola llamada al modelo escribe todas las frases,
prioridad P2 (`brain.py:218-232`). Si no hay humano, no se encola.

P2 se corta al 80 % del tope diario, P1 al 95 %, P0 solo al pasar el tope
(`spend.py:38-41`). Una línea rechazada por el ledger usa el fallback de la
ficha. Sin humano presente no hay llamada (principio 2 de `docs/bots.md`). El
límite de 40 respuestas de modelo por humano y día es P0
(`director.py` `_model_slot`, default `player_daily_limit` 40).

El otro tope bot-bot vive en el behavior viejo, el que corre cuando no hay
director: `respond_to_bots` default false y `bot_exchange_limit` default 2
(`behavior.py:150-160`, `config.py:737-739`). Con el director activo, el chat
de un bot no entra a `on_chat` porque `is_human` es false.

### 1.8 Quién trabaja y quién pregona

No hay un campo `role` en la ficha. `_CARD_KEYS` (`cast.py:27-31`) rechaza
claves desconocidas. `activity: "work"` es una etiqueta de horario, no un
cargo: deja el cuerpo en el puesto, y también la usan Mimi, Bruno y Kiki.

Evidencia, sin convertirla en una lista de nombres dentro del código:

| Quién | Dónde está escrito | Sala | Qué hace |
|---|---|---|---|
| Juanito | `scripts/bot_accounts.py` `WORK_ROLES` / `WORK_VISIBLE`, username `baristaia` | 110 cafetería | puesto medido, sin ficha en `cast/` |
| Viterbos | igual, username `pizzeroia` | 330 pizzería | puesto fijo `(397, 274)`, sin ficha |
| Cadence | igual, username `djia` | 120 disco | puesto fijo, sin ficha. Es el DJ, no la barra ni el horno |
| Peperina | `cast/peperina.json` | 330 | `activity: "work"` 12:00–23:00, `home_pois: counter`, relación `companero_de_trabajo` con Viterbos |
| Paco Pregón | `cast/pacopregon.json` | 300 | `activity: "patrol"` 10:00–22:00, "sabe dónde está cada pingüino". No es `work` |
| GuiaIA | `scripts/bot_accounts.py` `PERSONAS` | 100 en `config_document` | texto "explica lugares". No hay `cast/guia*.json` |
| Mimi, Bruno, Kiki | sus fichas | 310, 340, 340 | también tienen un bloque `work`. No son la barra, el horno ni el guía |

`events.py:40` ya lista nombres para clavar actores al teatro, la carrera y el
dojo. El ciclo de iglús no debe copiar ese patrón.

## 2. Qué se infiere (sigue de dos hechos verificados, no de una suposición suelta)

- Un humano del cliente AS2 solo acepta la invitación si el iglú está en el
  mapa (`g#or` previo) o si entra por un `j#jp` que algún otro UI mande. El
  servidor dejaría entrar igual con el iglú cerrado. La invitación, para que
  se pueda aceptar con el cliente de serie, incluye abrir el iglú.
- El primer `g#gm` crea la fila. Si el bot ya está online cuando el SQL corre,
  Houdini sigue con `igloo_rooms` cargado en el login. El script va con los
  bots parados, como ya exige `create-bot-accounts.py` antes de tocar
  nicknames. Al volver a arrancar, el login lee la fila.
- `(380, 320)` es un sitio razonable para quedarse quieto porque es el spawn
  del SWF y el pixel de `block_mc` está libre. Que el sprite no pise la pared
  lo confirma Ruffle, y eso no se ha hecho.
- Una escena dentro del iglú necesita que el director trate `2000+dueño` como
  sala observable. Si no, el humano entra y nadie contesta, porque el room id
  no está en el catálogo.

## 3. Qué no se pudo verificar

- Las filas vivas de las cuentas del Pi. No se abrió Postgres, no se usó
  kubectl, no se leyó el Secret. El código de alta no las crea. Puede que
  alguna exista si alguien mandó `g#gm` de ese id, o si un fixture de puffle
  se corrió contra esa base. El script de la sección 6 es idempotente por eso.
- Ruffle con un pingüino encima de `(380, 320)` y de un rectángulo de la
  máscara. El banco aislado no tiene navegador.
- Los `start_x`/`start_y` de los iglús que no son el tipo 1.
- Si el cliente, además del mapa y de la ficha, tiene otro botón de "entrar"
  escondido en un SWF que no se exportó. Se decompilaron `shell.swf`,
  `engine.swf`, `igloo.swf`, `interface.swf`, `airtower.swf`, `igloo_map.swf`
  e `igloos/1.swf`.
- El tamaño real en bytes del WAL de un `INSERT` en el Postgres del Pi. La
  estimación de la sección 8 es de orden, no una medida.

## 4. Diseño del ciclo

Todo el movimiento y la decisión viven en el proceso de bots, al lado de
`Travel`. El modelo no elige ir, no elige a quién, no elige aceptar. Escribe
una frase cuando las reglas de gasto ya dijeron que hay un humano escuchando.

### 4.1 Quién puede

Campo nuevo en la ficha, al lado de los que ya valida `cast.py`:

```json
"role": "helper"
```

Valores: `helper`, `guide`, o ausente. Ausente significa residente normal.
Cualquier otro valor es `CastError`, igual que una clave desconocida hoy.
`helper` y `guide` no invitan y no aceptan, da igual la hora, la energía y
quién pida. No hay una tupla de usernames en Python.

Valores que este diseño dejaría escritos en las fichas, para que Alexis los
confirme antes de implementar (sección 9):

- Peperina: `helper`
- Paco Pregón: `guide`

Juanito, Viterbos y Cadence no tienen ficha. Hasta que la tengan, o hasta que
el mismo campo exista en la entrada de credenciales, el ciclo no los conoce y
no debe adivinarlos por el número de sala. Quedan fuera de la primera versión
si siguen sin ficha: un puesto fijo con `wander_home_share` implícito de 1 ya
no viaja (`TravelDenied('fixed')`), y sus credenciales de trabajo no pasan por
`Card`. Hay que confirmar que el config de trabajo sigue marcándolos
`stationary` (Juanito es el único de los tres que camina, dentro de su
rectángulo medido de la 110).

Un bloque `work` que cubra la hora actual también retiene al pingüino, aunque
no tenga `role`. Así Mimi no abandona la tienda a media tarde, y puede ir
cuando el bloque no cubre. Eso reutiliza el horario que ya está en la ficha.
No sustituye a `role`: Paco es `patrol`, no `work`, y aun así es el guía.

### 4.2 Cuándo se va el anfitrión

En el mismo paso en que `travel.consider` mira si toca pasear, una tirada
aparte puede pedir iglú. Condiciones, todas locales:

1. la ficha no tiene `role` `helper` ni `guide`;
2. no hay bloque `work` cubriendo esta hora en la sala de casa;
3. no está `pinned` por el calendario, ni en un lease, ni en una reserva;
4. `conversation_locked` es false y no hay pareja de charla en el último minuto
   (`director.partner`);
5. energía ≥ 0.2 (si no, `Life` ya eligió `rest`);
6. hay alguien en la sala a quien invitar: un humano, o un bot con ficha que
   pase las mismas condiciones. Sala vacía de invitados: no se hace nada;
7. la reserva de ocupación de la sala de origen pasa el `_check` actual. Irse
   al iglú es irse de la sala del plan. El mínimo con humano dentro se respeta
   igual, y la reserva impide que el de al lado se vaya en el mismo tick.

La tirada usa el mismo estilo de semilla que `Life`:

```text
Random(life_seed ^ crc32(username) ^ crc32(f"{fecha_local}:{hora_bloque}"))
```

Probabilidad propuesta de considerar el iglú: 0.05 por paso de vida cuando las
condiciones pasan, y solo si el crédito de casa da para al menos 30 s (el
mismo suelo que `TravelDenied('budget')`). Con `life` entre 20 y 60 s, eso es
poco frecuente. El número concreto queda en las preguntas.

El destino de ocupación no es una sala del catálogo. El chequeo de origen se
reutiliza (mínimo, un solo residente con humano, crédito, conversación). El
iglú no cuenta para el `bot_cap` de una sala del plan. El cupo del iglú, en
esta versión, es el dueño más un invitado.

### 4.3 La invitación

No hay paquete de invitación en el protocolo. El anfitrión:

1. toma la reserva de salida (para que el mínimo ya cuente);
2. elige invitado en la sala: si hay humano, el humano; si no, un bot. Entre
   bots, primero uno que tenga una fila `relationships` cuyo `with` mencione
   al anfitrión, si no cualquiera que pueda aceptar. Una sola invitación;
3. abre el iglú con `g#or` (hace falta la fila, sección 6);
4. dice una frase en el chat de la sala;
5. entrega al invitado un intent en proceso, el mismo tipo de objeto que
   `travel.offer`. La frase no es la orden. Un fallback o un filtro no pueden
   convertir un "no" en un "sí".

La frase, si hay un humano en la sala, es una llamada P2 (una línea, el
modelo la escribe en personaje). Si la sala no tiene humano, la frase sale de
`greetings` o `fallbacks` y no hay llamada. Si el ledger niega P2, también
fallback. El intent se entrega igual: la decisión del invitado no depende de
que la frase haya salido del modelo.

Si el invitado es humano, el intent no existe. El humano usa el mapa o la
ficha, que ya ven el iglú porque hubo `g#or`. El bot se va a su iglú igual y
espera el lease. Si el humano no aparece, vuelve (abajo).

### 4.4 El invitado decide

Función pura, testeable sin red y sin modelo. Entrada: ficha del invitado,
ficha del anfitrión, hora local, energía, si está en conversación con un
humano, si el anfitrión es humano o bot, si esta pareja ya se rechazó hoy,
semilla. Salida: `accept` o `reject` más un motivo de la lista cerrada
(`role`, `on_duty`, `busy_human`, `pinned`, `lease`, `energy`, `occupancy`,
`recent_reject`, `roll`).

Rechazo fijo, sin tirada:

| Motivo | Regla |
|---|---|
| `role` | `helper` o `guide` |
| `on_duty` | el bloque de ahora es `work` |
| `busy_human` | `conversation_locked` o `partner` con un humano |
| `pinned` | el calendario lo quiere en su sitio |
| `lease` | ya tiene lease, reserva, o está en un iglú |
| `energy` | energía < 0.2 |
| `occupancy` | irse dejaría una sala del plan bajo su mínimo con un humano dentro, o es el único residente y hay humano |
| `recent_reject` | esta pareja (ordenado por player id) ya tuvo un reject en las últimas 2 h de reloj local. Memoria del proceso, tope 64 parejas, se olvida al reiniciar |

Si nada de eso dispara, hay tirada. Semilla:

```text
Random(life_seed ^ crc32(invitado) ^ crc32(f"{fecha_local}:{anfitrion_id}"))
```

Misma semilla, mismo día, mismo par: misma decisión. Eso es lo que el test
fija.

Probabilidad de aceptar, propuesta:

- base 0.35 si invita un bot;
- base 0.70 si invita un humano (te habló a ti: sigue siendo una tirada, no un sí automático);
- +0.20 si la ficha del invitado tiene una `relationships` hacia el anfitrión cuyo `kind` no está en el conjunto frío `{rival_amistosa, rival_amistoso, evitado_por, no_soporta_a, discute_con, regana_a, reganado_por}`;
- −0.15 si energía < 0.45;
- +0.10 si `social` > 0.6.

Se recorta a [0.05, 0.95]. `rng.random() < p` acepta. Los `kind` fríos son los
que ya existen en `cast/`; un `kind` nuevo no está en el conjunto y suma, hasta
que alguien lo añada a la lista fría en la ficha de reglas, no en un `if`
por nombre de pingüino.

El rechazo reciente se apunta cuando sale `roll` en reject o cuando sale
`busy_human`. Un `role` no gasta cooldown: no hace falta recordar que el guía
dijo que no.

La frase de la decisión:

- humano presente en la sala del invitado, y el ledger deja pasar la
  prioridad: el modelo escribe una frase en personaje. Si el humano invitó,
  la prioridad es P0 (le contestas a él) y cuenta para sus 40 del día. Si
  invitó un bot y el humano solo está ahí, es P2 y se corta al 80 %;
- sin humano, o con el ledger cerrado: una línea de `fallbacks` (rechazo) o
  de `greetings` (aceptación). Cero llamadas.

El motivo no se le cuenta al modelo como instrucción de herramientas. Entra
en el bloque de situación, como dato, igual que el resto de `brain.situation`.

### 4.5 Ir, quedarse, volver

Aceptar:

1. el invitado toma su propia reserva de salida (otro `_check`);
2. los dos mandan `j#jp` al iglú del anfitrión (`1000+id` del dueño);
3. esperan el `jr` con `2000+id`. No usan la barrera `grs` actual;
4. cada uno manda `u#sp` a (380, 320). El servidor lo reenvía (`u#sp` no
   valida). Los clientes dejan de mostrar puntos distintos;
5. se abre un lease de `visit_seconds` (default 180), acotado por el crédito
   de casa de cada uno, con el mismo reloj que `Travel.tick`.

Durante el lease el cuerpo no pasea: la sala no está medida. Si hay un humano
dentro del iglú (el world model ya tiene el roster del `jr`, con room id
`2000+dueño`), el director puede encolar una escena. Si el humano se va, la
escena no arranca y la que estuviera en cola se tira, igual que
`director._valid` exige `has_humans`.

Volver cuando se cumple lo primero de:

- el lease expira (se resta `departure_max`, como en `travel.py:345`);
- el humano invitado no llegó en 8 s (el mismo `lost_since` de un follow);
- el invitado bot se desconecta;
- un humano con el que el anfitrión estaba hablando aparece en la sala de casa
  (no se implementa en la primera versión: el paso 4 de la sección 4.2 ya
  impide salir en medio de una charla).

La vuelta es `j#jr` a `bot.room` en el spawn de la ficha, `life.position` se
actualiza, y `g#cr` solo si este proceso mandó el `g#or`. Luego se suelta el
lease. La reserva de vuelta es un homecoming: `_check` no le aplica el mínimo
de "no te vayas", le aplica el de la sala donde está, que no es una sala del
plan. Hay que mirar este caso en el test para que dos vueltas simultáneas no
se pisen el `body_lock`. No hace falta un mínimo dentro del iglú.

Nadie invita desde dentro del iglú. El invitado no puede aceptar otra
invitación con el lease puesto. Eso corta el bucle bot-bot: A invita a B, B
no invita a C, y al volver los dos tienen `recent_reject` o un cooldown de
visita de la misma pareja (la visita cuenta como la pareja ocupada 2 h, el
mismo reloj que el rechazo).

### 4.6 Dónde se engancha

| Pieza | Cambio, cuando se implemente |
|---|---|
| `cast.py` | aceptar `role` opcional |
| `travel.py` | kind `igloo`: misma reserva de origen, destino fuera del catálogo, lease, cooldown de pareja |
| `session.py` | `join_igloo(owner_id)` que manda `j#jp`, espera `jr` `2000+owner_id`, manda `u#sp`, y registra el room id en el world. El reader se aparca como en `change_room` |
| `as2.py` | el envío de `j#jp`, `g#or`, `g#cr`, `g#gm`. `g#gm` una vez por sesión si queremos el string, no para crear la fila |
| `director.py` | observar salas `>= 2000` solo para chat y escenas mientras el lease viva; la invitación humana "iglú" al lado de `invitation()` |
| `life.py` | el paso que llama a la tirada del anfitrión, y no caminar si `room_id >= 2000` |
| `brain.py` | la frase, con el motivo como dato. Sin acción nueva: `do` sigue siendo null, dance, wave o sit |
| `world.py` | `has_humans` ya funciona si el snapshot usó el external id. No persistir leases |

`rooms.json` no gana una sala 2000. El id depende del player id.

## 5. Límites que el ciclo respeta

- Sala del plan con humano: se usa el `_check` de ahora. Un único residente no
  se va si hay humano. Dos reservas no salen a la vez. El iglú no es un atajo.
- Lease con vuelta. Al reiniciar, `_enter_world` hace `j#jr` a casa y el mapa
  de iglús del world se vacía en el disconnect. No hay estado en el PVC.
- Sin bucles: una invitación, un invitado, nadie invita desde el iglú, la
  pareja queda en cooldown.
- Escena bot-bot con modelo solo si `has_humans` en el iglú, una llamada para
  todas las frases, P2. Sin humano, silenciosos o una línea de `fallbacks`,
  y el ledger ni se toca.
- P2 se corta al 80 %. Una frase de rechazo cuando el humano te invitó es P0
  y entra en las 40 del día. Un fallback no gasta una de esas 40 (el director
  ya suelta el slot si la línea no salió del modelo).
- El modelo no añade herramientas ni un `do` nuevo.

## 6. Eventos de diálogo (propuesta)

Dev Bitácora cierra los nombres y el JSON. Propuesta, para no inventar un
segundo contrato en el código antes de eso:

| Evento | Quién habla | Prioridad | Cuándo hay modelo |
|---|---|---|---|
| `igloo_invite` | el anfitrión, una frase | P2 si hay humano en la sala de origen; si no, local | humano presente y P2 abierto |
| `igloo_answer` | el invitado, una frase, con el motivo ya decidido | P0 si el anfitrión es humano; P2 si es bot y hay humano; local si no | igual |
| `igloo_scene` | los que están en el iglú, una llamada, una frase cada uno, como `scene` | P2 | humano dentro del iglú y P2 abierto |

No son eventos del calendario de `events.py`. No llevan nombres de personajes.
Si P2 está cerrado, `igloo_scene` no se encola: mejor callar que recitar
fallbacks en bucle.

## 7. Provisionar filas, sin ejecutarlo

Hace falta una fila por cuenta que vaya a abrir su iglú: los residentes que
no son `helper` ni `guide`, más cualquiera que pueda recibir visita. Los
ayudantes y el guía no la necesitan para el ciclo, porque no participan. Crearla
igual no molesta y deja el `g#gm` de un humano curioso sin sorpresa. El script
puede cubrir todas las cuentas bot.

No se corre contra el Pi ni contra ningún Postgres desde este PR. Esquema, en
una transacción, idempotente. Los usernames salen de la lista que el operador
pase (fichas enabled más cuentas de trabajo), validados como ya hace
`cast_nickname_sql` (`[a-z0-9]{4,12}`), nunca concatenados desde un chat.

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS igloo_provision_audit (
  penguin_id INTEGER PRIMARY KEY REFERENCES penguin(id),
  previous_igloo INTEGER,
  inserted_room_id INTEGER,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

WITH targets AS (
  SELECT id, igloo AS previous_igloo
  FROM penguin
  WHERE username IN (/* lista validada */)
    AND active
    AND NOT moderator
    AND NOT stealth_moderator
),
inserted AS (
  INSERT INTO penguin_igloo_room (penguin_id, type, flooring, location, music, locked)
  SELECT t.id, 1, 0, 1, 0, true
  FROM targets t
  WHERE t.previous_igloo IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM penguin_igloo_room r WHERE r.penguin_id = t.id
    )
  RETURNING id AS room_id, penguin_id
),
audit AS (
  INSERT INTO igloo_provision_audit (penguin_id, previous_igloo, inserted_room_id)
  SELECT t.id, t.previous_igloo, i.room_id
  FROM targets t
  LEFT JOIN inserted i ON i.penguin_id = t.id
  ON CONFLICT (penguin_id) DO NOTHING
  RETURNING penguin_id
)
UPDATE penguin p
SET igloo = COALESCE(
  p.igloo,
  (SELECT i.room_id FROM inserted i WHERE i.penguin_id = p.id),
  (SELECT min(r.id) FROM penguin_igloo_room r WHERE r.penguin_id = p.id)
)
FROM targets t
WHERE p.id = t.id
  AND p.igloo IS NULL;

COMMIT;
```

Qué hace: si `penguin.igloo` ya apunta a una fila, no toca nada. Si hay una
sala huérfana y `igloo` es null, apunta a la de menor id y no inserta otra.
Si no hay nada, inserta tipo 1, suelo 0, location 1, locked true, música 0.
`igloo_provision_audit` recuerda el valor anterior y el id insertado. Un
segundo pase no inserta porque `previous_igloo` ya no es null, y el `ON
CONFLICT DO NOTHING` deja el audit quieto.

Rollback de lo que este script haya hecho, y solo eso:

```sql
BEGIN;

UPDATE penguin p
SET igloo = a.previous_igloo
FROM igloo_provision_audit a
WHERE p.id = a.penguin_id
  AND a.previous_igloo IS NULL
  AND p.igloo IS NOT DISTINCT FROM COALESCE(
    a.inserted_room_id,
    (SELECT min(r.id) FROM penguin_igloo_room r WHERE r.penguin_id = p.id)
  );

DELETE FROM penguin_igloo_room r
USING igloo_provision_audit a
WHERE r.id = a.inserted_room_id
  AND a.inserted_room_id IS NOT NULL;

DELETE FROM igloo_provision_audit;

COMMIT;
```

`igloo_furniture` e `igloo_like` caen por el `ON DELETE CASCADE` de sus FK si
la fila insertada se borra. No se insertan muebles. No se toca el inventario
que el login haya creado (`penguin_igloo`, `penguin_location`): eso no es de
este script y el rollback no lo borra.

Orden operativo, el día que se ejecute, fuera de este PR: parar el Deployment
de bots y esperar a que no quede ningún pod, correr el SQL contra la base del
lab, revisar el `RETURNING`, arrancar. Igual que el comentario de
`create-bot-accounts.py` sobre no migrar con el proceso en caliente. El world
que ya esté corriendo no ve la fila hasta el próximo login de esa cuenta.

## 8. Pruebas en el banco aislado

El patrón es el de `scripts/test-containers.py` y `scripts/life_integration.py`:
red Docker `--internal`, Houdini y Postgres desechables, provider `simulated`,
y una comprobación de que no hay requests HTTP de provider. Nada de Moonshot.
Nada del Pi.

Casos, cada uno con semilla fija y reloj inyectado:

1. Ir y volver. Un residente entra con `j#jp`, el `jr` trae `2000+su id`,
   manda `u#sp` 380,320, el eco `sp` llega, al expirar el lease manda `j#jr` a
   su `home_room` y el world model queda en esa sala. `g#cr` después de `g#or`.
2. Aceptar y rechazar con la misma semilla. Dos pasadas, misma fecha, mismo
   par: el mismo `accept` o `reject` y el mismo motivo. Una tercera pasada con
   la energía forzada bajo 0.2 es `energy`, sin tirada.
3. Ayudante y guía. Fichas con `role: helper` y `role: guide` reciben el
   intent y siguen en su sala. Cero `j#jp`. El anfitrión no los elige.
4. Humano invita. Un cliente sintético dice una frase con "iglú" nombrando al
   bot. El bot tira su decisión local. Si rechaza, se queda. Si acepta, abre
   `g#or`, entra, y el humano puede entrar con `j#jp` porque `g#gr` lista al
   dueño. La métrica de provider sigue en cero porque el provider es simulado
   y, además, el test mira que no aparezca `bots_provider_requests_total`.
5. Sin bucles y sin salas vacías. Sala del plan con un residente y un humano
   dentro: el motivo es `occupancy` y el bot no sale. Dos bots en una sala sin
   humano: no hay `igloo_scene` y no hay llamada. Un bot dentro del iglú no
   emite otro intent.
6. Tras reinicio. Matar el proceso de bots (o el world) a mitad de lease. Al
   volver, cada sesión está en su `home_room` (`j#jr` de `_enter_world`). Un
   `g#gr` no lista a nadie. No queda lease en memoria.

La máscara y Ruffle no forman parte de este banco. Quedan como paso de
medición, sección 1.5, antes de soltar el punto fijo a otra cosa que no sea
quedarse quieto.

## 9. Costo en el Pi

El servicio sigue siendo un proceso, una réplica. El ciclo no carga SWF, no
abre otra conexión y no arranca otro contenedor.

RAM: un lease y una reserva por visita, más un dict de parejas con tope 64.
Eso cabe en el proceso que ya tiene 26 sesiones. No se copia el PNG de
`block_mc` al pod.

CPU: una tirada por paso de vida (el paso ya existe, 20–60 s) y, en una
visita, dos `j#jp`, dos `u#sp`, un `g#or` y un `g#cr`. El world ya hace ese
trabajo para cualquier jugador.

microSD: el script de la sección 7 escribe, una vez por cuenta, una fila de
`penguin_igloo_room`, un update de `penguin.igloo` y una fila de audit. Son
decenas de filas pequeñas, no un flujo. En caliente, `g#or` y `g#cr` no hacen
`UPDATE`. No se llama a `g#ur` (guardar muebles), que sí borraría e insertaría
`igloo_furniture` en cada visita. El ledger de gasto solo se escribe cuando
hay una reserva de modelo, y esa reserva solo existe con un humano presente y
por debajo del corte de la prioridad. Un día vacío de humanos no añade
escrituras respecto a hoy. El login ya escribe `minutes_played` al
desconectar; este ciclo no añade otro update por visita.

No hay número medido de bytes de WAL. El orden es el de unas pocas filas al
provisionar, y después el mismo patrón de escritura que un día normal de bots.

## 10. Preguntas para Alexis

1. ¿Peperina `helper` y Paco Pregón `guide` son los dos cargos de las fichas,
   o el guía es otro (GuiaIA está en `PERSONAS` y no tiene ficha)?
2. Juanito, Viterbos y Cadence no tienen ficha. ¿Les hacemos una con `role`,
   o el campo también vive en el JSON de credenciales de trabajo? Cadence es
   el DJ: ¿queda fuera del iglú como ellos, o solo fuera de su turno?
3. Mimi, Bruno y Kiki tienen bloque `work` y no son barra, horno ni guía. El
   diseño los deja ir cuando ese bloque no cubre la hora. ¿Te vale, o también
   llevan `role`?
4. Probabilidades: 0.05 de plantearse el iglú por paso, 0.35 de aceptar a un
   bot, 0.70 de aceptar a un humano, cooldown de pareja 2 h, lease el
   `visit_seconds` de ahora (180 s). ¿Alguno lo quieres distinto?
5. ¿El anfitrión abre el iglú con `g#or` solo durante la visita, para que el
   mapa y la ficha enseñen el botón? Es lo que el cliente AS2 necesita.
6. ¿La primera versión se queda quieta en (380, 320) hasta que alguien pase
   Ruffle, o la visita espera a la zona medida?
7. ¿El SQL de la sección 7 se corre en el Pi con los bots parados, o prefieres
   que el primer `g#gm` cree la fila solo?
8. Los nombres `igloo_invite`, `igloo_answer` e `igloo_scene`, y que la escena
   sea P2 con una sola llamada, ¿los das por buenos para que Dev Bitácora los
   cierre en `docs/bots.md`?
