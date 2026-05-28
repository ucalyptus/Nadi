"""Discord adapter for Nadi.

Requires:  pip install discord.py

Usage:
    from nadi.runtime import local_stack
    from nadi.adapters.discord_adapter import DiscordAdapter

    stack = local_stack("nadi.db")
    adapter = DiscordAdapter(stack["gateway"], bot_token="Bot YOUR_TOKEN")
    adapter.run()  # blocking
"""
from __future__ import annotations

import logging
from typing import Any

from .base import extract_reply_text, make_resource_id

log = logging.getLogger(__name__)


class DiscordAdapter:
    """Connects a Nadi Gateway to a Discord bot.

    Each Discord channel maps to one Nadi session per top-level context.
    Messages sent inside a thread use the thread ID as `thread_id`, giving
    each thread its own isolated session — matching the 1:1 thread↔session
    invariant described in Mastra's channel design.
    """

    def __init__(
        self,
        gateway: Any,
        bot_token: str,
        tenant_id: str = "discord",
        command_type: str = "model",
    ):
        try:
            import discord  # type: ignore[import]
        except ImportError as e:
            raise ImportError("pip install discord.py") from e

        self._gateway = gateway
        self._bot_token = bot_token
        self._tenant_id = tenant_id
        self._command_type = command_type

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready() -> None:
            log.info("Nadi Discord adapter ready as %s", self._client.user)

        @self._client.event
        async def on_message(message: Any) -> None:
            if message.author.bot:
                return
            await self._handle(message)

    _MAX_TEXT = 8000  # chars — Discord messages cap at 2000 but content can arrive larger via API

    async def _handle(self, message: Any) -> None:
        import discord  # type: ignore[import]

        channel_id = str(message.channel.id)
        # Thread channels have their own ID; use it as thread_id so each
        # Discord thread gets its own Nadi session.
        thread_id = str(message.channel.id) if isinstance(message.channel, discord.Thread) else ""
        resource_id = make_resource_id("discord", str(message.author.id))
        text = (message.content or "")[:self._MAX_TEXT]

        try:
            routing = self._gateway.get_or_create_channel_session(
                platform="discord",
                channel_id=channel_id,
                thread_id=thread_id,
                tenant_id=self._tenant_id,
                initiator_resource_id=resource_id,
                metadata={"guild_id": str(message.guild.id) if message.guild else None},
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
                await message.channel.send(reply[:2000])  # Discord hard limit
        except Exception:
            log.exception("error handling Discord message in session routing")

    def run(self) -> None:
        """Start the Discord bot (blocking)."""
        self._client.run(self._bot_token)
