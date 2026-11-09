"""Penyimpanan respons mentah + sidecar metadata (penulisan atomik)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .db import utcnow
from .http_client import FetchResult


def safe_segment(value: str | None) -> str:
    if not value:
        return "_"
    value = value.replace("*", "all").replace("+", "plus")
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:100] or "_"


def extension_for(content_type: str) -> str:
    ct = content_type.lower()
    if "json" in ct:
        return "json"
    if "xml" in ct:
        return "xml"
    if "csv" in ct:
        return "csv"
    return "txt"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class RawStore:
    """Struktur: raw/<resource_type>/<agency>/<id>/<version>/<partition>.<ext>
    plus <partition>.<ext>.meta.json berisi URL & metadata respons."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, endpoint: dict, result: FetchResult) -> tuple[Path, int]:
        folder = (self.root / safe_segment(endpoint["resource_type"]) / safe_segment(endpoint["agency"])
                  / safe_segment(endpoint["resource_id"]) / safe_segment(endpoint["version"]))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe_segment(endpoint['partition'])}.{extension_for(result.content_type)}"
        _atomic_write(path, result.content)
        meta = {
            "url": endpoint["url"], "final_url": result.url,
            "resource_type": endpoint["resource_type"], "agency": endpoint["agency"],
            "resource_id": endpoint["resource_id"], "version": endpoint["version"],
            "partition": endpoint["partition"], "http_status": result.status_code,
            "content_type": result.content_type, "size_bytes": len(result.content),
            "sha256": hashlib.sha256(result.content).hexdigest(),
            "fetched_at": utcnow(), "http_attempts": result.attempts,
            "response_headers": result.headers,
        }
        _atomic_write(path.with_name(path.name + ".meta.json"),
                      json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        return path, len(result.content)
