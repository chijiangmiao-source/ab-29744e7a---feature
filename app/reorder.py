"""Minimal adjacent-transposition replay correction.

Given a *frozen* audit whose sectors all pass the existing byte-level checks but
whose physical write order kept the target transaction from being adopted, find
the permutation of the very same sector bytes (no byte is ever modified) that:

  1. adopts the target transaction (prepare -> digest-matching complete page
     -> field-consistent complete, in that physical order),
  2. boots the target slot/generation in the final arrangement, and
  3. never presents the still-unfinished target page as bootable in a prefix.

The search is EXHAUSTIVE over every permutation of the sectors (n <= 12). It is
NOT a sort by record type, NOT a greedy move and NOT a final-order-only check:

  * dynamic programming over (written-sector mask, per-transaction replay
    phase) explores every legal append in every order;
  * step cost is the exact adjacent-transposition distance contribution
    (inversions against the original physical order);
  * ties are broken on the full sequence of ORIGINAL indices (stable total
    order);
  * the winning arrangement is then replayed PREFIX BY PREFIX through the
    unchanged ``judge_recovery`` -- exactly the existing recovery semantics --
    to authoritatively adjudicate every prefix and the final boot, and to
    collect prepare / slot-page / completion evidence.

The only source images refused outright are those the existing semantics cannot
adjudicate unambiguously (byte corruption, an inconsistent duplicate prepare,
or two transactions whose prepares claim the same generation+slot so page
ownership would be ambiguous) and images that simply do not contain a complete,
field-consistent target transaction.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field
from typing import Optional

from .parser import (
    TYPE_COMPLETE,
    TYPE_PAGE,
    TYPE_PREPARE,
    Sector,
    SectorParseError,
    decode_sector_b64,
    judge_recovery,
    parse_sector,
)

MAX_CORRECTION_SECTORS = 12


class ReorderError(Exception):
    """A correction request that must be refused. ``status`` is the HTTP code."""

    def __init__(self, code: str, message: str, status: int = 400,
                 detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


@dataclass
class PrefixVerdict:
    length: int
    physical_sequence: list[int]           # original indices in this prefix
    boot_slot: Optional[str]
    boot_generation: Optional[int]
    target_complete_included: bool
    target_bootable: bool                   # unfinished target page shown?
    first_violation: Optional[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "length": self.length,
            "physical_sequence": list(self.physical_sequence),
            "boot_slot": self.boot_slot,
            "boot_generation": self.boot_generation,
            "target_complete_included": self.target_complete_included,
            "target_bootable": self.target_bootable,
            "first_violation": self.first_violation,
            "reason": self.reason,
        }


@dataclass
class CorrectionPlan:
    correction_id: str
    source_audit_id: str
    active_slot: str
    target_transaction: int
    total_sectors: int
    source_hash: str
    source_sectors: list[str]               # frozen source bytes (base64)
    source_verdict: dict                    # original recovery conclusion summary
    target_slot: str
    target_generation: int
    target_digest: str
    target_payload_hex: str
    prepare_index: int                      # frozen physical indices
    page_index: int
    complete_index: int
    physical_sequence: list[int]            # original indices in final order
    swap_count: int
    swap_steps: list[list[int]]             # adjacent position pairs, in order
    prefixes: list[PrefixVerdict] = field(default_factory=list)

    def to_public_dict(self) -> dict:
        return {
            "kind": "reorder_correction",
            "frozen": True,
            "correction_id": self.correction_id,
            "source_audit_id": self.source_audit_id,
            "active_slot": self.active_slot,
            "target_transaction": self.target_transaction,
            "total_sectors": self.total_sectors,
            "source_hash": self.source_hash,
            "source_sectors": list(self.source_sectors),
            "source_verdict": dict(self.source_verdict),
            "swap_count": self.swap_count,
            "swap_steps": [list(s) for s in self.swap_steps],
            "physical_sequence": list(self.physical_sequence),
            "prefixes": [p.to_dict() for p in self.prefixes],
            "target": {
                "transaction_id": self.target_transaction,
                "slot": self.target_slot,
                "generation": self.target_generation,
                "digest": self.target_digest,
                "payload_hex": self.target_payload_hex,
                "prepare_index": self.prepare_index,
                "page_index": self.page_index,
                "complete_index": self.complete_index,
            },
        }


# --------------------------------------------------------------------------
# Source analysis
# --------------------------------------------------------------------------

# Per-sector effect on the replay state machine -----------------------------
#   ("prep", tx)                         prepare record for local tx index
#   ("page", tx | None, matches: bool)   slot page (owner key unique-or-None)
#   ("comp", tx | None, consistent:bool) complete record
@dataclass
class _Effect:
    kind: str
    tx: Optional[int]
    ok: bool


@dataclass
class _Source:
    sectors: list[Sector]
    effects: list[_Effect]
    tx_ids: list[int]                       # local tx index -> transaction id
    slots: list[str]
    generations: list[int]
    prep_mask: list[int]                    # local tx -> bitmask of prepares
    page_mask: list[int]                    # local tx -> bitmask of matching pages
    comp_mask: list[int]                    # local tx -> bitmask of consistent completes


def _conflict(message: str) -> ReorderError:
    return ReorderError("conflicting_transactions", message, 400)


def analyze_source(sectors_b64: list[str]) -> _Source:
    """Parse every sector and map it to a replay effect.

    Refuses byte-level damage (reordering cannot repair bytes) and the two
    ambiguities the recovery judge itself cannot resolve position-independent:
    an inconsistent duplicate prepare for one transaction, and two different
    transactions preparing the same (generation, slot) so page ownership would
    depend on something other than the bytes.
    """
    if not sectors_b64:
        raise ReorderError("empty_source", "来源镜像为空", 400)
    if len(sectors_b64) > MAX_CORRECTION_SECTORS:
        raise ReorderError(
            "source_too_large",
            f"纠正仅接受至多 {MAX_CORRECTION_SECTORS} 个扇区的镜像，"
            f"实际 {len(sectors_b64)} 个", 400)

    sectors: list[Sector] = []
    for index, text in enumerate(sectors_b64):
        try:
            raw = decode_sector_b64(text)
            sec = parse_sector(raw, index)
        except SectorParseError as exc:
            raise ReorderError(
                "source_corrupt",
                f"来源扇区 #{index} 未通过既有字节级校验（{exc.message}），"
                f"重排不能修复字节损坏，拒绝纠正",
                400,
                detail={"index": index, "code": exc.code, "message": exc.message},
            ) from exc
        sectors.append(sec)

    # First pass: index prepares, rejecting the two structural conflicts.
    prepare_by_tx: dict[int, list[Sector]] = {}
    owner_by_key: dict[tuple[int, str], int] = {}
    for sec in sectors:
        if sec.stype != TYPE_PREPARE:
            continue
        tx = sec.transaction_id
        if tx in prepare_by_tx:
            old = prepare_by_tx[tx][0]
            if (old.generation != sec.generation or old.slot != sec.slot
                    or old.payload_digest != sec.payload_digest):
                raise _conflict(
                    f"事务 {tx} 存在内容不一致的重复准备记录"
                    f"（扇区 #{old.index} 与 #{sec.index}），重排裁决不唯一")
        else:
            key = (sec.generation, sec.slot)
            if key in owner_by_key:
                raise _conflict(
                    f"扇区 #{sec.index} 的准备记录与事务 {owner_by_key[key]} "
                    f"占用相同（代次 {sec.generation}，槽 {sec.slot}），"
                    f"槽页归属存在歧义")
            owner_by_key[key] = tx
        prepare_by_tx.setdefault(tx, []).append(sec)

    tx_ids = sorted(prepare_by_tx)
    tx_local = {tx: i for i, tx in enumerate(tx_ids)}
    t = len(tx_ids)
    slots = [prepare_by_tx[tx][0].slot for tx in tx_ids]
    generations = [prepare_by_tx[tx][0].generation for tx in tx_ids]
    prep_digest = [prepare_by_tx[tx][0].payload_digest for tx in tx_ids]
    key_to_tx = {(prepare_by_tx[tx][0].generation,
                  prepare_by_tx[tx][0].slot): i for i, tx in enumerate(tx_ids)}

    effects: list[Optional[_Effect]] = [None] * len(sectors)
    prep_mask = [0] * t
    page_mask = [0] * t
    comp_mask = [0] * t

    for sec in sectors:
        bit = 1 << sec.index
        if sec.stype == TYPE_PREPARE:
            i = tx_local[sec.transaction_id]
            effects[sec.index] = _Effect("prep", i, True)
            prep_mask[i] |= bit
        elif sec.stype == TYPE_PAGE:
            i = key_to_tx.get((sec.generation, sec.slot))
            digest = zlib.crc32(sec.payload) & 0xFFFFFFFF
            matches = i is not None and digest == prep_digest[i]
            if matches:
                page_mask[i] |= bit
            effects[sec.index] = _Effect("page", i, bool(matches))
        else:  # TYPE_COMPLETE
            i = tx_local.get(sec.transaction_id)
            consistent = False
            if i is not None:
                prep0 = prepare_by_tx[tx_ids[i]][0]
                consistent = (prep0.generation == sec.generation
                              and prep0.slot == sec.slot
                              and prep0.payload_digest == sec.payload_digest)
            if consistent:
                comp_mask[i] |= bit
            # A complete for an unknown tx, or one with mismatching fields,
            # never adopts anything under the existing semantics: it is a free
            # sector whose placement can only produce the known violations.
            effects[sec.index] = _Effect("comp", i, consistent)

    assert all(e is not None for e in effects)
    return _Source(sectors=sectors, effects=[e for e in effects if e is not None],
                   tx_ids=tx_ids, slots=slots, generations=generations,
                   prep_mask=prep_mask, page_mask=page_mask, comp_mask=comp_mask)


# --------------------------------------------------------------------------
# Exhaustive DP
# --------------------------------------------------------------------------

def _search_sequence(src: _Source, target_local: int) -> tuple[int, tuple[int, ...]]:
    """Return (swap count, original-index sequence) of the optimal arrangement.

    State: (mask of written sectors, phase word). Each local transaction owns
    two phase bits:
        bit 0: a digest-matching page was written while its prepare was open
        bit 1: the transaction has been adopted by a consistent complete
    Everything else (prepare present, matching page present) derives from the
    mask. Every sector can be appended in every state -- all permutations are
    explored; records that violate under the existing semantics simply leave
    the phase word unchanged.
    """
    n = len(src.sectors)
    t = len(src.tx_ids)
    effects = src.effects
    full = (1 << n) - 1
    tgen, tslot = src.generations[target_local], src.slots[target_local]

    # Dominance: for the final boot pick to be the target, no other adopted
    # transaction may outrank it (higher generation, or same generation on a
    # lexicographically smaller slot).
    def target_outranks_completed(phase: int) -> bool:
        for i in range(t):
            if not (phase >> (2 * i + 1)) & 1:
                continue  # tx i not adopted in this prefix
            g, s = src.generations[i], src.slots[i]
            if g > tgen or (g == tgen and s != tslot and s < tslot):
                return False
        return True

    # best[(mask, phase)] = (cost, sequence of original indices)
    best: dict[tuple[int, int], tuple[int, tuple[int, ...]]] = {(0, 0): (0, ())}
    layers: list[list[tuple[int, int]]] = [[(0, 0)]]
    for _ in range(n):
        layers.append([])

    for depth, layer in enumerate(layers[:-1]):
        next_layer = layers[depth + 1]
        for state in layer:
            mask, phase = state
            cost, seq = best[state]
            for j in range(n):
                if mask & (1 << j):
                    continue
                eff = effects[j]
                new_phase = phase
                i = eff.tx
                if i is not None:
                    p_bit = 1 << (2 * i)
                    c_bit = 1 << (2 * i + 1)
                    prep_present = bool(mask & src.prep_mask[i])
                    if eff.kind == "page" and eff.ok and prep_present \
                            and not (phase & c_bit):
                        new_phase |= p_bit
                    elif eff.kind == "comp" and eff.ok and prep_present \
                            and (phase & p_bit) and not (phase & c_bit):
                        new_phase |= c_bit
                # adjacent-swap cost: smaller original indices still unwritten
                # end up to the right of j, each creating one inversion.
                smaller_unused = j - (mask & ((1 << j) - 1)).bit_count()
                cand = (cost + smaller_unused, seq + (j,))
                key = (mask | (1 << j), new_phase)
                old = best.get(key)
                if old is None:
                    best[key] = cand
                    next_layer.append(key)
                elif cand < old:
                    best[key] = cand

    winners: list[tuple[int, tuple[int, ...]]] = []
    for (mask, phase), value in best.items():
        if mask != full:
            continue
        if not (phase >> (2 * target_local + 1)) & 1:
            continue  # target transaction never adopted
        if target_outranks_completed(phase):
            winners.append(value)
    if not winners:
        raise ReorderError(
            "target_not_bootable",
            f"全部 {_factorial(n)} 种扇区排列均经重放：目标事务 "
            f"{src.tx_ids[target_local]} 要么无法三段齐备，要么最终启动槽"
            f"按代次/槽名裁决属于他事务；不允许靠打乱他事务记录窃取启动槽",
            400)
    return min(winners)


def _factorial(n: int) -> int:
    v = 1
    for k in range(2, n + 1):
        v *= k
    return v


def _adjacent_swap_steps(sequence: list[int]) -> list[list[int]]:
    """Concrete adjacent position swaps turning [0..n-1] into ``sequence``.

    Each wanted sector is bubbled left to its slot; the number of steps equals
    the inversion count (the minimal number of adjacent swaps).
    """
    arr = list(range(len(sequence)))
    steps: list[list[int]] = []
    for i, wanted in enumerate(sequence):
        j = arr.index(wanted)
        while j > i:
            arr[j - 1], arr[j] = arr[j], arr[j - 1]
            steps.append([j - 1, j])
            j -= 1
    return steps


def plan_correction(
    correction_id: str,
    source_audit_id: str,
    active_slot: str,
    sectors_b64: list[str],
    target_tx: int,
) -> CorrectionPlan:
    src = analyze_source(sectors_b64)
    n = len(src.sectors)

    if target_tx not in src.tx_ids:
        raise ReorderError(
            "target_not_in_image",
            f"目标事务 {target_tx} 的准备记录不在来源镜像中，无法在不改动字节的"
            f"前提下使其被采纳", 400)
    ti = src.tx_ids.index(target_tx)
    if not src.comp_mask[ti]:
        raise ReorderError(
            "target_not_in_image",
            f"镜像中缺少事务 {target_tx} 与准备记录字段相符的完成记录，"
            f"纯重排无法补齐扇区", 400)
    if not src.page_mask[ti]:
        raise ReorderError(
            "target_not_in_image",
            f"镜像中缺少事务 {target_tx} 与准备记录摘要相符的完整目标槽页，"
            f"纯重排无法制造该页", 400)

    swap_count, sequence = _search_sequence(src, ti)

    target_slot, target_gen = src.slots[ti], src.generations[ti]

    # Original-order recovery verdict of the frozen source (proves the target
    # was not adopted before the correction, and is frozen with the plan).
    original_verdict = judge_recovery(source_audit_id, active_slot, sectors_b64)
    source_verdict = {
        "audit_id": source_audit_id,
        "active_slot": active_slot,
        "total_sectors": n,
        "first_violation": original_verdict.first_violation,
        "boot_slot": original_verdict.boot_slot,
        "boot_generation": original_verdict.boot_generation,
        "boot_reason": original_verdict.boot_reason,
        "target_adopted_originally": (
            original_verdict.boot_slot == target_slot
            and original_verdict.boot_generation == target_gen
        ),
    }

    # Authoritative prefix-by-prefix replay through the unchanged judge. -----
    target_comp_sectors = src.comp_mask[ti]
    prefixes: list[PrefixVerdict] = []
    final_result = None
    for length in range(1, n + 1):
        prefix_orig = list(sequence[:length])
        prefix_mask = 0
        for k in range(length):
            prefix_mask |= 1 << sequence[k]
        reordered = [sectors_b64[sequence[i]] for i in range(length)]
        verdict = judge_recovery(source_audit_id, active_slot, reordered)
        comp_included = bool(prefix_mask & target_comp_sectors)
        target_bootable = (
            verdict.boot_slot == target_slot and verdict.boot_generation == target_gen
        )
        if target_bootable and not comp_included:
            raise ReorderError(
                "unsafe_prefix",
                f"长度 {length} 的前缀在目标完成记录写入前即把未完成目标页"
                f"（槽 {target_slot}，代次 {target_gen}）裁决为可启动", 400)
        prefixes.append(PrefixVerdict(
            length=length,
            physical_sequence=prefix_orig,
            boot_slot=verdict.boot_slot,
            boot_generation=verdict.boot_generation,
            target_complete_included=comp_included,
            target_bootable=target_bootable,
            first_violation=(verdict.first_violation or {}).get("code"),
            reason=verdict.boot_reason,
        ))
        if length == n:
            final_result = verdict

    assert final_result is not None
    if (final_result.boot_slot != target_slot
            or final_result.boot_generation != target_gen):
        raise ReorderError(
            "target_not_bootable",
            f"最终启动裁决为 {final_result.boot_slot} 代次 "
            f"{final_result.boot_generation}，目标为 {target_slot} 代次 "
            f"{target_gen}", 400)

    slot_state = final_result.slots[target_slot]
    page_orig = sequence[slot_state.page_index]
    complete_orig = sequence[slot_state.complete_index]
    page_pos = sequence.index(page_orig)
    # The judge adopts the first prepare of the tx encountered before the page.
    prepare_candidates = [
        j for j in range(n)
        if (src.prep_mask[ti] >> j) & 1 and sequence.index(j) < page_pos
    ]
    prepare_orig = min(prepare_candidates, key=lambda j: sequence.index(j))

    page_sector = src.sectors[page_orig]
    source_hash = hashlib.sha256(
        b"\n".join(s.encode("ascii") for s in sectors_b64)).hexdigest()

    return CorrectionPlan(
        correction_id=correction_id,
        source_audit_id=source_audit_id,
        active_slot=active_slot,
        target_transaction=target_tx,
        total_sectors=n,
        source_hash=source_hash,
        source_sectors=list(sectors_b64),
        source_verdict=source_verdict,
        target_slot=target_slot,
        target_generation=target_gen,
        target_digest=f"{zlib.crc32(page_sector.payload) & 0xFFFFFFFF:08x}",
        target_payload_hex=page_sector.payload.hex(),
        prepare_index=prepare_orig,
        page_index=page_orig,
        complete_index=complete_orig,
        physical_sequence=list(sequence),
        swap_count=swap_count,
        swap_steps=_adjacent_swap_steps(list(sequence)),
        prefixes=prefixes,
    )
