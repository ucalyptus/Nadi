"""Small stdlib HMAC JWT implementation for session-scoped tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


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
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {"sub": subject, "sid": session_id, "scope": scope, "exp": int(time.time()) + ttl_seconds}
        body = f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}.{_b64e(json.dumps(payload, separators=(',', ':')).encode())}"
        sig = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64e(sig)}"

    def verify(self, token: str, session_id: str, scope: str) -> dict[str, Any]:
        try:
            head, payload, sig = token.split(".")
        except ValueError as exc:
            raise JWTError("malformed token") from exc
        expected = _b64e(hmac.new(self.secret, f"{head}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            raise JWTError("bad signature")
        data = json.loads(_b64d(payload))
        if data.get("exp", 0) < int(time.time()):
            raise JWTError("token expired")
        if data.get("sid") != session_id:
            raise JWTError("wrong session")
        scopes = set(str(data.get("scope", "")).split())
        if scope not in scopes and data.get("scope") != "*":
            raise JWTError("missing scope")
        return data
