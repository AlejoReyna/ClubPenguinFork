# Respaldos, rollback y recuperación de datos

Son dos problemas distintos y se resuelven con herramientas distintas:

| | Rollback de aplicación | Recuperación de datos |
|---|---|---|
| Qué corrige | Una release mala (imágenes/configuración) | Datos perdidos o corruptos en PostgreSQL |
| Mecanismo | Volver a desplegar las etiquetas de una release anterior | Restaurar un dump verificado |
| Automático | Sí, con el mismo pipeline, plan y aprobación | **Nunca**: procedimiento manual explícito |
| Riesgo para los datos | Ninguno (no escribe en la base) | Reemplaza datos: destructivo |

Revertir la aplicación **no** deshace cambios que el código nuevo ya haya escrito en la
base de datos.

## Cuándo se respalda

`scripts/backup.py` hace `pg_dump`, lo restaura en una base temporal `verify_*`, lee
las cuentas, borra solo esa base, y sube el dump y un manifiesto SHA-256 a
`clubpenguin-backups/postgres/`. Conserva una copia local privada
(`~/.clubpenguin-lab/backups/`).

**Un respaldo posterior a un cambio no protege contra ese cambio.** Por eso el respaldo
va **antes** de aplicar, decidido por el plan guardado (`scripts/plan_gate.py`):

| El plan toca… | Respaldo previo |
|---|---|
| StatefulSet, PVC, Secret o contraseña de PostgreSQL | **Obligatorio** |
| Deployments `login`, `glaciar` o `dash` (imagen, entorno, recursos) | **Obligatorio** (el código del juego escribe en la base) |
| `web`, `exporter`, `redis`, conector, `release-info`, monitoreo | No |
| Solo metadatos (anotaciones/etiquetas) de los anteriores | No |

`backupMode: auto` aplica esta tabla y `always` respalda siempre; el pipeline no ofrece
omitirlo, y `deploy.py apply` rechaza `--backup never` si el plan toca datos. Si el respaldo o su verificación fallan, **no se
aplica nada**. Además, respalda a mano antes de apagar el Pi o hacer mantenimiento.

Límites reales (no inventar garantías): no hay respaldo programado, así que el RPO es
«desde el último respaldo manual o previo a un despliegue»; el RTO no está medido; no hay
política de retención automática (limpiar `releases/` y `postgres/` periódicamente, sin
tocar nunca los estados Terraform); un Pi apagado no puede respaldarse.

## Rollback de aplicación

Cada despliegue verificado se registra en Blob como
`releases/<UTC>-<commit12>.json` (commit, etiquetas, digests, plataforma). El clúster
también lo refleja:

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml --context default -n clubpenguin \
  get configmap release-info -o jsonpath='{.data}'
```

1. **Identificar la release anterior**
   ```bash
   python3 scripts/release.py list          # con tu sesión az o dentro de AzureCLI@2
   python3 scripts/release.py fetch previous --out /tmp/previous.json   # lo que estuvo vivo antes
   ```
   «previous» es la penúltima release **desplegada**. Tras un rollback, la última es el
   rollback y la penúltima es la release mala: en un segundo rollback pasa el nombre
   explícito.
2. **Planificar sin aplicar**: ejecuta el pipeline con `release = true`,
   `rollbackTo = previous` (o el nombre) y `deploymentAgent`. No construye nada: toma las
   etiquetas y digests del manifiesto. El gate **exige que el plan cambie solo imágenes**
   (y `release-info`); cualquier otra diferencia lo bloquea.
3. **Aplicar**: misma ejecución con `deploy = true`, aprobación del environment, y
   respaldo previo si el plan toca runtime o base de datos.
4. **Comprobar la salud**: `verify` es el mismo de un despliegue normal, y compara
   etiquetas **y digests** en ejecución con los del manifiesto.
5. **Registro**: queda anotado como `kind: rollback`.

Límites: solo revierte imágenes y requiere que esas imágenes sigan en el ACR (no las
borres). Un cambio de HCL (nuevos recursos, variables) se revierte con `git revert` y una
release normal. Un cambio de esquema/datos no se revierte con esto.

### Emergencia con el pipeline no disponible

```bash
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml --context default -n clubpenguin \
  rollout undo deployment/<nombre>      # o statefulset/postgres
```

Esto crea **deriva** respecto a Terraform: el siguiente plan volverá a proponer la
imagen nueva. Úsalo solo para detener el daño y concilia después con el rollback del
pipeline. No uses `terraform force-unlock` salvo que verifiques que no hay ejecución
activa; el bloqueo es una concesión (lease) del Blob.

### Si un `apply` falla a medias

El estado refleja lo ya aplicado. No reintentes a ciegas: ejecuta un `Release` nuevo
(nuevo plan sobre el estado actual) y revisa. Si el Pi no responde, ver
[agente-mac.md](agente-mac.md#túnel-ssh-al-api-de-kubernetes).

## Recuperación de datos (manual)

Solo si hay pérdida o corrupción real. Ningún pipeline lo hace.

1. **Congela la escritura**: `kubectl … scale deployment/login deployment/glaciar
   deployment/dash --replicas=0` (deriva intencionada: el próximo `apply` restaura las
   réplicas).
2. **Respalda lo que hay** aunque esté dañado: `python3 scripts/backup.py --reason
   before-restore`.
3. **Valida el dump elegido sin tocar producción**:
   ```bash
   python3 scripts/restore_verify.py --blob postgres/<UTC>.dump
   ```
   Comprueba tamaño y SHA-256 contra su manifiesto, lo restaura en una base temporal
   `restore_*`, lee las cuentas y la borra. Falla si el dump no coincide.
4. **Reemplazo** (decisión humana, con el juego detenido): descarga el dump con
   `az storage blob download`, cópialo al pod (`kubectl cp`) y restaura sobre la base viva
   con `pg_restore --clean --if-exists --no-owner --no-acl -d houdini` dentro de
   `postgres-0`, o crea una base nueva y repunta; léelo en `psql` antes de continuar.
5. **Reanuda**: `scale … --replicas=1` (o un `apply`), ejecuta el recorrido de dos cuentas
   (`game-smoke.py`, vía `deploy.py verify`) y registra qué se restauró y de cuándo.

Los dumps contienen datos de cuentas: manténlos en el contenedor privado y en el directorio
`0700` local; nunca como artefacto del pipeline.

## Cuentas sintéticas del smoke test

Cada `verify` crea **dos cuentas** (`lab` + 8 letras) en la base real. No se borran
automáticamente: borrar filas de producción desde un pipeline era un riesgo mayor que dos
filas por release. Para limpiarlas a mano, con el juego sin uso:
`DELETE FROM penguin WHERE username ~ '^lab[a-z]{8}$'` (las claves foráneas a `penguin` son
`ON DELETE CASCADE`/`SET NULL`), tras revisar con `SELECT` y respaldar.
