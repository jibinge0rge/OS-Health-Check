"""Keycloak OIDC Bearer-JWT authentication for every /api/* route.

Stateless by design (AUTH_MULTITENANCY_PLAN.md §2/§5): no server-side
session store. Each request's `Authorization: Bearer <token>` is validated
against *this deployment's own* Keycloak realm -- resolved once via OIDC
discovery and cached as a PyJWT PyJWKClient, which handles its own JWKS
caching + `kid` (key-rotation) lookups internally.

Works unchanged whether Keycloak is one instance per deployment, one Keycloak
server with a realm per deployment, or a single realm shared by every
deployment (plan §3.1) -- this module only ever cares about the one
KEYCLOAK_ISSUER_URL configured for *this* running app instance. It also
works unchanged once a realm starts federating to an upstream IdP (Azure AD,
KEYCLOAK_SETUP.md §5): federation only changes how Keycloak authenticates
the user upstream, never the shape of the JWT this module validates.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import jwt
import requests
from fastapi import Depends, HTTPException, Request

import iam_db

DEPLOYMENT_ID = os.environ.get("DEPLOYMENT_ID", "").strip()
# Validated against the token's `iss` claim, and handed to the browser
# (index.html -> auth.js) for the login/token/logout redirects -- must be
# whatever URL the browser actually reaches Keycloak at.
KEYCLOAK_ISSUER_URL = os.environ.get("KEYCLOAK_ISSUER_URL", "").strip().rstrip("/")
# Optional -- only needed when this server process can't reach
# KEYCLOAK_ISSUER_URL itself (e.g. Keycloak bundled in docker-compose: the
# browser reaches it via a published host port, but the app container has
# to reach it via the Docker network's own service hostname instead).
# Defaults to KEYCLOAK_ISSUER_URL, i.e. a no-op, whenever both are reachable
# at the same URL. Used ONLY for this server's own discovery/JWKS fetch --
# never for `iss` validation, which always checks KEYCLOAK_ISSUER_URL, since
# that's what a real browser-obtained token actually contains.
KEYCLOAK_INTERNAL_URL = (
    os.environ.get("KEYCLOAK_INTERNAL_URL", "").strip().rstrip("/") or KEYCLOAK_ISSUER_URL
)
KEYCLOAK_AUDIENCE = os.environ.get("KEYCLOAK_AUDIENCE", "").strip()
# Realm role gating who may publish a draft into the shared Data (plan §6.3).
# Any authenticated user can still create/edit/save their own private draft.
PUBLISHER_ROLE = os.environ.get("KEYCLOAK_PUBLISHER_ROLE", "lookup-publisher").strip()

# Fails loudly at import time, same pattern as app.py's own
# DATABASE_URL/LOOKUP_DB_ENABLED check -- there is no "auth disabled"
# fallback once this module is wired in.
if not (DEPLOYMENT_ID and KEYCLOAK_ISSUER_URL and KEYCLOAK_AUDIENCE):
    raise RuntimeError(
        "DEPLOYMENT_ID, KEYCLOAK_ISSUER_URL, and KEYCLOAK_AUDIENCE are all "
        "required -- every /api/* route requires an authenticated Keycloak "
        "user. See AUTH_MULTITENANCY_PLAN.md §5.1 for what these mean and "
        "KEYCLOAK_SETUP.md for how to set them."
    )

_DISCOVERY_CACHE_SECONDS = 3600
_jwks_client: jwt.PyJWKClient | None = None
_jwks_client_fetched_at = 0.0

# Cloudflare (and similar bot filters in front of Keycloak) often 403 the
# default Python/urllib User-Agent used by requests + PyJWKClient, while
# browsers and curl succeed. That shows up as every /api/* call failing with
# "Unable to verify token signature: ... HTTP Error 403" even though login
# itself worked. Send a normal browser UA on discovery + JWKS fetches only.
_OIDC_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; OS-Health-Check/1.0; "
        "+https://github.com/os-health-check) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# sub -> (user_id, cached_at); avoids a DB round trip on every single request
# -- last_login_at / username / email in iam_db still get refreshed on every
# cache miss, just not on every request within the TTL window.
_USER_ID_CACHE_SECONDS = 300
_user_id_cache: dict[str, tuple[str, float]] = {}

# Registers this deployment in iam.deployments lazily, on first real request
# rather than at import time -- importing this module (e.g. from a test)
# must not itself require a live Postgres connection.
_deployment_registered = False


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client, _jwks_client_fetched_at
    now = time.time()
    if _jwks_client is not None and now - _jwks_client_fetched_at < _DISCOVERY_CACHE_SECONDS:
        return _jwks_client
    # Fetched from KEYCLOAK_INTERNAL_URL (network reachability), not
    # KEYCLOAK_ISSUER_URL (identity) -- see the module-level comment on
    # KEYCLOAK_INTERNAL_URL. The response's own `jwks_uri` is expressed
    # relative to whichever URL we just used to reach it, so it's reachable
    # from here regardless of what the browser-facing issuer URL is.
    discovery_url = f"{KEYCLOAK_INTERNAL_URL}/.well-known/openid-configuration"
    response = requests.get(discovery_url, headers=_OIDC_HTTP_HEADERS, timeout=10)
    response.raise_for_status()
    jwks_uri = response.json()["jwks_uri"]
    _jwks_client = jwt.PyJWKClient(
        jwks_uri,
        lifespan=_DISCOVERY_CACHE_SECONDS,
        headers=_OIDC_HTTP_HEADERS,
    )
    _jwks_client_fetched_at = now
    return _jwks_client


@dataclass
class CurrentUser:
    deployment_id: str
    user_id: str
    keycloak_sub: str
    username: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)

    @property
    def is_publisher(self) -> bool:
        return PUBLISHER_ROLE in self.roles


class AuthError(HTTPException):
    def __init__(self, detail: str, status_code: int = 401):
        super().__init__(status_code=status_code, detail=detail)


def _extract_roles(claims: dict[str, object]) -> list[str]:
    """Realm roles (`realm_access.roles`) + this client's own roles
    (`resource_access.<KEYCLOAK_AUDIENCE>.roles`) -- covers both places
    Keycloak puts role assignments depending on how the role was granted."""
    roles: list[str] = []
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        roles.extend(str(r) for r in realm_access.get("roles", []) if isinstance(r, str))
    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        client_entry = resource_access.get(KEYCLOAK_AUDIENCE)
        if isinstance(client_entry, dict):
            roles.extend(str(r) for r in client_entry.get("roles", []) if isinstance(r, str))
    return roles


def _check_audience(claims: dict[str, object]) -> None:
    """Keycloak does NOT put the client id in `aud` by default for a public
    client -- only in `azp` (authorized party) -- unless an explicit
    "Audience" protocol mapper is added. Accepting either avoids the most
    common Keycloak-setup gotcha (a client that works in every other regard
    but every request 401s on audience) while still honoring an audience
    mapper if one is configured."""
    aud = claims.get("aud")
    aud_values = aud if isinstance(aud, list) else [aud] if aud else []
    azp = claims.get("azp")
    if KEYCLOAK_AUDIENCE not in aud_values and azp != KEYCLOAK_AUDIENCE:
        raise AuthError(
            "Token's audience/authorized-party does not match this deployment's "
            f"configured client ('{KEYCLOAK_AUDIENCE}')."
        )


def _decode_token(token: str) -> dict[str, object]:
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        raise AuthError(f"Unable to verify token signature: {exc}") from exc
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER_URL,
            options={"require": ["exp", "iat", "sub"], "verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired.") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError(
            "Token was not issued by this deployment's configured Keycloak realm."
        ) from exc
    except jwt.PyJWTError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc
    _check_audience(claims)
    return claims


def _resolve_user_id(sub: str, username: str, email: str) -> str:
    cached = _user_id_cache.get(sub)
    now = time.time()
    if cached is not None and now - cached[1] < _USER_ID_CACHE_SECONDS:
        return cached[0]
    user_id = iam_db.upsert_user(DEPLOYMENT_ID, sub, username=username, email=email)
    _user_id_cache[sub] = (user_id, now)
    return user_id


def authenticate_request(request: Request) -> CurrentUser:
    """Full validation path -- called once per request from app.py's
    require_authentication middleware (off the event loop, via
    asyncio.to_thread, since this does blocking network/DB calls)."""
    global _deployment_registered
    if not _deployment_registered:
        iam_db.get_or_create_deployment(DEPLOYMENT_ID, KEYCLOAK_ISSUER_URL)
        _deployment_registered = True

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthError("Missing bearer token. Log in and retry with an Authorization header.")
    token = header[len("Bearer "):].strip()
    if not token:
        raise AuthError("Missing bearer token.")

    claims = _decode_token(token)
    sub = str(claims.get("sub") or "")
    if not sub:
        raise AuthError("Token has no subject (sub) claim.")

    username = str(claims.get("preferred_username") or "")
    email = str(claims.get("email") or "")
    user_id = _resolve_user_id(sub, username, email)

    return CurrentUser(
        deployment_id=DEPLOYMENT_ID,
        user_id=user_id,
        keycloak_sub=sub,
        username=username,
        email=email,
        roles=_extract_roles(claims),
    )


def get_current_user(request: Request) -> CurrentUser:
    """FastAPI dependency for endpoints that need the actual identity (draft
    scoping, /api/auth/me). By the time any /api/* handler runs, the
    require_authentication middleware has already set request.state and
    would have 401'd otherwise -- this just exposes it with a real type."""
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise AuthError("Authentication required.")
    return current_user


def require_publisher(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not current_user.is_publisher:
        raise AuthError(
            f"Publishing requires the '{PUBLISHER_ROLE}' role, which this account does not have.",
            status_code=403,
        )
    return current_user
