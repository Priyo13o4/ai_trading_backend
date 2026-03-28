"""DEPRECATED (pre-production cutover)

The old Supabase service-role based subscription middleware has been removed.

Rules after cutover:
- No Supabase calls in request paths or middleware.
- Cookie session + Redis session store is authoritative.
- Subscription/plan data is fetched once during /auth/exchange.
"""

from typing import Any

from fastapi import Depends

from .auth import auth_context
from .authn.authz import require_permission


class SubscriptionRequired:
    """Compatibility dependency: enforces the 'signals' permission."""

    def __init__(self, *args: Any, **kwargs: Any):
        pass

    async def __call__(self, ctx: dict[str, Any] = Depends(auth_context)) -> str:
        require_permission(ctx, "signals")
        return ctx["user_id"]


# ============================================================================
# USAGE EXAMPLES IN FASTAPI
# ============================================================================

"""
Example 1: Protect endpoint with subscription check (any active subscription)
"""
# @app.get("/api/profile")
# async def get_profile(user_id: str = Depends(SubscriptionRequired())):
#     return {"user_id": user_id, "message": "Access granted"}


"""
Example 2: Require specific trading pair access
"""
# @app.get("/api/signals/{pair}")
# async def get_signals(
#     pair: str,
#     user_id: str = Depends(SubscriptionRequired(required_pairs=["{pair}"]))
# ):
#     # User has access to this specific pair
#     signals = fetch_signals_from_redis(pair)
#     return signals


"""
Example 3: Check subscription manually in route
"""
# @app.get("/api/signals/{pair}")
# async def get_signals(
#     pair: str,
#     user_id: str = Depends(get_user_from_token)
# ):
#     # Manual subscription check
#     subscription = await check_subscription_access(user_id, pair)
#     
#     # Access subscription details
#     if subscription["is_trial"]:
#         # Show trial banner
#         pass
#     
#     signals = fetch_signals_from_redis(pair)
#     return {
#         "signals": signals,
#         "subscription": {
#             "plan": subscription["plan_name"],
#             "days_remaining": subscription["days_remaining"]
#         }
#     }


"""
Example 4: Multiple pair access check
"""
# @app.get("/api/signals/multi")
# async def get_multiple_signals(
#     pairs: list[str],
#     user_id: str = Depends(get_user_from_token)
# ):
#     subscription = await check_subscription_access(user_id)
#     allowed_pairs = subscription["pairs_allowed"]
#     
#     # Filter pairs user can access
#     accessible_pairs = [p for p in pairs if p in allowed_pairs]
#     inaccessible_pairs = [p for p in pairs if p not in allowed_pairs]
#     
#     signals = {pair: fetch_signals_from_redis(pair) for pair in accessible_pairs}
#     
#     return {
#         "signals": signals,
#         "inaccessible_pairs": inaccessible_pairs,
#         "upgrade_message": "Upgrade to access more pairs" if inaccessible_pairs else None
#     }


"""
Example 5: Public endpoint (no auth required)
"""
# @app.get("/api/preview/{pair}")
# async def get_preview_signals(pair: str):
#     # No authentication required for preview
#     preview = supabase.table("signal_previews")\
#         .select("*")\
#         .eq("trading_pair", pair)\
#         .eq("is_current", True)\
#         .execute()
#     
#     return preview.data


# ============================================================================
# PAYMENT WEBHOOK HANDLER EXAMPLE
# ============================================================================

"""
Example: Stripe webhook handler for successful payment
"""
# @app.post("/api/webhooks/stripe")
# async def stripe_webhook(request: Request):
#     import stripe
#     
#     payload = await request.body()
#     sig_header = request.headers.get("stripe-signature")
#     
#     try:
#         event = stripe.Webhook.construct_event(
#             payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET")
#         )
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))
#     
#     # Handle successful payment
#     if event["type"] == "invoice.payment_succeeded":
#         invoice = event["data"]["object"]
#         subscription_id = invoice["subscription"]
#         amount = invoice["amount_paid"] / 100  # Convert cents to dollars
#         
#         # Get subscription from metadata
#         user_id = invoice["metadata"].get("user_id")
#         
#         # Record payment
#         supabase.rpc("record_payment", {
#             "p_user_id": user_id,
#             "p_subscription_id": subscription_id,
#             "p_amount": amount,
#             "p_currency": "USD",
#             "p_provider": "stripe",
#             "p_external_payment_id": invoice["id"],
#             "p_status": "succeeded"
#         }).execute()
#         
#         # Renew subscription
#         supabase.rpc("renew_subscription", {
#             "p_subscription_id": subscription_id
#         }).execute()
#     
#     return {"success": True}
