# STARBOOKS Video / Audio Transcriber

Transcribe spoken audio from local media into existing STARBOOKS MySQL records using Faster-Whisper. The script preserves the spoken language, skips records with `is_transcribe=1`, and saves nonempty transcripts with the completion flag in one guarded database update. It does not extract text from video frames.

## Setup

Python 3.10+ is required. In PowerShell, from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe video_transcriber_mysql.py --help
```

If `python` opens the Microsoft Store or cannot run, use your installed Python executable or `py -3` to create the environment. Activation is optional when using the executable directly.

The first transcription run may download model weights and needs internet access and disk space. `--model` also accepts a compatible local Faster-Whisper model directory. The model loads only when pending records exist and the run is not a dry run.

CPU is the default, with `int8` computation and the `small` model. CUDA is optional and requires an NVIDIA GPU and libraries compatible with the installed CTranslate2/Faster-Whisper versions. Verify CPU processing first. Media decoding uses PyAV; the script does not require a separate FFmpeg command-line installation.

## Database configuration

Set credentials in the same PowerShell session:

```powershell
$env:MYSQL_HOST = "localhost"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "starbooks_transcriber"
$env:MYSQL_PASSWORD = "your-password"
```

Defaults: `localhost:3306`, user `root`, empty password. These environment variable names differ from the PDF project's `STARBOOKS_DB_*` settings. CLI overrides include `--db-password`; prefer the environment variable to keep passwords out of shell history and process arguments. `--db-socket PATH` supports Unix sockets.

| Table | Columns used | Privileges |
| --- | --- | --- |
| `starbooks.records` | `HoldingsID`, `text_conversion`, `is_transcribe` | `SELECT`, `UPDATE` |
| `starbooks_old.tblmultimedia` | `Filename`, `HoldingsID` | `SELECT` |

Use InnoDB for `records` so failed updates can roll back. `HoldingsID` should be unique and indexed; index legacy `Filename` as well. Use a sufficiently large text column (typically `LONGTEXT`) and server packet limit. The script does not create tables or insert records. Override schemas with `--db-name` and `--old-db-name`; both accept simple MySQL identifiers.

## Usage

```powershell
# Check matches/status without model loading or record updates.
.\.venv\Scripts\python.exe video_transcriber_mysql.py --folder "D:\Videos" --dry-run

# Process two pending records, then the full folder.
.\.venv\Scripts\python.exe video_transcriber_mysql.py --folder "D:\Videos" --limit 2
.\.venv\Scripts\python.exe video_transcriber_mysql.py --folder "D:\Videos"

# Include subfolders and specify the spoken language.
.\.venv\Scripts\python.exe video_transcriber_mysql.py --folder "D:\Videos" --recursive --language tl

# Optional GPU configuration after installing compatible CUDA libraries.
.\.venv\Scripts\python.exe video_transcriber_mysql.py --folder "D:\Videos" --device cuda --model medium
```

By default, scanning covers only the script directory. Supported extensions:

```text
.dat .mp4 .mkv .mov .avi .webm .m4v .mpg .mpeg .ts .mts .wmv .flv .3gp
.mp3 .m4a .wav .flac .ogg .aac .wma
```

Extensions control discovery. Actual content must be decodable by PyAV and contain audio. Proprietary DAT files and document DAT files are not automatically converted to media.

## Matching and saving

1. Use the filename without its final extension as the candidate `HoldingsID`: `BOOK.001.mp4` becomes `BOOK.001`.
2. Query current `records`; skip completed rows before loading the model.
3. On a miss, match the exact original basename, including extension, in legacy `tblmultimedia.Filename`. Multiple legacy IDs are logged as ambiguous and skipped. Duplicate current IDs stop the database precheck.
4. Queue one file per resolved ID in sorted path order. Later files for the same ID are logged as duplicates and skipped. Files are not concatenated; combine multipart recordings before running this script if they belong to one record.
5. Transcribe each selected file fully. Failed, silent, or empty transcriptions are not marked complete.
6. Refresh the database connection and update text plus the flag only if the row is still incomplete. Updates affecting multiple rows are rolled back on a transactional table.

All discovered files are checked against MySQL before the model loads. `--limit` applies to the pending queue after matching, so it does not limit database precheck work. Both projects share the completion flag; a PDF transcription can cause a video record to be skipped.

## Options

| Option | Default / behavior |
| --- | --- |
| `--folder PATH` | Script directory |
| `--recursive` | Include subfolders |
| `--dry-run` | Matching only; still connects and writes a log |
| `--limit N` | At most N pending records; `0` means all |
| `--model NAME_OR_PATH` | `small`; larger models need more memory/time |
| `--language CODE` | `auto`; examples: `en`, `tl` |
| `--device cpu\|cuda` | `cpu` |
| `--compute-type TYPE` | `int8` on CPU, `float16` on CUDA |
| `--beam-size N` | `5`; must be positive |
| `--no-vad` | Disable default silence filtering |
| `--db-host`, `--db-port`, `--db-user`, `--db-password` | Override environment/default settings |
| `--db-socket PATH` | Optional Unix MySQL socket |
| `--db-name NAME` | `starbooks` |
| `--old-db-name NAME` | `starbooks_old` |

Transcripts contain normalized text segments separated by newlines, without timestamps. Automatic transcription can mishear speech; check representative recordings and language settings before a large run.

## Logs and troubleshooting

The script appends to `video_transcription.log` in the scanned folder, which must be writable. Exit codes: `0` completed run/dry run, including skipped/unmatched/ambiguous files; `1` setup, connection, or processing failure; `2` invalid CLI arguments; `130` interruption during per-file processing.

- **UNMATCHED:** check the filename stem and exact legacy filename mapping.
- **AMBIGUOUS / DUPLICATE:** inspect the log and resolve conflicting mappings or multiple recordings.
- **No speech:** check audio, language, and silence filtering. Failed files remain incomplete.
- **Model download or CUDA failure:** check model availability and compatible libraries, or use `--device cpu`.
- **Interrupted run:** committed files remain saved; rerun to skip them and retry unfinished work.

A dry run validates matches, not audio decoding or model setup. Test a small batch against a database copy before transcribing an archive.
