# Razorpay Payment Integration Audit Report

## 1. Security Audits

**Webhook Signature Verification & Integrity**
*   **Status**: `verify_webhook_signature` handles HMAC SHA256 verification securely using the Razorpay SDK (`client.utility.verify_webhook_signature`).
*   **Vulnerability Check (IDOR & Privilege Escalation)**: 
    *   In `routes.py->create_checkout`, the `user_id` attached to the Razorpay subscription as `notes` originates strictly from the trusted server-side `user_session`, mitigating checkout parameter tampering.
    *   In `webhook_handler.py`, the system rightfully overrides the webhook's `notes` payload by strictly relying on `transaction["user_id"]` pulled from the `payment_transactions` DB table corresponding to the matched `provider_payment_id`. This correctly blocks IDOR where a malicious user overrides the callback `user_id`.

**Missing Input Validation**
*   In `webhook_handler.py`, the JSON payload is decoded directly (`payload = json.loads(raw_body.decode('utf-8'))`). It ensures valid JSON, but schema validation against strictly expected fields (using Pydantic models) before traversing deep keys (e.g. `payload["payload"]["subscription"]["entity"]`) is highly recommended to block malicious generic payloads that might crash the worker.

## 2. Code logic and bugs

**Silent Failure in Plan Fetching (`razorpay_provider.py`)**
*   **Bug/Flaw**: In `create_checkout`, if `self.client.plan.fetch(target_plan_id)` fails (due to network failure, wrong plan ID, etc.), the integration lazily catches the general `Exception as e` and blindly defaults to `"INR"` and `5.00`.
*   *Why this is bad*: If Razorpay configuration fails, a fallback price could charge a customer incorrectly and instantiate real system liabilities instead of rejecting the transaction safely.
*   *Recommendation*: Drop the silent catch block. If the plan cannot be fetched, throw an `HTTPException(500)` so the transaction safely halts.

**Deferred Cancellation Desync (`routes.py`)**
*   **Logic Flaw**: `cancel_subscription` in `routes.py` flags `"cancel_at_period_end": True` in the database, deferring actual cancellation to a background cron job. However, if the cron job fails or is delayed past the Razorpay billing boundary, the user **will** be charged again despite officially cancelling through your system.
*   *Recommendation*: Razorpay natively supports `cancel_at_cycle_end=1` directly in the `subscription.cancel()` method. Relying on provider-level state is infinitely safer than local DB cron flags.

## 3. Edge Cases

**Race Conditions in Webhook Processing**
*   **Addressed well**: Redis distributed locks (`set(nx=True)`) are placed on `webhook_lock:razorpay:{event_id}` for 30s. A fallback DB-level unique constraint on `webhook_events` is also gracefully handled. This effectively blocks duplicate Razorpay event invocations.

**Unmapped Events Overwriting Processed State**
*   In `webhook_handler.py`, if an event is unmapped (e.g., `event_type` is ignored), you flag the event as processed and exit status `ok`. Because webhooks are un-ordered over the network, if out-of-band events trigger without mapping, you handle it cleanly without clogging the queue or throwing retry 500s.

**Provider Payment Entity Not Yet Transacted**
*   *Edge case*: The webhook arrives before the DB transaction `payment_transactions` is fully committed by the frontend flow.
*   *Current behavior*: It yields a `Transaction not found` error inside standard mapping and registers an `ok` response without retrying.
*   *Risk*: If Razorpay's initial webhook beats the HTTP response of `create_checkout` settling, the user does not get subscribed. Returning a `404` or `409` back to Razorpay for unknown transactions, combined with standard webhook automatic retries, would guarantee eventual consistency. 

## 4. Stale functions or better code logic choices

**Dead Code Isolation**
*   Based on our Axon Knowledge Graph analysis, the backend contains stale logic mapping that interacts poorly with legacy modules.
*   Notably, caching artifacts related to the broader subscription system in `cache.py` and `redis_cache.py` are unused (`_build_url`, old pubsubs).

**Logic Refactoring**:
*   *Session Invalidation*: Inside `webhook_handler.py`, the token cache invalidation operates generically (`hasattr(INVALIDATE_PERMS, '__call__')`). Given the `session_store.py` explicitly exports `update_all_sessions_for_user_perms` now, it should strictly use `await update_all_sessions_for_user_perms(...)` instead of deleting Redis keys `user:perms:{tx_user_id}`. Deleting raw keys disrupts the gracefully rolling cache mechanism in `session_store.py`.
*   *Cleaner State Enums*: `RazorpayProvider.map_event_to_state` explicitly matches 10 dict events. Move this configuration to the `PaymentProvider` base class as an abstract mapping configuration rather than hardcoding dictionary declarations upon the method instantiation on every webhook ping.

## 5. Frontend & UI/UX Audit (`Profile.tsx`)

**Checkout & Payment Flow Disconnect**
*   **Issue:** The payment method selection is persistently visible in the left column ("Payment Preferences"), while the actual "Subscribe" button is physically disconnected in a completely separate section on the right. Currently, users are forced to select a method first, then find the submit button. User feedback highlighted: "The payment option selection should come after we select to click on payments."
*   **Recommendation:** Remove the static "Payment Preferences" selection card. Instead, when the user clicks "Subscribe" or "Upgrade", open a dedicated modal or integrated checkout step asking them to choose their payment method (Razorpay vs Crypto) at the time of intent.

**Non-Functional "Manage Billing"**
*   **Issue:** Clicking "Manage Billing" for active subscribers triggers a placeholder toast (`toast.info('Billing portal integration... coming soon.')`) and performs no real action. This creates false affordance.
*   **Recommendation:** If the provider portal isn't fully integrated yet, the button should either be hidden, explicitly marked as `disabled`, or its functionality should be constrained strictly to the working cancellation flow until the fully integrated backend endpoints are ready.

**Hardcoded / Non-Functional "Export" Button**
*   **Issue:** The 'Export' (Download) button in the Billing History section is hardcoded to emit a toast message (`No history to download yet.`), ignoring whether the user actually has real billing history populated in the state.
*   **Recommendation:** Implement a client-side CSV export function based on the `billingHistory` payload. If the array is empty, dynamically disable the button rather than allowing a click that throws a failure toast.

**Superficial 'AI-Generated' Aesthetics & Mock Data**
*   **Issue:** The UI leans heavily into overly complex "AI-generated" tropes that serve no functional purpose, violating practical design guidelines. 
    *   The "Preferences" section (Email Notifications / Trade Alerts) consists of purely hardcoded mock data without interactive toggles.
    *   The "Security" section falsely and persistently displays "Last changed never" regardless of state.
    *   Excessive use of decorative badges and highly stylized components (e.g., the `opacity-20 hover:opacity-60 duration-700` styling on the account deletion zone and neon-glow tags) creates cognitive friction.
*   **Recommendation:** Strip out hardcoded mock sections that do not have functional backend hooks. Simplify visual hierarchy by removing meaningless decorative badges, replacing persistent structural borders with cleanly grouped spacing, and ensuring all visible data points (like password modified date) map strictly to real state objects.
