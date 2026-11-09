"""Satu unit kerja: ambil endpoint -> simpan file -> update status & log di SQLite."""
from __future__ import annotations

import logging
import time

from .config import Config
from .db import Database, DatabaseError, utcnow
from .http_client import HttpFetchError, SdmxHttpClient
from .storage import RawStore

log = logging.getLogger(__name__)


class EndpointProcessor:
    def __init__(self, cfg: Config, db: Database, client: SdmxHttpClient, store: RawStore):
        self.cfg, self.db, self.client, self.store = cfg, db, client, store

    def _accept_for(self, resource_type: str) -> str:
        return self.cfg.data_accept if resource_type == "data" else self.cfg.structure_accept

    def process(self, ep: dict) -> bool:
        """Mengembalikan True jika sukses. Tidak pernah melempar error jaringan/IO;
        semuanya dicatat sebagai `failed` agar run berikutnya bisa mencoba lagi."""
        started, t0 = utcnow(), time.monotonic()
        http_status = size = attempts = None
        error: str | None = None
        path = None
        try:
            result = self.client.get(ep["url"], self._accept_for(ep["resource_type"]))
            http_status, attempts = result.status_code, result.attempts
            path, size = self.store.save(ep, result)
        except HttpFetchError as exc:
            http_status, attempts, error = exc.status_code, exc.attempts, str(exc)
        except OSError as exc:
            error = f"Gagal menulis file: {exc}"
        except Exception as exc:  # jaring pengaman terakhir; jangan matikan seluruh run
            error = f"Unexpected {type(exc).__name__}: {exc}"
            log.exception("Error tak terduga pada %s", ep["url"])

        duration = int((time.monotonic() - t0) * 1000)
        try:
            if error is None:
                self.db.mark_success(ep["id"], str(path))
            else:
                self.db.mark_failed(ep["id"], error)
            self.db.add_log(endpoint_id=ep["id"], started_at=started, finished_at=utcnow(),
                            duration_ms=duration, http_status=http_status, file_size=size,
                            attempts=attempts, error_message=error)
        except DatabaseError as exc:
            log.error("Gagal mencatat status DB untuk %s: %s", ep["url"], exc)
            return False

        if error is None:
            log.info("OK   [%s] %s (%d bytes, %d ms)", ep["resource_type"], ep["url"], size, duration)
        else:
            log.error("FAIL [%s] %s -> %s", ep["resource_type"], ep["url"], error)
        return error is None
