# Kubernetes deployment

Postgres is **not** part of this deployment — point it at whatever managed
Postgres you're using in Azure/AWS (or anywhere reachable) via the
`DATABASE_URL` secret below. For local testing, keep using
`docker compose up` from the repo root (that setup runs its own Postgres
container) — these manifests are for the actual cluster only.

The app has no file-based storage fallback: it refuses to start unless
`DATABASE_URL` and `LOOKUP_DB_ENABLED=true` are both set (see `configmap.yaml`
/ `secret.example.yaml` below).

## Prerequisites

Before touching these manifests, make sure you have:

- **A Kubernetes cluster already running** (AKS, EKS, or anywhere else) and
  `kubectl` on your machine, pointed at it. If `kubectl -n default get pods`
  returns something (even "No resources found") instead of a connection
  error, you're pointed at a cluster correctly.
- **A reachable PostgreSQL database.** This can be a managed service (Azure
  Database for PostgreSQL, Amazon RDS, Supabase, etc.) or your own — as
  long as the cluster's pods can reach it over the network. You'll need its
  connection string (`postgresql://user:password@host:5432/dbname`).
- **A container registry account** to push the built image to (these
  manifests assume Docker Hub, but any registry `kubectl`/your cluster can
  pull from works — adjust the `image:` field in `deployment.yaml`
  accordingly).
- **Docker**, to build the image on your machine before pushing it.

If any of the above isn't set up yet, get that in place first — none of the
`kubectl apply` steps below will succeed without them.

## What's here

| File | Purpose |
|---|---|
| `namespace.yaml` | The `os-health-check` namespace everything else lives in |
| `configmap.yaml` | Non-secret env vars (`LOOKUP_DB_ENABLED=true` is critical — see its comment) |
| `secret.example.yaml` | **Template only** — shows the shape, don't fill in real values and commit it |
| `pvc.yaml` | Persists `_config/` (Settings, vendor-source toggles) across restarts |
| `deployment.yaml` | The app itself — 1 replica, `Recreate` strategy (see its comment for why) |
| `service.yaml` | `LoadBalancer` — exposes it with a public IP, no Ingress controller needed |

## First-time setup

**1. Build and push the image to Docker Hub:**
```bash
docker build -t <your-dockerhub-username>/os-health-check:v1 .
docker push <your-dockerhub-username>/os-health-check:v1
```
Then edit `deployment.yaml`'s `image:` field to match (replace
`REPLACE_ME` with your username, and `latest` with `v1`, or whatever tag
you used — see the comment there for why `:latest` gets confusing fast).

**2. Point `kubectl` at your cluster** (AKS or EKS — however you normally
authenticate, e.g. `az aks get-credentials` / `aws eks update-kubeconfig`).

**3. Create the namespace:**
```bash
kubectl apply -f namespace.yaml
```

**4. Create the secret — do NOT apply `secret.example.yaml` directly.**
Kubernetes Secrets are base64-*encoded*, not encrypted, so anything applied
from a committed file is only as safe as your git repo. Create it directly
instead:
```bash
kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://user:pass@host:5432/dbname?sslmode=require' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''
```
`DATABASE_URL` is the only one that actually matters right now — the AI
keys can stay empty until/unless you turn AI matching on in Settings.

**5. Apply everything else:**
```bash
kubectl apply -f configmap.yaml -f pvc.yaml -f deployment.yaml -f service.yaml
```

**6. Watch it come up — this is also how the database gets its first data:**
```bash
kubectl -n os-health-check get pods -w
kubectl -n os-health-check logs -f deployment/os-health-check
```
You don't run any separate "import" or "load data" step. On a genuinely
empty Postgres database, the pod's startup sequence automatically loads in
the lookup data baked into the image (`_data/eol_lookup.csv`) the first
time it connects and finds zero rows — you'll see a log line like:
```
[lookup_db] No 'data' rows in Postgres schema 'lookup' yet -- importing N row(s) from _data/eol_lookup.csv ...
```
This is the exact same startup hook the Docker deployment uses — it never
depended on Docker specifically. It's safe to leave running forever: once
the database has any rows (from this import, or a real publish), every
later pod restart just logs `already has 'data' rows -- skipping import`
and moves on. If you ever want to force a full re-import that overwrites
whatever's currently in the database, run `python lookup_db.py --force`
from inside a pod (`kubectl -n os-health-check exec -it deployment/os-health-check -- python lookup_db.py --force`) —
you shouldn't need this for a normal first-time setup.

**7. Get the external IP:**
```bash
kubectl -n os-health-check get service os-health-check
```
The `EXTERNAL-IP` column (may take a minute or two to provision) is where
you reach the app.

## Updating to a new build

```bash
docker build -t <you>/os-health-check:v2 .
docker push <you>/os-health-check:v2
kubectl -n os-health-check set image deployment/os-health-check os-health-check=<you>/os-health-check:v2
```
(Or edit `deployment.yaml`'s `image:` and `kubectl apply -f deployment.yaml`
again — same effect, just easier to keep in git history if you commit the
manifest change alongside it.)

## If you ever need more than 1 replica

Don't just bump `replicas:` — read `pvc.yaml`'s comment first. The
`_config/` volume is `ReadWriteOnce` (one pod at a time) and the app's
Draft/Publish flow assumes a single instance sharing Postgres, not several
instances editing concurrently. Come back to this when that's actually
needed rather than guessing at a fix in advance.
