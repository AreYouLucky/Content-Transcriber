# STARBOOKS Audio Transcriber

Transcribes audio files named after their `HoldingsID` (for example `ABC-0001.dat`) and saves the text into the matching `starbooks.records` row:

- `text_conversion` = transcript
- `is_transcribe` = `1`

Matching is by **HoldingsID only**: the filename without its extension must equal `records.HoldingsID`. Files with no matching record are logged as `UNMATCHED` and skipped. Records already marked `is_transcribe = 1` are skipped before any transcription. New records are never inserted.

Supported files: `.dat`, `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.wma`, `.opus`. A `.dat` file is read directly as long as FFmpeg can decode the audio inside it.

## 1. Open PowerShell in the audio folder

```powershell
cd "D:\Content Transcriber\audio"
```

## 2. Install dependencies (first time only)

Python 3.10 or newer is required.

```powershell
python -m pip install -r requirements.txt
```

## 3. Configure MySQL

The default connection is `localhost:3306`, user `root`, empty password, database `starbooks`. To change it, set these in the same PowerShell window:

```powershell
$env:MYSQL_HOST = "localhost"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "your-mysql-user"
$env:MYSQL_PASSWORD = "your-mysql-password"
```

## 4. Check matches first (dry run)

This connects to MySQL and logs which files are `PENDING`, `SKIP`, or `UNMATCHED`. It does not load a model or change any records.

```powershell
python audio_transcriber_mysql.py --folder "D:\STARBOOKS Content\audio" --dry-run
```

## 5. Run the transcriber

```powershell
python audio_transcriber_mysql.py --folder "D:\STARBOOKS Content\audio"
```

To use the GPU (NVIDIA CUDA):

```powershell
python audio_transcriber_mysql.py --folder "D:\STARBOOKS Content\audio" --device cuda --model small
```

To test one file first:

```powershell
python audio_transcriber_mysql.py --folder "D:\STARBOOKS Content\audio" --limit 1
```

Other useful options:

| Option | Meaning |
| --- | --- |
| `--recursive` | Also scan subfolders |
| `--model medium` | Larger, more accurate model (slower) |
| `--language tl` | Force a language (default: auto-detect) |
| `--no-vad` | Do not filter out silence |

The first run downloads the speech model and needs internet access. A log is written to `audio_transcription.log` inside the scanned folder.

For all options:

```powershell
python audio_transcriber_mysql.py --help
```
