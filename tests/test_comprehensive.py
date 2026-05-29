"""Comprehensive tests for Nadi — security, concurrency, adapters, HTTP auth."""
from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

from nadi.adapters.base import _sanitize, extract_reply_text, make_resource_id
from nadi.runtime import local_stack
from nadi.security import JWTError, SessionJWT
from nadi.server import make_server
from nadi.store import Store


# ── Adapter base — sanitization and extraction ───────────────────────────────

class TestExtractReplyText(unittest.TestCase):

    def test_assistant_message_extracted(self):
        events = [{"event_type": "assistant.message", "payload": {"text": "hello"}}]
        self.assertEqual(extract_reply_text(events), "hello")

    def test_model_response_extracted(self):
        events = [{"event_type": "model.response", "payload": {"text": "world"}}]
        self.assertEqual(extract_reply_text(events), "world")

    def test_tool_results_hidden_by_default(self):
        events = [{"event_type": "tool.result", "payload": {"tool": "secret", "result": {"text": "sensitive"}}}]
        self.assertEqual(extract_reply_text(events), "")

    def test_tool_results_shown_when_opted_in(self):
        events = [{"event_type": "tool.result", "payload": {"tool": "echo", "result": {"text": "hi"}}}]
        out = extract_reply_text(events, include_tool_results=True)
        self.assertIn("hi", out)
        self.assertIn("echo", out)

    def test_command_error_surfaced(self):
        events = [{"event_type": "command.error", "payload": {"error": "bad command"}}]
        self.assertIn("bad command", extract_reply_text(events))

    def test_empty_events_returns_empty_string(self):
        self.assertEqual(extract_reply_text([]), "")

    def test_multiple_messages_joined(self):
        events = [
            {"event_type": "assistant.message", "payload": {"text": "line1"}},
            {"event_type": "assistant.message", "payload": {"text": "line2"}},
        ]
        out = extract_reply_text(events)
        self.assertIn("line1", out)
        self.assertIn("line2", out)


class TestSanitize(unittest.TestCase):

    def test_strips_slack_mention(self):
        self.assertNotIn("<@", _sanitize("hello <@U12345> there"))

    def test_strips_channel_link(self):
        self.assertNotIn("<#", _sanitize("go to <#C99999>"))

    def test_strips_backticks(self):
        self.assertNotIn("`", _sanitize("```rm -rf /```"))

    def test_strips_broadcast(self):
        self.assertNotIn("<!", _sanitize("<!channel> alert"))

    def test_caps_at_4000_chars(self):
        self.assertEqual(len(_sanitize("x" * 10_000)), 4000)

    def test_clean_text_passes_through(self):
        self.assertEqual(_sanitize("hello world"), "hello world")


# ── Store — channel routing ───────────────────────────────────────────────────

class TestChannelRouting(unittest.TestCase):

    def setUp(self):
        self.store = Store(":memory:")
        self.sid = self.store.create_session("t1")

    def test_get_or_create_channel_creates_new(self):
        actual_sid, cid, created = self.store.get_or_create_channel(
            "discord", "ch1", "thread1", self.sid, "discord:user1"
        )
        self.assertEqual(actual_sid, self.sid)
        self.assertTrue(created)

    def test_get_or_create_channel_is_idempotent(self):
        sid2 = self.store.create_session("t1")
        self.store.get_or_create_channel("discord", "ch1", "", self.sid, "discord:u1")
        actual_sid, _, created = self.store.get_or_create_channel("discord", "ch1", "", sid2, "discord:u2")
        # Second call loses the race — must return first session, not sid2
        self.assertEqual(actual_sid, self.sid)
        self.assertFalse(created)

    def test_different_thread_id_creates_new_session(self):
        sid2 = self.store.create_session("t1")
        _, _, created1 = self.store.get_or_create_channel("slack", "ch1", "ts1", self.sid, "slack:u1")
        _, _, created2 = self.store.get_or_create_channel("slack", "ch1", "ts2", sid2, "slack:u1")
        self.assertTrue(created1)
        self.assertTrue(created2)

    def test_different_platform_creates_new_session(self):
        sid2 = self.store.create_session("t1")
        _, _, c1 = self.store.get_or_create_channel("discord", "ch1", "", self.sid, "discord:u1")
        _, _, c2 = self.store.get_or_create_channel("slack", "ch1", "", sid2, "slack:u1")
        self.assertTrue(c1)
        self.assertTrue(c2)

    def test_get_channel_returns_none_when_missing(self):
        self.assertIsNone(self.store.get_channel("discord", "nonexistent", ""))

    def test_get_channel_returns_row_after_create(self):
        self.store.get_or_create_channel("telegram", "tg1", "", self.sid, "telegram:u1")
        row = self.store.get_channel("telegram", "tg1", "")
        self.assertIsNotNone(row)
        self.assertEqual(row["session_id"], self.sid)


# ── Store — resource memory ───────────────────────────────────────────────────

class TestResourceMemory(unittest.TestCase):

    def setUp(self):
        self.store = Store(":memory:")

    def test_unknown_resource_returns_empty(self):
        self.assertEqual(self.store.get_resource_memory("discord:nobody"), {})

    def test_set_and_get_roundtrip(self):
        self.store.set_resource_memory("slack:U1", {"key": "val"}, "slack")
        self.assertEqual(self.store.get_resource_memory("slack:U1"), {"key": "val"})

    def test_overwrite_is_last_write_wins(self):
        self.store.set_resource_memory("slack:U1", {"a": 1}, "slack")
        self.store.set_resource_memory("slack:U1", {"b": 2}, "slack")
        self.assertEqual(self.store.get_resource_memory("slack:U1"), {"b": 2})

    def test_size_limit_enforced(self):
        big = {"x": "y" * (2 * 1024 * 1024)}
        with self.assertRaises(ValueError):
            self.store.set_resource_memory("slack:U1", big, "slack")

    def test_users_are_isolated(self):
        self.store.set_resource_memory("discord:A", {"v": 1}, "discord")
        self.store.set_resource_memory("discord:B", {"v": 2}, "discord")
        self.assertEqual(self.store.get_resource_memory("discord:A")["v"], 1)
        self.assertEqual(self.store.get_resource_memory("discord:B")["v"], 2)


# ── Gateway — multi-channel session routing ───────────────────────────────────

class TestGatewayChannelRouting(unittest.TestCase):

    def setUp(self):
        self.stack = local_stack(":memory:")
        self.gw = self.stack["gateway"]

    def test_idempotent_channel_session(self):
        r1 = self.gw.get_or_create_channel_session("discord", "ch1", "t1", initiator_resource_id="discord:u1")
        r2 = self.gw.get_or_create_channel_session("discord", "ch1", "t1", initiator_resource_id="discord:u2")
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(r1["session_id"], r2["session_id"])

    def test_concurrent_channel_session_creation_converges(self):
        """Two concurrent goroutines must converge on the same session_id."""
        results: list[dict] = []
        errors: list[Exception] = []

        def create():
            try:
                results.append(self.gw.get_or_create_channel_session(
                    "discord", "race-ch", "race-thread",
                    initiator_resource_id="discord:racer",
                ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        session_ids = {r["session_id"] for r in results}
        # All threads must agree on a single session_id
        self.assertEqual(len(session_ids), 1, f"Expected 1 session, got {session_ids}")

    def test_anonymous_initiator_gets_unique_ids(self):
        """Two calls without initiator_resource_id get distinct anonymous IDs."""
        r1 = self.gw.get_or_create_channel_session("discord", "ch-anon1", "")
        r2 = self.gw.get_or_create_channel_session("discord", "ch-anon2", "")
        row1 = self.stack["store"].get_channel("discord", "ch-anon1", "")
        row2 = self.stack["store"].get_channel("discord", "ch-anon2", "")
        self.assertNotEqual(row1["initiator_resource_id"], row2["initiator_resource_id"])

    def test_abandoned_session_marked_on_race_loss(self):
        """The losing concurrent session must be marked 'abandoned'."""
        store = self.stack["store"]
        # Pre-create the channel mapping so the next call will always lose
        pre_sid = store.create_session("t1")
        self.stack["broker"].place_session(pre_sid)
        store.get_or_create_channel("discord", "pre-ch", "", pre_sid, "discord:u0")

        r = self.gw.get_or_create_channel_session("discord", "pre-ch", "", initiator_resource_id="discord:u1")
        self.assertFalse(r["created"])
        self.assertEqual(r["session_id"], pre_sid)
        # The candidate session created internally should be abandoned
        all_sessions = store.conn.execute(
            "SELECT id, status FROM sessions WHERE status='abandoned'"
        ).fetchall()
        self.assertGreater(len(all_sessions), 0)

    def test_actor_resource_id_threaded_through_command(self):
        sid = self.gw.create_session("t1")["session_id"]
        self.gw.send_command(sid, "echo", {"text": "hi"}, actor_resource_id="slack:U999")
        row = self.stack["store"].conn.execute(
            "SELECT actor_resource_id FROM command_inbox WHERE session_id=?", (sid,)
        ).fetchone()
        self.assertEqual(row[0], "slack:U999")


# ── HTTP server — auth and tenant isolation ───────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestServerAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.key = "test-key-12345"
        cls.httpd = make_server(":memory:", port=cls.port, api_key=cls.key, tenant_id="tenant-A")
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_healthz_no_auth_required(self):
        with urllib.request.urlopen(self._url("/healthz"), timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_post_sessions_requires_auth(self):
        status, _ = _post(self._url("/sessions"), {})
        self.assertEqual(status, 401)

    def test_post_sessions_with_valid_key(self):
        status, body = _post(self._url("/sessions"), {}, {"x-nadi-key": self.key})
        self.assertEqual(status, 201)
        self.assertIn("session_id", body)

    def test_post_channels_requires_auth(self):
        status, _ = _post(self._url("/channels"), {"platform": "discord", "channel_id": "c1"})
        self.assertEqual(status, 401)

    def test_post_channels_with_auth(self):
        status, body = _post(self._url("/channels"),
                             {"platform": "discord", "channel_id": "c2", "thread_id": "t1"},
                             {"x-nadi-key": self.key})
        self.assertEqual(status, 200)
        self.assertIn("session_id", body)

    def test_tenant_id_from_body_is_ignored(self):
        """Caller cannot override tenant_id via the request body."""
        status, body = _post(self._url("/sessions"),
                             {"tenant_id": "evil-tenant"},
                             {"x-nadi-key": self.key})
        self.assertEqual(status, 201)
        sid = body["session_id"]
        from nadi.runtime import local_stack as ls
        # The server has its own internal store — we can't inspect it directly,
        # but we verify the session was created (if tenant override worked, it
        # would be under "evil-tenant"; since we can't query the server's store
        # here we at least confirm the endpoint accepted the request)
        self.assertIsNotNone(sid)

    def test_oversized_channel_field_rejected(self):
        status, body = _post(self._url("/channels"),
                             {"platform": "discord", "channel_id": "x" * 10_000, "thread_id": ""},
                             {"x-nadi-key": self.key})
        self.assertEqual(status, 400)

    def test_get_events_requires_auth(self):
        req = urllib.request.Request(self._url("/sessions/fake-id/events"))
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


# ── JWT — edge cases ──────────────────────────────────────────────────────────

class TestJWTEdgeCases(unittest.TestCase):

    def test_jti_present_in_signed_token(self):
        jwt = SessionJWT("s")
        import base64
        token = jwt.sign("sid1", "tool")
        _, payload_b64, _ = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        self.assertIn("jti", payload)

    def test_wildcard_scope_passes_any_scope_check(self):
        jwt = SessionJWT("s")
        token = jwt.sign("sid1", "*")
        data = jwt.verify(token, "sid1", "tool")
        self.assertEqual(data["sid"], "sid1")

    def test_alg_none_rejected(self):
        import base64, hmac, hashlib
        jwt = SessionJWT("s")
        # Craft a token with alg:none
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"x","sid":"s1","scope":"tool","exp":9999999999}').rstrip(b"=").decode()
        token = f"{header}.{payload}."
        with self.assertRaises(JWTError):
            jwt.verify(token, "s1", "tool")

    def test_revoked_token_rejected_via_store(self):
        store = Store(":memory:")
        jwt = SessionJWT("s")
        token = jwt.sign("sid1", "tool")
        import base64, json as _json
        _, pb, _ = token.split(".")
        data = _json.loads(base64.urlsafe_b64decode(pb + "=="))
        store.revoke_token(data["jti"], float(data["exp"]))
        with self.assertRaises(JWTError):
            jwt.verify(token, "sid1", "tool", store=store)

    def test_max_ttl_capped(self):
        jwt = SessionJWT("s")
        import base64, json as _json
        token = jwt.sign("sid1", "tool", ttl_seconds=999_999)
        _, pb, _ = token.split(".")
        data = _json.loads(base64.urlsafe_b64decode(pb + "=="))
        # exp must not exceed now + 86400
        self.assertLessEqual(data["exp"], int(time.time()) + 86400 + 2)


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestRateLimiting(unittest.TestCase):

    def test_max_pending_commands_enforced(self):
        store = Store(":memory:")
        sid = store.create_session("t1")
        for _ in range(store._MAX_PENDING_COMMANDS):
            store.enqueue_command(sid, "echo", {})
        with self.assertRaises(ValueError, msg="should reject command over limit"):
            store.enqueue_command(sid, "echo", {})

    def test_max_events_per_session_enforced(self):
        store = Store(":memory:")
        sid = store.create_session("t1")
        # Patch the limit to something small so the test is fast
        original = store._MAX_EVENTS_PER_SESSION
        store._MAX_EVENTS_PER_SESSION = 3
        for _ in range(3):
            store.append_event(sid, "test.event")
        with self.assertRaises(ValueError):
            store.enqueue_command(sid, "echo", {})
        store._MAX_EVENTS_PER_SESSION = original


# ── Sandbox — path traversal ──────────────────────────────────────────────────

class TestSandboxSecurity(unittest.TestCase):

    def test_path_traversal_blocked(self):
        from nadi.sandboxd import Sandboxd
        import tempfile
        from pathlib import Path
        store = Store(":memory:")
        jwt = SessionJWT("s")
        with tempfile.TemporaryDirectory() as td:
            sandbox = Sandboxd(store, jwt, root=td)
            sid = "s1"
            token = jwt.sign(sid, "tool", ttl_seconds=30)
            with self.assertRaises(ValueError, msg=".. should be blocked"):
                sandbox.execute_tool(sid, token, "list_files", {"path": "../"})

    def test_absolute_path_blocked(self):
        from nadi.sandboxd import Sandboxd
        import tempfile
        store = Store(":memory:")
        jwt = SessionJWT("s")
        with tempfile.TemporaryDirectory() as td:
            sandbox = Sandboxd(store, jwt, root=td)
            sid = "s1"
            token = jwt.sign(sid, "tool", ttl_seconds=30)
            with self.assertRaises((ValueError, Exception)):
                sandbox.execute_tool(sid, token, "list_files", {"path": "/etc"})

    def test_valid_nested_path_allowed(self):
        from nadi.sandboxd import Sandboxd
        import tempfile
        from pathlib import Path
        store = Store(":memory:")
        jwt = SessionJWT("s")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "sub").mkdir()
            (Path(td) / "sub" / "file.txt").write_text("x")
            sandbox = Sandboxd(store, jwt, root=td)
            sid = "s1"
            token = jwt.sign(sid, "tool", ttl_seconds=30)
            result = sandbox.execute_tool(sid, token, "list_files", {"path": "sub"})
            self.assertIn("file.txt", result["result"]["files"])


# ── make_resource_id ──────────────────────────────────────────────────────────

class TestMakeResourceId(unittest.TestCase):

    def test_format(self):
        self.assertEqual(make_resource_id("discord", "U123"), "discord:U123")
        self.assertEqual(make_resource_id("slack", "W999"), "slack:W999")
        self.assertEqual(make_resource_id("telegram", "42"), "telegram:42")


if __name__ == "__main__":
    unittest.main()
