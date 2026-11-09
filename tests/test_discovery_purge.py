"""Regression test for raw-file purging on Python 3.8."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdmx_crawler.config import Config
from sdmx_crawler.discovery import Discovery

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config(output_dir=tmp)
    db_file = cfg.raw_dir / "data" / "agency" / "dataset" / "1.0" / "part.json"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("raw")
    meta_file = db_file.with_name(db_file.name + ".meta.json")
    meta_file.write_text("{}")
    outside_file = Path(tmp) / "outside.json"
    outside_file.write_text("keep")

    Discovery(cfg, None, None)._purge_raw([str(db_file), str(outside_file)])

    assert not db_file.exists()
    assert not meta_file.exists()
    assert outside_file.exists(), "purge must not remove files outside raw/"
    assert not db_file.parent.exists(), "empty raw directories should be removed"
    print("DISCOVERY PURGE TEST OK")
