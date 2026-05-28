"""Base types and response-extraction utilities shared by all platform adapters."""
from __future__ import annotations

import re
from typing import Any, Protocol

# Slack/Discord control sequences that could inject mentions or channel links.
_PLATFORM_CONTROLS = re.compile(r"<[@#!][^>]*>")


def _sanitize(text: str) -> str:
    """Strip platform control sequences and limit length."""
    text = _PLATFORM_CONTROLS.sub("", text)
    # Remove backtick runs that could break code-span formatting.
    text = text.replace("`", "'")
    return text[:4000]  # hard cap per-segment to stay within platform limits


class GatewayProtocol(Protocol):
    def get_or_create_channel_session(
        self,
        platform: str,
        channel_id: str,
        thread_id: str,
        tenant_id: str,
        initiator_resource_id: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    def send_command(
        self,
        session_id: str,
        command_type: str,
        payload: dict[str, Any] | None,
        actor_resource_id: str | None,
    ) -> dict[str, Any]: ...


def extract_reply_text(
    events: list[dict[str, Any]],
    *,
    include_tool_results: bool = False,
) -> str:
    """Pull human-readable text from a list of Nadi events.

    Tool results are excluded by default — they contain internal implementation
    details that should not be forwarded to end users in a shared channel.
    Pass include_tool_results=True explicitly only when the channel is trusted.
    """
    parts: list[str] = []
    for ev in events:
        payload = ev.get("payload", {})
        et = ev.get("event_type", "")
        if et in ("assistant.message", "model.response"):
            text = _sanitize(str(payload.get("text", "")))
            if text:
                parts.append(text)
        elif et == "tool.result" and include_tool_results:
            result = payload.get("result", {})
            tool_name = _sanitize(str(payload.get("tool", "tool")))
            text = _sanitize(str(result.get("text", "")))
            if text:
                parts.append(f"[{tool_name}] {text}")
        elif et == "command.error":
            error = _sanitize(str(payload.get("error", "unknown error")))
            parts.append(f"Error: {error}")
    return "\n".join(parts) if parts else ""


def make_resource_id(platform: str, user_id: str) -> str:
    return f"{platform}:{user_id}"
