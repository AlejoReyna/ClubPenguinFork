# Acceso público con club.example.com

Estado (3 de noviembre de 2026): el DNS de `example.com` está en Cloudflare, el
túnel `clubpenguin-pi` está activo y `https://club.example.com` responde. Pendiente
antes de compartir la URL: rotar el token del túnel y proteger el acceso (ver
[Pendientes](#pendientes)).

## Cómo está armado

```
navegador → Cloudflare → túnel clubpenguin-pi → cloudflared (K3s)
          → web.clubpenguin.svc.cluster.local:8080 → proxy WebSocket → login / glaciar
```

- El Pi sólo abre conexiones salientes hacia Cloudflare. No hay puertos abiertos en el
  router ni se depende de una IP pública fija.
- `example.com` está registrado en GoDaddy, pero sus nameservers son los de
  Cloudflare. Los registros existentes (apex y `www` hacia Vercel, `gba`, `grafana`,
  `monitor`, `cafe`, `mcp`) se copiaron en **DNS only** para no cambiar su comportamiento.
- El registro `club` lo creó el túnel (CNAME proxied). No crear un registro A manual.

## Piezas en el repo

| Pieza | Dónde |
|---|---|
| Conector `cloudflared` y política de red | `infra/cluster/cloudflared.tf` (`cloudflared_enabled = true`) |
| Origen permitido en el proxy WebSocket | `containers/runtime/socket_proxy.py` (`ALLOWED_ORIGINS`) |
| Token del túnel | Secret `cloudflared-token`, creado a mano; **no** está en Git ni en el state |

`game-ingress` sólo admite pods del lab; `web-from-cloudflared` permite al conector
llegar a `web` en el puerto 8080 y a nada más.

## Crear o rotar el token

En Cloudflare: Zero Trust → Networking → Tunnels → `clubpenguin-pi` → **Refresh token**
(o copiar el comando de instalación). En el Pi, en una sola sesión de terminal:

```bash
read -s -p "Pega el comando docker run: " T; echo
T="${T##* }"
echo ${#T} ${T:0:4}                      # esperado: unos 184 y eyJh
sudo k3s kubectl -n clubpenguin delete secret cloudflared-token
sudo k3s kubectl -n clubpenguin create secret generic cloudflared-token --from-literal=token="$T"
unset T
sudo k3s kubectl -n clubpenguin rollout restart deployment cloudflared
```

No pegar el token en un archivo, en el historial del shell ni en un chat. Si ocurre,
rotarlo. No ejecutar el comando `docker run` de Cloudflare: el conector corre en K3s.

## Ruta pública

En el túnel, **Published applications**: `club.example.com` →
`http://web.clubpenguin.svc.cluster.local:8080`.

## Verificación

```bash
curl -I https://club.example.com                       # HTTP/2 200
sudo k3s kubectl -n clubpenguin logs deploy/cloudflared --tail=20
```

Probar desde datos móviles el registro de una cuenta y la conexión de login y mundo.
Un `502` desde Cloudflare indica que `cloudflared` no llega a `web`; revisar las
NetworkPolicy del namespace.

## Pendientes

1. Rotar el token del túnel (apareció en un archivo local y en una conversación).
2. Proteger la URL con Cloudflare Access (Zero Trust → Access → Applications) o revisar
   las cuentas de demostración y las dependencias heredadas antes de abrirla al público.
3. Investigar el apagado del Pi del 3 de noviembre: `vcgencmd get_throttled`,
   `journalctl -b -1`, `free -h`.
4. Reservar la IP del Pi en el router (abajo).

## Mantener la IP del Raspberry dentro de casa

Entrar al router en `http://192.168.0.1`. Buscar **LAN / DHCP / Reserva de dirección**
(los nombres dependen del router) y reservar:

| Campo | Valor |
|---|---|
| Nombre | penguin-pi / Raspberry Pi |
| Dirección IP | 192.168.0.10 |
| MAC Wi-Fi (wlan0) | 2c:cf:67:6c:b7:87 |

Si se conecta por Ethernet, usar su MAC `2c:cf:67:6c:b7:86` y revisar la dirección
asignada. El túnel no necesita IP pública fija, pero el acceso por LAN
(`http://192.168.0.10:30080`) sí depende de esta reserva.

El Pi debe permanecer encendido y con Internet mientras se use el juego.

## Alternativa descartada: registro A y puertos abiertos

Habría requerido IP pública fija o DDNS, comprobar que no hay CGNAT, certificados TLS
propios y abrir los puertos 80 y 443 hacia el Pi. Se eligió el túnel por no exponer
puertos de casa. Sólo se publica la web y su WebSocket; PostgreSQL, Redis, la API de
Kubernetes y los puertos TCP del juego no se exponen.

Referencias: [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/concepts/),
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/).
