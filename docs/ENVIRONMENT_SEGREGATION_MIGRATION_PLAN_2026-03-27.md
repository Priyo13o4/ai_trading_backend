# Environment Segregation Migration Plan

Date: 2026-03-27

## Purpose

This plan defines how to split the project into three real environments:

- `local`
- `dev`
- `production`

It is written against the current state of:

- `ai_trading_bot`
- `../ai-trading_frontend`
- the observed local Cloudflare tunnel config in `~/.cloudflared/config.yml`

No application code is changed by this document. This is the migration blueprint.

---

## Executive Summary

Your current setup works by routing real production hostnames to your laptop:

- `pipfactor.com -> localhost:3000`
- `www.pipfactor.com -> localhost:3000`
- `api.pipfactor.com -> localhost:8080`
- `sse.pipfactor.com -> localhost:8081`
- `mt5.pipfactor.com -> localhost:9001`
- `n8n.pipfactor.com -> localhost:5678`

That solved cookie/auth issues during development, but it created a bigger architectural problem:

- development is borrowing production hostnames
- backend/runtime defaults are production-first
- frontend env handling is production-first
- localhost is not a first-class environment
- production and development are now coupled by hostname, cookie scope, and tunnel routing

The right fix is not "make localhost behave like prod through the same tunnel forever."

The right fix is:

1. make `local` work natively on `localhost`
2. introduce a real `dev` environment with its own public URLs
3. move `production` onto a stable cloud deployment that no longer depends on your laptop

---

## What I Found

### Current frontend reality

- The frontend is documented as a single production-first app.
- `.env.example` and `README.md` tell you to keep `.env` pointed at production endpoints.
- Vite dev server explicitly allows `pipfactor.com` and `www.pipfactor.com`.
- API resolution includes hostname-based shortcuts that can send any `*.pipfactor.com` frontend to `https://api.pipfactor.com`.
- SSE uses credentialed requests and expects backend cookies to work across origins.
- Turnstile code has partial dev/prod support, but the repo still uses legacy single-key patterns in practice.

### Current backend reality

- `docker-compose.yml` forces `AUTH_ENV=production` and `TRUST_PROXY_HEADERS=1` for API services.
- The backend cookie/session model is solid, but it is being used with a production-shaped env contract.
- Cookie domain is driven by env and defaults to `.pipfactor.com`.
- CORS allows localhost and pipfactor domains, but REST and SSE each define this separately.
- External/public URLs are fragmented across `FRONTEND_URL`, `API_BASE_URL`, `N8N_BASE_URL`, `PLISIO_CALLBACK_URL`, and provider dashboard settings.

### Current infra reality

- One Compose stack runs Postgres, Redis variants, `api-web`, `api-sse`, `api-worker`, `n8n`, `n8n-worker`, `scraper`, and `news-analyzer`.
- Host ports are published directly for:
  - `5678`
  - `8080`
  - `8081`
  - `9001`
- Cloudflare tunnel config currently points real public production hostnames to local machine ports.
- Existing docs recognize the localhost cookie problem, but they still blur dev and prod in ways that should not be carried forward.

---

## Root Problem

The root problem is not just cookie behavior.

The real problem is that one environment is pretending to be all environments.

That shows up in four places:

1. local development uses production hostnames
2. frontend env selection is mostly production-first
3. backend env selection is mostly production-first
4. the tunnel is acting as both dev ingress and production ingress

That combination creates:

- cookie confusion
- accidental production coupling
- misleading test confidence
- unsafe proxy-header trust assumptions
- hard-to-debug auth drift

---

## Non-Negotiable Architecture Rules

These are the rules the migration should follow.

### Rule 1: Local must be truly local

Local development must work without Cloudflare Tunnel.

Local means:

- frontend on `localhost`
- API on `localhost`
- SSE on `localhost`
- host-only cookies
- insecure cookies allowed because this is HTTP

If auth only works when the laptop is pretending to be production, local is not real.

### Rule 2: One environment must not silently hit another

The frontend must never infer production URLs from hostname shortcuts once env split begins.

Environment must be explicit:

- local frontend -> local backend
- dev frontend -> dev backend
- prod frontend -> prod backend

No cross-environment defaults.

### Rule 3: Cookie scope must be isolated

Production and development must not share the same effective cookie scope unless cookie names also differ.

This matters because the project uses:

- session cookies
- CSRF cookies
- credentialed fetch
- cookie-backed SSE auth

### Rule 4: Public callback URLs are environment-scoped

Anything external must point to the right environment only, or be explicitly documented as production-only by policy:

- payment webhooks
- referral links
- email/auth callback URLs
- n8n base URLs

### Rule 5: If `TRUST_PROXY_HEADERS=1`, direct service exposure must be blocked

Do not trust proxy headers while also leaving `8080` and `8081` openly reachable from the internet.

---

## Target Architecture

## Recommended Target

### Local

- Frontend: `http://localhost:3000`
- API: `http://localhost:8080`
- SSE: `http://localhost:8081`
- N8N: optional local access only
- Cookie domain: none
- Cookie names: existing names are fine for local
- Cookie secure: off
- Tunnel: none
- Turnstile: disabled or test key
- Billing webhooks: not owned by local

### Dev

Canonical design for this project:

- use same-apex dev subdomains under `pipfactor.com`
- example:
   - `dev.pipfactor.com`
   - `api-dev.pipfactor.com`
   - `sse-dev.pipfactor.com`
   - `n8n-dev.pipfactor.com`
- Cookie domain: `.dev.pipfactor.com`
- Cookie names:
   - `SESSION_COOKIE_NAME=dev_session`
   - `CSRF_COOKIE_NAME=dev_csrf_token`
- Cookie secure: on
- Tunnel: separate dev tunnel
- Turnstile: dev widget and dev secret
- Billing: production credentials/callbacks may remain production-only if you intentionally avoid sandbox split

### Production

- Frontend: `https://pipfactor.com`
- Frontend alias: `https://www.pipfactor.com`
- API: `https://api.pipfactor.com`
- SSE: `https://sse.pipfactor.com` initially
- N8N: `https://n8n.pipfactor.com`
- Cookie domain: `.pipfactor.com`
- Cookie secure: on
- Tunnel: separate production tunnel
- Turnstile: production widget and production secret
- Billing: real callbacks only

---

## Why Same-Apex Dev Is Chosen Here

For this project, the most practical operator model is same-apex dev under `*.dev.pipfactor.com`.

This avoids managing a second zone while still keeping dev and prod logically separated.

Mandatory safeguard for same-apex dev:

- dev and prod cookie names must differ
   - prod: `session`, `csrf_token`
   - dev: `dev_session`, `dev_csrf_token`

Without that split, cookie collision and CSRF ambiguity can occur under `.pipfactor.com`.

---

## Optional Future Alternative: Separate Dev Apex

If you later want stricter isolation, move dev to a separate apex:

- `app.pipfactor-dev.com`
- `api.pipfactor-dev.com`
- `sse.pipfactor-dev.com`
- `n8n.pipfactor-dev.com`

Separate apex reduces cookie-collision risk but increases DNS and operational overhead.

1. make local work first
2. deploy production to the cloud server
3. stop routing production hostnames to your laptop
4. add remote dev afterward under `*.dev.pipfactor.com`

This avoids trying to redesign local, dev, and prod at the same time.

---

## Environment Contract

Each environment needs a full, explicit contract.

## Shared variables

These should exist in env bundles for every environment:

- `APP_ENV`
- `API_BASE_URL`
- `FRONTEND_URL`
- `N8N_BASE_URL`
- `SESSION_COOKIE_NAME`
- `CSRF_COOKIE_NAME`
- `COOKIE_DOMAIN`
- `COOKIE_SECURE`
- `COOKIE_SAMESITE`
- `TRUST_PROXY_HEADERS`
- `ALLOWED_ORIGINS`
- `ALLOWED_ORIGIN_REGEX`
- `TURNSTILE_SECRET_KEY`
- `SUPABASE_URL`
- `SUPABASE_PROJECT_URL`

## Frontend variables

The frontend should eventually use env values as the single source of truth for:

- `VITE_ENV_NAME`
- `VITE_PUBLIC_APP_URL`
- `VITE_PUBLIC_SITE_URL`
- `VITE_API_BASE_URL`
- `VITE_API_SSE_URL`
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_PUBLISHABLE_KEY`
- `VITE_TURNSTILE_SITE_KEY_DEV`
- `VITE_TURNSTILE_SITE_KEY_PROD`
- optional future:
  - `VITE_SESSION_COOKIE_NAME`
  - `VITE_CSRF_COOKIE_NAME`

## Backend rules by environment

### Local

- `APP_ENV=local`
- `COOKIE_DOMAIN=` blank
- `COOKIE_SECURE=0`
- `COOKIE_SAMESITE=lax`
- `TRUST_PROXY_HEADERS=0`
- allowed origins limited to localhost variants
- no Cloudflare dependency

### Dev

- `APP_ENV=dev`
- `COOKIE_DOMAIN` set to dev-only domain
- `SESSION_COOKIE_NAME=dev_session`
- `CSRF_COOKIE_NAME=dev_csrf_token`
- `COOKIE_SECURE=1`
- `COOKIE_SAMESITE=lax`
- `TRUST_PROXY_HEADERS=1`
- allowed origins limited to dev frontend URLs
- provider callbacks can remain production-only if sandbox split is intentionally not adopted

### Production

- `APP_ENV=production`
- `COOKIE_DOMAIN=.pipfactor.com`
- `COOKIE_SECURE=1`
- `COOKIE_SAMESITE=lax`
- `TRUST_PROXY_HEADERS=1`
- allowed origins limited to production frontend URLs
- provider callbacks point to prod API only

## Single-Operator External Integration Policy

This project may intentionally keep some third-party values production-only across environments.

Allowed, with explicit guardrails:

- Supabase project URL/keys can remain production if you are not splitting Supabase projects
- Razorpay and Plisio live callbacks stay production-only

Clarification for Supabase in this model:

- production-only Supabase means one shared project/credential set
- callback/redirect allowlists can still include `localhost`, `dev.pipfactor.com`, and production hosts

Required guardrails when following this policy:

- local and dev must never route production hostnames through the laptop
- local and dev are not webhook owners for live payment callbacks
- local and dev billing flows should be treated as non-authoritative unless sandbox mode is explicitly introduced

---

## What Will Change By Layer

## Frontend scope

The frontend work after this planning phase should include:

1. replace production-first `.env` usage with explicit local/dev/prod files
2. remove hostname-based production API overrides
3. update Vite `allowedHosts` for real dev hosts
4. make CSP env-aware for API and SSE hosts
5. move deep-link/universal-link host config into env-aware config
6. align Turnstile with real local/dev/prod hostnames
7. make session and CSRF cookie names environment-aware for same-apex dev (`dev_session`, `dev_csrf_token`)

## Backend scope

The backend work after this planning phase should include:

1. stop hardcoding production-like auth settings in Compose
2. standardize on one canonical app environment variable
3. split env bundles into local/dev/prod
4. centralize CORS config so `api-web` and `api-sse` cannot drift
5. keep localhost host-only cookie behavior
6. make public URLs explicit per environment
7. keep provider callback ownership explicit; if production-only, local/dev must not claim those callbacks
8. enforce environment-specific cookie names for same-apex dev
9. review proxy-header trust so it is enabled only behind controlled ingress

## Infra scope

The infra work after this planning phase should include:

1. stop using production hostnames as your dev laptop ingress
2. create separate tunnel configs for dev and production
3. bind server services to loopback or keep them internal-only
4. remove unnecessary public port exposure
5. deploy a real production frontend artifact to the cloud server
6. decide whether `sse.*` remains separate or is collapsed later behind API ingress

---

## Phased Migration Plan

## Phase 0: Freeze The Current Shape

Objective:

- document the current topology before changing it

Actions:

1. keep a snapshot of:
   - current Cloudflare DNS records
   - current local `~/.cloudflared/config.yml`
   - current `.env`
   - current provider webhook URLs
2. list the public hosts currently in use:
   - `pipfactor.com`
   - `www.pipfactor.com`
   - `api.pipfactor.com`
   - `sse.pipfactor.com`
   - `n8n.pipfactor.com`
   - `mt5.pipfactor.com`
3. note which third-party systems currently point to production:
   - Razorpay
   - Plisio
   - Supabase auth callbacks
   - Turnstile widget hostnames

Exit criteria:

- you have a rollback reference for every hostname and callback

## Phase 1: Make Local Real

Objective:

- make auth work on localhost without Cloudflare

Target local topology:

- frontend: `http://localhost:3000`
- API: `http://localhost:8080`
- SSE: `http://localhost:8081`

Actions:

1. create local env bundles for frontend and backend
2. ensure local backend uses:
   - blank `COOKIE_DOMAIN`
   - `COOKIE_SECURE=0`
   - `TRUST_PROXY_HEADERS=0`
3. ensure frontend local env points to local API/SSE only
4. make Vite local run path independent of public domains
5. keep Turnstile disabled or use the test key locally
6. confirm login, session refresh, logout, and CSRF-protected writes all work on localhost

Why this phase comes first:

- it removes your current dependency on production hostnames for normal development
- it gives you a reliable baseline before any cloud deployment work

Exit criteria:

- login works from `localhost`
- `Set-Cookie` is host-only
- protected routes work without the tunnel
- SSE works locally

## Phase 2: Move Production Off The Laptop

Objective:

- production must run from a stable cloud server, not from the local tunnel on your machine

Actions:

1. deploy frontend static artifact to the cloud server
2. deploy backend stack to the cloud server
3. run production tunnel from the cloud server
4. update production tunnel ingress so production hostnames point to server-local services
5. remove production hostname forwarding to laptop ports
6. enforce network hardening before go-live:
   - bind `8080`, `8081`, `5678`, and `9001` (if used) to loopback-only
   - ensure cloud firewall/security group denies public direct access to those ports
7. verify:
   - cookies
   - auth
   - SSE
   - payment webhooks
   - n8n

Important:

- this is the moment where production stops depending on whether your laptop is awake

Exit criteria:

- `pipfactor.com` and `api.pipfactor.com` are served by the cloud server
- production stays alive when your laptop is offline

## Phase 3: Introduce A Real Dev Environment

Objective:

- create a public non-production environment for realistic browser and Turnstile testing
- payment execution tests are sandbox-only; if sandbox is not adopted, treat local/dev billing checks as non-authoritative

Target for this project:

- frontend: `https://dev.pipfactor.com`
- API: `https://api-dev.pipfactor.com`
- SSE: `https://sse-dev.pipfactor.com`
- N8N: `https://n8n-dev.pipfactor.com`

Actions:

1. create dev subdomains under the existing `pipfactor.com` zone
2. create a separate Cloudflare tunnel for dev
3. create dev env bundles for frontend and backend
4. point dev frontend only to dev backend
5. create a dev Turnstile widget bound to dev hosts
6. add dev callback URLs in Supabase if required by the same production Supabase project
7. set dev-only cookie names:
   - `SESSION_COOKIE_NAME=dev_session`
   - `CSRF_COOKIE_NAME=dev_csrf_token`
8. keep Razorpay/Plisio live callbacks pointed to production unless sandbox mode is explicitly adopted
9. validate that prod and dev cookies do not collide

Exit criteria:

- dev login works on public HTTPS hosts
- dev cookies are isolated from prod
- dev frontend never calls prod API
- dev SSE works

## Phase 4: Post-Cutover Hardening

Objective:

- reduce attack surface and simplify operations

Note:

- loopback binding and direct-port blocking are Phase 2 cutover requirements
- Phase 4 re-validates and tightens those controls

Actions:

1. bind backend ports to loopback only on the cloud server
2. keep `TRUST_PROXY_HEADERS=1` only when direct exposure is blocked
3. restrict or remove direct exposure for:
   - `8080`
   - `8081`
   - `5678`
   - `9001` (default: internal-only; only expose if an explicit external requirement is validated)
4. review WAF rules for payment providers
5. optionally collapse public SSE hostname later if you want to reduce DNS/tunnel/CSP complexity

Exit criteria:

- only intended ingress paths remain public
- no direct internet path bypasses Cloudflare/Tunnel unexpectedly

---

## Recommended File Layout For The Future

This is the file layout I would use when you implement the plan.

### Backend

- `.env.local`
- `.env.dev`
- `.env.prod`
- `docker-compose.yml`
- `docker-compose.local.yml`
- `docker-compose.dev.yml`
- `docker-compose.prod.yml`

### Frontend

- `.env.local`
- `.env.dev`
- `.env.production`

This keeps environment selection explicit and avoids one giant shared `.env`.

---

## DNS and Hostname Matrix

## Production

- `pipfactor.com`
- `www.pipfactor.com`
- `api.pipfactor.com`
- `sse.pipfactor.com`
- `n8n.pipfactor.com`
- optional:
  - `mt5.pipfactor.com`

## Dev (Canonical For This Project)

- `dev.pipfactor.com`
- `api-dev.pipfactor.com`
- `sse-dev.pipfactor.com`
- `n8n-dev.pipfactor.com`

Use dev-only cookie names with this topology:

- `SESSION_COOKIE_NAME=dev_session`
- `CSRF_COOKIE_NAME=dev_csrf_token`

## Alternative Dev (Separate Apex)

- `app.pipfactor-dev.com`
- `api.pipfactor-dev.com`
- `sse.pipfactor-dev.com`
- `n8n.pipfactor-dev.com`

---

## What Not To Do

Do not keep any of these patterns after the migration:

- production apex routed to local Vite dev server
- local development depending on `pipfactor.com`
- dev frontend talking to prod API
- one `.env` pretending to be all environments
- `TRUST_PROXY_HEADERS=1` on directly internet-exposed ports
- shared cookie scope between dev and prod without cookie-name isolation

---

## Risk Register

### Risk 1: Dev accidentally talks to production

Cause:

- hostname shortcuts in frontend API resolution

Impact:

- misleading testing
- accidental writes to production
- cookie confusion

Mitigation:

- env-driven API/SSE URLs only

### Risk 2: Dev and prod cookies collide

Cause:

- same apex + same cookie names

Impact:

- broken auth
- unpredictable CSRF behavior
- hard-to-reproduce browser issues

Mitigation:

- for same-apex dev, use different cookie names (`dev_session`, `dev_csrf_token`)
- never run dev with production cookie names

### Risk 3: Proxy trust is exploitable

Cause:

- trusting forwarded headers on directly exposed ports

Impact:

- bad IP attribution
- rate-limit bypass risk
- scheme confusion

Mitigation:

- loopback-only binding
- firewall enforcement
- tunnel-only public ingress

### Risk 4: Turnstile breaks during migration

Cause:

- hostnames change but widget keys do not

Impact:

- login/signup failures that look like backend bugs

Mitigation:

- separate local/dev/prod Turnstile planning

### Risk 5: Payment callbacks point to wrong environment

Cause:

- single-value callback envs and dashboard config drift

Impact:

- missed webhooks
- false billing failures

Mitigation:

- explicit environment-by-environment callback inventory

---

## Validation Checklist

Run this checklist for each environment before you consider it ready.

### Browser/auth checks

- login works
- page refresh keeps session
- logout clears session
- protected API routes work
- CSRF-protected POST/PATCH/DELETE works
- SSE connects and stays authenticated

### Cookie checks

- session cookie name is correct
- CSRF cookie name is correct
- cookie domain is correct
- `Secure` flag matches environment
- `SameSite` is correct
- no stray duplicate cookies remain from another environment

### Header checks

- `Access-Control-Allow-Origin` matches expected frontend
- `Access-Control-Allow-Credentials` is present
- `x-forwarded-proto` handling matches deployment topology

### External integration checks

- Supabase redirect URLs are correct
- Turnstile widget loads and validates
- payment webhook endpoints are correct
- referral links point to the right frontend
- n8n public URL is correct
- if Supabase/Razorpay/Plisio are intentionally production-only, verify local/dev do not receive their live callbacks

### Network hardening checks

- verify ports `8080`, `8081`, `5678`, and `9001` (if present) are loopback-only on host
- verify cloud firewall/security group does not allow public direct ingress to those ports

---

## Final Recommendation

The most practical, scalable, non-overengineered path is:

1. make `localhost` the real local environment
2. deploy production to a cloud server immediately after that
3. stop using production hostnames as your dev ingress
4. create a true public `dev` environment after production is stable
5. use same-apex dev under `*.dev.pipfactor.com` with dev-specific cookie names

If you follow that order, you fix the real problem without trying to redesign the whole platform in one risky cutover.

If production hostnames are still routed through a laptop, prioritize completing Phase 1 quickly and then execute Phase 2 before Phase 3.
