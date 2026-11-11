# Banco local de iglús

Base: `edbd5c9` (#22), worktree separado `clubpenguin-lab-wt/vida`.
El diseño de referencia sigue en el PR #19; este banco no lo mergea.

## Repetir sin saldo

Docker Desktop encendido en la Mac ARM64. Desde el worktree:

```bash
python3 scripts/fetch-sources.py --only houdini,dash
python3 scripts/build-images.py --components runtime --tag vida-iglus-runtime --platform linux/arm64
python3 scripts/build-images.py --components bots --tag vida-iglus-local --platform linux/arm64
bash scripts/run-bot-tests.sh clubpenguin/runtime:vida-iglus-runtime
python3 -u scripts/test-containers.py --tag vida-iglus-runtime \
  --component-tag database=lab-002 --bots-tag vida-iglus-local --igloos
```

`database:lab-002` y `redis:7.4-alpine` deben estar disponibles localmente.
La imagen de bots debe coincidir byte por byte con las fuentes del worktree.
El arnés crea una red Docker `--internal`, verifica que no tenga salida y usa
cuentas nuevas, sin montar kubeconfig, credenciales privadas ni claves de proveedor.
Solo el Postgres desechable recibe SQL. La limpieza usa la etiqueta única de la corrida.

El modo `--igloos` usa un elenco pequeño en lugar de `--live-cast`:

- Aplica dos veces la provisión SQL, compara las referencias activas, ejecuta
  el rollback y vuelve a provisionar antes del login.
- Conecta ocho sesiones reales de `BotManager`: tres residentes y los cinco
  ayudantes/guía, con sus fichas y puestos de trabajo.
- Fija semilla 42, fecha y parejas para repetir aceptación y rechazo; también
  comprueba el rechazo sin dado por energía baja.
- Comprueba la exclusión por `role`, tanto al invitar como al reservar salida.
- Reserva una salida y rechaza una segunda que rompería el mínimo de sala.
- Entra al iglú por `j#jp`; un cliente humano consulta el mapa y el roster,
  observa `(380,320)` y verifica que no nace otra invitación desde dentro.
- Expira el lease, observa la vuelta a casa y la retirada del mapa.
- Un humano sintético manda «Lola vamos a tu iglu» por AS2; el director
  recibe el mensaje y el bot rechaza con energía baja.
- Mata el proceso con `docker kill` durante otra visita, arranca un proceso
  nuevo con las mismas cuentas y comprueba casas, leases y mapa vacíos.
- Rechaza cualquier serie de requests HTTP del proveedor; modo simulado.

No acredita Ruffle, calidad del modelo, aceptación humana, carga, disponibilidad
sostenida ni el estado de las cuentas reales del Pi. El punto sigue fijo y no
se declara una zona medida. La aceptación/rechazo con dado se verifica en las
reglas reales; el chat humano de extremo a extremo cubre el rechazo por energía.

## Evidencia (11 nov 2026)

Mac, Docker `linux/arm64`, base `edbd5c9`:

- Suite aislada: **353 pruebas, 0 fallos, 0 errores** (36.755 s).
- Banco: **PASS** en red sin salida, registro/login, proxy, SQL idempotente
  y rollback, decisiones fijas, cinco cargos excluidos, visita y vuelta,
  invitación humana y reinicio abrupto con las ocho cuentas en casa.
- Runtime local: `vida-iglus-runtime`, imagen
  `sha256:24de62ad0fbe47894858388163bf34448e5727fbb4f54fcc9bd465aa6bfb8bc9`.
- Bots locales: `vida-iglus-local`, imagen
  `sha256:1e004963e20622fd17b2ed4a95fa1bcdb3557221e444b5d596b4f0c52b861d05`.
- La primera corrida con `rt-origin-001` falló porque esa imagen histórica
  no tiene `MODE=puffle`; se reconstruyó el runtime desde el worktree.
  La primera suite carecía de `vendor/houdini`; tras descargar los pins,
  la suite completa pasó. No fueron cambios al código de bots.

## Pasos manuales para Alexis

Este PR solo cambia pruebas: no requiere deploy ni cambios en el Pi.
Para revertirlo basta omitir `--igloos` o revertir su commit.
No desplegar `main` hasta provisionar las cuentas y revisar el PR #19.
El deploy de Bitácora desde `9daf136` es independiente.

Preparar archivos SQL desde el checkout revisado, sin ejecutar nada remoto:

```bash
umask 077
mkdir -p .local/igloos
python3 scripts/provision-igloos.py --cast containers/bots/cast > .local/igloos/provision.sql
python3 scripts/provision-igloos.py --rollback > .local/igloos/rollback.sql
```

Solo Alexis, cuando decida provisionar, con acceso al API ya disponible:

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin scale deployment/bots --replicas=0
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin wait --for=delete pod -l app=bots --timeout=120s
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin exec -i postgres-0 -- \
  psql -U houdini -d houdini -v ON_ERROR_STOP=1 < .local/igloos/provision.sql
```

Revisar las referencias activas antes de iniciar bots. Para deshacer la provisión,
con los bots todavía apagados y antes de usar los iglús:

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin exec -i postgres-0 -- \
  psql -U houdini -d houdini -v ON_ERROR_STOP=1 < .local/igloos/rollback.sql
```

El despliegue de la funcionalidad es un paso posterior: preparar un manifiesto
revisado que conserve los otros componentes y seguir `docs/operations.md`:

```bash
python3 scripts/deploy.py plan --manifest "$MANIFIESTO_REVISADO"
python3 scripts/deploy.py apply --expect-sha256 "$SHA256_PLAN_REVISADO"
python3 scripts/deploy.py verify --manifest "$MANIFIESTO_REVISADO"
```

Rollback de aplicación: usar el manifiesto anterior con esos mismos comandos de
plan/apply/verify, conservando ambos PVC y una sola réplica. No aplicar SQL de
rollback con bots activos. Nada de esta sección se ejecuta desde el banco.
