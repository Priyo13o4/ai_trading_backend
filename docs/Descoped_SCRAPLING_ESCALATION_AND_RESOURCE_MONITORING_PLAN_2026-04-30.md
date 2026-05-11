# Scrapling Escalation and Resource Monitoring Plan

## Scope

This document scopes the current scraper incident and the follow-up monitoring work.

Assumptions for the plan:

- n8n will be configured with `continue on error` for the scrape node.
- A downstream code node in n8n will inspect the response/error payload and trigger the debug notification workflow.
- Backoff delays will not be added.
- Broad domain-level blocking or domain-wide skip lists will not be added.

## Why This Needs To Change

The live logs show a consistent escalation pattern on ForexFactory that is expensive, slow, and now dominates the scraper path.

Observed flow from the live trace on 2026-04-30:

- Step 1 HTTP request fails immediately with `403`.
- Step 2 stealth without Cloudflare gets a `307` redirect and then a `403`.
- Step 3 stealth with Cloudflare detects Turnstile, loops through CAPTCHA handling, and eventually succeeds with `200`.

Concrete evidence from the trace:

- HTTP mode: `Fetched (403)` and `HTTP insufficient (0 chars) or error (403)`.
- Stealth no CF: `Fetched (307)` followed by `Fetched (403)`.
- Stealth with CF: `The turnstile version discovered is "managed"`, `Cloudflare page didn't disappear after 10s`, `captcha still present`, `Cloudflare captcha is solved`, then `Fetched (200)`.
- Final request latency is high enough to matter operationally, with the full path taking roughly 45 seconds in the successful case.

This tells us two important things:

1. The VM IP is probably not the root problem by itself, because other reputation checks still look clean.
2. The scraper is reacting to request shape, browser behavior, and CF detection, not only to network reputation.

There is a second operational issue in the same trace:

- The scrape container sustained high CPU, with Chromium processes running away during the stealth/CF path.
- That makes the failure mode more than just a slow request; it becomes a resource leak and can affect unrelated workloads on the VM.

## What Is Actually Broken

### 1. The escalation path is too expensive

The current logic always tries the cheapest path first, but it does not distinguish well enough between:

- a genuine transient HTTP failure,
- a site that is consistently blocking plain HTTP,
- a redirect-to-block loop,
- a Cloudflare challenge that requires the full browser path.

As a result, the scraper pays for multiple doomed steps before reaching the one path that works.

### 2. Chromium lifecycle cleanup is too fragile

The live symptom is sustained CPU saturation and runaway browser processes. That points to cleanup risk in timeout/cancellation paths, especially where blocking fetches are run in worker threads.

The repo note on Scrapling threading is relevant here:

- persistent `StealthySession` reuse can fail under threaded servers,
- thread affinity matters if browser sessions are reused,
- a fast HTTP preflight is useful, but only if the browser cleanup path is deterministic.

### 3. The response contract needs a clean error signal

You want the workflow to continue, but you also want the scraper to tell n8n when the scrape failed or degraded.

That means the scraper should not silently look healthy when it has fallen back.

## Where To Change Code

### Scraper runtime

- [ai_trading_bot/scrapling-api/app_async.py](../scrapling-api/app_async.py)
- [ai_trading_bot/scrapling-api/app.py](../scrapling-api/app.py)
- [ai_trading_bot/scrapling-api/escalation.py](../scrapling-api/escalation.py)

These files control:

- HTTP first-pass behavior,
- stealth no-CF escalation,
- stealth with CF escalation,
- timeout handling,
- degraded result signaling,
- cleanup behavior.

### Alerting and n8n payload transport

- [ai_trading_bot/api-web/app/notifications/error_alerts.py](../api-web/app/notifications/error_alerts.py)
- [ai_trading_bot/api-web/app/notifications/dead_letter.py](../api-web/app/notifications/dead_letter.py)

These files already own the reusable webhook alert transport. They are the right place to add a new resource-spike event type rather than inventing a parallel alert path.

### Resource monitor implementation

- New host-side or VM-side monitor script under [ai_trading_bot/scripts](../scripts)
- Potential Docker Compose wiring if the monitor should run as a containerized service

This monitor should live close to the deployment target that can actually see the container and process list. On your VM, that usually means a host-level script or a privileged sidecar with Docker access.

### n8n workflow

- Existing scrape workflow node configuration in n8n
- New code node that checks `error`, `meta.degraded_mode`, and/or `success`

This is where the `continue on error` behavior should be paired with explicit error detection and notification.

## What To Change

### A. Keep the response contract explicit

Do not make the scraper pretend success when it is degraded.

Recommended response behavior:

- Successful scrape: `success: true`, `error: null`, `meta.degraded_mode: false`.
- Best-effort or fallback scrape: `success: false` or a clearly flagged degraded state, with `meta.degraded_mode: true` and a real `error` string.
- Complete failure: non-2xx HTTP status with a structured error payload.

Because n8n will be set to `continue on error`, this will not stop the workflow. It will only make the error visible to the downstream code node.

Recommended fields to keep or standardize:

- `success`
- `error`
- `meta.status_code`
- `meta.escalation_step`
- `meta.degraded_mode`
- `meta.fallback_mode_used`

### B. Keep the 3-step escalation model, but tighten the control flow

Do not add backoff delays.

Do not add coarse domain-level lockouts.

Instead:

- Keep Step 1 HTTP as the cheapest probe so you still know when HTTPx recovers.
- Keep Step 2 stealth without CF as the intermediate path.
- Keep Step 3 stealth with CF as the last resort.
- Reduce wasted work by making the scraper decide faster when a request is clearly in a CF block pattern.

Practical examples:

- Treat `403` plus empty body as a hard signal to escalate.
- Treat `307 -> 403` in the no-CF path as a strong signal that Step 2 is not worth repeated internal retries.
- Keep the CF path focused on solving once, not spinning in long internal loops.

### C. Fix Chromium leak and CPU saturation

This is the highest-risk operational issue.

Changes needed:

- Make sure every browser/session object is closed in `finally` paths, not only on happy-path exits.
- Ensure a timeout cannot leave a live Chromium child process behind.
- Revisit persistent session reuse in threaded execution.
- If browser reuse remains, pin it to strict thread affinity or a single worker.
- Add a hard cap on concurrent stealth sessions.
- Add a cleanup pass for stale Chromium descendants inside the scrape container.

The goal here is not to hide the problem with a restart. The goal is to guarantee that a failed request cannot leak a browser process.

### D. Add resource-spike monitoring

Extend the alerting payloads so n8n can receive resource events, not only runtime errors.

The new monitor should capture:

- container name,
- CPU percentage over time,
- memory percentage or RSS,
- sustained spike duration,
- top processes inside the container,
- command line or process name for the offending Chromium process.

Trigger conditions should be based on sustained spike windows, not single samples.

Suggested event type:

- `resource_spike`

Suggested payload shape:

- `event_type`
- `service`
- `environment`
- `container_name`
- `severity`
- `cpu_percent`
- `memory_percent`
- `duration_seconds`
- `top_processes`
- `timestamp`
- `context`

## Recommended Implementation Order

1. Update the scraper response contract so degraded or failed results are clearly marked.
2. Fix cleanup around browser/session timeouts so Chromium cannot leak.
3. Add the resource-spike alert payload and monitor.
4. Tune the escalation heuristics only after the telemetry is visible.

## Validation Plan

### Scraper contract

- Run a known ForexFactory URL that currently forces escalation.
- Confirm HTTP still starts first.
- Confirm the final payload contains the right `meta.escalation_step` and `meta.degraded_mode` values.
- Confirm n8n continues execution with `continue on error` and the code node sees the error/degraded signal.

### Chromium cleanup

- Trigger a timeout on the stealth path.
- Confirm no orphaned Chromium processes remain in the scrape container.
- Confirm CPU returns to baseline after the request completes or fails.

### Resource monitoring

- Simulate a high-CPU container or replay the scrapling spike.
- Confirm the monitor emits a single resource-spike alert for a sustained window.
- Confirm the alert includes enough process detail to identify Chromium descendants.

## Bottom Line

The fix is not to abandon HTTP probing and it is not to add backoff.

The fix is to:

- keep the fast HTTP probe,
- fail faster when the block pattern is obvious,
- clean up Chromium deterministically,
- surface failed or degraded scrapes to n8n without stopping the workflow,
- and add resource monitoring so the next runaway browser process is visible before it takes the VM down.