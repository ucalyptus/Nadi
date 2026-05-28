"""Slack adapter for Nadi.

Requires:  pip install slack-bolt

Usage:
    from nadi.runtime import local_stack
    from nadi.adapters.slack_adapter import SlackAdapter

    stack = local_stack("nadi.db")
    adapter = SlackAdapter(
        stack["gateway"],
        bot_token="xoxb-...",
        signing_secret="...",
        port=3000,
    )
    adapter.run()  # blocking, starts Socket Mode or HTTP server
"""
from __future__ import annotations

import logging
from typing import Any

from .base import extract_reply_text, make_resource_id

log = logging.getLogger(__name__)


class SlackAdapter:
    """Connects a Nadi Gateway to a Slack app via Slack Bolt.

    Thread semantics: a top-level channel message uses channel_id as the
    thread_id anchor; replies inside a thread use the thread_ts as thread_id.
    This gives each Slack thread its own Nadi session — matching the 1:1
    thread↔session invariant from Mastra's channel design.

    For local dev, set SLACK_APP_TOKEN (xapp-...) and use socket_mode=True.
    For production, expose the HTTP handler behind a public URL.
    """

    def __init__(
        self,
        gateway: Any,
        bot_token: str,
        signing_secret: str,
        tenant_id: str = "slack",
        command_type: str = "model",
        port: int = 3000,
        socket_mode: bool = False,
        app_token: str | None = None,
    ):
        try:
            from slack_bolt import App  # type: ignore[import]
        except ImportError as e:
            raise ImportError("pip install slack-bolt") from e

        self._gateway = gateway
        self._tenant_id = tenant_id
        self._command_type = command_type
        self._port = port
        self._socket_mode = socket_mode
        self._app_token = app_token

        self._app = App(token=bot_token, signing_secret=signing_secret)

        @self._app.event("message")
        def on_message(body: dict[str, Any], say: Any) -> None:
            event = body.get("event", {})
            # Ignore bot messages and message edits.
            if event.get("bot_id") or event.get("subtype"):
                return
            self._handle(event, say)

        @self._app.event("app_mention")
        def on_mention(body: dict[str, Any], say: Any) -> None:
            event = body.get("event", {})
            self._handle(event, say)

    _MAX_TEXT = 8000  # chars — truncate before sending to the model

    def _handle(self, event: dict[str, Any], say: Any) -> None:
        channel_id = event.get("channel", "")
        # thread_ts is the root message timestamp and is stable for all replies
        # in a Slack thread. Top-level messages have no thread_ts, so we use ""
        # — meaning all top-level messages in a channel share one session, and
        # each thread gets its own session. Do NOT fall back to event["ts"]:
        # that would give every top-level message its own isolated session,
        # breaking conversation continuity within the channel.
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "unknown")
        text = str(event.get("text", ""))[:self._MAX_TEXT]
        resource_id = make_resource_id("slack", user_id)

        try:
            routing = self._gateway.get_or_create_channel_session(
                platform="slack",
                channel_id=channel_id,
                thread_id=thread_ts,
                tenant_id=self._tenant_id,
                initiator_resource_id=resource_id,
                metadata={"team": event.get("team")},
            )
            session_id = routing["session_id"]
            result = self._gateway.send_command(
                session_id,
                self._command_type,
                {"text": text, "prompt": text},
                actor_resource_id=resource_id,
            )
            reply = extract_reply_text(result.get("events", []))
            if reply:
                # Reply in-thread to keep Slack tidy.
                say(reply, thread_ts=thread_ts)
        except Exception:
            log.exception("error handling Slack message in session routing")

    def run(self) -> None:
        """Start the Slack app (blocking)."""
        if self._socket_mode:
            try:
                from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore[import]
            except ImportError as e:
                raise ImportError("pip install slack-bolt[adapter]") from e
            if not self._app_token:
                raise ValueError("app_token required for socket_mode=True")
            handler = SocketModeHandler(self._app, self._app_token)
            handler.start()
        else:
            self._app.start(port=self._port)
