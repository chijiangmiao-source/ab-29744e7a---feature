"""HTTP API: submit sector images for audit, and re-open frozen conclusions.

Endpoints (JSON in / JSON out, all server logic driven by the real judge):
  GET  /healthz
  GET  /api/audits/<audit_id>
  POST /api/audits
       body: {"audit_id","active_slot","sectors":[base64,...]}
  GET  /api/corrections/<correction_id>
  POST /api/corrections
       body: {"correction_id","source_audit_id","target_transaction",
               "active_slot","sectors":[base64,...]}

A correction freezes the minimal adjacent-transposition arrangement that lets
the target transaction be adopted from the frozen source bytes. It is frozen
under its own correction id in a separate file: the original audit conclusion
is never mutated and stays readable. Reusing a correction id with different
source bytes is refused.

The page is real-API-driven: the HTML is a thin shell and all conclusions,
decisions and violations come from the API.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .parser import MAX_SECTORS, judge_recovery
from .reorder import MAX_CORRECTION_SECTORS, ReorderError, plan_correction
from .storage import AuditExistsError, FrozenStore

AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SLOT_NAME_RE = re.compile(r"^[A-Z0-9_]{1,8}$")


def _json_bytes(obj: dict, status: int = 200) -> tuple[bytes, int]:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8"), status


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "SlotAudit/1.0"

    # silence default noisy access logs; keep only errors
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def _send(self, body: bytes, status: int, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, status: int = 200):
        body, status = _json_bytes(obj, status)
        self._send(body, status)

    def _bad_request(self, code: str, message: str, **extra):
        self._send_json({"error": code, "message": message, **extra}, 400)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "ok", "service": "slot-audit", "version": 1}, 200)
            return
        if path == "/" or path == "/index.html":
            self._send(INDEX_HTML.encode("utf-8"), 200, "text/html")
            return
        if path.startswith("/api/audits/"):
            audit_id = path[len("/api/audits/"):]
            if not AUDIT_ID_RE.match(audit_id):
                self._bad_request("bad_audit_id", "审计标识格式非法")
                return
            store = self.server.store  # type: ignore[attr-defined]
            record = store.get(audit_id)
            if record is None:
                self._send_json({"error": "not_found",
                                 "message": f"审计 {audit_id} 不存在或尚未冻结"}, 404)
                return
            self._send_json(record, 200)
            return
        if path.startswith("/api/corrections/"):
            correction_id = path[len("/api/corrections/"):]
            if not AUDIT_ID_RE.match(correction_id):
                self._bad_request("bad_correction_id", "纠正标识格式非法")
                return
            record = self.server.correction_store.get(correction_id)  # type: ignore[attr-defined]
            if record is None:
                self._send_json({"error": "not_found",
                                 "message": f"纠正 {correction_id} 不存在或尚未冻结"}, 404)
                return
            self._send_json(record, 200)
            return
        self._send_json({"error": "not_found", "message": "path not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/corrections":
            self._post_correction()
            return
        if path != "/api/audits":
            self._send_json({"error": "not_found", "message": "path not found"}, 404)
            return
        body, err = self._read_json_body()
        if err is not None:
            self._bad_request(err[0], err[1])
            return
        if not isinstance(body, dict):
            self._bad_request("bad_json", "请求体必须是 JSON 对象")
            return
        ok, err = self._validate(body)
        if not ok:
            self._bad_request(err[0], err[1])
            return

        audit_id = body["audit_id"].strip()
        active_slot = body["active_slot"].strip().upper()
        sectors = body["sectors"]

        result = judge_recovery(audit_id, active_slot, sectors)
        conclusion = result.to_public_dict()
        try:
            stored = self.server.store.put_if_absent(audit_id, conclusion)  # type: ignore[attr-defined]
        except AuditExistsError:
            existing = self.server.store.get(audit_id)  # type: ignore[attr-defined]
            self._send_json({
                "error": "audit_exists",
                "message": (f"审计标识 {audit_id} 的恢复结论已被冻结，"
                            f"不能重新裁决；请通过 GET 查看冻结结论"),
                "frozen": existing,
            }, 409)
            return
        self._send_json(stored, 201)

    def _read_json_body(self) -> tuple[object, tuple | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, ("bad_content_length", "Content-Length 非法")
        if length <= 0 or length > 256 * 1024:
            return None, ("bad_content_length", "请求体为空或超过 256KiB 限制")
        raw_body = self.rfile.read(length)
        try:
            return json.loads(raw_body.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, ("bad_json", "请求体不是合法 JSON")

    def _post_correction(self) -> None:
        body, err = self._read_json_body()
        if err is not None:
            self._bad_request(err[0], err[1])
            return
        if not isinstance(body, dict):
            self._bad_request("bad_json", "请求体必须是 JSON 对象")
            return
        ok, err = self._validate_correction(body)
        if not ok:
            self._bad_request(err[0], err[1])
            return

        correction_id = body["correction_id"].strip()
        source_audit_id = body["source_audit_id"].strip()
        active_slot = body["active_slot"].strip().upper()
        target_tx = body["target_transaction"]
        sectors = body.get("sectors")

        # The frozen source (sectors / active slot) is authoritative. If the
        # referenced audit is frozen here, its bytes are replayed; a direct
        # submission must match an already-frozen correction's source hash.
        store = self.server.store  # type: ignore[attr-defined]
        cstore = self.server.correction_store  # type: ignore[attr-defined]
        frozen_source = store.get(source_audit_id)
        if frozen_source is not None:
            frozen_sectors = list(frozen_source.get("sectors", []))
            if sectors is not None and sectors != frozen_sectors:
                self._send_json({
                    "error": "source_mismatch",
                    "message": (f"提交的扇区与已冻结来源审计 {source_audit_id} "
                                f"的字节不一致；纠正不得改动任何扇区字节"),
                }, 409)
                return
            sectors = frozen_sectors
            active_slot = frozen_source.get("active_slot", active_slot)
        elif not sectors:
            self._bad_request(
                "source_not_frozen",
                f"来源审计 {source_audit_id} 未在本服务冻结，且请求未提供来源扇区；"
                f"请先冻结来源或随纠正提交与其一致的扇区")
            return

        try:
            plan = plan_correction(
                correction_id, source_audit_id, active_slot, sectors, target_tx)
        except ReorderError as exc:
            self._send_json({
                "error": exc.code,
                "message": exc.message,
                **({"detail": exc.detail} if exc.detail else {}),
            }, exc.status)
            return
        plan_dict = plan.to_public_dict()
        try:
            stored = cstore.put_if_absent(correction_id, plan_dict)
        except AuditExistsError:
            existing = cstore.get(correction_id)
            same = (
                existing is not None
                and existing.get("source_audit_id") == source_audit_id
                and existing.get("source_hash") == plan_dict["source_hash"]
                and existing.get("target_transaction") == target_tx
            )
            if same:
                self._send_json(existing, 200)
                return
            self._send_json({
                "error": "correction_exists",
                "message": (f"纠正标识 {correction_id} 已冻结且绑定不同来源数据"
                            f"（来源哈希 {existing.get('source_hash')}），"
                            f"同一纠正标识不得改换来源数据"),
                "frozen": existing,
            }, 409)
            return
        self._send_json(stored, 201)

    def _validate_correction(self, body: dict) -> tuple[bool, tuple | None]:
        cid = body.get("correction_id")
        if not isinstance(cid, str) or not AUDIT_ID_RE.match(cid.strip()):
            return False, ("bad_correction_id",
                           "纠正标识须为 1-64 位字母/数字/_.-且首字符为字母数字")
        sid = body.get("source_audit_id")
        if not isinstance(sid, str) or not AUDIT_ID_RE.match(sid.strip()):
            return False, ("bad_source_audit_id", "来源审计标识格式非法")
        active_slot = body.get("active_slot")
        if not isinstance(active_slot, str) or not SLOT_NAME_RE.match(
                active_slot.strip().upper()):
            return False, ("bad_active_slot", "初始活动槽须为 1-8 位大写字母数字下划线")
        tx = body.get("target_transaction")
        if not isinstance(tx, int) or isinstance(tx, bool) \
                or not 0 <= tx <= 0xFFFFFFFF:
            return False, ("bad_target_transaction",
                           "目标事务标识须为 0..2^32-1 的整数")
        sectors = body.get("sectors")
        if sectors is not None:
            if not isinstance(sectors, list) or not sectors:
                return False, ("empty_sectors",
                               "来源扇区须为非空数组，或省略以使用冻结来源")
            if len(sectors) > MAX_CORRECTION_SECTORS:
                return False, ("too_many_sectors",
                               f"纠正仅接受至多 {MAX_CORRECTION_SECTORS} 个扇区")
            for i, item in enumerate(sectors):
                if not isinstance(item, str):
                    return False, ("bad_sector", f"第 {i} 个扇区不是字符串")
        return True, None

    def _validate(self, body: dict) -> tuple[bool, tuple | None]:
        audit_id = body.get("audit_id")
        if not isinstance(audit_id, str) or not AUDIT_ID_RE.match(audit_id.strip()):
            return False, ("bad_audit_id",
                           "审计标识须为 1-64 位字母/数字/_.-且首字符为字母数字")
        active_slot = body.get("active_slot")
        if not isinstance(active_slot, str) or not SLOT_NAME_RE.match(
                active_slot.strip().upper()):
            return False, ("bad_active_slot", "初始活动槽须为 1-8 位大写字母数字下划线")
        sectors = body.get("sectors")
        if not isinstance(sectors, list) or not sectors:
            return False, ("empty_sectors", "至少提交一个 Base64 扇区")
        if len(sectors) > MAX_SECTORS:
            return False, ("too_many_sectors", f"至多 {MAX_SECTORS} 个扇区")
        for i, item in enumerate(sectors):
            if not isinstance(item, str):
                return False, ("bad_sector", f"第 {i} 个扇区不是字符串")
        return True, None


def build_server(host: str, port: int, store: FrozenStore,
                 correction_store: FrozenStore) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    httpd.store = store  # type: ignore[attr-defined]
    httpd.correction_store = correction_store  # type: ignore[attr-defined]
    return httpd


# Front-end shell (real-API-driven). Filled by app.web (kept out of this file
# to avoid a giant string here).
from .page import INDEX_HTML  # noqa: E402
