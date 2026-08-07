# Azure AKS Production Test Plan

> **Purpose.** A beginner-friendly, ordered plan to run a **prod-like** end-to-end
> test of OS Health Check on **Azure AKS**, with **PostgreSQL on Azure Flexible
> Server**, **Keycloak on your remote host** (`keycloack.frostgate.in`), and
> **Microsoft Entra ID (Azure AD) federation into Keycloak** — all within an
> Azure free / trial account (~**$200** credit).
>
> **What you are proving.** Browser login via Azure AD → Keycloak brokers the
> identity → AKS app validates Keycloak JWTs → app reads/writes Azure Postgres
> → publish role works. Not “forever production”, but close enough to find
> networking, TLS, issuer-URL, and firewall mistakes before a real rollout.
>
> **Companions in this repo.**
> - [`k8s/README.md`](k8s/README.md) — how to apply the manifests once the cluster exists
> - [`KEYCLOAK_SETUP.md`](KEYCLOAK_SETUP.md) — realm/client/roles + Azure federation overview
> - [`AUTH_MULTITENANCY_PLAN.md`](AUTH_MULTITENANCY_PLAN.md) — why auth is shaped this way
> - [`.env.example`](.env.example) — exact env var names the app needs

---

## Table of contents

1. [Target architecture](#1-target-architecture)
2. [Cost fit for ~$200 (critical)](#2-cost-fit-for-200-critical)
3. [What you need before Day 1](#3-what-you-need-before-day-1)
4. [Recommended order of work (timeline)](#4-recommended-order-of-work-timeline)
5. [Phase 0 — Azure account, tools, naming](#5-phase-0--azure-account-tools-naming)
6. [Phase 1 — Harden Keycloak on the remote server](#6-phase-1--harden-keycloak-on-the-remote-server)
7. [Phase 2 — Azure resource group + networking basics](#7-phase-2--azure-resource-group--networking-basics)
8. [Phase 3 — Azure Database for PostgreSQL Flexible Server](#8-phase-3--azure-database-for-postgresql-flexible-server)
9. [Phase 4 — Create AKS (small, cost-aware)](#9-phase-4--create-aks-small-cost-aware)
10. [Phase 5 — Let AKS reach Postgres and Keycloak](#10-phase-5--let-aks-reach-postgres-and-keycloak)
11. [Phase 6 — Build, push, and deploy the app](#11-phase-6--build-push-and-deploy-the-app)
12. [Phase 7 — Azure AD (Entra ID) federation in Keycloak](#12-phase-7--azure-ad-entra-id-federation-in-keycloak)
13. [Phase 8 — End-to-end test checklist](#13-phase-8--end-to-end-test-checklist)
14. [Phase 9 — Tear down (protect the $200)](#14-phase-9--tear-down-protect-the-200)
15. [Troubleshooting map](#15-troubleshooting-map)
16. [Decision log (defaults this plan locks in)](#16-decision-log-defaults-this-plan-locks-in)

---

## 1. Target architecture

```mermaid
flowchart LR
  User["Browser"]

  subgraph Azure["Azure subscription (~$200 credit)"]
    AKS["AKS cluster<br/>1 small node + OS Health Check pod"]
    PG["Azure Database for PostgreSQL<br/>Flexible Server (Burstable B1ms)"]
    LB["LoadBalancer public IP<br/>→ app :8000"]
    Entra["Microsoft Entra ID<br/>App registration"]
  end

  subgraph Remote["Your remote server"]
    KC["Keycloak<br/>https://keycloack.frostgate.in"]
  end

  User --> LB
  LB --> AKS
  AKS -->|"DATABASE_URL<br/>sslmode=require"| PG
  User -->|"Login redirect"| KC
  KC -->|"OIDC broker"| Entra
  AKS -->|"JWKS / OIDC discovery<br/>KEYCLOAK_ISSUER_URL"| KC
  User -->|"Keycloak JWT Bearer"| AKS
```

| Piece | Where it lives | Why |
|---|---|---|
| **OS Health Check app** | Azure AKS (1 replica) | Matches real k8s deploy path in [`k8s/`](k8s/) |
| **App database** | Azure Database for PostgreSQL **Flexible Server** | Managed, TLS, backups; what `k8s/README.md` already assumes |
| **Keycloak** | Remote server `keycloack.frostgate.in` | You already have it; app only needs a stable HTTPS issuer URL |
| **User identities** | Microsoft Entra ID → federated **into** Keycloak | App never talks to Azure AD directly; only Keycloak-issued JWTs |
| **Redis / Azure Cache** | **Not used** | This app has no Redis dependency today — skip it to save credit |

**Important hostname note.** The host you mentioned is spelled `keycloack.frostgate.in` (extra `a`). DNS and TLS certificates must use the **exact** spelling that resolves. In config below, substitute your real public URL whenever you see `KEYCLOAK_PUBLIC_URL`.

---

## 2. Cost fit for ~$200 (critical)

Azure “free” accounts usually include **time-limited credit** (often ~$200 for ~30 days) plus some always-free SKUs. **AKS nodes and Flexible Server are not free forever** — they burn credit while they exist, even if you are not testing.

### Cheap size that still works for a 7–14 day test

| Resource | Suggested SKU for the test | Rough vibe |
|---|---|---|
| AKS control plane | Free/standard (varies by region/offer) | Usually low |
| AKS node pool | **1 × `Standard_B2s_v2`** (2 vCPU / 4 GiB class) | Biggest cost driver; use `_v2` in `centralindia` |
| PostgreSQL Flexible | **Burstable `B1ms`**, 32 GB storage, **stop when idle if available** | Second biggest |
| Public LoadBalancer IP | 1 standard IP | Small |
| Azure Container Registry | Optional; Docker Hub is fine for a short test | Skip ACR to save money |
| Redis | **Do not create** | Save money |

**Hard rules while credit lasts**

1. Create everything in **one resource group** so delete-all is one click/command.
2. Prefer **East US / West Europe / Central India** (pick one close to you and to the Keycloak server’s latency).
3. **Stop or delete** when you are done for the day if the test spans many days. Idle AKS + Postgres can empty $200 quickly.
4. Set a **budget alert** at $50 / $100 / $150 (Phase 0).
5. Do **not** create a second AKS cluster, Azure Cache for Redis, App Gateway, or large Postgres SKUs “just in case”.

### What this plan deliberately does *not* include

- Multi-AZ / HA Postgres  
- Ingress controller + cert-manager + custom DNS for the app (LoadBalancer IP is enough for a test)  
- ACR, Front Door, WAF, Private Link (nice later; costly/complex now)  
- Keycloak inside AKS (you already have a remote Keycloak)

---

## 3. What you need before Day 1

| Need | Notes |
|---|---|
| Azure account with credit | Personal Microsoft account → [azure.microsoft.com/free](https://azure.microsoft.com/free/) |
| Laptop tools | `az` CLI, `kubectl`, Docker, a registry login (Docker Hub is fine) |
| Keycloak host access | SSH + ability to edit reverse proxy / TLS / env for `keycloack.frostgate.in` |
| DNS for Keycloak | Hostname already points at that server; HTTPS must work in a browser |
| Entra ID access | Permission to create an **App registration** (most personal/org tenants allow this) |
| This repo cloned | Includes `k8s/` manifests |

Install tools (macOS example):

```bash
brew install azure-cli kubernetes-cli
az login
az account show   # confirm the free/trial subscription is selected
```

---

## 4. Recommended order of work (timeline)

Do **not** start with AKS. Order matters: Keycloak issuer URL and Postgres must exist before the app pod can stay healthy.

| Day | Focus | Done when… |
|---|---|---|
| **0** | Azure login, budget alert, naming | `az account show` works; budget email configured |
| **1** | Keycloak production-ish mode + realm + client | Browser reaches `https://…/realms/<realm>`; local users can log into Keycloak |
| **2** | Azure RG + Flexible Postgres | `psql` or a GUI can connect from your laptop with SSL |
| **3** | AKS + kube credentials | `kubectl get nodes` shows Ready |
| **4** | Networking + deploy app image to AKS | Pod Running; `/` returns HTML from EXTERNAL-IP |
| **5** | Entra ID app + Keycloak identity provider | “Login with Microsoft” appears; Azure user gets a Keycloak JWT |
| **6** | Full app E2E + role test | Editor can draft; publisher can publish; 401s without token |
| **7** | Optional: leave running 24h, then **tear down** | Resource group deleted; credit preserved |

---

## 5. Phase 0 — Azure account, tools, naming

### 5.1 Create / confirm the subscription

1. Sign up / sign in at Azure Portal.
2. Open **Cost Management + Billing** and confirm free credit remaining.
3. Create a **Budget**:
   - Scope: this subscription  
   - Amount: e.g. `$180`  
   - Alerts at 50%, 75%, 90%, 100%

### 5.2 Pick fixed names (write them down)

Use short ASCII names. Example set (change `fg` to your initials if you want):

| Name | Example value |
|---|---|
| Resource group | `rg-oshealth-prodtest` |
| Region | `centralindia` (or nearest) |
| AKS cluster | `aks-oshealth-test` |
| Postgres server | `psql-oshealth-test` (must be globally unique) |
| Postgres DB name | `oshealth` |
| Postgres admin user | `oshealthadmin` |
| App deployment id | `azure-aks-prodtest` |
| Keycloak realm | `os-health-check` |
| Keycloak client id | `os-health-check-web` |
| Publisher role | `lookup-publisher` |
| Keycloak public URL | `https://keycloack.frostgate.in` |

### 5.3 Login and set defaults

```bash
az login
az account list -o table
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
az configure --defaults group=rg-oshealth-prodtest location=centralindia
```

---

## 6. Phase 1 — Harden Keycloak on the remote server

You said Keycloak is **not fully configured**. Complete this **before** AKS, because the app will crash-loop without a reachable issuer and JWKS.

Follow the concepts in [`KEYCLOAK_SETUP.md`](KEYCLOAK_SETUP.md). Below is the production-test cut of that guide for your remote host.

### 6.1 Production mode (not `start-dev`)

On the Keycloak host:

1. Run Keycloak with `start` (production), **not** `start-dev`.
2. Back Keycloak with **its own Postgres** (local Docker Postgres on that server is fine).  
   **Do not** point Keycloak’s own DB at the Azure Flexible Server you create for the app — keep Keycloak data and app data separate ([`KEYCLOAK_SETUP.md` §7](KEYCLOAK_SETUP.md)).
3. Put HTTPS in front (Caddy / nginx / Traefik). Browser users and AKS pods must both reach Keycloak over **HTTPS**.
4. Set hostname env so issuer URLs are stable, roughly:
   - `KC_HOSTNAME=keycloack.frostgate.in` (or your real host)
   - `KC_PROXY_HEADERS=xforwarded` (or equivalent for your proxy)
   - `KC_HTTP_ENABLED=true` only behind the proxy on localhost

### 6.2 Smoke-check Keycloak HTTPS

From your laptop:

```bash
curl -fsS "https://keycloack.frostgate.in/realms/master" | head
# Should return JSON, not HTML error / certificate failure
```

If this fails, fix DNS / TLS / firewall **now**. AKS will not magically work later.

### 6.3 Create realm, client, role, and two local users

In Keycloak Admin Console (`https://keycloack.frostgate.in`, master admin login):

1. **Create realm** `os-health-check` (or your chosen name).
2. **Create client** `os-health-check-web` (Login settings / URLs only — this is **not** where you assign publisher):
   - Client authentication: **Off** (public client)
   - Standard flow: **On**
   - Root URL / Home URL: leave empty for now (optional)
   - Valid redirect URIs: you will add the AKS app URL later; for now add a placeholder you can edit, e.g. `http://localhost:8000/*` and later `http://<AKS-EXTERNAL-IP>:8000/*` (and preferably HTTPS if you put TLS in front of the app)
   - Valid post logout redirect URIs (optional): `http://localhost:8000/*`
   - Web origins: matching origin(s), or `+` during early testing only
   - Advanced → PKCE Code Challenge Method: **S256**
3. **Realm role** `lookup-publisher` (Realm roles → Create role). This is a **realm** role, not a client role and not an Entra role.
4. **Local users** (useful even after Azure federation exists — for break-glass):
   - `publisher` — assign `lookup-publisher`
   - `editor` — no publisher role  
   Set First name + Last name (Keycloak default profile requires them).

**Hiccup — role not listed when assigning to a user.** In Users → user → **Role mapping** → **Assign role**, the dialog often defaults to **Filter by clients**, which hides realm roles. Switch the filter to **Filter by realm roles**, then select `lookup-publisher` → Assign. Do not look for this role on the client’s Roles or Login settings screens.

### 6.4 Write down issuer URL

Issuer pattern:

```text
https://keycloack.frostgate.in/realms/os-health-check
```

Confirm discovery works:

```bash
curl -fsS "https://keycloack.frostgate.in/realms/os-health-check/.well-known/openid-configuration" | head
```

You should see `"issuer":"https://keycloack.frostgate.in/realms/os-health-check"` — the `issuer` string **must** match what you put in `KEYCLOAK_ISSUER_URL` later, character-for-character (including `https` and no trailing slash).

---

## 7. Phase 2 — Azure resource group + networking basics

```bash
az group create --name rg-oshealth-prodtest --location centralindia
```

### Networking choice for a first test (simple)

For a **short** production-style test on free credit, use this simpler path:

1. Create AKS with its default managed VNet (Azure creates it).
2. Create Postgres Flexible Server with **public access enabled**.
3. Restrict Postgres firewall to:
   - your laptop IP (admin), and  
   - AKS outbound / node public IPs (or temporarily “Allow public access from any Azure service within Azure” while debugging — **tighten before any real data**).

**Later / more secure (optional):** put Postgres on a private VNet + private access from AKS only. That is better production hygiene but slower to learn; skip it for week-1 of the test unless you already know Azure networking.

---

## 8. Phase 3 — Azure Database for PostgreSQL Flexible Server

### 8.1 Create the server (CLI)

Generate a strong password and keep it somewhere safe (password manager). Then:

```bash
export PG_ADMIN=oshealthadmin
export PG_PASSWORD='REPLACE_WITH_LONG_RANDOM_PASSWORD'
# Name must be globally unique across Azure; change if taken
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

**Hiccup — `export: not valid in this context: —`.** Copy/paste from this markdown can pull in a Unicode em dash (`—`) from a comment. zsh then treats it as an `export` argument. Paste only the bare `export VAR=value` lines, or keep comments on their own `# ...` lines with ASCII hyphens.

**Hiccup — CLI says “Paid Tier”.** Expected. `Standard_B1ms` / Burstable is still a **paid** SKU; on a free/trial account it burns credit (it is not always-free). Continue; it is the cheap SKU this plan intends.

> `--public-access 0.0.0.0` at create means **allow Azure services**, **not** “open to the whole internet” and **not** your laptop IP. You still need §8.2 (laptop IP or AllowAll) before `psql` from home works. If your CLI version rejects that flag, create with portal UI: Public access → add your client IP.

Create the application database:

```bash
az postgres flexible-server db create \
  --resource-group rg-oshealth-prodtest \
  --server-name "$PG_SERVER" \
  --name oshealth
```

**Hiccup — `the following arguments are required: --name/-n`.** This CLI uses `--name` for the **database** name. There is no `--database-name` flag (easy to guess wrong from other Azure commands).

### 8.2 Firewall: laptop first (or AllowAll for dynamic IP)

Laptop-only (prefer when your public IP is stable):

```bash
# Your current public IP
MYIP=$(curl -fsS https://ifconfig.me)
az postgres flexible-server firewall-rule create \
  --resource-group rg-oshealth-prodtest \
  --server-name "$PG_SERVER" \
  --name AllowMyLaptop \
  --start-ip-address "$MYIP" \
  --end-ip-address "$MYIP"
```

**Hiccup — home/ISP IP keeps changing.** For a short throwaway test only, allow every public IP (use a strong admin password; delete RG when done):

```bash
az postgres flexible-server firewall-rule create \
  --resource-group rg-oshealth-prodtest \
  --server-name "$PG_SERVER" \
  --name AllowAll \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 255.255.255.255
```

**Hiccup — `required: --server-name/-s` on firewall-rule.** On this command, `--server-name` is the Postgres server and `--name` is the **rule** name. Do not use `--name` for the server or `--rule-name` (those flags belong to other subcommands / older examples).

You can skip the laptop-only rule if you use AllowAll. You cannot skip firewall entirely if you need internet/`psql` access: create-time `0.0.0.0` alone will not open the server to your changing laptop IP.

### 8.3 Connection string shape (app expects this)

Hostname looks like:

```text
psql-oshealth-test.postgres.database.azure.com
```

App `DATABASE_URL` (note `sslmode=require` — required for Azure Flexible Server):

```text
postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require
```

Test from your laptop if you have `psql`:

```bash
psql "postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require" -c 'select version();'
```

The app will create its own schemas (`lookup`, vendor caches, `iam`, etc.) on first successful connect — you do **not** pre-create tables.

### 8.4 Optional: stop server overnight (credit saver)

If your SKU/region supports stop/start:

```bash
az postgres flexible-server stop --resource-group rg-oshealth-prodtest --name "$PG_SERVER"
# later:
az postgres flexible-server start --resource-group rg-oshealth-prodtest --name "$PG_SERVER"
```

AKS nodes do **not** stop as cleanly — for multi-day pauses, prefer deleting the node pool or the whole resource group (Phase 9).

---

## 9. Phase 4 — Create AKS (small, cost-aware)

### 9.1 Create the cluster

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

**Hiccup — `Standard_B2s` not allowed in `centralindia`.** Subscription/region SKU lists often drop classic B-series in favor of v2. Use `Standard_B2s_v2` (listed as `standard_b2s_v2` in the error). If that fails too, pick another small size from the error’s available list (e.g. `Standard_B2as_v2`). Listing helpers:

```bash
az vm list-skus --location centralindia --size Standard_B --all --output table
# or use the “available VM sizes” list from the Failed AKS BadRequest message
```

This can take 5–15 minutes.

Get credentials (required even if you already used kubectl with Minikube — otherwise kubectl keeps talking to `127.0.0.1` and fails with “connection refused”):

```bash
az aks get-credentials \
  --resource-group rg-oshealth-prodtest \
  --name aks-oshealth-test \
  --overwrite-existing

kubectl config current-context   # should mention aks-oshealth-test, not minikube
kubectl get nodes
kubectl cluster-info
```

You want one node `Ready`.

### 9.2 Find AKS egress IP(s) for Postgres firewall

AKS pods reach the internet (and your public Postgres) via managed **outbound** public IP(s), not via the node’s `EXTERNAL-IP` column and not via the app Service LoadBalancer IP.

**Hiccup — `kubectl get nodes -o wide` shows `EXTERNAL-IP: <none>`.** Normal on AKS with Azure CNI + load-balancer outbound. Do not wait for a node public IP.

**Hiccup — `az aks show ... --query networkProfile.outboundIPs` prints nothing.** That field is often empty/null on default clusters. Use the node resource group’s public IPs instead:

```bash
NODE_RG=$(az aks show -g rg-oshealth-prodtest -n aks-oshealth-test --query nodeResourceGroup -o tsv)
echo "Node RG: $NODE_RG"
az network public-ip list -g "$NODE_RG" --query "[].{name:name,ip:ipAddress}" -o table
```

Add each listed public IP as a Postgres firewall rule (same pattern as `AllowMyLaptop`, with `--server-name` / `--name` for the rule).

**Skip this if you already created the throwaway `AllowAll` firewall rule (§8.2).** Pods can already reach Postgres; tighten later if you remove AllowAll.

If the pod later cannot connect to Postgres, a missing egress firewall rule is the #1 cause ([`k8s/README.md`](k8s/README.md) already calls this out).

---

## 10. Phase 5 — Let AKS reach Postgres and Keycloak

Checklist before deploy:

| Path | Must work |
|---|---|
| AKS pod → Azure Postgres `:5432` TLS | Firewall allows AKS egress IP; `sslmode=require` in URL |
| AKS pod → `https://keycloack.frostgate.in` | Server firewall allows Azure egress; valid public TLS cert |
| Browser → Keycloak HTTPS | Same host, valid cert |
| Browser → App EXTERNAL-IP | After Service exists (next phase) |

**Keycloak server firewall.** Allow inbound **443** from the internet (browsers + AKS). Do not expose Keycloak admin over the internet without a strong admin password; ideally restrict admin paths by IP later.

**Issuer split (only if needed).** If AKS for some reason cannot use the public hostname but browsers can, you would set:

- `KEYCLOAK_ISSUER_URL` = browser-facing URL (must match token `iss`)
- `KEYCLOAK_INTERNAL_URL` = URL the **pod** uses to fetch JWKS  

For a normal public hostname with valid DNS from Azure, leave `KEYCLOAK_INTERNAL_URL` unset (defaults to issuer). See [`KEYCLOAK_SETUP.md` §4 / §6](KEYCLOAK_SETUP.md).

---

## 11. Phase 6 — Build, push, and deploy the app

This phase is the real-cluster path from [`k8s/README.md`](k8s/README.md), filled in for Azure.

### 11.1 Build and push image

```bash
# from repo root
export DOCKERHUB_USER=youruser
docker build -t "$DOCKERHUB_USER/os-health-check:v1" .
docker push "$DOCKERHUB_USER/os-health-check:v1"
```

Edit `k8s/deployment.yaml`:

- `image: docker.io/<you>/os-health-check:v1`
- `imagePullPolicy: Always`

(Do not leave `os-health-check:local` / `IfNotPresent` — those are for minikube only.)

### 11.2 ConfigMap values for this test

Edit `k8s/configmap.yaml` before apply:

```yaml
DEPLOYMENT_ID: "azure-aks-prodtest"
KEYCLOAK_ISSUER_URL: "https://keycloack.frostgate.in/realms/os-health-check"
KEYCLOAK_AUDIENCE: "os-health-check-web"
KEYCLOAK_PUBLISHER_ROLE: "lookup-publisher"
LOOKUP_DB_ENABLED: "true"
```

### 11.3 Namespace + Secret + apply

```bash
cd k8s
kubectl apply -f namespace.yaml

kubectl create secret generic os-health-check-secrets \
  --namespace os-health-check \
  --from-literal=DATABASE_URL='postgresql://oshealthadmin:YOUR_PASSWORD@psql-oshealth-test.postgres.database.azure.com:5432/oshealth?sslmode=require' \
  --from-literal=OPENAI_API_KEY='' \
  --from-literal=GEMINI_API_KEY='' \
  --from-literal=OPENROUTER_API_KEY=''

kubectl apply -f configmap.yaml -f pvc.yaml -f deployment.yaml -f service.yaml
```

Watch startup:

```bash
kubectl -n os-health-check get pods -w
kubectl -n os-health-check logs -f deployment/os-health-check
```

Healthy first boot should eventually log either:

- import of seed CSV into empty `lookup` data, or  
- `already has 'data' rows -- skipping import`

If the pod crash-loops complaining about missing Keycloak/DB env, fix ConfigMap/Secret and `kubectl apply` / recreate the secret.

### 11.4 Get the app URL

```bash
kubectl -n os-health-check get service os-health-check
```

Wait until `EXTERNAL-IP` is filled (1–3 minutes). App URL for the test:

```text
http://<EXTERNAL-IP>:8000
```

(Your `service.yaml` is type `LoadBalancer` — Azure provisions a public IP automatically.)

### 11.5 Update Keycloak redirect URIs

Back in Keycloak client `os-health-check-web`:

- Valid redirect URIs: `http://<EXTERNAL-IP>:8000/*`
- Web origins: `http://<EXTERNAL-IP>:8000`

Without this, login fails with `invalid redirect_uri`.

---

## 12. Phase 7 — Azure AD (Entra ID) federation in Keycloak

The app does **not** talk to Azure AD. Keycloak brokers Microsoft login and still issues its **own** JWT. That is why no app code change is required ([`KEYCLOAK_SETUP.md` §2 / §5](KEYCLOAK_SETUP.md)).

### 12.1 Register an app in Microsoft Entra ID

In [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**:

| Field | Value |
|---|---|
| Name | `OS Health Check Keycloak Broker` |
| Supported account types | Single tenant (or multi if you know you need it) |
| Redirect URI platform | **Web** |
| Redirect URI | `https://keycloack.frostgate.in/realms/os-health-check/broker/microsoft/endpoint` |

Notes on the redirect URI:

- Path is Keycloak’s identity-provider broker callback.
- Alias `microsoft` in the path must match the **Alias** you set when adding the IdP in Keycloak (next step). If you alias it `azure-ad`, the path becomes `.../broker/azure-ad/endpoint`.

Then:

1. **Certificates & secrets** → New client secret → copy the **Value** once.
2. Overview → copy **Application (client) ID** and **Directory (tenant) ID**.
3. Optional but useful: **Token configuration** → add optional claim `email` (and maybe `preferred_username`) to ID token.

Entra endpoints you will need (tenant-specific):

```text
Authorization: https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/authorize
Token:         https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
Logout:        https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/logout
Metadata / JWKS via OIDC discovery:
https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration
```

### 12.2 Add Identity Provider in Keycloak

Keycloak Admin → realm `os-health-check` → **Identity providers** → **OpenID Connect v1.0** (or Microsoft/Entra template if present):

| Field | Value |
|---|---|
| Alias | `microsoft` (must match redirect URI path) |
| Display name | `Microsoft` |
| Discovery endpoint | `https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration` |
| Client ID | Entra Application (client) ID |
| Client Secret | Entra client secret Value |
| Default scopes | `openid profile email` |

Save. On the IdP **Mappers** tab, map `email` (and names if you want) into the Keycloak user profile — cosmetic for this app; identity key remains Keycloak `sub`.

### 12.3 First Microsoft login

1. Open the app URL → login → choose **Microsoft**.
2. Sign in with an Entra user.
3. Keycloak creates a local linked user on first brokered login.
4. In Keycloak **Users**, open that user → **Role mapping** → assign `lookup-publisher` to at least one Azure user you will use for publish tests.

### 12.4 Optional cutover settings

Once Microsoft login works:

- You may hide the local username/password form (realm Authentication / login theme / IdP “GUI order”) so testers only see Microsoft — keep one local admin/break-glass account.
- Export the realm JSON backup **before** more IdP changes ([`KEYCLOAK_SETUP.md` §7](KEYCLOAK_SETUP.md)).

---

## 13. Phase 8 — End-to-end test checklist

Run these in order. Tick them off literally.

### A. Infrastructure

- [ ] `kubectl get nodes` → Ready  
- [ ] `kubectl -n os-health-check get pods` → Running / Ready  
- [ ] Logs show DB seed or skip-import, not connection timeouts  
- [ ] Browser opens `http://EXTERNAL-IP:8000` and gets the app shell  

### B. Auth (local Keycloak users)

- [ ] Login as `editor` works  
- [ ] Can create/edit a Draft  
- [ ] Publish is denied without `lookup-publisher`  
- [ ] Login as `publisher` → publish succeeds  

### C. Azure AD federation

- [ ] “Microsoft” button shown on Keycloak login  
- [ ] Entra login returns to app without `invalid_redirect` / consent errors  
- [ ] API calls succeed with the federated user’s JWT  
- [ ] Assign `lookup-publisher` to that federated user → publish works  

### D. App data path on Azure Postgres

- [ ] Refresh EOL/EOAS completes against a few rows  
- [ ] Vendor lookup Update for one source succeeds (optional; slow)  
- [ ] Publish creates revision; second browser/session sees published Data  
- [ ] Pod restart (`kubectl -n os-health-check rollout restart deployment/os-health-check`) keeps Postgres data and PVC `_config`  

### E. Negative tests (prove security wiring)

- [ ] Call `/api/lookup` without `Authorization` → 401  
- [ ] Token from a **different** realm/issuer → 401  
- [ ] Wrong redirect URI in Keycloak breaks login (confirm, then restore)  

---

## 14. Phase 9 — Tear down (protect the $200)

When the test is done (or overnight pause for days):

```bash
# Deletes AKS, Postgres, public IPs, disks created in this group
az group delete --name rg-oshealth-prodtest --yes --no-wait
```

Confirm in portal that the resource group is gone and Cost Management no longer shows those resources accumulating.

**What this does not delete**

- Your remote Keycloak server / DNS / TLS  
- Entra ID app registration (delete manually under App registrations if you want)  
- Docker Hub image tags  

---

## 15. Troubleshooting map

| Symptom | Most likely cause | Fix |
|---|---|---|
| `export: not valid in this context: —` (or similar) | Unicode em dash from markdown comment pasted into shell | Re-run bare `export VAR=value`; put notes on separate `#` lines |
| Keycloak client Login settings: what to fill? | Confusing client URLs with roles | Set Valid redirect URIs + Web origins only; roles are under Realm roles / Users → Role mapping |
| Created `lookup-publisher` but Assign role list is empty / missing it | Assign dialog filtered to **clients** | Switch filter to **Filter by realm roles** |
| Looking for `lookup-publisher` under Entra or client Roles | Wrong place | It is a Keycloak **realm** role on the linked Keycloak user |
| Postgres create warns “Paid Tier” | Expected for `Standard_B1ms` | Continues to bill credit; not always-free — OK for this plan |
| `db create` errors: required `--name/-n` | Used `--database-name` | Use `--name oshealth` (DB name flag is `-n` / `--name`) |
| `firewall-rule create` errors: required `--server-name/-s` | Used `--name` for server / `--rule-name` for rule | Use `--server-name` for server, `--name` for the rule |
| AKS create: `Standard_B2s` not allowed in location | Region/subscription SKU denylist | Retry with `--node-vm-size Standard_B2s_v2` (or another size from the error list) |
| `kubectl` connection refused to `127.0.0.1:...` | Still on Minikube/old context | `az aks get-credentials ... --overwrite-existing`; check `kubectl config current-context` |
| Node `EXTERNAL-IP` is `<none>` | Expected on AKS outbound LB | List public IPs in MC_* node RG (see §9.2); or rely on Postgres AllowAll |
| `networkProfile.outboundIPs` empty | Wrong/legacy query path | `az network public-ip list -g "$NODE_RG"` |
| `psql` / laptop cannot reach Postgres after create | `--public-access 0.0.0.0` ≠ allow-all internet | Add laptop IP (§8.2) or AllowAll `0.0.0.0`–`255.255.255.255` |
| Firewall “AllowAll” still feels wrong for prod | Wide open for convenience | Fine only for short credit test; tear down RG; tighten before real data |
| Pod stuck / no `[lookup_db]` log | AKS cannot reach Postgres (firewall) | Add AKS egress IP to Flexible Server firewall; verify `sslmode=require` |
| CrashLoop: missing env RuntimeError | ConfigMap/Secret incomplete | Set `DEPLOYMENT_ID`, `KEYCLOAK_*`, `DATABASE_URL`, `LOOKUP_DB_ENABLED=true` |
| Login `invalid redirect_uri` | Client redirect URIs don’t include EXTERNAL-IP | Update Keycloak client |
| Login OK but every API 401 issuer mismatch | `KEYCLOAK_ISSUER_URL` ≠ token `iss` | Must match discovery `issuer` exactly; use public HTTPS URL |
| Browser OK, server cannot fetch JWKS | Pod cannot reach Keycloak HTTPS | Open 443 on Keycloak host; DNS from Azure; optional `KEYCLOAK_INTERNAL_URL` |
| Microsoft login fails at Entra | Redirect URI mismatch | Entra redirect must equal Keycloak broker endpoint including IdP alias |
| Federated user cannot publish | Missing realm role | Assign `lookup-publisher` on the Keycloak user created after first Microsoft login (realm-role filter) |
| EXTERNAL-IP stuck `<pending>` | Azure LB provisioning / quota | Check AKS service events; wait; verify subscription not blocked |
| Credit draining fast | Idle AKS node + Postgres | Stop Postgres; delete RG when idle |

More Keycloak-specific gotchas (PKCE, audience/`azp`, required actions): [`KEYCLOAK_SETUP.md` §6](KEYCLOAK_SETUP.md).

---

## 16. Decision log (defaults this plan locks in)

| Decision | Choice for this test | Rationale |
|---|---|---|
| Postgres placement | Azure Flexible Server `B1ms` | Managed + matches repo k8s guidance |
| Redis | Skip | App does not use Redis |
| Keycloak placement | Keep on `keycloack.frostgate.in` | Already owned; app only needs stable OIDC issuer |
| App exposure | Service type LoadBalancer HTTP | Fastest; enough for a closed test audience |
| Auth federation | Entra OIDC → Keycloak broker | Required; no app code change |
| AKS size | 1 × `Standard_B2s_v2` | Fits ~$200 if deleted after the test window; `B2s` often unavailable in `centralindia` |
| Image registry | Docker Hub | Avoid ACR cost for a short test |
| Networking sophistication | Public Postgres + firewall IPs | Learnable in a day; private VNet later |

---

## Quick command cheat sheet

```bash
# Azure context
az account show
az group list -o table

# Cluster
az aks get-credentials -g rg-oshealth-prodtest -n aks-oshealth-test
kubectl -n os-health-check get all
kubectl -n os-health-check logs -f deployment/os-health-check

# App URL
kubectl -n os-health-check get svc os-health-check

# Postgres firewall: add current laptop IP
MYIP=$(curl -fsS https://ifconfig.me)
az postgres flexible-server firewall-rule create \
  -g rg-oshealth-prodtest -s psql-oshealth-test \
  --name AllowLaptop-$(date +%s) \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# Postgres firewall: allow all public IPs (throwaway test / changing home IP only)
az postgres flexible-server firewall-rule create \
  -g rg-oshealth-prodtest -s psql-oshealth-test \
  --name AllowAll \
  --start-ip-address 0.0.0.0 --end-ip-address 255.255.255.255

# Nuclear teardown
az group delete -n rg-oshealth-prodtest --yes --no-wait
```

---

## What “success” looks like

You can walk a colleague through:

1. Open the AKS LoadBalancer URL.  
2. Click login → Microsoft → Entra credentials.  
3. Edit a Draft in OS Health Check.  
4. Publish only if that Entra-linked Keycloak user has `lookup-publisher`.  
5. Data survives a pod restart because it lives in Azure Postgres.

When that works, the production-shaped path (AKS + managed Postgres + remote Keycloak + Azure federation) is validated. Everything after that is hardening: custom DNS + TLS on the app, private Postgres, smaller blast radius for Keycloak admin, backups, and monitoring — not prerequisites for this credit-limited test.
