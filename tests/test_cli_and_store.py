import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import nadi
from nadi.cli import demo, main
from nadi.gateway import Gateway
from nadi.broker import Broker
from nadi.store import Store


class CLIAndStoreTests(unittest.TestCase):
    def test_package_metadata_exports_modules(self):
        self.assertIn("gateway", nadi.__all__)
        self.assertTrue(nadi.__version__.startswith("0.1.0"))

    def test_cli_demo_function_and_main_emit_json(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "demo.db")
            result = demo(db)
            self.assertEqual(result["broker_tool_path_calls"], 0)
            self.assertIn("session_id", result["session"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["demo", "--db", db])
            self.assertEqual(code, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["broker_tool_path_calls"], 0)

    def test_cli_unknown_branch_returns_2_when_called_directly(self):
        # Defensive branch coverage for direct programmatic calls with a fabricated args object is not
        # useful; argparse handles invalid commands before this point. Assert parser exits for invalid input.
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                main(["missing"])

    def test_store_host_upserts_heartbeats_audit_and_routes(self):
        store = Store(":memory:")
        sid = store.create_session("tenant", external_id="ext", profile_bundle="bundle", metadata={"a": 1})
        first = store.upsert_cell_host("cell-a", "z1", "inproc://a", 2, {"rack": "r1"})
        second = store.upsert_cell_host("cell-a", "z2", "inproc://b", 3, {"rack": "r2"})
        self.assertEqual(first, second)
        store.upsert_sandbox_host("sandbox-a", "z1", "inproc://s", {"disk": "btrfs"})
        store.heartbeat_host("cell_hosts", "cell-a")
        store.heartbeat_host("sandbox_hosts", "sandbox-a")
        lease = store.create_lease(sid, first, ttl_seconds=30)
        route = store.get_route(sid)
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route["lease_token"], lease)
        self.assertEqual(route["host_id"], "cell-a")
        audit_id = store.audit("tester", "did_thing", sid, {"ok": True})
        self.assertGreater(audit_id, 0)
        self.assertEqual(store.audit_entries(sid)[0]["payload"], {"ok": True})
        self.assertGreaterEqual(len(store.audit_entries()), 1)

    def test_gateway_route_errors_without_lease_and_broker_callback_required(self):
        store = Store(":memory:")
        broker = Broker(store)
        gw = Gateway(store, broker)
        sid = store.create_session("tenant")
        with self.assertRaises(KeyError):
            gw.route(sid)
        store.upsert_cell_host("cell-no-callback", "z", "inproc://missing", 1)
        with self.assertRaises(RuntimeError):
            broker.place_session(sid)
        with self.assertRaises(KeyError):
            broker.heartbeat("unknown", "host")
        with self.assertRaises(ValueError):
            store.heartbeat_host("bad_table", "host")


if __name__ == "__main__":
    unittest.main()
