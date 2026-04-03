import logging
import os
import uuid
import hashlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
from plisio import PlisioAioClient
import plisio as plisio_sdk

from app.observability.debug import debug_log
from app.db import get_supabase_client, async_db
from app.payments.constants import PaymentTransactionStatus
from app.payments.payment_providers.base import PaymentProvider

logger = logging.getLogger(__name__)

def _plisio_debug(msg: str, *args: object) -> None:
    debug_log(logger, "payments.plisio", msg, *args)

class PlisioProvider(PaymentProvider):
    def __init__(self):
        self.api_key = (os.getenv("PLISIO_API_KEY") or "").strip()
        self.callback_url = (os.getenv("PLISIO_CALLBACK_URL") or "").strip()
        self.callback_url_local = (os.getenv("PLISIO_CALLBACK_URL_LOCAL") or "").strip()
        self.callback_url_prod = (os.getenv("PLISIO_CALLBACK_URL_PROD") or "").strip()
        self.crypto_currency = (os.getenv("PLISIO_CRYPTO_CURRENCY") or "USDT").strip().upper()
        self.default_source_amount_usd = self._resolve_default_source_amount_usd()
        self.client = PlisioAioClient(api_key=self.api_key) if self.api_key else None

    @staticmethod
    def _runtime_environment_name() -> str:
        for env_name in ("APP_ENV", "AUTH_ENV", "ENVIRONMENT", "FASTAPI_ENV", "ENV"):
            raw = (os.getenv(env_name) or "").strip()
            if raw:
                return raw.lower()
        return "production"

    def _resolve_callback_base_url(self) -> str:
        runtime_env = self._runtime_environment_name()

        env_specific_callback = ""
        if runtime_env in {"local", "development", "dev"}:
            env_specific_callback = self.callback_url_local
        elif runtime_env in {"production", "prod"}:
            env_specific_callback = self.callback_url_prod

        return env_specific_callback or self.callback_url or (
            os.getenv("API_BASE_URL", "").rstrip("/") + "/api/webhooks/plisio"
        )

    @staticmethod
    def _resolve_default_source_amount_usd() -> float:
        raw_value = (os.getenv("PLISIO_DEFAULT_SOURCE_AMOUNT_USD") or "5.00").strip()
        try:
            amount = Decimal(raw_value)
        except Exception:
            logger.warning(
                "Invalid PLISIO_DEFAULT_SOURCE_AMOUNT_USD=%r; falling back to 5.00",
                raw_value,
            )
            amount = Decimal("5.00")

        if amount <= 0:
            logger.warning(
                "Non-positive PLISIO_DEFAULT_SOURCE_AMOUNT_USD=%r; falling back to 5.00",
                raw_value,
            )
            amount = Decimal("5.00")

        return float(amount.quantize(Decimal("0.01")))

    @staticmethod
    def _append_json_true(url: str) -> str:
        parsed = urlsplit(url)
        query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query_items["json"] = "true"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_items), parsed.fragment))

    @staticmethod
    def _is_absolute_http_url(url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _to_plain_dict(result: Any) -> Dict[str, Any]:
        if result is None:
            return {}
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            try:
                return result.model_dump()
            except Exception:
                pass
        if hasattr(result, "dict"):
            try:
                return result.dict()
            except Exception:
                pass
        if hasattr(result, "__dict__"):
            return dict(result.__dict__)
        return {"raw": str(result)}

    @staticmethod
    def _stable_order_number_int(order_number: str) -> int:
        digest = hashlib.sha256(order_number.encode("utf-8")).hexdigest()
        return int(digest, 16) % 10**15

    @staticmethod
    def _resolve_crypto_currency_enum(currency_code: str):
        normalized = str(currency_code or "").strip().upper()
        value = getattr(plisio_sdk.CryptoCurrency, normalized, None)
        if value is None:
            raise ValueError(f"Unsupported PLISIO_CRYPTO_CURRENCY: {currency_code}")
        return value

    @staticmethod
    def _resolve_fiat_currency_enum(currency_code: str):
        normalized = str(currency_code or "").strip().upper()
        value = getattr(plisio_sdk.FiatCurrency, normalized, None)
        if value is None:
            raise ValueError(f"Unsupported source currency: {currency_code}")
        return value

    async def create_checkout(self, user_id: str, plan_id: str, billing_period: str = "monthly") -> Dict[str, Any]:
        order_number = (
            f"{user_id}:{plan_id}:{billing_period}:{int(datetime.now(timezone.utc).timestamp())}:{uuid.uuid4().hex[:8]}"
        )
        _plisio_debug(
            "PLISIO_CALL create_checkout.start user_id=%s plan_id=%s billing_period=%s order_number=%s currency=%s",
            user_id,
            plan_id,
            billing_period,
            order_number,
            self.crypto_currency,
        )
        return await self.create_invoice(
            user_id=user_id,
            plan_id=plan_id,
            billing_period=billing_period,
            order_number=order_number,
            ttl_minutes=int((os.getenv("PLISIO_CHECKOUT_TTL_MINUTES") or "15").strip() or "15"),
        )

    async def create_invoice(
        self,
        *,
        user_id: str,
        plan_id: str,
        order_number: str,
        billing_period: str = "monthly",
        description: Optional[str] = None,
        order_name: Optional[str] = None,
        ttl_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.client:
            raise ValueError("Plisio API key not configured")
        if not order_number:
            raise ValueError("order_number is required for Plisio invoice creation")

        _plisio_debug(
            "PLISIO_CALL create_invoice.start user_id=%s plan_id=%s billing_period=%s order_number=%s",
            user_id,
            plan_id,
            billing_period,
            order_number,
        )

        supabase = get_supabase_client()
        plan_query = (
            supabase.table("subscription_plans")
            .select("id, name, price_usd, billing_period")
            .eq("is_active", True)
            .limit(1)
        )
        try:
            uuid.UUID(str(plan_id))
            plan_query = plan_query.eq("id", plan_id)
        except (ValueError, TypeError):
            plan_query = plan_query.eq("name", plan_id)

        plan_res = await async_db(lambda: plan_query.execute())
        if not plan_res.data:
            raise HTTPException(status_code=404, detail=f"Active subscription plan not found: {plan_id}")
        plan_row = plan_res.data[0]
        resolved_plan_name = str(plan_row.get("name") or plan_id)

        profile_res = await async_db(lambda user_id=user_id: (
            supabase.table("profiles")
            .select("email")
            .eq("id", user_id)
            .limit(1)
            .execute()
        ))
        user_email = profile_res.data[0].get("email") if profile_res.data else None

        plan_amount_usd = float(Decimal(str(plan_row.get("price_usd") or 0)).quantize(Decimal("0.01")))
        if plan_amount_usd <= 0:
            raise ValueError(f"Invalid USD amount configured for plan {plan_id}")

        effective_source_amount_usd = plan_amount_usd

        base_callback = self._resolve_callback_base_url()
        if not base_callback or not self._is_absolute_http_url(base_callback):
            raise ValueError(
                "Configure one of PLISIO_CALLBACK_URL_LOCAL / PLISIO_CALLBACK_URL_PROD / "
                "PLISIO_CALLBACK_URL, or set API_BASE_URL"
            )
        callback_with_json = self._append_json_true(base_callback)

        invoice_payload = {
            "currency": self._resolve_crypto_currency_enum(self.crypto_currency),
            "order_name": order_name or f"pipfactor-{resolved_plan_name}-{billing_period}",
            "order_number": self._stable_order_number_int(order_number),
            # SDK requires amount even for fiat conversion flows.
            "amount": effective_source_amount_usd,
            "source_currency": self._resolve_fiat_currency_enum("USD"),
            "source_amount": effective_source_amount_usd,
            "callback_url": callback_with_json,
            "description": description or f"PipFactor {resolved_plan_name} subscription ({billing_period})",
        }
        if ttl_minutes and ttl_minutes > 0:
            invoice_payload["expire_min"] = int(ttl_minutes)
        if user_email:
            invoice_payload["email"] = user_email

        _plisio_debug(
            "PLISIO_CALL create_invoice.request order_number_int=%s currency=%s source_currency=%s source_amount=%s callback_url=%s plan_amount_usd=%s effective_amount=%s",
            invoice_payload.get("order_number"),
            self.crypto_currency,
            "USD",
            invoice_payload.get("source_amount"),
            invoice_payload.get("callback_url"),
            plan_amount_usd,
            effective_source_amount_usd,
        )

        try:
            invoice_kwargs = {
                "source_currency": invoice_payload["source_currency"],
                "source_amount": invoice_payload["source_amount"],
                "description": invoice_payload["description"],
                "callback_url": invoice_payload["callback_url"],
                "email": invoice_payload.get("email"),
            }
            if invoice_payload.get("expire_min"):
                invoice_kwargs["expire_min"] = invoice_payload["expire_min"]

            try:
                invoice_result = await self.client.invoice(
                    invoice_payload["currency"],
                    invoice_payload["order_name"],
                    invoice_payload["order_number"],
                    invoice_payload["amount"],
                    **invoice_kwargs,
                )
            except TypeError:
                # Backward compatibility with SDK variants that do not accept expire_min.
                invoice_kwargs.pop("expire_min", None)
                invoice_result = await self.client.invoice(
                    invoice_payload["currency"],
                    invoice_payload["order_name"],
                    invoice_payload["order_number"],
                    invoice_payload["amount"],
                    **invoice_kwargs,
                )
        except Exception:
            logger.exception(
                "PLISIO_CALL create_invoice.error order_number_int=%s currency=%s source_amount=%s",
                invoice_payload.get("order_number"),
                self.crypto_currency,
                invoice_payload.get("source_amount"),
            )
            raise
        invoice_data = self._to_plain_dict(invoice_result)

        _plisio_debug(
            "PLISIO_CALL create_invoice.response order_number_int=%s txn_id=%s invoice_url=%s status=%s",
            invoice_payload.get("order_number"),
            invoice_data.get("txn_id") or invoice_data.get("id"),
            invoice_data.get("invoice_url") or invoice_data.get("checkout_url") or invoice_data.get("url"),
            invoice_data.get("status"),
        )

        checkout_url = (
            invoice_data.get("invoice_url")
            or invoice_data.get("checkout_url")
            or invoice_data.get("url")
        )
        # Keep a stable correlation id for webhook matching across status updates.
        provider_payment_id = str(invoice_payload["order_number"])

        return {
            "checkout_url": checkout_url,
            "provider_checkout_data": invoice_data,
            "provider_payment_id": provider_payment_id,
            "amount": effective_source_amount_usd,
            "currency": "USD",
            "source_currency": "USD",
            "crypto_currency": self.crypto_currency,
            "invoice_ttl_minutes": invoice_payload.get("expire_min"),
        }

    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        if not self.client:
            logger.error("Plisio API key not configured")
            return False

        try:
            # Some SDK versions return bool synchronously, others may be awaitable.
            validate_result = self.client.validate_callback(payload)
            if inspect.isawaitable(validate_result):
                validate_result = await validate_result
            is_valid = bool(validate_result)
            _plisio_debug(
                "PLISIO_CALLBACK signature_check result=%s payload_bytes=%s",
                is_valid,
                len(payload or b""),
            )
            return is_valid
        except Exception as exc:
            logger.error("Error validating Plisio callback: %s", exc)
            return False

    async def process_webhook(self, payload: Dict[str, Any]) -> None:
        return None

    def map_event_to_state(self, status: str) -> Optional[PaymentTransactionStatus]:
        if not status:
            return None

        normalized = str(status).strip().lower()
        mapping = {
            "new": PaymentTransactionStatus.PENDING,
            "pending": PaymentTransactionStatus.PENDING,
            "waiting": PaymentTransactionStatus.PENDING,
            "confirming": PaymentTransactionStatus.PROCESSING,
            "completed": PaymentTransactionStatus.SUCCEEDED,
            "success": PaymentTransactionStatus.SUCCEEDED,
            "confirmed": PaymentTransactionStatus.SUCCEEDED,
            "expired": PaymentTransactionStatus.EXPIRED,
            "cancelled": PaymentTransactionStatus.CANCELLED,
            "canceled": PaymentTransactionStatus.CANCELLED,
            "failed": PaymentTransactionStatus.FAILED,
            "error": PaymentTransactionStatus.FAILED,
            "mismatch": PaymentTransactionStatus.FAILED,
            "refunded": PaymentTransactionStatus.REFUNDED,
        }
        return mapping.get(normalized)

    async def cancel_subscription(self, subscription_id: str) -> bool:
        # Plisio is invoice-driven for this phase; no provider subscription cancel call exists.
        logger.info("Plisio cancel_subscription noop for id=%s", subscription_id)
        return False

    async def cancel_checkout_attempt(self, provider_payment_id: str) -> bool:
        # Plisio invoice cancellation is not available in the current SDK flow.
        logger.info("Plisio cancel_checkout_attempt noop for id=%s", provider_payment_id)
        return False
