
import random
import string
import tempfile
import unittest
from pathlib import Path

from nadi.runtime import local_stack
from nadi.sandboxd import Sandboxd
from nadi.security import JWTError, SessionJWT
from nadi.store import Store


class PropertyStyleTests(unittest.TestCase):
    """Stdlib randomized/property-style checks for core invariants."""

    def test_echo_and_uppercase_preserve_expected_string_properties(self):
        rng = random.Random(1337)
        alphabet = string.ascii_letters + string.digits + " -_"
        for _ in range(80):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 64)))
            with self.subTest(text=text):
                stack = local_stack(":memory:")
                sid = stack["gateway"].create_session("prop")["session_id"]
                echo = stack["gateway"].send_command(sid, "echo", {"text": text})
                upper = stack["gateway"].send_command(
                    sid, "tool", {"name": "uppercase", "args": {"text": text}}
                )
                self.assertEqual(echo["events"][0]["payload"]["text"], text)
                self.assertEqual(upper["events"][0]["payload"]["result"]["text"], text.upper())
                self.assertEqual(stack["broker"].tool_path_calls, 0)

    def test_event_sequences_are_contiguous_for_random_command_streams(self):
        rng = random.Random(2026)
        for case in range(25):
            stack = local_stack(":memory:")
            gw = stack["gateway"]
            sid = gw.create_session(f"tenant-{case}")["session_id"]
            for n in range(rng.randint(1, 20)):
                kind = rng.choice(["echo", "model", "unknown"])
                payload = {"text": f"m-{case}-{n}", "prompt": f"p-{case}-{n}"}
                gw.send_command(sid, kind, payload)
            events = gw.get_session_events(sid)
            self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[0]["event_type"], "session.started")

    def test_jwt_round_trips_many_sessions_and_scopes(self):
        jwt = SessionJWT("property-secret")
        rng = random.Random(44)
        for _ in range(100):
            sid = "s-" + "".join(rng.choice(string.ascii_lowercase) for _ in range(12))
            scope = rng.choice(["tool", "cell", "tool cell", "*"])
            token = jwt.sign(sid, scope, ttl_seconds=30, subject="prop")
            expected_scope = "tool" if "tool" in scope or scope == "*" else scope.split()[0]
            decoded = jwt.verify(token, sid, expected_scope)
            self.assertEqual(decoded["sid"], sid)
            self.assertEqual(decoded["sub"], "prop")

    def test_sandbox_list_files_never_escapes_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("a")
            (root / "nested").mkdir()
            store = Store(":memory:")
            jwt = SessionJWT("sandbox-secret")
            sandbox = Sandboxd(store, jwt, root=str(root))
            sid = "s1"
            token = jwt.sign(sid, "tool", ttl_seconds=30)
            self.assertEqual(sandbox.execute_tool(sid, token, "list_files", {"path": "."})["result"]["files"], ["a.txt", "nested"])
            with self.assertRaises(ValueError):
                sandbox.execute_tool(sid, token, "list_files", {"path": ".."})
            with self.assertRaises(JWTError):
                sandbox.execute_tool("other", token, "echo", {"text": "x"})


if __name__ == "__main__":
    unittest.main()
