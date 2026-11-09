"""FASE 2 - Scraping inkremental dengan checkpoint di SQLite."""
from __future__ import annotations

import logging
import signal
import threading

from .db import Database
from .fetcher import EndpointProcessor

log = logging.getLogger(__name__)
BATCH = 200


class Scraper:
    def __init__(self, db: Database, processor: EndpointProcessor):
        self.db, self.proc = db, processor
        self._stop = threading.Event()

    def _install_signals(self) -> dict:
        previous = {}
        if threading.current_thread() is not threading.main_thread():
            return previous
        def handler(signum, _frame):
            log.warning("Sinyal %s diterima; berhenti setelah item berjalan selesai...", signum)
            self._stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, handler)
        return previous

    def run(self, types: list[str] | None = None, limit: int | None = None,
            retry_failed: bool = True) -> dict:
        """Proses endpoint berstatus pending (+ failed jika retry_failed).
        Status disimpan per item, jadi run ulang otomatis melanjutkan sisanya."""
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        previous = self._install_signals()
        ok = fail = 0
        last_id = 0
        try:
            while not self._stop.is_set():
                batch = self.db.fetch_runnable(after_id=last_id, statuses=statuses, types=types,
                                               limit=BATCH if limit is None else min(BATCH, limit - ok - fail))
                if not batch:
                    break
                for ep in batch:
                    if self._stop.is_set() or (limit is not None and ok + fail >= limit):
                        break
                    last_id = ep["id"]
                    if self.proc.process(ep):
                        ok += 1
                    else:
                        fail += 1
                if limit is not None and ok + fail >= limit:
                    break
        finally:
            for sig, h in previous.items():
                signal.signal(sig, h)
        interrupted = self._stop.is_set()
        log.info("Scraping selesai%s: sukses=%d gagal=%d", " (dihentikan manual)" if interrupted else "", ok, fail)
        return {"success": ok, "failed": fail, "interrupted": interrupted}
