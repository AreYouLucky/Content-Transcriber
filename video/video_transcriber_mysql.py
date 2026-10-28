#!/usr/bin/env python3
"""Batch transcribe local video/audio files into STARBOOKS MySQL records.

Behavior:
  - By default, scan video/audio/.dat files beside this Python script.
  - Determine HoldingsID from the filename without its final extension.
  - Query starbooks.records first; skip immediately if is_transcribe = 1.
  - If HoldingsID is absent, search starbooks_old.tblmultimedia.Filename
    using the ORIGINAL filename (including .dat/.mp4/etc), and use the
    HoldingsID found there to look up starbooks.records.
  - Transcribe audio directly with Faster-Whisper (including .dat containers
    supported by FFmpeg/PyAV); do NOT extract video frame text.
  - Only after a nonempty, successful transcription, update
    records.text_conversion and records.is_transcribe = 1 together.
  - Never insert new records or mark silent/failed files as transcribed.

Install:
    python -m pip install -r requirements.txt

Examples:
    python video_transcriber_mysql.py --dry-run
    python video_transcriber_mysql.py
    python video_transcriber_mysql.py --folder /path/to/videos --model medium
    python video_transcriber_mysql.py --language tl --device cuda --model medium
    python video_transcriber_mysql.py --db-user root --db-password ''

Database credentials may also be supplied through MYSQL_HOST, MYSQL_PORT,
MYSQL_USER, and MYSQL_PASSWORD environment variables. Password is empty by
DEFAULT, as requested. Pass credentials in an environment variable rather
than command line when they are sensitive.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MEDIA_EXTENSIONS = {
    ".dat", ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".mts", ".wmv", ".flv", ".3gp",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma",
}

LOGGER = logging.getLogger("starbooks_video_transcriber")


@dataclass(frozen=True)
class PendingVideo:
    file: Path
    holdings_id: Any
    source: str  # 'records' or 'starbooks_old.tblmultimedia'


def safe_identifier(name: str) -> str:
    """Only permit simple MySQL schema names (no arbitrary SQL identifiers)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError(f"Invalid MySQL schema name: {name!r}")
    return f"`{name}`"


def scan_media(folder: Path, recursive: bool) -> list[Path]:
    if not folder.is_dir():
        raise NotADirectoryError(f"Video folder does not exist: {folder}")
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (p for p in it if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS),
        key=lambda path: str(path).lower(),
    )


def connect_mysql(args):
    try:
        import mysql.connector
    except ImportError as exc:
        raise RuntimeError(
            "MySQL connector missing. Install: pip install mysql-connector-python"
        ) from exc

    options = {
        "host": args.db_host,
        "port": args.db_port,
        "user": args.db_user,
        "password": args.db_password,
        "database": args.db_name,
        "charset": "utf8mb4",
        "use_unicode": True,
        "autocommit": True,
        "connection_timeout": 15,
    }
    if args.db_socket:
        options["unix_socket"] = args.db_socket

    return mysql.connector.connect(**options)


def get_record(conn, schema: str, holdings_id: Any):
    sql = (
        f"SELECT `HoldingsID`, `is_transcribe` "
        f"FROM {schema}.`records` WHERE `HoldingsID` = %s LIMIT 2"
    )
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, (holdings_id,))
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError(f"Duplicate HoldingsID in records: {holdings_id}")
        return rows[0] if rows else None
    finally:
        cursor.close()


def resolve_video(conn, filename: str, stem: str, schema: str, old_schema: str):
    """Return (status, HoldingsID, source).

    status: 'pending', 'already_done', 'unmatched', or 'ambiguous'.
    """
    # First query the current records table, avoiding transcription entirely
    # for items already marked as completed.
    record = get_record(conn, schema, stem)
    source = "records"

    if record is None:
        # Lookup uses complete original file name, e.g. SeSIDASa.dat.
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT DISTINCT `HoldingsID` FROM {old_schema}.`tblmultimedia` "
                "WHERE `Filename` = %s AND `HoldingsID` IS NOT NULL LIMIT 2",
                (filename,),
            )
            matches = [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

        if not matches:
            return "unmatched", None, None
        if len(matches) > 1:
            # Multiple candidates are unsafe to choose arbitrarily.
            return "ambiguous", None, None

        record = get_record(conn, schema, matches[0])
        source = "starbooks_old.tblmultimedia"
        if record is None:
            return "unmatched", None, None

    if record["is_transcribe"] is not None and int(record["is_transcribe"]) == 1:
        return "already_done", record["HoldingsID"], source

    return "pending", record["HoldingsID"], source


def transcribe(model, file: Path, language: str, beam_size: int, vad: bool) -> str:
    # PyAV probes the actual media contents. Files with a .dat extension
    # are passed directly; renaming/converting them is not required when
    # the internal audio/video format is supported by FFmpeg.
    segments, info = model.transcribe(
        str(file),
        task="transcribe",  # preserve the spoken language
        language=None if language == "auto" else language,
        beam_size=beam_size,
        vad_filter=vad,
    )
    LOGGER.info("Detected language for %s: %s", file.name, info.language)

    lines: list[str] = []
    for segment in segments:  # IMPORTANT: inference runs during iteration.
        line = " ".join(segment.text.split())
        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def save_transcript(conn, schema: str, holdings_id: Any, transcript: str) -> bool:
    if not transcript.strip():
        raise ValueError("Cannot save an empty transcript")

    # Video processing can take a long time. Refresh a timed-out DB connection.
    conn.ping(reconnect=True, attempts=3, delay=2)

    # Conditional UPDATE prevents overwriting a record completed in the interim.
    # A transaction lets us roll back if an invalid schema matches many rows.
    sql = (
        f"UPDATE {schema}.`records` "
        "SET `text_conversion` = %s, `is_transcribe` = 1 "
        "WHERE `HoldingsID` = %s "
        "AND (`is_transcribe` IS NULL OR `is_transcribe` <> 1)"
    )
    conn.start_transaction()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, (transcript, holdings_id))
            updated = cursor.rowcount
        finally:
            cursor.close()
        if updated > 1:
            raise ValueError(f"Update matched {updated} records for HoldingsID {holdings_id}")
        conn.commit()
        return updated == 1
    except BaseException:
        conn.rollback()
        raise


def setup_logger(folder: Path) -> Path:
    logfile = folder / "video_transcription.log"
    LOGGER.setLevel(logging.INFO)
    for handler in LOGGER.handlers:
        handler.close()
    LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(stream)
    LOGGER.addHandler(file_handler)
    return logfile


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch-transcribe local videos into starbooks.records.text_conversion."
    )
    parser.add_argument(
        "--folder", type=Path, default=Path(__file__).resolve().parent,
        help="Folder to scan (default: folder containing this script)",
    )
    parser.add_argument("--recursive", action="store_true", help="Also scan subfolders")
    parser.add_argument("--dry-run", action="store_true", help="Check DB matches, but do not transcribe or update")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N pending records (0 = no limit)")
    parser.add_argument("--model", default="small", help="tiny, base, small, medium, large-v3 (default: small)")
    parser.add_argument("--language", default="auto", help="Whisper language code e.g. tl, en, or auto")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--compute-type", default=None, help="Default int8 on CPU, float16 on CUDA")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="Disable silence filtering")
    parser.add_argument("--db-host", default=os.getenv("MYSQL_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=os.getenv("MYSQL_PORT", "3306"))
    parser.add_argument("--db-user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("MYSQL_PASSWORD", ""))
    parser.add_argument("--db-socket", default=None, help="Optional Unix MySQL socket for local authentication")
    parser.add_argument("--db-name", default="starbooks")
    parser.add_argument("--old-db-name", default="starbooks_old")
    args = parser.parse_args(argv)
    if args.limit < 0 or args.beam_size < 1:
        parser.error("--limit must be >= 0 and --beam-size must be >= 1")
    if not 1 <= args.db_port <= 65535:
        parser.error("--db-port must be between 1 and 65535")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    folder = args.folder.expanduser().resolve()

    try:
        current_schema = safe_identifier(args.db_name)
        old_schema = safe_identifier(args.old_db_name)
        files = scan_media(folder, args.recursive)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        log_path = setup_logger(folder)
    except OSError as exc:
        print(f"ERROR: Cannot create log in {folder}: {exc}", file=sys.stderr)
        return 1
    LOGGER.info("Scanning %s (%s): %d media files", folder, "recursive" if args.recursive else "top level", len(files))
    LOGGER.info("MySQL: %s@%s:%s / %s, fallback %s", args.db_user, args.db_host, args.db_port, args.db_name, args.old_db_name)
    if not files:
        LOGGER.warning("No supported video/audio/.dat files were found.")
        return 0

    try:
        conn = connect_mysql(args)
    except Exception as exc:
        LOGGER.error("Could not connect to MySQL: %s", exc)
        return 1

    stats = {"already_done": 0, "unmatched": 0, "ambiguous": 0, "duplicate": 0, "saved": 0, "failed": 0, "raced": 0}
    pending: list[PendingVideo] = []
    queued_ids: set[str] = set()
    try:
        # Resolve every file against MySQL BEFORE downloading/loading a model.
        for i, file in enumerate(files, 1):
            try:
                status, holdings_id, source = resolve_video(
                    conn, file.name, file.stem, current_schema, old_schema
                )
            except Exception as exc:
                LOGGER.error("[%d/%d] DATABASE ERROR %s: %s", i, len(files), file.name, exc)
                return 1

            if status == "already_done":
                stats["already_done"] += 1
                LOGGER.info("[%d/%d] SKIP (is_transcribe=1) %s => HoldingsID %s", i, len(files), file.name, holdings_id)
            elif status == "unmatched":
                stats["unmatched"] += 1
                LOGGER.warning("[%d/%d] UNMATCHED %s: no matching HoldingsID in records/legacy", i, len(files), file.name)
            elif status == "ambiguous":
                stats["ambiguous"] += 1
                LOGGER.warning("[%d/%d] AMBIGUOUS %s: legacy Filename maps to multiple HoldingsIDs", i, len(files), file.name)
            else:
                key = str(holdings_id)
                if key in queued_ids:
                    stats["duplicate"] += 1
                    LOGGER.warning("[%d/%d] DUPLICATE %s: HoldingsID %s already queued; skipping", i, len(files), file.name, holdings_id)
                    continue
                queued_ids.add(key)
                pending.append(PendingVideo(file=file, holdings_id=holdings_id, source=source))
                LOGGER.info("[%d/%d] PENDING %s => HoldingsID %s (via %s)", i, len(files), file.name, holdings_id, source)

        if args.limit:
            pending = pending[:args.limit]
        LOGGER.info("Database precheck finished: %d pending | %d already transcribed | %d unmatched | %d ambiguous | %d duplicates",
                    len(pending), stats["already_done"], stats["unmatched"], stats["ambiguous"], stats["duplicate"])
        if args.dry_run:
            LOGGER.info("DRY RUN: no Whisper model was loaded, no database records changed. Log: %s", log_path)
            return 0
        if not pending:
            LOGGER.info("No pending videos. Nothing to transcribe. Log: %s", log_path)
            return 0

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            LOGGER.error("Faster-Whisper missing. Install: pip install -U faster-whisper")
            return 1

        compute_type = args.compute_type or ("float16" if args.device == "cuda" else "int8")
        LOGGER.info("Loading model %s on %s (%s); first run may download the model", args.model, args.device, compute_type)
        try:
            model = WhisperModel(args.model, device=args.device, compute_type=compute_type)
        except Exception as exc:
            LOGGER.error("Whisper model could not be initialized: %s", exc)
            return 1

        for index, item in enumerate(pending, 1):
            LOGGER.info("[%d/%d] TRANSCRIBING %s => HoldingsID %s", index, len(pending), item.file.name, item.holdings_id)
            try:
                transcript = transcribe(model, item.file, args.language, args.beam_size, not args.no_vad)
                if not transcript:
                    stats["failed"] += 1
                    LOGGER.error("[%d/%d] FAILED %s: no spoken words detected; database unchanged", index, len(pending), item.file.name)
                    continue
                if save_transcript(conn, current_schema, item.holdings_id, transcript):
                    stats["saved"] += 1
                    LOGGER.info("[%d/%d] SAVED %s: %d characters => HoldingsID %s (is_transcribe=1)",
                                index, len(pending), item.file.name, len(transcript), item.holdings_id)
                else:
                    stats["raced"] += 1
                    LOGGER.warning("[%d/%d] NOT UPDATED %s: record already completed/removed by another process", index, len(pending), item.file.name)
            except KeyboardInterrupt:
                LOGGER.warning("Interrupted: completed files remain saved; the current file was not marked completed.")
                return 130
            except Exception as exc:
                stats["failed"] += 1
                LOGGER.error("[%d/%d] FAILED %s => HoldingsID %s: %s", index, len(pending), item.file.name, item.holdings_id, exc, exc_info=True)
                # Continue with other files; failed files remain is_transcribe != 1.

        LOGGER.info("FINISHED: saved=%d | skipped_done=%d | unmatched=%d | ambiguous=%d | duplicate=%d | failed=%d | concurrently_changed=%d",
                    stats["saved"], stats["already_done"], stats["unmatched"], stats["ambiguous"], stats["duplicate"], stats["failed"], stats["raced"])
        LOGGER.info("Log: %s", log_path)
        return 1 if stats["failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
