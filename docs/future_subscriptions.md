# Future Scope: Multi-Plan & Multi-Cycle Subscriptions

## The Scenario
In the future, the platform will offer multiple subscription plans (e.g., Starter, Professional, Elite) and multiple billing cycles (e.g., Monthly, Yearly at a discount). Users will have the ability to:
1. **Upgrade**: Move to a higher tier mid-cycle.
2. **Downgrade**: Move to a lower tier mid-cycle.
3. **Change Cycle**: Switch from Monthly to Yearly (or vice versa).

## 1. Codebase & Database Readiness

Our current database architecture is highly prepared for this transition. The foundational work done during the initial payment integration makes this future scope entirely additive, requiring no destructive migrations.

*   **`subscription_plans` table**: Already structured to support `price_usd` and `billing_period`. We simply add new rows for "Professional (Monthly)", "Professional (Yearly)", etc.
*   **`user_subscriptions` table**: Uses a `plan_id` foreign key paired with a `plan_snapshot` JSONB column. This ensures historical integrity—even if plan prices change later, the snapshot preserves exactly what the user bought.
*   **`provider_prices` table**: Maps our internal `plan_id` securely to the specific provider-level IDs (e.g., Razorpay Plan ID, NOWPayments ID), supporting endless provider integrations.
*   **`payment_transactions` table**: Tracks individual payment events identically, cleanly supporting pro-rated upgrade charges as separate transaction records linked to the same subscription.

**Required Additions (Codebase):**
*   **Backend Proration Logic**: Endpoints like `POST /api/payments/preview-upgrade` (to calculate cost) and `POST /api/payments/update-subscription`.
*   **Frontend UI**: The pricing page must become state-aware, transitioning from showing "Subscribe" to showing "Current Plan", "Upgrade", "Downgrade", and a Monthly/Yearly toggle.

## 2. Implementation Strategy per Provider

### Razorpay (Fiat)
Razorpay natively supports complex subscription upgrades, downgrades, and proration via their **Update a Subscription API**.

*   **Upgrades**: When a user upgrades, the backend calls the Razorpay API with the new `plan_id`. Razorpay calculates the prorated difference for the unused days of the current month and immediately attempts to charge the user's saved card for that difference. The billing cycle natively continues.
*   **Downgrades / Crossgrades**: When a user downgrades, the industry standard (and safest route) is to use Razorpay's scheduling feature. We tell Razorpay to schedule the downgrade to take effect **at the end of the current billing cycle**. No complex refunds or credit notes are needed; they simply pay the lower amount on their next renewal date, preserving the premium access they already paid for this month.
*   **API Usage**: `razorpay_client.subscription.update(sub_id, {"plan_id": new_plan_id, "schedule_change_at": "cycle_end"})`

### NOWPayments (Crypto)
NOWPayments operates on a "push" model. Because crypto wallets do not function like saved fiat credit cards, we cannot automatically pull or charge pro-rated differences. Subscriptions are essentially automated email invoices or scheduled payment links.

*   **Upgrades**: If a user upgrades mid-cycle, the backend must create a **new, one-off NOWPayments invoice** for the prorated difference amount. Once the user manually pays this invoice, our backend cancels their old recurring instruction and creates a new one for the higher tier, scheduling its first strict cycle bill for the start of the next month.
*   **Downgrades**: Because issuing fractional crypto refunds is complex (gas fees, volatile network conditions), downgrades are handled identically to Razorpay's scheduled downgrades. The user retains their current premium tier until the month finishes. We simply update the NOWPayments recurring payment instruction so that the *next* automated invoice sent to the user is for the lower tier.

## 3. Recommended Implementation Phases (Future Scope)

**Phase 1: Admin & Plan Configuration**
*   Create Yearly and higher-tier plans inside the Razorpay and NOWPayments commercial dashboards.
*   Seed these configuration IDs into the `subscription_plans` and `provider_prices` database tables.

**Phase 2: API & Core Logic Enhancements**
*   Add `POST /api/payments/preview-update` to return calculated prorated costs before the user clicks confirm.
*   Add `POST /api/payments/update-subscription` to trigger the Razorpay Update API or generate the NOWPayments upgrade invoice.

**Phase 3: Frontend Overhaul**
*   Introduce billing interval toggles (Monthly / Yearly) to `Pricing.tsx`.
*   Update the `useAuth` hook and `ProtectedRoute` guards to respect hierarchical `subscriptionTier` logic (e.g., `elite` > `professional` > `starter`), rather than a simple boolean check.

**Phase 4: Webhook Synchronisation**
*   Ensure backend processing of the `subscription.updated` Razorpay webhook to instantly reflect plan changes and new `plan_snapshot` generations in the `user_subscriptions` table.
