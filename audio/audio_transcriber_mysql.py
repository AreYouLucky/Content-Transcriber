#!/usr/bin/env python3
"""Batch transcribe local audio files into STARBOOKS MySQL records.

Behavior:
  - Scan a folder (default: the folder beside this script) for audio files.
    Supported: .dat plus common audio extensions (.mp3, .wav, .m4a, ...).
  - The filename without its final extension is the HoldingsID, e.g.
    ABC-0001.dat -> HoldingsID 'ABC-0001'.
  - Look up starbooks.records by HoldingsID ONLY (no legacy/content_file
    fallback). Unmatched files are logged and skipped.
  - Skip records already marked is_transcribe = 1, before loading the model.
  - Transcribe speech with Faster-Whisper (.dat containers are read directly
    by FFmpeg/PyAV; no renaming needed).
  - Only after a nonempty, successful transcription, update
    records.text_conversion and records.is_transcribe = 1 together.
  - Never insert new records or mark silent/failed files as transcribed.

Install:
    python -m pip install -r requirements.txt

Examples:
    python audio_transcriber_mysql.py --folder "D:\\Audio" --dry-run
    python audio_transcriber_mysql.py --folder "D:\\Audio"
    python audio_transcriber_mysql.py --folder "D:\\Audio" --device cuda --model medium
    python audio_transcriber_mysql.py --folder "D:\\Audio" --language tl --limit 1

Database credentials may also be supplied through MYSQL_HOST, MYSQL_PORT,
MYSQL_USER, and MYSQL_PASSWORD environment variables. The password is empty
by default. Prefer an environment variable over the command line for secrets.
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

AUDIO_EXTENSIONS = {
    ".dat", ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".opus",
}

LOGGER = logging.getLogger("starbooks_audio_transcriber")


@dataclass(frozen=True)
class PendingAudio:
    file: Path
    holdings_id: Any


def safe_identifier(name: str) -> str:
    """Only permit simple MySQL schema names (no arbitrary SQL identifiers)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError(f"Invalid MySQL schema name: {name!r}")
    return f"`{name}`"


def scan_media(folder: Path, recursive: bool) -> list[Path]:
    if not folder.is_dir():
        raise NotADirectoryError(f"Audio folder does not exist: {folder}")
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
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


def resolve_audio(conn, stem: str, schema: str):
    """Match a filename stem to records.HoldingsID.

    Returns (status, HoldingsID) where status is one of
    'pending', 'already_done', 'unmatched', or 'ambiguous'.
    """
    sql = (
        f"SELECT `HoldingsID`, `is_transcribe` "
        f"FROM {schema}.`records` WHERE `HoldingsID` = %s LIMIT 2"
    )
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, (stem,))
        rows = cursor.fetchall()
    finally:
        cursor.close()

    if not rows:
        return "unmatched", None
    if len(rows) > 1:
        return "ambiguous", None

    record = rows[0]
    if record["is_transcribe"] is not None and int(record["is_transcribe"]) == 1:
        return "already_done", record["HoldingsID"]
    return "pending", record["HoldingsID"]


def transcribe(model, file: Path, language: str, beam_size: int, vad: bool) -> str:
    # PyAV probes the actual media contents, so .dat files are passed directly
    # when their internal audio format is supported by FFmpeg.
    segments, info = model.transcribe(
        str(file),
        task="transcribe",  # keep the spoken language; do not translate
        language=None if language == "auto" else language,
        beam_size=beam_size,
        vad_filter=vad,
    )
    LOGGER.info("Detected language for %s: %s", file.name, info.language)

    lines: list[str] = []
    for segment in segments:  # inference runs lazily during iteration
        line = " ".join(segment.text.split())
        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def save_transcript(conn, schema: str, holdings_id: Any, transcript: str) -> bool:
    if not transcript.strip():
        raise ValueError("Cannot save an empty transcript")

    # Long transcriptions can outlast the MySQL idle timeout.
    conn.ping(reconnect=True, attempts=3, delay=2)

    # Conditional UPDATE never overwrites a record completed in the meantime;
    # the transaction rolls back if more than one row would change.
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
    logfile = folder / "audio_transcription.log"
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
        description="Batch-transcribe HoldingsID-named audio files into starbooks.records.text_conversion."
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
        schema = safe_identifier(args.db_name)
        files = scan_media(folder, args.recursive)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        log_path = setup_logger(folder)
    except OSError as exc:
        print(f"ERROR: Cannot create log in {folder}: {exc}", file=sys.stderr)
        return 1
    LOGGER.info("Scanning %s (%s): %d audio files", folder, "recursive" if args.recursive else "top level", len(files))
    LOGGER.info("MySQL: %s@%s:%s / %s", args.db_user, args.db_host, args.db_port, args.db_name)
    if not files:
        LOGGER.warning("No supported audio/.dat files were found.")
        return 0

    try:
        conn = connect_mysql(args)
    except Exception as exc:
        LOGGER.error("Could not connect to MySQL: %s", exc)
        return 1

    stats = {"already_done": 0, "unmatched": 0, "ambiguous": 0, "duplicate": 0, "saved": 0, "failed": 0, "raced": 0}
    pending: list[PendingAudio] = []
    queued_ids: set[str] = set()
    try:
        # Resolve every file against MySQL BEFORE loading the Whisper model.
        for i, file in enumerate(files, 1):
            try:
                status, holdings_id = resolve_audio(conn, file.stem, schema)
            except Exception as exc:
                LOGGER.error("[%d/%d] DATABASE ERROR %s: %s", i, len(files), file.name, exc)
                return 1

            if status == "already_done":
                stats["already_done"] += 1
                LOGGER.info("[%d/%d] SKIP (is_transcribe=1) %s => HoldingsID %s", i, len(files), file.name, holdings_id)
            elif status == "unmatched":
                stats["unmatched"] += 1
                LOGGER.warning("[%d/%d] UNMATCHED %s: no records.HoldingsID = %r", i, len(files), file.name, file.stem)
            elif status == "ambiguous":
                stats["ambiguous"] += 1
                LOGGER.warning("[%d/%d] AMBIGUOUS %s: HoldingsID %r appears more than once in records", i, len(files), file.name, file.stem)
            else:
                key = str(holdings_id)
                if key in queued_ids:
                    stats["duplicate"] += 1
                    LOGGER.warning("[%d/%d] DUPLICATE %s: HoldingsID %s already queued; skipping", i, len(files), file.name, holdings_id)
                    continue
                queued_ids.add(key)
                pending.append(PendingAudio(file=file, holdings_id=holdings_id))
                LOGGER.info("[%d/%d] PENDING %s => HoldingsID %s", i, len(files), file.name, holdings_id)

        if args.limit:
            pending = pending[:args.limit]
        LOGGER.info("Database precheck finished: %d pending | %d already transcribed | %d unmatched | %d ambiguous | %d duplicates",
                    len(pending), stats["already_done"], stats["unmatched"], stats["ambiguous"], stats["duplicate"])
        if args.dry_run:
            LOGGER.info("DRY RUN: no Whisper model was loaded, no database records changed. Log: %s", log_path)
            return 0
        if not pending:
            LOGGER.info("No pending audio files. Nothing to transcribe. Log: %s", log_path)
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
                if save_transcript(conn, schema, item.holdings_id, transcript):
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
