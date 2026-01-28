"""Compatibility shim.

Implementation moved to app.authn.csrf.
"""

from .authn.csrf import CSRF_HEADER, enforce_csrf, generate_csrf_token
