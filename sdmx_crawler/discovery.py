"""FASE 1 - Discovery & Collection: membangun katalog endpoint.

Mode normal : tambah endpoint baru saja (idempotent, status lama dipertahankan).
Mode refresh: crawl ulang SEMUA listing, lalu GANTI seluruh katalog endpoint
              (endpoint lama dihapus, semua endpoint baru berstatus 'pending').
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Database, DatabaseError
from .fetcher import EndpointProcessor
from .urls import build_url

log = logging.getLogger(__name__)

# Kunci array di SDMX-JSON structure message untuk tiap tipe resource.
JSON_KEYS = {
    "dataflow": "dataflows", "datastructure": "dataStructures", "codelist": "codelists",
    "conceptscheme": "conceptSchemes", "agencyscheme": "agencySchemes",
    "categoryscheme": "categorySchemes", "categorisation": "categorisations",
    "dataconstraint": "dataConstraints", "valuelist": "valueLists",
}
_URN_RE = re.compile(r"=([^:()]+):([^()]+)\(([^)]+)\)")


def parse_urn(urn: str | None) -> tuple[str, str, str] | None:
    """'urn:...DataStructure=IMF.RES:DSD_WEO(1.0+.0)' -> ('IMF.RES','DSD_WEO','1.0+.0')"""
    m = _URN_RE.search(urn or "")
    return m.groups() if m else None  # type: ignore[return-value]


def extract_items(payload: dict, structure_type: str) -> list[dict]:
    data = payload.get("data", payload)
    items = data.get(JSON_KEYS[structure_type]) or []
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def _agency(item: dict, default: str) -> str:
    return item.get("agencyID") or item.get("agency") or default


class Discovery:
    def __init__(self, cfg: Config, db: Database, processor: EndpointProcessor):
        self.cfg, self.db, self.proc = cfg, db, processor
        self._fatal: list[tuple[str, str]] = []   # (tipe, error) listing gagal bukan-404

    # -- listing ----------------------------------------------------------
    def _list_url(self, stype: str) -> str:
        agency = "*" if stype == "agencyscheme" else self.cfg.agency_id
        params = {"detail": "full"} if stype in self.cfg.stub_list_types else None
        return build_url(self.cfg.base_url, ["structure", stype, agency, "*", "+"], params)

    def _load_list(self, stype: str, force: bool) -> list[dict] | None:
        """Ambil listing (dari server bila perlu). None = gagal."""
        url = self._list_url(stype)
        self.db.upsert_endpoint(url=url, resource_type=f"list_{stype}",
                                agency=self.cfg.agency_id, resource_id="_list", version="_",
                                params={"detail": "allstubs"} if stype in self.cfg.stub_list_types else {})
        ep = self.db.get_endpoint(url)
        assert ep is not None
        if ep["status"] != "success" or force or not ep["file_path"] or not Path(ep["file_path"]).exists():
            if not self.proc.process(ep):
                err = (self.db.get_endpoint(url) or {}).get("last_error") or ""
                log.warning("Listing '%s' gagal/tidak didukung; dilewati.", stype)
                if not err.startswith("HTTP 404"):   # 404 = tipe memang tidak didukung server
                    self._fatal.append((stype, err[:200]))
                return None
            ep = self.db.get_endpoint(url)
        try:
            payload = json.loads(Path(ep["file_path"]).read_text(encoding="utf-8"))
            return extract_items(payload, stype)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Tidak bisa mem-parse listing %s: %s", stype, exc)
            self._fatal.append((stype, f"parse error: {exc}"))
            return None

    # -- spesifikasi endpoint (belum ditulis ke DB) -----------------------
    def _item_specs(self, stype: str, item: dict, list_url: str) -> list[dict]:
        agency, rid, ver = _agency(item, self.cfg.agency_id), item["id"], item.get("version", "+")
        meta = {"source_list": list_url, "item": item}
        common = dict(agency=agency, resource_id=rid, version=ver, metadata=meta)
        params = {"references": "descendants"} if stype == "dataflow" else \
                 {"references": "children"} if stype == "datastructure" else {}
        specs = [dict(url=build_url(self.cfg.base_url, ["structure", stype, agency, rid, ver], params),
                      resource_type=stype, params=params, **common)]
        if stype == "dataflow":
            for part in self.cfg.data_partitions:
                pparams = part.get("params", {})
                specs.append(dict(
                    url=build_url(self.cfg.base_url,
                                  ["data", "dataflow", agency, rid, ver, self.cfg.data_key], pparams)
                                  .replace("[", "%5B").replace("]", "%5D"),
                    resource_type="data", partition=part.get("name", "all"), params=pparams, **common))
            if self.cfg.include_availability:
                specs.append(dict(
                    url=build_url(self.cfg.base_url,
                                  ["availability", "dataflow", agency, rid, ver, self.cfg.data_key, "*"]),
                    resource_type="availability", **common))
        return specs

    # -- entry point ------------------------------------------------------
    def run(self, refresh: bool = False, purge_raw: bool = False) -> dict:
        self._fatal = []
        types = sorted(set(self.cfg.structure_types), key=lambda t: (t != "dataflow", t))
        if "dataflow" not in types:
            types.insert(0, "dataflow")  # dataflow wajib: dasar penentu endpoint data
        wanted_flows = set(self.cfg.dataflow_ids)
        needed_dsds: set[tuple[str, str]] = set()
        specs: list[dict] = []
        list_urls: set[str] = set()
        summary: dict = {}

        for stype in types:
            items = self._load_list(stype, refresh) or []
            list_urls.add(self._list_url(stype))
            if stype == "dataflow":
                if wanted_flows:
                    items = [i for i in items if i["id"] in wanted_flows]
                    missing = wanted_flows - {i["id"] for i in items}
                    if missing:
                        log.warning("Dataflow tidak ditemukan di listing: %s", sorted(missing))
                for i in items:
                    ref = parse_urn(i.get("structure"))
                    if ref:
                        needed_dsds.add((ref[0], ref[1]))
            elif stype == "datastructure" and wanted_flows:
                items = [i for i in items if (_agency(i, self.cfg.agency_id), i["id"]) in needed_dsds]

            if stype in self.cfg.detail_types:
                list_url = self._list_url(stype)
                for i in items:
                    specs += self._item_specs(stype, i, list_url)
            summary[stype] = len(items)
            log.info("Discovery %-15s item=%d", stype, len(items))

        if summary.get("dataflow", 0) == 0:
            log.error("Tidak ada dataflow ditemukan. Periksa base_url/agency_id/dataflow_ids.")

        if refresh:
            fatal = [(t, e) for t, e in self._fatal if t == "dataflow" or t in self.cfg.detail_types]
            if fatal or summary.get("dataflow", 0) == 0:
                for t, e in fatal:
                    log.error("Listing '%s' gagal: %s", t, e)
                log.error("REFRESH DIBATALKAN: katalog lama TIDAK diubah. Perbaiki masalahnya, lalu ulangi.")
                summary["aborted"] = True
                return summary
            self._backup_manifest()
            self._backup_state()
            result = self.db.replace_catalog(list_urls, specs)
            log.info("Refresh: %d endpoint lama dihapus, %d endpoint baru dibuat (semua 'pending').",
                     result["removed"], result["created"])
            if purge_raw:
                self._purge_raw(result["removed_files"])
        else:
            added = sum(self.db.upsert_endpoint(**s) for s in specs)
            log.info("Endpoint baru ditambahkan: %d (dari %d kandidat)", added, len(specs))

        n = self.export_manifest()
        log.info("Manifest ditulis: %s (%d baris)", self.cfg.manifest_path, n)
        return summary

    # -- refresh helpers --------------------------------------------------
    def _backup_manifest(self) -> None:
        mp = self.cfg.manifest_path
        if mp.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = mp.with_name(f"manifest.{stamp}.ndjson")
            try:
                shutil.copy2(mp, backup)
                log.info("Manifest lama dicadangkan: %s", backup)
            except OSError as exc:
                log.warning("Gagal mencadangkan manifest lama: %s", exc)

    def _backup_state(self) -> None:
        """Cadangkan state.sqlite (termasuk scraping_logs) sebelum katalog diganti."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.cfg.db_path.with_name(f"state.{stamp}.sqlite")
        try:
            self.db.backup(dest)
            log.info("State lama dicadangkan: %s", dest)
        except (DatabaseError, OSError) as exc:
            log.warning("Gagal mencadangkan state lama: %s", exc)

    def _purge_raw(self, files: list[str]) -> None:
        """Hapus file mentah milik endpoint yang dihapus (hanya di dalam folder raw/)."""
        root = self.cfg.raw_dir.resolve()
        removed = 0
        for f in files:
            p = Path(f).resolve()
            try:
                p.relative_to(root)
            except ValueError:
                continue
            for target in (p, p.with_name(p.name + ".meta.json")):
                try:
                    if target.exists():
                        target.unlink()
                        removed += 1
                except OSError as exc:
                    log.warning("Gagal menghapus %s: %s", target, exc)
            d = p.parent
            while d != root:
                try:
                    d.relative_to(root)
                except ValueError:
                    break
                try:
                    d.rmdir()          # hanya berhasil jika folder sudah kosong
                except OSError:
                    break
                d = d.parent
        log.info("Purge raw: %d file dihapus.", removed)

    def export_manifest(self) -> int:
        """Tulis seluruh katalog ke NDJSON (satu endpoint per baris)."""
        self.cfg.catalog_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.manifest_path.with_suffix(".tmp")
        count = 0
        with open(tmp, "w", encoding="utf-8") as fh:
            for ep in self.db.iter_endpoints():
                for k in ("params", "metadata"):
                    ep[k] = json.loads(ep[k]) if ep[k] else None
                fh.write(json.dumps(ep, ensure_ascii=False) + "\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.cfg.manifest_path)
        return count
