# Raspberry Pi: probar pingüinos vivos (fases 2–4) y poner la clave Moonshot

Guía operativa para el Pi (`alexis`, kubeconfig dedicado `~/.kube/clubpenguin-pi.yaml`,
túnel `scripts/tunnel.py`). **No sustituye** un plan revisado ni autoriza gasto por sí sola.

## 1. Validar en Docker (Mac o CI)

```bash
cd ~/theCode/clubpenguin-lab
python3 -I scripts/build-images.py --components bots --tag bots-vivos-local --platform linux/arm64
bash scripts/run-bot-tests.sh clubpenguin/runtime:rt-origin-001
python3 -I -u scripts/test-containers.py \
  --tag rt-origin-001 --component-tag database=lab-002 \
  --bots-tag bots-vivos-local --live-cast --recall
```

Opcional: añadir `--voices` y/o `--life` en la misma línea. `--recall` exige `--live-cast`.

## 2. Publicar imagen ARM al ACR

Sustituir `<CP_ACR_NAME>` por el registro privado de `deploy.env`.

```bash
python3 scripts/publish-images.py --components bots --tag bots-vivos-local --registry "$CP_ACR_NAME"
```

Registrar el digest en el manifiesto de despliegue (`.local/bots/manifest-*.json`).

## 3. Variables de Terraform (tfvars privado)

Partir de `infra/cluster/terraform.tfvars.example`. Para **probar sin gasto** primero:

```hcl
bots_enabled           = true
bots_image_tag         = "bots-vivos-local"   # tag publicado
bots_provider_mode     = "simulated"
bots_replicas          = 1
bots_cast_enabled      = true
bots_dialogue_enabled  = true
bots_life_enabled      = false   # true solo tras validar viajes en Docker
bots_recall_enabled    = true
world_capacity         = "60"
# bots_daily_spend_cap_usd   = 1.00
# bots_monthly_spend_cap_usd = 30.00
# bots_max_concurrent        = 4
```

Los flags `BOTS_*_ENABLED` del Deployment activan director, vida y recall **sin**
editar el JSON del Secret `bot-credentials`. El cast va en el ConfigMap `bots-cast`.

Cuando quieras **Kimi real**, cambia solo `bots_provider_mode = "api"` en un plan
**nuevo y revisado** (y confirma topes USD). El Secret de la clave debe existir antes.

## 4. Plan y apply en el Pi

Con el túnel abierto:

```bash
python3 scripts/tunnel.py verify
python3 scripts/deploy.py plan --manifest .local/bots/manifest-bots-vivos.json
# Revisar diff: imagen bots, env cast/dialogue/recall, sin tocar PVC bots-budget.
python3 scripts/deploy.py apply --expect-sha256 <SHA256_DEL_PLAN>
```

Ejemplo de manifiesto local (no versionar):

```json
{
  "schema": 1,
  "commit": "<SHA>",
  "created_utc": "<ISO>",
  "run": {"build_id": "manual-bots-vivos"},
  "components": {
    "runtime": {"tag": "rt-origin-001"},
    "database": {"tag": "lab-002"},
    "web": {"tag": "lab-006"},
    "bots": {"tag": "bots-vivos-local"}
  }
}
```

## 5. Dónde poner la API key (sin activar gasto)

La clave **no** va en Terraform, imágenes ni chat. Va en el Secret externo `moonshot-api`,
campo `MOONSHOT_API_KEY`.

```bash
python3 scripts/set-moonshot-key.py --expected-node alexis --execute
```

- Pide la clave con **entrada oculta** y hace `kubectl apply` al Secret.
- **No** cambia `bots_provider_mode`; con `simulated` no hay llamadas facturables.
- Repetir solo si rotas la clave; nunca pegues la clave en el historial de terminal.

Orden recomendado: **guardar la clave → probar en simulated → plan aparte a `api`**.

## 6. Cuentas y cast en PostgreSQL

Los tres bots de trabajo actuales (Juanito, DJ Cobra, Viterbos) siguen en
`bot-credentials` con `--work-roles`. El reparto de **26** cuerpos exige
provisionar cuentas y nicknames del cast (ver `scripts/create-bot-accounts.py --cast`
y `docs/pinguinos-vivos.md` §14). Hasta entonces puedes probar dialogue/recall
con las tres sesiones si el Secret incluye sus usernames y el cast está montado.

## 7. Comprobaciones en el juego (simulated)

1. `kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin get pods -l app=bots`
2. `kubectl ... port-forward svc/bots-metrics 9102:9102` → `/readyz` y `/metrics`
3. En sala con bot: mencionar por nombre, **tres** mensajes seguidos; al día
   siguiente (o tras reiniciar el pod) la nota de relación debería influir en el tono.
4. «Lola, borra mi memoria» (o el personaje presente) debe confirmar borrado.

En modo `api`, revisa `bots_spend_cap_ratio` y autorización de §7 en
[pinguinos-vivos.md](pinguinos-vivos.md).

## 8. Checklist rápido

```bash
bash scripts/pi-bots-vivos-ready.sh --expected-node alexis
```

Solo comprueba túnel, nodo Ready y presencia del kubeconfig; **no** despliega ni pide la clave.
