# Session/Auth And Security Audit Report (2026-03-18)

Scope:
- Replace prior verification matrix with a current issue-first report.
- Document all surfaced findings from latest parallel audits (security, bugs, edge cases, performance, session cleanup worker, Turnstile dev/prod cohesion, and Axon dead-code scan).
- No code fixes proposed in this document.

Method:
- Parallel code audit agents across frontend and backend.
- Targeted review of session worker + Turnstile config paths.
- Axon repository and dead-code analysis.

## Severity Summary

- Critical: 1
- High: 5
- Medium: 5
- Low: 2

## Findings (Ordered By Severity)

### CRITICAL-01: Turnstile exchange enforcement is disabled at runtime

Impact:
- Bot/challenge verification for auth exchange can be bypassed in runtime despite Turnstile wiring existing in code.

Evidence:
- `ai_trading_bot/docker-compose.yml` (`AUTH_EXCHANGE_TURNSTILE_ENFORCE: "0"`)
- `ai_trading_bot/api-web/app/authn/routes.py` (`_should_enforce_turnstile` depends on enforce flag)

Risk Type:
- Security hardening gap

---

### HIGH-01: Frontend route protection is effectively bypassed

Impact:
- Client-side access control assumptions do not hold; unauthenticated navigation to protected views is possible.

Evidence:
- `ai-trading_frontend/src/App.tsx`
- `ai-trading_frontend/src/components/RequireAuth.tsx`

Risk Type:
- Auth gate regression

---

### HIGH-02: Protected API paths still rely on legacy auth dependency path

Impact:
- New session-hardening checks are not consistently guaranteed across all protected API traffic paths.

Evidence:
- `ai_trading_bot/api-web/app/auth.py`
- `ai_trading_bot/api-web/app/main.py`
- `ai_trading_bot/api-web/app/authn/routes.py` (hardened logic exists but not fully central in all route dependencies)

Risk Type:
- Session enforcement inconsistency

---

### HIGH-03: Dev/local environment inherits production auth mode

Impact:
- Localhost and dev behavior can drift from intended test assumptions, increasing false confidence and missed regressions.

Evidence:
- `ai_trading_bot/docker-compose.yml` (`AUTH_ENV: production`)
- `ai_trading_bot/docker-compose.dev.yml`
- `ai_trading_bot/docker-compose.local.yml`

Risk Type:
- Environment cohesion / config drift

---

### HIGH-04: Silent rehydrate can drop valid session when Turnstile token is unavailable

Impact:
- Frontend may reset auth state during non-interactive recovery flows when backend cookie expires and captcha token is absent.

Evidence:
- `ai-trading_frontend/src/hooks/useAuth.tsx` (silent exchange branch requiring captcha token and resetting auth)

Risk Type:
- Edge-case auth reliability

---

### HIGH-05: Session index drift fallback can trigger scan-heavy cleanup behavior

Impact:
- Between expiry and janitor cleanup, stale index memberships can increase expensive fallback scanning and latency/churn under load.

Evidence:
- `ai_trading_bot/api-web/app/authn/session_store.py` (`delete_all_sessions_for_user`, scan fallback and index prune flow)

Risk Type:
- Performance + data consistency window

---

### MEDIUM-01: Janitor ownership is not singular in multi-worker contexts

Impact:
- Duplicate janitor loops may run concurrently (session prune and strategy expiry), causing duplicate work and race-style noise.

Evidence:
- `ai_trading_bot/api-web/start.sh`
- `ai_trading_bot/api-web/app/main.py`

Risk Type:
- Background worker concurrency / performance

---

### MEDIUM-02: Internal API key verification style is inconsistent across endpoints

Impact:
- Mixed verification paths increase drift risk and auditing complexity for sensitive internal routes.

Evidence:
- `ai_trading_bot/api-web/app/main.py` (constant-time helper in some places, direct comparisons in others)

Risk Type:
- Security consistency

---

### MEDIUM-03: Turnstile configuration contract drift between code and docs

Impact:
- Mode-specific key behavior in frontend runtime can diverge from single-key documentation expectations.

Evidence:
- `ai-trading_frontend/src/config/turnstile.ts`
- `ai-trading_frontend/README.md`

Risk Type:
- Operational misconfiguration

---

### MEDIUM-04: Session janitor observability is low for healthy idle operation

Impact:
- Harder to distinguish "idle but healthy" from "stalled" in production monitoring.

Evidence:
- `ai_trading_bot/api-web/app/main.py` (logs emitted mostly on removals/errors)

Risk Type:
- Monitoring / incident response

---

### MEDIUM-05: Test coverage gaps for highest-risk auth regressions

Impact:
- Key regressions can pass current checks due to incomplete route-protection and integration lifecycle tests.

Evidence:
- Frontend lacks broad route-guard regression tests.
- Backend integration coverage for full `exchange -> validate -> me` cookie lifecycle remains incomplete.

Risk Type:
- Quality / regression escape

---

### LOW-01: Turnstile bypass logging message is semantically misleading

Impact:
- Log language implies environment-based bypass, but bypass is flag-based.

Evidence:
- `ai_trading_bot/api-web/app/authn/routes.py`

Risk Type:
- Operational clarity

---

### LOW-02: Dead-code footprint remains non-trivial (Axon)

Impact:
- Increases maintenance surface and can obscure live-path security reasoning.

Evidence (sample):
- `api-web/app/auth.py` (`optional_auth_context`)
- `api-web/app/authn/routes.py` (`_env_bool`, `_env_int`, `_env_cookie_samesite`, `_is_development_environment` reported as unreachable by Axon)
- Multiple additional symbols across cache, worker, and analyzer modules.

Risk Type:
- Maintainability / potential stale logic

## Session Cleanup Worker Focus

Observed Issues:
- Multi-worker duplicate janitor execution risk.
- Stale index drift window before prune can cause fallback scans.
- Sparse heartbeat-level observability during no-op intervals.

Primary Affected Files:
- `ai_trading_bot/api-web/app/main.py`
- `ai_trading_bot/api-web/app/authn/session_store.py`
- `ai_trading_bot/api-web/start.sh`

## Turnstile Cohesion Focus (Localhost vs Production)

Observed Issues:
- Backend exchange enforcement disabled by runtime flag.
- Dev/local compose path inherits production auth environment.
- Frontend mode-aware key resolution and docs are not fully aligned.
- Silent rehydrate edge path can force auth reset when captcha token is missing.

Primary Affected Files:
- `ai_trading_bot/docker-compose.yml`
- `ai_trading_bot/docker-compose.dev.yml`
- `ai_trading_bot/docker-compose.local.yml`
- `ai_trading_bot/api-web/app/authn/routes.py`
- `ai-trading_frontend/src/config/turnstile.ts`
- `ai-trading_frontend/src/hooks/useAuth.tsx`
- `ai-trading_frontend/README.md`

## Frontend Alert Visibility Audit Delta

Issue surfaced:
- Some user-facing alerts/toasts used low-contrast styling and could become visually weak/invisible depending on page palette.

Related files reviewed:
- `ai-trading_frontend/src/components/ui/sonner.tsx`
- `ai-trading_frontend/src/components/auth/LoginDialog.tsx`
- `ai-trading_frontend/src/components/auth/SignUpDialog.tsx`
- `ai-trading_frontend/src/components/auth/VerificationPending.tsx`
- `ai-trading_frontend/src/pages/AuthCallback.tsx`

Note:
- This section documents the surfaced UI visibility issue family; fix status is tracked separately in implementation notes/PR history.

## Delta From Previous (2026-03-16) Verification File

Key change:
- Prior file was primarily a conformance matrix.
- Current file is issue-first and records newly surfaced regressions/drift risks discovered by fresh audit.

Most important contradiction with prior assumptions:
- "Implemented" architecture claims remain broadly true, but enforcement consistency and runtime config posture currently introduce materially relevant gaps.

## Recommended Next Audit Pass (No Fixes In This File)

1. Re-run a dedicated route-protection and dependency-path audit after next auth merge.
2. Re-run Turnstile mode parity audit with explicit dev/local/prod test matrix.
3. Add and run integration checks for cookie lifecycle and logout behavior under tab/session races.
4. Schedule a dead-code cleanup validation pass using Axon before next release branch cut.
