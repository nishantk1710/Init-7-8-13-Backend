# Fallback workbooks — local source folder

This folder is where `python -m app.ingest.fallback --upload` (no file
arguments) and `--sync` look for the workbooks that stand in for the five tables
the live SAP routes cannot fill. It is the default `FALLBACK_SOURCE_DIR`.

**Everything here except this README is gitignored.** The workbooks are real
SAP extracts (material numbers, suppliers, prices); they reach Azure through
storage, never through git.

## Expected files

Matched by file name, case-insensitive. Anything else in the folder is reported
as refused and skipped.

| File | Table | Storage key (under `STORAGE_URL`) |
| --- | --- | --- |
| `EKKO.XLSX` | `raw_ekko` | `KPI 02 Data Extract/Tables/EKKO.XLSX` |
| `EKET.XLSX` | `raw_eket` | `KPI 02 Data Extract/Tables/EKET.XLSX` |
| `GB-ZMM065 - July 2026.XLSX` | `raw_zmm065_gb` | `Resources Shared - Rohit/GB-ZMM065 - July 2026.XLSX` |
| `BMM-ZMM065_Aging_Jul 26.xlsx` | `raw_zmm065_bmm` | `Resources Shared - Rohit/BMM-ZMM065_Aging_Jul 26.xlsx` |
| `30 Day GR Report.xlsx` | `raw_gr_30day` | `Resources Shared - Rohit/30 Day GR Report.xlsx` |

The keys come from `app/seed/manifest.py`; if this table and the manifest ever
disagree, the manifest is right. `python -m app.ingest.fallback --list` prints
the current keys and whether each is already in storage.

## Using it

```bash
python -m app.ingest.fallback --upload        # folder -> storage, report per file
python -m app.ingest.fallback --sync          # folder -> storage -> Azure SQL
python -m app.ingest.fallback --sync ekko     # just the tables named
```

`--sync` refuses a table that already holds live SAP data unless `--force` is
given, exactly as `--load` does.

Uploading needs `STORAGE_URL` and a credential: `az login` on a laptop, the
managed identity on the App Service. Loading needs Azure SQL, which answers only
from inside the VNet — so `--sync` end to end runs from the App Service SSH
console. There, point `FALLBACK_SOURCE_DIR` at a folder under `/home` (for
example `/home/fallback`): anything outside `/home` is lost on restart.
