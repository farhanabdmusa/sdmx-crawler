"""CLI: discover | scrape | run | status"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .db import Database, DatabaseError
from .discovery import Discovery
from .fetcher import EndpointProcessor
from .http_client import SdmxHttpClient
from .scraper import Scraper
from .storage import RawStore


def setup_logging(cfg: Config, level: str) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper()), format=fmt, handlers=[
        logging.StreamHandler(), logging.FileHandler(cfg.log_path, encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdmx_crawler", description="Crawler & scraper SDMX 3.0")
    p.add_argument("-c", "--config", help="Path file config JSON (default: bawaan IMF WEO)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="Fase 1: bangun katalog endpoint (SQLite + manifest NDJSON)")
    d.add_argument("--refresh", action="store_true",
                   help="Crawl ulang semua listing & GANTI seluruh katalog: endpoint lama dihapus, "
                        "semua endpoint baru berstatus pending (dibatalkan jika listing gagal)")
    d.add_argument("--purge-raw", action="store_true",
                   help="(dengan --refresh) hapus juga file mentah milik endpoint yang dihapus")
    s = sub.add_parser("scrape", help="Fase 2: ambil payload dari endpoint pending/failed")
    s.add_argument("--types", help="Filter resource_type, pisah koma (mis. data,dataflow)")
    s.add_argument("--limit", type=int, help="Batasi jumlah endpoint yang diproses")
    s.add_argument("--no-retry-failed", action="store_true", help="Hanya proses 'pending'")
    r = sub.add_parser("run", help="Jalankan Fase 1 lalu Fase 2")
    r.add_argument("--refresh", action="store_true", help="Sama seperti discover --refresh")
    r.add_argument("--purge-raw", action="store_true")
    st = sub.add_parser("status", help="Ringkasan status per resource_type")
    st.add_argument("--failed", action="store_true", help="Tampilkan juga daftar endpoint gagal + errornya")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.load(args.config)
        setup_logging(cfg, args.log_level)
        db = Database(cfg.db_path)

        if args.command == "status":
            rows = db.stats()
            if not rows:
                print("Katalog kosong. Jalankan: python -m sdmx_crawler discover")
            for r in rows:
                print(f"{r['resource_type']:<22} {r['status']:<8} {r['n']}")
            if args.failed:
                for ep in db.failed_endpoints():
                    print(f"\nFAILED {ep['url']}\n  {ep['last_error']}")
            return 0

        if args.command == "scrape" and not db.stats():
            print("Katalog kosong. Jalankan dulu: python -m sdmx_crawler discover", file=sys.stderr)
            return 2

        with SdmxHttpClient(cfg) as client:
            proc = EndpointProcessor(cfg, db, client, RawStore(cfg.raw_dir))
            if args.command in ("discover", "run"):
                if getattr(args, "purge_raw", False) and not args.refresh:
                    raise ValueError("--purge-raw hanya bisa dipakai bersama --refresh")
                summary = Discovery(cfg, db, proc).run(
                    refresh=args.refresh, purge_raw=getattr(args, "purge_raw", False))
                if summary.get("aborted") or summary.get("dataflow", 0) == 0:
                    return 2
            if args.command in ("scrape", "run"):
                types = args.types.split(",") if getattr(args, "types", None) else None
                res = Scraper(db, proc).run(
                    types=types, limit=getattr(args, "limit", None),
                    retry_failed=not getattr(args, "no_retry_failed", False))
                if res["interrupted"]:
                    return 130
                return 1 if res["failed"] else 0
        return 0
    except (ValueError, DatabaseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
