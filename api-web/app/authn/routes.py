import hmac
import hashlib
import httpx
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request, Response

from .csrf import generate_csrf_token
from .rate_limit_auth import rate_limit
from .session_store import (
    CSRF_COOKIE_NAME,
    SERVER_SESSION_MAX_PER_USER,
    SESSION_COOKIE_NAME,
    create_session,
    delete_all_sessions_for_user,
    delete_session,
    get_cached_perms,
    get_cached_profile,
    get_session,
    list_active_sessions_for_user,
    public_session_id,
    refresh_session_activity,
    invalidate_perms,
    invalidate_profile_cache,
    put_replay_guard_once,
    set_cached_perms,
    set_cached_profile,
    update_all_sessions_for_user_perms,
)
from .supabase_admin import SupabaseAdminError, admin_get_user, admin_update_user
from .supabase_rpc import rpc_get_active_subscription
from .token_verify import verify_supabase_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_DEV_ENV_VALUES = {"dev", "development", "local", "test", "testing"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    logger.warning(
        "Invalid %s=%r. Falling back to secure default=%s.",
        name,
        raw,
        int(default),
    )
    return default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r. Falling back to secure default=%s.",
            name,
            raw,
            default,
        )
        return default

    if value < minimum:
        logger.warning(
            "Invalid %s=%r (must be >= %s). Falling back to secure default=%s.",
            name,
            raw,
            minimum,
            default,
        )
        return default

    return value


def _env_cookie_samesite(name: str, default: str = "lax") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in {"lax", "strict", "none"}:
        return normalized

    logger.warning(
        "Invalid %s=%r. Falling back to secure default=%s.",
        name,
        raw,
        default,
    )
    return default


def _runtime_environment_name() -> str:
    for env_name in ("AUTH_ENV", "APP_ENV", "ENVIRONMENT", "FASTAPI_ENV", "ENV"):
        raw = (os.getenv(env_name) or "").strip()
        if raw:
            return raw.lower()
    return "production"


def _is_development_environment() -> bool:
    return _runtime_environment_name() in _DEV_ENV_VALUES


COOKIE_SECURE = _env_bool("COOKIE_SECURE", True)
COOKIE_SAMESITE = _env_cookie_samesite("COOKIE_SAMESITE", "lax")
COOKIE_DOMAIN = (os.getenv("COOKIE_DOMAIN") or "").strip().lstrip(".")
TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", False)
SESSION_BINDING_MODE = (os.getenv("SESSION_BINDING_MODE") or "ua_only").strip().lower()
AUTHDBG_ENABLED = _env_bool("AUTHDBG_ENABLED", False)
# Secure-by-default: require signed invalidation webhooks unless explicitly disabled.
AUTH_INVALIDATION_USE_SIGNED = _env_bool("AUTH_INVALIDATION_USE_SIGNED", True)
AUTH_INVALIDATION_TOLERANCE_SECONDS = _env_int(
    "AUTH_INVALIDATION_TOLERANCE_SECONDS",
    300,
    minimum=1,
)

if not AUTH_INVALIDATION_USE_SIGNED and not _is_development_environment():
    raise RuntimeError(
        "AUTH_INVALIDATION_USE_SIGNED=0 is only allowed in development/test environments"
    )

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY")
AUTH_EXCHANGE_TURNSTILE_ENFORCE = _env_bool("AUTH_EXCHANGE_TURNSTILE_ENFORCE", False)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COUNTRY_PATTERN = re.compile(r"^[A-Za-z]{2}$")
SESSION_PUBLIC_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")


def _rid(request: Request) -> str:
    return (
        (request.headers.get("x-request-id") or "").strip()
        or (request.headers.get("x-correlation-id") or "").strip()
        or uuid.uuid4().hex[:12]
    )


def _sid_hash(sid: str | None) -> str:
    if not sid:
        return "none"
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:10]


def _authdbg(message: str, *args: Any) -> None:
    if AUTHDBG_ENABLED:
        logger.info("AUTHDBG " + message, *args)


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].strip().lower()


def _is_local_host(request: Request) -> bool:
    return _request_host(request) in {"localhost", "127.0.0.1"}


def _should_enforce_turnstile(request: Request) -> bool:
    # Deterministic behavior across environments is controlled by one explicit flag.
    return bool(TURNSTILE_SECRET_KEY) and AUTH_EXCHANGE_TURNSTILE_ENFORCE


def _turnstile_verify_form_payload(turnstile_token: str, request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "secret": TURNSTILE_SECRET_KEY,
        "response": turnstile_token,
    }

    # Only include remoteip when the deployment explicitly trusts proxy headers.
    # In containerized proxy topologies, request.client.host is often an internal hop.
    if TRUST_PROXY_HEADERS:
        remote_ip = _request_client_ip(request)
        if remote_ip:
            payload["remoteip"] = remote_ip

    return payload


def _parse_json_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as err:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from err

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return payload


def _trim_sha256_prefix(signature: str) -> str:
    if signature.startswith("sha256="):
        return signature[len("sha256=") :]
    return signature


async def _verify_signed_invalidation(request: Request, secret: str, raw_body: bytes) -> None:
    timestamp_raw = (request.headers.get("x-webhook-timestamp") or "").strip()
    provided_sig = _trim_sha256_prefix((request.headers.get("x-webhook-signature") or "").strip())
    replay_id = (request.headers.get("x-webhook-id") or "").strip()

    if not timestamp_raw or not provided_sig or not replay_id:
        raise HTTPException(status_code=401, detail="Missing webhook signature headers")

    try:
        timestamp = int(timestamp_raw)
    except ValueError as err:
        raise HTTPException(status_code=401, detail="Invalid webhook timestamp") from err

    now = int(time.time())
    if abs(now - timestamp) > AUTH_INVALIDATION_TOLERANCE_SECONDS:
        raise HTTPException(status_code=401, detail="Stale webhook timestamp")

    signed = timestamp_raw.encode("utf-8") + b"." + raw_body
    expected_sig = hmac.new(secret.encode("utf-8"), signed, digestmod="sha256").hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    replay_ttl = max(60, AUTH_INVALIDATION_TOLERANCE_SECONDS * 2)
    is_new = await put_replay_guard_once(f"replay:auth_invalidate:{replay_id}", replay_ttl)
    if not is_new:
        raise HTTPException(status_code=401, detail="Replay detected")


def _should_use_secure_cookie(request: Request) -> bool:
    if not COOKIE_SECURE:
        return False

    if _is_local_host(request):
        return False

    # Only trust forwarded proto when the deployment explicitly opts in.
    xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if TRUST_PROXY_HEADERS and xf_proto:
        return xf_proto == "https"

    return True


def _set_cookie(request: Request, response: Response, name: str, value: str, max_age: int) -> None:
    domain = _cookie_domain_for_request(request)
    rid = _rid(request)
    _authdbg(
        "event=cookie.set rid=%s cookie=%s secure=%s httponly=%s samesite=%s domain=%s max_age=%s",
        rid,
        name,
        int(_should_use_secure_cookie(request)),
        int(name == SESSION_COOKIE_NAME),
        COOKIE_SAMESITE,
        domain or "<host-only>",
        max_age,
    )
    logger.debug(
        "auth.cookie.set name=%s host=%s domain=%s secure=%s samesite=%s max_age=%s httponly=%s",
        name,
        _request_host(request),
        domain or "<host-only>",
        int(_should_use_secure_cookie(request)),
        COOKIE_SAMESITE,
        max_age,
        int(name == SESSION_COOKIE_NAME),
    )
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=(name == SESSION_COOKIE_NAME),
        secure=_should_use_secure_cookie(request),
        samesite=COOKIE_SAMESITE,
        path="/",
        domain=domain,
    )


def _cookie_domain_for_request(request: Request) -> str | None:
    if not COOKIE_DOMAIN:
        return None
    host = _request_host(request)
    if _is_local_host(request):
        return None
    # Never attach Domain for raw IP hosts; browsers reject domain attrs there.
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass

    # Only use configured domain when host belongs to that domain.
    # Misconfigured values (e.g., COOKIE_DOMAIN=www.example.com with host=api.example.com)
    # cause browsers to reject Set-Cookie and break session persistence.
    if host == COOKIE_DOMAIN or host.endswith(f".{COOKIE_DOMAIN}"):
        return COOKIE_DOMAIN

    logger.warning(
        "auth.cookie.domain_mismatch configured=%s host=%s fallback=host-only",
        COOKIE_DOMAIN,
        host,
    )
    return None


def _delete_cookie_both_scopes(request: Request, response: Response, name: str) -> None:
    # Clear host-only cookie.
    response.delete_cookie(name, path="/")
    # Also clear shared domain cookie to prevent stale auth in mixed deployments.
    domain = _cookie_domain_for_request(request)
    if domain:
        response.delete_cookie(name, path="/", domain=domain)


def _request_client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        cf_connecting_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf_connecting_ip:
            return cf_connecting_ip
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _ip_prefix_for_storage(request: Request) -> str:
    raw_ip = _request_client_ip(request)
    if not raw_ip:
        return ""

    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return ""

    if ip.version == 4:
        octets = str(ip).split(".")
        if len(octets) != 4:
            return ""
        return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"

    hextets = ip.exploded.split(":")
    return f"{':'.join(hextets[:4])}::/64"


def _extract_country_code(request: Request) -> str | None:
    for header_name in (
        "cf-ipcountry",
        "cloudfront-viewer-country",
        "x-vercel-ip-country",
        "x-country-code",
        "x-country",
    ):
        value = (request.headers.get(header_name) or "").strip().upper()
        if COUNTRY_PATTERN.match(value) and value not in {"XX", "ZZ"}:
            return value
    return None


def _user_agent_summary(user_agent: str) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "Unknown"

    ua_lower = ua.lower()
    browser = "Unknown Browser"
    if "edg/" in ua_lower:
        browser = "Edge"
    elif "opr/" in ua_lower or "opera" in ua_lower:
        browser = "Opera"
    elif "chrome/" in ua_lower and "chromium" not in ua_lower:
        browser = "Chrome"
    elif "safari/" in ua_lower and "chrome/" not in ua_lower:
        browser = "Safari"
    elif "firefox/" in ua_lower:
        browser = "Firefox"

    os_name = "Unknown OS"
    if "windows" in ua_lower:
        os_name = "Windows"
    elif "mac os x" in ua_lower or "macintosh" in ua_lower:
        os_name = "macOS"
    elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
        os_name = "iOS"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "linux" in ua_lower:
        os_name = "Linux"

    device = "Desktop"
    if "ipad" in ua_lower or "tablet" in ua_lower:
        device = "Tablet"
    elif "mobile" in ua_lower or "iphone" in ua_lower or "android" in ua_lower:
        device = "Mobile"

    return f"{browser} on {os_name} ({device})"[:160]


def _user_agent_hash(user_agent: str) -> str:
    normalized = (user_agent or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _device_binding_matches(session: dict[str, Any], request: Request) -> bool:
    expected_ua_hash = str(session.get("ua_hash") or "")
    if not expected_ua_hash:
        return True

    current_ua_hash = _user_agent_hash(request.headers.get("user-agent") or "")
    ua_ok = (not expected_ua_hash) or hmac.compare_digest(current_ua_hash, expected_ua_hash)
    return ua_ok


def _session_binding_components(request: Request) -> tuple[str, str]:
    mode = SESSION_BINDING_MODE
    if mode in {"off", "none", "disabled"}:
        return "", _ip_prefix_for_storage(request)
    # IP binding is intentionally disabled. Even in strict mode, tunnels/proxies/mobile
    # frequently alter source address metadata and cause false invalidations.
    return (
        _user_agent_hash(request.headers.get("user-agent") or ""),
        _ip_prefix_for_storage(request),
    )


def _parse_remember_me_flag(body: dict[str, Any]) -> bool:
    raw = body.get("remember_me")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        return normalized in {"1", "true", "yes", "on"}
    return False


async def _require_session(request: Request) -> dict[str, Any]:
    rid = _rid(request)
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        _authdbg("event=routes.require_session.denied rid=%s reason=missing_sid", rid)
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = await get_session(sid)
    if not session:
        _authdbg(
            "event=routes.require_session.denied rid=%s reason=sid_not_found sid=%s",
            rid,
            _sid_hash(sid),
        )
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 🛡️ CSRF PROTECTION: Enforce for all mutating methods (POST, PATCH, DELETE, etc.)
    # We use the 'Double Submit Cookie' pattern. The frontend must send the value
    # from the 'csrf_token' cookie in the 'X-CSRF-Token' header.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
        header_csrf = (
            request.headers.get("X-CSRF-Token")
            or request.headers.get("x-csrf-token")
            or ""
        )
        if not cookie_csrf or not header_csrf or not hmac.compare_digest(cookie_csrf, header_csrf):
            _authdbg(
                "event=routes.require_session.denied rid=%s reason=csrf method=%s cookie_present=%s header_present=%s",
                rid,
                request.method,
                int(bool(cookie_csrf)),
                int(bool(header_csrf)),
            )
            logger.warning(
                "auth.csrf_failure user_id=%s method=%s",
                session.get("user_id") or "unknown",
                request.method,
            )
            raise HTTPException(
                status_code=403, detail="CSRF validation failed. Please refresh the page."
            )

    if not _device_binding_matches(session, request):
        _authdbg(
            "event=routes.require_session.denied rid=%s reason=device_mismatch sid=%s mode=%s ua_present=%s",
            rid,
            _sid_hash(sid),
            SESSION_BINDING_MODE,
            int(bool(session.get("ua_hash"))),
        )
        await delete_session(sid)
        logger.warning(
            "auth.session.device_mismatch event=device-mismatch user_id=%s sid=%s",
            session.get("user_id") or "",
            sid,
        )
        raise HTTPException(status_code=401, detail="Session invalidated")

    refreshed = await refresh_session_activity(sid, session)
    if not refreshed:
        _authdbg(
            "event=routes.require_session.denied rid=%s reason=refresh_failed sid=%s",
            rid,
            _sid_hash(sid),
        )
        raise HTTPException(status_code=401, detail="Not authenticated")

    _authdbg(
        "event=routes.require_session.allowed rid=%s sid=%s user_tail=%s",
        rid,
        _sid_hash(sid),
        str(refreshed.get("user_id") or "")[-6:],
    )

    return refreshed


def _build_profile(user: dict[str, Any]) -> dict[str, Any]:
    metadata = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    return {
        "id": user.get("id"),
        "email": user.get("email") or "",
        "full_name": metadata.get("full_name"),
        "avatar_url": metadata.get("avatar_url"),
        "is_active": True,
        "email_verified": bool(user.get("email_confirmed_at")),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at") or user.get("created_at"),
    }


@router.post("/exchange")
async def auth_exchange(request: Request, response: Response) -> dict[str, Any]:
    await rate_limit(request, "auth_exchange", limit_per_minute=20)
    rid = _rid(request)

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    remember_me = _parse_remember_me_flag(body)
    token = (body.get("access_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="access_token is required")
    turnstile_token = (body.get("turnstile_token") or "").strip() or None
    enforce_turnstile = _should_enforce_turnstile(request)
    _authdbg(
        "event=exchange.start rid=%s host=%s origin=%s referer=%s enforce_turnstile=%s remember_me=%s",
        rid,
        _request_host(request),
        request.headers.get("origin") or "",
        request.headers.get("referer") or "",
        int(enforce_turnstile),
        int(remember_me),
    )
    logger.debug(
        "auth.exchange.start host=%s origin=%s referer=%s enforce_turnstile=%s token_present=%s secure_cookie=%s cookie_domain=%s samesite=%s xf_proto=%s cf_connecting_ip=%s xff=%s",
        _request_host(request),
        request.headers.get("origin") or "",
        request.headers.get("referer") or "",
        int(enforce_turnstile),
        int(bool(turnstile_token)),
        int(_should_use_secure_cookie(request)),
        _cookie_domain_for_request(request) or "<host-only>",
        COOKIE_SAMESITE,
        (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower(),
        request.headers.get("cf-connecting-ip") or "",
        (request.headers.get("x-forwarded-for") or "").split(",")[0].strip(),
    )
    if enforce_turnstile and not turnstile_token:
        logger.warning(
            "auth.exchange rejected: missing turnstile token user_id=unknown host=%s",
            _request_host(request),
        )
        raise HTTPException(status_code=400, detail="turnstile_token is required")

    claims = await verify_supabase_access_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    supabase_exp = int(claims.get("exp") or 0)
    now = int(time.time())
    if supabase_exp <= now:
        raise HTTPException(status_code=401, detail="Token expired")

    # -------------------------------------------------------------------------
    # 🛡️ AI AUDIT SAFEGUARD: CLOUDFLARE TURNSTILE ARCHITECTURE
    # -------------------------------------------------------------------------
    # DO NOT REMOVE THIS LOGIC. However, in production, AUTH_EXCHANGE_TURNSTILE_ENFORCE
    # MUST ALWAYS BE "0" (disabled). 
    # Why? Turnstile tokens are strictly SINGLE-USE. In our architecture:
    # 1. The frontend sends the Turnstile token to Supabase (`signInWithPassword`).
    # 2. Supabase consumes the token, verifies it with Cloudflare, and issues a JWT.
    # 3. The frontend then sends the JWT to this /exchange endpoint.
    # 4. If we try to verify the exact same Turnstile token again here, Cloudflare
    #    will reject it with a 403 "timeout-or-duplicate" error.
    # 
    # We keep this verification logic intact for endpoints/flows that might bypass
    # Supabase in the future or for specific dev testing, but for the standard login flow, 
    # this must remain bypassed by setting AUTH_EXCHANGE_TURNSTILE_ENFORCE=0.
    # -------------------------------------------------------------------------
    if enforce_turnstile and turnstile_token:
        turnstile_payload = _turnstile_verify_form_payload(turnstile_token, request)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                ts_response = await client.post(
                    TURNSTILE_VERIFY_URL,
                    data=turnstile_payload,
                )
            ts_result = ts_response.json()
            logger.debug(
                "auth.exchange.turnstile.result status=%s success=%s hostname=%s action=%s error_codes=%s remoteip_included=%s",
                getattr(ts_response, "status_code", "unknown"),
                int(bool(ts_result.get("success"))),
                ts_result.get("hostname") or "",
                ts_result.get("action") or "",
                ",".join(str(x) for x in (ts_result.get("error-codes") or [])),
                int("remoteip" in turnstile_payload),
            )
        except httpx.HTTPError as exc:
            logger.warning("auth.exchange turnstile verify request failed: %s", exc)
            raise HTTPException(status_code=503, detail="Captcha verification unavailable") from exc
        except ValueError as exc:
            logger.warning(
                "auth.exchange turnstile verify returned non-JSON response status=%s",
                getattr(ts_response, "status_code", "unknown"),
            )
            raise HTTPException(status_code=503, detail="Captcha verification unavailable") from exc

        if not ts_result.get("success"):
            error_codes = ts_result.get("error-codes")
            if not isinstance(error_codes, list):
                error_codes = []
            logger.warning(
                "auth.exchange turnstile verify failed user_id=%s host=%s cf_hostname=%s cf_action=%s error_codes=%s remoteip_included=%s",
                user_id,
                _request_host(request),
                ts_result.get("hostname") or "",
                ts_result.get("action") or "",
                ",".join(str(x) for x in error_codes),
                int("remoteip" in turnstile_payload),
            )
            raise HTTPException(
                status_code=403,
                detail="Captcha verification failed. Please try again.",
            )
    elif TURNSTILE_SECRET_KEY and not enforce_turnstile:
        logger.info(
            "auth.exchange turnstile enforcement disabled (AUTH_EXCHANGE_TURNSTILE_ENFORCE=0); "
            "token not verified for host=%s",
            _request_host(request),
        )

    perms_cached = await get_cached_perms(user_id)
    if perms_cached:
        plan = perms_cached.get("plan") or "free"
        permissions = perms_cached.get("permissions") or []
    else:
        sub = None
        try:
            sub = await rpc_get_active_subscription(user_id)
        except Exception as exc:
            # Keep login available during transient DB/PostgREST outages by falling
            # back to least-privilege access until subscription lookup recovers.
            logger.warning(
                "auth.exchange subscription lookup failed for user=%s: %s",
                user_id,
                exc,
            )

        if sub and sub.get("is_current") and (sub.get("status") in ("active", "trial")):
            plan = sub.get("plan_name") or "unknown"
            permissions = ["dashboard", "signals"]
        else:
            plan = (sub.get("plan_name") if sub else None) or "free"
            permissions = ["dashboard"]

        await set_cached_perms(
            user_id,
            {
                "allowed": True,
                "plan": plan,
                "permissions": permissions,
                "updated_at": int(time.time()),
            },
        )

    existing_sid = request.cookies.get(SESSION_COOKIE_NAME)

    ua_hash, ip_prefix = _session_binding_components(request)

    created = await create_session(
        user_id=user_id,
        supabase_exp=supabase_exp,
        plan=plan,
        permissions=permissions,
        remember_me=remember_me,
        ua_hash=ua_hash,
        ip_prefix=ip_prefix,
        ua_summary=_user_agent_summary(request.headers.get("user-agent") or ""),
        country=_extract_country_code(request),
    )
    logger.debug(
        "auth.exchange.session.created user_id=%s sid=%s ttl=%s remember_me=%s ua_bound=%s ip_bound=%s",
        user_id,
        created["sid"],
        created["ttl"],
        int(remember_me),
        int(bool(ua_hash)),
        int(bool(ip_prefix)),
    )
    _authdbg(
        "event=exchange.session rid=%s user_tail=%s sid=%s ttl=%s rotated_from=%s",
        rid,
        str(user_id or "")[-6:],
        _sid_hash(created.get("sid")),
        created.get("ttl"),
        _sid_hash(existing_sid),
    )

    if existing_sid and existing_sid != created["sid"]:
        await delete_session(existing_sid)
        logger.info(
            "auth.session.rotated event=rotate user_id=%s old_sid=%s new_sid=%s",
            user_id,
            existing_sid,
            created["sid"],
        )

    csrf_token = generate_csrf_token()
    # Clear stale cookie variants first (host-only + shared-domain) so the browser
    # does not send duplicate `session` cookies with conflicting SIDs.
    _delete_cookie_both_scopes(request, response, SESSION_COOKIE_NAME)
    _delete_cookie_both_scopes(request, response, CSRF_COOKIE_NAME)
    _set_cookie(request, response, SESSION_COOKIE_NAME, created["sid"], max_age=created["ttl"])
    _set_cookie(request, response, CSRF_COOKIE_NAME, csrf_token, max_age=created["ttl"])
    logger.debug(
        "auth.exchange.cookies_set user_id=%s secure=%s domain=%s samesite=%s ttl=%s",
        user_id,
        int(_should_use_secure_cookie(request)),
        _cookie_domain_for_request(request) or "<host-only>",
        COOKIE_SAMESITE,
        created["ttl"],
    )

    session_cap_warning = None
    if int(created.get("evicted_count") or 0) > 0:
        session_cap_warning = {
            "code": "session_cap_eviction",
            "evicted_count": int(created.get("evicted_count") or 0),
            "session_cap": int(SERVER_SESSION_MAX_PER_USER),
            "message": "Older devices were signed out because your session limit was reached.",
        }

    return {
        "ok": True,
        "user_id": user_id,
        "plan": plan,
        "permissions": permissions,
        "remember_me": remember_me,
        "csrf_token": csrf_token,
        "expires_in": created["ttl"],
        "session_evicted_count": int(created.get("evicted_count") or 0),
        "session_cap_warning": session_cap_warning,
    }


@router.get("/sessions")
async def auth_list_sessions(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_list_sessions", limit_per_minute=120)

    session = await _require_session(request)
    user_id = str(session.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    current_sid = request.cookies.get(SESSION_COOKIE_NAME) or ""
    active_sessions = await list_active_sessions_for_user(user_id)

    result = []
    for entry in active_sessions:
        sid = str(entry.get("sid") or "")
        data = entry.get("session") if isinstance(entry.get("session"), dict) else {}
        result.append(
            {
                "sid": public_session_id(sid),
                "current": bool(current_sid and sid == current_sid),
                "created_at": int(data.get("iat") or 0) or None,
                "last_activity": int(data.get("last_activity") or 0) or None,
                "expires_at": int(data.get("exp") or 0) or None,
                "remember_me": bool(data.get("remember_me")),
                "user_agent": {
                    "summary": (str(data.get("ua_summary") or "Unknown")[:160]),
                },
                "ip": (str(data.get("ip_prefix") or "")[:64]) or None,
                "country": (str(data.get("country") or "").upper()[:2]) or None,
            }
        )

    return {"ok": True, "sessions": result, "total": len(result)}


@router.delete("/sessions/{public_sid}")
async def auth_revoke_session(
    request: Request,
    response: Response,
    public_sid: str = Path(..., min_length=16, max_length=16),
) -> dict[str, Any]:
    await rate_limit(request, "auth_revoke_session", limit_per_minute=60)

    if not SESSION_PUBLIC_ID_PATTERN.match(public_sid):
        raise HTTPException(status_code=400, detail="Invalid session id")

    session = await _require_session(request)
    user_id = str(session.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    active_sessions = await list_active_sessions_for_user(user_id)
    target_sid = None
    for entry in active_sessions:
        sid = str(entry.get("sid") or "")
        if sid and hmac.compare_digest(public_session_id(sid), public_sid):
            target_sid = sid
            break

    if not target_sid:
        raise HTTPException(status_code=404, detail="Session not found")

    await delete_session(target_sid)

    current_sid = request.cookies.get(SESSION_COOKIE_NAME) or ""
    current_revoked = bool(current_sid and current_sid == target_sid)
    if current_revoked:
        _delete_cookie_both_scopes(request, response, SESSION_COOKIE_NAME)
        _delete_cookie_both_scopes(request, response, CSRF_COOKIE_NAME)

    return {"ok": True, "revoked": True, "current_revoked": current_revoked}


@router.post("/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        # Read session before deleting so we can bust the profile cache.
        session = await get_session(sid)
        await delete_session(sid)
        if session and session.get("user_id"):
            await invalidate_profile_cache(session["user_id"])

    _delete_cookie_both_scopes(request, response, SESSION_COOKIE_NAME)
    _delete_cookie_both_scopes(request, response, CSRF_COOKIE_NAME)
    return {"ok": True}


@router.post("/logout-all")
async def auth_logout_all(request: Request, response: Response) -> dict[str, Any]:
    session = await _require_session(request)
    user_id = session["user_id"]

    await invalidate_profile_cache(user_id)
    deleted = await delete_all_sessions_for_user(user_id)
    _delete_cookie_both_scopes(request, response, SESSION_COOKIE_NAME)
    _delete_cookie_both_scopes(request, response, CSRF_COOKIE_NAME)
    return {"ok": True, "deleted": deleted}


@router.get("/validate")
async def auth_validate(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_validate", limit_per_minute=120)
    rid = _rid(request)

    try:
        session = await _require_session(request)
    except HTTPException as exc:
        _authdbg(
            "event=validate.result rid=%s allowed=0 sid_present=%s host=%s",
            rid,
            int(bool(request.cookies.get(SESSION_COOKIE_NAME))),
            _request_host(request),
        )
        logger.debug(
            "auth.validate.denied host=%s sid_present=%s",
            _request_host(request),
            int(bool(request.cookies.get(SESSION_COOKIE_NAME))),
        )
        raise HTTPException(status_code=401, detail=exc.detail or "Not authenticated") from exc

    _authdbg(
        "event=validate.result rid=%s allowed=1 user_tail=%s perms_count=%s",
        rid,
        str(session.get("user_id") or "")[-6:],
        len(session.get("permissions") or []),
    )

    return {
        "allowed": True,
        "user_id": session.get("user_id"),
        "plan": session.get("plan"),
        "permissions": session.get("permissions", []),
    }


@router.get("/me")
async def auth_me(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_me", limit_per_minute=120)

    session = await _require_session(request)
    user_id = session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # --- Fast path: profile cache hit ---
    cached = await get_cached_profile(user_id)
    if cached:
        # Always reflect the latest plan/permissions from the session (may have been
        # updated by a perms invalidation webhook since the profile was cached).
        cached["plan"] = session.get("plan")
        cached["permissions"] = session.get("permissions", [])
        return cached

    # --- Slow path: fetch from Supabase Admin + subscription RPC ---
    try:
        user = await admin_get_user(user_id)
    except SupabaseAdminError as exc:
        logger.error("auth.me failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=exc.status_code, detail="Profile service unavailable") from exc

    subscription = None
    try:
        subscription = await rpc_get_active_subscription(user_id)
    except Exception as exc:
        logger.warning("auth.me subscription lookup failed for user=%s: %s", user_id, exc)

    payload = {
        "allowed": True,
        "user_id": user_id,
        "plan": session.get("plan"),
        "permissions": session.get("permissions", []),
        "profile": _build_profile(user),
        "subscription": subscription,
    }

    # Store everything except plan/permissions in cache (those come from the live session)
    await set_cached_profile(user_id, payload)

    return payload


@router.patch("/profile")
async def auth_update_profile(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_update_profile", limit_per_minute=30)

    session = await _require_session(request)
    user_id = session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    full_name_raw = body.get("full_name") if isinstance(body, dict) else None
    full_name = (full_name_raw or "").strip() if isinstance(full_name_raw, str) else ""
    if not full_name:
        raise HTTPException(status_code=400, detail="full_name is required")
    if len(full_name) > 120:
        raise HTTPException(status_code=400, detail="full_name is too long")

    try:
        current_user = await admin_get_user(user_id)
        metadata = current_user.get("user_metadata") if isinstance(current_user.get("user_metadata"), dict) else {}
        metadata["full_name"] = full_name
        updated_user = await admin_update_user(user_id, {"user_metadata": metadata})
    except SupabaseAdminError as exc:
        logger.error("auth.update_profile failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=exc.status_code, detail="Profile update unavailable") from exc

    # Bust cache so next /auth/me reflects updated name immediately
    await invalidate_profile_cache(user_id)

    return {"ok": True, "profile": _build_profile(updated_user)}


@router.patch("/email")
async def auth_update_email(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_update_email", limit_per_minute=20)

    session = await _require_session(request)
    user_id = session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    email_raw = body.get("email") if isinstance(body, dict) else None
    email = (email_raw or "").strip().lower() if isinstance(email_raw, str) else ""
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail="email is invalid")

    try:
        updated_user = await admin_update_user(user_id, {"email": email})
    except SupabaseAdminError as exc:
        logger.error("auth.update_email failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=exc.status_code, detail="Email update unavailable") from exc

    # Bust cache so next /auth/me reflects the pending email change
    await invalidate_profile_cache(user_id)

    return {
        "ok": True,
        "message": "Email update started. Please verify your new email address.",
        "profile": _build_profile(updated_user),
    }


@router.patch("/password")
async def auth_update_password(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_update_password", limit_per_minute=20)

    session = await _require_session(request)
    user_id = session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    password_raw = body.get("password") if isinstance(body, dict) else None
    password = password_raw if isinstance(password_raw, str) else ""
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")

    try:
        await admin_update_user(user_id, {"password": password})
    except SupabaseAdminError as exc:
        logger.error("auth.update_password failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=exc.status_code, detail="Password update unavailable") from exc

    return {"ok": True}


@router.post("/invalidate")
async def auth_invalidate_user(request: Request) -> dict[str, Any]:
    """Internal webhook target: invalidate a user's perms and optionally sessions."""
    await rate_limit(request, "auth_invalidate", limit_per_minute=60)

    secret = os.getenv("AUTH_INVALIDATION_WEBHOOK_SECRET") or ""
    if not secret:
        logger.error("auth.invalidate webhook secret is not configured")
        raise HTTPException(status_code=503, detail="Service unavailable")

    raw_body = await request.body()
    if AUTH_INVALIDATION_USE_SIGNED:
        await _verify_signed_invalidation(request, secret, raw_body)
    else:
        provided = request.headers.get("x-webhook-secret") or ""
        if not hmac.compare_digest(provided, secret):
            raise HTTPException(status_code=401, detail="Unauthorized")

    payload = _parse_json_body(raw_body)
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    force_logout = bool(payload.get("force_logout", False))
    logger.info("auth.invalidate event=received user=%s force_logout=%s", user_id, force_logout)

    # Always bust caches
    await invalidate_perms(user_id)
    await invalidate_profile_cache(user_id)
    
    if force_logout:
        deleted = await delete_all_sessions_for_user(user_id)
        logger.info("auth.invalidated event=full_logout user=%s sessions_deleted=%s", user_id, deleted)
        return {"ok": True, "deleted": deleted, "mode": "full_logout"}
    else:
        # Graceful refresh
        try:
            sub = await rpc_get_active_subscription(user_id)
            plan = sub.get("plan_id") or "free"
            permissions = sub.get("permissions") or []
            
            updated = await update_all_sessions_for_user_perms(user_id, plan, permissions)
            logger.info("auth.invalidated event=graceful_refresh user=%s sessions_updated=%s plan=%s", user_id, updated, plan)
            return {"ok": True, "updated": updated, "mode": "graceful_refresh"}
        except Exception as e:
            logger.error("auth.invalidated event=refresh_failed user=%s error=%s", user_id, str(e))
            return {"ok": True, "error": str(e), "mode": "failed_refresh"}
