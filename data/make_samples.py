"""Generate three bundled sample sector images (newline-delimited base64).

Run: python -m data.make_samples   (writes data/*.txt)

Samples:
  01_clean_switch.txt        two full transactions, newer generation boots
  02_corrupt_complete.txt    newer switch loses power mid-complete (bad CRC32);
                             the half-written newer slot must NOT be reported
                             bootable; last valid generation survives
  03_older_invalid_kept.txt  valid gen-3 switch followed by an invalid older
                             write that must not overwrite it
  04_reorder_target.txt      byte-clean complete transaction whose physical
                             write order (page, prepare, complete) keeps it
                             from being adopted; the correction endpoint must
                             produce the replayable minimum [prepare,page,
                             complete] with exactly one adjacent swap
"""

from __future__ import annotations

import os

from app.builders import (
    b64_many,
    corrupt_byte,
    make_complete,
    make_page,
    make_prepare,
    make_transaction,
)

P1 = bytes.fromhex("01") * 32
P2 = bytes.fromhex("02") * 32
P3 = bytes.fromhex("03") * 32
P_OTHER = bytes.fromhex("AB") * 32


def write(name: str, sectors: list[bytes]) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, name)
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(b64_many(sectors)) + "\n")
    print(f"wrote {path} ({len(sectors)} sectors)")


def main() -> None:
    # 1) clean: SLOT_A gen1 complete, SLOT_B gen2 complete -> boots SLOT_B g2
    clean = make_transaction(100, 1, "SLOT_A", P1, 0)
    clean += make_transaction(200, 2, "SLOT_B", P2, 3)
    write("01_clean_switch.txt", clean)

    # 2) power cut during the newer complete record: its sector CRC is broken.
    #    Valid SLOT_A gen1 must remain the bootable generation; the
    #    half-written SLOT_B gen2 must never be reported bootable.
    corrupt = make_transaction(100, 1, "SLOT_A", P1, 0)
    corrupt.append(make_prepare(200, 2, "SLOT_B", P2, 3))
    corrupt.append(make_page(2, "SLOT_B", P2, 4))
    broken_complete = corrupt_byte(make_complete(200, 2, "SLOT_B", P2, 5), 40)
    corrupt.append(broken_complete)
    write("02_corrupt_complete.txt", corrupt)

    # 3) valid gen3, then an older gen2 complete on the same slot whose
    #    prepare/page pair exists but the older generation must not roll back.
    kept = make_transaction(300, 3, "SLOT_A", P3, 0)
    kept.append(make_prepare(301, 2, "SLOT_A", P_OTHER, 3))
    kept.append(make_page(2, "SLOT_A", P_OTHER, 4))
    kept.append(make_complete(301, 2, "SLOT_A", P_OTHER, 5))
    write("03_older_invalid_kept.txt", kept)

    # 4) every sector is byte-clean, but the target transaction 400 (SLOT_B
    #    gen2) was physically written page -> prepare -> complete, so the page
    #    precedes its prepare and the complete then finds no adopted page; the
    #    target is never adopted. The correction endpoint reorders to
    #    prepare -> page -> complete with a single adjacent swap.
    reorder = [
        make_page(2, "SLOT_B", P2, 0),
        make_prepare(400, 2, "SLOT_B", P2, 1),
        make_complete(400, 2, "SLOT_B", P2, 2),
    ]
    write("04_reorder_target.txt", reorder)


if __name__ == "__main__":
    main()
