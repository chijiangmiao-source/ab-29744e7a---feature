"""Durable store of frozen recovery conclusions, keyed by audit id.

Each audit id is write-once: the first submission freezes the conclusion and
later submissions for the same id are rejected, so a reviewer revisiting the
page always sees the original verdict. A JSON file under $DATA_DIR (with an
fcntl lock) backs the store.
"""

from __future__ import annotations

import fcntl
import json
import os
from typing import Optional


class AuditExistsError(Exception):
    pass


class FrozenStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({}, fh)

    def _load_locked(self, fh) -> dict:
        fh.seek(0)
        text = fh.read()
        return json.loads(text) if text.strip() else {}

    def get(self, audit_id: str) -> Optional[dict]:
        with open(self.path, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_locked(fh).get(audit_id)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def put_if_absent(self, audit_id: str, conclusion: dict) -> dict:
        """Freeze `conclusion` under audit_id. Returns the stored record.

        Raises AuditExistsError if the id was already frozen.
        """
        with open(self.path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load_locked(fh)
                if audit_id in data:
                    raise AuditExistsError(audit_id)
                frozen = dict(conclusion)
                frozen["frozen"] = True
                data[audit_id] = frozen
                fh.seek(0)
                fh.truncate()
                json.dump(data, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
                return frozen
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
