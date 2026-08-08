# Kubernetes deployment

Postgres is **not** part of this deployment — point it at managed Postgres
(Azure Flexible Server, RDS, etc.) via a `DATABASE_URL` Secret. For everyday
local development use `docker compose` from the repo root instead.

Manifests use [Kustomize](https://kubectl.docs.kubernetes.io/references/kustomize/)
(built into `kubectl`):

```text
k8s/
  base/                 # AKS / EKS: Namespace, ConfigMap, PVC, Deployment,
                        #         Service (ClusterIP), Ingress
  overlays/
    minikube/           # local image; no Ingress; local Keycloak issuer
  secret.example.yaml   # template only — never apply with real secrets
  README.md
```

```bash
kubectl apply -k k8s/base                 # AKS / EKS (edit base first)
kubectl apply -k k8s/overlays/minikube    # local
```

Preview: `kubectl kustomize k8s/base`

## Prerequisites

- A cluster (`kubectl` working: AKS, EKS, or minikube)
- Reachable PostgreSQL + connection string
- Container registry (or `minikube image load` for local)
- Docker to build the image

The app refuses to start without `DATABASE_URL` + `LOOKUP_DB_ENABLED=true`
and `DEPLOYMENT_ID` / `KEYCLOAK_ISSUER_URL` / `KEYCLOAK_AUDIENCE`. See
[../docs/KEYCLOAK_SETUP.md](../docs/KEYCLOAK_SETUP.md).

## Before `kubectl apply -k k8s/base` (AKS / EKS)

Edit these files in **`k8s/base/`**:

| File | What to set |
|------|-------------|
| `deployment.yaml` → `image:` | Your Docker Hub user + tag |
| `configmap.yaml` | `DEPLOYMENT_ID`, **exact** `KEYCLOAK_ISSUER_URL`, audience |
| `ingress.yaml` | App hostname in **both** host fields |

Create the Secret (not in git):

```bash
kubectl create namespace os-health-check --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://user:pass@host:5432/dbname?sslmode=require' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''
```

(Namespace is also created by the base; creating the secret after the first
`apply -k` is fine if the namespace already exists.)

Then:

```bash
kubectl apply -k k8s/base
kubectl -n os-health-check get pods,pvc,ingress,certificate
```

Service is **ClusterIP** — traffic enters via Ingress (after ingress-nginx +
cert-manager + DNS; see the Azure/AWS production test plans). After the
Certificate is Ready, turn HTTPS redirect on:

```bash
kubectl -n os-health-check annotate ingress os-health-check \
  nginx.ingress.kubernetes.io/ssl-redirect=true --overwrite
```

## Local (minikube) vs cloud — quick translation

| Step | Minikube | AKS / EKS |
|------|----------|-----------|
| Apply | `k8s/overlays/minikube` | `k8s/base` |
| Image | `minikube image load os-health-check:local` | Edit `base/deployment.yaml` + `docker push` |
| Postgres | `host.minikube.internal` in Secret | Managed DB + `sslmode=require` |
| Reach app | `minikube service os-health-check -n os-health-check --url` | `https://YOUR_APP_HOSTNAME` via Ingress |
| Ingress | Removed by overlay | Edit hosts in `base/ingress.yaml` |

## Testing locally with minikube

```bash
minikube start
docker compose build          # or: docker build -t os-health-check:local .
minikube image load os-health-check:local
```

Expose compose Postgres on the host (`ports: ["5432:5432"]` on `db`), then:

```bash
kubectl apply -k k8s/overlays/minikube

kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://oshealth:oshealth@host.minikube.internal:5432/oshealth' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''

# If the Deployment started before the Secret existed:
kubectl -n os-health-check rollout restart deploy/os-health-check

kubectl -n os-health-check get pods -w
minikube service os-health-check -n os-health-check --url
```

Browser Keycloak for this overlay defaults to
`http://localhost:8081/realms/os-health-check-dev` (bundled compose Keycloak).

## First-time data load

No separate import Job. On an empty DB the pod logs:

```text
[lookup_db] No 'data' rows in Postgres schema 'lookup' yet -- importing …
```

Later restarts skip import. Force re-import only if you intend to overwrite:

```bash
kubectl -n os-health-check exec -it deployment/os-health-check -- \
  python lookup_db.py --force
```

## Updating to a new build

**Cloud:** bump the image tag in `base/deployment.yaml`, push the image, then
`kubectl apply -k k8s/base`.

**Minikube:**

```bash
docker compose build
minikube image load os-health-check:local
kubectl -n os-health-check rollout restart deployment/os-health-check
```

## Tear down

```bash
kubectl delete namespace os-health-check
```

Does not delete Postgres. For minikube: `minikube delete` removes the cluster.

## Scaling

Do not bump `replicas:` without reading the PVC / Draft-Publish constraints
(`base/pvc.yaml`, Auth plan). Volume is ReadWriteOnce.

## Production plans

- [Azure AKS](../docs/AZURE_AKS_PRODUCTION_TEST_PLAN.md)
- [AWS EKS](../docs/AWS_EKS_PRODUCTION_TEST_PLAN.md)
