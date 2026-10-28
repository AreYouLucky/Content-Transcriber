# STARBOOKS PDF / DAT Transcriber

## 1. Open PowerShell in the PDF folder

```powershell
cd "D:\Content Transcriber\pdf"
```

## 2. Install dependencies (first time only)

Python 3.10 or newer is required.

```powershell
python -m pip install -r requirements.txt
```

## 3. Configure MySQL

Make sure MySQL is running and the existing STARBOOKS databases are available: `starbooks` and `starbooks_old`.

The default connection is `127.0.0.1:3306`, user `root`, with an empty password. If your connection is different, set these values in the same PowerShell window:

```powershell
$env:STARBOOKS_DB_HOST = "127.0.0.1"
$env:STARBOOKS_DB_PORT = "3306"
$env:STARBOOKS_DB_USER = "your-mysql-user"
$env:STARBOOKS_DB_PASSWORD = "your-mysql-password"
```

## 4. Run the transcriber

Replace `D:\Documents` with the folder containing your PDF or document DAT files:

```powershell
python starbooks_transcribe.py --root "D:\STARBOOKS Content\fulltext"
```

Subfolders are included automatically. Keep documents for each record in a separate folder named after its `HoldingsID`. Files directly inside the root folder are grouped by the filename before the first dot.

Extracted text is saved to matching existing MySQL records. Completed records are skipped.

For scanned PDFs on Windows, use Windows OCR. Keep `windows_pdf_ocr.ps1` beside the Python script and make sure the English OCR language is installed in Windows:

```powershell
python starbooks_transcribe.py --root "D:\STARBOOKS Content\fulltext" --ocr-engine windows
```

To check database matches without extracting text or updating records:

```powershell
python starbooks_transcribe.py --root "D:\STARBOOKS Content\fulltext" --dry-run
```

To preview one file without MySQL and save its extracted text:

```powershell
python starbooks_transcribe.py --preview "D:\STARBOOKS Content\fulltext\BOOK-001.pdf" --output preview.txt
```

For all available options:

```powershell
python starbooks_transcribe.py --help
```

```to test run
"D:/Content Transcriber/pdf/.venv/Scripts/python.exe" "D:/Content Transcriber/pdf/starbooks_transcribe.py" --root "D:/STARBOOKS Content/fulltext" --dry-run
```

```to run explicitly
"D:/Content Transcriber/pdf/.venv/Scripts/python.exe" "D:/Content Transcriber/pdf/starbooks_transcribe.py" --root "D:/STARBOOKS Content/fulltext"
```
