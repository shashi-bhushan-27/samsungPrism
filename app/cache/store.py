"""Persistent cache storage (SQLite, WAL) and an in-memory variant for tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from app.cache.models import CacheRecord


class CacheStore(ABC):
    @abstractmethod
    def load_all(self) -> Iterator[tuple[CacheRecord, Optional[np.ndarray], str]]: ...

    @abstractmethod
    def put(self, record: CacheRecord, key_vectors: np.ndarray, embedding_signature: str) -> None: ...

    @abstractmethod
    def delete(self, plan_id: str) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    def size_bytes(self) -> Optional[int]:
        return None

    def close(self) -> None:
        return None


class MemoryCacheStore(CacheStore):
    def __init__(self) -> None:
        self._rows: dict[str, tuple[CacheRecord, np.ndarray, str]] = {}

    def load_all(self):
        for rec, vec, sig in list(self._rows.values()):
            yield rec, vec, sig

    def put(self, record, key_vectors, embedding_signature):
        self._rows[record.plan_id] = (record, np.array(key_vectors, dtype=np.float32), embedding_signature)

    def delete(self, plan_id):
        self._rows.pop(plan_id, None)

    def clear(self):
        self._rows.clear()


class SqliteCacheStore(CacheStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id TEXT PRIMARY KEY,
        origin TEXT NOT NULL,
        source_fp TEXT NOT NULL,
        record_json TEXT NOT NULL,
        key_vectors BLOB,
        key_dim INTEGER,
        embedding_signature TEXT,
        created_at REAL
    );
    CREATE INDEX IF NOT EXISTS plans_source ON plans(source_fp);
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self.SCHEMA)

    def load_all(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json, key_vectors, key_dim, embedding_signature FROM plans ORDER BY created_at, plan_id"
            ).fetchall()
        for record_json, blob, dim, sig in rows:
            rec = CacheRecord.from_json(json.loads(record_json))
            vec = None
            if blob is not None and dim:
                vec = np.frombuffer(blob, dtype=np.float32).reshape(-1, dim).copy()
            yield rec, vec, sig or ""

    def put(self, record, key_vectors, embedding_signature):
        vec = np.ascontiguousarray(key_vectors, dtype=np.float32)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO plans(plan_id, origin, source_fp, record_json, key_vectors, key_dim, "
                "embedding_signature, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    record.plan_id,
                    record.origin,
                    record.source_fp,
                    json.dumps(record.to_json(), ensure_ascii=False),
                    vec.tobytes(),
                    int(vec.shape[1]) if vec.ndim == 2 and vec.shape[0] else 0,
                    embedding_signature,
                    record.created_at,
                ),
            )

    def delete(self, plan_id):
        with self._lock:
            self._conn.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM plans")

    def size_bytes(self) -> Optional[int]:
        try:
            total = self.path.stat().st_size
            wal = self.path.with_name(self.path.name + "-wal")
            if wal.exists():
                total += wal.stat().st_size
            return total
        except OSError:
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
