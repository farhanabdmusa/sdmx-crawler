# SDMX 3.0 Crawler & Scraper (contoh: IMF WEO)

Dua fase terpisah. Status disimpan di SQLite, sehingga proses bisa dilanjutkan kapan saja.

```
Fase 1  discover  -> listing struktur SDMX -> katalog endpoint (SQLite + catalog/manifest.ndjson)
Fase 2  scrape    -> ambil payload tiap endpoint pending/failed -> raw/ + tabel scraping_logs
```

## Struktur proyek (mana yang dipakai)

```
sdmx_scraper/
├── sdmx_crawler/          <- MODUL UTAMA (jalankan: python -m sdmx_crawler ...)
│   ├── cli.py             perintah discover | scrape | run | status | export
│   ├── export_data.py     konversi JSON hasil scrape -> CSV/XLSX
│   ├── discovery.py       Fase 1: katalog endpoint + manifest.ndjson
│   ├── scraper.py         Fase 2: loop pending/failed, resume, Ctrl+C aman ga akan langsung abort command
│   ├── fetcher.py         single worker: fetch -> simpan -> update status & log
│   ├── http_client.py     httpx + retry/exponential backoff
│   ├── db.py              SQLite (endpoints, scraping_logs)
│   ├── storage.py         simpan file mentah + .meta.json
│   ├── config.py, urls.py konfigurasi (termasuk default config) &  URL Builder
│   ├── __init__.py, __main__.py   wajib ada (agar `python -m sdmx_crawler` jalan)
├── config.example.json    salin menjadi config.json lalu sesuaikan
├── requirements.txt
├── README.md
└── tests/                 OPSIONAL, hanya untuk verifikasi offline
    ├── smoke_test.py      uji crawler (server tiruan)
    └── test_export.py     uji sdmx_crawler.export_data
```

Folder `output/` dibuat otomatis saat dijalankan (database, katalog, file mentah, hasil export); bukan bagian dari kode. Tergantung dari `config - output_dir`.

## 1. Setup (venv)

```bash
cd sdmx_scraper
python3 -m venv .venv
```

Aktifkan virtual environment sesuai sistem operasi:

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

```cmd
:: Windows (Command Prompt)
.venv\Scripts\activate.bat
```

Lalu instal dependensi:

```bash
pip install -r requirements.txt
```

## 2. Menjalankan

```bash
cp config.example.json config.json        # opsional; tanpa argument -c akan mengambil semua possible data

python -m sdmx_crawler -c config.json discover   # Fase 1
python -m sdmx_crawler -c config.json scrape     # Fase 2
python -m sdmx_crawler -c config.json status     # ringkasan status (tambah --failed untuk detail error)
python -m sdmx_crawler -c config.json run        # Fase 1 + Fase 2 sekaligus
```

### Opsi args

- **`scrape --types data,dataflow`** — batasi scrape ke tipe endpoint yang disebutkan. Pisahkan
  beberapa tipe dengan koma; tipe lain tidak diproses pada pemanggilan tersebut. Contoh,
  `--types data` hanya mengambil endpoint data, sedangkan `--types data,dataflow` mengambil
  endpoint data dan dataflow.
- **`scrape --limit 10`** — proses paling banyak 10 endpoint yang memenuhi kriteria scrape pada
  pemanggilan ini. Endpoint lain tetap ada di katalog dan dapat diproses pada pemanggilan berikutnya.
- **`scrape --no-retry-failed`** — jangan mencoba kembali endpoint berstatus `failed`; endpoint
  `pending` yang sesuai filter tetap diproses. Tanpa opsi ini, endpoint `failed` dapat dicoba lagi.
- **`discover --refresh`** — crawl ulang listing dan bangun ulang katalog. Endpoint lama dan log
  terkait diganti, lalu endpoint hasil katalog baru berstatus `pending`. Gunakan dengan memahami
  dampaknya seperti dijelaskan di bagian 3b.
- **`discover --refresh --purge-raw`** — lakukan refresh sekaligus hapus file raw dan `.meta.json`
  milik endpoint lama. File raw tidak dihapus jika hanya menggunakan `--refresh`.
- **`--log-level DEBUG`** — aktifkan keluaran log yang lebih rinci untuk membantu menelusuri
  proses. Contoh pemakaian:

```bash
python -m sdmx_crawler -c config.json --log-level DEBUG scrape --types data --limit 10
python -m sdmx_crawler -c config.json discover --refresh --purge-raw
```

## 3. Cara kerja status & resume

Semua endpoint ada di tabel `endpoints` (`output/state.sqlite`) dengan status `pending`, `success`, atau
`failed`. Setiap percobaan fetch dicatat di tabel `scraping_logs` (waktu, status HTTP, ukuran file,
jumlah percobaan HTTP, error). Status di-commit per item.

Untuk melanjutkan proses yang terhenti, **jalankan perintah yang sama lagi**:

```bash
python -m sdmx_crawler -c config.json scrape
```

- Hanya endpoint `pending` dan `failed` yang diproses; yang `success` dilewati.
- `Ctrl+C` / SIGTERM: item yang sedang berjalan diselesaikan dulu, setelah selesai baru berhenti.
- Crash / kill paksa: item yang sedang berjalan tetap `pending`/`failed` dan diulang.
- `discover` (tanpa `--refresh`) hanya menambah endpoint baru (`INSERT OR IGNORE`); status lama tidak berubah.
  Untuk memperbarui katalog, lihat bagian 3b.

Exit code `scrape`: 0 = semua sukses, 1 = ada yang gagal, 130 = dihentikan manual.

## 3b. Memperbarui katalog (`discover --refresh`)

Gunakan saat dataset/versi di server berubah, atau Anda mengubah `dataflow_ids`, `data_partitions`,
dan pengaturan katalog lain di config.

```bash
python -m sdmx_crawler -c config.json discover --refresh               # bangun ulang katalog
python -m sdmx_crawler -c config.json discover --refresh --purge-raw   # + hapus file mentah lama
python -m sdmx_crawler -c config.json scrape                           # ambil semua endpoint baru
```

Yang terjadi pada `--refresh`:

1. Semua listing struktur di-crawl ulang dari server (bukan memakai hasil lama).
2. Daftar endpoint dihitung ulang dari listing terbaru + config saat ini.
3. Dalam **satu transaksi atomik**, semua endpoint lama dihapus (beserta `scraping_logs`-nya) dan diganti
   endpoint baru yang seluruhnya `pending`. Karena itu `scrape` berikutnya mengambil _semua_ data dari
   awal, bukan hanya melanjutkan.
4. Sebelum diganti, `manifest.ndjson` dan `state.sqlite` lama dicadangkan sebagai
   `manifest.<timestamp>.ndjson` dan `state.<timestamp>.sqlite` (riwayat log lama tetap bisa dibuka di cadangan).

Pengaman: jika listing dataflow atau listing tipe yang dipakai gagal (error jaringan, 5xx), refresh
**dibatalkan** dan katalog lama tidak disentuh (exit code 2). Tipe yang tidak didukung server (404)
tidak membatalkan refresh.

`--purge-raw` juga menghapus file mentah (dan `.meta.json`) milik endpoint lama. Disarankan jika Anda
menghapus/mengganti nama partisi, karena `sdmx_crawler.export_data` menggabungkan semua file di folder versi
yang sama dan file partisi lama akan ikut terbawa. Tanpa `--purge-raw`, file lama dibiarkan di disk
(akan tertimpa jika endpoint-nya diambil lagi).

## 4. Output

```
output/
  state.sqlite                     tabel endpoints (katalog + status) dan scraping_logs
  catalog/manifest.ndjson          katalog endpoint (1 endpoint per baris, termasuk metadata mentah)
  logs/crawler.log
  raw/<resource_type>/<agency>/<id>/<version>/<partisi>.json
  raw/.../<partisi>.json.meta.json URL, status HTTP, sha256, ukuran, waktu fetch
```

Query berguna:

```bash
python -m sdmx_crawler -c config.json status --failed
sqlite3 output/state.sqlite "SELECT resource_type,status,COUNT(*) FROM endpoints GROUP BY 1,2;"
sqlite3 output/state.sqlite "SELECT url,last_error FROM endpoints WHERE status='failed';"
sqlite3 output/state.sqlite "SELECT * FROM scraping_logs ORDER BY id DESC LIMIT 20;"
```

## 5. Cakupan Fase 1

| Resource                        | Endpoint (relatif ke `base_url`)                                                                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Listing struktur                | `structure/{type}/{agency}/*/+` untuk dataflow, datastructure, codelist, conceptscheme, agencyscheme, categoryscheme, categorisation, dataconstraint |
| Detail dataflow                 | `structure/dataflow/{agency}/{id}/{ver}?references=descendants` (DSD, codelist, concept ikut)                                                        |
| Detail DSD / codelist / concept | `structure/{type}/{agency}/{id}/{ver}`                                                                                                               |
| Data                            | `data/dataflow/{agency}/{id}/{ver}/{key}?{params}` (satu endpoint per partisi)                                                                       |
| Ketersediaan nilai              | `availability/dataflow/{agency}/{id}/{ver}/{key}/*`                                                                                                  |

Tipe yang tidak didukung server (404) dicatat `failed` dan tidak menghentikan proses.

## 6. Memecah dataset besar (partisi)

Di `config.json`, tiap partisi menjadi satu endpoint dengan status sendiri:

```json
"data_partitions": [
  {"name": "1980-1999", "params": {"c[TIME_PERIOD]": "ge:1980-01-01+le:1999-12-31", "attributes": "dsd", "measures": "all"}},
  {"name": "2000-2030", "params": {"c[TIME_PERIOD]": "ge:2000-01-01+le:2030-12-31", "attributes": "dsd", "measures": "all"}}
]
```

Filter dimensi lain mengikuti sintaks SDMX 3.0 (koma = OR, `+` = AND; nama dimensi sesuai DSD, bisa
`COUNTRY` bukan `REF_AREA`), mis. `"c[REF_AREA]": "IDN,USA"`, atau ubah `data_key` di `config`.
Setelah mengubah partisi, jalankan `discover --refresh` lalu `scrape`. Tambahkan `--purge-raw` jika perlu.

## 7. Catatan penting

- Respons dimuat ke memori sebelum ditulis; untuk dataset sangat besar, gunakan partisi.
- Retry: exponential backoff + jitter untuk timeout, error jaringan, 408/425/429/5xx (`Retry-After`).
  4xx lain langsung `failed` tanpa retry.
- Uji offline (tanpa jaringan): `python tests/smoke_test.py` dan `python tests/test_export.py`.

## 8. Konversi data JSON ke CSV / XLSX

Setelah `scrape`, ubah payload data (SDMX-JSON) menjadi tabel dengan modul ekspor:

```bash
python -m sdmx_crawler export                              # CSV, satu baris per observasi
python -m sdmx_crawler export --format xlsx --labels       # XLSX + kolom <DIM>_name dari label/codelist
python -m sdmx_crawler export --format both --dataflow WEO
python -m sdmx_crawler export --wide --format xlsx         # satu baris per series, kolom = tahun
```

| Opsi                       | Fungsi                                                                                 |
| -------------------------- | -------------------------------------------------------------------------------------- |
| `--input` / `--out`        | Folder sumber (`output/raw/data`) dan tujuan (`output/export`)                         |
| `--format csv\|xlsx\|both` | Format output (default `csv`)                                                          |
| `--labels`                 | Tambah kolom label di samping kode dimensi; label dicari di metadata dataflow/codelist |
| `--wide`                   | Pivot periode waktu menjadi kolom                                                      |
| `--add-source`             | Tambah kolom `source_file` (asal partisi)                                              |

- Semua partisi satu dataflow digabung menjadi satu file: `output/export/<agency>_<dataflow>_<versi>.csv`.
- XLSX ditulis dengan mode streaming (hemat memori) dan pindah ke sheet baru otomatis jika melewati
  batas 1.048.576 baris Excel. Sheet `sources` mencatat file asal dan jumlah barisnya.
- CSV memakai UTF-8 dengan BOM supaya langsung terbaca benar di Excel.
