import os

# The core list of reputable, non-disposable email providers
CORE_ALLOWED_DOMAINS = {
    # Google
    "gmail.com", "googlemail.com",
    # Microsoft
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    # Yahoo
    "yahoo.com", "ymail.com", "rocketmail.com",
    # Apple
    "icloud.com", "me.com", "mac.com",
    # Privacy / Secure
    "protonmail.com", "proton.me", "pm.me", "tutanota.com",
    # Other Major Portals
    "aol.com", "zoho.com"
}

def get_allowed_email_domains() -> set[str]:
    """
    Returns the set of explicitly allowed email domains.
    Includes the core list + any custom domains specified in AUTH_EXTRA_ALLOWED_DOMAINS.
    """
    allowed = set(CORE_ALLOWED_DOMAINS)
    
    # Check for custom overrides from the env
    extra_str = os.getenv("AUTH_EXTRA_ALLOWED_DOMAINS", "").strip()
    if extra_str:
        extra_domains = [d.strip().lower() for d in extra_str.split(",") if d.strip()]
        allowed.update(extra_domains)
        
    return allowed

def is_email_allowed(email: str) -> bool:
    """
    Returns True if the email domain is explicitly allowed.
    Returns False if the email is invalid or the domain is not in the allowed list.
    """
    if not email or "@" not in email:
        return False
        
    domain = email.split("@")[-1].lower().strip()
    return domain in get_allowed_email_domains()
