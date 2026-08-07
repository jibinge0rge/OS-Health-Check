# Kubernetes deployment

Postgres is **not** part of this deployment — point it at whatever managed
Postgres you're using in Azure/AWS (or anywhere reachable) via the
`DATABASE_URL` secret below. For everyday local development, keep using
`docker compose up` from the repo root instead (it runs its own Postgres
container and doesn't need any of this). These manifests are for testing or
running the app *as Kubernetes will actually run it* — either against a
real cluster, or locally against [minikube](#testing-locally-with-minikube-no-cloud-needed)
first, which is the safest way to prove they work before touching a real
cluster.

The app has no file-based storage fallback: it refuses to start unless
`DATABASE_URL` and `LOOKUP_DB_ENABLED=true` are both set (see `configmap.yaml`
/ `secret.example.yaml` below). It also refuses to start unless
`DEPLOYMENT_ID`/`KEYCLOAK_ISSUER_URL`/`KEYCLOAK_AUDIENCE` are set — see
`configmap.yaml`'s comments and
[../KEYCLOAK_SETUP.md](../KEYCLOAK_SETUP.md) for what these mean and how to
get real values from Keycloak.

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
| `configmap.yaml` | Non-secret env vars (`LOOKUP_DB_ENABLED=true` is critical — see its comment; `DEPLOYMENT_ID`/`KEYCLOAK_*` need real values — see [../KEYCLOAK_SETUP.md](../KEYCLOAK_SETUP.md)) |
| `secret.example.yaml` | **Template only** — shows the shape, don't fill in real values and commit it |
| `pvc.yaml` | Persists `_config/` (Settings, vendor-source toggles) across restarts |
| `deployment.yaml` | The app itself — 1 replica, `Recreate` strategy (see its comment for why) |
| `service.yaml` | `LoadBalancer` — public IP without Ingress (fine for minikube / quick smoke); on AKS with TLS use ClusterIP + `ingress.yaml` |
| `ingress.yaml` | Optional HTTPS Ingress — set **your** hostname before apply (not production-specific); needs ingress-nginx + cert-manager; `proxy-body-size: 50m` avoids 413 on draft/export |

## Local (minikube) vs. real cluster (AKS/EKS) — quick translation

Every step below is one of two flavors. If you're ever unsure which
instruction applies to you, use this table — the detailed walkthroughs
further down are just these same rows spelled out with full commands.

| Step | Local (minikube) | Real cluster (AKS/EKS) |
|---|---|---|
| The cluster itself | `minikube start` — creates/boots a cluster on your own machine | Already provisioned (by you or your cloud team) in Azure/AWS; you just point `kubectl` at it |
| Connecting `kubectl` | Automatic — `minikube start` does this for you | `az aks get-credentials ...` / `aws eks update-kubeconfig ...` |
| Getting the image to the cluster | `minikube image load os-health-check:local` — no registry involved | `docker push` to Docker Hub (or your registry), then the cluster pulls it |
| `deployment.yaml`'s `image:` | `os-health-check:local` | `docker.io/<you>/os-health-check:v1` (your real registry path) |
| `deployment.yaml`'s `imagePullPolicy:` | `IfNotPresent` (use the local copy, don't try to download it) | `Always` (always fetch the named tag from the registry) |
| Postgres | Reuse your `docker compose` Postgres, reached via the special hostname `host.minikube.internal` | A real managed Postgres (Azure Database for PostgreSQL, Amazon RDS, etc.) reachable over the network |
| Secret's `DATABASE_URL` | `postgresql://oshealth:oshealth@host.minikube.internal:5432/oshealth` | `postgresql://user:pass@<your-real-host>:5432/<dbname>?sslmode=require` |
| Namespace / Secret / ConfigMap / PVC / Deployment / Service | **Identical** `kubectl apply -f ...` commands either way | **Identical** `kubectl apply -f ...` commands either way |
| Loading data into the database the first time | **Automatic, identical either way** — the pod's own startup script does it; you never run a separate import command | **Automatic, identical either way** — the pod's own startup script does it; you never run a separate import command |
| Reaching the running app | `minikube service os-health-check -n os-health-check --url` (fakes a public IP) | `kubectl -n os-health-check get service os-health-check` — the `EXTERNAL-IP` column fills in a real public IP on its own |
| Shipping a new build | `minikube image load` + `kubectl rollout restart deployment/os-health-check` | `docker push` a new tag + `kubectl set image deployment/os-health-check os-health-check=<new-image>` |
| Removing this app | `kubectl delete namespace os-health-check` — same command either way | `kubectl delete namespace os-health-check` — same command either way |
| Removing the whole cluster | `minikube delete` | Delete the AKS/EKS cluster itself from the Azure/AWS console or CLI (out of scope for this repo) |

## Testing locally with minikube (no cloud needed)

You don't need Azure/AWS to try this out — [minikube](https://minikube.sigs.k8s.io/)
runs a real, one-node Kubernetes cluster inside Docker on your own machine.
`kubectl` talks to it exactly the same way it would talk to a cloud
cluster — same commands, same YAML files. This is the fastest way to prove
these manifests actually work before touching a real cluster.

**Two things are different from a real cluster, called out below:** there's
no container registry (we hand the image to minikube directly instead of
pushing to Docker Hub), and there's no managed Postgres (we reuse the
Postgres you already run via `docker compose`).

**1. Start minikube** (installs/boots a local cluster if one doesn't exist
yet; if you already have one and it's stopped, this also restarts it):
```bash
minikube start
```
Confirm `kubectl` can see it:
```bash
kubectl cluster-info
kubectl get nodes
```

**2. Build the app's image normally, then hand it to minikube directly.**
minikube runs its **own separate Docker engine**, isolated from your host's
Docker — even though you already built `os-health-check:local` via
`docker compose`, minikube can't see it until you load it in explicitly:
```bash
docker compose build          # or: docker build -t os-health-check:local .
minikube image load os-health-check:local
```

**3. Point `deployment.yaml` at that local image instead of a registry.**
Real-cluster deployments push to Docker Hub and reference that image path
(see the next section); for this local test, edit `deployment.yaml`'s
container spec to:
```yaml
image: os-health-check:local
imagePullPolicy: IfNotPresent
```
(`IfNotPresent` — not `Always` — so Kubernetes uses the local copy instead
of trying to pull from the internet.) **Remember to change this back**
before ever pointing these manifests at a real cluster.

**4. Make your `docker compose` Postgres reachable from inside minikube.**
Add a port mapping to `docker-compose.yml`'s `db` service so it's exposed
to your host machine, not just to other containers in that same compose
stack:
```yaml
  db:
    ports:
      - "5432:5432"
```
Then recreate it: `docker compose up -d db`. minikube provides a special
hostname, `host.minikube.internal`, that resolves to your host machine from
inside the cluster — confirm it can actually reach Postgres:
```bash
minikube ssh -- "nc -zv host.minikube.internal 5432"
# -> Connection to host.minikube.internal (...) 5432 port [tcp/postgresql] succeeded!
```

**5. Create the namespace:**
```bash
kubectl apply -f namespace.yaml
```

**6. Create the secret**, pointing `DATABASE_URL` at that same host Postgres
(swap in your real `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` if
you changed them from the defaults):
```bash
kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://oshealth:oshealth@host.minikube.internal:5432/oshealth' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''
```

**7. Apply everything else and watch it come up:**
```bash
kubectl apply -f configmap.yaml -f pvc.yaml -f deployment.yaml -f service.yaml
kubectl -n os-health-check get pods -w
kubectl -n os-health-check logs -f deployment/os-health-check
```
Look for the same `[lookup_db] ...` line described in step 6 below — it
means the pod is talking to Postgres correctly (either seeding it for the
first time, or confirming it already has data, e.g. because it's the same
database your `docker compose` stack has already been using).

**8. Reach the app.** minikube doesn't have a real cloud load balancer, so
`Service` type `LoadBalancer` never fills in an `EXTERNAL-IP` on its own
(you'll see it stuck at `<pending>` — that's expected here, not an error).
Use minikube's own command to get a working URL instead:
```bash
minikube service os-health-check -n os-health-check --url
```
This prints a URL like `http://127.0.0.1:PORT` and keeps running in your
terminal to maintain the tunnel (on Windows/Mac with the Docker driver) —
leave that terminal open while you're using the app, or run it again later
to get a fresh URL.

## First-time setup (real cluster — AKS, EKS, etc.)

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

**6. Watch it come up — this is also how the database gets its first data.**
There is **no separate "import" or "load data" step to run** — not by hand,
not as some one-time Kubernetes `Job`, nothing. It happens automatically,
inside the pod, before the app even starts serving requests:
```bash
kubectl -n os-health-check get pods -w
kubectl -n os-health-check logs -f deployment/os-health-check
```
On a genuinely empty database, the pod's startup sequence loads in the
lookup data baked into the image (`_data/eol_lookup.csv`) the moment it
connects and finds zero rows — you'll see a log line like:
```
[lookup_db] No 'data' rows in Postgres schema 'lookup' yet -- importing N row(s) from _data/eol_lookup.csv ...
```
This is the *exact same* startup hook the Docker deployment (and the
minikube walkthrough above) uses — it has nothing to do with Docker or
minikube specifically, so it behaves identically here. It's safe to leave
running forever: once the database has any rows (from this import, or a
real publish), every later pod restart — including every future
`kubectl rollout restart` when you ship a new build — just logs
`already has 'data' rows -- skipping import` and moves on without touching
existing data.

**The one thing that genuinely differs on a real cluster**: your managed
Postgres (Azure Database for PostgreSQL, Amazon RDS, etc.) has to actually
be reachable from the AKS/EKS network — this is the most common first-time
snag, and it has nothing to do with this app's code. If the pod's logs show
it hanging (no `[lookup_db]` line appears at all, or it stays on
`Waiting for application startup` far longer than a couple of seconds)
rather than an explicit error, that's almost always a networking problem,
not a bug:
- The database's firewall/security-group rules must allow inbound
  connections from the cluster (its VNet/VPC, or its public egress IP if
  the database is only reachable publicly).
- If the database is in a different VNet/VPC than the cluster, they need
  peering (or a private endpoint) configured between them.
- Double-check the `DATABASE_URL` you put in the Secret in step 4 — a typo
  in the host/port there produces exactly this kind of silent hang, not a
  clear error message.

If you ever want to force a full re-import that overwrites whatever's
currently in the database, run `python lookup_db.py --force` from inside
the pod:
```bash
kubectl -n os-health-check exec -it deployment/os-health-check -- python lookup_db.py --force
```
You shouldn't need this for a normal first-time setup.

**7. Get the external IP:**
```bash
kubectl -n os-health-check get service os-health-check
```
The `EXTERNAL-IP` column (may take a minute or two to provision) is where
you reach the app.

## Updating to a new build

**On a real cluster:**
```bash
docker build -t <you>/os-health-check:v2 .
docker push <you>/os-health-check:v2
kubectl -n os-health-check set image deployment/os-health-check os-health-check=<you>/os-health-check:v2
```
(Or edit `deployment.yaml`'s `image:` and `kubectl apply -f deployment.yaml`
again — same effect, just easier to keep in git history if you commit the
manifest change alongside it.)

**Locally with minikube** (no registry involved — same idea, just reload
the image and force the pod to pick it up):
```bash
docker compose build
minikube image load os-health-check:local
kubectl -n os-health-check rollout restart deployment/os-health-check
```
The `rollout restart` step matters here: since the image tag (`:local`)
never changes between builds, Kubernetes doesn't automatically notice a new
image is available the way it would with a genuinely new tag (`:v2`) — the
restart forces it to start a fresh pod using whatever's currently loaded.

## Tearing it down

**Delete everything this app owns in the cluster** — the namespace and
everything inside it (pods, the Deployment, the Service, the ConfigMap, the
Secret, and the PVC, which also deletes its underlying persisted `_config/`
data):
```bash
kubectl delete namespace os-health-check
```
This is the one command that removes all of it — there's no need to
`delete -f` each manifest individually. It does **not** touch Postgres
(managed or your own `docker compose` one) — that data is untouched either
way, since Postgres was never part of this namespace.

If you were testing with minikube and want to tear down the whole local
cluster too (not just this app), rather than just this app's namespace:
```bash
minikube stop     # keeps the cluster, just powers it off
minikube delete   # deletes it entirely -- next `minikube start` builds fresh
```

## If you ever need more than 1 replica

Don't just bump `replicas:` — read `pvc.yaml`'s comment first. The
`_config/` volume is `ReadWriteOnce` (one pod at a time) and the app's
Draft/Publish flow assumes a single instance sharing Postgres, not several
instances editing concurrently. Come back to this when that's actually
needed rather than guessing at a fix in advance.
