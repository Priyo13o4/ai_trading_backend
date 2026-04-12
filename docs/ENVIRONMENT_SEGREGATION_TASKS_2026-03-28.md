# Environment Segregation Implementation Tasks

Date: 2026-03-28
Source Plan: ENVIRONMENT_SEGREGATION_MIGRATION_PLAN_2026-03-27.md
Status: Phase 1 implementation complete; Phase 2 execution in progress

Caveat (2026-04-03): Tunnel split and ingress isolation are intentionally deferred until server cutover.
Current local/prod overlap in tunnel routing is temporary and accepted for pre-server rollout.

---

## 1) Decisions Locked Before Implementation

These decisions are applied to this task file (including your inline comments):

1. Dev topology will use same-apex fallback hosts for lower ops complexity:
   - `dev.pipfactor.com`
   - `api-dev.pipfactor.com`
   - `sse-dev.pipfactor.com`
   - `n8n-dev.pipfactor.com`
2. Because same-apex dev is selected, env-specific cookie names are mandatory:
   - production: `session`, `csrf_token`
   - dev: `dev_session`, `dev_csrf_token`
3. MT5 bridge path should be treated as internal-only unless proven otherwise:
   - bridge script defaults to localhost backend (`MT5_stuff/bridge_server.py`)
   - EA defaults to localhost bridge (`MT5_stuff/SmartStreamBinary.mq5`)
   - keep external exposure of port `9001` disabled by default in cloud

---

## 2) Scope Coverage Map (No Scope Missed)

- Frontend scope: env split, API/SSE URL resolution, Vite hosts/CSP, Turnstile keys, deep-link host config, optional cookie-name configurability (now required due same-apex dev).
- Backend scope: compose env contract, canonical env variable usage, CORS centralization for api-web and api-sse, cookie domain/secure/name behavior by env, callback URLs by env, proxy trust constraints.
- Infra scope: tunnel split, DNS split, production off laptop, loopback-only service bindings, direct port exposure removal, WAF review, SSE hostname simplification decision.
- External integrations: Supabase redirects, Turnstile widgets/secrets, Razorpay/Plisio webhooks, referral URL bases, n8n public URL separation.
- Validation scope: auth/session/csrf/sse/cors/cookies/headers/provider callbacks checklist per environment.

---

## 3) File Targets To Touch

### Backend (`ai_trading_bot`)

- `.env.example`
- New env bundles:
  - `.env.local`
  - `.env.dev`
  - `.env.prod`
- Compose files:
  - `docker-compose.yml` (base defaults cleanup)
  - `docker-compose.local.yml`
  - `docker-compose.dev.yml`
  - `docker-compose.prod.yml`
- API/Cookie/Auth/CORS:
  - `api-web/app/authn/routes.py`
  - `api-web/app/authn/session_store.py`
  - `api-web/app/main.py`
  - `api-web/app/authn/rate_limit_auth.py`
- SSE service CORS/auth parity (if split file differs):
  - `api-web/app/sse_main.py` (or equivalent SSE app entry)
- Start scripts (only if bind/headers behavior is environmentized):
  - `api-web/start.sh`
  - `api-web/sse_start.sh`
  - `api-worker/worker_start.sh`

### Frontend (`../ai-trading_frontend`)

- `.env.example`
- New env bundles:
  - `.env.local`
  - `.env.dev`
  - `.env.production`
- API/SSE resolution and host overrides:
  - `src/services/api.ts`
  - `src/services/sseService.ts`
- Type-safe env interface:
  - `src/vite-env.d.ts`
- Vite host/csp:
  - `vite.config.ts`
- Turnstile and env switching docs/scripts:
  - `README.md`
  - `switch-env.sh`

### Infra and runbooks

- Cloudflare tunnel configs (dev/prod split) and host ingress mapping docs
- Deployment runbooks:
  - `docs/CLOUD_SERVER_DEPLOYMENT_RUNBOOK_2026-03-27.md`
  - `docs/DEPLOYMENT_GUIDE.md` / `docs/PRODUCTION_DEPLOYMENT.md` (as applicable)

---

## 4) Environment Contract (Implementation Contract)

### Shared backend env keys (all envs)

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

### Shared frontend env keys (all envs)

- `VITE_ENV_NAME`
- `VITE_PUBLIC_APP_URL`
- `VITE_PUBLIC_SITE_URL`
- `VITE_API_BASE_URL`
- `VITE_API_SSE_URL`
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_PUBLISHABLE_KEY`
- `VITE_TURNSTILE_SITE_KEY_DEV`
- `VITE_TURNSTILE_SITE_KEY_PROD`
- required for same-apex dev:
  - `VITE_SESSION_COOKIE_NAME`
  - `VITE_CSRF_COOKIE_NAME`

### Required value profile by environment

1. Local
   - `APP_ENV=local`
   - cookie domain blank
   - secure cookie off
   - `TRUST_PROXY_HEADERS=0`
   - frontend points only to localhost API/SSE
2. Dev (`*.dev.pipfactor.com`)
   - `APP_ENV=dev`
   - cookie domain `.dev.pipfactor.com`
   - secure cookie on
   - `TRUST_PROXY_HEADERS=1` only behind tunnel/proxy
   - cookie names `dev_session` + `dev_csrf_token`
3. Production
   - `APP_ENV=production`
   - cookie domain `.pipfactor.com`
   - secure cookie on
   - `TRUST_PROXY_HEADERS=1` only behind tunnel/proxy
   - cookie names `session` + `csrf_token`

---

## 5) Execution Sequence

This sequence reflects your operational preference and reduces risk:

1. Phase 0: freeze topology and callback inventory
2. Phase 1: make localhost fully real and tunnel-independent
3. Phase 2: move production to cloud and remove laptop coupling
4. Phase 3: add remote dev on `*.dev.pipfactor.com` with cookie-name isolation
5. Phase 4: post-cutover hardening and exposure minimization

---

## 6) Master Task Checklist

## Phase 0 - Freeze Current State (Rollback Safety)

- [ ] Export current Cloudflare DNS records for all in-use hostnames.
- [ ] Snapshot current local tunnel config (`~/.cloudflared/config.yml`).
- [ ] Snapshot backend `.env` and frontend `.env` currently in use.
- [ ] Record provider callback destinations currently active:
  - Razorpay
  - Plisio
  - Supabase auth redirects
  - Turnstile hostname bindings
- [ ] Store snapshots in a dated folder under `docs/` or `backups/`.

Exit gate:
- [ ] Rollback references exist for every public hostname and external callback.

## Phase 1 - Local Becomes First-Class

### Backend tasks

- [x] Add `.env.local` and wire local-safe defaults:
  - blank `COOKIE_DOMAIN`
  - `COOKIE_SECURE=0`
  - `TRUST_PROXY_HEADERS=0`
  - local-only CORS allowlist and regex
- [x] Remove production-forced defaults from base compose (`AUTH_ENV=production` and forced proxy trust).
- [x] Standardize canonical env detection around `APP_ENV` first, fallback only for compatibility.
- [x] Ensure cookie-domain logic remains host-only for localhost and IP hosts.
- [x] Ensure rate-limit and auth IP extraction honor proxy trust only when enabled.
- [x] Align CORS behavior between REST and SSE entrypoints from one env contract.

### Frontend tasks

- [x] Add `.env.local` and point to `http://localhost:8080` + `http://localhost:8081`.
- [x] Remove hostname-based production API override logic from `src/services/api.ts`.
- [x] Keep SSE resolution explicit via `VITE_API_SSE_URL` and remove cross-env implicit behavior.
- [x] Update Vite `allowedHosts` so local does not depend on production hostnames.
- [x] Make CSP `connect-src` environment-aware (local must allow localhost only for local mode).
- [x] Keep Turnstile disabled locally or use test/site-dev key only.

### Verification tasks

- [ ] Login works from localhost.
- [ ] Session persists across refresh.
- [ ] Logout clears cookies.
- [ ] CSRF-protected writes succeed.
- [ ] SSE connects with credentialed auth.
- [ ] `Set-Cookie` is host-only on localhost.

Exit gate:
- [ ] Local development is tunnel-free and production hostname independent.

## Phase 2 - Production Cutover To Cloud Server

### Infra tasks

- [ ] Build and deploy frontend production artifact to cloud server.
- [ ] Deploy backend compose stack to cloud server with `.env.prod`.
- [ ] Run production tunnel on cloud server (not laptop).
- [ ] Update tunnel ingress so production hostnames target server-local services.
- [ ] Remove production hostname forwarding to laptop ports.

### Backend/ops tasks

- [ ] Ensure production services are bound loopback-only where possible.
- [ ] Ensure `TRUST_PROXY_HEADERS=1` only where direct exposure is blocked.
- [ ] Validate callback URLs are production-only for real providers.
- [ ] Validate n8n base/editor/webhook URLs are production-correct.

### Phase 2 command snippets (`.env.prod` + `docker-compose.prod.yml`)

```bash
# Build frontend artifact (run from ../ai-trading_frontend)
npm ci
npm run build
tar -czf pipfactor-frontend-dist.tgz dist

# Copy artifact to cloud server
scp pipfactor-frontend-dist.tgz <user>@<server>:/opt/pipfactor/frontend/

# Run backend stack on cloud server (run from ai_trading_bot)
docker compose --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.prod.yml up -d --build

# Verify backend services started with prod overlay
docker compose --env-file .env.prod \
  -f docker-compose.yml \
  -f docker-compose.prod.yml ps
```

### Verification tasks

- [ ] `pipfactor.com` and `api.pipfactor.com` are served by cloud server.
- [ ] Production works while laptop is offline.
- [ ] Auth + SSE + payment webhooks + n8n flows are healthy.

Exit gate:
- [ ] No production traffic path depends on local machine.

## Phase 3 - Remote Dev on Same Apex (`*.dev.pipfactor.com`)

### DNS + tunnel tasks

Interim decision (2026-04-03): Do not modify tunnel topology before cloud server cutover.
Keep existing tunnel behavior for now, and execute this block only during server migration.

- [ ] Create DNS for:
  - `dev.pipfactor.com`
  - `api-dev.pipfactor.com`
  - `sse-dev.pipfactor.com`
  - `n8n-dev.pipfactor.com`
- [ ] Create separate dev tunnel and isolated ingress mapping.
- [ ] Ensure no dev hostname shares ingress config with prod tunnel.

### Backend dev-env tasks

- [ ] Add `.env.dev` with:
  - `APP_ENV=dev`
  - `COOKIE_DOMAIN=.dev.pipfactor.com`
  - `COOKIE_SECURE=1`
  - `SESSION_COOKIE_NAME=dev_session`
  - `CSRF_COOKIE_NAME=dev_csrf_token`
  - dev-only `ALLOWED_ORIGINS` and `ALLOWED_ORIGIN_REGEX`
- [ ] Ensure cookie set/delete paths are compatible with renamed dev cookies.
- [ ] Ensure CSRF middleware reads env-driven CSRF cookie name everywhere.

### Frontend dev-env tasks

- [ ] Add `.env.dev` pointing only to dev API/SSE URLs.
- [ ] Add cookie-name env support in frontend request/csrf helpers:
  - `VITE_SESSION_COOKIE_NAME`
  - `VITE_CSRF_COOKIE_NAME`
- [ ] Remove any remaining production hostname inference paths.
- [ ] Update Vite/CSP for dev hosts and APIs.

### External integration tasks (dev)

- [ ] Create dev Turnstile widget + dev secret and bind to dev hosts.
- [ ] Add dev Supabase redirect/callback URLs.
- [ ] If billing sandbox is needed, set sandbox provider keys and dev webhook endpoints only.

### Verification tasks

- [ ] Dev frontend never calls prod API/SSE.
- [ ] Dev cookies do not collide with prod cookies.
- [ ] Dev auth + CSRF + SSE are stable on HTTPS.

Exit gate:
- [ ] Public dev is isolated from production by hostname, env, and cookie names.

## Phase 4 - Post-Cutover Hardening

### Exposure reduction tasks

- [ ] Restrict or remove direct internet exposure for `8080`, `8081`, `5678`, `9001`.
- [ ] Keep direct service access loopback/internal where tunnel is ingress.
- [ ] Enforce firewall rules to prevent bypass of Cloudflare tunnel path.

### MT5 tasks (from user comment)

- [ ] Confirm MT5 bridge topology in runtime docs:
  - EA (`SmartStreamBinary.mq5`) -> local bridge port `5005`
  - bridge (`bridge_server.py`) -> backend `127.0.0.1:9001`
- [ ] Keep `9001` internal-only by default in cloud deployment.
- [ ] Only expose `mt5.pipfactor.com` if external bridge is explicitly required and secured.

### Security and operations tasks

- [ ] Review and tighten Cloudflare WAF rules for payment webhook routes.
- [ ] Verify `TRUST_PROXY_HEADERS=1` is never used on directly exposed services.
- [ ] Decide whether to retain separate `sse.*` hostname or collapse under API ingress later.

Exit gate:
- [ ] Public exposure is intentional and minimized.

---

## 7) Cross-Repo Implementation Backlog (Concrete)

## Frontend backlog

- [ ] Convert environment strategy from single `.env` to `.env.local` / `.env.dev` / `.env.production`.
- [ ] Rewrite API base URL resolver to be env-only (no hostname shortcuts).
- [ ] Rewrite SSE base URL resolver to prefer explicit env and avoid prod fallback behavior.
- [ ] Make CSP connect-src generated from env values.
- [ ] Expand `vite-env.d.ts` with new env keys.
- [ ] Update frontend README and switch-env workflow for 3-env model.

## Backend backlog

- [ ] Replace compose hardcoded `AUTH_ENV=production` and default proxy-trust assumptions.
- [ ] Add three backend env files and compose overlays.
- [ ] Make `APP_ENV` canonical in auth runtime environment resolution.
- [ ] Unify CORS parsing and application between api-web and api-sse.
- [ ] Ensure provider callback base URLs are explicit per env.
- [ ] Ensure referral/email links use environment-specific frontend URL.

## Infra backlog

- [ ] Build separate tunnel definitions for dev and production.
- [ ] Ensure production tunnel runs only on cloud host.
- [ ] Ensure service ports are loopback-bound on server.
- [ ] Remove laptop ingress usage for production hostnames.

---

## 8) Validation Matrix (Run For Each Env)

### Browser/auth

- [ ] login
- [ ] refresh session continuity
- [ ] logout
- [ ] protected route access
- [ ] CSRF-protected POST/PATCH/DELETE
- [ ] authenticated SSE continuity

### Cookie correctness

- [ ] session cookie name
- [ ] csrf cookie name
- [ ] cookie domain
- [ ] `Secure`
- [ ] `SameSite`
- [ ] no duplicate stale cookies from another env

### Header/CORS

- [ ] `Access-Control-Allow-Origin`
- [ ] `Access-Control-Allow-Credentials`
- [ ] `x-forwarded-proto` handling vs proxy trust mode

### External integrations

- [ ] Supabase redirects/callbacks
- [ ] Turnstile widget validation
- [ ] payment webhook destinations/signatures
- [ ] referral links
- [ ] n8n public URL

---

## 9) Risks and Mitigations To Track During Execution

- [ ] Dev accidentally hitting production (mitigation: env-only URL resolution).
- [ ] Dev/prod cookie collision on same apex (mitigation: mandatory env-specific cookie names).
- [ ] Proxy-header spoofing risk (mitigation: tunnel-only ingress + loopback binds + firewall).
- [ ] Turnstile breakage after host migration (mitigation: dedicated dev/prod keys and host bindings).
- [ ] Payment callback drift (mitigation: explicit callback inventory and per-env cutover checklist).

---

## 10) Completion Definition

Environment segregation is complete only when all are true:

- [ ] Local works fully on localhost with no Cloudflare dependency.
- [ ] Production is cloud-hosted and laptop-independent.
- [ ] Dev is publicly reachable on `*.dev.pipfactor.com` and isolated from prod.
- [ ] Cookie domains and names are isolated as defined.
- [ ] No direct public bypass path exists for trusted-proxy services.
- [ ] All validation matrix checks pass in local/dev/prod.
