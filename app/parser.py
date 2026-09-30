"""
Byte-level parser / recovery judge for satellite controller boot-slot sector images.

Sector wire layout (every sector is exactly RAW_SIZE bytes; the UI transmits them
base64-encoded, one sector per list entry, in PHYSICAL write order):

  common header (8 bytes)
    offset 0  magic[4]          = b"SCTR"
    offset 4  version           = uint8  (must be FORMAT_VERSION)
    offset 5  sector_type       = uint8  (1=slot page, 2=prepare, 3=complete)
    offset 6  reserved          = uint8  (must be zero)
    offset 7  header_crc        = uint8  (checksum over bytes [0:7])

  slot page (TYPE_PAGE = 1), 64 bytes:
    8  generation      uint32 BE
    12 slot_name       8 ASCII bytes
    20 payload         32 bytes
    52 payload_crc     uint32 BE   (zlib.crc32 over the 32 payload bytes)
    56 seq             uint32 BE   (physical sequence assigned at write time)
    60 sector_crc      uint32 BE   (zlib.crc32 over bytes [0:60])

  prepare record (TYPE_PREPARE = 2), 64 bytes:
    8  transaction_id  uint32 BE
    12 generation      uint32 BE
    16 slot_name       8 ASCII bytes
    24 payload_digest  uint32 BE   (crc32 of the payload the page must carry)
    28 zero_padding    28 bytes   (must all be zero)
    56 seq             uint32 BE
    60 sector_crc      uint32 BE

  complete record (TYPE_COMPLETE = 3), 64 bytes:
    8  transaction_id  uint32 BE
    12 generation      uint32 BE
    16 slot_name       8 ASCII bytes
    24 payload_digest  uint32 BE
    28 zero_padding    28 bytes
    56 seq             uint32 BE
    60 sector_crc      uint32 BE

The judge NEVER decides from "latest page number" alone, never skips damaged
records, and never trusts a complete marker without the matching prepared
transaction, complete target page and matching id/generation/slot/digest.
"""

from __future__ import annotations

import base64
import binascii
import zlib
from dataclasses import dataclass, field
from typing import Optional

MAGIC = b"SCTR"
FORMAT_VERSION = 1
RAW_SIZE = 64
B64_SIZE = 88  # ceil(64/3)*4
MAX_SECTORS = 32
SLOT_NAME_LEN = 8
PAYLOAD_LEN = 32

TYPE_PAGE = 1
TYPE_PREPARE = 2
TYPE_COMPLETE = 3
TYPE_NAMES = {TYPE_PAGE: "slot_page", TYPE_PREPARE: "prepare", TYPE_COMPLETE: "complete"}

# Violation codes -----------------------------------------------------------
V_BASE64 = "bad_base64"
V_LENGTH = "bad_length"
V_MAGIC = "bad_magic"
V_VERSION = "bad_version"
V_TYPE = "bad_type"
V_RESERVED = "bad_reserved"
V_HEADER_CRC = "bad_header_crc"
V_SECTOR_CRC = "bad_sector_crc"
V_PAYLOAD_CRC = "bad_payload_crc"
V_SLOT_NAME = "bad_slot_name"
V_PADDING = "bad_padding"
V_DUP_TX_INCONSISTENT = "duplicate_tx_inconsistent"
V_COMPLETE_NO_PREPARE = "complete_without_prepare"
V_PREPARE_AFTER_COMPLETE = "prepare_after_complete"
V_PAGE_BEFORE_PREPARE = "page_before_prepare"
V_PAGE_TX_MISMATCH = "page_tx_mismatch"
V_COMPLETE_TX_MISMATCH = "complete_tx_mismatch"


class SectorParseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Sector:
    index: int
    raw: bytes
    stype: int
    seq: int
    generation: Optional[int] = None
    slot: Optional[str] = None
    transaction_id: Optional[int] = None
    payload_digest: Optional[int] = None
    payload: Optional[bytes] = None
    payload_crc: Optional[int] = None

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.stype, f"type_{self.stype}")


@dataclass
class RecordDecision:
    """Why one physically-written sector was adopted or discarded."""

    index: int
    kind: str  # "slot_page" | "prepare" | "complete"
    seq: int
    adopted: bool
    basis: str
    generation: Optional[int] = None
    slot: Optional[str] = None
    transaction_id: Optional[int] = None
    violation: Optional[str] = None


@dataclass
class SlotState:
    generation: int
    slot: str
    payload: bytes
    digest: int
    transaction_id: int
    page_index: int
    complete_index: int


@dataclass
class RecoveryResult:
    audit_id: str
    active_slot: str
    total_sectors: int
    first_violation: Optional[dict] = None
    decisions: list[RecordDecision] = field(default_factory=list)
    slots: dict[str, SlotState] = field(default_factory=dict)
    boot_slot: Optional[str] = None
    boot_generation: Optional[int] = None
    boot_digest: Optional[str] = None
    boot_payload_hex: Optional[str] = None
    boot_reason: str = ""
    frozen: bool = False

    def to_public_dict(self) -> dict:
        return {
            "audit_id": self.audit_id,
            "active_slot": self.active_slot,
            "total_sectors": self.total_sectors,
            "frozen": self.frozen,
            "first_violation": self.first_violation,
            "decisions": [d.__dict__ for d in self.decisions],
            "slots": {
                name: {
                    "slot": s.slot,
                    "generation": s.generation,
                    "digest": f"{s.digest:08x}",
                    "payload_hex": s.payload.hex(),
                    "transaction_id": s.transaction_id,
                    "page_index": s.page_index,
                    "complete_index": s.complete_index,
                }
                for name, s in sorted(self.slots.items())
            },
            "boot": None
            if self.boot_slot is None
            else {
                "slot": self.boot_slot,
                "generation": self.boot_generation,
                "digest": self.boot_digest,
                "payload_hex": self.boot_payload_hex,
                "reason": self.boot_reason,
            },
        }


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _header_crc(head7: bytes) -> int:
    return sum(head7) & 0xFF


def _valid_slot_name(raw: bytes) -> bool:
    # 1..8 chars, ASCII uppercase / digit / underscore, NUL-padded to the right;
    # embedded NULs are illegal.
    stripped = raw.rstrip(b"\x00")
    if not stripped or b"\x00" in stripped:
        return False
    return all(65 <= b <= 90 or 48 <= b <= 57 or b == 95 for b in stripped)


def decode_sector_b64(text: str) -> bytes:
    """Decode one base64 sector; reject non-padded / whitespace-padded junk."""
    if not isinstance(text, str) or len(text) != B64_SIZE:
        raise SectorParseError(V_LENGTH, f"base64 扇区长度必须为 {B64_SIZE} 字符")
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise SectorParseError(V_BASE64, "非法 Base64 编码")
    if len(raw) != RAW_SIZE:
        raise SectorParseError(V_LENGTH, f"解码后扇区必须为 {RAW_SIZE} 字节，实际 {len(raw)} 字节")
    return raw


def parse_sector(raw: bytes, index: int) -> Sector:
    """Parse a single raw sector byte by byte, validating every fixed field."""
    if len(raw) != RAW_SIZE:
        raise SectorParseError(V_LENGTH, f"扇区 #{index} 长度必须为 {RAW_SIZE} 字节")

    if raw[0:4] != MAGIC:
        raise SectorParseError(V_MAGIC, f"扇区 #{index} 魔数错误: {raw[0:4]!r}")
    if raw[4] != FORMAT_VERSION:
        raise SectorParseError(V_VERSION, f"扇区 #{index} 版本字段错误: {raw[4]}")
    stype = raw[5]
    if stype not in TYPE_NAMES:
        raise SectorParseError(V_TYPE, f"扇区 #{index} 类型字段非法: {stype}")
    if raw[6] != 0:
        raise SectorParseError(V_RESERVED, f"扇区 #{index} 保留字节非零")
    if raw[7] != _header_crc(raw[0:7]):
        raise SectorParseError(V_HEADER_CRC, f"扇区 #{index} 头部校验字节错误")

    stored_crc = int.from_bytes(raw[60:64], "big")
    calc_crc = zlib.crc32(raw[0:60]) & 0xFFFFFFFF
    if stored_crc != calc_crc:
        raise SectorParseError(
            V_SECTOR_CRC,
            f"扇区 #{index} 整扇区 CRC32 不匹配: 存储 {stored_crc:08x} 计算 {calc_crc:08x}",
        )

    seq = int.from_bytes(raw[56:60], "big")

    if stype == TYPE_PAGE:
        generation = int.from_bytes(raw[8:12], "big")
        slot_raw = raw[12:20]
        if not _valid_slot_name(slot_raw):
            raise SectorParseError(V_SLOT_NAME, f"扇区 #{index} 槽名字段非法")
        payload = raw[20:52]
        stored_pcrc = int.from_bytes(raw[52:56], "big")
        calc_pcrc = zlib.crc32(payload) & 0xFFFFFFFF
        if stored_pcrc != calc_pcrc:
            raise SectorParseError(
                V_PAYLOAD_CRC,
                f"扇区 #{index} 载荷 CRC32 不匹配: 存储 {stored_pcrc:08x} 计算 {calc_pcrc:08x}",
            )
        return Sector(
            index=index, raw=raw, stype=stype, seq=seq,
            generation=generation, slot=slot_raw.rstrip(b"\x00").decode("ascii"),
            payload=payload, payload_crc=stored_pcrc,
        )

    # prepare / complete share a record layout
    tx_id = int.from_bytes(raw[8:12], "big")
    generation = int.from_bytes(raw[12:16], "big")
    slot_raw = raw[16:24]
    if not _valid_slot_name(slot_raw):
        raise SectorParseError(V_SLOT_NAME, f"扇区 #{index} 槽名字段非法")
    digest = int.from_bytes(raw[24:28], "big")
    if raw[28:56] != b"\x00" * 28:
        raise SectorParseError(V_PADDING, f"扇区 #{index} 保留填充区非零")
    return Sector(
        index=index, raw=raw, stype=stype, seq=seq,
        generation=generation, slot=slot_raw.rstrip(b"\x00").decode("ascii"),
        transaction_id=tx_id, payload_digest=digest,
    )


# --------------------------------------------------------------------------
# Recovery judge
# --------------------------------------------------------------------------

def _violation(index: int, code: str, message: str, sector: Optional[Sector] = None) -> dict:
    out = {"index": index, "code": code, "message": message}
    if sector is not None:
        out["sector_type"] = sector.type_name
        out["seq"] = sector.seq
    return out


def judge_recovery(
    audit_id: str,
    active_slot: str,
    sectors_b64: list[str],
    frozen: bool = False,
) -> RecoveryResult:
    """Walk sectors in physical order and produce the frozen recovery conclusion.

    Returns a RecoveryResult. The first violation encountered (structural or
    transactional) is recorded; later invalid writes can never overwrite an
    already-adopted generation.
    """
    result = RecoveryResult(audit_id=audit_id, active_slot=active_slot,
                            total_sectors=len(sectors_b64), frozen=frozen)

    prepares: dict[int, Sector] = {}        # transaction id -> prepare sector
    completed_tx: set[int] = set()
    pending_pages: dict[int, list[Sector]] = {}  # tx id -> accepted page list
    first_violation: Optional[dict] = None

    def note_first(v: dict) -> None:
        nonlocal first_violation
        if first_violation is None:
            first_violation = v

    if len(sectors_b64) > MAX_SECTORS:
        note_first(_violation(MAX_SECTORS, V_LENGTH,
                              f"扇区数量超过上限 {MAX_SECTORS}"))

    for index, text in enumerate(sectors_b64[:MAX_SECTORS]):
        try:
            raw = decode_sector_b64(text)
        except SectorParseError as e:
            note_first(_violation(index, e.code, e.message))
            result.decisions.append(RecordDecision(
                index=index, kind="unreadable", seq=-1, adopted=False,
                basis=e.message, violation=e.code))
            continue

        try:
            sec = parse_sector(raw, index)
        except SectorParseError as e:
            note_first(_violation(index, e.code, e.message))
            result.decisions.append(RecordDecision(
                index=index, kind="corrupt", seq=-1, adopted=False,
                basis=e.message, violation=e.code))
            continue

        if sec.stype == TYPE_PREPARE:
            tx = sec.transaction_id
            if tx in completed_tx:
                msg = f"扇区 #{index} 事务 {tx} 已完成后再次出现准备记录"
                note_first(_violation(index, V_PREPARE_AFTER_COMPLETE, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="prepare", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    transaction_id=tx, violation=V_PREPARE_AFTER_COMPLETE))
                continue
            if tx in prepares:
                old = prepares[tx]
                consistent = (
                    old.generation == sec.generation
                    and old.slot == sec.slot
                    and old.payload_digest == sec.payload_digest
                )
                if not consistent:
                    msg = (f"扇区 #{index} 事务 {tx} 的准备记录与扇区 #{old.index} "
                           f"内容不一致（代次/槽名/摘要冲突）")
                    note_first(_violation(index, V_DUP_TX_INCONSISTENT, msg, sec))
                    result.decisions.append(RecordDecision(
                        index=index, kind="prepare", seq=sec.seq, adopted=False,
                        basis=msg, generation=sec.generation, slot=sec.slot,
                        transaction_id=tx, violation=V_DUP_TX_INCONSISTENT))
                    continue
                result.decisions.append(RecordDecision(
                    index=index, kind="prepare", seq=sec.seq, adopted=False,
                    basis=(f"扇区 #{index} 与扇区 #{old.index} 为同一事务 {tx} 的"
                           f"重复准备记录，内容一致，仅采纳首次出现者"),
                    generation=sec.generation, slot=sec.slot, transaction_id=tx))
                continue
            prepares[tx] = sec
            result.decisions.append(RecordDecision(
                index=index, kind="prepare", seq=sec.seq, adopted=True,
                basis=(f"扇区 #{index} 开启事务 {tx}：目标槽 {sec.slot}，"
                       f"代次 {sec.generation}，载荷摘要 {sec.payload_digest:08x}"),
                generation=sec.generation, slot=sec.slot, transaction_id=tx))
            continue

        if sec.stype == TYPE_PAGE:
            # A page is only meaningful inside an open transaction and must
            # arrive after that transaction's prepare in physical order.
            # Pages embed no transaction id; attribution is positional:
            # the page belongs to the most recent open prepare whose
            # (generation, slot) it matches and which has not completed.
            owner = _attribute_page(sec, prepares, completed_tx)
            if owner is None:
                msg = (f"扇区 #{index} 槽页（槽 {sec.slot}，代次 {sec.generation}）"
                       f"之前没有匹配的未完成准备记录")
                note_first(_violation(index, V_PAGE_BEFORE_PREPARE, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="slot_page", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    violation=V_PAGE_BEFORE_PREPARE))
                continue

            tx = owner.transaction_id
            digest = zlib.crc32(sec.payload) & 0xFFFFFFFF
            if digest != owner.payload_digest:
                msg = (f"扇区 #{index} 槽页载荷摘要 {digest:08x} 与事务 {tx} "
                       f"准备记录要求的 {owner.payload_digest:08x} 不符")
                note_first(_violation(index, V_PAGE_TX_MISMATCH, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="slot_page", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    transaction_id=tx, violation=V_PAGE_TX_MISMATCH))
                continue

            pending_pages.setdefault(tx, []).append(sec)
            result.decisions.append(RecordDecision(
                index=index, kind="slot_page", seq=sec.seq, adopted=True,
                basis=(f"扇区 #{index} 槽页归属事务 {tx}：槽 {sec.slot}，"
                       f"代次 {sec.generation}，载荷摘要 {digest:08x} 相符，"
                       f"等待完成记录"),
                generation=sec.generation, slot=sec.slot, transaction_id=tx))
            continue

        if sec.stype == TYPE_COMPLETE:
            tx = sec.transaction_id
            prep = prepares.get(tx)
            if prep is None or tx in completed_tx:
                if tx in completed_tx:
                    msg = f"扇区 #{index} 完成记录指向已完成事务 {tx}（重复完成）"
                else:
                    msg = f"扇区 #{index} 完成记录指向不存在的事务 {tx}（悬空完成记录）"
                note_first(_violation(index, V_COMPLETE_NO_PREPARE, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="complete", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    transaction_id=tx, violation=V_COMPLETE_NO_PREPARE))
                continue

            mismatch_bits = []
            if prep.generation != sec.generation:
                mismatch_bits.append(f"代次 准备={prep.generation} 完成={sec.generation}")
            if prep.slot != sec.slot:
                mismatch_bits.append(f"槽名 准备={prep.slot} 完成={sec.slot}")
            if prep.payload_digest != sec.payload_digest:
                mismatch_bits.append(
                    f"摘要 准备={prep.payload_digest:08x} 完成={sec.payload_digest:08x}")
            if mismatch_bits:
                msg = f"扇区 #{index} 完成记录与事务 {tx} 准备记录字段不符：" + "；".join(mismatch_bits)
                note_first(_violation(index, V_COMPLETE_TX_MISMATCH, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="complete", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    transaction_id=tx, violation=V_COMPLETE_TX_MISMATCH))
                continue

            pages = pending_pages.get(tx, [])
            if not pages:
                msg = (f"扇区 #{index} 完成记录指向事务 {tx}，但准备与完成之间"
                       f"没有完整目标槽页（指向未完成页）")
                note_first(_violation(index, V_COMPLETE_NO_PREPARE, msg, sec))
                result.decisions.append(RecordDecision(
                    index=index, kind="complete", seq=sec.seq, adopted=False,
                    basis=msg, generation=sec.generation, slot=sec.slot,
                    transaction_id=tx, violation=V_COMPLETE_NO_PREPARE))
                continue

            page = pages[-1]  # last physically written valid page of the tx
            digest = zlib.crc32(page.payload) & 0xFFFFFFFF
            completed_tx.add(tx)

            existing = result.slots.get(sec.slot)
            if existing is not None and existing.generation > sec.generation:
                # Invalid/older write must never overwrite a newer valid gen.
                result.decisions.append(RecordDecision(
                    index=index, kind="complete", seq=sec.seq, adopted=False,
                    basis=(f"扇区 #{index} 事务 {tx} 虽三段齐备，但代次 "
                           f"{sec.generation} 旧于槽 {sec.slot} 已生效代次 "
                           f"{existing.generation}，不得回滚覆盖"),
                    generation=sec.generation, slot=sec.slot,
                    transaction_id=tx))
                continue

            result.slots[sec.slot] = SlotState(
                generation=sec.generation, slot=sec.slot, payload=page.payload,
                digest=digest, transaction_id=tx,
                page_index=page.index, complete_index=index)
            result.decisions.append(RecordDecision(
                index=index, kind="complete", seq=sec.seq, adopted=True,
                basis=(f"扇区 #{index} 完成事务 {tx}：准备（扇区 #{prep.index}）"
                       f"→槽页（扇区 #{page.index}）→完成三段齐备且顺序正确，"
                       f"槽 {sec.slot} 切换至代次 {sec.generation}，摘要 {digest:08x}"),
                generation=sec.generation, slot=sec.slot, transaction_id=tx))
            continue

    result.first_violation = first_violation
    _select_boot_slot(result)
    return result


def _attribute_page(
    page: Sector,
    prepares: dict[int, Sector],
    completed_tx: set[int],
) -> Optional[Sector]:
    """Find the open transaction a physically-written page belongs to.

    Rule: walk prepares in physical (index) order; the owner is the last
    prepare positioned before the page, still open, whose generation and
    slot name match the page.
    """
    owner: Optional[Sector] = None
    for prep in prepares.values():
        if prep.index >= page.index:
            continue
        if prep.transaction_id in completed_tx:
            continue
        if prep.generation == page.generation and prep.slot == page.slot:
            if owner is None or prep.index > owner.index:
                owner = prep
    return owner


def _select_boot_slot(result: RecoveryResult) -> None:
    """Pick the boot configuration from completed, validated slots only.

    Highest generation wins; ties are broken by physical slot name with a
    stable, total ordering (lexicographic). The originally active slot never
    overrides generation ordering.
    """
    if not result.slots:
        result.boot_reason = "不存在任何三段齐备的有效事务，保持初始活动槽（无可启动新代次）"
        return
    best: Optional[SlotState] = None
    for state in result.slots.values():
        if best is None:
            best = state
            continue
        if state.generation > best.generation or (
            state.generation == best.generation and state.slot < best.slot
        ):
            best = state
    assert best is not None
    same_gen = sorted(s.slot for s in result.slots.values()
                      if s.generation == best.generation)
    tie_note = ""
    if len(same_gen) > 1:
        tie_note = (f"；代次 {best.generation} 并列槽 {same_gen}，"
                    f"按物理槽名稳定选择 {best.slot}")
    result.boot_slot = best.slot
    result.boot_generation = best.generation
    result.boot_digest = f"{best.digest:08x}"
    result.boot_payload_hex = best.payload.hex()
    result.boot_reason = (
        f"槽 {best.slot} 持有最高有效代次 {best.generation}"
        f"（完成于扇区 #{best.complete_index}，载荷摘要 {best.digest:08x}）{tie_note}"
    )
