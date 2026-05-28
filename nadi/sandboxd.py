"""Sandbox tier and credentials proxy for the local MVP."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .security import SessionJWT
from .store import Store


class CredentialsProxy:
    """Issues fake, scoped credentials and records every exchange in audit_log."""

    def __init__(self, store: Store, jwt: SessionJWT):
        self.store = store
        self.jwt = jwt

    def exchange(self, session_id: str, token: str, audience: str = "tool") -> dict[str, Any]:
        self.jwt.verify(token, session_id, "tool")
        cred = {"access_token": f"fake-{session_id[:8]}-{audience}", "audience": audience}
        self.store.audit("credentials-proxy", "exchange", session_id, {"audience": audience})
        return cred


class Sandboxd:
    def __init__(self, store: Store, jwt: SessionJWT, root: str | None = None):
        self.store = store
        self.jwt = jwt
        self.root = Path(root or ".").resolve()
        self.credentials = CredentialsProxy(store, jwt)
        self.tool_calls = 0

    def execute_tool(self, session_id: str, token: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.jwt.verify(token, session_id, "tool")
        args = args or {}
        self.tool_calls += 1
        if name == "echo":
            result = {"text": str(args.get("text", ""))}
        elif name == "uppercase":
            result = {"text": str(args.get("text", "")).upper()}
        elif name == "list_files":
            rel = str(args.get("path", "."))
            target = (self.root / rel).resolve()
            # Check that the resolved path is the root itself or strictly inside it.
            # str.startswith on the string form avoids the /tmp/foo vs /tmp/foobar
            # false-positive that target.parents membership checks can miss.
            root_prefix = str(self.root)
            target_str = str(target)
            inside = target_str == root_prefix or target_str.startswith(root_prefix + "/")
            if not inside:
                raise ValueError("path escapes sandbox root")
            result = {"files": sorted(p.name for p in target.iterdir()) if target.is_dir() else []}
        elif name == "credential":
            result = self.credentials.exchange(session_id, token, str(args.get("audience", "tool")))
        else:
            raise ValueError(f"unknown tool: {name}")
        self.store.audit("sandboxd", "execute_tool", session_id, {"tool": name})
        return {"tool": name, "result": result}
