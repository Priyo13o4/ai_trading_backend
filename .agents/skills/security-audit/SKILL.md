---
name: ai-trading-security-audit
description: >
  Repo-specific security audit skill for the ai_trading_bot + ai-trading_frontend project.
  Covers the Supabase → backend cookie session → Redis auth stack, Cloudflare Turnstile
  wiring, FastAPI route protection, and environment parity between dev/local/prod.
  Use this skill before merging any auth-related PR or when debugging auth or Turnstile issues.
---

# AI Trading Bot — Security Audit Skill

## What This Skill Does

This skill guides AI agents and developers through a systematic security and auth audit of the `ai_trading_bot` (FastAPI backend) and `ai-trading_frontend` (React/Vite) project. It codifies the project's auth architecture and a 9-point checklist that must be verified for every auth-related change.

---

## Project Auth Architecture

```
User Browser
  │
  ├─ Supabase (Supabase JS SDK)
  │    - signIn / signUp → returns access_token (JWT)
  │    - stores session in localStorage/sessionStorage
  │
  ├─ Frontend (React, useAuth.tsx)
  │    - calls POST /auth/exchange with Supabase access_token + Turnstile token
  │    - backend sets HttpOnly session cookie (pipfactor-session) + CSRF cookie (pipfactor-csrf)
  │    - subsequent API calls send both cookies automatically
  │
  ├─ Cloudflare Turnstile (optional/enforced)
  │    - widget rendered in LoginDialog / SignUpDialog
  │    - token passed to Supabase signIn captchaToken AND to /auth/exchange
  │    - backend verifies token with Cloudflare API (only if TURNSTILE_SECRET_KEY set)
  │    - enforcement controlled by AUTH_EXCHANGE_TURNSTILE_ENFORCE flag
  │
  └─ Backend (FastAPI, api-web)
       - /auth/exchange: verifies Supabase JWT, verifies Turnstile, creates Redis session
       - /auth/validate: reads Redis session cookie → returns allowed, plan, permissions
       - /auth/me: reads Redis session → fetches full profile from Supabase Admin API
       - All protected endpoints: use _require_session() (authn/routes.py) for session lookup
         + device binding check + activity refresh
       - Session storage: Redis (redis-sessions container), separate from cache Redis
       - CSRF: double-submit cookie pattern via X-CSRF-Token header
```

---

## Environment Variable Matrix

| Variable | Dev/Local | Production |
|---|---|---|
| `AUTH_ENV` | `development` or `local` | `production` |
| `AUTH_EXCHANGE_TURNSTILE_ENFORCE` | `"0"` | `"1"` |
| `COOKIE_SECURE` | `"false"` | `"true"` (default) |
| `TURNSTILE_SECRET_KEY` | optional (test key) | required |
| `VITE_TURNSTILE_SITE_KEY_DEV` | `1x00000000000000000000AA` (CF always-pass) | — |
| `VITE_TURNSTILE_SITE_KEY_PROD` | — | real production site key |

> **Cloudflare always-pass test key for localhost dev:**
> Site key: `1x00000000000000000000AA`
> Secret key: `1x0000000000000000000000000000000AA` (or any test secret)

---

## 9-Point Security Audit Checklist

Run this checklist before merging any PR that touches auth, session, or Turnstile code.

### 1. Turnstile Enforcement (CRITICAL)

Check that `AUTH_EXCHANGE_TURNSTILE_ENFORCE` is:
- `"1"` in `docker-compose.yml` (production base)
- `"0"` in `docker-compose.dev.yml` and `docker-compose.local.yml`

Check that `TURNSTILE_SECRET_KEY` is set in production `.env`.

**Files to review:**
- `ai_trading_bot/docker-compose.yml` → `AUTH_EXCHANGE_TURNSTILE_ENFORCE`
- `ai_trading_bot/docker-compose.dev.yml` → must override to `"0"`
- `ai_trading_bot/docker-compose.local.yml` → must override to `"0"`
- `ai_trading_bot/api-web/app/authn/routes.py` → `_should_enforce_turnstile()`

### 2. Route Protection (HIGH)

Check that all protected routes in the frontend are wrapped in `<ProtectedRoute>`.

**Files to review:**
- `ai-trading_frontend/src/App.tsx` → ensure `/signal`, `/strategy`, `/news`, `/profile` are inside `<Route element={<ProtectedRoute />}>` blocks (no `TEMP` comments disabling the gate)
- `ai-trading_frontend/src/components/RequireAuth.tsx` → ensure the original auth logic is not commented out

### 3. Auth Dependency Path (HIGH)

All protected API endpoints must use `_require_session()` from `authn/routes.py` (or an equivalent wrapper exported from `authn/`) — not the legacy `auth_context` from `auth.py`.

**Files to review:**
- `ai_trading_bot/api-web/app/main.py` → verify every route with `Depends(auth_context)` has been migrated to `Depends(require_session)` or equivalent
- `ai_trading_bot/api-web/app/auth.py` → `optional_auth_context` should be removed (dead code)

Grep test: `grep -rn "Depends(auth_context)" api-web/` should return 0 results after migration.

### 4. Session Cookie Security (HIGH)

- `COOKIE_SECURE=true` in production (default).
- `COOKIE_SECURE=false` only in local/dev (localhost).
- `COOKIE_SAMESITE=lax` unless cross-site POST is required.
- Session cookie must be `HttpOnly`.

**Files to review:**
- `api-web/app/authn/routes.py` → `_set_cookie()`, `_should_use_secure_cookie()`

### 5. Silent Rehydrate / Session Recovery (HIGH)

When `turnstileEnabled && !captchaToken` in `hydrateSession`:
- The user must **NOT** be logged out silently if they have a valid Supabase session.
- They should stay `authenticated` and defer backend cookie exchange to the next interactive login.

**File to review:**
- `ai-trading_frontend/src/hooks/useAuth.tsx` → lines around `if (turnstileEnabled && !captchaToken)`

### 6. Janitor Worker Deduplication (MEDIUM)

The strategy expiry and session index prune janitors must start from exactly one worker in multi-worker deployments.

- Use Redis `SET NX` atomically (`REDIS.set("janitor:leader:lock", pid, nx=True, ex=interval)`) — not file-based lock detection.
- The `is_first` pattern using `/tmp/fastapi_startup.lock` is racy.

**File to review:**
- `api-web/app/main.py` → `startup_event()` janitor creation block

### 7. Internal API Key Consistency (MEDIUM)

All endpoints that accept `X-API-Key` must use `_require_internal_api_key()` with `hmac.compare_digest`. 

Grep test: `grep -n "X-API-Key" api-web/app/main.py` — every occurrence should flow through `_require_internal_api_key()`.

### 8. Turnstile Frontend Config (MEDIUM)

The Turnstile site key resolution must follow the three-key precedence:
1. `VITE_TURNSTILE_SITE_KEY_PROD` (production)
2. `VITE_TURNSTILE_SITE_KEY_DEV` (dev)
3. `VITE_TURNSTILE_SITE_KEY` (legacy fallback)

**Files to review:**
- `ai-trading_frontend/src/config/turnstile.ts`
- `ai-trading_frontend/README.md` → must document all three keys and the localhost dev test key

### 9. Dead Code Footprint (LOW)

> [!CAUTION]
> Axon uses static import-graph analysis and **does not track dynamic dispatch, module-load-time calls, or runtime string-based references**. Before removing any symbol Axon flags as dead, verify manually:
> - Is it called during module import/initialisation (not a runtime call)?
> - Is it referenced via `getattr`, `importlib`, or a string-based plugin pattern?
> - Is it exported as part of a public API consumed by another service?
> Only remove confirmed dead code. When in doubt, add a comment explaining why it's not dead rather than deleting it.

Run Axon dead-code scan before any release branch cut:

```bash
# Via Axon MCP (backend)
axon_dead_code (server: axon-ai_trading_backend)

# Via Axon MCP (frontend)
axon_dead_code (server: axon-ai_trading_frontend)
```

Focus on auth-relevant dead code:
- `api-web/app/auth.py` → `optional_auth_context` (remove)
- `api-web/app/authn/routes.py` → `_env_bool`, `_env_int`, `_env_cookie_samesite` are module-init calls, not dead — annotate accordingly
- `api-web/app/authn/routes.py` → `_is_development_environment` — verify if still needed after env config changes

---

## How to Run a Full Audit

1. **Run Axon dead-code scans** (see checklist point 9)
2. **Grep for disabled route guards:** `grep -n "TEMP\|ProtectedRoute\|RequireAuth" ai-trading_frontend/src/App.tsx ai-trading_frontend/src/components/RequireAuth.tsx`
3. **Check docker-compose env matrix** (see table above)
4. **Check Turnstile enforcement flag:** `grep -n "AUTH_EXCHANGE_TURNSTILE_ENFORCE" ai_trading_bot/docker-compose*.yml`
5. **Check legacy auth dependency:** `grep -rn "Depends(auth_context)" ai_trading_bot/api-web/`
6. **Verify silent rehydrate logic:** Read `useAuth.tsx` around `if (turnstileEnabled && !captchaToken)`
7. **Verify janitor worker init:** Read `main.py` `startup_event()` and check for atomic Redis lock

---

## Known Historical Issues (Audit 2026-03-18)

See full findings in: `ai_trading_bot/docs/SESSION_AUTH_SPEC_VERIFICATION_2026-03-16.md`

Summary of issues found and their fix status:

| ID | Issue | Fix Applied |
|----|-------|-------------|
| CRITICAL-01 | Turnstile enforcement disabled at runtime | ⬜ Pending |
| HIGH-01 | Frontend route protection bypassed | ⬜ Pending |
| HIGH-02 | Protected API uses legacy auth dependency | ⬜ Pending |
| HIGH-03 | Dev/local inherits production AUTH_ENV | ⬜ Pending |
| HIGH-04 | Silent rehydrate drops session | ⬜ Pending |
| HIGH-05 | Session index drift fallback scan | ⬜ Monitoring |
| MEDIUM-01 | Janitor multi-worker race | ⬜ Pending |
| MEDIUM-02 | API key verification inconsistent | ⬜ Pending |
| MEDIUM-03 | Turnstile config/docs drift | ⬜ Pending |
| MEDIUM-04 | Janitor observability | ⬜ Pending |
| LOW-01 | Misleading bypass log | ⬜ Pending |
| LOW-02 | Dead code footprint | ⬜ Pending |
| NEW-01 | api.ts auth methods dead per Axon | ⬜ Pending investigation |
| NEW-02 | File-lock janitor dedup racy | ⬜ Pending |

Update this table when fixes are applied.
