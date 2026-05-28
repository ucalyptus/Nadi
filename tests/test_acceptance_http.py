
import json
import tempfile
import unittest
import urllib.error
import urllib.request

from nadi.server import make_server


class HTTPAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=True)
        self.httpd = make_server(self.tmp.name, "127.0.0.1", 0)
        self.host, self.port = self.httpd.server_address
        self.thread = __import__("threading").Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.close()

    def url(self, path):
        return f"http://{self.host}:{self.port}{path}"

    def request(self, method, path, body=None, expected=200):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url(path), data=data, method=method, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, expected)
            return json.loads(resp.read())

    def test_http_lifecycle_acceptance(self):
        self.assertEqual(self.request("GET", "/healthz"), {"ok": True})
        created = self.request("POST", "/sessions", {"tenant_id": "acceptance"}, expected=201)
        sid = created["session_id"]
        out = self.request("POST", f"/sessions/{sid}/commands", {"type": "tool", "payload": {"name": "uppercase", "args": {"text": "gherkin"}}}, expected=202)
        self.assertEqual(out["events"][0]["payload"]["result"]["text"], "GHERKIN")
        events = self.request("GET", f"/sessions/{sid}/events")
        self.assertIn("tool.result", [e["event_type"] for e in events["events"]])

    def test_http_404s_unknown_routes(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request("GET", "/missing")
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request("POST", "/missing", {})
        self.assertEqual(cm.exception.code, 404)
