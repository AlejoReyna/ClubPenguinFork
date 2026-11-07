<div align="center">

# Club Penguin

<p><em>Image preview pending.</em></p>

<!-- Add the home screenshot here when it is ready. -->

_A self-hosted Club Penguin AS2 world on Raspberry Pi, with Ruffle in the browser and K3s behind the scenes._

</div>

Run against an already configured Raspberry Pi deployment from the operator Mac:

```bash
git clone https://github.com/AlejoReyna/ClubPenguinFork.git
cd ClubPenguinFork
python3 scripts/tunnel.py start
kubectl --kubeconfig ~/.kube/clubpenguin-pi.yaml -n clubpenguin \
  port-forward --address 127.0.0.1 svc/web 13080:8080
```

Open [localhost:13080](http://localhost:13080). First-time setup requires the Pi, its dedicated kubeconfig, and the [operator configuration](./docs/ci-cd/agente-mac.md).

<sub>[Setup and technical reference](./DEVELOPMENT.md) · [Puffle integration](./docs/ci-cd/puffle-produccion.md)</sub>
