# STARBOOKS PDF / DAT Transcriber

Extract text from local PDF and document DAT files into `starbooks.records.text_conversion`. The script reads embedded PDF text and, by default, uses OCR for scanned or weak pages. Completed records are skipped before extraction. Only successful, nonempty extraction is saved with `is_transcribe=1`.

## Setup

Python 3.10+ is required. In PowerShell, from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe starbooks_transcribe.py --help
```

If `python` opens the Microsoft Store or cannot run, use your installed Python executable or `py -3` to create the environment. Activation is optional when using the environment's executable directly.

Required packages: `pypdf` and `PyMySQL`. OCR setup depends on the engine:

- **Windows:** the included `windows_pdf_ocr.ps1` uses Windows PowerShell, the Windows PDF renderer, and an installed Windows OCR language. No PyMuPDF or Tesseract installation is needed. Keep the helper beside the Python script. Select it with `--ocr-engine windows`.
- **PyMuPDF:** install with `.\.venv\Scripts\python.exe -m pip install PyMuPDF`. OCR also needs Tesseract language data. Pass `--tessdata "C:\path\to\tessdata"`, set `TESSDATA_PREFIX`, or place language files in `pdf/tessdata/`. English needs `eng.traineddata`. Select it with `--ocr-engine pymupdf`.
- **Text extraction only:** use `--ocr never`. Pages detected as requiring OCR cause the job to fail instead of silently saving incomplete text.

`--ocr-engine auto` prefers PyMuPDF when importable; otherwise it uses pypdf with Windows OCR when needed. A PyMuPDF OCR failure does not automatically switch to Windows: select `windows` explicitly if native libraries or Tesseract data are unavailable. Outside Windows, scanned PDFs require PyMuPDF and its OCR language data.

## Preview without MySQL

```powershell
.\.venv\Scripts\python.exe starbooks_transcribe.py --preview "D:\Documents\BOOK-001.pdf" --output preview.txt
.\.venv\Scripts\python.exe starbooks_transcribe.py --preview "D:\Documents\BOOK-001.dat" --ocr never
```

Without `--output`, text is printed to the terminal. Output is UTF-8; the output path must differ from the input. Preview still creates a log under `--root` (the script directory by default).

## Database configuration

Set credentials in the same PowerShell session:

```powershell
$env:STARBOOKS_DB_HOST = "127.0.0.1"
$env:STARBOOKS_DB_PORT = "3306"
$env:STARBOOKS_DB_USER = "starbooks_transcriber"
$env:STARBOOKS_DB_PASSWORD = "your-password"
```

Defaults: `127.0.0.1:3306`, user `root`, empty password. On Unix, `STARBOOKS_DB_SOCKET` or `--db-socket` selects a socket instead of host/port. Host, port, and user also have CLI overrides; the password comes from `STARBOOKS_DB_PASSWORD`.

| Table | Columns used | Privileges |
| --- | --- | --- |
| `starbooks.records` | `HoldingsID`, `text_conversion`, `is_transcribe` | `SELECT`, `UPDATE` |
| `starbooks_old.tblmultimedia` | `Filename`, `HoldingsID` | `SELECT` |

Use an InnoDB `records` table so failed transactions can roll back. `HoldingsID` should be unique and indexed; index legacy `Filename` for fast lookups. Use a text column large enough for complete books (typically `LONGTEXT`) and a server `max_allowed_packet` large enough for updates. The script does not create or migrate tables.

## Batch usage

```powershell
# Check matches/status without extraction or record updates.
.\.venv\Scripts\python.exe starbooks_transcribe.py --root "D:\Documents" --dry-run

# Process a small batch, then the full archive.
.\.venv\Scripts\python.exe starbooks_transcribe.py --root "D:\Documents" --limit 10
.\.venv\Scripts\python.exe starbooks_transcribe.py --root "D:\Documents"
```

The default root is the script directory. Scanning includes nested folders, excluding hidden directories and common environment/cache folders.

```text
Documents/
  BOOK-001.pdf
  BOOK-002/
    BOOK-002.pdf1.dat
    BOOK-002.pdf2.dat
    BOOK-002.pdf10.dat
```

Each non-root folder containing documents is one record, using its folder name as the candidate ID. Root files are grouped by the name before the first dot. Numbered page files are sorted numerically; pages are separated with `\n\n\f\n\n`. Nested folders form independent jobs. Keep each folder associated with one record. If PDF and DAT copies coexist in one job, both are included and a warning is logged.

Matching first checks `starbooks.records.HoldingsID`. On a miss, it checks exact original basenames in `starbooks_old.tblmultimedia.Filename`. Conflicting legacy IDs across the job's files or duplicate current IDs fail the job. A successful extraction updates the existing row only if its completion flag is still unset. The connection is refreshed after extraction; updates affecting multiple rows are rolled back on a transactional table.

## Options

| Option | Default / behavior |
| --- | --- |
| `--root PATH` | Script directory |
| `--dry-run` | Check IDs/status only; requires MySQL |
| `--preview FILE` | Extract one PDF/DAT without MySQL; excludes `--dry-run` |
| `--output FILE` | UTF-8 destination; requires `--preview` |
| `--limit N` | Maximum jobs, including skipped/unmatched jobs; `0` means all |
| `--ocr auto\|never\|always` | `auto`: OCR weak pages with graphics or extraction problems |
| `--ocr-engine auto\|pymupdf\|windows` | `auto` |
| `--ocr-language CODE` | `eng`; PyMuPDF accepts combinations such as `eng+fil` with matching data |
| `--ocr-dpi N` | `300`; permitted range `72–600` |
| `--ocr-min-chars N` | `40`; threshold based on usable text score |
| `--tessdata PATH` | PyMuPDF OCR language-data folder |
| `--max-dat-mb N` | `256`; positive limit for DAT input and decoded payload sizes |
| `--db-host`, `--db-port`, `--db-user`, `--db-socket` | Override database connection settings |
| `--verbose` | Include debug messages in logs |

Windows OCR accepts one installed language at a time. `eng` maps to English; selecting a code does not install that language. OCR quality depends on scan quality, resolution, and language data. A successfully processed blank/image-only page may remain empty, but an entirely empty job is never saved as complete.

DAT decoding supports PDF bytes, UTF-8/UTF-16/Windows-1252 text, HTML, gzip, zlib, bzip2, xz, supported ZIP entries, and certain base64 wrappers. Encrypted or proprietary formats need a separate decoder. A source extraction error leaves that job incomplete.

## Logs and troubleshooting

Logs are written to `starbooks_transcribe_YYYYMMDD_HHMMSS.log` under the scanned root, which must be writable. Exit codes: `0` successful run (possibly skipped/unmatched jobs), `1` processing failures, `2` invalid arguments/settings or database connection/schema failures.

- **OCR unavailable:** select `--ocr-engine windows` on Windows and install the requested OCR language, or configure PyMuPDF and its Tesseract data.
- **UNMATCHED:** check folder/filename IDs and exact legacy basenames. Missing records are not inserted.
- **Already completed:** rows with `is_transcribe=1` are skipped; the script does not reset the flag.
- **Large update rejected:** check column capacity and the server packet limit.
- **Interrupted run:** rerun the command; previously committed records are skipped.

Test a dry run and a small batch against a database copy before processing an archive. Dry runs validate matching, not PDF readability or OCR availability; use `--preview` to check extraction.
