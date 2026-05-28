import time
import unittest

from nadi.celld import Cell
from nadi.runtime import local_stack
from nadi.security import JWTError, SessionJWT
from nadi.store import Store


class NadiMVPTests(unittest.TestCase):
    def test_e2e_lifecycle(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        created = gw.create_session("t1")
        sid = created["session_id"]
        gw.send_command(sid, "echo", {"text": "hi"})
        model_out = gw.send_command(sid, "model", {"prompt": "p"})
        gw.send_command(sid, "tool", {"name": "uppercase", "args": {"text": "river"}})
        events = gw.get_session_events(sid)
        types = [e["event_type"] for e in events]
        self.assertIn("session.started", types)
        self.assertIn("assistant.message", types)
        self.assertIn("model.response", types)
        self.assertIn("tool.result", types)
        self.assertEqual(model_out["events"][0]["payload"]["text"], "deterministic-model:p")
        self.assertEqual(stack["store"].get_session(sid)["status"], "running")

    def test_broker_not_in_tool_path(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        sid = gw.create_session("t1")["session_id"]
        gw.send_command(sid, "tool", {"name": "echo", "args": {"text": "x"}})
        self.assertEqual(stack["broker"].tool_path_calls, 0)
        self.assertEqual(stack["sandboxd"].tool_calls, 1)

    def test_reconstruction_from_event_log(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        sid = gw.create_session("t1")["session_id"]
        gw.send_command(sid, "echo", {"text": "persisted"})
        old_cell = stack["celld"].cells[sid]
        stack["celld"].cells.pop(sid)
        new_cell = stack["celld"].reconstruct_cell(sid)
        self.assertIsInstance(new_cell, Cell)
        self.assertIsNot(new_cell, old_cell)
        self.assertIn("persisted", new_cell.state["messages"])
        self.assertTrue(new_cell.state["started"])

    def test_jwt_rejects_wrong_session_and_expired(self):
        jwt = SessionJWT("s")
        token = jwt.sign("a", "tool", ttl_seconds=30)
        with self.assertRaises(JWTError):
            jwt.verify(token, "b", "tool")
        expired = jwt.sign("a", "tool", ttl_seconds=-1)
        time.sleep(1)
        with self.assertRaises(JWTError):
            jwt.verify(expired, "a", "tool")
        cell_only = jwt.sign("a", "cell", ttl_seconds=30)
        with self.assertRaises(JWTError):
            jwt.verify(cell_only, "a", "tool")

    def test_credentials_proxy_audit(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        sid = gw.create_session("t1")["session_id"]
        out = gw.send_command(sid, "tool", {"name": "credential", "args": {"audience": "demo"}})
        self.assertIn("fake-", str(out))
        entries = stack["store"].audit_entries(sid)
        actions = [e["action"] for e in entries]
        self.assertIn("exchange", actions)
        self.assertIn("execute_tool", actions)


    def test_token_revocation_blocks_tool_execution(self):
        stack = local_stack(":memory:")
        gw = stack["gateway"]
        sid = gw.create_session("t1")["session_id"]
        # Cell was created; destroy it — this should revoke the cell JWT
        stack["celld"].destroy_cell(sid)
        # Attempt to execute a tool with the now-revoked token must fail
        cell_jwt = stack["celld"].jwt.sign(sid, "tool", ttl_seconds=30)
        # Token not yet revoked — should work
        stack["sandboxd"].execute_tool(sid, cell_jwt, "echo", {"text": "ok"})
        # Revoke it explicitly via store
        import json, base64
        _, pb, _ = cell_jwt.split(".")
        data = json.loads(base64.urlsafe_b64decode(pb + "==" ))
        stack["store"].revoke_token(data["jti"], float(data["exp"]))
        with self.assertRaises(JWTError):
            stack["sandboxd"].execute_tool(sid, cell_jwt, "echo", {"text": "blocked"})

    def test_claim_token_uniqueness_across_claims(self):
        store = Store(":memory:")
        sid = store.create_session("t1")
        store.enqueue_command(sid, "echo", {"text": "a"})
        store.enqueue_command(sid, "echo", {"text": "b"})
        batch1 = store.claim_pending_commands(sid, "worker1")
        # Second claim should return empty (no pending rows left)
        batch2 = store.claim_pending_commands(sid, "worker1")
        self.assertEqual(len(batch1), 2)
        self.assertEqual(len(batch2), 0)
        # claim_token values for each row must exist and be unique per batch
        tokens = {row["claim_token"] for row in batch1}
        self.assertEqual(len(tokens), 1)  # same token for all rows in one batch
        # Enqueue more and verify second batch gets a different token
        store.enqueue_command(sid, "echo", {"text": "c"})
        batch3 = store.claim_pending_commands(sid, "worker1")
        self.assertNotEqual(batch3[0]["claim_token"], batch1[0]["claim_token"])


if __name__ == "__main__":
    unittest.main()
