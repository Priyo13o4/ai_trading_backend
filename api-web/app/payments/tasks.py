import asyncio
import logging
from datetime import datetime, timezone, timedelta
from app.db import get_supabase_client
from app.payments.payment_providers.router import get_provider

logger = logging.getLogger(__name__)

DEFERRED_CANCELLATION_JANITOR_INTERVAL_SECONDS = 3600  # run every 1 hour
CANCELLATION_LEAD_TIME_HOURS = 24

async def _run_deferred_cancellation_tick() -> int:
    """
    Checks the database for subscriptions that are flagged to cancel_at_period_end
    but haven't actually been cancelled at the provider level yet (cancelled_at is null).
    If they are within 24 hours of expiration, it invokes the provider's cancel API.
    """
    try:
        supabase = get_supabase_client()
        
        # 1. Fetch active subscriptions flagged for cancellation
        query = supabase.table("user_subscriptions") \
            .select("id, user_id, payment_provider, external_subscription_id, expires_at") \
            .eq("status", "active") \
            .eq("cancel_at_period_end", True) \
            .is_("cancelled_at", "null") \
            .execute()
            
        subscriptions = query.data
        if not subscriptions:
            return 0
            
        cancelled_count = 0
        now_utc = datetime.now(timezone.utc)
        
        for sub in subscriptions:
            expires_at_str = sub.get("expires_at")
            if not expires_at_str:
                continue
                
            try:
                # Parse expires_at
                parsed = expires_at_str.strip().replace("Z", "+00:00")
                expires_at_dt = datetime.fromisoformat(parsed)
                if expires_at_dt.tzinfo is None:
                    expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                    
                # Check if within lead time (or already past)
                time_until_expiry = expires_at_dt - now_utc
                if time_until_expiry <= timedelta(hours=CANCELLATION_LEAD_TIME_HOURS):
                    
                    provider_name = sub.get("payment_provider")
                    external_id = sub.get("external_subscription_id")
                    
                    if provider_name == "manual" or not external_id:
                        # Manual subscriptions are just cancelled internally
                        supabase.table("user_subscriptions").update({
                            "cancelled_at": now_utc.isoformat(),
                            "auto_renew": False
                        }).eq("id", sub["id"]).execute()
                        cancelled_count += 1
                        continue
                        
                    # Call provider
                    provider = get_provider(provider_name)
                    success = await provider.cancel_subscription(external_id)
                    
                    if success:
                        # Update DB
                        # Note: status is still 'active', expire_subscriptions cron will flip it to 'expired'
                        supabase.table("user_subscriptions").update({
                            "cancelled_at": now_utc.isoformat(),
                            "auto_renew": False
                        }).eq("id", sub["id"]).execute()
                        
                        logger.info(f"[DEFERRED CANCEL] Successfully executed provider cancellation for sub {sub['id']}")
                        cancelled_count += 1
                    else:
                        logger.warning(f"[DEFERRED CANCEL] Provider failed to cancel sub {sub['id']}")
                        
            except Exception as e:
                logger.error(f"[DEFERRED CANCEL] Error processing sub {sub['id']}: {e}")
                continue
                
        return cancelled_count
        
    except Exception as e:
        logger.error(f"[DEFERRED CANCEL] Tick failed: {e}")
        return 0

async def deferred_cancellation_janitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("[JANITOR] Deferred cancellation janitor started")
    while not stop_event.is_set():
        try:
            cancelled = await _run_deferred_cancellation_tick()
            if cancelled > 0:
                logger.info(f"[JANITOR] Deferred cancellation processed {cancelled} subscriptions")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[JANITOR] Deferred cancellation janitor tick failed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=DEFERRED_CANCELLATION_JANITOR_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue

    logger.info("[JANITOR] Deferred cancellation janitor stopped")
