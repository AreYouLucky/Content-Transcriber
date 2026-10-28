# STARBOOKS Video / Audio Transcriber

## 1. Open PowerShell in the video folder

```powershell
cd "D:\Content Transcriber\video"
```

## 2. Install dependencies (first time only)

Python 3.10 or newer is required.

```powershell
python -m pip install -r requirements.txt
```

## 3. Configure MySQL

Make sure MySQL is running and the existing STARBOOKS databases are available: `starbooks` and `starbooks_old`.

The default connection is `localhost:3306`, user `root`, with an empty password. If your connection is different, set these values in the same PowerShell window:

```powershell
$env:MYSQL_HOST = "localhost"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "your-mysql-user"
$env:MYSQL_PASSWORD = "your-mysql-password"
```

## 4. Run the transcriber

Replace `D:\Videos` with the folder containing your video or audio files:

```powershell
python video_transcriber_mysql.py --folder "D:\Videos"

```

python video_transcriber_mysql.py --folder "D:\STARBOOKS Content\video" --device cuda --model small

cd "D:\Content Transcriber\video"
python video_transcriber_mysql.py --folder "D:\STARBOOKS Content\video" --device cuda --model small --limit 1

to run in GPU

Transcripts are saved to matching existing MySQL records. Completed records are skipped. The first transcription run may download the speech model and require internet access.

To include subfolders:

```powershell
python video_transcriber_mysql.py --folder "D:\Videos" --recursive
```

To check database matches without transcribing or updating records:

```powershell
python video_transcriber_mysql.py --folder "D:\Videos" --dry-run
```

For all available options:

```powershell
python video_transcriber_mysql.py --help
```
