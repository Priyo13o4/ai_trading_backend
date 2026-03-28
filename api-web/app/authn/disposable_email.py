import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_DEFAULT_DISPOSABLE_DOMAINS = {
    "mailinator.com",
    "tempmail.com",
    "10minutemail.com",
    "guerrillamail.com",
    "yopmail.com",
    "trashmail.com",
    "throwawaymail.com",
    "getnada.com",
    "sharklasers.com",
    "dispostable.com",
}

_DEFAULT_BLOCKLIST_PATH = (
    Path(__file__).resolve().parent
    / "disposable_domains"
    / "disposable_email_blocklist.conf"
)


def _parse_env_domain_list(raw: str) -> set[str]:
    domains: set[str] = set()
    for token in raw.split(","):
        normalized = token.strip().lower().lstrip("@")
        if normalized:
            domains.add(normalized)
    return domains


def _parse_blocklist_file(raw: str) -> set[str]:
    domains: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip().lower()
        if not stripped or stripped.startswith("#"):
            continue
        domains.add(stripped.lstrip("@"))
    return domains


def _load_blocklist_file(path: Path) -> set[str]:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return _parse_blocklist_file(contents)


@lru_cache(maxsize=1)
def get_disposable_domains() -> set[str]:
    list_path_raw = (os.getenv("AUTH_DISPOSABLE_EMAIL_FILE") or "").strip()
    list_path = Path(list_path_raw) if list_path_raw else _DEFAULT_BLOCKLIST_PATH

    disposable_domains = _load_blocklist_file(list_path)
    if not disposable_domains:
        disposable_domains = set(_DEFAULT_DISPOSABLE_DOMAINS)

    extra_domains_raw = (os.getenv("AUTH_DISPOSABLE_EMAIL_DOMAINS") or "").strip()
    if extra_domains_raw:
        disposable_domains.update(_parse_env_domain_list(extra_domains_raw))

    return disposable_domains


def _extract_email_domain(email: str) -> str:
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[1].strip(".")


def _iter_domain_candidates(domain: str) -> Iterable[str]:
    current = domain
    while current:
        yield current
        if "." not in current:
            break
        current = current.split(".", 1)[1]


def is_disposable_email(email: str) -> bool:
    domain = _extract_email_domain(email)
    if not domain:
        return False

    disposable = get_disposable_domains()
    return any(candidate in disposable for candidate in _iter_domain_candidates(domain))
