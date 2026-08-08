# Azure AKS Production Test Plan

> **Purpose.** An ordered, beginner-friendly path that was run end-to-end on an
> Azure free/trial subscription (~**$200** credit): **AKS** + **Azure Database
> for PostgreSQL Flexible Server** + remote Keycloak (`keycloak.example.com`,
> often behind a **Cloudflare Tunnel**) + app HTTPS at
> **`https://app.example.com`**. Entra ID federation into Keycloak is the
> last auth step.
>
> **What you are proving.** Browser login (HTTPS) → Keycloak JWT → AKS app
> validates JWKS → Postgres read/write → publisher role works.
>
> **AWS counterpart.** [`AWS_EKS_PRODUCTION_TEST_PLAN.md`](AWS_EKS_PRODUCTION_TEST_PLAN.md)
> — same app and Keycloak; EKS + RDS instead of AKS + Flexible Server.
>
> **Companions.** [`k8s/README.md`](../k8s/README.md) · [`KEYCLOAK_SETUP.md`](KEYCLOAK_SETUP.md) ·
> [`AUTH_MULTITENANCY_PLAN.md`](AUTH_MULTITENANCY_PLAN.md) · [`.env.example`](../.env.example)

---

## Table of contents

1. [Target architecture](#1-target-architecture)
2. [Cost fit for ~$200](#2-cost-fit-for-200)
3. [What you need before Day 1](#3-what-you-need-before-day-1)
4. [Order of work](#4-order-of-work)
5. [Phase 0 — Azure account, tools, naming](#5-phase-0--azure-account-tools-naming)
6. [Phase 1 — Keycloak realm, client, role, users](#6-phase-1--keycloak-realm-client-role-users)
7. [Phase 2 — Resource group](#7-phase-2--resource-group)
8. [Phase 3 — Azure Postgres Flexible Server](#8-phase-3--azure-postgres-flexible-server)
9. [Phase 4 — Create AKS](#9-phase-4--create-aks)
10. [Phase 5 — Connectivity checklist](#10-phase-5--connectivity-checklist)
11. [Phase 6 — Build image, deploy app](#11-phase-6--build-image-deploy-app)
12. [Phase 7 — App HTTPS (Ingress + cert-manager + DNS)](#12-phase-7--app-https-ingress--cert-manager--dns)
13. [Phase 8 — Keycloak redirects + first login](#13-phase-8--keycloak-redirects--first-login)
14. [Phase 9 — Entra ID federation (optional next)](#14-phase-9--entra-id-federation-optional-next)
15. [Phase 10 — End-to-end checklist](#15-phase-10--end-to-end-checklist)
16. [Phase 11 — Tear down](#16-phase-11--tear-down)
17. [Decision log](#17-decision-log)
18. [Command cheat sheet](#18-command-cheat-sheet)

---

## 1. Target architecture

```mermaid
flowchart LR
  User["Browser"]

  subgraph Azure["Azure subscription"]
    Ingress["ingress-nginx LB<br/>TLS termination"]
    AKS["AKS · os-health-check pod"]
    PG["Postgres Flexible B1ms"]
    Entra["Microsoft Entra ID"]
  end

  subgraph Remote["Remote + Cloudflare"]
    CFApp["DNS A app<br/>DNS only → Ingress IP"]
    CFKC["Cloudflare Tunnel<br/>keycloak.example.com"]
    KC["Keycloak"]
  end

  User --> CFApp --> Ingress --> AKS
  User -->|"Login / OIDC"| CFKC --> KC
  KC -->|"OIDC broker"| Entra
  AKS -->|"DATABASE_URL sslmode=require"| PG
  AKS -->|"OIDC discovery + JWKS<br/>User-Agent set in auth.py"| CFKC
  User -->|"Bearer JWT"| Ingress
```

| Piece | Where | Notes from this test |
|---|---|---|
| App | AKS, 1 replica, Service **ClusterIP** | Public entry is Ingress, not the app LoadBalancer |
| App URL | `https://app.example.com` | Required for PKCE (`crypto.subtle` needs HTTPS) |
| DB | Flexible Server `Standard_B1ms` | CLI says “Paid Tier” — expected; burns credit |
| Keycloak | `https://keycloak.example.com` | Keep Tunnel **proxied** — do not grey-cloud this hostname |
| Image | Docker Hub `linux/amd64` | Apple Silicon must build with `--platform linux/amd64` |

---

## 2. Cost fit for ~$200

| Resource | Use |
|---|---|
| AKS | 1 × **`Standard_B2s_v2`** (`Standard_B2s` is often blocked in `centralindia`) |
| Postgres | Burstable **B1ms**, 32 GB |
| Public LBs | **One** for ingress-nginx (app Service stays ClusterIP) |
| ACR / Redis | Skip |

Hard rules: one resource group; budget alerts; stop Postgres / delete the RG when idle.

---

## 3. What you need before Day 1

| Need | Notes |
|---|---|
| Azure free/trial + `az` CLI | `az login` |
| `kubectl`, Docker, **Helm**, Docker Hub login | Helm for ingress-nginx |
| Cloudflare zone for `example.com` | App A record + existing Keycloak Tunnel |
| Keycloak admin on `keycloak.example.com` | Realm/client already planned here |
| This repo | Build from **repo root** (where `Dockerfile` lives) |

```bash
brew install azure-cli kubernetes-cli helm
az login
az account show
```

---

## 4. Order of work

| Step | Done when |
|---|---|
| 0 Tools + names | Subscription selected; names written down |
| 1 Keycloak realm/client/role/users | Local `publisher` / `editor` exist |
| 2 Resource group | `rg-oshealth-prodtest` exists |
| 3 Postgres + DB + firewall | `oshealth` DB exists; AllowAll or laptop IP works |
| 4 AKS | `kubectl get nodes` → Ready |
| 5 Connectivity | Clear what must reach what |
| 6 Build amd64 + deploy | Pod Running; seed log present |
| 7 Ingress + TLS + DNS | `https://app.example.com` → 200 |
| 8 Keycloak redirects + login | Local publisher can use the app (data + avatar) |
| 9 Entra federation | Optional; after local login works |
| 10 Checklist + tear down | RG deleted when finished |

---

## 5. Phase 0 — Azure account, tools, naming

1. Confirm credit and create a **budget** (e.g. $180) with alerts.
2. Fix these names (examples used below):

| Name | Value |
|---|---|
| Resource group | `rg-oshealth-prodtest` |
| Region | `centralindia` |
| AKS | `aks-oshealth-test` |
| Postgres server | `psql-oshealth-test` (globally unique) |
| Database | `oshealth` |
| Admin user | `oshealthadmin` |
| `DEPLOYMENT_ID` | `azure-aks-prodtest` |
| Keycloak realm / client | `os-health-check` / `os-health-check-web` |
| Publisher role | `lookup-publisher` |
| Keycloak URL | `https://keycloak.example.com` |
| App hostname | `app.example.com` |
| Docker Hub | e.g. `jibingeorge/os-health-check:v1` |

```bash
az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
az configure --defaults group=rg-oshealth-prodtest location=centralindia
```

---

## 6. Phase 1 — Keycloak realm, client, role, users

Keycloak must be on HTTPS before AKS. Concepts: [`KEYCLOAK_SETUP.md`](KEYCLOAK_SETUP.md).

### 6.1 Smoke HTTPS

```bash
curl -fsS "https://keycloak.example.com/realms/master" | head
```

### 6.2 Realm + public client

Admin console → create realm **`os-health-check`**.

Create client **`os-health-check-web`**:

| Setting | Value |
|---|---|
| Client authentication | **Off** (public) |
| Standard flow | **On** |
| Valid redirect URIs | `http://localhost:8000/*` for now; replace in Phase 8 with `https://app.example.com/*` |
| Web origins | `http://localhost:8000` for now; later `https://app.example.com` |
| Advanced → PKCE | **S256** |

Root URL / Home URL can stay empty. This screen is only redirects/origins — **not** where publisher is assigned.

### 6.3 Realm role + users

1. **Realm roles** → create **`lookup-publisher`** (realm role, not client role, not Entra).
2. Users → create **`publisher`** and **`editor`** (set First/Last name).
3. Open **`publisher`** → **Role mapping** → **Assign role**.
4. Change the filter from **Filter by clients** to **Filter by realm roles**.
5. Assign **`lookup-publisher`**. Leave `editor` without that role.

### 6.4 Issuer (must match ConfigMap later)

```bash
curl -fsS "https://keycloak.example.com/realms/os-health-check/.well-known/openid-configuration" | head
```

Confirm `"issuer":"https://keycloak.example.com/realms/os-health-check"` with no trailing slash. That exact string is `KEYCLOAK_ISSUER_URL`.

---

## 7. Phase 2 — Resource group

```bash
az group create --name rg-oshealth-prodtest --location centralindia
```

Networking for this test: AKS default VNet + **public** Postgres + firewall rules (AllowAll for a short test is fine with a strong password).

---

## 8. Phase 3 — Azure Postgres Flexible Server

Put comments on their own `#` lines (Unicode dashes in comments break some shells if pasted into `export`).

```bash
export PG_ADMIN=oshealthadmin
export PG_PASSWORD='REPLACE_WITH_LONG_RANDOM_PASSWORD'
export PG_SERVER=psql-oshealth-test

az postgres flexible-server create \
  --resource-group rg-oshealth-prodtest \
  --name "$PG_SERVER" \
  --location centralindia \
  --admin-user "$PG_ADMIN" \
  --admin-password "$PG_PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 16 \
  --public-access 0.0.0.0 \
  --yes
```

CLI may say **Paid Tier** — continue.  
`--public-access 0.0.0.0` only allows Azure services, not “open to the world”.

Create the app database (`--name` is the **database** name):

```bash
az postgres flexible-server db create \
  --resource-group rg-oshealth-prodtest \
  --server-name "$PG_SERVER" \
  --name oshealth
```

Open firewall for laptop / AKS. For a changing home IP during a throwaway test:

```bash
az postgres flexible-server firewall-rule create \
  --resource-group rg-oshealth-prodtest \
  --server-name "$PG_SERVER" \
  --name AllowAll \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 255.255.255.255
```

On this command: **`--server-name`** = Postgres server, **`--name`** = rule name.

Connection string:

```text
postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require
```

Optional overnight saver:

```bash
az postgres flexible-server stop --resource-group rg-oshealth-prodtest --name "$PG_SERVER"
az postgres flexible-server start --resource-group rg-oshealth-prodtest --name "$PG_SERVER"
```

---

## 9. Phase 4 — Create AKS

Use **`Standard_B2s_v2`** in `centralindia` (classic `Standard_B2s` is often rejected).

```bash
az aks create \
  --resource-group rg-oshealth-prodtest \
  --name aks-oshealth-test \
  --node-count 1 \
  --node-vm-size Standard_B2s_v2 \
  --generate-ssh-keys \
  --network-plugin azure \
  --enable-managed-identity
```

Point kubectl at AKS (required if Minikube was used earlier):

```bash
az aks get-credentials \
  --resource-group rg-oshealth-prodtest \
  --name aks-oshealth-test \
  --overwrite-existing

kubectl config current-context
kubectl get nodes
```

One node `Ready` is enough. `EXTERNAL-IP: <none>` on the node is normal.

With Postgres **AllowAll** already set, you do not need to add AKS egress IPs for DB access. To list them later anyway:

```bash
NODE_RG=$(az aks show -g rg-oshealth-prodtest -n aks-oshealth-test --query nodeResourceGroup -o tsv)
az network public-ip list -g "$NODE_RG" --query "[].{name:name,ip:ipAddress}" -o table
```

(`networkProfile.outboundIPs` is usually empty — ignore it.)

---

## 10. Phase 5 — Connectivity checklist

| Path | Requirement |
|---|---|
| Pod → Postgres | Firewall open; `sslmode=require` |
| Pod → Keycloak HTTPS | Tunnel/public HTTPS reachable from Azure |
| Browser → Keycloak | Same issuer URL as ConfigMap |
| Browser → app | After Phase 7: `https://app.example.com` |

Leave `KEYCLOAK_INTERNAL_URL` unset unless the pod must use a different URL than the browser for discovery (issuer `iss` still must match `KEYCLOAK_ISSUER_URL`).

---

## 11. Phase 6 — Build image, deploy app

### 11.1 Build and push (amd64, repo root)

Apple Silicon defaults to arm64; AKS needs amd64. Build from the directory that contains `Dockerfile`:

```bash
cd /path/to/OS-Health-Check
export DOCKERHUB_USER=youruser   # e.g. jibingeorge

docker build --platform linux/amd64 -t "$DOCKERHUB_USER/os-health-check:v1" .
docker push "$DOCKERHUB_USER/os-health-check:v1"
```

Current `auth.py` sends a browser-like **User-Agent** on OIDC discovery/JWKS fetches. That is required when Keycloak sits behind Cloudflare Tunnel: `curl` from the pod may get HTTP 200 while Python’s default UA gets **403**, which breaks every `/api/*` call after login. Deploy an image that includes that fix.

Set Docker Hub repo to **Public** (or configure an imagePullSecret).

### 11.2 Manifests (`k8s/base`)

Edit before apply (same manifests for AKS and EKS):

1. `k8s/base/deployment.yaml` → `image:` (your Docker Hub user / tag)
2. `k8s/base/configmap.yaml` — set `DEPLOYMENT_ID` and issuer must match Keycloak exactly:

```yaml
DEPLOYMENT_ID: "azure-aks-prodtest"
KEYCLOAK_ISSUER_URL: "https://keycloak.example.com/realms/os-health-check"
KEYCLOAK_AUDIENCE: "os-health-check-web"
KEYCLOAK_PUBLISHER_ROLE: "lookup-publisher"
```

3. `k8s/base/ingress.yaml` — both host fields = your app DNS (used in Phase 7; safe to set now)

Service is already **ClusterIP** (Ingress is the public path).

### 11.3 Apply

```bash
cd /path/to/OS-Health-Check

kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY='' \
  --dry-run=client -o yaml | kubectl apply -f -

# If namespace does not exist yet, create it first or apply twice after base creates it:
kubectl apply -k k8s/base

kubectl -n os-health-check get pods -w
kubectl -n os-health-check logs -f deployment/os-health-check
```

If the Secret was created before the namespace existed:

```bash
kubectl apply -k k8s/base
kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''
kubectl -n os-health-check rollout restart deploy/os-health-check
```

Expect seed import or `already has 'data' rows -- skipping import`, and Uvicorn listening on `:8000` inside the pod.

Service maps **port 80 → container 8000**. Never open public `:8000` on the load balancer.

---

## 12. Phase 7 — App HTTPS (Ingress + cert-manager + DNS)

Plain `http://<public-IP>` cannot run PKCE login (`crypto.subtle` is unavailable outside a secure context). Use HTTPS on a real hostname.

### 12.1 ingress-nginx

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-health-probe-request-path"=/healthz

kubectl -n ingress-nginx get svc ingress-nginx-controller -w
```

Copy **EXTERNAL-IP** (example from this run: `20.207.98.241`).

### 12.2 cert-manager + Let’s Encrypt issuer

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.17.2/cert-manager.yaml
kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=120s

kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: you@example.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
EOF
```

### 12.3 Cloudflare DNS for the app only

Zone **`example.com`** → DNS → Add record:

| Type | Name | Content | Proxy |
|---|---|---|---|
| A | `oshealth` | `<INGRESS-EXTERNAL-IP>` | **DNS only** (grey cloud) |

HTTP-01 issuance needs grey cloud so Let’s Encrypt hits Ingress directly.  
**Do not** grey-cloud **`keycloak`** if that hostname is a Cloudflare Tunnel — leave Keycloak orange/proxied.

```bash
dig +short app.example.com
# must equal the ingress EXTERNAL-IP (not 104.x / 172.x Cloudflare anycast)
```

### 12.4 Ingress (included in `k8s/base`)

Service is already ClusterIP. Ensure `k8s/base/ingress.yaml` hosts match your DNS, then re-apply if needed:

```bash
cd /path/to/OS-Health-Check
kubectl apply -k k8s/base

kubectl -n os-health-check get certificate -w
kubectl -n os-health-check describe ingress os-health-check
```

Ingress ships with `ssl-redirect: "false"` so Let’s Encrypt HTTP-01 can complete. After `READY=True`:

```bash
kubectl -n os-health-check annotate ingress os-health-check \
  nginx.ingress.kubernetes.io/ssl-redirect=true --overwrite
```

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" https://app.example.com/
# expect 200  (curl -I / HEAD may return 405 — app only allows GET)
```

---

## 13. Phase 8 — Keycloak redirects + first login

### 13.1 Client URLs

Keycloak → client **`os-health-check-web`** → save:

| Field | Value |
|---|---|
| Valid redirect URIs | `https://app.example.com/*` |
| Web origins | `https://app.example.com` |

### 13.2 Login

Open **`https://app.example.com`**, sign in as local **`publisher`**.

Healthy result:

- Avatar shows initials (from `preferred_username` / email), not only `?`
- Lookup row count > 0 (seeded Data)
- DevTools → `/api/auth/me` and `/api/lookup?source=data` return **200**

### 13.3 If `/api/*` returns 401 with JWKS 403

From the pod, Cloudflare often allows `curl` but blocks Python’s default User-Agent:

```bash
# curl — usually 200
kubectl -n os-health-check exec deploy/os-health-check -- \
  curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://keycloak.example.com/realms/os-health-check/protocol/openid-connect/certs"

# python default UA — may be 403 without the auth.py fix
kubectl -n os-health-check exec deploy/os-health-check -- \
  python3 -c "import urllib.request; print(urllib.request.urlopen('https://keycloak.example.com/realms/os-health-check/protocol/openid-connect/certs', timeout=15).status)"
```

Fix that shipped in this repo: rebuild/push the image that includes the User-Agent headers in `auth.py`, then:

```bash
kubectl -n os-health-check rollout restart deployment/os-health-check
```

Do **not** grey-cloud the Keycloak Tunnel hostname to “fix” this.

---

## 14. Phase 9 — Entra ID federation (optional next)

Do this after local Keycloak login + APIs already work.

App never talks to Entra — Keycloak brokers and still issues its own JWT ([`KEYCLOAK_SETUP.md` §5](KEYCLOAK_SETUP.md)).

### 14.1 Entra app registration

Portal → **Microsoft Entra ID** → **App registrations** → New:

| Field | Value |
|---|---|
| Name | `OS Health Check Keycloak Broker` |
| Redirect URI (Web) | `https://keycloak.example.com/realms/os-health-check/broker/microsoft/endpoint` |

Create a client secret; copy Application (client) ID and Directory (tenant) ID.

### 14.2 Keycloak Identity Provider

Realm → Identity providers → OpenID Connect (alias **`microsoft`** must match the redirect path):

| Field | Value |
|---|---|
| Discovery | `https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration` |
| Client ID / secret | From Entra |
| Default scopes | `openid profile email` |

First Microsoft login creates a linked Keycloak user — assign **`lookup-publisher`** via **Filter by realm roles** if they should publish.

---

## 15. Phase 10 — End-to-end checklist

### Infrastructure

- [ ] `kubectl get nodes` → Ready  
- [ ] Pod Running; logs show DB seed/skip  
- [ ] `https://app.example.com` → 200  
- [ ] Certificate Ready  

### Auth (local)

- [ ] Login as `editor` → draft works; publish denied  
- [ ] Login as `publisher` → publish works  
- [ ] `/api/auth/me` and `/api/lookup` return 200  

### Entra (if configured)

- [ ] Microsoft button works  
- [ ] Federated user can use API; publisher role assigned in Keycloak  

### Data

- [ ] Published Data visible after refresh  
- [ ] Pod restart keeps Postgres data  

### Tear-down ready

- [ ] Budget checked; ready to delete RG when done  

---

## 16. Phase 11 — Tear down

```bash
az group delete --name rg-oshealth-prodtest --yes --no-wait
```

Does **not** delete: Keycloak host, Cloudflare DNS/Tunnel, Entra app registration, Docker Hub tags.

---

## 17. Decision log

| Decision | Choice | Why |
|---|---|---|
| Postgres | Flexible `B1ms` public + AllowAll for short test | Fast; strong password; delete RG after |
| AKS node | `Standard_B2s_v2` | Available in `centralindia` |
| App entry | Ingress + Let’s Encrypt + `app.example.com` | PKCE needs HTTPS |
| App Service | ClusterIP | One public LB (Ingress) |
| Image | Docker Hub `linux/amd64` | AKS nodes are amd64 |
| Keycloak edge | Keep Cloudflare Tunnel | Don’t grey-cloud tunnel hostname |
| JWKS from AKS | Browser-like User-Agent in `auth.py` | Cloudflare blocks default Python UA |
| Redis / ACR | Skip | Cost |

---

## 18. Command cheat sheet

```bash
# Context
az account show
az aks get-credentials -g rg-oshealth-prodtest -n aks-oshealth-test --overwrite-existing
kubectl config current-context

# App
kubectl -n os-health-check get pods,svc,ingress,certificate
kubectl -n os-health-check logs -f deployment/os-health-check
curl -fsS -o /dev/null -w "%{http_code}\n" https://app.example.com/

# Rebuild after code change (from repo root)
export DOCKERHUB_USER=jibingeorge
docker build --platform linux/amd64 -t "$DOCKERHUB_USER/os-health-check:v1" .
docker push "$DOCKERHUB_USER/os-health-check:v1"
kubectl -n os-health-check rollout restart deployment/os-health-check

# JWKS reachability from pod
kubectl -n os-health-check exec deploy/os-health-check -- \
  curl -fsS -o /dev/null -w "%{http_code}\n" \
  "https://keycloak.example.com/realms/os-health-check/protocol/openid-connect/certs"

# Postgres AllowAll (throwaway)
az postgres flexible-server firewall-rule create \
  -g rg-oshealth-prodtest -s psql-oshealth-test \
  --name AllowAll \
  --start-ip-address 0.0.0.0 --end-ip-address 255.255.255.255

# Teardown
az group delete -n rg-oshealth-prodtest --yes --no-wait
```

---

## What “success” looks like

1. Colleague opens **`https://app.example.com`**.  
2. Logs in via Keycloak (local user or Microsoft after Phase 9).  
3. Sees populated Lookup Data and a real username/avatar.  
4. Publisher can edit Draft and Publish; editor cannot publish.  
5. Data survives pod restart (Azure Postgres).

That validates AKS + managed Postgres + remote Keycloak (Tunnel) + app HTTPS + JWT validation from the cluster.
