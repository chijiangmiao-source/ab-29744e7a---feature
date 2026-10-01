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
from app.builders import b64, b64_many, corrupt_byte, make_complete, make_page, make_prepare, make_transaction  # noqa: E402
from app.storage import FrozenStore  # noqa: E402

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32

class ServerHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = FrozenStore(os.path.join(self.tmp.name, "audits.json"))
        cstore = FrozenStore(os.path.join(self.tmp.name, "corrections.json"))
        self.httpd = build_server("127.0.0.1", 0, store, cstore)
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

    def post_corr(self, payload):
        req = urllib.request.Request(
            self.url("/api/corrections"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as r:
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


def _out_of_order_image():
    # page, prepare, complete: byte-clean but target tx 200 is not adopted
    return [
        make_page(2, "SLOT_B", P2, 0),
        make_prepare(200, 2, "SLOT_B", P2, 1),
        make_complete(200, 2, "SLOT_B", P2, 2),
    ]


class CorrectionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ServerHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_correction_minimum_arrangement_frozen_and_replayable(self):
        sectors = b64_many(_out_of_order_image())
        status, body = self.h.post_corr({
            "correction_id": "CORR-OK", "source_audit_id": "SRC-OK",
            "active_slot": "SLOT_A", "target_transaction": 200,
            "sectors": sectors})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["physical_sequence"], [1, 0, 2])
        self.assertEqual(body["swap_count"], 1)
        self.assertEqual(body["swap_steps"], [[0, 1]])
        self.assertEqual(body["target"]["slot"], "SLOT_B")
        self.assertEqual(body["target"]["generation"], 2)
        self.assertEqual(body["target"]["prepare_index"], 1)
        self.assertEqual(body["target"]["page_index"], 0)
        self.assertEqual(body["target"]["complete_index"], 2)
        self.assertTrue(body["frozen"])
        self.assertEqual(len(body["prefixes"]), 3)
        # no prefix shows the unfinished target page as bootable
        for p in body["prefixes"]:
            if p["target_bootable"]:
                self.assertTrue(p["target_complete_included"])
        self.assertEqual(body["prefixes"][-1]["boot_slot"], "SLOT_B")

        # re-open the frozen result by correction id
        status2, body2 = self.h.get("/api/corrections/CORR-OK")
        self.assertEqual(status2, 200)
        self.assertEqual(body2["physical_sequence"], [1, 0, 2])
        self.assertEqual(body2["swap_count"], 1)

    def test_correction_derives_bytes_from_frozen_source_audit(self):
        sectors = b64_many(_out_of_order_image())
        status, frozen_audit = self.h.post({
            "audit_id": "SRC-FROZEN", "active_slot": "SLOT_A",
            "sectors": sectors})
        self.assertEqual(status, 201)
        # correction referencing the frozen source with sectors OMITTED: the
        # server replays the frozen bytes and frozen active slot
        status, body = self.h.post_corr({
            "correction_id": "CORR-FROMSRC", "source_audit_id": "SRC-FROZEN",
            "active_slot": "SLOT_A", "target_transaction": 200})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["physical_sequence"], [1, 0, 2])
        self.assertEqual(body["source_audit_id"], "SRC-FROZEN")
        self.assertEqual(body["total_sectors"], 3)

    def test_correction_requires_sectors_when_source_not_frozen(self):
        status, body = self.h.post_corr({
            "correction_id": "CORR-NOSRC", "source_audit_id": "GHOST-AUDIT",
            "active_slot": "SLOT_A", "target_transaction": 200})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "source_not_frozen")

    def test_correction_source_bytes_must_match_frozen_audit(self):
        sectors = b64_many(_out_of_order_image())
        self.h.post({"audit_id": "SRC-LOCK", "active_slot": "SLOT_A",
                     "sectors": sectors})
        tampered = list(sectors)
        # different payload sector but still byte-valid -> byte content differs
        tampered[0] = b64_many([make_page(2, "SLOT_B", P1, 0)])[0]
        status, body = self.h.post_corr({
            "correction_id": "CORR-TAMPER", "source_audit_id": "SRC-LOCK",
            "active_slot": "SLOT_A", "target_transaction": 200,
            "sectors": tampered})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "source_mismatch")

    def test_same_correction_id_same_source_is_idempotent(self):
        sectors = b64_many(_out_of_order_image())
        payload = {"correction_id": "CORR-IDEM", "source_audit_id": "SRC-IDEM",
                   "active_slot": "SLOT_A", "target_transaction": 200,
                   "sectors": sectors}
        s1, b1 = self.h.post_corr(payload)
        self.assertEqual(s1, 201)
        s2, b2 = self.h.post_corr(payload)
        self.assertEqual(s2, 200)  # frozen plan returned, not a new freeze
        self.assertEqual(b2["physical_sequence"], b1["physical_sequence"])

    def test_same_correction_id_different_source_refused(self):
        sectors_a = b64_many(_out_of_order_image())
        sectors_b = b64_many([
            make_page(3, "SLOT_C", P1, 0),
            make_prepare(300, 3, "SLOT_C", P1, 1),
            make_complete(300, 3, "SLOT_C", P1, 2),
        ])
        s1, _ = self.h.post_corr({
            "correction_id": "CORR-DUP", "source_audit_id": "SRC-A",
            "active_slot": "SLOT_A", "target_transaction": 200,
            "sectors": sectors_a})
        self.assertEqual(s1, 201)
        s2, b2 = self.h.post_corr({
            "correction_id": "CORR-DUP", "source_audit_id": "SRC-B",
            "active_slot": "SLOT_A", "target_transaction": 300,
            "sectors": sectors_b})
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"], "correction_exists")
        # original frozen plan still readable and unchanged
        s3, b3 = self.h.get("/api/corrections/CORR-DUP")
        self.assertEqual(s3, 200)
        self.assertEqual(b3["target_transaction"], 200)

    def test_corrupt_source_refused(self):
        import base64
        sectors = b64_many(make_transaction(200, 2, "SLOT_B", P2))
        sectors[0] = b64(corrupt_byte(base64.b64decode(sectors[0]), 10))
        status, body = self.h.post_corr({
            "correction_id": "CORR-BAD", "source_audit_id": "SRC-BAD",
            "active_slot": "SLOT_A", "target_transaction": 200,
            "sectors": sectors})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "source_corrupt")

    def test_target_not_in_image_refused(self):
        sectors = b64_many(_out_of_order_image())
        status, body = self.h.post_corr({
            "correction_id": "CORR-NOPE", "source_audit_id": "SRC-X",
            "active_slot": "SLOT_A", "target_transaction": 4242,
            "sectors": sectors})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "target_not_in_image")

    def test_conflicting_transactions_refused(self):
        sectors = b64_many([
            make_prepare(7, 1, "SLOT_A", P1, 0),
            make_prepare(7, 2, "SLOT_B", P2, 1),
            make_page(1, "SLOT_A", P1, 2),
            make_complete(7, 1, "SLOT_A", P1, 3),
        ])
        status, body = self.h.post_corr({
            "correction_id": "CORR-CONF", "source_audit_id": "SRC-C",
            "active_slot": "SLOT_A", "target_transaction": 7,
            "sectors": sectors})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "conflicting_transactions")

    def test_correction_bad_requests_and_missing(self):
        sectors = b64_many(_out_of_order_image())
        # bad correction id
        status, _ = self.h.post_corr({
            "correction_id": "bad id!", "source_audit_id": "S",
            "active_slot": "SLOT_A", "target_transaction": 1,
            "sectors": sectors})
        self.assertEqual(status, 400)
        # bad target transaction type
        status, _ = self.h.post_corr({
            "correction_id": "C", "source_audit_id": "S",
            "active_slot": "SLOT_A", "target_transaction": "1",
            "sectors": sectors})
        self.assertEqual(status, 400)
        # > 12 sectors
        big = sectors * 5  # 15
        status, body = self.h.post_corr({
            "correction_id": "C", "source_audit_id": "S",
            "active_slot": "SLOT_A", "target_transaction": 1,
            "sectors": big})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "too_many_sectors")
        # unknown frozen correction
        status, body = self.h.get("/api/corrections/NO-SUCH-CORRECTION")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
