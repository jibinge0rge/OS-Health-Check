# Setting Up Keycloak for OS Health Check

> **What this document is for.** A practical, step-by-step guide to standing
> up Keycloak for this app — dev now, production federation later — plus the
> config values `auth.py` actually needs and a troubleshooting section for
> the gotchas most likely to bite a first-time setup.
>
> This is the "how" companion to
> [AUTH_MULTITENANCY_PLAN.md](AUTH_MULTITENANCY_PLAN.md), which is the
> "why" — the architecture decisions (multi-tenant isolation, per-user
> Draft, why `DEPLOYMENT_ID` is independent of Keycloak) behind everything
> below. Read that doc's §1-§7 first if you want the reasoning; come here
> once you're ready to actually click through Keycloak.
>
> Every step here was actually run against a real (throwaway) Keycloak
> instance while implementing this app's auth — both standalone and, for
> §3.1, as a fully containerized `db` + `keycloak` + `os-health-check`
> stack via docker-compose — including hitting and fixing the gotchas in
> [§6](#6-troubleshooting--gotchas-actually-hit-during-setup).

---

## Table of contents

1. [Which topology to use](#1-which-topology-to-use)
2. [Why federation can be added later, for free](#2-why-federation-can-be-added-later-for-free)
3. [Quick local/dev setup](#3-quick-localdev-setup)
   - [3.1 Fastest: the bundled docker-compose Keycloak](#31-fastest-the-bundled-docker-compose-keycloak)
   - [3.2 Manual click-through setup (any Keycloak)](#32-manual-click-through-setup-any-keycloak)
4. [Config values the app needs](#4-config-values-the-app-needs)
5. [Adding Azure AD federation later (production)](#5-adding-azure-ad-federation-later-production)
6. [Troubleshooting / gotchas actually hit during setup](#6-troubleshooting--gotchas-actually-hit-during-setup)
7. [Production hardening checklist for Keycloak itself](#7-production-hardening-checklist-for-keycloak-itself)

---

## 1. Which topology to use

Three Keycloak topologies all work with this app **unchanged** — see
AUTH_MULTITENANCY_PLAN.md §3.1 for the full explanation of why. Pick
whichever matches what you already have, or default to option 2 if you're
starting fresh:

1. **Fully separate Keycloak per deployment** — each deployment's own
   Keycloak host.
2. **One Keycloak server, one realm per deployment** (recommended default)
   — same host, a distinct realm (and its own `KEYCLOAK_ISSUER_URL`) per
   deployment. Gives each deployment its own user pool/roles/federation
   config while only operating one Keycloak server.
3. **One Keycloak server, one realm shared by every deployment** — every
   deployment's app instance points at the same `KEYCLOAK_ISSUER_URL` and
   client. Simplest to operate; the app's own `DEPLOYMENT_ID` (never derived
   from Keycloak) still keeps each deployment's users/drafts separate.

Nothing below differs based on this choice beyond "one realm" vs. "several
realms" — the steps are the same either way, just repeated once per realm
if you go with options 1 or 2.

## 2. Why federation can be added later, for free

The app's backend (`auth.py`) only ever talks to **Keycloak's own** OIDC
endpoints (discovery, JWKS, token validation) and only ever trusts
**Keycloak-issued** JWTs — it never talks to Azure AD, or any other upstream
identity provider, directly.

Whether a realm's users are stored locally in Keycloak or **federated** in
from Azure AD (via Keycloak's "Identity provider" broker feature — OpenID
Connect or SAML) is entirely a realm-level configuration matter inside
Keycloak. The token Keycloak hands to the app has the same shape either
way: same issuer, same JWKS-verifiable signature, and `sub` is Keycloak's
own stable internal user id even for a brokered/federated identity —
Keycloak mints its own local user record to represent the federated
identity the first time someone logs in through the broker.

Practically: build and test everything now against plain Keycloak-local
users (§3 below). Turning on Azure AD federation later is a change made
entirely in the Keycloak admin console (§5) — not in this app's code or
data model. The one thing worth knowing up front: exactly which claims
(`email`, `preferred_username`) get populated, and how, can differ slightly
for a federated login depending on the attribute mappers you configure on
that identity provider — the app already treats those as nullable,
display-only fields and never as the actual identity key (`sub` is, and
that's unaffected by federation).

## 3. Quick local/dev setup

### 3.1 Fastest: the bundled docker-compose Keycloak

For local testing (never for AKS/EKS — see §1), `docker-compose.yml` ships
a throwaway Keycloak with a **pre-configured realm, client, role, and two
test users — zero manual admin-console clicking.** This is the
recommended way to test this app's auth locally; use §3.2 instead only if
you need a from-scratch realm (e.g. to actually configure production) or
you're not using docker-compose at all.

```bash
docker compose up -d --build
```

That's it — Keycloak starts alongside `db` and the app, no separate flag
needed. The `os-health-check` service's `KEYCLOAK_ISSUER_URL`/
`KEYCLOAK_AUDIENCE`/`DEPLOYMENT_ID` already default to match this bundled
Keycloak (see `docker-compose.yml`'s comments), so no `.env` changes are
needed at all for this path.

**Already have a real/different Keycloak you'd rather use instead?** Set
`KEYCLOAK_ISSUER_URL`/`KEYCLOAK_AUDIENCE`/`KEYCLOAK_INTERNAL_URL` in `.env`
to point at it — the bundled `keycloak` container still starts (same as
the bundled `db` already does if you point `DATABASE_URL` elsewhere), it
just sits there unused. See §3.2 for setting up a from-scratch realm on
whatever Keycloak you're pointing at instead.

What's pre-configured, from
[docker/keycloak/os-health-check-dev-realm.json](docker/keycloak/os-health-check-dev-realm.json)
(realm `os-health-check-dev`, imported automatically on first boot):

| Thing | Value |
|---|---|
| Client | `os-health-check-web` — public, PKCE S256, redirect URI `http://localhost:8000/*` (the app's default `APP_PORT`) |
| Realm role | `lookup-publisher` |
| Test user (can publish) | `publisher` / `publisher123` |
| Test user (cannot publish) | `editor` / `editor123` |

The realm re-imports fresh every time (there's no persisted volume for
Keycloak here — see the service's comment in `docker-compose.yml`), so this
stays a clean, reproducible state to test against instead of accumulating
manual changes.

**If you're running the app on a different port** (not the default 8000 —
e.g. testing multiple instances side by side), the client's redirect URI
above won't match, and login will fail with `invalid redirect_uri`. Add
your port to the client via `kcadm.sh` (there's no admin-console GUI needed
for a one-off addition):

```bash
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://localhost:8080 --realm master --user admin --password admin
CID=$(docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get clients \
  -r os-health-check-dev -q clientId=os-health-check-web --fields id --format csv --noquotes | tr -d '\r')
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh update clients/$CID \
  -r os-health-check-dev -s 'redirectUris=["http://localhost:8000/*","http://localhost:YOUR_PORT/*"]'
```

This section — including the container-to-container networking behind it
— was actually run against a real, fully containerized `db` + `keycloak` +
`os-health-check` stack while building this feature; see §6 for the one
real gotcha it surfaced (the `iss` mismatch between what the browser sees
and what the app container can reach).

### 3.2 Manual click-through setup (any Keycloak)

Use this for a from-scratch realm on any Keycloak — a real one you're
standing up for production, or a local one not run via this repo's
docker-compose. For local dev specifically, §3.1 above is faster and needs
no manual steps at all.

Run Keycloak locally to develop and test against:

```bash
docker run -d --name keycloak -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:latest start-dev
```

`start-dev` is dev-only — it allows plain HTTP and skips production
hardening. Never point a real deployment at a `start-dev` instance (§7
covers production mode).

In the admin console (`http://localhost:8080`, login `admin`/`admin`):

1. **Create a Realm** — realm dropdown (top-left) → *Create Realm*. Name it
   after your deployment if you're going with §1's recommended
   one-realm-per-deployment option (e.g. `os-health-check-acme`), or a
   single realm name if you're intentionally sharing one realm across
   deployments (§1, option 3) — both work unchanged.
2. **Create a Client** — *Clients* → *Create client*:
   - Client ID: e.g. `os-health-check-web`
   - Client authentication: **Off** — this makes it a *public* client,
     correct for a browser app doing Authorization Code + PKCE with no
     secret to manage or leak.
   - Capability config → Standard flow: **On**.
   - Valid redirect URIs: your app's URL, e.g. `http://localhost:8000/*` for
     local dev.
   - Web origins: the same origin, so the browser can call the token
     endpoint directly (CORS).
   - Advanced tab → "Proof Key for Code Exchange Code Challenge Method":
     **S256** — required, since a public client has no secret to prove its
     identity otherwise.
3. **Create the publish role** — *Realm roles* → *Create role* → name it
   `lookup-publisher` (matches `auth.py`'s `KEYCLOAK_PUBLISHER_ROLE`
   default). Assign it (*Users → [user] → Role mapping*) to whichever users
   should be allowed to publish a draft.
4. **Create test users** — *Users* → *Add user*. Fill in **First name** and
   **Last name** at creation time, not just username/email — Keycloak's
   default realm profile requires them, and skipping this is the single
   most common way to get stuck (see §6). Set a password under the
   *Credentials* tab (turn off "Temporary" for convenience while testing).
   Create at least two: one with the `lookup-publisher` role assigned and
   one without, so you can verify both the "can publish" and "cannot
   publish" paths end to end.

## 4. Config values the app needs

| Env var | Where to find it |
|---|---|
| `KEYCLOAK_ISSUER_URL` | `http://<host>:8080/realms/<realm-name>` — also shown on *Realm settings → General* as the realm's issuer. **Must be reachable from the browser** (and is handed to it, via index.html, to build the login/token/logout redirects). |
| `KEYCLOAK_INTERNAL_URL` | Optional, defaults to `KEYCLOAK_ISSUER_URL`. Only set this when the app **server** can't reach `KEYCLOAK_ISSUER_URL` itself — the bundled docker-compose setup (§3.1) is exactly this case: the browser reaches Keycloak via a published host port (`http://localhost:8081/...`), but the app container reaches it via the Docker network's own service name (`http://keycloak:8080/...`) instead. Used only for this server's own JWKS/discovery fetch — never for `iss` validation. |
| `KEYCLOAK_AUDIENCE` | The Client ID you created in §3.2 step 2 (e.g. `os-health-check-web`) — already `os-health-check-web` if using the bundled realm (§3.1). |
| `DEPLOYMENT_ID` | Your own choice, independent of anything in Keycloak (§1) — pick whatever slug identifies this deployment. |
| `KEYCLOAK_PUBLISHER_ROLE` | Optional — only set if you used a realm role name other than `lookup-publisher`. |

**Don't hardcode JWKS/token/auth endpoints.** `auth.py` fetches
`<issuer>/.well-known/openid-configuration` itself (cached) to find the
JWKS URI and other endpoints — Keycloak serves this automatically for every
realm, so nothing beyond the issuer URL(s) above needs to be looked up by
hand.

## 5. Adding Azure AD federation later (production)

Realm-level config only — no app changes, no env var changes:

1. In the target realm: *Identity providers* → *Add provider* →
   **OpenID Connect** (or the built-in Microsoft/Entra template) → fill in
   the Azure AD app registration's client ID, client secret, and tenant
   endpoint (from the Azure/Entra portal).
2. Optionally hide the local username/password form once that realm should
   fully cut over to Azure AD sign-in.
3. Use that identity provider's *Mappers* tab to map any Azure AD claims you
   want surfaced (e.g. `email`) — cosmetic only, since the app never keys
   identity off these fields (§2).

The app keeps validating tokens against the exact same Keycloak realm
endpoints as before; Keycloak is simply now brokering to Azure AD behind
the scenes for that realm's logins.

## 6. Troubleshooting / gotchas actually hit during setup

Real issues hit (and fixed) while building and verifying this app's auth
against a real Keycloak instance — worth checking first if something's not
working:

- **Login redirects back to the app, but the token exchange fails with
  `invalid_code_verifier` / a 400 from `/protocol/openid-connect/token`.**
  This is a PKCE code-verifier mismatch, not a Keycloak misconfiguration —
  double-check whatever's calling the token endpoint sends the *exact same*
  `code_verifier` that produced the `code_challenge` sent to the
  authorization endpoint, and that it's read *before* anything clears it
  from storage. (This app's own `static/js/auth.js` hit exactly this bug
  once during development — the fix was ordering, not Keycloak config.)
- **A brand-new user can't log in via a non-interactive grant (e.g. testing
  with the password grant via `curl`), failing with `"error":
  "resolve_required_actions"` / `"reason": "Account is not fully set up"`.**
  Keycloak's default realm profile requires First name + Last name; a user
  created via the admin API/CLI without them has an outstanding required
  action that only an *interactive* login flow can resolve (Keycloak shows
  an "Update Account Information" form). Fix: set `firstName`/`lastName`
  on the user (or fill them in on first interactive login) — see §3.2 step
  4. (Not a concern with the bundled realm in §3.1 — its two test users
  already have these fields set.)
- **Every request 401s with an audience error, even though login itself
  succeeded.** Keycloak does **not** put the client id in a token's `aud`
  claim by default for a public client — only in `azp` (authorized party)
  — unless you add an explicit "Audience" protocol mapper to the client.
  `auth.py` already accounts for this (it accepts a match on either `aud`
  or `azp`), so this shouldn't bite you here — but it's the thing to check
  first if you're ever debugging audience validation against a *different*
  OIDC library that only checks `aud` strictly.
- **The authorization redirect fails with an `invalid redirect_uri` error
  from Keycloak.** The client's *Valid redirect URIs* (§3.2 step 2) must
  literally match the URL the app is actually served from, including port
  — a client configured for `http://localhost:8000/*` will reject a login
  attempt from `http://localhost:8010/`. Update the client's redirect URIs
  (and *Web origins*) to match wherever you're actually running the app
  (§3.1 has the one-off `kcadm.sh` command for the bundled realm).
- **Login succeeds, but every subsequent API call 401s with an issuer
  mismatch (`"Token was not issued by this deployment's configured
  Keycloak realm"`).** This is the container-networking version of the
  same root cause as the redirect_uri gotcha above: the browser and the
  app **server** can reach Keycloak via two different URLs (a published
  host port vs. the Docker network's internal service name), so the token's
  real `iss` claim (based on whatever URL the browser used) doesn't match
  what the server expects. Fix: set `KEYCLOAK_INTERNAL_URL` to whatever URL
  the app container can actually reach Keycloak at, and leave
  `KEYCLOAK_ISSUER_URL` as the browser-facing one (§4) — never the reverse.
  The bundled docker-compose setup (§3.1) already has this split configured
  correctly; this only bites you if you're wiring up a similar bundled
  setup yourself.

## 7. Production hardening checklist for Keycloak itself

Separate from this app's own production concerns
(AUTH_MULTITENANCY_PLAN.md §10-11):

- Run Keycloak in production mode (`start`, not `start-dev`), backed by a
  real Postgres database for **Keycloak's own** internal storage — this
  must be a separate database from this app's `DATABASE_URL`/`lookup`
  schema, not a shared one.
- Terminate TLS in front of Keycloak and set `KC_HOSTNAME` explicitly, so
  the issuer URL embedded in every token stays stable and correct behind a
  proxy/ingress.
- Export/back up realm configuration before structural changes (e.g. adding
  the Azure AD identity provider in §5) — Keycloak supports realm
  export/import for this.
- Replace the dev-quickstart `admin`/`admin` credentials with real, rotated
  admin credentials before anything is internet-reachable.
- Decide a client-secret rotation policy for any future *confidential*
  clients (e.g. a server-to-server integration) — the browser client itself
  stays public/PKCE regardless, so this only applies if you add other
  client types later.
