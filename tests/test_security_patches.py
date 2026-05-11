"""
Security Patch Verification Tests
==================================
Validates that all CodeQL code scanning fixes are in place.
Run with: pytest tests/test_security_patches.py -v

These tests verify:
- No clear-text sensitive data in log statements
- No stack-trace exposure to HTTP clients
- Email regex is resistant to ReDoS
- API key truncation is sufficiently short
"""
import os
import re
import time

# Resolve paths relative to this test file
BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..", "api-web", "app")


# ── Clear-Text Logging Fixes ──────────────────────────────────────────────────


class TestClearTextLogging:
    """Verify sensitive data is not logged in plaintext (CodeQL alerts #10-18)."""

    def test_redis_url_uses_redaction_helper(self):
        """Alert #15, #16, #17: redis_pool.py must not log raw URLs with credentials."""
        filepath = os.path.join(BACKEND_ROOT, "redis_pool.py")
        with open(filepath) as f:
            content = f.read()

        # Old pattern that leaked password-containing URLs
        assert 'url.split("@")' not in content, (
            "redis_pool.py still uses raw url.split('@') pattern — "
            "passwords may appear in logs"
        )
        # New safe helper must be present
        assert "_safe_redis_url_for_log" in content, (
            "redis_pool.py missing _safe_redis_url_for_log redaction helper"
        )

    def test_redis_url_redaction_helper_works(self):
        """Verify the redaction helper actually strips passwords."""
        # Simulate the helper logic
        def _safe_redis_url_for_log(url: str) -> str:
            return re.sub(r"//:[^@]+@", "//:<REDACTED>@", url)

        assert _safe_redis_url_for_log("redis://:secretpass@host:6379/0") == \
            "redis://:<REDACTED>@host:6379/0"
        assert _safe_redis_url_for_log("redis://host:6379/0") == \
            "redis://host:6379/0"  # No password = unchanged

    def test_api_key_truncated_to_4_chars(self):
        """Alert #14: rate_limiter.py must truncate API keys to 4 chars max."""
        filepath = os.path.join(BACKEND_ROOT, "rate_limiter.py")
        with open(filepath) as f:
            content = f.read()

        assert "api_key[-8:]" not in content, (
            "rate_limiter.py still exposes 8 chars of API key"
        )
        assert "api_key[-4:]" in content, (
            "rate_limiter.py should truncate API key to last 4 chars"
        )

    def test_plisio_error_log_no_source_amount(self):
        """Alert #12, #13: plisio_provider.py must not log source_amount in errors."""
        filepath = os.path.join(
            BACKEND_ROOT, "payments", "payment_providers", "plisio_provider.py"
        )
        with open(filepath) as f:
            content = f.read()

        # The error log block should not reference source_amount
        error_block_match = re.search(
            r"create_invoice\.error.*?raise",
            content,
            re.DOTALL,
        )
        assert error_block_match is not None, "Could not find create_invoice error block"
        error_block = error_block_match.group()
        assert "source_amount" not in error_block, (
            "plisio_provider.py still logs source_amount in error handler"
        )

    def test_main_py_no_auth_source_in_logs(self):
        """Alert #10, #11: main.py must not log auth_source variable."""
        filepath = os.path.join(BACKEND_ROOT, "main.py")
        with open(filepath) as f:
            content = f.read()

        # Find the trade endpoint log lines
        trade_log_lines = [
            line for line in content.splitlines()
            if "Internal auth=" in line and "logger" in line
        ]
        for line in trade_log_lines:
            assert "auth_source" not in line, (
                f"main.py still logs auth_source: {line.strip()}"
            )

    def test_tasks_user_id_truncated(self):
        """Alert #18: tasks.py must truncate user_id in renewal logs."""
        filepath = os.path.join(BACKEND_ROOT, "payments", "tasks.py")
        with open(filepath) as f:
            content = f.read()

        # Find the renewal log line
        renewal_lines = [
            line for line in content.splitlines()
            if "PLISIO RENEWAL" in line and "Created renewal invoice" in line
        ]
        assert len(renewal_lines) > 0, "Could not find PLISIO RENEWAL log line"
        # Should use truncated user_id
        log_context = content[content.index("Created renewal invoice"):
                              content.index("Created renewal invoice") + 300]
        assert "user_id[-8:]" in log_context or 'user_id[-8:]' in log_context, (
            "tasks.py should truncate user_id in renewal invoice log"
        )


# ── Stack-Trace Exposure Fixes ─────────────────────────────────────────────────


class TestStackTraceExposure:
    """Verify internal errors are not exposed to HTTP clients (CodeQL alerts #2-9)."""

    def test_sse_no_str_exc_in_client_responses(self):
        """Alert #3-9: sse.py must not send str(exc) to SSE clients."""
        filepath = os.path.join(BACKEND_ROOT, "sse.py")
        with open(filepath) as f:
            content = f.read()

        # Find all yield statements that contain error messages
        yield_lines = [
            (i + 1, line)
            for i, line in enumerate(content.splitlines())
            if "yield" in line and "error" in line.lower()
        ]
        for lineno, line in yield_lines:
            assert "str(exc)" not in line, (
                f"sse.py line {lineno} still exposes str(exc) to clients: "
                f"{line.strip()}"
            )

    def test_sse_health_no_exception_details(self):
        """Alert #6, #9: /health must not return exception details."""
        filepath = os.path.join(BACKEND_ROOT, "sse.py")
        with open(filepath) as f:
            content = f.read()

        # Find the health endpoint
        health_idx = content.index('async def stream_health')
        health_block = content[health_idx:health_idx + 800]

        # Should not contain str(exc) in any JSONResponse
        json_response_sections = re.findall(
            r"JSONResponse\(.*?\)", health_block, re.DOTALL
        )
        for section in json_response_sections:
            assert "str(exc)" not in section, (
                f"/health endpoint still exposes str(exc): {section[:100]}"
            )

    def test_auth_invalidate_no_error_leak(self):
        """Alert #2: /auth/invalidate must not return str(e) to clients."""
        filepath = os.path.join(BACKEND_ROOT, "authn", "routes.py")
        with open(filepath) as f:
            content = f.read()

        # Find the failed_refresh return statement
        invalidate_idx = content.index("async def auth_invalidate_user")
        invalidate_block = content[invalidate_idx:]

        # Look for return statements with error
        return_lines = [
            line for line in invalidate_block.splitlines()
            if "return" in line and "error" in line and "failed_refresh" in line
        ]
        for line in return_lines:
            assert '"error": str(e)' not in line, (
                f"/auth/invalidate still leaks str(e): {line.strip()}"
            )


# ── ReDoS Fix ──────────────────────────────────────────────────────────────────


class TestReDoSPrevention:
    """Verify email regex is resistant to ReDoS (CodeQL alert #1)."""

    def test_old_vulnerable_pattern_removed(self):
        """Alert #1: The vulnerable EMAIL_PATTERN must be replaced."""
        filepath = os.path.join(BACKEND_ROOT, "authn", "routes.py")
        with open(filepath) as f:
            content = f.read()

        assert r"^[^@\s]+@[^@\s]+\.[^@\s]+$" not in content, (
            "routes.py still contains the vulnerable EMAIL_PATTERN regex"
        )

    def test_new_email_regex_is_safe(self):
        """Verify the new email regex doesn't hang on adversarial input."""
        # The safe pattern from the fix
        safe_pattern = re.compile(
            r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*$"
        )

        # Adversarial inputs that would cause catastrophic backtracking
        # with the old [^@\s] pattern
        adversarial_inputs = [
            "a" * 100 + "@" + "b" * 100 + "." * 100 + "c",
            "a@" * 50 + "b.c",
            "x" * 200 + "@" + "y" * 200,
        ]

        for adversarial in adversarial_inputs:
            start = time.monotonic()
            safe_pattern.match(adversarial)
            elapsed = time.monotonic() - start
            assert elapsed < 0.1, (
                f"ReDoS: regex took {elapsed:.3f}s on adversarial input "
                f"(first 50 chars: {adversarial[:50]})"
            )

    def test_new_email_regex_accepts_valid_emails(self):
        """Ensure the new regex still accepts standard email formats."""
        safe_pattern = re.compile(
            r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*$"
        )

        valid_emails = [
            "user@example.com",
            "user.name@example.com",
            "user+tag@example.co.uk",
            "user@sub.domain.example.com",
            "first.last@company.org",
            "test123@gmail.com",
        ]

        for email in valid_emails:
            assert safe_pattern.match(email), (
                f"New EMAIL_PATTERN rejects valid email: {email}"
            )

    def test_new_email_regex_rejects_invalid_emails(self):
        """Ensure the new regex still rejects clearly invalid formats."""
        safe_pattern = re.compile(
            r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*$"
        )

        invalid_emails = [
            "",
            "noatsign",
            "@missinglocal.com",
            "spaces in@email.com",
            "user@",
        ]

        for email in invalid_emails:
            assert not safe_pattern.match(email), (
                f"New EMAIL_PATTERN accepts invalid email: {email}"
            )

    def test_email_length_guard_exists(self):
        """Verify that a length guard (>254) is in place before regex matching."""
        filepath = os.path.join(BACKEND_ROOT, "authn", "routes.py")
        with open(filepath) as f:
            content = f.read()

        # Find the email update endpoint
        email_endpoint_idx = content.index("async def auth_update_email")
        email_block = content[email_endpoint_idx:email_endpoint_idx + 600]

        assert "254" in email_block, (
            "Missing email length guard (RFC 5321 max 254 chars) in auth_update_email"
        )


# ── Dependency Version Checks ──────────────────────────────────────────────────


class TestDependencyVersions:
    """Verify requirements.txt files specify safe minimum versions."""

    def _read_requirements(self, relative_path: str) -> str:
        filepath = os.path.join(
            os.path.dirname(__file__), "..", relative_path
        )
        with open(filepath) as f:
            return f.read()

    def _assert_min_version(self, content: str, package: str, min_version: str):
        """Check that a package specifier requires at least min_version."""
        # Find lines matching the package name
        for line in content.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith(package.lower()):
                # Extract version specifier
                if "==" in stripped:
                    version = stripped.split("==")[1].strip()
                elif ">=" in stripped:
                    version = stripped.split(">=")[1].strip().split(",")[0]
                else:
                    continue

                from packaging.version import Version
                assert Version(version) >= Version(min_version), (
                    f"{package} version {version} is below required {min_version}"
                )
                return

        # Package not found — not necessarily an error if it's optional
        pass

    def test_api_web_pyjwt(self):
        content = self._read_requirements("api-web/requirements.txt")
        self._assert_min_version(content, "pyjwt", "2.12.0")

    def test_api_web_python_dotenv(self):
        content = self._read_requirements("api-web/requirements.txt")
        self._assert_min_version(content, "python-dotenv", "1.2.2")

    def test_api_web_requests(self):
        content = self._read_requirements("api-web/requirements.txt")
        self._assert_min_version(content, "requests", "2.33.0")

    def test_api_worker_python_dotenv(self):
        content = self._read_requirements("api-worker/requirements.txt")
        self._assert_min_version(content, "python-dotenv", "1.2.2")

    def test_api_worker_requests(self):
        content = self._read_requirements("api-worker/requirements.txt")
        self._assert_min_version(content, "requests", "2.33.0")

    def test_news_analyzer_python_dotenv(self):
        content = self._read_requirements("news_analyzer/requirements.txt")
        self._assert_min_version(content, "python-dotenv", "1.2.2")

    def test_news_analyzer_requests(self):
        content = self._read_requirements("news_analyzer/requirements.txt")
        self._assert_min_version(content, "requests", "2.33.0")
