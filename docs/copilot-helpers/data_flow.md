# AgNext S3 Download Manager — Application Data Flow

## Architecture Overview

This is a **single-page application (SPA)** with a FastAPI backend. There is only **one URL**
in the entire app: `/`. All "navigation" (Login → Browse → Download) is purely **in-page DOM
toggling** via `goToStep(n)` — the browser URL never changes after login. The only real
browser-level redirects are to/from Keycloak.

    Browser (index.html)
        │
        ├── FastAPI (app.py)           ← serves HTML, handles auth & S3
        │       ├── web_api_handle.py  ← Keycloak + Gateway API calls
        │       └── config.INI         ← all secrets / URLs
        │
        └── Keycloak SSO (external)    ← identity provider
                └── CentralIAM realm

---

## Core Components

| Component              | File                     | Responsibility                                        |
|------------------------|--------------------------|-------------------------------------------------------|
| `init()` IIFE          | `index.html` line ~596   | Runs on page load — decides login vs. dashboard       |
| `goToStep(n)`          | `index.html` line ~258   | The ONLY "redirect" inside the app — shows/hides DOM  |
| `_get_session()`       | `app.py` line ~264       | Validates JWT on every API request                    |
| `_populate_user_cache()` | `app.py` line ~302     | Resolves S3 folder via Gateway APIs, caches per user  |
| `exchange_keycloak_code()` | `web_api_handle.py` ~68 | Server-side code → token exchange with Keycloak    |
| `verify_keycloak_token()` | `web_api_handle.py` ~95 | JWKS-based JWT signature + expiry validation        |
| `user_cache` dict      | `app.py` line ~78        | In-memory session store: `{ sub → S3 config }`        |

---

## What Decides the Redirect Back to `/`?

**Keycloak** is what redirects back to `/`. Here is the exact chain:

`_kcAuthUrl()` (index.html ~240) builds:

    https://<KC_SERVER>/realms/CentralIAM/protocol/openid-connect/auth
      ?client_id=agnext-download-tool
      &redirect_uri=http://127.0.0.1:8080/    ← KC_REDIRECT
      &response_type=code
      &scope=openid

`KC_REDIRECT` is set at page-load time (index.html line ~228):

    let KC_REDIRECT = window.location.origin + window.location.pathname;
    // → "http://127.0.0.1:8080/"

After the user authenticates, Keycloak redirects the browser to:

    http://127.0.0.1:8080/?code=<auth_code>&session_state=<...>

That hits FastAPI `GET /` → serves `index.html` again → `init()` runs again →
detects `?code=` → proceeds to token exchange.

---

## Full Authentication Flow (Fresh Login)

    User opens http://127.0.0.1:8080/
            │
            ▼
    [Browser] GET /
            │
            ▼
    [FastAPI] GET / → returns index.html  (Cache-Control: no-store)
            │
            ▼
    [Browser] index.html loads → init() auto-runs
            │
            ▼
    Step 1: fetch('/api/kc-config')
            │   ← { url, realm, clientId }
            ▼
    Step 2: localStorage.getItem('kc_access_token')
            │
            ├── Token EXISTS → goToStep(2)   ← dashboard immediately, no redirect
            │
            └── No token + no ?code in URL
                    │
                    ▼
            sessionStorage.setItem('kc_redirect_ts', Date.now())  ← loop guard
            window.location.href = _kcAuthUrl()
                    │
                    ▼
            [BROWSER LEAVES → Keycloak login page]
                    │
                    ▼
            User enters credentials
                    │
                    ▼
            Keycloak redirects back to:
            http://127.0.0.1:8080/?code=ABC123&session_state=XYZ
                    │
                    ▼
    [FastAPI] GET / → returns index.html again
                    │
                    ▼
    [Browser] init() runs again
                    │
                    ▼
    Step 2: code = urlParams.get('code') → "ABC123"  ✓
                    │
                    ▼
    Step 3: POST /api/token-exchange  { code: "ABC123", redirect_uri: KC_REDIRECT }
            │
            ▼
    [FastAPI /api/token-exchange]
        → exchange_keycloak_code(code, redirect_uri)   [web_api_handle.py]
        → POST to Keycloak /token endpoint  (server-to-server, no browser)
        ← { access_token, refresh_token, expires_in, ... }
            │
            ▼
    [Browser] access_token received
        localStorage.setItem('kc_access_token', token)
        window.history.replaceState({}, '', '/')    ← strips ?code= from URL
        goToStep(2)                                 ← shows dashboard  ✓

---

## Session-Based Redirect Logic (Returning User / Page Refresh)

    User refreshes or re-opens http://127.0.0.1:8080/
            │
            ▼
    init() → checks localStorage
            │
            ├── 'kc_access_token' EXISTS
            │       └── goToStep(2)   ← dashboard instantly, zero network calls
            │
            └── 'kc_access_token' MISSING
                    └── → Keycloak redirect (same as fresh login flow above)

> **Important:** Token lives in `localStorage` — it survives page refreshes, new tabs,
> and browser restarts. It is only cleared by `logout()` or manual browser storage wipe.
> There is **no client-side expiry check** — an expired JWT only fails when it reaches
> a protected backend endpoint like `/api/files`.

---

## Backend Session Validation (Every Protected API Call)

Every call to `/api/files`, `/api/download/prepare`, `/api/download/get` goes through:

    Request: Authorization: Bearer <JWT>
            │
            ▼
    get_current_user(request)   [FastAPI Depends() — app.py ~293]
            │
            ▼
    _get_session(request)   [app.py ~264]
        ├── Extract Bearer token from header
        ├── verify_keycloak_token(token)   [web_api_handle.py ~95]
        │       ├── PyJWKClient fetches JWKS from Keycloak (cached)
        │       ├── Validate RS256 signature
        │       └── Validate exp + 30s clock-skew leeway
        │
        ├── sub NOT in user_cache   ← first call after server restart / new user
        │       │
        │       └── _populate_user_cache(sub, token, claims)   [app.py ~302]
        │               ├── fetch_user_profile_via_gateway(token)
        │               │       └── → customer_id, customer_name
        │               ├── fetch_client_meta_via_gateway(token, customer_id)
        │               │       └── → s3_folder
        │               │   fallback: fetch_s3_client_via_gateway(token, customer_name)
        │               │   fallback: config.INI [S3] client value
        │               └── user_cache[sub] = { id, name, bucket, folder, region, pool, type }
        │               └── logger "[LOGIN] Client successfully logged in: username=..."
        │
        └── sub IN user_cache   ← all subsequent calls this server session
                └── return user_cache[sub]   ← instant, no network

---

## Why `[LOGIN]` Log Appears on First API Call, Not at `goToStep(2)`

`goToStep(2)` only toggles CSS classes — it makes **zero backend calls**. The `[LOGIN]` log
lives in `_get_session()` which only runs when the frontend hits a protected endpoint:

1. `goToStep(2)` → dashboard visible  ← **no log yet**
2. User picks dates → `GET /api/files` called → `_get_session()` runs → **`[LOGIN]` fires here**

---

## Logout Flow

    logout() — index.html ~295
            │
            ├── POST /api/logout  { Authorization: Bearer <token> }
            │       └── [Backend] user_cache.pop(sub)
            │
            ├── localStorage.removeItem('kc_access_token')
            ├── sessionStorage.removeItem('kc_redirect_ts')
            │
            └── window.location.href = _kcLogoutUrl()
                    → Keycloak /logout?post_logout_redirect_uri=http://127.0.0.1:8080/
                            └── Keycloak ends SSO session → redirects to /
                                    └── init() → no token, no code → KC login redirect

---

## API Endpoints Summary

| Method | Path                      | Auth | Purpose                              |
|--------|---------------------------|------|--------------------------------------|
| GET    | `/`                       | No   | Serves `index.html`                  |
| GET    | `/api/kc-config`          | No   | Keycloak config for frontend         |
| POST   | `/api/token-exchange`     | No   | Exchanges auth code for JWT          |
| GET    | `/api/files`              | Yes  | Lists S3 sample folders by date      |
| GET    | `/api/dc-expand`          | Yes  | Expands a data_collection subfolder  |
| GET    | `/api/download/prepare`   | Yes  | Packages ZIP, streams SSE progress   |
| GET    | `/api/download/get`       | Yes  | Serves prepared ZIP by token         |
| POST   | `/api/logout`             | No   | Clears server-side user cache        |

---

## Diagnosing "Redirects Back to `/` After Login"

Check browser **DevTools → Console** for `[AUTH]` log lines. Find where it stops:

| Console output stops at / shows                         | Root Cause                                                                 |
|---------------------------------------------------------|----------------------------------------------------------------------------|
| `Token exchange successful` → then nothing              | `goToStep(2)` threw a JS error — look for red errors after that line       |
| `/api/token-exchange response status: 400`              | `redirect_uri` mismatch — KC_REDIRECT must exactly match Keycloak admin    |
| `/api/token-exchange response status: 401`              | Code already consumed (reloaded page), or Keycloak client secret wrong     |
| `Redirect loop detected`                                | Page reloaded within 5s of KC redirect — token exchange never completed    |
| `Step 2: savedToken present= false, code= false`        | Previous exchange failed silently — token never saved to localStorage      |
| No `[AUTH]` logs at all after returning from Keycloak   | `/api/kc-config` failing — backend not running or wrong port               |

**Most common root cause:** The `redirect_uri` value (`http://127.0.0.1:8080/`) must
**exactly** match a URI in `Valid Redirect URIs` inside the Keycloak client config for
`agnext-download-tool`. A trailing-slash mismatch causes Keycloak to silently reject the
token exchange with HTTP 400.