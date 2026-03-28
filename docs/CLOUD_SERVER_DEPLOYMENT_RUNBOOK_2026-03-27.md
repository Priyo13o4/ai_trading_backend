# Cloud Server Deployment Runbook

Date: 2026-03-27

## Purpose

This runbook describes how to deploy the stack to a cloud server with proper separation between:

- local
- dev
- production

It focuses on:

- Cloudflare DNS
- Cloudflare Tunnel ingress rules
- ports
- Docker/container layout
- rollout order
- operational hardening

This document is intentionally concrete and based on the current stack:

- frontend on Vite during development
- backend services in Docker Compose
- separate `api-web` and `api-sse`
- current local tunnel routes for `pipfactor.com`, `api.pipfactor.com`, `sse.pipfactor.com`, `n8n.pipfactor.com`, and `mt5.pipfactor.com`

---

## Deployment Principle

Production must be served from the cloud server.

Your laptop should no longer be the runtime for:

- `pipfactor.com`
- `api.pipfactor.com`
- `sse.pipfactor.com`
- `n8n.pipfactor.com`

Local development remains local.

Remote dev, if you add it, should use either:

- a separate dev server
- or a second isolated Compose project on the same server with different loopback ports

---

## Recommended Server Model

## Best practical model

- one production server
- optional second dev server later
- Cloudflare Tunnel running on each server
- Docker Compose for application services
- loopback-only service bindings

This keeps the system simple and reduces cross-environment coupling.

## Acceptable minimum model

If you must host both dev and production on one server:

- use separate Compose projects
- use separate env files
- use different loopback ports
- use separate tunnels
- never share public hostnames

---

## Recommended Service Topology

## Production

Public hosts:

- `pipfactor.com`
- `www.pipfactor.com`
- `api.pipfactor.com`
- `sse.pipfactor.com`
- `n8n.pipfactor.com`
- optional:
  - `mt5.pipfactor.com`

Internal service ports on the server:

- frontend static server: `127.0.0.1:3000`
- `api-web`: `127.0.0.1:8080`
- `api-sse`: `127.0.0.1:8081`
- `n8n`: `127.0.0.1:5678`
- `api-worker` MT5 ingress:
  - `127.0.0.1:9001` if tunnelled only
  - or no host binding if not externally required

## Dev on a second server

Use the same internal ports as production if it is a separate machine.

Example public hosts:

- `app.pipfactor-dev.com`
- `api.pipfactor-dev.com`
- `sse.pipfactor-dev.com`
- `n8n.pipfactor-dev.com`

## Dev on the same server

Use different loopback ports.

Suggested dev ports:

- frontend: `127.0.0.1:4300`
- `api-web`: `127.0.0.1:48080`
- `api-sse`: `127.0.0.1:48081`
- `n8n`: `127.0.0.1:45678`
- MT5 if needed: `127.0.0.1:49001`

Why loopback-only:

- Cloudflare Tunnel can still reach these ports
- the public internet cannot bypass the tunnel directly
- `TRUST_PROXY_HEADERS=1` becomes much safer

---

## Container Strategy

## Base rule

Do not treat the Vite dev server as the production frontend.

Production frontend should be:

- built once
- served as a static artifact
- restarted like a normal server component

## Recommended runtime layout

### Production frontend

Options:

1. static build served by a small web server container
2. static build served by a lightweight host service

Recommended:

- static frontend artifact served on `127.0.0.1:3000`

### Production backend

Keep the existing split:

- `api-web`
- `api-sse`
- `api-worker`
- `postgres`
- `redis-queue`
- `redis-app`
- `redis-sessions`
- `n8n`
- `n8n-worker`
- `scraper`
- `news-analyzer`

This is already a reasonable scaling boundary.

### Cloudflared

Preferred:

- run `cloudflared` as a system service on the host

Why:

- simple credential handling
- easy system startup
- tunnel lifecycle independent of Compose restarts

Alternative:

- run `cloudflared` as a Docker service on the same Docker network

Only do that if you want everything fully containerized and you are comfortable managing tunnel credentials as mounted secrets.

---

## Cloudflare Tunnel Strategy

## Recommended

Use separate tunnels for dev and production.

### Production tunnel

- tunnel name: `pipfactor-prod`
- hostnames:
  - `pipfactor.com`
  - `www.pipfactor.com`
  - `api.pipfactor.com`
  - `sse.pipfactor.com`
  - `n8n.pipfactor.com`
  - optional `mt5.pipfactor.com`

### Dev tunnel

- tunnel name: `pipfactor-dev`
- hostnames:
  - `app.pipfactor-dev.com`
  - `api.pipfactor-dev.com`
  - `sse.pipfactor-dev.com`
  - `n8n.pipfactor-dev.com`

Why separate tunnels:

- cleaner blast-radius control
- easier rollback
- simpler audits
- less chance of routing production hostnames to dev ports by mistake

## Not preferred, but possible

One tunnel can serve both environments, but only if:

- hostnames are strictly separated
- config is carefully reviewed
- environment ports are distinct

This is still more error-prone than separate tunnels.

---

## DNS Plan

## Production DNS

Create or keep proxied CNAMEs pointing to the production tunnel target:

- `pipfactor.com`
- `www.pipfactor.com`
- `api.pipfactor.com`
- `sse.pipfactor.com`
- `n8n.pipfactor.com`
- optional `mt5.pipfactor.com`

## Preferred dev DNS

In the dev zone, create proxied CNAMEs pointing to the dev tunnel target:

- `app.pipfactor-dev.com`
- `api.pipfactor-dev.com`
- `sse.pipfactor-dev.com`
- `n8n.pipfactor-dev.com`

## Same-apex fallback dev DNS

If you stay under the production apex:

- `dev.pipfactor.com`
- `api.dev.pipfactor.com`
- `sse.dev.pipfactor.com`
- `n8n.dev.pipfactor.com`

Use this only if your cookie strategy also isolates dev from prod.

---

## Example Production Tunnel Config

This is the shape to run on the cloud server, not on your laptop.

```yaml
tunnel: <prod-tunnel-id>
credentials-file: /etc/cloudflared/<prod-tunnel-id>.json

ingress:
  - hostname: pipfactor.com
    service: http://127.0.0.1:3000
  - hostname: www.pipfactor.com
    service: http://127.0.0.1:3000
  - hostname: api.pipfactor.com
    service: http://127.0.0.1:8080
  - hostname: sse.pipfactor.com
    service: http://127.0.0.1:8081
  - hostname: n8n.pipfactor.com
    service: http://127.0.0.1:5678
  - hostname: mt5.pipfactor.com
    service: http://127.0.0.1:9001
  - service: http_status:404
```

## Example Dev Tunnel Config

Same server, different ports:

```yaml
tunnel: <dev-tunnel-id>
credentials-file: /etc/cloudflared/<dev-tunnel-id>.json

ingress:
  - hostname: app.pipfactor-dev.com
    service: http://127.0.0.1:4300
  - hostname: api.pipfactor-dev.com
    service: http://127.0.0.1:48080
  - hostname: sse.pipfactor-dev.com
    service: http://127.0.0.1:48081
  - hostname: n8n.pipfactor-dev.com
    service: http://127.0.0.1:45678
  - service: http_status:404
```

---

## Port Rules

## Production rules

- never expose `8080` and `8081` publicly if `cloudflared` is the intended ingress
- bind public-facing app services to `127.0.0.1`
- only expose a non-loopback port if an external non-HTTP client truly requires it

## Special note on `9001`

Your current stack publishes MT5 ingress on `9001`.

Decide which of these is true:

1. an external MT5 client must connect remotely
2. MT5 traffic can remain internal or tunnelled only

If option 2 is true, do not leave `9001` internet-exposed.

## Special note on `5678`

N8N should not be left broadly exposed.

If it stays public:

- keep Basic Auth enabled
- keep it on a dedicated hostname
- keep it behind Cloudflare
- consider Cloudflare Access later if admin exposure grows

---

## Docker Compose Guidance

## Recommended file structure

- `docker-compose.yml`
- `docker-compose.prod.yml`
- `docker-compose.dev.yml`
- `.env.prod`
- `.env.dev`

## Production compose behavior

- bind service ports to loopback
- load `.env.prod`
- keep `TRUST_PROXY_HEADERS=1`
- keep `COOKIE_SECURE=1`
- keep `COOKIE_DOMAIN=.pipfactor.com`

## Dev compose behavior

- bind service ports to loopback
- load `.env.dev`
- use dev-only public URLs
- keep `COOKIE_SECURE=1`
- use dev-only cookie domain

## Local compose behavior

- use `.env.local`
- local ports can bind normally on localhost
- `TRUST_PROXY_HEADERS=0`
- `COOKIE_SECURE=0`
- no Cloudflare dependency

---

## Public URL Ownership

Each environment must own its own URLs.

## Backend-owned public values

- `API_BASE_URL`
- `FRONTEND_URL`
- `N8N_BASE_URL`
- `PLISIO_CALLBACK_URL` if explicitly set

## Frontend-owned public values

- `VITE_PUBLIC_APP_URL`
- `VITE_API_BASE_URL`
- `VITE_API_SSE_URL`
- `VITE_SUPABASE_URL`
- Turnstile site key

## Third-party dashboards that must match

- Supabase auth callback URLs
- Cloudflare Turnstile widget hostnames
- Razorpay webhook endpoint
- Plisio webhook/callback endpoint

---

## Suggested Bring-Up Order For Production

1. provision the server
2. install Docker and Docker Compose
3. install `cloudflared`
4. copy tunnel credentials to the server
5. prepare `.env.prod`
6. build and start the backend stack
7. build and start the frontend artifact server
8. verify local loopback health endpoints:
   - `127.0.0.1:8080/api/health`
   - `127.0.0.1:8081/health`
   - frontend on `127.0.0.1:3000`
9. start the production tunnel
10. verify public endpoints through Cloudflare
11. switch webhook/dashboard settings if required

---

## Smoke Test Checklist After Deployment

## Frontend

- homepage loads on `https://pipfactor.com`
- `www.pipfactor.com` canonicalizes correctly
- login dialog loads
- Turnstile loads correctly for production hostnames

## Backend auth

- login succeeds
- `Set-Cookie` shows:
  - correct name
  - `Secure`
  - `SameSite=Lax`
  - `Domain=.pipfactor.com`
- protected API calls succeed
- logout clears cookies

## SSE

- `https://sse.pipfactor.com` connects
- authenticated streams work
- stream survives normal navigation

## Payments

- checkout creation works
- provider callback URL is correct
- webhook reaches the server
- WAF rules do not block webhook traffic

## n8n

- host is reachable only through intended hostname
- Basic Auth works

---

## WAF and Security Notes

### Payment webhooks

Your existing docs already note that payment webhooks may need Cloudflare WAF exceptions.

Before final production cutover:

1. verify the webhook paths in Cloudflare rules
2. verify the provider IP allowlists are still current in your operational docs
3. confirm the rules apply to the actual production hostname and path

### Proxy trust

If any of these are publicly reachable directly:

- `8080`
- `8081`
- `5678`

then `TRUST_PROXY_HEADERS=1` is too trusting for that topology.

Tunnel-only ingress and loopback-only service bindings are the clean fix.

---

## Rollback Plan

If production cutover fails:

1. stop the new production tunnel service on the cloud server
2. restore the last known-good tunnel ingress target
3. restore provider webhook targets if they were changed
4. invalidate cookies if cookie scope or names changed during rollout
5. keep the server stack intact for debugging, but remove it from public ingress

This is why Phase 0 inventory is important.

---

## Minimum Safe Production Cutover

If you want the shortest path to a safe production deployment:

1. keep dev local for now
2. move only production to the cloud server
3. use separate production tunnel credentials on that server
4. stop serving production from your laptop

That alone removes the largest operational risk in the current setup.

---

## Final Recommendation

For the next deployment milestone, do this:

1. deploy production to the cloud server with loopback-bound services
2. run production Cloudflare Tunnel from the server
3. remove laptop-based production hostname routing
4. keep local on plain localhost
5. add remote dev only after that, preferably under a separate dev domain

That gives you a stable deployment path now without forcing a giant multi-environment rewrite in one shot.
