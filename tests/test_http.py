"""HTTP smoke tests: exercise the real API server over a live socket."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import build_server  # noqa: E402
from app.builders import b64_many, corrupt_byte, make_complete, make_page, make_prepare, make_transaction  # noqa: E402
from app.storage import FrozenStore  # noqa: E402

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32


class ServerHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = FrozenStore(os.path.join(self.tmp.name, "audits.json"))
        self.httpd = build_server("127.0.0.1", 0, store)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, payload):
        req = urllib.request.Request(
            self.url("/api/audits"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ServerHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_healthz(self):
        status, body = self.h.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_page_served(self):
        with urllib.request.urlopen(self.h.url("/"), timeout=5) as r:
            html = r.read().decode()
        self.assertIn("恢复审查", html)
        self.assertIn("/api/audits", html)

    def test_clean_switch_end_to_end(self):
        sectors = make_transaction(1, 1, "SLOT_A", P1)
        sectors += make_transaction(2, 2, "SLOT_B", P2, 3)
        status, body = self.h.post({"audit_id": "SMOKE-CLEAN",
                                    "active_slot": "SLOT_A",
                                    "sectors": b64_many(sectors)})
        self.assertEqual(status, 201)
        self.assertIsNone(body["first_violation"])
        self.assertEqual(body["boot"]["slot"], "SLOT_B")
        self.assertEqual(body["boot"]["generation"], 2)
        self.assertTrue(body["frozen"])
        # frozen retrieval
        status2, body2 = self.h.get("/api/audits/SMOKE-CLEAN")
        self.assertEqual(status2, 200)
        self.assertEqual(body2["boot"]["slot"], "SLOT_B")

    def test_corrupt_complete_reported_and_frozen(self):
        sectors = make_transaction(1, 1, "SLOT_A", P1)
        sectors += [
            make_prepare(2, 2, "SLOT_B", P2, 3),
            make_page(2, "SLOT_B", P2, 4),
            corrupt_byte(make_complete(2, 2, "SLOT_B", P2, 5), 40),
        ]
        status, body = self.h.post({"audit_id": "SMOKE-CORRUPT",
                                    "active_slot": "SLOT_A",
                                    "sectors": b64_many(sectors)})
        self.assertEqual(status, 201)
        self.assertEqual(body["first_violation"]["index"], 5)
        self.assertEqual(body["first_violation"]["code"], "bad_sector_crc")
        self.assertEqual(body["boot"]["slot"], "SLOT_A")
        self.assertEqual(body["boot"]["generation"], 1)

    def test_resubmit_same_audit_is_rejected_with_frozen_copy(self):
        sectors = b64_many(make_transaction(1, 1, "SLOT_A", P1))
        s1, b1 = self.h.post({"audit_id": "SMOKE-ONCE", "active_slot": "SLOT_A",
                              "sectors": sectors})
        self.assertEqual(s1, 201)
        s2, b2 = self.h.post({"audit_id": "SMOKE-ONCE", "active_slot": "SLOT_A",
                              "sectors": sectors})
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"], "audit_exists")
        self.assertEqual(b2["frozen"]["boot"]["slot"], "SLOT_A")

    def test_bad_requests(self):
        status, body = self.h.post({"audit_id": "bad id!", "active_slot": "SLOT_A",
                                    "sectors": ["x"]})
        self.assertEqual(status, 400)
        status, body = self.h.post({"audit_id": "X", "active_slot": "SLOT_A",
                                    "sectors": []})
        self.assertEqual(status, 400)
        status, body = self.h.get("/api/audits/NOPE-MISSING")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
