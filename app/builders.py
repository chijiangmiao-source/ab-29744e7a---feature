"""Sector builders used by tests and the bundled sample images.

These produce the exact 64-byte wire format parsed by app.parser, plus helpers
to deliberately truncate / corrupt sectors so the judge's rejection paths can
be exercised.
"""

from __future__ import annotations

import base64
import zlib

from .parser import (
    FORMAT_VERSION,
    MAGIC,
    PAYLOAD_LEN,
    RAW_SIZE,
    SLOT_NAME_LEN,
    TYPE_COMPLETE,
    TYPE_PAGE,
    TYPE_PREPARE,
    _header_crc,
)


def _slot_raw(name: str) -> bytes:
    data = name.encode("ascii")
    if not 1 <= len(data) <= SLOT_NAME_LEN:
        raise ValueError("slot name must be 1..8 ascii chars")
    return data.ljust(SLOT_NAME_LEN, b"\x00")


def _header(stype: int) -> bytearray:
    head = bytearray(7)
    head[0:4] = MAGIC
    head[4] = FORMAT_VERSION
    head[5] = stype
    head[6] = 0
    out = bytearray(RAW_SIZE)
    out[0:7] = head
    out[7] = _header_crc(bytes(head))
    return out


def _finish(buf: bytearray) -> bytes:
    buf[60:64] = (zlib.crc32(bytes(buf[0:60])) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(buf)


def make_page(generation: int, slot: str, payload: bytes, seq: int,
              *, payload_crc: int | None = None) -> bytes:
    if len(payload) != PAYLOAD_LEN:
        raise ValueError(f"payload must be {PAYLOAD_LEN} bytes")
    buf = _header(TYPE_PAGE)
    buf[8:12] = (generation & 0xFFFFFFFF).to_bytes(4, "big")
    buf[12:20] = _slot_raw(slot)
    buf[20:52] = payload
    digest = zlib.crc32(payload) & 0xFFFFFFFF
    buf[52:56] = ((digest if payload_crc is None else payload_crc)
                 & 0xFFFFFFFF).to_bytes(4, "big")
    buf[56:60] = (seq & 0xFFFFFFFF).to_bytes(4, "big")
    return _finish(buf)


def make_prepare(transaction_id: int, generation: int, slot: str,
                 payload: bytes, seq: int) -> bytes:
    digest = zlib.crc32(payload) & 0xFFFFFFFF
    buf = _header(TYPE_PREPARE)
    buf[8:12] = (transaction_id & 0xFFFFFFFF).to_bytes(4, "big")
    buf[12:16] = (generation & 0xFFFFFFFF).to_bytes(4, "big")
    buf[16:24] = _slot_raw(slot)
    buf[24:28] = digest.to_bytes(4, "big")
    buf[56:60] = (seq & 0xFFFFFFFF).to_bytes(4, "big")
    return _finish(buf)


def make_complete(transaction_id: int, generation: int, slot: str,
                  payload: bytes, seq: int, *,
                  digest_override: int | None = None) -> bytes:
    digest = zlib.crc32(payload) & 0xFFFFFFFF
    if digest_override is not None:
        digest = digest_override & 0xFFFFFFFF
    buf = _header(TYPE_COMPLETE)
    buf[8:12] = (transaction_id & 0xFFFFFFFF).to_bytes(4, "big")
    buf[12:16] = (generation & 0xFFFFFFFF).to_bytes(4, "big")
    buf[16:24] = _slot_raw(slot)
    buf[24:28] = digest.to_bytes(4, "big")
    buf[56:60] = (seq & 0xFFFFFFFF).to_bytes(4, "big")
    return _finish(buf)


def make_transaction(transaction_id: int, generation: int, slot: str,
                     payload: bytes, seq_start: int = 0) -> list[bytes]:
    """The canonical good write sequence: prepare, page, complete."""
    return [
        make_prepare(transaction_id, generation, slot, payload, seq_start),
        make_page(generation, slot, payload, seq_start + 1),
        make_complete(transaction_id, generation, slot, payload, seq_start + 2),
    ]


def b64(sector: bytes) -> str:
    return base64.b64encode(sector).decode("ascii")


def b64_many(sectors: list[bytes]) -> list[str]:
    return [b64(s) for s in sectors]


def truncate(sector: bytes, keep: int) -> bytes:
    """Simulate a half-written sector (kept raw; caller re-encodes as needed)."""
    return sector[:keep]


def corrupt_byte(sector: bytes, offset: int) -> bytes:
    buf = bytearray(sector)
    buf[offset] ^= 0xFF
    return bytes(buf)
