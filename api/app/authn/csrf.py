import secrets
from fastapi import HTTPException, Request

CSRF_HEADER = "x-csrf-token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def enforce_csrf(request: Request, csrf_cookie_name: str) -> None:
    cookie_token = request.cookies.get(csrf_cookie_name)
    header_token = request.headers.get(CSRF_HEADER)
    if not cookie_token or not header_token or header_token != cookie_token:
        raise HTTPException(status_code=403, detail="CSRF validation failed")
