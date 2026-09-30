"""Code-level tests for the byte parser and recovery judge.

Scenarios required by the audit contract:
  * complete, clean switchover (prepare -> page -> complete, multiple gens)
  * corrupted completion marker (power loss mid-write)
  * last valid generation survives subsequent invalid writes
  * plus: truncation, bad checksum, inconsistent duplicate tx id,
    complete pointing at an unfinished page, same-generation stable slot tie.
"""

from __future__ import annotations

import os
import sys
import unittest
import zlib

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
from app.parser import (  # noqa: E402
    MAX_SECTORS,
    V_COMPLETE_NO_PREPARE,
    V_DUP_TX_INCONSISTENT,
    V_PAGE_BEFORE_PREPARE,
    V_PAYLOAD_CRC,
    V_SECTOR_CRC,
    judge_recovery,
)
from app.storage import AuditExistsError, FrozenStore  # noqa: E402
import tempfile  # noqa: E402

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32
P3 = bytes.fromhex("03") * 32


def run(sectors, active="SLOT_A", audit="A1"):
    return judge_recovery(audit, active, b64_many(sectors))


class CleanSwitchTests(unittest.TestCase):
    def test_single_full_transaction_boots(self):
        r = run(make_transaction(1, 1, "SLOT_A", P1))
        self.assertIsNone(r.first_violation)
        self.assertEqual(r.boot_slot, "SLOT_A")
        self.assertEqual(r.boot_generation, 1)
        adopted = [d for d in r.decisions if d.adopted]
        self.assertEqual(len(adopted), 3)
        self.assertEqual([d.kind for d in adopted], ["prepare", "slot_page", "complete"])

    def test_higher_generation_wins_never_latest_page_number_alone(self):
        sectors = make_transaction(10, 2, "SLOT_B", P2, 0)
        sectors += make_transaction(20, 1, "SLOT_A", P1, 3)
        r = run(sectors)
        self.assertIsNone(r.first_violation)
        # gen 2 SLOT_B wins even though SLOT_A was written later
        self.assertEqual(r.boot_slot, "SLOT_B")
        self.assertEqual(r.boot_generation, 2)

    def test_same_generation_tie_breaks_on_slot_name_stably(self):
        sectors = make_transaction(1, 5, "SLOT_Z", P1, 0)
        sectors += make_transaction(2, 5, "SLOT_A", P2, 3)
        r = run(sectors)
        self.assertEqual(r.boot_generation, 5)
        self.assertEqual(r.boot_slot, "SLOT_A")  # lexicographic, stable

    def test_initial_active_slot_does_not_override_generation(self):
        r = run(make_transaction(1, 9, "SLOT_B", P2), active="SLOT_A")
        self.assertEqual(r.boot_slot, "SLOT_B")


class CorruptCompletionTests(unittest.TestCase):
    def _power_loss_image(self):
        sectors = make_transaction(100, 1, "SLOT_A", P1, 0)
        sectors.append(make_prepare(200, 2, "SLOT_B", P2, 3))
        sectors.append(make_page(2, "SLOT_B", P2, 4))
        sectors.append(corrupt_byte(make_complete(200, 2, "SLOT_B", P2, 5), 40))
        return sectors

    def test_broken_complete_crc_locates_first_violation(self):
        r = run(self._power_loss_image())
        v = r.first_violation
        self.assertIsNotNone(v)
        self.assertEqual(v["index"], 5)
        self.assertEqual(v["code"], V_SECTOR_CRC)

    def test_half_written_newer_slot_must_not_boot(self):
        r = run(self._power_loss_image())
        # the corrupt newer gen-2 slot is absent; gen-1 SLOT_A boots
        self.assertNotIn("SLOT_B", r.slots)
        self.assertEqual(r.boot_slot, "SLOT_A")
        self.assertEqual(r.boot_generation, 1)
        last = r.decisions[-1]
        self.assertFalse(last.adopted)
        self.assertEqual(last.violation, V_SECTOR_CRC)

    def test_payload_crc_corruption_rejects_page(self):
        sectors = make_transaction(1, 1, "SLOT_A", P1)
        sectors[1] = make_page(1, "SLOT_A", P1, 1,
                               payload_crc=(zlib.crc32(P1) ^ 0xDEADBEEF) & 0xFFFFFFFF)
        r = run(sectors)
        self.assertEqual(r.first_violation["index"], 1)
        self.assertEqual(r.first_violation["code"], V_PAYLOAD_CRC)
        self.assertIsNone(r.boot_slot)

    def test_truncated_base64_sector_is_located(self):
        sectors = b64_many(make_transaction(1, 1, "SLOT_A", P1))
        sectors[2] = sectors[2][:40]  # half-written line
        r = judge_recovery("A1", "SLOT_A", sectors)
        self.assertEqual(r.first_violation["index"], 2)
        self.assertEqual(r.first_violation["code"], "bad_length")
        # prepare + page present, but no complete => nothing boots
        self.assertIsNone(r.boot_slot)

    def test_truncated_raw_sector_bytes(self):
        raw = make_complete(1, 1, "SLOT_A", P1, 2)[:30]
        sectors = b64_many(make_transaction(1, 1, "SLOT_A", P1)[:2]) + [b64(raw + b"\x00" * 34)]
        # rebuild: truncate content but padding makes CRC fail, not length;
        # here just assert the violation is detected at index 2
        r = judge_recovery("A1", "SLOT_A", sectors)
        self.assertEqual(r.first_violation["index"], 2)
        self.assertIn(r.first_violation["code"], (V_SECTOR_CRC, "bad_reserved"))


class TransactionalViolationTests(unittest.TestCase):
    def test_complete_without_prepare_is_dangling(self):
        sectors = [make_complete(99, 1, "SLOT_A", P1, 0)]
        r = run(sectors)
        self.assertEqual(r.first_violation["code"], V_COMPLETE_NO_PREPARE)
        self.assertEqual(r.first_violation["index"], 0)
        self.assertIsNone(r.boot_slot)

    def test_complete_pointing_at_unfinished_page(self):
        # prepare + complete, page missing entirely
        sectors = [
            make_prepare(1, 1, "SLOT_A", P1, 0),
            make_complete(1, 1, "SLOT_A", P1, 1),
        ]
        r = run(sectors)
        self.assertEqual(r.first_violation["index"], 1)
        self.assertEqual(r.first_violation["code"], V_COMPLETE_NO_PREPARE)
        self.assertIsNone(r.boot_slot)

    def test_page_before_prepare_rejected(self):
        sectors = [make_page(1, "SLOT_A", P1, 0)]
        r = run(sectors)
        self.assertEqual(r.first_violation["code"], V_PAGE_BEFORE_PREPARE)

    def test_duplicate_transaction_id_with_conflicting_content(self):
        sectors = [
            make_prepare(7, 1, "SLOT_A", P1, 0),
            make_prepare(7, 2, "SLOT_B", P2, 1),  # same id, different gen/slot
        ]
        r = run(sectors)
        self.assertEqual(r.first_violation["index"], 1)
        self.assertEqual(r.first_violation["code"], V_DUP_TX_INCONSISTENT)

    def test_duplicate_consistent_prepare_is_ignored_not_fatal(self):
        sectors = [
            make_prepare(7, 1, "SLOT_A", P1, 0),
            make_prepare(7, 1, "SLOT_A", P1, 1),
            make_page(1, "SLOT_A", P1, 2),
            make_complete(7, 1, "SLOT_A", P1, 3),
        ]
        r = run(sectors)
        self.assertIsNone(r.first_violation)
        self.assertEqual(r.boot_slot, "SLOT_A")

    def test_page_digest_mismatch_does_not_complete(self):
        sectors = [
            make_prepare(1, 1, "SLOT_A", P1, 0),
            make_page(1, "SLOT_A", P2, 1),          # different payload
            make_complete(1, 1, "SLOT_A", P1, 2),
        ]
        r = run(sectors)
        self.assertEqual(r.first_violation["index"], 1)
        self.assertEqual(r.first_violation["code"], "page_tx_mismatch")
        # complete then finds no accepted page
        self.assertIsNone(r.boot_slot)

    def test_complete_fields_mismatch_prepare(self):
        sectors = [
            make_prepare(1, 1, "SLOT_A", P1, 0),
            make_page(1, "SLOT_A", P1, 1),
            make_complete(1, 2, "SLOT_A", P1, 2),   # generation altered
        ]
        r = run(sectors)
        self.assertEqual(r.first_violation["code"], "complete_tx_mismatch")
        self.assertIsNone(r.boot_slot)

    def test_completed_marker_alone_never_boots(self):
        # prepare+page+complete for gen1, then a fresh "complete" with an
        # unknown id must not fabricate a bootable slot
        sectors = make_transaction(1, 1, "SLOT_A", P1)
        sectors.append(make_complete(2, 7, "SLOT_B", P2, 3))
        r = run(sectors)
        self.assertEqual(r.first_violation["index"], 3)
        self.assertEqual(r.boot_slot, "SLOT_A")
        self.assertEqual(r.boot_generation, 1)


class RetentionTests(unittest.TestCase):
    def test_older_valid_generation_cannot_roll_back_slot(self):
        sectors = make_transaction(300, 3, "SLOT_A", P3, 0)
        sectors += [
            make_prepare(301, 2, "SLOT_A", P1, 3),
            make_page(2, "SLOT_A", P1, 4),
            make_complete(301, 2, "SLOT_A", P1, 5),
        ]
        r = run(sectors)
        self.assertEqual(r.slots["SLOT_A"].generation, 3)
        self.assertEqual(r.slots["SLOT_A"].payload, P3)
        self.assertEqual(r.boot_generation, 3)
        # the older complete is shown as discarded with an explicit basis
        discarded = [d for d in r.decisions
                     if d.kind == "complete" and not d.adopted]
        self.assertEqual(len(discarded), 1)
        self.assertIn("不得回滚覆盖", discarded[0].basis)

    def test_invalid_write_after_valid_generation_does_not_overwrite(self):
        sectors = make_transaction(1, 1, "SLOT_A", P1)
        # newer prepare/page but a corrupted complete
        sectors += [
            make_prepare(2, 2, "SLOT_A", P2, 3),
            make_page(2, "SLOT_A", P2, 4),
            corrupt_byte(make_complete(2, 2, "SLOT_A", P2, 5), 61),
        ]
        r = run(sectors)
        self.assertEqual(r.slots["SLOT_A"].generation, 1)
        self.assertEqual(r.slots["SLOT_A"].payload, P1)
        self.assertEqual(r.boot_digest, f"{zlib.crc32(P1) & 0xFFFFFFFF:08x}")

    def test_max_32_sectors_enforced(self):
        many = [b64(make_page(1, "SLOT_A", P1, i)) for i in range(MAX_SECTORS + 2)]
        r = judge_recovery("A1", "SLOT_A", many)
        self.assertIsNotNone(r.first_violation)
        self.assertLessEqual(len(r.decisions), MAX_SECTORS)


class StoreTests(unittest.TestCase):
    def test_write_once_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            store = FrozenStore(os.path.join(d, "audits.json"))
            store.put_if_absent("X", {"boot": {"slot": "SLOT_A"}})
            with self.assertRaises(AuditExistsError):
                store.put_if_absent("X", {"boot": {"slot": "SLOT_B"}})
            self.assertEqual(store.get("X")["boot"]["slot"], "SLOT_A")


if __name__ == "__main__":
    unittest.main(verbosity=2)
