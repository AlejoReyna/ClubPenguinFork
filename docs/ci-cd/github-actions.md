# GitHub Actions: CI y despliegue de imágenes

El workflow `.github/workflows/ci.yml` se ejecuta en PR y pushes a `main`.
Ambos validan y prueban en Ubuntu hospedado, sin credenciales del laboratorio.
La integración usa Houdini desechable, red Docker interna y proveedor simulado.
Un push a `main` que pase CI puede actualizar imágenes automáticamente; una
ejecución manual solo hace CI. El despliegue permanece deshabilitado mientras la
variable de repositorio `CP_AUTO_DEPLOY_ENABLED` no sea exactamente `true`.

## Qué hace el despliegue

Un único job en un Mac ARM64 propio, con las etiquetas `self-hosted`, `macOS`,
`ARM64` y `clubpenguin-lab`, usa el environment `clubpenguin-lab`. Se serializa
con otros despliegues de este workflow y rechaza un commit que dejó de ser la
punta de `main`. Un push posterior puede sustituir otro pendiente; no cancela
un despliegue ya iniciado. Esto no serializa despliegues manuales o de otro
pipeline: no ejecutarlos simultáneamente.

El job comprueba el destino del túnel, nodo y proveedor efectivo de bots;
resuelve etiquetas por componente, construye y prueba en ARM64 y publica imágenes
ausentes. El plan de `infra/cluster` solo admite cambios de imagen y del mapa
`release-info`; si hay otros cambios falla. Aplica el plan guardado y verificado
por SHA-256, con backup comprobado primero si afecta a datos. Verifica imágenes,
rollouts y conectividad sin crear cuentas de prueba en el Pi. Registra la release
verificada y borra su plan y túnel propios al terminar. Un fallo de verificación
no revierte automáticamente lo ya aplicado.

## Preparación externa, por el operador

1. Instalar el runner GitHub dedicado al repositorio en el Mac de confianza,
   con las etiquetas anteriores, Docker, Azure CLI, Terraform, Helm, Ansible,
   Python, `kubectl`, herramientas de build y acceso SSH al Pi. Nunca ejecutar
   PRs en este runner. Debe permanecer encendido. La conexión SSH y el kubeconfig
   dedicado `~/.kube/clubpenguin-pi.yaml` deben apuntar al Pi previsto; el script
   comprueba API y nodo antes de planificar.
2. Preparar `~/.config/clubpenguin-lab/deploy.env` con permisos `0600`, usando
   [deploy.env.example](deploy.env.example) como plantilla anonimizada y los
   valores reales privados. Fijar `CP_NODE_NAME` y `CP_SSH_CONFIG` si aplica.
   No poner contraseñas, Secrets ni datos reales en el repositorio.
3. Crear el environment de GitHub `clubpenguin-lab`, restringido a `main`, y
   configurar en Azure federación OIDC para el subject de ese environment. La
   identidad necesita permisos mínimos para ACR, estado y respaldos; no administra
   `infra/cloud`. Configurar las variables de repositorio `AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `CP_NODE_NAME` y
   `CP_BOTS_PROVIDER_MODE`. La última debe coincidir con el Deployment real
   (`api` o `simulated`); si no coincide, el job falla antes de publicar.
4. La migración inicial de identidades, Secrets, habilitación de recursos y otros
   cambios de infraestructura sigue siendo manual y revisada: el límite de
   imágenes del pipeline los bloquea. El paso de 0 a 1 réplica tampoco cabe en
   `--expect images-only`. Las zonas y la provisión desechable de
   [bots.md](../bots.md) ya están hechas; la aceptación con un jugador en el Pi
   sigue pendiente y es anterior a encender el despliegue automático.
5. Versionar los archivos, integrar la rama en `main` y confirmar que la CI pasa
   con el interruptor apagado. Verificar runner, permisos y plan con una primera
   ejecución controlada antes de habilitar el despliegue automático. Solo entonces:

   ```bash
   gh variable set CP_AUTO_DEPLOY_ENABLED --body true
   ```

   Ese comando modifica la configuración remota. Para desactivar futuras
   ejecuciones automáticas: `gh variable set CP_AUTO_DEPLOY_ENABLED --body false`.
   No detiene un job que ya esté en curso. Proteger `main` con PR y checks; si el
   environment exige revisores, esos revisores deben aprobar cada despliegue.

Azure DevOps tiene otro pipeline histórico y otra configuración: no activar ambos
despliegues automáticos sobre el mismo clúster. La presencia de este archivo no
acredita que GitHub, Azure, el runner o un despliegue real estén configurados.
