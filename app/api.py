"""HTTP API: submit sector images for audit, and re-open frozen conclusions.

Endpoints (JSON in / JSON out, all server logic driven by the real judge):
  GET  /healthz
  GET  /api/audits/<audit_id>
  POST /api/audits
       body: {"audit_id","active_slot","sectors":[base64,...]}

The page is real-API-driven: the HTML is a thin shell and all conclusions,
decisions and violations come from the API.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .parser import MAX_SECTORS, judge_recovery
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
        self._send_json({"error": "not_found", "message": "path not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/audits":
            self._send_json({"error": "not_found", "message": "path not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._bad_request("bad_content_length", "Content-Length 非法")
            return
        if length <= 0 or length > 256 * 1024:
            self._bad_request("bad_content_length",
                              "请求体为空或超过 256KiB 限制")
            return
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._bad_request("bad_json", "请求体不是合法 JSON")
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


def build_server(host: str, port: int, store: FrozenStore) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


# Front-end shell (real-API-driven). Filled by app.web (kept out of this file
# to avoid a giant string here).
from .page import INDEX_HTML  # noqa: E402
