"""Platform adapters for Nadi — Discord, Slack, Telegram.

Each adapter wraps a Nadi Gateway and handles the full message lifecycle:
  receive → get_or_create_channel_session → send_command → reply

Import adapters directly; their platform SDKs are optional dependencies:
  pip install discord.py          # for DiscordAdapter
  pip install slack-bolt          # for SlackAdapter
  pip install python-telegram-bot # for TelegramAdapter
"""
from .base import GatewayProtocol, extract_reply_text, make_resource_id

__all__ = ["GatewayProtocol", "extract_reply_text", "make_resource_id"]
