#!/usr/bin/env python3
"""Konversi respons DATA SDMX-JSON (hasil Fase 2) menjadi CSV dan/atau XLSX.

Input : output/raw/data/<agency>/<dataflow>/<versi>/<partisi>.json
Output: output/export/<agency>_<dataflow>_<versi>.csv / .xlsx (semua partisi digabung)

Mode:
  long (default): satu baris per observasi  -> COUNTRY, INDICATOR, TIME_PERIOD, OBS_VALUE, ...
  --wide        : satu baris per series, kolom = periode waktu (butuh pandas)

Hanya membaca JSON (SDMX-JSON 2.0, dan fallback untuk 1.0). File CSV/XML dilewati.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("export_data")

XLSX_MAX_ROWS = 1_048_576  # batas baris Excel per sheet (termasuk header)
_ILLEGAL_XML = re.compile(r"[\000-\010\013\014\016-\037]")


# --------------------------------------------------------------------------
# Parsing SDMX-JSON
# --------------------------------------------------------------------------
def _label(value: Any) -> Any:
    """Nama bisa string atau dict multi-bahasa {"en": "..."}."""
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), None)
    return value


def _attr_value(defn: dict, raw: Any) -> Any:
    """Atribut disimpan sebagai indeks ke defn['values'] (jika ada), selain itu nilai langsung."""
    if raw is None:
        return None
    values = defn.get("values")
    if values is not None and isinstance(raw, int) and not isinstance(raw, bool):
        try:
            item = values[raw]
        except IndexError:
            return raw
        return item.get("id", _label(item.get("name"))) if isinstance(item, dict) else item
    return raw


@dataclass
class Layout:
    columns: list[str]
    dim_cols: list[str]          # semua kolom hasil dimensi (kode + nama)
    time_col: str | None
    value_col: str


_WARNED_DIMS: set[str] = set()
_INT_RE = re.compile(r"^-?\d+$")


def _code(item: dict) -> Any:
    """Kode nilai dimensi. Nilai ber-kode memakai 'id'; dimensi tak ber-kode (mis. TIME_PERIOD)
    di SDMX-JSON bisa memakai 'value', atau hanya 'name'/'start'."""
    for key in ("id", "value"):
        if item.get(key) is not None:
            return item[key]
    name = _label(item.get("name"))
    return name if name is not None else item.get("start")


def _urn_parts(reference: Any) -> tuple[str, str, str | None] | None:
    if not isinstance(reference, str) or "=" not in reference:
        return None
    body = reference.split("=", 1)[1]
    resource, separator, version_and_child = body.partition("(")
    agency, separator, resource_id = resource.partition(":")
    if not separator or not resource_id:
        return None
    _, closing, child = version_and_child.partition(")")
    if not closing:
        return None
    return agency, resource_id, child[1:] if child.startswith(".") else None


def _dimension_labels(structure: dict) -> dict[str, dict[str, Any]]:
    """Resolve dimension labels through the DSD concept's codelist representation."""
    data = structure.get("data", structure)
    codelists = {
        (item.get("agencyID"), item.get("id")): item
        for item in data.get("codelists") or []
    }
    concepts = {
        (scheme.get("agencyID"), scheme.get("id"), concept.get("id")): concept
        for scheme in data.get("conceptSchemes") or []
        for concept in scheme.get("concepts") or []
    }
    labels: dict[str, dict[str, Any]] = {}
    for data_structure in data.get("dataStructures") or []:
        structure_components = data_structure.get("dataStructureComponents") or {}
        dimensions = structure_components.get("dimensionList") or {}
        dimension_items = (dimensions.get("dimensions") or []) + (dimensions.get("timeDimensions") or [])
        for component in dimension_items:
            representation = component.get("localRepresentation") or {}
            enumeration = representation.get("enumeration")
            if not enumeration:
                concept_ref = _urn_parts(component.get("conceptIdentity"))
                if concept_ref and concept_ref[2]:
                    concept = concepts.get(concept_ref)
                    enumeration = ((concept or {}).get("coreRepresentation") or {}).get("enumeration")
            codelist_ref = _urn_parts(enumeration)
            codelist = codelists.get(codelist_ref[:2]) if codelist_ref else None
            if not codelist:
                continue
            code_labels: dict[str, Any] = {}
            for code in codelist.get("codes") or []:
                label = _label(code.get("name")) or _label(code.get("names"))
                if label is not None and code.get("id") is not None:
                    code_labels[str(code["id"])] = label
            if code_labels:
                labels[component["id"]] = code_labels
    return labels


def _num(v: Any) -> Any:
    """Nilai pengukuran sering berupa string ("3.23"); ubah ke angka bila memungkinkan."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if _INT_RE.match(s):
        return int(s)
    try:
        f = float(s)
    except ValueError:
        return v
    return f if f == f and f not in (float("inf"), float("-inf")) else v


def _fill_dim(row: dict, dim: dict, idx: int, labels: bool,
              label_maps: dict[str, dict[str, Any]] | None = None) -> None:
    values = dim.get("values") or []
    item = values[idx] if 0 <= idx < len(values) else {}
    code = _code(item) if isinstance(item, dict) else item
    if code is None and dim["id"] not in _WARNED_DIMS:
        _WARNED_DIMS.add(dim["id"])
        log.warning("Dimensi %s: nilai indeks %s tidak terbaca (item=%s). Jalankan "
                    "`python -m sdmx_crawler.export_data --inspect` dan kirim hasilnya.",
                    dim["id"], idx, json.dumps(item, ensure_ascii=False)[:200])
    row[dim["id"]] = code
    if labels:
        name = _label(item.get("name")) if isinstance(item, dict) else None
        mapped = (label_maps or {}).get(dim["id"], {}).get(str(code))
        label = name if name is not None else mapped
        row[dim["id"] + "_name"] = label if label is not None else code


def _components(structure: dict) -> tuple[list, list, list, list, list, list]:
    dims = structure.get("dimensions") or {}
    attrs = structure.get("attributes") or {}
    measures = (structure.get("measures") or {}).get("observation") or [{"id": "OBS_VALUE"}]
    return (dims.get("series") or [], dims.get("observation") or [],
            attrs.get("dataSet") or [], attrs.get("series") or [],
            attrs.get("observation") or [], measures)


def build_layout(structure: dict, labels: bool, add_source: bool) -> Layout:
    sdims, odims, a_ds, a_s, a_o, measures = _components(structure)
    dim_cols: list[str] = []
    for d in sdims + odims:
        dim_cols.append(d["id"])
        if labels:
            dim_cols.append(d["id"] + "_name")
    all_ids = [d["id"] for d in sdims + odims]
    time_col = "TIME_PERIOD" if "TIME_PERIOD" in all_ids else (odims[0]["id"] if odims else None)
    measure_ids = [m["id"] for m in measures]
    columns = list(dim_cols) + measure_ids
    for defn in a_ds + a_s + a_o:
        if defn["id"] not in columns:
            columns.append(defn["id"])
    if add_source:
        columns.append("source_file")
    return Layout(columns, dim_cols, time_col, measure_ids[0])


def iter_rows(dataset: dict, structure: dict, labels: bool,
              label_maps: dict[str, dict[str, Any]] | None = None) -> Iterator[dict]:
    sdims, odims, a_ds, a_s, a_o, measures = _components(structure)
    n_meas = len(measures)

    ds_level: dict[str, Any] = {}
    for defn, raw in zip(a_ds, dataset.get("attributes") or []):
        ds_level[defn["id"]] = _attr_value(defn, raw)

    def emit_obs(row: dict, obs: Any) -> None:
        arr = obs if isinstance(obs, list) else [obs]
        for m, v in zip(measures, arr[:n_meas]):
            row[m["id"]] = _num(v)
        for defn, raw in zip(a_o, arr[n_meas:]):
            row[defn["id"]] = _attr_value(defn, raw)

    # Bentuk 1: observasi bersarang di bawah series
    for skey, series in (dataset.get("series") or {}).items():
        base = dict(ds_level)
        for dim, i in zip(sdims, (int(x) for x in skey.split(":")) if skey else []):
            _fill_dim(base, dim, i, labels, label_maps)
        for defn, raw in zip(a_s, series.get("attributes") or []):
            base[defn["id"]] = _attr_value(defn, raw)
        for okey, obs in (series.get("observations") or {}).items():
            row = dict(base)
            for dim, i in zip(odims, (int(x) for x in okey.split(":"))):
                _fill_dim(row, dim, i, labels, label_maps)
            emit_obs(row, obs)
            yield row

    # Bentuk 2: dimensionAtObservation=AllDimensions (observasi langsung di dataset)
    for okey, obs in (dataset.get("observations") or {}).items():
        row = dict(ds_level)
        for dim, i in zip(sdims + odims, (int(x) for x in okey.split(":"))):
            _fill_dim(row, dim, i, labels, label_maps)
        emit_obs(row, obs)
        yield row


def parse_message(message: dict, labels: bool, add_source: bool,
                  label_maps: dict[str, dict[str, Any]] | None = None) -> tuple[Layout | None, Iterator[dict]]:
    data = message.get("data", message)
    structures = data.get("structures") or ([data["structure"]] if data.get("structure") else [])
    datasets = data.get("dataSets") or []
    if not datasets or not structures:
        return None, iter(())

    def gen() -> Iterator[dict]:
        for ds in datasets:
            st = structures[ds.get("structure", 0)] if len(structures) > 1 else structures[0]
            yield from iter_rows(ds, st, labels, label_maps)

    first_st = structures[datasets[0].get("structure", 0)] if len(structures) > 1 else structures[0]
    return build_layout(first_st, labels, add_source), gen()


# --------------------------------------------------------------------------
# Sinks (penulis output)
# --------------------------------------------------------------------------
class CsvSink:
    def __init__(self, path: Path, layout: Layout):
        self.path = path
        self._fh = open(path, "w", newline="", encoding="utf-8-sig")  # BOM: nyaman dibuka di Excel
        self._w = csv.DictWriter(self._fh, fieldnames=layout.columns, extrasaction="ignore", restval="")
        self._w.writeheader()

    def write(self, row: dict) -> None:
        self._w.writerow(row)

    def close(self, sources: list[dict]) -> None:
        self._fh.close()


class XlsxSink:
    """Mode write_only openpyxl (hemat memori); otomatis pindah sheet jika > batas baris Excel."""

    def __init__(self, path: Path, layout: Layout):
        try:
            from openpyxl import Workbook
        except ImportError:
            raise SystemExit("openpyxl belum terpasang: pip install openpyxl")
        self.path, self.layout = path, layout
        self._wb = Workbook(write_only=True)
        self._sheet_no, self._rows, self._ws = 0, 0, None
        self._new_sheet()

    def _new_sheet(self) -> None:
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
        self._sheet_no += 1
        self._ws = self._wb.create_sheet("data" if self._sheet_no == 1 else f"data_{self._sheet_no}")
        self._ws.freeze_panes = "A2"
        header = []
        for name in self.layout.columns:
            cell = WriteOnlyCell(self._ws, value=name)
            cell.font = Font(name="Arial", bold=True)
            header.append(cell)
        self._ws.append(header)
        self._rows = 1

    @staticmethod
    def _clean(v: Any) -> Any:
        if isinstance(v, str):
            return _ILLEGAL_XML.sub("", v)[:32767]
        return v

    def write(self, row: dict) -> None:
        if self._rows >= XLSX_MAX_ROWS:
            self._new_sheet()
        self._ws.append([self._clean(row.get(c)) for c in self.layout.columns])
        self._rows += 1

    def close(self, sources: list[dict]) -> None:
        info = self._wb.create_sheet("sources")
        info.append(["source_file", "rows"])
        for s in sources:
            info.append([s["file"], s["rows"]])
        self._wb.save(self.path)


class WideSink:
    """Pivot: satu baris per series, satu kolom per periode. Seluruh data dimuat ke memori (pandas)."""

    def __init__(self, base: Path, formats: list[str], layout: Layout):
        self.base, self.formats, self.layout, self.rows = base, formats, layout, []

    def _path(self, ext: str) -> Path:
        # Jangan pakai with_suffix: nama berisi titik (versi "6.0.0") akan terpotong.
        return self.base.parent / (self.base.name + ext)

    def write(self, row: dict) -> None:
        self.rows.append(row)

    def close(self, sources: list[dict]) -> None:
        import pandas as pd
        lay = self.layout
        if not self.rows or not lay.time_col:
            log.warning("Tidak ada data / dimensi waktu untuk pivot: %s", self.base.name)
            return
        df = pd.DataFrame(self.rows)
        if df[lay.time_col].isna().all():
            log.error("Kolom %s kosong semua; pivot wide dilewati (lihat peringatan di atas).", lay.time_col)
            return
        skip = {lay.time_col, lay.time_col + "_name", "source_file"}
        idx = [c for c in lay.dim_cols if c not in skip and c in df.columns]
        if not idx:
            log.warning("Tidak ada dimensi series untuk pivot: %s", self.base.name)
            return
        df[idx] = df[idx].fillna("")
        wide = (df.groupby(idx + [lay.time_col], dropna=False)[lay.value_col].first()
                  .unstack(lay.time_col).reset_index())
        wide.columns.name = None
        if "csv" in self.formats:
            wide.to_csv(self._path(".csv"), index=False, encoding="utf-8-sig")
        if "xlsx" in self.formats:
            if len(wide) >= XLSX_MAX_ROWS:
                log.error("Terlalu banyak baris untuk satu sheet XLSX (%d); gunakan CSV.", len(wide))
            else:
                with pd.ExcelWriter(self._path(".xlsx"), engine="openpyxl") as xw:
                    wide.to_excel(xw, index=False, sheet_name="data")
                    pd.DataFrame(sources).to_excel(xw, index=False, sheet_name="sources")
        log.info("Wide: %d baris x %d kolom", *wide.shape)


# --------------------------------------------------------------------------
# Orkestrasi
# --------------------------------------------------------------------------
def discover_files(root: Path, dataflow: str | None) -> dict[Path, list[Path]]:
    """Kelompokkan file partisi per folder versi: <agency>/<dataflow>/<versi>/."""
    groups: dict[Path, list[Path]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.endswith(".meta.json") or p.name.endswith(".tmp"):
            continue
        if p.suffix != ".json":
            log.warning("Dilewati (bukan JSON): %s", p)
            continue
        rel = p.relative_to(root)
        if len(rel.parts) < 4:
            continue
        if dataflow and rel.parts[1] != dataflow:
            continue
        groups.setdefault(rel.parent, []).append(p)
    return groups


def export_group(rel: Path, paths: list[Path], args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "_".join(rel.parts)
    formats = ["csv", "xlsx"] if args.format == "both" else [args.format]
    label_maps: dict[str, dict[str, Any]] = {}
    if args.labels:
        structure_path = Path(args.input).parent / "dataflow" / rel / "default.json"
        try:
            label_maps = _dimension_labels(json.loads(structure_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            log.warning("Metadata dataflow tidak ditemukan di %s; label hanya memakai metadata data.", structure_path)
        except (OSError, ValueError) as exc:
            log.warning("Gagal membaca metadata dataflow %s: %s", structure_path, exc)
        if not label_maps and structure_path.is_file():
            log.warning("Tidak ada pemetaan codelist dimensi di %s.", structure_path)

    sinks: list = []
    layout: Layout | None = None
    sources: list[dict] = []
    errors = 0

    for p in paths:
        try:
            message = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Gagal membaca %s: %s", p, exc)
            errors += 1
            continue
        try:
            lay, rows = parse_message(message, args.labels, args.add_source, label_maps)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.error("Struktur SDMX-JSON tak dikenali di %s: %s", p, exc)
            errors += 1
            continue
        if lay is None:
            log.warning("Tidak ada dataSets di %s (kosong?)", p)
            continue

        if layout is None:
            layout = lay
            filename = p.stem
            if args.wide:
                sinks = [WideSink(out_dir / f"{stem}_{filename}_wide", formats, layout)]
            else:
                if "csv" in formats:
                    sinks.append(CsvSink(out_dir / f"{stem}_{filename}.csv", layout))
                if "xlsx" in formats:
                    sinks.append(XlsxSink(out_dir / f"{stem}_{filename}.xlsx", layout))
        elif lay.columns != layout.columns:
            log.warning("Kolom %s berbeda dari file pertama; kolom ekstra diabaikan.", p.name)

        n = 0
        try:
            for row in rows:
                if args.add_source:
                    row["source_file"] = p.name
                for s in sinks:
                    s.write(row)
                n += 1
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.error("Error di tengah parsing %s setelah %d baris: %s", p, n, exc)
            errors += 1
        sources.append({"file": p.name, "rows": n})
        log.info("%s: %d baris", p.name, n)

    for s in sinks:
        s.close(sources)
    if sinks:
        log.info("Selesai %s (%d baris total)", stem, sum(x["rows"] for x in sources))
    return errors


def inspect_message(message: dict) -> None:
    """Cetak ringkasan struktur SDMX-JSON untuk diagnosis (tanpa menulis file)."""
    data = message.get("data", message)
    print("kunci teratas:", list(message), "| kunci data:", list(data))
    structures = data.get("structures") or ([data["structure"]] if data.get("structure") else [])
    for si, st in enumerate(structures):
        print(f"[structure {si}] kunci: {list(st)}")
        for kind, dims in (st.get("dimensions") or {}).items():
            for d in dims:
                vals = d.get("values") or []
                print(f"  dimensi/{kind}: {d.get('id')} ({len(vals)} nilai) kunci item: "
                      f"{sorted({k for v in vals[:20] if isinstance(v, dict) for k in v})} "
                      f"contoh: {json.dumps(vals[:2], ensure_ascii=False)[:220]}")
        for m in (st.get("measures") or {}).get("observation", []):
            print(f"  measure: {m.get('id')}")
        for kind, defs in (st.get("attributes") or {}).items():
            for a in defs:
                vals = a.get("values") or []
                print(f"  atribut/{kind}: {a.get('id')} ({len(vals)} nilai) "
                      f"contoh: {json.dumps(vals[:2], ensure_ascii=False)[:160]}")
    for di, ds in enumerate(data.get("dataSets") or []):
        print(f"[dataSet {di}] kunci: {list(ds)} | struktur ke-{ds.get('structure', 0)}")
        series = ds.get("series") or {}
        for skey in list(series)[:1]:
            print(f"  series '{skey}': {json.dumps(series[skey], ensure_ascii=False)[:400]}")
        obs = ds.get("observations") or {}
        for okey in list(obs)[:2]:
            print(f"  observasi '{okey}': {json.dumps(obs[okey], ensure_ascii=False)[:200]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Konversi data SDMX-JSON ke CSV/XLSX")
    add_arguments(ap)
    return run(ap.parse_args(argv))


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--input", default="output/raw/data", help="Folder raw data (default: output/raw/data)")
    ap.add_argument("--out", default="output/export", help="Folder output (default: output/export)")
    ap.add_argument("--format", choices=["csv", "xlsx", "both"], default="csv")
    ap.add_argument("--dataflow", help="Hanya dataflow ini (mis. WEO)")
    ap.add_argument("--labels", action="store_true", help="Tambah kolom <DIM>_name berisi label dari codelist")
    ap.add_argument("--wide", action="store_true", help="Pivot: kolom = periode waktu (butuh pandas)")
    ap.add_argument("--add-source", action="store_true", help="Tambah kolom source_file (nama partisi)")
    ap.add_argument("--inspect", action="store_true",
                    help="Diagnosis: cetak struktur file JSON pertama tiap dataflow lalu keluar")


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO").upper()),
        format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.input)
    if not root.is_dir():
        print(f"Folder input tidak ada: {root}", file=sys.stderr)
        return 2
    groups = discover_files(root, args.dataflow)
    if not groups:
        print("Tidak ada file JSON data yang cocok. Sudah menjalankan scrape?", file=sys.stderr)
        return 2
    if args.inspect:
        for rel, paths in groups.items():
            print(f"===== {paths[0]}")
            try:
                inspect_message(json.loads(paths[0].read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                print(f"gagal membaca: {exc}")
        return 0
    errors = sum(export_group(rel, paths, args) for rel, paths in groups.items())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())