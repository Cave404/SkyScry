"""Hardened authentication, PBKDF2/scrypt password hashing, dual Cookie + X-SkyScry-Token session security, and brute-force rate limiting for SkyScry (v0.0.11)."""

from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from flask import Response, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

if TYPE_CHECKING:
    from src.db import WeatherDatabase

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 300.0  # 5 minutes
LOCKOUT_DURATION_SECONDS = 300.0   # 5 minutes temporary lockout
PBKDF2_ITERATIONS = 600000


def hash_admin_password(password: str) -> str:
    """Hash an admin password using werkzeug scrypt or Python PBKDF2-HMAC-SHA256 (600,000 iterations)."""
    cleaned = (password or "").strip()
    if len(cleaned) < 6:
        raise ValueError("Admin password must be at least 6 characters long.")
    try:
        return generate_password_hash(cleaned, method="scrypt")
    except Exception:
        salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            cleaned.encode("utf-8"),
            salt.encode("utf-8"),
            PBKDF2_ITERATIONS,
        )
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_admin_password(password: str, stored_hash: str) -> bool:
    """Verify a candidate password against a stored scrypt or PBKDF2-HMAC-SHA256 hash in constant time."""
    if not password or not stored_hash:
        return False
    cleaned = password.strip()
    if stored_hash.startswith("pbkdf2_sha256$"):
        parts = stored_hash.split("$")
        if len(parts) != 4:
            return False
        try:
            iterations = int(parts[1])
            salt = parts[2]
            expected_hex = parts[3]
            dk = hashlib.pbkdf2_hmac(
                "sha256",
                cleaned.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            )
            return hmac.compare_digest(dk.hex(), expected_hex)
        except Exception:
            return False
    try:
        return check_password_hash(stored_hash, cleaned)
    except Exception:
        return False


class BruteForceRateLimiter:
    """Thread-safe in-memory IP rate limiter for `/api/auth/unlock` (max 5 failed attempts per 5 minutes)."""

    def __init__(
        self,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_DURATION_SECONDS,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._lockouts: dict[str, float] = {}

    def _prune_ip(self, ip: str, now: float) -> None:
        lock_until = self._lockouts.get(ip)
        if lock_until is not None and now >= lock_until:
            del self._lockouts[ip]

        attempts = self._failures.get(ip)
        if attempts:
            cutoff = now - self.window_seconds
            valid = [ts for ts in attempts if ts >= cutoff]
            if valid:
                self._failures[ip] = valid
            else:
                del self._failures[ip]

    def check_allowed(self, ip: str, now: Optional[float] = None) -> tuple[bool, int]:
        """Return `(allowed, retry_after_seconds)` for the given client IP."""
        curr = time.monotonic() if now is None else float(now)
        key = (ip or "unknown").strip()
        with self._lock:
            self._prune_ip(key, curr)
            lock_until = self._lockouts.get(key)
            if lock_until is not None and curr < lock_until:
                retry_sec = max(1, int(lock_until - curr))
                return False, retry_sec
            return True, 0

    def record_failure(self, ip: str, now: Optional[float] = None) -> tuple[int, int]:
        """Record a failed login attempt and return `(remaining_attempts, lockout_seconds)`."""
        curr = time.monotonic() if now is None else float(now)
        key = (ip or "unknown").strip()
        with self._lock:
            self._prune_ip(key, curr)
            history = self._failures.setdefault(key, [])
            history.append(curr)
            if len(history) >= self.max_attempts:
                self._lockouts[key] = curr + self.lockout_seconds
                return 0, int(self.lockout_seconds)
            remaining = max(0, self.max_attempts - len(history))
            return remaining, 0

    def record_success(self, ip: str) -> None:
        """Clear failure counters and lockouts for an IP upon successful authentication."""
        key = (ip or "unknown").strip()
        with self._lock:
            self._failures.pop(key, None)
            self._lockouts.pop(key, None)


class SecurityManager:
    """Manages persistent secret signing keys, admin password setup, dual Cookie + X-SkyScry-Token sessions, and auth decorators."""

    def __init__(
        self,
        db: WeatherDatabase,
        fallback_pin: Optional[str] = None,
        read_only_public: bool = False,
    ) -> None:
        self.db = db
        self.fallback_pin = fallback_pin.strip() if fallback_pin and fallback_pin.strip() else None
        self.read_only_public = bool(read_only_public)
        self.rate_limiter = BruteForceRateLimiter()
        self._token_lock = threading.Lock()
        self._active_tokens: dict[str, float] = {}  # token -> last_active_epoch
        self._signing_key: Optional[bytes] = None

    def ensure_secret_signing_key(self) -> bytes:
        """Load or generate a persistent 32-byte cryptographic `secret_signing_key` in `system_settings`."""
        if self._signing_key is not None:
            return self._signing_key

        raw_hex = self.db.get_setting("secret_signing_key")
        if raw_hex:
            try:
                key_bytes = bytes.fromhex(raw_hex.strip())
                if len(key_bytes) >= 32:
                    self._signing_key = key_bytes
                    return key_bytes
            except ValueError:
                pass

        new_key = secrets.token_bytes(32)
        self.db.set_setting("secret_signing_key", new_key.hex())
        self._signing_key = new_key
        return new_key

    def is_password_configured(self) -> bool:
        """Return True if an encrypted `admin_password_hash` is stored in `system_settings`."""
        stored = self.db.get_setting("admin_password_hash")
        return bool(stored and stored.strip())

    def get_inactivity_timeout_minutes(self) -> int:
        """Return configured session inactivity timeout in minutes (default 15, bounded 1..240)."""
        raw = self.db.get_setting("inactivity_timeout_minutes", "15")
        try:
            val = int(float(raw or "15"))
            return max(1, min(val, 240))
        except (ValueError, TypeError):
            return 15

    def _issue_signed_token(self) -> str:
        """Generate an HMAC-SHA256 signed admin session token and register its activity timestamp."""
        key = self.ensure_secret_signing_key()
        now_ts = time.time()
        nonce = secrets.token_hex(16)
        payload = f"{int(now_ts)}.{nonce}"
        sig = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token = f"{payload}.{sig}"
        with self._token_lock:
            self._active_tokens[token] = now_ts
        return token

    def _verify_and_check_token(self, token: str, now_ts: float, timeout_sec: float) -> tuple[bool, Optional[int]]:
        """Verify an `X-SkyScry-Token` header token and check its inactivity window."""
        if not token or "." not in token:
            return False, None
        parts = token.strip().split(".")
        if len(parts) != 3:
            return False, None
        ts_str, nonce, sig = parts
        try:
            issued_ts = float(int(ts_str))
        except ValueError:
            return False, None

        key = self.ensure_secret_signing_key()
        payload = f"{ts_str}.{nonce}"
        expected_sig = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, None

        with self._token_lock:
            last_active = self._active_tokens.get(token, issued_ts)
            # If the token was explicitly revoked via lock_session(), _active_tokens stores -1.0
            if last_active < 0:
                return False, None
            if (now_ts - last_active) <= timeout_sec:
                rem = max(0, int(timeout_sec - (now_ts - last_active)))
                return True, rem
            # Expired
            self._active_tokens.pop(token, None)
            return False, None

    def get_request_token(self) -> str:
        """Extract `X-SkyScry-Token` from request headers if present."""
        return (request.headers.get("X-SkyScry-Token") or "").strip()

    def is_session_unlocked(self) -> bool:
        """Check if the current `X-SkyScry-Token`, Flask session cookie, or legacy header is unlocked and within inactivity timeout."""
        now_ts = time.time()
        timeout_sec = self.get_inactivity_timeout_minutes() * 60.0

        # 1. Check X-SkyScry-Token header (works across Cloudflare Tunnel, Brave Shields, and LAN)
        hdr_token = self.get_request_token()
        if hdr_token:
            ok, _ = self._verify_and_check_token(hdr_token, now_ts, timeout_sec)
            if ok:
                return True

        # 2. Check Flask signed session cookie
        if session.get("admin_unlocked") is True:
            last_active = float(session.get("admin_last_active") or 0.0)
            if now_ts - last_active <= timeout_sec:
                return True
            session.pop("admin_unlocked", None)
            session.pop("admin_last_active", None)
            session.pop("admin_token", None)

        # 3. Support header-based password/PIN for headless API callers
        candidate_hdr = (
            request.headers.get("X-Admin-Password")
            or request.headers.get("X-Admin-Pin")
            or ""
        ).strip()
        if candidate_hdr:
            stored_hash = self.db.get_setting("admin_password_hash")
            if stored_hash and verify_admin_password(candidate_hdr, stored_hash):
                return True
            eff_pin = self.fallback_pin or os.environ.get("ADMIN_PIN", "").strip()
            if eff_pin and hmac.compare_digest(candidate_hdr, eff_pin):
                return True

        return False

    def touch_admin_session(self) -> None:
        """Refresh the admin session activity timestamp on admin interaction (both cookie and token)."""
        now_ts = time.time()
        hdr_token = self.get_request_token()
        if hdr_token:
            with self._token_lock:
                if hdr_token in self._active_tokens and self._active_tokens[hdr_token] > 0:
                    self._active_tokens[hdr_token] = now_ts

        if session.get("admin_unlocked") is True:
            session["admin_last_active"] = now_ts
            session.modified = True

    def unlock_session(self) -> str:
        """Mark the current Flask session as unlocked AND issue a signed `X-SkyScry-Token`."""
        now_ts = time.time()
        token = self._issue_signed_token()
        session.permanent = True
        session["admin_unlocked"] = True
        session["admin_last_active"] = now_ts
        session["admin_token"] = token
        session.modified = True
        return token

    def lock_session(self) -> None:
        """Immediately lock both the `X-SkyScry-Token` and the current Flask session cookie."""
        hdr_token = self.get_request_token()
        sess_token = session.get("admin_token")
        with self._token_lock:
            if hdr_token:
                self._active_tokens[hdr_token] = -1.0
            if sess_token:
                self._active_tokens[str(sess_token)] = -1.0
        session.pop("admin_unlocked", None)
        session.pop("admin_last_active", None)
        session.pop("admin_token", None)
        session.modified = True

    def get_auth_status(self, issued_token: Optional[str] = None) -> dict[str, Any]:
        """Compile `/api/auth/status` payload."""
        now_ts = time.time()
        pw_configured = self.is_password_configured()
        timeout_min = self.get_inactivity_timeout_minutes()
        timeout_sec = timeout_min * 60.0
        unlocked = self.is_session_unlocked()

        remaining_sec: Optional[int] = None
        active_token: Optional[str] = issued_token

        if unlocked:
            hdr_token = self.get_request_token()
            if hdr_token:
                ok, rem = self._verify_and_check_token(hdr_token, now_ts, timeout_sec)
                if ok:
                    remaining_sec = rem
                    if not active_token:
                        active_token = hdr_token
            if remaining_sec is None and session.get("admin_unlocked") is True:
                last_active = float(session.get("admin_last_active") or now_ts)
                remaining_sec = max(0, int(timeout_sec - (now_ts - last_active)))
                if not active_token and session.get("admin_token"):
                    active_token = str(session.get("admin_token"))

        payload: dict[str, Any] = {
            "ok": True,
            "password_configured": pw_configured,
            "unlocked": unlocked,
            "timeout_minutes": timeout_min,
            "remaining_seconds": remaining_sec,
        }
        if active_token:
            payload["token"] = active_token
        return payload


def require_admin_auth(security_mgr: SecurityManager) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator `@require_admin_auth` blocking mutating endpoints when locked with HTTP 401/403."""

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            body = request.get_json(silent=True) if request.is_json else None
            body_pw = ""
            if isinstance(body, dict):
                raw_pw = body.get("password") or body.get("admin_pin")
                if raw_pw is not None:
                    body_pw = str(raw_pw).strip()

            if security_mgr.is_session_unlocked():
                security_mgr.touch_admin_session()
                return view_func(*args, **kwargs)

            if body_pw:
                stored_hash = security_mgr.db.get_setting("admin_password_hash")
                if stored_hash and verify_admin_password(body_pw, stored_hash):
                    security_mgr.unlock_session()
                    return view_func(*args, **kwargs)
                eff_pin = security_mgr.fallback_pin or os.environ.get("ADMIN_PIN", "").strip()
                if eff_pin and hmac.compare_digest(body_pw, eff_pin):
                    security_mgr.unlock_session()
                    return view_func(*args, **kwargs)

            if not security_mgr.is_password_configured():
                eff_pin = security_mgr.fallback_pin or os.environ.get("ADMIN_PIN", "").strip()
                eff_ro = security_mgr.read_only_public or (
                    os.environ.get("READ_ONLY_PUBLIC", "").strip().lower() in ("1", "true", "yes", "on")
                )
                if eff_pin:
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": "Admin authentication required to perform this action.",
                                "auth_required": True,
                                "locked": True,
                            }
                        ),
                        401,
                    )
                if eff_ro:
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": "Public Read-Only mode is active. Set up or unlock Admin controls to modify settings.",
                                "read_only": True,
                                "locked": True,
                            }
                        ),
                        403,
                    )
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "Admin security password has not been created yet. Please complete first-run password setup.",
                            "setup_required": True,
                            "locked": True,
                        }
                    ),
                    403,
                )

            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Admin controls are locked. Please unlock with your Admin Password.",
                        "auth_required": True,
                        "locked": True,
                    }
                ),
                401,
            )

        return wrapped

    return decorator
