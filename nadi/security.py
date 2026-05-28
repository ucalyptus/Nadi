"""Small stdlib HMAC JWT implementation for session-scoped tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store

_MAX_TTL_SECONDS = 86400  # 24 hours hard ceiling


class JWTError(ValueError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class SessionJWT:
    def __init__(self, secret: str = "local-nadi-secret"):
        self.secret = secret.encode()

    def sign(self, session_id: str, scope: str, ttl_seconds: int = 300, subject: str = "cell") -> str:
        # Enforce a hard ceiling on token lifetime; negative values are allowed
        # (they produce already-expired tokens, useful in tests and deliberate short-circuits).
        ttl_seconds = min(ttl_seconds, _MAX_TTL_SECONDS)
        exp = int(time.time()) + ttl_seconds
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {"sub": subject, "sid": session_id, "scope": scope, "exp": exp, "jti": str(uuid.uuid4())}
        body = f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}.{_b64e(json.dumps(payload, separators=(',', ':')).encode())}"
        sig = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64e(sig)}"

    def verify(self, token: str, session_id: str, scope: str, store: "Store | None" = None) -> dict[str, Any]:
        try:
            head, payload_b64, sig = token.split(".")
        except ValueError as exc:
            raise JWTError("malformed token") from exc

        # Validate algorithm before anything else — rejects alg:none and other
        # unexpected algorithms even though the HMAC comparison would also catch
        # a forged empty signature (defence in depth).
        try:
            header_data = json.loads(_b64d(head))
        except Exception as exc:
            raise JWTError("malformed header") from exc
        if header_data.get("alg") != "HS256":
            raise JWTError("unsupported algorithm")

        # Constant-time signature check.
        expected = _b64e(hmac.new(self.secret, f"{head}.{payload_b64}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            raise JWTError("bad signature")

        try:
            data = json.loads(_b64d(payload_b64))
        except Exception as exc:
            raise JWTError("malformed payload") from exc

        if data.get("exp", 0) < int(time.time()):
            raise JWTError("token expired")
        if data.get("sid") != session_id:
            raise JWTError("wrong session")
        scopes = set(str(data.get("scope", "")).split())
        if scope not in scopes and data.get("scope") != "*":
            raise JWTError("missing scope")
        if store is not None:
            jti = data.get("jti")
            if jti and store.is_token_revoked(jti):
                raise JWTError("token revoked")
        return data
