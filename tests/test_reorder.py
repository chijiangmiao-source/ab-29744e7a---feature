"""Tests for the minimal adjacent-transposition replay correction.

Covers the whole contract:
  * out-of-order but byte-clean target transaction -> replayable minimum
  * minimum adjacent-swap count + stable original-index tie break
  * every prefix replayed, unfinished target page never bootable
  * competing valid transaction chains are never scrambled to steal boot
  * refusals: source missing / byte corrupt / conflicting transactions /
    target absent, >12 sectors
  * no sector byte is modified (result is a pure permutation)
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.builders import (  # noqa: E402
    b64,
    b64_many,
    corrupt_byte,
    make_complete,
    make_page,
    make_prepare,
    make_transaction,
)
from app.parser import judge_recovery  # noqa: E402
from app.reorder import (  # noqa: E402
    ReorderError,
    analyze_source,
    plan_correction,
)

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32
P3 = bytes.fromhex("03") * 32


def plan(sectors, tx, active="SLOT_A", cid="C1", sid="S1"):
    return plan_correction(cid, sid, active, b64_many(sectors), tx)


def is_permutation(seq, n):
    return sorted(seq) == list(range(n))


def inversion_count(seq):
    return sum(1 for a in range(len(seq)) for b in range(a + 1, len(seq))
               if seq[a] > seq[b])


class MinimalArrangementTests(unittest.TestCase):
    def test_page_before_prepare_one_swap(self):
        # physical: page, prepare, complete -> page is orphaned, tx not adopted
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_prepare(200, 2, "SLOT_B", P2, 1),
            make_complete(200, 2, "SLOT_B", P2, 2),
        ]
        before = judge_recovery("S", "SLOT_A", b64_many(sectors))
        self.assertIsNone(before.boot_slot)
        self.assertEqual(before.first_violation["code"], "page_before_prepare")

        p = plan(sectors, 200)
        self.assertEqual(p.physical_sequence, [1, 0, 2])
        self.assertEqual(p.swap_count, 1)
        self.assertEqual(p.swap_steps, [[0, 1]])

    def test_complete_first_needs_two_swaps(self):
        # complete, page, prepare -> prepare must lead: [2,1,0], 3 inversions
        sectors = [
            make_complete(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 2),
            make_prepare(200, 2, "SLOT_B", P2, 1),
        ]
        p = plan(sectors, 200)
        self.assertEqual(p.physical_sequence, [2, 1, 0])
        self.assertEqual(p.swap_count, 3)
        self.assertEqual(len(p.swap_steps), 3)

    def test_already_ordered_needs_zero_swaps(self):
        p = plan(make_transaction(200, 2, "SLOT_B", P2), 200)
        self.assertEqual(p.physical_sequence, [0, 1, 2])
        self.assertEqual(p.swap_count, 0)
        self.assertEqual(p.swap_steps, [])

    def test_bytes_unchanged_pure_permutation(self):
        sectors = [
            make_complete(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 2),
            make_prepare(200, 2, "SLOT_B", P2, 1),
        ]
        src = b64_many(sectors)
        p = plan(sectors, 200)
        self.assertTrue(is_permutation(p.physical_sequence, 3))
        reordered = [src[i] for i in p.physical_sequence]
        self.assertEqual(sorted(reordered), sorted(src))
        # and the replayed arrangement truly boots the target
        verdict = judge_recovery("S", "SLOT_A", reordered)
        self.assertEqual(verdict.boot_slot, "SLOT_B")
        self.assertEqual(verdict.boot_generation, 2)

    def test_target_evidence_indices(self):
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_prepare(200, 2, "SLOT_B", P2, 1),
            make_complete(200, 2, "SLOT_B", P2, 2),
        ]
        p = plan(sectors, 200)
        self.assertEqual(p.prepare_index, 1)
        self.assertEqual(p.page_index, 0)
        self.assertEqual(p.complete_index, 2)
        self.assertEqual(p.target_digest, f"{__import__('zlib').crc32(P2) & 0xFFFFFFFF:08x}")
        self.assertEqual(p.target_payload_hex, P2.hex())


class PrefixSafetyTests(unittest.TestCase):
    def test_every_prefix_replayed_and_unfinished_page_never_bootable(self):
        # two valid complete chains, both scrambled; target = SLOT_B gen2
        sectors = [
            make_complete(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 1),
            make_complete(100, 1, "SLOT_A", P1, 2),
            make_prepare(100, 1, "SLOT_A", P1, 3),
            make_page(1, "SLOT_A", P1, 4),
            make_prepare(200, 2, "SLOT_B", P2, 5),
        ]
        p = plan(sectors, 200)
        self.assertEqual(len(p.prefixes), 6)
        for pv in p.prefixes:
            if pv.target_bootable:
                self.assertTrue(
                    pv.target_complete_included,
                    f"prefix {pv.length} booted target before its complete")
            else:
                self.assertFalse(pv.target_complete_included is False
                                 and pv.boot_slot == "SLOT_B")
        # full prefix boots target
        last = p.prefixes[-1]
        self.assertEqual(last.boot_slot, "SLOT_B")
        self.assertEqual(last.boot_generation, 2)
        self.assertTrue(last.target_complete_included)
        self.assertTrue(last.target_bootable)

    def test_dangling_and_orphan_records_need_not_move(self):
        # target tx200 scrambled; an orphan complete (tx999) and an orphan page
        # (SLOT_Z, no prepare) can stay at the end without spending swaps.
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_prepare(200, 2, "SLOT_B", P2, 1),
            make_complete(200, 2, "SLOT_B", P2, 2),
            make_complete(999, 1, "SLOT_Z", P1, 3),
            make_page(8, "SLOT_Z", P3, 4),
        ]
        p = plan(sectors, 200)
        # only prepare/page swap required; orphans remain at the tail
        self.assertEqual(p.swap_count, 1)
        self.assertEqual(p.physical_sequence[:3], [1, 0, 2])
        self.assertEqual(p.physical_sequence[-2:], [3, 4])


class CompetingTransactionTests(unittest.TestCase):
    def test_higher_generation_competitor_can_be_neutralised_by_order(self):
        # Over ALL permutations the target can boot: the higher-generation
        # competitor is simply left in a non-adopting order (its complete is
        # written before its matching page). The acceptance rule only requires
        # the final boot to be the target and no prefix to show the unfinished
        # target page bootable; it does not privilege other transactions.
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_prepare(200, 2, "SLOT_B", P2, 1),
            make_complete(200, 2, "SLOT_B", P2, 2),
            make_prepare(300, 3, "SLOT_C", P3, 3),
            make_page(3, "SLOT_C", P3, 4),
            make_complete(300, 3, "SLOT_C", P3, 5),
        ]
        p = plan(sectors, 200)
        seq = p.physical_sequence
        verdict = judge_recovery("S", "SLOT_A",
                                 [b64_many(sectors)[i] for i in seq])
        self.assertEqual(verdict.boot_slot, "SLOT_B")
        self.assertEqual(verdict.boot_generation, 2)
        self.assertNotIn("SLOT_C", verdict.slots)
        # minimum: one swap to fix the target, one to neutralise the competitor
        self.assertEqual(p.swap_count, 2)
        # target's own chain stays prepare<page<complete
        self.assertLess(seq.index(1), seq.index(0))
        self.assertLess(seq.index(0), seq.index(2))

    def test_same_generation_name_order_competitor_can_be_neutralised(self):
        # SLOT_A would win the stable tie only if it adopts; with its records
        # placed out of adopting order, SLOT_B (target) boots.
        sectors = [
            make_prepare(1, 5, "SLOT_B", P2, 0),
            make_page(5, "SLOT_B", P2, 1),
            make_complete(1, 5, "SLOT_B", P2, 2),
            make_prepare(2, 5, "SLOT_A", P1, 3),
            make_page(5, "SLOT_A", P1, 4),
            make_complete(2, 5, "SLOT_A", P1, 5),
        ]
        p = plan(sectors, 1)
        seq = p.physical_sequence
        verdict = judge_recovery("S", "SLOT_A",
                                 [b64_many(sectors)[i] for i in seq])
        self.assertEqual(verdict.boot_slot, "SLOT_B")
        self.assertNotIn("SLOT_A", verdict.slots)
        # leaving the records as-is adopts SLOT_A, so at least one swap exists
        self.assertGreaterEqual(p.swap_count, 1)

    def test_lower_generation_competitor_does_not_block_target(self):
        # target gen2 SLOT_B; complete gen1 SLOT_A present and stays intact
        sectors = [
            make_complete(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 1),
            make_prepare(100, 1, "SLOT_A", P1, 2),
            make_page(1, "SLOT_A", P1, 3),
            make_complete(100, 1, "SLOT_A", P1, 4),
            make_prepare(200, 2, "SLOT_B", P2, 5),
        ]
        p = plan(sectors, 200)
        seq = p.physical_sequence
        # competitor chain remains prepare<page<complete (never scrambled)
        self.assertLess(seq.index(2), seq.index(3))
        self.assertLess(seq.index(3), seq.index(4))
        self.assertEqual(p.swap_count, inversion_count(seq))


class RefusalTests(unittest.TestCase):
    def test_corrupt_source_refused(self):
        good = make_transaction(200, 2, "SLOT_B", P2)
        src = b64_many(good)
        raw = bytearray(__import__("base64").b64decode(src[0]))
        raw[10] ^= 0xFF
        src[0] = b64(bytes(raw))
        with self.assertRaises(ReorderError) as cm:
            plan_correction("C", "S", "SLOT_A", src, 200)
        self.assertEqual(cm.exception.code, "source_corrupt")
        self.assertEqual(cm.exception.detail["index"], 0)

    def test_target_prepare_absent(self):
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_complete(200, 2, "SLOT_B", P2, 1),
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 200)
        self.assertEqual(cm.exception.code, "target_not_in_image")

    def test_target_complete_absent(self):
        sectors = [
            make_prepare(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 1),
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 200)
        self.assertEqual(cm.exception.code, "target_not_in_image")

    def test_target_page_absent(self):
        sectors = [
            make_prepare(200, 2, "SLOT_B", P2, 0),
            make_complete(200, 2, "SLOT_B", P2, 1),
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 200)
        self.assertEqual(cm.exception.code, "target_not_in_image")

    def test_target_complete_fields_mismatch_prepare(self):
        sectors = [
            make_prepare(200, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 1),
            make_complete(200, 3, "SLOT_B", P2, 2),  # generation differs
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 200)
        self.assertEqual(cm.exception.code, "target_not_in_image")

    def test_unknown_target_transaction(self):
        sectors = make_transaction(200, 2, "SLOT_B", P2)
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 999)
        self.assertEqual(cm.exception.code, "target_not_in_image")

    def test_conflicting_duplicate_prepare_refused(self):
        sectors = [
            make_prepare(7, 1, "SLOT_A", P1, 0),
            make_prepare(7, 2, "SLOT_B", P2, 1),  # same id, different content
            make_page(1, "SLOT_A", P1, 2),
            make_complete(7, 1, "SLOT_A", P1, 3),
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 7)
        self.assertEqual(cm.exception.code, "conflicting_transactions")

    def test_conflicting_same_generation_slot_prepares_refused(self):
        sectors = [
            make_prepare(1, 1, "SLOT_A", P1, 0),
            make_prepare(2, 1, "SLOT_A", P2, 1),  # page ownership ambiguous
            make_page(1, "SLOT_A", P1, 2),
            make_complete(1, 1, "SLOT_A", P1, 3),
        ]
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 1)
        self.assertEqual(cm.exception.code, "conflicting_transactions")

    def test_too_many_sectors_refused(self):
        # 13 clean sectors (four transactions + one) exceeds the cap of 12
        sectors = make_transaction(1, 1, "SLOT_A", P1, 0)
        sectors += make_transaction(2, 2, "SLOT_B", P2, 3)
        sectors += make_transaction(3, 3, "SLOT_C", P3, 6)
        sectors += [make_page(9, "SLOT_D", P1, 9)]
        self.assertEqual(len(sectors), 10)
        sectors += [make_page(9, "SLOT_D", P2, i) for i in range(3)]
        self.assertEqual(len(sectors), 13)
        with self.assertRaises(ReorderError) as cm:
            plan(sectors, 1)
        self.assertEqual(cm.exception.code, "source_too_large")

    def test_empty_source_refused(self):
        with self.assertRaises(ReorderError) as cm:
            plan_correction("C", "S", "SLOT_A", [], 1)
        self.assertEqual(cm.exception.code, "empty_source")


class StableTieBreakTests(unittest.TestCase):
    def test_minimal_count_then_lexicographic_index_sequence(self):
        # Two complete chains already individually ordered; interleaving needs
        # zero swaps, so identity is the stable answer.
        sectors = [
            make_prepare(1, 1, "SLOT_A", P1, 0),
            make_prepare(2, 2, "SLOT_B", P2, 1),
            make_page(1, "SLOT_A", P1, 2),
            make_page(2, "SLOT_B", P2, 3),
            make_complete(1, 1, "SLOT_A", P1, 4),
            make_complete(2, 2, "SLOT_B", P2, 5),
        ]
        p = plan(sectors, 2)  # target is the later chain
        self.assertEqual(p.swap_count, 0)
        self.assertEqual(p.physical_sequence, [0, 1, 2, 3, 4, 5])

    def test_extra_matching_page_after_complete_is_free(self):
        # prepare,page,complete,extra-matching-page is a valid adoption with 0
        # swaps; the trailing page must not be dragged into the chain.
        sectors = [
            make_prepare(1, 2, "SLOT_B", P2, 0),
            make_page(2, "SLOT_B", P2, 1),
            make_complete(1, 2, "SLOT_B", P2, 2),
            make_page(2, "SLOT_B", P2, 3),
        ]
        p = plan(sectors, 1)
        self.assertEqual(p.swap_count, 0)
        self.assertEqual(p.physical_sequence, [0, 1, 2, 3])


class PublicDictTests(unittest.TestCase):
    def test_public_dict_shape(self):
        sectors = [
            make_page(2, "SLOT_B", P2, 0),
            make_prepare(200, 2, "SLOT_B", P2, 1),
            make_complete(200, 2, "SLOT_B", P2, 2),
        ]
        d = plan(sectors, 200).to_public_dict()
        self.assertEqual(d["kind"], "reorder_correction")
        self.assertTrue(d["frozen"])
        self.assertEqual(d["correction_id"], "C1")
        self.assertEqual(d["source_audit_id"], "S1")
        self.assertEqual(d["target_transaction"], 200)
        self.assertEqual(d["physical_sequence"], [1, 0, 2])
        self.assertEqual(d["swap_count"], 1)
        self.assertEqual(len(d["prefixes"]), 3)
        # source bytes, active slot and original verdict are frozen with it
        self.assertEqual(d["source_sectors"], b64_many(sectors))
        sv = d["source_verdict"]
        self.assertIsNone(sv["boot_slot"])
        self.assertFalse(sv["target_adopted_originally"])
        self.assertEqual(sv["first_violation"]["code"], "page_before_prepare")
        for key in ("transaction_id", "slot", "generation", "digest",
                    "payload_hex", "prepare_index", "page_index",
                    "complete_index"):
            self.assertIn(key, d["target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
