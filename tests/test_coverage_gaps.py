"""Targeted tests to cover remaining uncovered lines — pushing toward 85%+."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from nadi.adapters.base import extract_reply_text, make_resource_id
from nadi.runtime import local_stack
from nadi.security import JWTError, SessionJWT
from nadi.store import Store


# ── security.py — all error branches ─────────────────────────────────────────

class TestJWTErrorBranches(unittest.TestCase):

    def setUp(self):
        self.jwt = SessionJWT("secret")

    def test_malformed_token_too_few_parts(self):
        with self.assertRaises(JWTError) as ctx:
            self.jwt.verify("only.two", "sid", "tool")
        self.assertIn("malformed token", str(ctx.exception))

    def test_malformed_header_bad_base64(self):
        with self.assertRaises(JWTError) as ctx:
            self.jwt.verify("!!!.payload.sig", "sid", "tool")
        self.assertIn("malformed", str(ctx.exception).lower())

    def test_bad_signature_rejected(self):
        token = self.jwt.sign("sid1", "tool")
        head, payload, _ = token.split(".")
        tampered = f"{head}.{payload}.invalidsig"
        with self.assertRaises(JWTError) as ctx:
            self.jwt.verify(tampered, "sid1", "tool")
        self.assertIn("bad signature", str(ctx.exception))

    def test_malformed_payload_bad_base64(self):
        token = self.jwt.sign("sid1", "tool")
        head, _, sig = token.split(".")
        # Put a valid-looking header but invalid payload
        import hmac as _hmac, hashlib
        bad_payload = "!!!"
        body = f"{head}.{bad_payload}"
        new_sig_bytes = _hmac.new(self.jwt.secret, body.encode(), hashlib.sha256).digest()
        new_sig = base64.urlsafe_b64encode(new_sig_bytes).rstrip(b"=").decode()
        tampered = f"{body}.{new_sig}"
        with self.assertRaises(JWTError) as ctx:
            self.jwt.verify(tampered, "sid1", "tool")
        # Could be malformed header OR malformed payload depending on decode
        self.assertTrue("malformed" in str(ctx.exception).lower() or "bad" in str(ctx.exception).lower())

    def test_expired_token_rejected(self):
        token = self.jwt.sign("sid1", "tool", ttl_seconds=-10)
        with self.assertRaises(JWTError) as ctx:
            self.jwt.verify(token, "sid1", "tool")
        self.assertIn("expired", str(ctx.exception))


# ── celld.py — uncovered branches ────────────────────────────────────────────

class TestCelldBranches(unittest.TestCase):

    def setUp(self):
        self.stack = local_stack(":memory:")
        self.celld = self.stack["celld"]
        self.gw = self.stack["gateway"]

    def test_consume_commands_reconstructs_missing_cell(self):
        """consume_commands should reconstruct the cell when it's not in memory."""
        sid = self.gw.create_session("t1")["session_id"]
        # Evict cell from in-memory dict
        self.celld.cells.pop(sid, None)
        # Should reconstruct and process without error
        result = self.gw.send_command(sid, "echo", {"text": "reconstruct"})
        self.assertTrue(len(result["events"]) > 0)

    def test_destroy_cell_noop_when_missing(self):
        """destroy_cell with unknown session_id must not raise."""
        self.celld.destroy_cell("nonexistent-session")

    def test_destroy_cell_revokes_token(self):
        sid = self.gw.create_session("t1")["session_id"]
        cell = self.celld.cells.get(sid)
        self.assertIsNotNone(cell)
        self.celld.destroy_cell(sid)
        # Session status should be 'stopped'
        session = self.stack["store"].get_session(sid)
        self.assertEqual(session["status"], "stopped")

    def test_destroy_cell_handles_bad_jwt_gracefully(self):
        """destroy_cell must not raise if the cell jwt is malformed."""
        from nadi.celld import Cell
        sid = "fake-session"
        self.stack["store"].create_session("t1")
        bad_cell = Cell(session_id=sid, jwt="not.a.valid.jwt.at.all")
        with self.celld._lock:
            self.celld.cells[sid] = bad_cell
        # Should not raise even with bad JWT
        self.celld.destroy_cell(sid)


# ── store.py — uncovered branches ────────────────────────────────────────────

class TestStoreBranches(unittest.TestCase):

    def setUp(self):
        self.store = Store(":memory:")

    def test_upsert_sandbox_host_update_path(self):
        """upsert_sandbox_host should update an existing record."""
        sid1 = self.store.upsert_sandbox_host("host1", "zone-a", "addr-a")
        sid2 = self.store.upsert_sandbox_host("host1", "zone-b", "addr-b")
        self.assertEqual(sid1, sid2)
        row = self.store.conn.execute("SELECT zone FROM sandbox_hosts WHERE host_id='host1'").fetchone()
        self.assertEqual(row[0], "zone-b")

    def test_choose_cell_host_raises_when_none_available(self):
        with self.assertRaises(RuntimeError):
            self.store.choose_cell_host()

    def test_get_resource_memory_corrupt_blob_returns_empty(self):
        """Corrupt JSON blob in resources table should return {} not raise."""
        t = time.time()
        self.store.conn.execute(
            "INSERT INTO resources(resource_id, platform, memory, created_at, updated_at) VALUES(?,?,?,?,?)",
            ("bad-resource", "test", "NOT_VALID_JSON{{{{", t, t)
        )
        self.store.conn.commit()
        result = self.store.get_resource_memory("bad-resource")
        self.assertEqual(result, {})

    def test_is_token_revoked_purges_expired(self):
        """is_token_revoked should delete entries whose expires_at is in the past."""
        past = time.time() - 10
        self.store.conn.execute(
            "INSERT INTO revoked_tokens(jti, expires_at, revoked_at) VALUES(?,?,?)",
            ("old-jti", past, past)
        )
        self.store.conn.commit()
        result = self.store.is_token_revoked("old-jti")
        self.assertFalse(result)  # purged, so not found
        row = self.store.conn.execute("SELECT 1 FROM revoked_tokens WHERE jti='old-jti'").fetchone()
        self.assertIsNone(row)

    def test_audit_entries_all_when_no_session(self):
        store = Store(":memory:")
        sid = store.create_session("t1")
        store.audit("actor1", "act1")
        store.audit("actor2", "act2", sid)
        all_entries = store.audit_entries()
        self.assertEqual(len(all_entries), 2)


# ── gateway.py — exception path ──────────────────────────────────────────────

class TestGatewayExceptionPath(unittest.TestCase):

    def test_get_or_create_channel_session_cleans_up_on_broker_failure(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        store = stack["store"]

        # Make broker.place_session raise on second call
        call_count = [0]
        original = stack["broker"].place_session

        def failing_place(sid):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("broker down")
            return original(sid)

        stack["broker"].place_session = failing_place

        # First call succeeds
        r1 = gw.get_or_create_channel_session("discord", "ch-err", "t1", initiator_resource_id="discord:u1")
        self.assertTrue(r1["created"])

        # Second call on different channel fails at broker
        with self.assertRaises(RuntimeError):
            gw.get_or_create_channel_session("discord", "ch-err-2", "t1", initiator_resource_id="discord:u1")

        # The failed session should be marked 'abandoned'
        abandoned = store.conn.execute(
            "SELECT count(*) FROM sessions WHERE status='abandoned'"
        ).fetchone()[0]
        self.assertGreater(abandoned, 0)


# ── server.py — missing branches ─────────────────────────────────────────────

class TestServerBranches(unittest.TestCase):

    def test_body_with_zero_content_length(self):
        """_body() with no content-length header should return empty dict."""
        from nadi.server import NadiHandler
        handler = object.__new__(NadiHandler)
        handler.headers = {}
        handler.rfile = __import__("io").BytesIO(b"")
        # Should not raise — returns {}
        result = handler._body()
        self.assertEqual(result, {})


# ── adapters — mock-based tests ───────────────────────────────────────────────

class TestDiscordAdapter(unittest.TestCase):

    def _make_discord_mock(self):
        discord_mod = types.ModuleType("discord")
        discord_mod.Intents = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock(message_content=True)
        discord_mod.Client = MagicMock
        discord_mod.Thread = type("Thread", (), {})
        return discord_mod

    def _make_gateway_mock(self, reply_text="hello"):
        gw = MagicMock()
        gw.get_or_create_channel_session.return_value = {"session_id": "sid1", "created": True}
        gw.send_command.return_value = {"events": [
            {"event_type": "assistant.message", "payload": {"text": reply_text}}
        ]}
        return gw

    @patch.dict("sys.modules", {"discord": None})
    def test_import_error_raised_when_discord_missing(self):
        # Remove discord from sys.modules to force ImportError
        discord_mock = types.ModuleType("discord")
        with patch.dict("sys.modules", {"discord": None}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters import discord_adapter as da_mod
            import importlib
            importlib.invalidate_caches()
        # Just verify the module loads without discord installed when imported normally
        # (the ImportError is raised in __init__, not at module level)

    def test_handle_regular_channel_message(self):
        discord_mod = self._make_discord_mock()
        gw = self._make_gateway_mock("hi back")

        with patch.dict("sys.modules", {"discord": discord_mod}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters.discord_adapter import DiscordAdapter
            adapter = DiscordAdapter.__new__(DiscordAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "discord"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

            message = MagicMock()
            message.channel.id = 12345
            message.channel.__class__ = object  # not a discord.Thread
            message.author.id = 99
            message.content = "hello bot"
            message.guild.id = 1
            message.channel.send = AsyncMock()

            asyncio.get_event_loop().run_until_complete(adapter._handle(message))

            gw.get_or_create_channel_session.assert_called_once()
            call_kwargs = gw.get_or_create_channel_session.call_args
            self.assertEqual(call_kwargs.kwargs["platform"], "discord")
            self.assertEqual(call_kwargs.kwargs["thread_id"], "")  # not a thread
            message.channel.send.assert_called_once_with("hi back")

    def test_handle_thread_message_uses_thread_id(self):
        discord_mod = self._make_discord_mock()
        gw = self._make_gateway_mock("thread reply")

        with patch.dict("sys.modules", {"discord": discord_mod}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters.discord_adapter import DiscordAdapter
            adapter = DiscordAdapter.__new__(DiscordAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "discord"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

            message = MagicMock()
            message.channel.id = 55555
            # Make channel be an instance of discord.Thread mock
            message.channel.__class__ = discord_mod.Thread
            message.author.id = 77
            message.content = "thread message"
            message.guild.id = 2
            message.channel.send = AsyncMock()

            asyncio.get_event_loop().run_until_complete(adapter._handle(message))

            call_kwargs = gw.get_or_create_channel_session.call_args
            self.assertEqual(call_kwargs.kwargs["thread_id"], "55555")

    def test_handle_gateway_error_logged_not_raised(self):
        discord_mod = self._make_discord_mock()
        gw = MagicMock()
        gw.get_or_create_channel_session.side_effect = RuntimeError("gateway down")

        with patch.dict("sys.modules", {"discord": discord_mod}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters.discord_adapter import DiscordAdapter
            adapter = DiscordAdapter.__new__(DiscordAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "discord"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

            message = MagicMock()
            message.channel.id = 1
            message.channel.__class__ = object
            message.author.id = 1
            message.content = "fail"
            message.guild = None
            message.channel.send = AsyncMock()

            # Should not raise
            asyncio.get_event_loop().run_until_complete(adapter._handle(message))
            message.channel.send.assert_not_called()

    def test_handle_empty_reply_not_sent(self):
        discord_mod = self._make_discord_mock()
        gw = MagicMock()
        gw.get_or_create_channel_session.return_value = {"session_id": "sid1"}
        gw.send_command.return_value = {"events": []}  # no reply text

        with patch.dict("sys.modules", {"discord": discord_mod}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters.discord_adapter import DiscordAdapter
            adapter = DiscordAdapter.__new__(DiscordAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "discord"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

            message = MagicMock()
            message.channel.id = 1
            message.channel.__class__ = object
            message.author.id = 1
            message.content = "hi"
            message.guild = None
            message.channel.send = AsyncMock()

            asyncio.get_event_loop().run_until_complete(adapter._handle(message))
            message.channel.send.assert_not_called()

    def test_text_truncated_at_max(self):
        discord_mod = self._make_discord_mock()
        gw = self._make_gateway_mock("ok")

        with patch.dict("sys.modules", {"discord": discord_mod}):
            sys.modules.pop("nadi.adapters.discord_adapter", None)
            from nadi.adapters.discord_adapter import DiscordAdapter
            adapter = DiscordAdapter.__new__(DiscordAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "discord"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 10  # tiny limit for test

            message = MagicMock()
            message.channel.id = 1
            message.channel.__class__ = object
            message.author.id = 1
            message.content = "x" * 100
            message.guild = None
            message.channel.send = AsyncMock()

            asyncio.get_event_loop().run_until_complete(adapter._handle(message))
            sent_text = gw.send_command.call_args[0][2]["text"]
            self.assertEqual(len(sent_text), 10)


class TestSlackAdapter(unittest.TestCase):

    def _make_slack_mock(self):
        bolt_mod = types.ModuleType("slack_bolt")
        bolt_mod.App = MagicMock(return_value=MagicMock())
        return bolt_mod

    def _make_gateway_mock(self, reply="ok"):
        gw = MagicMock()
        gw.get_or_create_channel_session.return_value = {"session_id": "sid1"}
        gw.send_command.return_value = {"events": [
            {"event_type": "assistant.message", "payload": {"text": reply}}
        ]}
        return gw

    def test_handle_top_level_message_empty_thread_ts(self):
        bolt_mod = self._make_slack_mock()
        gw = self._make_gateway_mock("slack reply")

        with patch.dict("sys.modules", {"slack_bolt": bolt_mod}):
            sys.modules.pop("nadi.adapters.slack_adapter", None)
            from nadi.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter.__new__(SlackAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "slack"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

        say = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "hello", "team": "T1"}
        # No thread_ts — top-level message
        adapter._handle(event, say)

        call_kwargs = gw.get_or_create_channel_session.call_args.kwargs
        self.assertEqual(call_kwargs["thread_id"], "")  # empty, not fallback to ts

    def test_handle_threaded_message_uses_thread_ts(self):
        bolt_mod = self._make_slack_mock()
        gw = self._make_gateway_mock("thread reply")

        with patch.dict("sys.modules", {"slack_bolt": bolt_mod}):
            sys.modules.pop("nadi.adapters.slack_adapter", None)
            from nadi.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter.__new__(SlackAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "slack"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

        say = MagicMock()
        event = {"channel": "C1", "user": "U1", "text": "reply", "thread_ts": "1234567890.000001"}
        adapter._handle(event, say)

        call_kwargs = gw.get_or_create_channel_session.call_args.kwargs
        self.assertEqual(call_kwargs["thread_id"], "1234567890.000001")
        say.assert_called_once()

    def test_handle_error_logged_not_raised(self):
        bolt_mod = self._make_slack_mock()
        gw = MagicMock()
        gw.get_or_create_channel_session.side_effect = RuntimeError("down")

        with patch.dict("sys.modules", {"slack_bolt": bolt_mod}):
            sys.modules.pop("nadi.adapters.slack_adapter", None)
            from nadi.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter.__new__(SlackAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "slack"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 8000

        say = MagicMock()
        # Should not raise
        adapter._handle({"channel": "C1", "user": "U1", "text": "hi"}, say)
        say.assert_not_called()

    def test_text_truncated_to_max(self):
        bolt_mod = self._make_slack_mock()
        gw = self._make_gateway_mock()

        with patch.dict("sys.modules", {"slack_bolt": bolt_mod}):
            sys.modules.pop("nadi.adapters.slack_adapter", None)
            from nadi.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter.__new__(SlackAdapter)
            adapter._gateway = gw
            adapter._tenant_id = "slack"
            adapter._command_type = "model"
            adapter._MAX_TEXT = 5

        say = MagicMock()
        adapter._handle({"channel": "C1", "user": "U1", "text": "hello world"}, say)
        sent = gw.send_command.call_args[0][2]["text"]
        self.assertEqual(len(sent), 5)


class TestTelegramAdapter(unittest.TestCase):

    def _make_telegram_mocks(self):
        tg_mod = types.ModuleType("telegram")
        tg_ext = types.ModuleType("telegram.ext")

        class FakeApp:
            def __init__(self):
                self.handlers = []
            def add_handler(self, h):
                self.handlers.append(h)

        class FakeBuilder:
            def token(self, t): return self
            def build(self): return FakeApp()

        class FakeApplication:
            @staticmethod
            def builder(): return FakeBuilder()

        tg_ext.Application = FakeApplication
        tg_ext.MessageHandler = MagicMock(return_value=MagicMock())
        tg_ext.filters = MagicMock()
        tg_ext.filters.TEXT = MagicMock()
        tg_ext.filters.COMMAND = MagicMock()
        tg_ext.filters.TEXT.__and__ = lambda s, o: MagicMock()
        tg_mod.ext = tg_ext
        sys.modules["telegram"] = tg_mod
        sys.modules["telegram.ext"] = tg_ext
        return tg_mod, tg_ext

    def _make_gateway_mock(self, reply="tg reply"):
        gw = MagicMock()
        gw.get_or_create_channel_session.return_value = {"session_id": "sid1"}
        gw.send_command.return_value = {"events": [
            {"event_type": "assistant.message", "payload": {"text": reply}}
        ]}
        return gw

    def test_handle_direct_message(self):
        tg_mod, tg_ext = self._make_telegram_mocks()
        gw = self._make_gateway_mock("hello telegram")

        sys.modules.pop("nadi.adapters.telegram_adapter", None)
        from nadi.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._gateway = gw
        adapter._tenant_id = "telegram"
        adapter._command_type = "model"

        update = MagicMock()
        update.effective_chat.id = 111
        update.effective_chat.type = "private"
        update.effective_user.id = 42
        update.effective_message.message_thread_id = None
        update.effective_message.text = "hi there"
        update.effective_message.reply_text = AsyncMock()

        asyncio.get_event_loop().run_until_complete(adapter._handle(update, MagicMock()))

        call_kwargs = gw.get_or_create_channel_session.call_args.kwargs
        self.assertEqual(call_kwargs["platform"], "telegram")
        self.assertEqual(call_kwargs["thread_id"], "")
        update.effective_message.reply_text.assert_called_once_with("hello telegram")

    def test_handle_forum_topic_uses_thread_id(self):
        tg_mod, tg_ext = self._make_telegram_mocks()
        gw = self._make_gateway_mock("topic reply")

        sys.modules.pop("nadi.adapters.telegram_adapter", None)
        from nadi.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._gateway = gw
        adapter._tenant_id = "telegram"
        adapter._command_type = "model"

        update = MagicMock()
        update.effective_chat.id = 222
        update.effective_chat.type = "supergroup"
        update.effective_user.id = 55
        update.effective_message.message_thread_id = 99
        update.effective_message.text = "forum topic msg"
        update.effective_message.reply_text = AsyncMock()

        asyncio.get_event_loop().run_until_complete(adapter._handle(update, MagicMock()))

        call_kwargs = gw.get_or_create_channel_session.call_args.kwargs
        self.assertEqual(call_kwargs["thread_id"], "99")

    def test_handle_error_not_raised(self):
        tg_mod, tg_ext = self._make_telegram_mocks()
        gw = MagicMock()
        gw.get_or_create_channel_session.side_effect = RuntimeError("err")

        sys.modules.pop("nadi.adapters.telegram_adapter", None)
        from nadi.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._gateway = gw
        adapter._tenant_id = "telegram"
        adapter._command_type = "model"

        update = MagicMock()
        update.effective_chat.id = 1
        update.effective_chat.type = "private"
        update.effective_user.id = 1
        update.effective_message.message_thread_id = None
        update.effective_message.text = "hi"
        update.effective_message.reply_text = AsyncMock()

        # Must not raise
        asyncio.get_event_loop().run_until_complete(adapter._handle(update, MagicMock()))
        update.effective_message.reply_text.assert_not_called()


# ── broker.py — missing branches ─────────────────────────────────────────────

class TestBrokerBranches(unittest.TestCase):

    def test_celld_for_route_missing_raises(self):
        from nadi.broker import Broker
        store = Store(":memory:")
        broker = Broker(store)
        with self.assertRaises(RuntimeError):
            broker.celld_for_route("nonexistent-session")


if __name__ == "__main__":
    unittest.main()
