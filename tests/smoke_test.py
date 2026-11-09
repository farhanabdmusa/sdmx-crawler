"""Uji end-to-end offline memakai httpx.MockTransport (tanpa jaringan)."""
import dataclasses, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sdmx_crawler.config import Config
from sdmx_crawler.db import Database
from sdmx_crawler.discovery import Discovery
from sdmx_crawler.fetcher import EndpointProcessor
from sdmx_crawler.http_client import SdmxHttpClient
from sdmx_crawler.scraper import Scraper
from sdmx_crawler.storage import RawStore

calls = {"data": 0, "break_flow_list": False}
FLOW = {"id": "WEO", "agencyID": "IMF.RES", "version": "6.0.0", "name": "WEO",
        "structure": "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=IMF.RES:DSD_WEO(6.0.0)"}
OTHER_FLOW = {"id": "OTHER", "agencyID": "OTHER.AGENCY", "version": "1.0", "name": "Other",
              "structure": "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=OTHER.AGENCY:DSD_OTHER(1.0)"}

def handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    js = {"content-type": "application/vnd.sdmx.structure+json;version=2.0.0"}
    if "/structure/dataflow/*/*/+" in p:
        if calls["break_flow_list"]:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"data": {"dataflows": [FLOW, OTHER_FLOW]}}, headers=js)
    if "/structure/datastructure/*/*/+" in p:
        return httpx.Response(200, json={"data": {"dataStructures": [
            {"id": "DSD_WEO", "agencyID": "IMF.RES", "version": "6.0.0"},
            {"id": "DSD_OTHER", "agencyID": "OTHER.AGENCY", "version": "1.0"}]}}, headers=js)
    if "/structure/codelist/*/*/+" in p:
        return httpx.Response(200, json={"data": {"codelists": [{"id": "CL_AREA", "agencyID": "IMF.RES", "version": "1.0"}]}}, headers=js)
    if p.startswith("/3.0/data/"):
        calls["data"] += 1
        if calls["data"] == 1:
            return httpx.Response(503, text="busy", headers={"retry-after": "0"})  # diuji retry
        return httpx.Response(200, json={"data": {"dataSets": []}}, headers={"content-type": "application/vnd.sdmx.data+json"})
    if "/structure/" in p and "/*/+" in p:
        return httpx.Response(404, text="unsupported")
    if p.startswith("/3.0/availability/"):
        return httpx.Response(404, text="no results")
    return httpx.Response(200, json={"data": {}}, headers=js)

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config(base_url="https://mock.test/3.0", output_dir=tmp, request_delay=0, backoff_base=0.01)
    assert cfg.agency_id == "*" and cfg.dataflow_ids == [], "default config harus mengambil semua agency dan dataflow"
    db = Database(cfg.db_path)
    with SdmxHttpClient(cfg, transport=httpx.MockTransport(handler)) as client:
        proc = EndpointProcessor(cfg, db, client, RawStore(cfg.raw_dir))

        # ---------- Fase 1 + Fase 2 ----------
        summary = Discovery(cfg, db, proc).run()
        assert summary["dataflow"] == 2, "semua dataflow harus ditemukan"
        data_endpoints = [e for e in db.iter_endpoints() if e["resource_type"] == "data"]
        assert len(data_endpoints) == 2
        assert {e["agency"] for e in data_endpoints} == {"IMF.RES", "OTHER.AGENCY"}
        res = Scraper(db, proc).run()
        assert res["success"] >= 2 and res["failed"] >= 1, res   # dataflow lintas agency + listing/availability 404
        assert all(e["status"] == "success" for e in db.iter_endpoints() if e["resource_type"] == "data")
        assert Scraper(db, proc).run(retry_failed=False) == {"success": 0, "failed": 0, "interrupted": False}
        assert Database(cfg.db_path).stats() == db.stats(), "status harus persisten di SQLite"
        with db.connection() as c:
            assert c.execute("SELECT COUNT(*) FROM scraping_logs").fetchone()[0] >= 10

        # ---------- REFRESH ----------
        urls = lambda t: {e["url"] for e in db.iter_endpoints() if e["resource_type"] == t}
        non_list = lambda: [(e["url"], e["status"]) for e in db.iter_endpoints() if not e["resource_type"].startswith("list_")]
        old_data = urls("data")
        assert len(old_data) == 2 and any(Path(tmp, "raw", "data").rglob("*.json"))
        cfg2 = dataclasses.replace(cfg, data_partitions=[
            {"name": "p1", "params": {"c[TIME_PERIOD]": "ge:2000+le:2009"}},
            {"name": "p2", "params": {"c[TIME_PERIOD]": "ge:2010+le:2020"}}])
        proc2 = EndpointProcessor(cfg2, db, client, RawStore(cfg2.raw_dir))

        # (a) listing dataflow gagal -> refresh dibatalkan, katalog lama utuh
        before = non_list()
        calls["break_flow_list"] = True
        assert Discovery(cfg2, db, proc2).run(refresh=True).get("aborted")
        assert non_list() == before, "katalog lama harus utuh saat refresh dibatalkan"
        calls["break_flow_list"] = False

        # (b) refresh normal
        r = Discovery(cfg2, db, proc2).run(refresh=True, purge_raw=True)
        assert not r.get("aborted"), r
        assert len(urls("data")) == 4 and not (urls("data") & old_data)
        assert all(s == "pending" for _, s in non_list()), "semua endpoint baru harus pending"
        assert not any(Path(tmp, "raw", "data").rglob("*.json")), "purge-raw harus menghapus file lama"
        assert list(cfg.catalog_dir.glob("manifest.2*.ndjson")), "manifest lama harus dicadangkan"
        backups = list(Path(tmp).glob("state.2*.sqlite"))
        assert backups, "state.sqlite lama harus dicadangkan"
        old = Database(backups[0])                      # cadangan berisi riwayat lama utuh
        assert any(e["resource_type"] == "data" and e["status"] == "success" for e in old.iter_endpoints())
        with db.connection() as c:                      # tidak ada log yatim
            assert c.execute("SELECT COUNT(*) FROM scraping_logs l LEFT JOIN endpoints e ON e.id=l.endpoint_id WHERE e.id IS NULL").fetchone()[0] == 0
        assert {e["status"] for e in db.iter_endpoints() if e["resource_type"] == "list_dataflow"} == {"success"}

        # (c) scrape setelah refresh mengambil semua endpoint baru
        assert Scraper(db, proc2).run(types=["data"])["success"] == 4

    print(*[f"{r['resource_type']:<20}{r['status']:<8}{r['n']}" for r in db.stats()], sep="\n")
    assert cfg.manifest_path.read_text().count("\n") == sum(r["n"] for r in db.stats())
    print("SMOKE TEST OK")
