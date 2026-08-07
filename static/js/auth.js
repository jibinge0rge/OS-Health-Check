// Keycloak OIDC login for a server-rendered, no-build-step app -- there's no
// framework/bundler here (see ARCHITECTURE.md §2), so this hand-rolls
// Authorization Code + PKCE directly against Keycloak's standard endpoints
// rather than pulling in an external JS adapter. Works unchanged whether
// Keycloak is per-deployment or one realm shared by every deployment
// (AUTH_MULTITENANCY_PLAN.md §3.1) -- this only ever talks to whichever
// issuer/client this page was rendered with (window.__OSHC_AUTH__, set by
// index.html from the server's own KEYCLOAK_ISSUER_URL/KEYCLOAK_AUDIENCE).

const STORAGE_KEYS = {
  tokens: "oshc.auth.tokens",
  pkceVerifier: "oshc.auth.pkce_verifier",
  state: "oshc.auth.state",
};

// sessionStorage, not localStorage: cleared when the tab/window closes,
// so a stale token doesn't linger indefinitely on a shared machine. Still
// script-readable like localStorage (this is an internal admin tool, not
// a target rich enough to justify a full BFF/cookie-session redesign for
// v1) -- revisit if that trust assumption ever changes.
function loadTokens() {
  try {
    return JSON.parse(sessionStorage.getItem(STORAGE_KEYS.tokens) || "null");
  } catch (_err) {
    return null;
  }
}

function saveTokens(tokens) {
  sessionStorage.setItem(STORAGE_KEYS.tokens, JSON.stringify(tokens));
}

function clearTokens() {
  sessionStorage.removeItem(STORAGE_KEYS.tokens);
}

function base64UrlEncode(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(byteLength = 32) {
  return base64UrlEncode(crypto.getRandomValues(new Uint8Array(byteLength)));
}

async function pkceChallengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(digest);
}

let discoveryPromise = null;

function discover() {
  if (!discoveryPromise) {
    const issuer = window.__OSHC_AUTH__.issuerUrl;
    discoveryPromise = fetch(`${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`).then((response) => {
      if (!response.ok) throw new Error("Unable to reach Keycloak's OIDC discovery endpoint.");
      return response.json();
    });
  }
  return discoveryPromise;
}

function redirectUri() {
  return `${window.location.origin}${window.location.pathname}`;
}

export async function login() {
  const config = await discover();
  const verifier = randomString();
  const state = randomString(16);
  sessionStorage.setItem(STORAGE_KEYS.pkceVerifier, verifier);
  sessionStorage.setItem(STORAGE_KEYS.state, state);

  const params = new URLSearchParams({
    client_id: window.__OSHC_AUTH__.clientId,
    response_type: "code",
    redirect_uri: redirectUri(),
    scope: "openid profile email",
    state,
    code_challenge: await pkceChallengeFor(verifier),
    code_challenge_method: "S256",
  });
  window.location.assign(`${config.authorization_endpoint}?${params.toString()}`);
}

export async function logout() {
  const tokens = loadTokens();
  clearTokens();
  // Without id_token_hint, Keycloak can't confirm which session it's
  // ending and shows its own "are you sure?" page instead of logging out
  // immediately -- easy to mistake for "logout did nothing" if you don't
  // notice that second page. Should always be present after a real login;
  // surfaced here rather than silently proceeding without it.
  if (!tokens?.id_token) console.warn("logout(): no id_token on file -- Keycloak may ask for an extra confirmation.");
  try {
    const config = await discover();
    const params = new URLSearchParams({
      client_id: window.__OSHC_AUTH__.clientId,
      post_logout_redirect_uri: redirectUri(),
      ...(tokens?.id_token ? { id_token_hint: tokens.id_token } : {}),
    });
    window.location.assign(`${config.end_session_endpoint}?${params.toString()}`);
  } catch (err) {
    // Tokens are already cleared above regardless -- the next API call
    // will 401 and this same page's getValidAccessToken() will redirect to
    // login anyway, but that's a confusing silent delay. Force it now
    // instead of leaving the page looking unchanged.
    console.error("logout(): failed to reach Keycloak's discovery endpoint, reloading instead:", err);
    window.location.reload();
  }
}

function tokensFromResponse(payload) {
  return {
    access_token: payload.access_token,
    refresh_token: payload.refresh_token || null,
    id_token: payload.id_token || null,
    // expires_in is seconds-from-now per the token response; store an
    // absolute deadline so expiry checks don't depend on when this ran.
    // Refresh a bit early (30s) so a request never races an about-to-expire token.
    expires_at: Date.now() + (Number(payload.expires_in) || 60) * 1000 - 30_000,
  };
}

async function exchangeCodeForTokens(code, verifier) {
  const config = await discover();
  const response = await fetch(config.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: window.__OSHC_AUTH__.clientId,
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    }),
  });
  if (!response.ok) throw new Error("Keycloak rejected the login code — please try signing in again.");
  return tokensFromResponse(await response.json());
}

async function refreshTokens(refreshToken) {
  const config = await discover();
  const response = await fetch(config.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      client_id: window.__OSHC_AUTH__.clientId,
      refresh_token: refreshToken,
    }),
  });
  if (!response.ok) return null;
  return tokensFromResponse(await response.json());
}

// Called once, at app startup, before anything else renders. Resolves once
// a valid access token is available; otherwise navigates away to Keycloak
// (the promise then never needs to resolve -- the page is unloading).
export async function ensureAuthenticated() {
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");

  if (code) {
    const expectedState = sessionStorage.getItem(STORAGE_KEYS.state) || "";
    const verifier = sessionStorage.getItem(STORAGE_KEYS.pkceVerifier) || "";
    sessionStorage.removeItem(STORAGE_KEYS.pkceVerifier);
    sessionStorage.removeItem(STORAGE_KEYS.state);
    if (returnedState !== expectedState) {
      throw new Error("Login state mismatch — please try signing in again.");
    }
    const tokens = await exchangeCodeForTokens(code, verifier);
    saveTokens(tokens);
    stripOidcCallbackParams(url);
    return;
  }

  const tokens = loadTokens();
  if (tokens && tokens.expires_at > Date.now()) {
    stripOidcCallbackParams(url);
    return;
  }
  if (tokens?.refresh_token) {
    const refreshed = await refreshTokens(tokens.refresh_token);
    if (refreshed) {
      saveTokens(refreshed);
      stripOidcCallbackParams(url);
      return;
    }
  }
  await login();
}

function stripOidcCallbackParams(url) {
  const junk = ["code", "state", "session_state", "iss", "error", "error_description"];
  let dirty = false;
  for (const key of junk) {
    if (url.searchParams.has(key)) {
      url.searchParams.delete(key);
      dirty = true;
    }
  }
  if (dirty) {
    window.history.replaceState({}, "", `${url.pathname}${url.hash}`);
  }
}

// Returns a currently-valid access token, refreshing first if it's expired
// or about to be -- used by api.js on every request. Redirects to login
// (and never resolves) if there's no way to get a valid token anymore.
export async function getValidAccessToken() {
  const tokens = loadTokens();
  if (tokens && tokens.expires_at > Date.now()) return tokens.access_token;
  if (tokens?.refresh_token) {
    const refreshed = await refreshTokens(tokens.refresh_token);
    if (refreshed) {
      saveTokens(refreshed);
      return refreshed.access_token;
    }
  }
  await login();
  return new Promise(() => {}); // page is navigating away; never resolves
}
