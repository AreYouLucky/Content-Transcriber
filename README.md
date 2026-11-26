# Content Transcriber

Three command-line projects extract text from local STARBOOKS content and save it to existing MySQL records.

| Project | Input | Method | Instructions |
| --- | --- | --- | --- |
| [PDF](pdf/) | PDF and document DAT files | Embedded text extraction and OCR | [PDF README](pdf/README.md) |
| [Video](video/) | Video, audio, and media DAT files | Speech transcription with Faster-Whisper | [Video README](video/README.md) |
| [Audio](audio/) | Audio files named `HoldingsID.dat` (or `.mp3`, `.wav`, ...) | Speech transcription with Faster-Whisper, matched by `HoldingsID` only | [Audio README](audio/README.md) |

All projects match existing `HoldingsID` values, skip records where `is_transcribe=1`, and save `text_conversion` and the completion flag together. They never insert missing records. A dry run checks database matches without extraction, model downloads, or record updates; it still connects to MySQL and writes a log.

Use Python 3.10+ and a separate virtual environment for each project. Start with its README and a dry run against a test database. The PDF project also supports local extraction previews without MySQL.

The projects share the same completion flag: once any project completes a record, the others skip it. Choose the correct project for the content; a `.dat` extension alone does not identify its format.

## Development checks

From this directory, using the PDF virtual environment after installing its requirements:

```powershell
.\pdf\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The regression tests use temporary files and database/model doubles; they do not connect to MySQL, download models, or change archive records. Live MySQL, OCR, and model inference need separate environment-specific checks.
