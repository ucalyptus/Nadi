"""Telegram adapter for Nadi.

Requires:  pip install python-telegram-bot

Usage:
    from nadi.runtime import local_stack
    from nadi.adapters.telegram_adapter import TelegramAdapter

    stack = local_stack("nadi.db")
    adapter = TelegramAdapter(stack["gateway"], bot_token="YOUR_TOKEN")
    adapter.run()  # blocking, uses long-polling
"""
from __future__ import annotations

import logging
from typing import Any

from .base import extract_reply_text, make_resource_id

log = logging.getLogger(__name__)


class TelegramAdapter:
    """Connects a Nadi Gateway to a Telegram bot via python-telegram-bot.

    Thread semantics: Telegram's chat_id is used as channel_id; message
    thread_id (forum topics) is used as thread_id when present, defaulting
    to "" for direct chats. Each chat / topic gets its own Nadi session.
    """

    def __init__(
        self,
        gateway: Any,
        bot_token: str,
        tenant_id: str = "telegram",
        command_type: str = "model",
    ):
        try:
            from telegram.ext import Application, MessageHandler, filters  # type: ignore[import]
        except ImportError as e:
            raise ImportError("pip install python-telegram-bot") from e

        self._gateway = gateway
        self._tenant_id = tenant_id
        self._command_type = command_type

        self._app = Application.builder().token(bot_token).build()

        from telegram.ext import MessageHandler, filters  # type: ignore[import]
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle))

    async def _handle(self, update: Any, context: Any) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        channel_id = str(chat.id)
        # message_thread_id is set for forum topic replies; blank for normal chats.
        thread_id = str(message.message_thread_id) if message.message_thread_id else ""
        resource_id = make_resource_id("telegram", str(user.id))
        text = message.text or ""

        try:
            routing = self._gateway.get_or_create_channel_session(
                platform="telegram",
                channel_id=channel_id,
                thread_id=thread_id,
                tenant_id=self._tenant_id,
                initiator_resource_id=resource_id,
                metadata={"chat_type": chat.type},
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
                await message.reply_text(reply)
        except Exception:
            log.exception("error handling Telegram message in session routing")

    def run(self) -> None:
        """Start the Telegram bot (blocking, long-poll)."""
        self._app.run_polling()
