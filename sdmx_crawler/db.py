"""Lapisan SQLite: tabel `endpoints` (katalog + status) dan `scraping_logs`."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,          -- dataflow | datastructure | data | list_* | ...
    agency        TEXT,
    resource_id   TEXT,
    version       TEXT,
    partition     TEXT NOT NULL DEFAULT 'default',
    params        TEXT,                   -- JSON
    metadata      TEXT,                   -- JSON metadata mentah dari hasil discovery
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','success','failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    file_path     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_endpoints_status ON endpoints(status, resource_type);

CREATE TABLE IF NOT EXISTS scraping_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER NOT NULL REFERENCES endpoints(id),
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    duration_ms   INTEGER,
    http_status   INTEGER,
    file_size     INTEGER,
    attempts      INTEGER,                -- jumlah percobaan HTTP dalam eksekusi ini
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_endpoint ON scraping_logs(endpoint_id);
"""


class DatabaseError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Koneksi aman: commit jika sukses, rollback jika error, selalu close."""
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn:
                conn.rollback()
            raise DatabaseError(f"SQLite error: {exc}") from exc
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def backup(self, dest: Path) -> None:
        """Salin konsisten seluruh database ke file lain (memakai API backup SQLite)."""
        with self.connection() as src:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()

    # ---- endpoints -------------------------------------------------------
    @staticmethod
    def _insert(conn: sqlite3.Connection, now: str, *, url: str, resource_type: str,
                agency: str | None = None, resource_id: str | None = None,
                version: str | None = None, partition: str = "default",
                params: dict | None = None, metadata: dict | None = None) -> bool:
        cur = conn.execute(
            """INSERT OR IGNORE INTO endpoints
               (url, resource_type, agency, resource_id, version, partition,
                params, metadata, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
            (url, resource_type, agency, resource_id, version, partition,
             json.dumps(params or {}), json.dumps(metadata) if metadata else None, now, now))
        return cur.rowcount == 1

    def upsert_endpoint(self, **spec: Any) -> bool:
        """INSERT OR IGNORE: aman dijalankan ulang, status lama tidak tertimpa."""
        with self.connection() as conn:
            return self._insert(conn, utcnow(), **spec)

    def replace_catalog(self, keep_urls: set[str], specs: list[dict]) -> dict:
        """Ganti katalog secara ATOMIK (satu transaksi): hapus semua endpoint selain
        `keep_urls` beserta log-nya, lalu buat `specs` sebagai 'pending'.
        Jika gagal di tengah, rollback otomatis dan katalog lama utuh."""
        now = utcnow()
        with self.connection() as conn:
            rows = conn.execute("SELECT id, url, file_path FROM endpoints").fetchall()
            doomed = [r for r in rows if r["url"] not in keep_urls]
            conn.executemany("DELETE FROM scraping_logs WHERE endpoint_id = ?", [(r["id"],) for r in doomed])
            conn.executemany("DELETE FROM endpoints WHERE id = ?", [(r["id"],) for r in doomed])
            created = sum(self._insert(conn, now, **spec) for spec in specs)
        return {"removed": len(doomed), "created": created,
                "removed_files": [r["file_path"] for r in doomed if r["file_path"]]}

    def get_endpoint(self, url: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM endpoints WHERE url = ?", (url,)).fetchone()
            return dict(row) if row else None

    def fetch_runnable(self, *, after_id: int, statuses: tuple[str, ...],
                       types: list[str] | None, limit: int) -> list[dict]:
        sql = f"SELECT * FROM endpoints WHERE id > ? AND status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = [after_id, *statuses]
        if types:
            sql += f" AND resource_type IN ({','.join('?' * len(types))})"
            args += types
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def mark_success(self, endpoint_id: int, file_path: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE endpoints SET status='success', attempts=attempts+1,
                   last_error=NULL, file_path=?, updated_at=? WHERE id=?""",
                (file_path, utcnow(), endpoint_id))

    def mark_failed(self, endpoint_id: int, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE endpoints SET status='failed', attempts=attempts+1,
                   last_error=?, updated_at=? WHERE id=?""",
                (error[:2000], utcnow(), endpoint_id))

    def add_log(self, *, endpoint_id: int, started_at: str, finished_at: str,
                duration_ms: int, http_status: int | None, file_size: int | None,
                attempts: int | None, error_message: str | None) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO scraping_logs (endpoint_id, started_at, finished_at,
                   duration_ms, http_status, file_size, attempts, error_message)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (endpoint_id, started_at, finished_at, duration_ms, http_status,
                 file_size, attempts, (error_message or None) and error_message[:2000]))

    def iter_endpoints(self) -> Iterator[dict]:
        with self.connection() as conn:
            for row in conn.execute("SELECT * FROM endpoints ORDER BY id"):
                yield dict(row)

    def failed_endpoints(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT url, last_error FROM endpoints WHERE status='failed' ORDER BY id")]

    def stats(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT resource_type, status, COUNT(*) AS n FROM endpoints
                   GROUP BY resource_type, status ORDER BY resource_type, status""")]
