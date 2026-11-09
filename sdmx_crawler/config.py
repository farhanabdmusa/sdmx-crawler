"""Konfigurasi terpusat. Semua nilai punya default (contoh: IMF WEO) dan bisa
ditimpa lewat file JSON (lihat config.example.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+json;version=2.0.0"
DATA_ACCEPT = "application/vnd.sdmx.data+json;version=2.0.0"


@dataclass
class Config:
    # --- Target API -------------------------------------------------------
    base_url: str = "https://api.imf.org/external/sdmx/3.0"
    agency_id: str = "*"
    dataflow_ids: list[str] = field(default_factory=lambda: [])
    structure_types: list[str] = field(default_factory=lambda: [
        "dataflow", "datastructure", "codelist", "conceptscheme",
        "agencyscheme", "categoryscheme", "categorisation", "dataconstraint",
    ])
    # Tipe yang di-list sebagai stub (ringan); isi lengkap diambil per item.
    stub_list_types: list[str] = field(default_factory=lambda: [
        "dataflow", "datastructure", "codelist", "conceptscheme",
    ])
    # Tipe yang didaftarkan endpoint detail per item.
    detail_types: list[str] = field(default_factory=lambda: [
        "dataflow", "datastructure", "codelist", "conceptscheme",
    ])
    data_key: str = "*"
    # Satu endpoint data per partisi; berguna untuk memecah dataset besar,
    # mis. {"name": "2000-2009", "params": {"c[TIME_PERIOD]": "ge:2000+le:2009"}}
    data_partitions: list[dict[str, Any]] = field(default_factory=lambda: [
        {"name": "all", "params": {"attributes": "dsd", "measures": "all"}}
    ])
    include_availability: bool = True
    structure_accept: str = STRUCTURE_ACCEPT
    data_accept: str = DATA_ACCEPT

    # --- HTTP -------------------------------------------------------------
    connect_timeout: float = 15.0
    read_timeout: float = 120.0
    max_retries: int = 5            # retry per request (di luar percobaan pertama)
    backoff_base: float = 1.5       # detik; delay = base * 2**(n-1) + jitter
    backoff_max: float = 60.0
    request_delay: float = 0.5      # jeda minimum antar request (sopan ke server)
    user_agent: str = "sdmx-crawler/1.0 (+research)"

    # --- Penyimpanan ------------------------------------------------------
    output_dir: str = "output"

    @property
    def raw_dir(self) -> Path:
        return Path(self.output_dir) / "raw"

    @property
    def catalog_dir(self) -> Path:
        return Path(self.output_dir) / "catalog"

    @property
    def manifest_path(self) -> Path:
        return self.catalog_dir / "manifest.ndjson"

    @property
    def db_path(self) -> Path:
        return Path(self.output_dir) / "state.sqlite"

    @property
    def log_path(self) -> Path:
        return Path(self.output_dir) / "logs" / "crawler.log"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        if not path:
            return cls()
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Gagal membaca config '{path}': {exc}") from exc
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Kunci config tidak dikenal: {sorted(unknown)}")
        cfg = cls(**raw)
        cfg.base_url = cfg.base_url.rstrip("/")
        return cfg
