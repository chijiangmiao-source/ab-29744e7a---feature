"""Stable correction planner: minimum adjacent-swap reordering of a frozen,
byte-valid sector image so an already-present target transaction is adopted.

The reviewer never alters a single sector byte.  The planner searches **all**
permutations of the frozen sectors (at most CORRECTION_MAX_SECTORS = 12),
adjudicating every prefix with the same recovery semantics as app.parser, and
accepts only permutations whose final boot slot is the target transaction's
slot, and in whose prefixes the target page can never appear bootable while the
target transaction itself is not yet completed.

Optimality is exact, not greedy:

  * the minimum number of adjacent transpositions that turns the physical
    order into a permutation equals that permutation's inversion number;
  * a subset DP over placements keeps, for every placed subset, every distinct
    recovery-machine state, tagged with the smallest inversion count and the
    lexicographically smallest final original-index sequence.

Records are never grouped by type and moved as blocks: each next sector is
adjudicated individually.  The winning permutation is always replayed through
the real `judge_recovery` before the frozen correction record is produced.
"""

from __future__ import annotations

import zlib
from typing import Optional

from .parser import (
    TYPE_COMPLETE,
    TYPE_PAGE,
    TYPE_PREPARE,
    SectorParseError,
    decode_sector_b64,
    judge_recovery,
    parse_sector,
)

CORRECTION_MAX_SECTORS = 12


class CorrectionError(Exception):
    """A correction request that must be refused.

    `status` is the HTTP status the API layer returns; `index` (when set) is
    the physical source sector index the refusal is located at.
    """

    def __init__(self, code: str, message: str, *, status: int = 400,
                 index: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.index = index


# --------------------------------------------------------------------------
# Validation of the frozen source
# --------------------------------------------------------------------------

def _parse_all_strict(sectors_b64: list[str]):
    """Decode + byte-validate every sector; the first failure is refused."""
    parsed = []
    for index, text in enumerate(sectors_b64):
        try:
            raw = decode_sector_b64(text)
        except SectorParseError as e:
            raise CorrectionError(e.code, e.message, index=index)
        try:
            parsed.append(parse_sector(raw, index))
        except SectorParseError as e:
            raise CorrectionError(e.code, e.message, index=index)
    return parsed


def _record_key(sec) -> tuple:
    return sec.generation, sec.slot, sec.payload_digest


def _check_conflicts(secs) -> None:
    """Reject duplicate transaction ids carrying contradictory content.

    All prepare records of one transaction must agree on
    (generation, slot, payload_digest), all complete records must agree, and a
    complete record must not contradict the prepare of the same transaction.
    """
    prepares: dict[int, list] = {}
    completes: dict[int, list] = {}
    for sec in secs:
        if sec.stype == TYPE_PREPARE:
            prepares.setdefault(sec.transaction_id, []).append(sec)
        elif sec.stype == TYPE_COMPLETE:
            completes.setdefault(sec.transaction_id, []).append(sec)

    for tx, group in prepares.items():
        base = _record_key(group[0])
        for sec in group[1:]:
            if _record_key(sec) != base:
                raise CorrectionError(
                    "conflicting_transaction",
                    f"事务 {tx} 存在多条互相冲突的准备记录"
                    f"（扇区 #{group[0].index} 与扇区 #{sec.index} 的"
                    f"代次/槽名/载荷摘要不一致），无法在不改正字节的前提下裁决",
                    index=sec.index)
    for tx, group in completes.items():
        base = _record_key(group[0])
        for sec in group[1:]:
            if _record_key(sec) != base:
                raise CorrectionError(
                    "conflicting_transaction",
                    f"事务 {tx} 存在多条互相冲突的完成记录"
                    f"（扇区 #{group[0].index} 与扇区 #{sec.index}）",
                    index=sec.index)
        if tx in prepares and _record_key(prepares[tx][0]) != base:
            sec = group[0]
            prep = prepares[tx][0]
            raise CorrectionError(
                "conflicting_transaction",
                f"事务 {tx} 的完成记录（扇区 #{sec.index}）与其准备记录"
                f"（扇区 #{prep.index}）字段冲突",
                index=sec.index)


# --------------------------------------------------------------------------
# Incremental recovery machine (mirrors judge_recovery exactly)
#
# State signature (all tuples, hashable):
#   prepares  : arrival-ordered ((tx, prepare_orig_index), ...) of first
#               accepted prepare per transaction
#   completed : (tx, ...) sorted
#   pending   : ((tx, last_page_orig_index), ...) sorted; only the last
#               accepted page of an open transaction can complete it
#   slots     : ((slot, generation, tx, page_orig_index, complete_position),)
#               sorted by slot name
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------

def _slots_of(state) -> tuple:
    return state[3]


def _boot_pick(state):
    slots = _slots_of(state)
    if not slots:
        return None
    best = slots[0]
    for cur in slots[1:]:
        if cur[1] > best[1] or (cur[1] == best[1] and cur[0] < best[0]):
            best = cur
    return best  # (slot, generation, tx, page_orig, complete_pos)


def _advance(state, orig_index: int, position: int, secs: list):
    """Append sector `orig_index` at permutation `position`.

    Returns (new_state, violated): a transactional rejection leaves the
    recovery state untouched (exactly as judge_recovery records the violation
    and continues), so `violated` is informational only.
    """
    prepares, completed, pending, slots = state
    sec = secs[orig_index]

    if sec.stype == TYPE_PREPARE:
        tx = sec.transaction_id
        if tx in completed:
            return state, True  # prepare after complete: rejected
        for ptx, pidx in prepares:
            if ptx == tx:
                # content conflicts were refused at validation; a consistent
                # duplicate prepare is ignored by the recovery machine
                return state, False
        prepares = prepares + ((tx, orig_index),)
        return (prepares, completed, pending, slots), False

    if sec.stype == TYPE_PAGE:
        owner = None
        for ptx, pidx in prepares:
            if ptx in completed:
                continue
            prep = secs[pidx]
            if prep.generation == sec.generation and prep.slot == sec.slot:
                owner = (ptx, pidx)  # latest (most recent) matching open prep
        if owner is None:
            return state, True  # no matching open prepare
        tx, pidx = owner
        digest = zlib.crc32(sec.payload) & 0xFFFFFFFF
        if digest != secs[pidx].payload_digest:
            return state, True  # digest mismatch
        pending = tuple(sorted(
            ({(t, i) for t, i in pending if t != tx}
             | {(tx, orig_index)}), key=lambda x: x[0]))
        return (prepares, completed, pending, slots), False

    # complete record
    tx = sec.transaction_id
    prep_idx = None
    for ptx, pidx in prepares:
        if ptx == tx:
            prep_idx = pidx
            break
    if prep_idx is None or tx in completed:
        return state, True  # dangling / duplicate complete
    prep = secs[prep_idx]
    if (prep.generation != sec.generation or prep.slot != sec.slot
            or prep.payload_digest != sec.payload_digest):
        return state, True
    page_idx = None
    for ptx, pidx in pending:
        if ptx == tx:
            page_idx = pidx
            break
    if page_idx is None:
        return state, True  # complete without an accepted target page

    completed = tuple(sorted(set(completed) | {tx}))
    pending = tuple((t, i) for t, i in pending if t != tx)
    new_entry = (sec.slot, sec.generation, tx, page_idx, position)
    replaced = False
    out = []
    for entry in slots:
        if entry[0] == sec.slot:
            if entry[1] > sec.generation:
                out.append(entry)  # older generation may not roll back
            else:
                out.append(new_entry)
            replaced = True
        else:
            out.append(entry)
    if not replaced:
        out.append(new_entry)
    slots = tuple(sorted(out, key=lambda e: e[0]))
    return (prepares, completed, pending, slots), False


# --------------------------------------------------------------------------
# Exhaustive optimal search (subset DP over the recovery machine)
# --------------------------------------------------------------------------

def _target_pages(secs, target_tx: int) -> frozenset[int]:
    prep = next((s for s in secs if s.stype == TYPE_PREPARE
                 and s.transaction_id == target_tx), None)
    if prep is None:
        return frozenset()
    out = set()
    for i, s in enumerate(secs):
        if (s.stype == TYPE_PAGE and s.slot == prep.slot
                and s.generation == prep.generation
                and (zlib.crc32(s.payload) & 0xFFFFFFFF) == prep.payload_digest):
            out.add(i)
    return frozenset(out)


def _is_safe(state, target_tx: int, target_pages: frozenset[int]) -> bool:
    """No prefix may present an unfinished target page as bootable."""
    boot = _boot_pick(state)
    if boot is None:
        return True
    _, _, boot_tx, boot_page, _ = boot
    if boot_tx == target_tx:
        return True
    if boot_page in target_pages and target_tx not in state[1]:
        return False
    return True


def _search(secs, target_tx: int):
    n = len(secs)
    target_pages = _target_pages(secs, target_tx)
    init = ((), (), (), ())

    # dp[mask] = {state: (inversions, permutation_tuple)}
    dp: dict[int, dict] = {0: {init: (0, ())}}
    order = sorted(range(1 << n), key=lambda m: m.bit_count())
    for mask in order:
        states = dp.get(mask)
        if not states:
            continue
        position = mask.bit_count()
        for state, (inv, perm) in states.items():
            for j in range(n):
                if mask & (1 << j):
                    continue
                nxt, _violated = _advance(state, j, position, secs)
                if not _is_safe(nxt, target_tx, target_pages):
                    continue
                added = sum(1 for k in range(j + 1, n) if mask & (1 << k))
                cand = (inv + added, perm + (j,))
                bucket = dp.setdefault(mask | (1 << j), {})
                old = bucket.get(nxt)
                if old is None or cand < old:
                    bucket[nxt] = cand

    full = (1 << n) - 1
    best = None
    for state, (inv, perm) in dp.get(full, {}).items():
        boot = _boot_pick(state)
        if boot is None or boot[2] != target_tx:
            continue
        cand = (inv, perm)
        if best is None or cand < best:
            best = cand
    return best


# --------------------------------------------------------------------------
# Swap choreography + evidence
# --------------------------------------------------------------------------

def _swap_steps(perm: tuple[int, ...]) -> list[dict]:
    """Adjacent swaps (positions counted between neighbours) that transform
    the original physical order into `perm`.  Their count equals the inversion
    number of the permutation."""
    arr = list(range(len(perm)))
    steps = []
    for want in range(len(perm)):
        cur = arr.index(perm[want], want)
        while cur > want:
            steps.append({
                "at": cur - 1,
                "swap": [cur - 1, cur],
                "moves_original_index": arr[cur],
            })
            arr[cur - 1], arr[cur] = arr[cur], arr[cur - 1]
            cur -= 1
    return steps


def _prefix_conclusions(audit_id: str, active_slot: str,
                        sectors_b64: list[str], perm: tuple[int, ...],
                        target_tx: int, target_pages: frozenset[int]) -> list[dict]:
    prefixes = []
    for length in range(1, len(perm) + 1):
        sub = perm[:length]
        replay = judge_recovery(
            f"{audit_id}#prefix{length}", active_slot,
            [sectors_b64[i] for i in sub])
        boot_state = None
        if replay.boot_slot is not None:
            boot_state = replay.slots[replay.boot_slot]
        unfinished_page_booted = (
            boot_state is not None
            and boot_state.transaction_id != target_tx
            and sub[boot_state.page_index] in target_pages
        )
        prefixes.append({
            "length": length,
            "physical_prefix": list(sub),
            "boot_slot": replay.boot_slot,
            "boot_generation": replay.boot_generation,
            "target_transaction_completed": _tx_completed(replay, target_tx),
            "unfinished_target_page_bootable": unfinished_page_booted,
            "safe": not unfinished_page_booted,
            "reason": replay.boot_reason,
            "first_violation": None
            if replay.first_violation is None
            else {"index": replay.first_violation.get("index"),
                  "code": replay.first_violation.get("code")},
        })
    return prefixes


def _tx_completed(replay, tx: int) -> bool:
    return any(s.transaction_id == tx for s in replay.slots.values())


def _target_evidence(replay, perm: tuple[int, ...], target_tx: int,
                     target_pages: frozenset[int]) -> dict:
    by_kind = {"prepare": [], "slot_page": [], "complete": []}
    for d in replay.decisions:
        if d.transaction_id != target_tx:
            continue
        if d.kind in by_kind:
            by_kind[d.kind].append({
                "final_index": d.index,
                "original_index": perm[d.index],
                "seq": d.seq,
                "adopted": d.adopted,
                "basis": d.basis,
                "violation": d.violation,
            })
    state = next((s for s in replay.slots.values()
                  if s.transaction_id == target_tx), None)
    return {
        "transaction_id": target_tx,
        "slot": None if state is None else state.slot,
        "generation": None if state is None else state.generation,
        "digest": None if state is None else f"{state.digest:08x}",
        "payload_hex": None if state is None else state.payload.hex(),
        "prepare": by_kind["prepare"],
        "slot_page": by_kind["slot_page"],
        "complete": by_kind["complete"],
        "page_original_indices": sorted(target_pages),
        "adopted": state is not None,
        "final_page_index": None if state is None else state.page_index,
        "final_complete_index": None if state is None else state.complete_index,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def plan_correction(correction_id: str, source_audit: dict,
                    target_tx: int) -> dict:
    """Produce the frozen correction plan for a target transaction inside a
    frozen source audit.  Raises CorrectionError on every refusal path."""
    audit_id = source_audit.get("audit_id")
    active_slot = source_audit.get("active_slot", "")
    sectors_b64 = source_audit.get("sectors")
    if not isinstance(sectors_b64, list) or not sectors_b64:
        raise CorrectionError(
            "source_sectors_missing",
            "来源冻结审计中没有扇区快照，无法在不改动字节的前提下重排",
            status=404)
    if len(sectors_b64) > CORRECTION_MAX_SECTORS:
        raise CorrectionError(
            "too_many_sectors",
            f"稳定纠正仅支持至多 {CORRECTION_MAX_SECTORS} 个扇区的镜像，"
            f"来源镜像含 {len(sectors_b64)} 个扇区")

    secs = _parse_all_strict(sectors_b64)
    _check_conflicts(secs)

    prepare = next((s for s in secs if s.stype == TYPE_PREPARE
                    and s.transaction_id == target_tx), None)
    complete = next((s for s in secs if s.stype == TYPE_COMPLETE
                     and s.transaction_id == target_tx), None)
    target_pages = _target_pages(secs, target_tx)
    if prepare is None or complete is None or not target_pages:
        missing = []
        if prepare is None:
            missing.append("准备记录")
        if not target_pages:
            missing.append("匹配的目标槽完整页")
        if complete is None:
            missing.append("完成记录")
        raise CorrectionError(
            "target_not_in_image",
            f"事务 {target_tx} 不在镜像中：缺少{('、'.join(missing))}",
            status=404)

    best = _search(secs, target_tx)
    if best is None:
        raise CorrectionError(
            "no_valid_permutation",
            f"在全部 {_factorial(len(secs))} 种排列中不存在满足条件的编排："
            f"事务 {target_tx} 无法成为最终启动事务"
            f"（可能被更高代次槽稳定胜出，或其页在某前缀必然悬空）",
            status=422)
    inv, perm = best

    reordered = [sectors_b64[i] for i in perm]
    final = judge_recovery(f"{audit_id}#correction#{correction_id}",
                           active_slot, reordered, frozen=True)
    # Belt-and-braces: the real judge must accept the target at final boot.
    if (final.boot_slot is None
            or final.slots[final.boot_slot].transaction_id != target_tx):
        raise CorrectionError("no_valid_permutation",
                              "候选编排重放失败：目标事务未被最终启动",
                              status=422)

    prefixes = _prefix_conclusions(audit_id, active_slot, sectors_b64, perm,
                                   target_tx, target_pages)
    if not all(p["safe"] for p in prefixes):
        raise CorrectionError("no_valid_permutation",
                              "候选编排存在不安全前缀", status=422)

    steps = _swap_steps(perm)
    if len(steps) != inv:  # internal invariant; should never happen
        raise CorrectionError("internal_error",
                              "相邻换位计数与逆序数不一致", status=500)

    source_boot = None
    if source_audit.get("boot"):
        source_boot = {
            "slot": source_audit["boot"].get("slot"),
            "generation": source_audit["boot"].get("generation"),
        }

    return {
        "kind": "reorder_correction",
        "correction_id": correction_id,
        "audit_id": audit_id,
        "active_slot": active_slot,
        "target_transaction_id": target_tx,
        "frozen": True,
        "source_sector_count": len(sectors_b64),
        "source_sectors": list(sectors_b64),
        "source_boot": source_boot,
        "source_first_violation": source_audit.get("first_violation"),
        "solution": {
            "physical_index_sequence": list(perm),
            "adjacent_swap_count": inv,
            "swap_steps": steps,
            "prefixes": prefixes,
            "target_evidence": _target_evidence(
                final, perm, target_tx, target_pages),
            "final_conclusion": final.to_public_dict(),
        },
    }


def _factorial(n: int) -> int:
    out = 1
    for i in range(2, n + 1):
        out *= i
    return out
