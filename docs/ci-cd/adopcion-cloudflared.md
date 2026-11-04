# Adoptar el conector Cloudflare en el estado de Terraform

El Deployment `cloudflared` y la NetworkPolicy `web-from-cloudflared` se crearon **a
mano**. Escribirlos en `infra/cluster/cloudflared.tf` no los importa: con
`cloudflared_enabled=true` y fuera del estado, `terraform plan` propone **crearlos** (y
el `apply` fallaría porque ya existen). Tampoco se debe borrar el conector para que el
pipeline pase.

El pipeline lo detecta (plan real del 3 de noviembre de 2026):

```
  + kubernetes_deployment_v1.cloudflared[0]  [create]
  + kubernetes_network_policy_v1.web_from_cloudflared[0]  [create]
BLOCKED: …exists in the cluster but not in the Terraform state; import it (terraform import …)
```

## Habilitación explícita

`scripts/deploy.py` pasa siempre `-var=cloudflared_enabled=true` en la línea de comandos
(no depende de un `terraform.tfvars` local, que no viaja a Git). Así, un checkout limpio
no retira el túnel una vez adoptado. `scripts/lint_pipeline.py` falla si esa línea
desaparece, y el gate bloquea cualquier plan que borre el conector o su política.

## Procedimiento

En el Mac con acceso al Pi, con tu sesión de Azure (`az login`) y `deploy.env` cargado:

```bash
# 1. Simulacro: muestra qué importaría, sin tocar el estado
python3 scripts/deploy.py adopt

# 2. Importar (solo estado; no modifica ni borra objetos del clúster)
python3 scripts/deploy.py adopt --execute
```

`adopt --execute`: abre/reutiliza el túnel, comprueba con `kubectl` que ambos objetos
existen y que no están ya en el estado, guarda una **copia privada del estado** (contiene
secretos; nunca a Git ni a artefactos) y ejecuta `terraform import` para cada uno. Es
idempotente: lo que ya está en el estado se omite.

Equivalente manual, con las mismas variables que usa el pipeline:

```bash
terraform -chdir=infra/cluster import -var=game_enabled=true -var=web_enabled=true \
  -var=cloudflared_enabled=true -var-file=<variables> \
  'kubernetes_deployment_v1.cloudflared[0]' clubpenguin/cloudflared
terraform -chdir=infra/cluster import … \
  'kubernetes_network_policy_v1.web_from_cloudflared[0]' clubpenguin/web-from-cloudflared
```

## Después de importar

1. Ejecuta un `Release` (o `terraform plan`) y **revisa** el plan. Diferencias esperables:
   el conector vivo usa `cloudflare/cloudflared:latest` y el HCL fija
   `cloudflare/cloudflared:2026.10.7`; aplicar actualiza esa imagen y reinicia solo el
   conector (el túnel se corta unos segundos).
2. El Secret `cloudflared-token` **no** está en Terraform ni en el estado y no se toca.
3. El plan ya no debe mostrar `create` del conector ni de su política.
4. Borra la copia privada del estado cuando termines de revisar.

Si algo sale mal tras importar, la copia privada permite restaurar el estado con
`terraform state push`, con el backend bloqueado y tras comprobar el número de serie.
