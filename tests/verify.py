"""One-shot verification used by the compose `verify` service.

Runs, in order, and exits non-zero on the first failure:
  1. build check  : byte-compile every package (python -m compileall equivalent)
  2. code tests   : full unittest suite (parser, judge, store, in-process HTTP)
  3. HTTP smoke   : against the already-running web container, using the three
                    bundled sample images (clean switch / corrupt completion /
                    older invalid write retention), including frozen re-view.
"""

from __future__ import annotations

import compileall
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE_URL = os.environ.get("BASE_URL", "http://web:8080")
HEALTH_ATTEMPTS = 30


def step(title: str):
    print(f"\n=== verify: {title} ===", flush=True)


def fail(msg: str):
    print(f"VERIFY FAIL: {msg}", flush=True)
    sys.exit(1)


def http(method: str, path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_healthy():
    for i in range(HEALTH_ATTEMPTS):
        try:
            status, body = http("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                print(f"healthz ok after {i + 1} attempt(s): {body}", flush=True)
                return
        except Exception as e:  # network not up yet
            print(f"  waiting for web ({i + 1}): {e}", flush=True)
        time.sleep(1)
    fail("web service did not become healthy")


def read_sample(name: str) -> list[str]:
    with open(os.path.join(ROOT, "data", name), encoding="ascii") as fh:
        return [line.strip() for line in fh if line.strip()]


def expect(cond: bool, msg: str):
    if not cond:
        fail(msg)
    print(f"  ok: {msg}", flush=True)


def smoke():
    wait_healthy()

    # 1) clean switch: newer generation on SLOT_B wins
    status, body = http("POST", "/api/audits", {
        "audit_id": "VERIFY-CLEAN", "active_slot": "SLOT_A",
        "sectors": read_sample("01_clean_switch.txt")})
    expect(status == 201, f"clean switch accepted (HTTP {status})")
    expect(body.get("first_violation") is None, "clean switch has no violation")
    expect(body["boot"]["slot"] == "SLOT_B" and body["boot"]["generation"] == 2,
           "clean switch boots SLOT_B generation 2")
    expect(body["frozen"] is True, "conclusion frozen")

    # 2) corrupted completion marker: half-written newer slot never boots
    status, body = http("POST", "/api/audits", {
        "audit_id": "VERIFY-CORRUPT", "active_slot": "SLOT_A",
        "sectors": read_sample("02_corrupt_complete.txt")})
    expect(status == 201, f"corrupt image accepted for audit (HTTP {status})")
    v = body.get("first_violation") or {}
    expect(v.get("index") == 5 and v.get("code") == "bad_sector_crc",
           "first violation located at sector #5 bad_sector_crc")
    expect(body["boot"]["slot"] == "SLOT_A" and body["boot"]["generation"] == 1,
           "boot stays on last-valid SLOT_A generation 1")
    expect("SLOT_B" not in body["slots"],
           "half-written newer SLOT_B is not reported as a bootable slot")

    # 3) later invalid/older write does not overwrite last valid generation
    status, body = http("POST", "/api/audits", {
        "audit_id": "VERIFY-RETAIN", "active_slot": "SLOT_A",
        "sectors": read_sample("03_older_invalid_kept.txt")})
    expect(status == 201, f"retention image accepted (HTTP {status})")
    expect(body["boot"]["slot"] == "SLOT_A" and body["boot"]["generation"] == 3,
           "SLOT_A stays at valid generation 3")
    discarded = [d for d in body["decisions"]
                 if d["kind"] == "complete" and not d["adopted"]]
    expect(len(discarded) == 1 and "不得回滚覆盖" in discarded[0]["basis"],
           "older generation complete is shown discarded with basis")

    # 4) frozen re-view by stable audit id
    status, body = http("GET", "/api/audits/VERIFY-CORRUPT")
    expect(status == 200 and body["boot"]["generation"] == 1,
           "frozen conclusion re-opened by audit id with same verdict")

    # 5) resubmitting a frozen id is refused
    status, body = http("POST", "/api/audits", {
        "audit_id": "VERIFY-CLEAN", "active_slot": "SLOT_A",
        "sectors": read_sample("01_clean_switch.txt")})
    expect(status == 409 and body.get("error") == "audit_exists",
           "re-submitting a frozen audit id is refused with 409")

    correction_smoke()


def correction_smoke():
    # Out-of-order but byte-clean complete transaction: first freeze it as a
    # normal recovery audit (the original verdict is "target not adopted"),
    # then ask for the minimal replay correction.
    reorder_sectors = read_sample("04_reorder_target.txt")

    status, body = http("POST", "/api/audits", {
        "audit_id": "VERIFY-REORDER-SRC", "active_slot": "SLOT_A",
        "sectors": reorder_sectors})
    expect(status == 201, f"out-of-order source frozen as a normal audit (HTTP {status})")
    expect(body.get("boot") is None,
           "the physically out-of-order target transaction is not adopted originally")
    expect(body.get("first_violation", {}).get("code") == "page_before_prepare",
           "original audit records page_before_prepare")

    # Correction replayed purely from the frozen source bytes (sectors omitted).
    status, body = http("POST", "/api/corrections", {
        "correction_id": "VERIFY-REORDER-FIX",
        "source_audit_id": "VERIFY-REORDER-SRC",
        "active_slot": "SLOT_A", "target_transaction": 400})
    expect(status == 201, f"replay correction accepted (HTTP {status})")
    expect(body.get("physical_sequence") == [1, 0, 2],
           "canonical physical index sequence is [1, 0, 2]")
    expect(body.get("swap_count") == 1 and body.get("swap_steps") == [[0, 1]],
           "the minimum is exactly one adjacent swap at positions 0/1")
    expect(body.get("target", {}).get("slot") == "SLOT_B"
           and body["target"].get("generation") == 2,
           "target transaction 400 switches SLOT_B to generation 2")
    ev = body.get("target", {})
    expect((ev.get("prepare_index"), ev.get("page_index"),
            ev.get("complete_index")) == (1, 0, 2),
           "prepare / slot-page / complete evidence points at physical indices 1/0/2")
    prefixes = body.get("prefixes", [])
    expect(len(prefixes) == 3, "every prefix length is present")
    expect(all(not (p["target_bootable"] and not p["target_complete_included"])
               for p in prefixes),
           "no prefix treats the unfinished target page as bootable")
    expect(prefixes[-1].get("boot_slot") == "SLOT_B"
           and prefixes[-1].get("boot_generation") == 2
           and prefixes[-1].get("target_complete_included") is True,
           "the final prefix boots the target after its complete is written")

    # Frozen correction can be reopened by its stable correction id.
    status, body = http("GET", "/api/corrections/VERIFY-REORDER-FIX")
    expect(status == 200 and body.get("swap_count") == 1
           and body.get("physical_sequence") == [1, 0, 2],
           "frozen correction reopened by correction id with the same plan")

    # A corrupted source must be refused for correction (reorder cannot fix bytes).
    import base64 as _b64
    corrupt = list(reorder_sectors)
    raw = bytearray(_b64.b64decode(corrupt[1]))
    raw[20] ^= 0xFF
    corrupt[1] = _b64.b64encode(bytes(raw)).decode("ascii")
    status, body = http("POST", "/api/corrections", {
        "correction_id": "VERIFY-REORDER-CORRUPT",
        "source_audit_id": "VERIFY-REORDER-SRC",
        "active_slot": "SLOT_A", "target_transaction": 400,
        "sectors": corrupt})
    expect(status == 409 and body.get("error") == "source_mismatch",
           "bytes differing from the frozen source are refused as source_mismatch")

    status, body = http("POST", "/api/corrections", {
        "correction_id": "VERIFY-REORDER-CORRUPT2",
        "source_audit_id": "STANDALONE-CORRUPT",
        "active_slot": "SLOT_A", "target_transaction": 400,
        "sectors": corrupt})
    expect(status == 400 and body.get("error") == "source_corrupt",
           "a byte-corrupt standalone source is refused with source_corrupt")

    # The original recovery audit remains readable with its ORIGINAL verdict.
    status, body = http("GET", "/api/audits/VERIFY-REORDER-SRC")
    expect(status == 200 and body.get("boot") is None
           and body.get("first_violation", {}).get("code") == "page_before_prepare",
           "the original recovery audit still reports its original conclusion")


def main() -> int:
    step("build check (compileall)")
    ok = compileall.compile_dir(os.path.join(ROOT, "app"), quiet=1, maxlevels=10)
    ok &= compileall.compile_dir(os.path.join(ROOT, "tests"), quiet=1, maxlevels=10)
    if not ok:
        fail("compileall reported syntax errors")
    print("  ok: all modules byte-compile", flush=True)

    step("code tests (unittest)")
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py",
                            top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        fail("unit tests failed")

    step(f"HTTP smoke against {BASE_URL}")
    smoke()

    print("\nVERIFY OK: build check, code tests and HTTP smoke all passed",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
