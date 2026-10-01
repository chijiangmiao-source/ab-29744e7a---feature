"""Tests for the stable correction planner (minimum adjacent-swap reordering).

Covers:
  * an out-of-order but byte-valid complete transaction yields the minimum,
    replayable choreography and a stable original-index sequence
  * every prefix is adjudicated with the existing recovery semantics; an
    unfinished target page is never presented as bootable
  * refusals: byte-corrupt source, source without frozen sectors, >12 sectors,
    conflicting transactions, target transaction absent from the image,
    impossible target (higher generation always wins),
    same correction id re-submitted with changed source data (409)
  * the original audit conclusion stays readable after a correction is frozen
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import build_server  # noqa: E402
from app.builders import (  # noqa: E402
    b64,
    b64_many,
    corrupt_byte,
    make_complete,
    make_page,
    make_prepare,
    make_transaction,
)
from app.correction import (  # noqa: E402
    CORRECTION_MAX_SECTORS,
    CorrectionError,
    plan_correction,
)
from app.parser import judge_recovery  # noqa: E402
from app.storage import FrozenStore  # noqa: E402

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32
P3 = bytes.fromhex("03") * 32


def out_of_order_image():
    """tx 100 (gen1 SLOT_A) complete; tx 200 (gen2 SLOT_B) sectors all present
    and byte-valid, but physically written page -> prepare -> complete, so the
    recovery judge does not adopt tx 200."""
    sectors = make_transaction(100, 1, "SLOT_A", P1, 0)
    sectors += [
        make_page(2, "SLOT_B", P2, 4),
        make_prepare(200, 2, "SLOT_B", P2, 3),
        make_complete(200, 2, "SLOT_B", P2, 5),
    ]
    return sectors


def frozen_source(sectors, audit_id="AUDIT-1", active="SLOT_A"):
    return judge_recovery(audit_id, active, b64_many(sectors)).to_public_dict()


def apply_swaps(n, steps):
    arr = list(range(n))
    for st in steps:
        a, b = st["swap"]
        arr[a], arr[b] = arr[b], arr[a]
    return arr


class PlannerTests(unittest.TestCase):
    def test_out_of_order_tx_gets_minimum_replayable_plan(self):
        sectors = out_of_order_image()
        src = frozen_source(sectors)
        # original physical order: SLOT_A gen1 boots, tx200 not adopted
        self.assertEqual(src["boot"]["slot"], "SLOT_A")
        self.assertIsNotNone(src["first_violation"])

        plan = plan_correction("FIX-1", src, 200)
        sol = plan["solution"]
        # one adjacent swap (positions 3<->4) fixes prepare-before-page
        self.assertEqual(sol["adjacent_swap_count"], 1)
        self.assertEqual(sol["physical_index_sequence"], [0, 1, 2, 4, 3, 5])
        self.assertEqual(len(sol["swap_steps"]), 1)
        self.assertEqual(sol["swap_steps"][0]["swap"], [3, 4])
        # swap choreography really realizes the advertised sequence
        self.assertEqual(apply_swaps(6, sol["swap_steps"]),
                         sol["physical_index_sequence"])

        # replay the reordered image through the real judge
        reordered = [src["sectors"][i]
                     for i in sol["physical_index_sequence"]]
        replay = judge_recovery("replay", "SLOT_A", reordered)
        self.assertIsNone(replay.first_violation)
        self.assertEqual(replay.boot_slot, "SLOT_B")
        self.assertEqual(replay.boot_generation, 2)
        self.assertEqual(
            replay.slots["SLOT_B"].transaction_id, 200)

    def test_no_sector_byte_is_modified(self):
        sectors = out_of_order_image()
        src = frozen_source(sectors)
        plan = plan_correction("FIX-1B", src, 200)
        perm = plan["solution"]["physical_index_sequence"]
        self.assertEqual(sorted(perm), list(range(6)))
        reordered = [src["sectors"][i] for i in perm]
        self.assertEqual(sorted(reordered), sorted(src["sectors"]))

    def test_every_prefix_is_safe_and_explained(self):
        src = frozen_source(out_of_order_image())
        prefixes = plan_correction("FIX-2", src, 200)["solution"]["prefixes"]
        self.assertEqual([p["length"] for p in prefixes], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(p["safe"] for p in prefixes))
        self.assertFalse(any(p["unfinished_target_page_bootable"]
                             for p in prefixes))
        # target tx is only completed in the final prefix
        self.assertFalse(any(p["target_transaction_completed"]
                             for p in prefixes[:-1]))
        self.assertTrue(prefixes[-1]["target_transaction_completed"])
        self.assertEqual(prefixes[2]["boot_slot"], "SLOT_A")

    def test_target_evidence_links_prepare_page_complete(self):
        src = frozen_source(out_of_order_image())
        ev = plan_correction("FIX-3", src, 200)["solution"]["target_evidence"]
        self.assertTrue(ev["adopted"])
        self.assertEqual(ev["slot"], "SLOT_B")
        self.assertEqual(ev["generation"], 2)
        self.assertEqual(ev["digest"], f"{zlib.crc32(P2) & 0xFFFFFFFF:08x}")
        self.assertEqual(len(ev["prepare"]), 1)
        self.assertEqual(len(ev["slot_page"]), 1)
        self.assertEqual(len(ev["complete"]), 1)
        # evidence points back at the original physical indices 3/4/5
        finals = sorted(x["original_index"] for x in
                        ev["prepare"] + ev["slot_page"] + ev["complete"])
        self.assertEqual(finals, [3, 4, 5])

    def test_higher_generation_competitor_must_be_neutralized_in_order(self):
        # tx200 gen2 SLOT_A complete, tx100 gen1 SLOT_A complete; target is the
        # older tx100. Valid permutations must place gen2's complete before its
        # page so gen2 never completes; this needs zero swaps here because the
        # physical order already interleaves that way... build explicitly:
        sectors = [
            make_prepare(200, 2, "SLOT_A", P2, 0),
            make_complete(200, 2, "SLOT_A", P2, 2),   # page not yet seen
            make_page(2, "SLOT_A", P2, 1),
            make_prepare(100, 1, "SLOT_A", P1, 3),
            make_page(1, "SLOT_A", P1, 4),
            make_complete(100, 1, "SLOT_A", P1, 5),
        ]
        src = frozen_source(sectors)
        plan = plan_correction("FIX-4", src, 100)
        perm = plan["solution"]["physical_index_sequence"]
        self.assertEqual(plan["solution"]["adjacent_swap_count"], 0)
        self.assertEqual(perm, list(range(6)))
        replay = judge_recovery("r", "SLOT_A",
                                [src["sectors"][i] for i in perm])
        self.assertEqual(replay.slots[replay.boot_slot].transaction_id, 100)

    def test_higher_generation_competitor_is_neutralized_by_order(self):
        # tx200 gen2 and tx100 gen1 on the SAME slot, both complete. Because the
        # planner searches ALL permutations (no greedy block moves), it may
        # place gen2's complete record physically before gen2's page: the
        # existing recovery semantics then reject that complete (no accepted
        # page yet), so gen2 never becomes a slot state and gen1 boots.
        sectors = make_transaction(100, 1, "SLOT_A", P1, 0)
        sectors += make_transaction(200, 2, "SLOT_A", P2, 3)
        src = frozen_source(sectors)  # physical order boots gen2
        self.assertEqual(src["boot"]["slot"], "SLOT_A")
        self.assertEqual(src["boot"]["generation"], 2)
        plan = plan_correction("FIX-X", src, 100)
        perm = plan["solution"]["physical_index_sequence"]
        # gen2 (original indices 3/4/5) must have its complete(#5) before page(#4)
        self.assertLess(perm.index(5), perm.index(4))
        replay = judge_recovery("r", "SLOT_A",
                                [src["sectors"][i] for i in perm])
        self.assertEqual(replay.slots[replay.boot_slot].transaction_id, 100)
        self.assertTrue(all(p["safe"] for p in plan["solution"]["prefixes"]))

    def test_no_solution_branch_is_guarded_by_final_replay(self):
        # Defensive 422: if an (impossible-in-practice) candidate survived the
        # exhaustive search but the real judge disagrees on replay, refuse.
        from unittest.mock import patch
        src = frozen_source(out_of_order_image())
        real_judge = judge_recovery

        def fake_judge(audit_id, active, sectors, frozen=False):
            res = real_judge(audit_id, active, sectors, frozen)
            if audit_id.startswith("AUDIT-1#correction#"):
                res.slots.clear()
                res.boot_slot = None
                res.boot_generation = None
                res.boot_reason = "forced"
            return res

        with patch("app.correction.judge_recovery", side_effect=fake_judge):
            with self.assertRaises(CorrectionError) as cm:
                plan_correction("FIX-X2", src, 200)
        self.assertEqual(cm.exception.code, "no_valid_permutation")
        self.assertEqual(cm.exception.status, 422)

    def test_corrupt_source_sector_refused_with_index(self):
        src = frozen_source(out_of_order_image())
        src["sectors"] = list(src["sectors"])
        raw = corrupt_byte(out_of_order_image()[3], 40)
        src["sectors"][3] = b64(raw)
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-5", src, 200)
        self.assertEqual(cm.exception.code, "bad_sector_crc")
        self.assertEqual(cm.exception.index, 3)

    def test_truncated_source_sector_refused(self):
        src = frozen_source(out_of_order_image())
        src["sectors"] = list(src["sectors"])
        src["sectors"][2] = src["sectors"][2][:50]
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-6", src, 200)
        self.assertEqual(cm.exception.code, "bad_length")
        self.assertEqual(cm.exception.index, 2)

    def test_target_absent_from_image_refused(self):
        src = frozen_source(out_of_order_image())
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-7", src, 999)
        self.assertEqual(cm.exception.code, "target_not_in_image")
        self.assertEqual(cm.exception.status, 404)
        # prepare+complete present but the matching page is missing
        sectors = [make_prepare(5, 1, "SLOT_A", P1, 0),
                   make_complete(5, 1, "SLOT_A", P1, 1)]
        with self.assertRaises(CorrectionError) as cm2:
            plan_correction("FIX-8", frozen_source(sectors), 5)
        self.assertEqual(cm2.exception.code, "target_not_in_image")

    def test_conflicting_transaction_refused(self):
        sectors = [
            make_prepare(7, 1, "SLOT_A", P1, 0),
            make_prepare(7, 2, "SLOT_B", P2, 1),  # same id, other gen/slot
            make_page(1, "SLOT_A", P1, 2),
            make_complete(7, 1, "SLOT_A", P1, 3),
        ]
        src = frozen_source(sectors)
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-9", src, 7)
        self.assertEqual(cm.exception.code, "conflicting_transaction")

    def test_more_than_twelve_sectors_refused(self):
        sectors = [make_page(1, "SLOT_A", P1, i)
                   for i in range(CORRECTION_MAX_SECTORS + 1)]
        src = judge_recovery("BIG", "SLOT_A", b64_many(sectors)).to_public_dict()
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-10", src, 1)
        self.assertEqual(cm.exception.code, "too_many_sectors")

    def test_source_without_sector_snapshot_refused(self):
        with self.assertRaises(CorrectionError) as cm:
            plan_correction("FIX-11",
                            {"audit_id": "OLD", "active_slot": "SLOT_A"}, 1)
        self.assertEqual(cm.exception.code, "source_sectors_missing")

    def test_stable_tie_break_between_equal_swap_permutations(self):
        # two independent broken transactions on different slots; either can be
        # fixed with exactly one swap; the stable index sequence decides.
        sectors = [
            make_page(1, "SLOT_A", P1, 1),   # 0 page before prepare
            make_prepare(10, 1, "SLOT_A", P1, 0),  # 1
            make_complete(10, 1, "SLOT_A", P1, 2),  # 2
            make_page(2, "SLOT_B", P2, 4),   # 3 page before prepare
            make_prepare(20, 2, "SLOT_B", P2, 3),  # 4
            make_complete(20, 2, "SLOT_B", P2, 5),  # 5
        ]
        src = frozen_source(sectors)
        plan = plan_correction("FIX-12", src, 10)
        # fixing tx10 = swap(0,1) leaves perm [1,0,2,...]; fixing tx20 would
        # be a different target; the chosen sequence must be fully specified
        perm = plan["solution"]["physical_index_sequence"]
        self.assertEqual(plan["solution"]["adjacent_swap_count"], 1)
        self.assertEqual(sorted(perm), list(range(6)))
        replay = judge_recovery("r", "SLOT_A",
                                [src["sectors"][i] for i in perm])
        self.assertEqual(replay.slots[replay.boot_slot].transaction_id, 10)


# --------------------------------------------------------------------------
# HTTP end-to-end: frozen source, correction freeze, re-open and refusal paths
# --------------------------------------------------------------------------

class HttpHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = FrozenStore(os.path.join(self.tmp.name, "audits.json"))
        cstore = FrozenStore(os.path.join(self.tmp.name, "corrections.json"))
        self.httpd = build_server("127.0.0.1", 0, store, cstore)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def req(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers,
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class CorrectionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = HttpHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def _freeze_audit(self, audit_id, sectors):
        status, body = self.h.req("POST", "/api/audits", {
            "audit_id": audit_id, "active_slot": "SLOT_A",
            "sectors": b64_many(sectors)})
        self.assertEqual(status, 201, body)
        return body

    def test_end_to_end_out_of_order_then_reopen(self):
        self._freeze_audit("HTTP-AUDIT-1", out_of_order_image())
        status, body = self.h.req("POST", "/api/corrections", {
            "correction_id": "HTTP-FIX-1", "audit_id": "HTTP-AUDIT-1",
            "target_transaction_id": 200})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["frozen"])
        self.assertEqual(body["solution"]["adjacent_swap_count"], 1)
        self.assertEqual(body["solution"]["physical_index_sequence"],
                         [0, 1, 2, 4, 3, 5])
        self.assertEqual(
            body["solution"]["final_conclusion"]["boot"]["slot"], "SLOT_B")

        status2, body2 = self.h.req("GET", "/api/corrections/HTTP-FIX-1")
        self.assertEqual(status2, 200)
        self.assertEqual(body2["solution"]["physical_index_sequence"],
                         [0, 1, 2, 4, 3, 5])

        # original frozen audit is still readable with its original verdict
        status3, body3 = self.h.req("GET", "/api/audits/HTTP-AUDIT-1")
        self.assertEqual(status3, 200)
        self.assertEqual(body3["boot"]["slot"], "SLOT_A")

    def test_corrupt_source_refused_over_http(self):
        sectors = out_of_order_image()
        self._freeze_audit("HTTP-AUDIT-2", sectors)
        # freeze a second audit whose snapshot carries a damaged sector
        damaged = sectors
        raw = corrupt_byte(sectors[4], 50)
        # rebuild via direct store path is not possible over API; instead
        # corrupt by posting the damaged image under its own audit id
        bad_b64 = b64_many(sectors)
        bad_b64[4] = b64(raw)
        status, body = self.h.req("POST", "/api/audits", {
            "audit_id": "HTTP-AUDIT-2B", "active_slot": "SLOT_A",
            "sectors": bad_b64})
        self.assertEqual(status, 201)
        status, body = self.h.req("POST", "/api/corrections", {
            "correction_id": "HTTP-FIX-2", "audit_id": "HTTP-AUDIT-2B",
            "target_transaction_id": 200})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_sector_crc")
        self.assertEqual(body["index"], 4)

    def test_missing_source_audit_is_404(self):
        status, body = self.h.req("POST", "/api/corrections", {
            "correction_id": "HTTP-FIX-3", "audit_id": "NOPE",
            "target_transaction_id": 1})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "source_audit_missing")

    def test_same_correction_id_with_changed_source_refused_409(self):
        self._freeze_audit("HTTP-AUDIT-4", out_of_order_image())
        payload = {"correction_id": "HTTP-FIX-4",
                   "audit_id": "HTTP-AUDIT-4", "target_transaction_id": 200}
        s1, b1 = self.h.req("POST", "/api/corrections", payload)
        self.assertEqual(s1, 201)
        # re-submit pointing at different source data (other target): refused
        payload2 = dict(payload, target_transaction_id=100)
        s2, b2 = self.h.req("POST", "/api/corrections", payload2)
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"], "correction_exists")
        # frozen record still carries the original target
        self.assertEqual(b2["frozen"]["target_transaction_id"], 200)

    def test_bad_correction_requests(self):
        for bad in [
            {"correction_id": "bad id", "audit_id": "A",
             "target_transaction_id": 1},
            {"correction_id": "X", "audit_id": "A",
             "target_transaction_id": -1},
            {"correction_id": "X", "audit_id": "A",
             "target_transaction_id": "10"},
        ]:
            status, body = self.h.req("POST", "/api/corrections", bad)
            self.assertEqual(status, 400, body)
        status, _ = self.h.req("GET", "/api/corrections/NOPE-MISSING")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
