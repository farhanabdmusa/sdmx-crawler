"""Uji offline sdmx_crawler.export_data dengan pesan SDMX-JSON 2.0 sintetis (dua partisi)."""
import csv, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdmx_crawler import export_data

def message(years, idn, usa):
    return {"data": {
        "dataSets": [{"structure": 0, "series": {
            "0:0": {"attributes": [0], "observations": {f"0:{i}": [v, 1] for i, v in enumerate(idn)}},
            "1:0": {"attributes": [None], "observations": {f"0:{i}": [v, None] for i, v in enumerate(usa)}},
        }}],
        "structures": [{
            "dimensions": {
                "series": [
                    {"id": "COUNTRY", "values": [{"id": "IDN", "name": "Indonesia"}, {"id": "USA", "name": "United States"}]},
                    {"id": "INDICATOR", "values": [{"id": "NGDP_RPCH", "name": {"en": "Real GDP growth"}}]}],
                "observation": [
                    {"id": "FREQUENCY", "values": [{"id": "A"}]},
                    {"id": "TIME_PERIOD", "values": [{"id": y} for y in years]}]},
            "measures": {"observation": [{"id": "OBS_VALUE"}]},
            "attributes": {"series": [{"id": "SCALE", "values": [{"id": "UNITS"}]}],
                           "observation": [{"id": "STATUS", "values": [{"id": "A"}, {"id": "E"}]}]},
        }]}}

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp, "raw", "data", "IMF.RES", "WEO", "6.0.0"); base.mkdir(parents=True)
    structure = Path(tmp, "raw", "dataflow", "IMF.RES", "WEO", "6.0.0")
    structure.mkdir(parents=True)
    (structure / "default.json").write_text(json.dumps({"data": {
        "dataStructures": [{"dataStructureComponents": {"dimensionList": {"dimensions": [
            {"id": "FREQUENCY", "conceptIdentity": "urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=IMF:CS_MASTER_SYSTEM(1.0).FREQ"}
        ]}}}],
        "conceptSchemes": [{"id": "CS_MASTER_SYSTEM", "agencyID": "IMF", "concepts": [
            {"id": "FREQ", "coreRepresentation": {
                "enumeration": "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=IMF:CL_FREQ(1.0)"
            }}
        ]}],
        "codelists": [{"id": "CL_FREQ", "agencyID": "IMF", "codes": [
            {"id": "A", "name": "Annual"}
        ]}]
    }}))
    (base / "p1.json").write_text(json.dumps(message(["2020", "2021"], [-2.1, 3.7], [-2.2, 5.8])))
    (base / "p2.json").write_text(json.dumps(message(["2022", "2023"], [5.3, 5.0], [1.9, 2.5])))
    (base / "p1.json.meta.json").write_text("{}")  # harus diabaikan
    out = Path(tmp, "export")
    args = ["--input", str(Path(tmp, "raw", "data")), "--out", str(out)]

    assert export_data.main(args + ["--format", "both", "--labels", "--add-source"]) == 0
    rows = list(csv.DictReader(open(out / "IMF.RES_WEO_6.0.0.csv", encoding="utf-8-sig")))
    assert len(rows) == 8, len(rows)
    r = rows[0]
    assert (r["COUNTRY"], r["COUNTRY_name"], r["INDICATOR_name"], r["TIME_PERIOD"], r["OBS_VALUE"],
            r["FREQUENCY"], r["FREQUENCY_name"], r["SCALE"], r["STATUS"], r["source_file"]) == (
                "IDN", "Indonesia", "Real GDP growth", "2020", "-2.1", "A", "Annual", "UNITS", "E", "p1.json"), r
    from openpyxl import load_workbook
    wb = load_workbook(out / "IMF.RES_WEO_6.0.0.xlsx", read_only=True)
    assert wb.sheetnames == ["data", "sources"] and len(list(wb["data"].iter_rows())) == 9

    assert export_data.main(args + ["--format", "both", "--wide"]) == 0
    wide = list(csv.DictReader(open(out / "IMF.RES_WEO_6.0.0_wide.csv", encoding="utf-8-sig")))
    assert len(wide) == 2 and wide[0]["2023"] == "5.0" and wide[1]["2021"] == "5.8", wide
    print("EXPORT TEST OK")
