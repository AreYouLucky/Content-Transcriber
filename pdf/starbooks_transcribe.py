#!/usr/bin/env python3
"""STARBOOKS: convert PDF and .dat document contents into records.text_conversion.

- Scans this script's directory by default (including immediate/nested folders).
- Each subfolder containing documents is one record; root documents are grouped
  by the filename portion preceding the first dot.
- Resolves HoldingsID first via starbooks.records, then (if necessary) via
  starbooks_old.tblmultimedia.Filename.
- Never extracts or overwrites a record that is already is_transcribe=1.
- Reads PDF text with PyMuPDF and pypdf; automatically OCRs scanned pages.
- Saves text_conversion and is_transcribe=1 in a single protected UPDATE.

Install: python -m pip install -r requirements.txt
Usage:   python starbooks_transcribe.py --dry-run
         python starbooks_transcribe.py --preview ATI0021.pdf --output preview.txt
         python starbooks_transcribe.py
"""

from __future__ import annotations

import argparse
import base64
import bz2
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import io
import json
import logging
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import pymupdf
    PYMUPDF_ERROR = ""
except ImportError as exc:
    pymupdf = None
    PYMUPDF_ERROR = str(exc)
    if exc.__context__:
        PYMUPDF_ERROR += f"; {exc.__context__}"

try:
    import pymysql
except ImportError:
    pymysql = None


# Form-feed separates pages without injecting text not present in the document.
PAGE_SEPARATOR = "\n\n\f\n\n"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
PAGE_PATTERN = re.compile(r"\.pdf\s*(\d+)\.(?:dat|pdf)$", re.IGNORECASE)
OTHER_PAGE_PATTERN = re.compile(r"\.(?:page|p)(\d+)\.(?:dat|pdf)$", re.IGNORECASE)
NUMBERED_FILE_PATTERN = re.compile(r"[_ .](\d+)\.(?:dat|pdf)$", re.IGNORECASE)
LOGGER = logging.getLogger("starbooks_transcriber")


class UnreadableDocument(Exception):
    """Unsupported, password-protected, corrupt document or failed OCR."""


class DatabaseMatchError(Exception):
    """Could not uniquely/safely match a document to a primary record."""


@dataclass(frozen=True)
class Job:
    label: str
    candidate_id: str
    files: tuple[Path, ...]


@dataclass(frozen=True)
class PDFOptions:
    ocr: str = "auto"
    language: str = "eng"
    dpi: int = 300
    min_text_chars: int = 40
    tessdata: Path | None = None
    engine: str = "auto"


def text_score(text: str) -> int:
    """Discount undecodable glyphs instead of mistaking them for usable text."""
    bad = sum(ch == "\ufffd" or (not ch.isprintable() and not ch.isspace()) for ch in text)
    return max(0, sum(ch.isalnum() for ch in text) - 5 * bad)


def find_tessdata(options: PDFOptions) -> str | None:
    """Use explicit/local language files; PyMuPDF can also discover Tesseract."""
    configured = options.tessdata or os.getenv("TESSDATA_PREFIX")
    local = Path(__file__).resolve().parent / "tessdata"
    folder = Path(configured).expanduser().resolve() if configured else local
    if configured or folder.is_dir():
        missing = [lang for lang in options.language.split("+")
                   if not (folder / f"{lang}.traineddata").is_file()]
        if missing:
            raise UnreadableDocument(
                f"OCR language data missing in {folder}: {', '.join(missing)}. "
                "See README.md or supply --tessdata PATH."
            )
        return str(folder)
    return None


class TextHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "section", "article", "header", "footer", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "canvas", "noscript"}:
            self.ignore_depth += 1
        elif not self.ignore_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "canvas", "noscript"}:
            self.ignore_depth = max(0, self.ignore_depth - 1)
        elif not self.ignore_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignore_depth:
            self.parts.append(data)


def is_document(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".pdf", ".dat"}


def initial_id(filename: str) -> str:
    """PHILRICE-0193.pdf1.dat -> PHILRICE-0193."""
    return filename.split(".", 1)[0].strip()


def page_number(path: Path) -> int | None:
    for pattern in (PAGE_PATTERN, OTHER_PAGE_PATTERN, NUMBERED_FILE_PATTERN):
        match = pattern.search(path.name)
        if match:
            return int(match.group(1))
    return None


def sort_files(files: list[Path]) -> tuple[Path, ...]:
    # Numerical sort: page2 precedes page10, not the other way around.
    return tuple(sorted(files, key=lambda p: (
        page_number(p) is None,
        page_number(p) if page_number(p) is not None else 0,
        p.name.casefold(),
    )))


def build_jobs(root: Path) -> list[Job]:
    """Any directory below root containing documents is one record.

    Documents directly in root are grouped by candidate HoldingsID to avoid
    marking one page as complete while other pages of the same ID remain.
    """
    jobs: list[Job] = []
    root_files: dict[str, list[Path]] = {}

    for current, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        folder = Path(current)
        paths = [folder / name for name in filenames if is_document(folder / name)]
        if not paths:
            continue

        if folder == root:
            for path in paths:
                key = initial_id(path.name)
                if key:
                    root_files.setdefault(key, []).append(path)
        else:
            jobs.append(Job(str(folder.relative_to(root)), folder.name, sort_files(paths)))

    for candidate_id, paths in sorted(root_files.items(), key=lambda kv: kv[0].casefold()):
        jobs.append(Job(candidate_id, candidate_id, sort_files(paths)))

    return sorted(jobs, key=lambda job: job.label.casefold())


def clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # Preserve paragraph/newline boundaries; only trim outside whitespace.
    return value.strip(" \t\n\f\ufeff")


def combine_pages(pages: list[str]) -> str:
    return PAGE_SEPARATOR.join(clean_text(page) for page in pages).strip(" \t\n\f")


def open_pypdf(source: Path | bytes):
    if PdfReader is None:
        raise UnreadableDocument("pypdf is unavailable; install requirements.txt")
    reader = PdfReader(io.BytesIO(source) if isinstance(source, bytes) else str(source), strict=False)
    # Many PDFs are encrypted but allow reading with an empty user password.
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnreadableDocument("PDF requires a password")
    return reader


def windows_ocr(source: Path | bytes, indexes: list[int], options: PDFOptions) -> dict[int, str]:
    """Call the Windows PDF renderer/OCR via ordinary PowerShell commands.

    Document paths and options are environment values, never command text.
    No execution-policy or application-control settings are changed.
    """
    powershell = shutil.which("powershell.exe") if os.name == "nt" else None
    if not powershell:
        raise UnreadableDocument(
            f"OCR requires PyMuPDF or Windows PowerShell; PyMuPDF import failed: {PYMUPDF_ERROR}"
        )
    helper = Path(__file__).resolve().with_name("windows_pdf_ocr.ps1")
    if not helper.is_file():
        raise UnreadableDocument(f"Windows OCR helper missing: {helper}")
    command = ("& {\n" + helper.read_text(encoding="utf-8") + "\n} "
               "-InputPdf $env:STARBOOKS_OCR_INPUT -PageIndexes $env:STARBOOKS_OCR_PAGES "
               "-Language $env:STARBOOKS_OCR_LANGUAGE -Dpi $env:STARBOOKS_OCR_DPI")
    with tempfile.TemporaryDirectory(prefix="starbooks_ocr_") as folder:
        if isinstance(source, bytes):
            path = Path(folder) / "document.pdf"
            path.write_bytes(source)
        else:
            path = source.resolve()
        env = dict(os.environ, STARBOOKS_OCR_INPUT=str(path),
                   STARBOOKS_OCR_PAGES=",".join(map(str, indexes)),
                   STARBOOKS_OCR_LANGUAGE=options.language,
                   STARBOOKS_OCR_DPI=str(options.dpi))
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                env=env, capture_output=True, encoding="utf-8", errors="replace",
                timeout=max(120, 60 * len(indexes)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UnreadableDocument(f"Windows OCR could not finish: {exc}") from exc
        if result.returncode:
            raise UnreadableDocument(f"Windows OCR failed: {result.stderr.strip()}")
        try:
            pages = json.loads(result.stdout.lstrip("\ufeff").strip())
            recognized = {int(item["index"]): clean_text(item["text"]) for item in pages}
        except (ValueError, TypeError, KeyError) as exc:
            raise UnreadableDocument("Windows OCR returned an invalid response") from exc
        if set(recognized) != set(indexes):
            raise UnreadableDocument("Windows OCR did not return every requested page")
        return recognized


def extract_pdf_pypdf(source: Path | bytes, options: PDFOptions) -> str:
    """Portable native text extraction, with Windows OCR when needed."""
    try:
        reader = open_pypdf(source)
        pages = []
        ocr_indexes = []
        for index, page in enumerate(reader.pages):
            failed = False
            try:
                text = clean_text(page.extract_text() or "")
            except Exception as exc:
                LOGGER.debug("pypdf page %d failed: %s", index + 1, exc)
                text, failed = "", True
            weak = text_score(text) < options.min_text_chars
            graphics = False
            if weak:
                try:
                    content = page.get_contents()
                    paint = {b"Do", b"INLINE IMAGE", b"S", b"s", b"f", b"F", b"f*",
                             b"B", b"B*", b"b", b"b*"}
                    graphics = content is not None and any(op in paint for _, op in content.operations)
                except Exception:
                    failed = True
            needs_ocr = (options.ocr == "always" or failed or (weak and graphics)
                         or "\ufffd" in text or (bool(text) and text_score(text) == 0))
            if needs_ocr:
                if options.ocr == "never":
                    raise UnreadableDocument(f"page {index + 1} needs OCR; disabled by --ocr never")
                ocr_indexes.append(index)
            pages.append(text)
        if ocr_indexes:
            label = str(source) if isinstance(source, Path) else "embedded PDF"
            LOGGER.info("Windows OCR %s | %d/%d pages | %s, %d DPI",
                        label, len(ocr_indexes), len(pages), options.language, options.dpi)
            recognized = windows_ocr(source, ocr_indexes, options)
            for index, text in recognized.items():
                if options.ocr == "always" or text_score(text) > text_score(pages[index]):
                    pages[index] = text
                if not pages[index]:
                    LOGGER.warning("%s page %d: OCR found no text (possibly blank/image only)",
                                   label, index + 1)
        return combine_pages(pages)
    except UnreadableDocument:
        raise
    except Exception as exc:
        raise UnreadableDocument(f"invalid or unsupported PDF: {exc}") from exc


def extract_pdf(source: Path | bytes, options: PDFOptions | None = None) -> str:
    """Prefer native text; recover weak pages with pypdf or local OCR.

    A page failure aborts the record, so it cannot be saved as complete with
    unread pages. Truly blank pages may remain empty after a successful OCR.
    """
    options = options or PDFOptions()
    label = str(source) if isinstance(source, Path) else "embedded PDF"
    if options.engine == "pymupdf" and pymupdf is None:
        raise UnreadableDocument(f"PyMuPDF could not load: {PYMUPDF_ERROR}")
    if pymupdf is None or options.engine == "windows":
        LOGGER.debug("Using pypdf/Windows reader (PyMuPDF: %s)", PYMUPDF_ERROR or "not selected")
        return extract_pdf_pypdf(source, options)

    # Read the original bytes intact, including any prefix before %PDF-.
    # Slicing off the prefix can invalidate the PDF's absolute object offsets.
    try:
        doc = (pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes)
               else pymupdf.open(str(source)))
    except Exception as exc:
        # Recover documents that one engine cannot open, but never bypass OCR
        # requirements or silently accept an unread image page.
        if PdfReader is not None and options.engine == "auto":
            try:
                LOGGER.warning("%s: using pypdf after PyMuPDF failed: %s", label, exc)
                return extract_pdf_pypdf(source, options)
            except UnreadableDocument as fallback_exc:
                raise UnreadableDocument(f"PyMuPDF: {exc}; fallback: {fallback_exc}") from fallback_exc
        raise UnreadableDocument(f"cannot open PDF: {exc}") from exc

    try:
        with doc:
            if doc.needs_pass and not doc.authenticate(""):
                raise UnreadableDocument("PDF requires a password")
            reader = None
            tried_pypdf = False
            pages = []
            for index, page in enumerate(doc):
                try:
                    native_failed = False
                    try:
                        native = clean_text(page.get_text("text", sort=True))
                    except Exception as exc:
                        LOGGER.debug("%s page %d: native extraction failed: %s", label, index + 1, exc)
                        native_failed = True
                        native = ""
                    score = text_score(native)
                    if score < options.min_text_chars and options.ocr != "always":
                        if not tried_pypdf:
                            tried_pypdf = True
                            try:
                                reader = open_pypdf(source)
                            except Exception as exc:
                                LOGGER.debug("%s: pypdf unavailable: %s", label, exc)
                        if reader is not None:
                            try:
                                alternate = clean_text(reader.pages[index].extract_text() or "")
                                if text_score(alternate) > score:
                                    native, score = alternate, text_score(alternate)
                                    native_failed = False
                            except Exception as exc:
                                LOGGER.debug("%s page %d: pypdf failed: %s", label, index + 1, exc)

                    # Short title pages are valid text. OCR short pages only
                    # when they have images, drawings, or undecodable glyphs.
                    needs_ocr = options.ocr == "always" or native_failed or (
                        score < options.min_text_chars and (
                            bool(page.get_images()) or bool(page.get_image_info()) or
                            bool(page.get_drawings()) or "\ufffd" in native
                        )
                    )
                    if needs_ocr:
                        if options.ocr == "never":
                            raise UnreadableDocument(
                                "scanned or unreadable page; OCR disabled by --ocr never"
                            )
                        LOGGER.info("OCR %s | page %d/%d | %s, %d DPI",
                                    label, index + 1, len(doc), options.language, options.dpi)
                        tessdata = find_tessdata(options)
                        try:
                            textpage = page.get_textpage_ocr(
                                language=options.language, dpi=options.dpi,
                                full=True, tessdata=tessdata,
                            )
                            recognized = clean_text(page.get_text("text", textpage=textpage, sort=True))
                        except Exception as exc:
                            raise UnreadableDocument(
                                f"OCR failed: {exc}. Check --tessdata and --ocr-language "
                                "(see README.md)"
                            ) from exc
                        if options.ocr == "always" or text_score(recognized) > score:
                            native = recognized
                        if not native:
                            LOGGER.warning("%s page %d: OCR found no text (possibly blank/image only)",
                                           label, index + 1)
                    pages.append(native)
                    LOGGER.debug("%s page %d: %d characters", label, index + 1, len(native))
                except Exception as exc:
                    raise UnreadableDocument(f"page {index + 1}: {exc}") from exc
            return combine_pages(pages)
    except UnreadableDocument:
        raise
    except Exception as exc:
        raise UnreadableDocument(f"invalid or unsupported PDF: {exc}") from exc


def looks_like_plain_text(s: str) -> bool:
    if not s or "\x00" in s:
        return False
    sample = s[:8192]
    allowed = sum(ch.isprintable() or ch in "\n\r\t\f" for ch in sample)
    letters = sum(ch.isalpha() for ch in sample)
    return allowed / len(sample) >= 0.98 and letters / len(sample) >= 0.12


def decode_plain_text(data: bytes) -> str:
    if not data:
        raise UnreadableDocument("empty file")

    # Explicit encodings first; avoid treating random bytes as legitimate text.
    encodings = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    elif data.startswith(b"\xef\xbb\xbf"):
        encodings.append("utf-8-sig")
    else:
        sample = data[:512]
        if len(sample) > 16:
            odd_nulls = sample[1::2].count(0)
            even_nulls = sample[0::2].count(0)
            if odd_nulls > len(sample) // 6:
                encodings.append("utf-16-le")
            elif even_nulls > len(sample) // 6:
                encodings.append("utf-16-be")
        encodings.extend(["utf-8", "cp1252"])

    for encoding in encodings:
        try:
            output = data.decode(encoding)
        except UnicodeError:
            continue
        if looks_like_plain_text(output):
            return clean_text(output)
    raise UnreadableDocument("DAT is binary or uses an unsupported text encoding/container")


def decompress_zlib(data: bytes, max_bytes: int, wbits: int) -> bytes:
    try:
        decoder = zlib.decompressobj(wbits)
        output = decoder.decompress(data, max_bytes + 1)
        if len(output) > max_bytes or not decoder.eof or decoder.unconsumed_tail:
            raise UnreadableDocument("decompressed DAT exceeds limit or is incomplete")
        return output + decoder.flush()
    except zlib.error as exc:
        raise UnreadableDocument(f"invalid compressed data: {exc}") from exc


def decode_dat_payload(data: bytes, max_bytes: int, depth: int = 0,
                       options: PDFOptions | None = None) -> str:
    """Best-effort decoding based on content, not the .dat extension.

    Supported: PDF bytes, UTF-8/UTF-16/Windows-1252 text, HTML, gzip, zlib,
    bz2, xz, zip entries and base64-encoded PDF. Proprietary/encrypted DAT
    must be decoded with the format-specific tool first.
    """
    if depth > 3:
        raise UnreadableDocument("DAT compression/wrapper nesting too deep")
    if not data:
        raise UnreadableDocument("empty DAT")
    if len(data) > max_bytes:
        raise UnreadableDocument(f"DAT is larger than configured {max_bytes // 1024**2} MB limit")

    if data.startswith(b"\x1f\x8b"):
        return decode_dat_payload(decompress_zlib(data, max_bytes, 31), max_bytes, depth + 1, options)
    if len(data) >= 2 and data[0] & 15 == 8 and (data[0] * 256 + data[1]) % 31 == 0:
        return decode_dat_payload(decompress_zlib(data, max_bytes, 15), max_bytes, depth + 1, options)
    if data.startswith(b"BZh"):
        try:
            decoder = bz2.BZ2Decompressor()
            output = decoder.decompress(data, max_length=max_bytes + 1)
            if len(output) > max_bytes or not decoder.eof:
                raise UnreadableDocument("bz2 payload too large or incomplete")
            return decode_dat_payload(output, max_bytes, depth + 1, options)
        except OSError as exc:
            raise UnreadableDocument(f"invalid bz2 content: {exc}") from exc
    if data.startswith(b"\xfd7zXZ\x00"):
        try:
            decoder = lzma.LZMADecompressor()
            output = decoder.decompress(data, max_length=max_bytes + 1)
            if len(output) > max_bytes or not decoder.eof:
                raise UnreadableDocument("xz payload too large or incomplete")
            return decode_dat_payload(output, max_bytes, depth + 1, options)
        except lzma.LZMAError as exc:
            raise UnreadableDocument(f"invalid xz content: {exc}") from exc

    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = sorted(
                    (i for i in archive.infolist() if not i.is_dir() and
                     Path(i.filename).suffix.lower() in {".txt", ".html", ".htm", ".pdf", ".dat"}),
                    key=lambda i: (page_number(Path(i.filename)) is None,
                                   page_number(Path(i.filename)) or 0,
                                   i.filename.casefold()),
                )
                if not members:
                    raise UnreadableDocument("ZIP DAT contains no supported document entries")
                if sum(i.file_size for i in members) > max_bytes:
                    raise UnreadableDocument("ZIP contents exceed decompression limit")
                pages = [decode_dat_payload(archive.read(item), max_bytes, depth + 1, options)
                         for item in members]
                return combine_pages(pages)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise UnreadableDocument(f"invalid ZIP DAT: {exc}") from exc

    # Check containers first: an uncompressed ZIP entry can contain a PDF
    # signature within the first kilobyte without the ZIP itself being a PDF.
    pdf_pos = data.find(b"%PDF-", 0, 1024)
    if pdf_pos >= 0:
        return extract_pdf(data, options)

    # Restrict base64 detection to PDF/known wrapped data so real text isn't
    # accidentally interpreted as encoded content.
    stripped = re.sub(rb"\s", b"", data)
    if len(stripped) >= 64 and len(stripped) % 4 == 0 and re.fullmatch(rb"[A-Za-z0-9+/]*={0,2}", stripped):
        try:
            decoded = base64.b64decode(stripped, validate=True)
            if decoded.startswith((b"%PDF-", b"\x1f\x8b", b"PK\x03\x04")):
                return decode_dat_payload(decoded, max_bytes, depth + 1, options)
        except (ValueError, base64.binascii.Error):
            pass

    plain = decode_plain_text(data)
    if re.search(r"<!doctype\s+html|<html\b|<body\b|<p\b|<div\b|<h[1-6]\b", plain[:1024], re.I):
        parser = TextHTMLParser()
        parser.feed(plain)
        plain = clean_text(unescape("".join(parser.parts)))
    return plain


def extract_dat(path: Path, max_bytes: int, options: PDFOptions | None = None) -> str:
    if path.stat().st_size > max_bytes:
        raise UnreadableDocument("DAT exceeds configured size limit")
    try:
        return decode_dat_payload(path.read_bytes(), max_bytes, options=options)
    except OSError as exc:
        raise UnreadableDocument(f"cannot read DAT file: {exc}") from exc


def extract_job(job: Job, max_bytes: int, options: PDFOptions | None = None) -> str:
    # Process every document in the record; do not silently discard any file.
    # A user should check for duplicate content when PDF and DAT coexist.
    if any(p.suffix.lower() == ".pdf" for p in job.files) and any(
        p.suffix.lower() == ".dat" for p in job.files
    ):
        LOGGER.warning("%s: PDF and DAT are both present; including all files (check duplicates)", job.label)

    parts: list[str] = []
    for file in job.files:
        try:
            text = (extract_pdf(file, options) if file.suffix.lower() == ".pdf"
                    else extract_dat(file, max_bytes, options))
            parts.append(text)
            LOGGER.debug("Read %s (%d characters)", file, len(text))
        except (UnreadableDocument, OSError) as exc:
            raise UnreadableDocument(f"{file}: {exc}") from exc
    combined = combine_pages(parts)
    # If ALL pages are empty, don't mark success (often a scanned/image PDF).
    if not combined.strip():
        raise UnreadableDocument("no text found, even after configured PDF/OCR extraction")
    return combined


def mysql_connect(args: argparse.Namespace):
    if pymysql is None:
        raise RuntimeError("PyMySQL is missing; run python -m pip install -r requirements.txt")
    kwargs = {
        "user": args.db_user,
        "password": os.getenv("STARBOOKS_DB_PASSWORD", ""),
        "database": "starbooks",
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
        "connect_timeout": 15,
        "read_timeout": 60,
        "write_timeout": 60,
        "max_allowed_packet": 64 * 1024 * 1024,
    }
    if args.db_socket:
        kwargs["unix_socket"] = args.db_socket
    else:
        kwargs["host"] = args.db_host
        kwargs["port"] = args.db_port
    return pymysql.connect(**kwargs)


def assert_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT HoldingsID, is_transcribe, text_conversion FROM starbooks.records LIMIT 0")
        cur.execute("SELECT Filename, HoldingsID FROM starbooks_old.tblmultimedia LIMIT 0")


def lookup_record(conn, holdings_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT HoldingsID, is_transcribe FROM starbooks.records "
            "WHERE HoldingsID = %s LIMIT 2", (holdings_id,)
        )
        rows = cur.fetchall()
    if len(rows) > 1:
        raise DatabaseMatchError(f"duplicate HoldingsID in starbooks.records: {holdings_id}")
    return rows[0] if rows else None


def lookup_old_id(conn, filename: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT HoldingsID FROM starbooks_old.tblmultimedia "
            "WHERE Filename = %s AND HoldingsID IS NOT NULL LIMIT 2", (filename,)
        )
        rows = cur.fetchall()
    ids = {str(row["HoldingsID"]).strip() for row in rows if row["HoldingsID"] is not None}
    if len(ids) > 1:
        raise DatabaseMatchError(f"ambiguous legacy filename: {filename} -> {ids}")
    return next(iter(ids)) if ids else None


def resolve_record(conn, job: Job) -> tuple[dict | None, str]:
    """Return record and lookup source, trying file names only on initial miss."""
    record = lookup_record(conn, job.candidate_id)
    if record:
        return record, "primary"

    # Every file in this job must resolve consistently. Returning the first
    # match can mark the wrong record complete for a mixed/misnamed folder.
    recovered_ids: set[str] = set()
    matched_filename = ""
    for file in job.files:
        recovered_id = lookup_old_id(conn, file.name)
        if recovered_id:
            recovered_ids.add(recovered_id)
            matched_filename = file.name
    if len(recovered_ids) > 1:
        raise DatabaseMatchError(
            f"conflicting legacy IDs for {job.label}: {sorted(recovered_ids)}"
        )
    if recovered_ids:
        recovered_id = next(iter(recovered_ids))
        record = lookup_record(conn, recovered_id)
        if record:
            return record, f"legacy ({matched_filename})"
        LOGGER.warning("%s: legacy ID %s missing from starbooks.records",
                       job.label, recovered_id)
    return None, "not found in primary/legacy tables"


def is_completed(record: dict) -> bool:
    try:
        return int(record["is_transcribe"] or 0) == 1
    except (ValueError, TypeError):
        return False


def update_record(conn, holdings_id: str, content: str) -> int:
    """Atomic protected update; re-check the status in SQL to avoid overwrites."""
    if not content.strip():
        raise ValueError("Cannot save empty document text")
    # Extraction/OCR may outlast the server's idle connection timeout.
    conn.ping(reconnect=True)
    conn.begin()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE starbooks.records "
                "SET text_conversion = %s, is_transcribe = 1 "
                "WHERE HoldingsID = %s AND (is_transcribe IS NULL OR is_transcribe <> 1)",
                (content, holdings_id),
            )
            updated = cur.rowcount
        if updated > 1:
            raise DatabaseMatchError(f"update matched {updated} records for {holdings_id}")
        conn.commit()
        return updated
    except BaseException:
        conn.rollback()
        raise


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STARBOOKS PDF/.dat text transcription")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="Folder to scan (default: folder containing this script)")
    parser.add_argument("--db-host", default=os.getenv("STARBOOKS_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=os.getenv("STARBOOKS_DB_PORT", "3306"))
    parser.add_argument("--db-user", default=os.getenv("STARBOOKS_DB_USER", "root"))
    parser.add_argument("--db-socket", default=os.getenv("STARBOOKS_DB_SOCKET", ""),
                        help="Optional Unix MySQL socket; overrides host/port")
    parser.add_argument("--max-dat-mb", type=int, default=256,
                        help="Maximum size of each DAT/decompressed DAT (default: 256 MB)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                        help="Resolve database IDs and status without extracting/updating")
    mode.add_argument("--preview", type=Path, metavar="FILE",
                      help="Extract one PDF/DAT locally without connecting to MySQL")
    parser.add_argument("--output", type=Path, metavar="FILE",
                        help="Save --preview text as UTF-8 (default: print text)")
    parser.add_argument("--ocr", choices=("auto", "never", "always"), default="auto",
                        help="OCR weak/scanned pages, no pages, or all pages (default: auto)")
    parser.add_argument("--ocr-engine", choices=("auto", "pymupdf", "windows"), default="auto",
                        help="PDF/OCR engine (auto uses Windows if PyMuPDF cannot load)")
    parser.add_argument("--ocr-language", default="eng",
                        help="Tesseract languages, e.g. eng or eng+fil (default: eng)")
    parser.add_argument("--ocr-dpi", type=int, default=300,
                        help="Resolution for OCR (default: 300 DPI)")
    parser.add_argument("--ocr-min-chars", type=int, default=40,
                        help="Try OCR on image/drawing pages below this text score (default: 40)")
    parser.add_argument("--tessdata", type=Path,
                        help="Folder with OCR .traineddata files (default: local tessdata or Tesseract)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after this many jobs (0 = all)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.db_port <= 65535:
        parser.error("--db-port must be between 1 and 65535")
    if args.max_dat_mb <= 0 or args.limit < 0:
        parser.error("--max-dat-mb must be positive; --limit cannot be negative")
    if args.output and not args.preview:
        parser.error("--output requires --preview")
    if args.ocr_dpi < 72 or args.ocr_dpi > 600:
        parser.error("--ocr-dpi must be between 72 and 600")
    if args.ocr_min_chars < 1:
        parser.error("--ocr-min-chars must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", args.ocr_language):
        parser.error("--ocr-language must contain language codes such as eng or eng+fil")
    return args


def configure_logging(root: Path, verbose: bool) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = root / f"starbooks_transcribe_{timestamp}.log"
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in LOGGER.handlers:
        handler.close()
    LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    logfile = logging.FileHandler(log_path, encoding="utf-8")
    logfile.setFormatter(formatter)
    LOGGER.addHandler(logfile)
    return log_path


def main(argv=None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    try:
        log_path = configure_logging(root, args.verbose)
    except OSError as exc:
        print(f"Cannot create log in {root}: {exc}", file=sys.stderr)
        return 2
    options = PDFOptions(args.ocr, args.ocr_language, args.ocr_dpi,
                         args.ocr_min_chars, args.tessdata, args.ocr_engine)
    if args.preview:
        source = args.preview.expanduser().resolve()
        try:
            if not is_document(source):
                raise UnreadableDocument(f"not a PDF/DAT file: {source}")
            if args.output and args.output.expanduser().resolve() == source:
                raise UnreadableDocument("--output must differ from the input document")
            job = Job(source.name, initial_id(source.name), (source,))
            content = extract_job(job, args.max_dat_mb * 1024 * 1024, options)
            if args.output:
                output = args.output.expanduser().resolve()
                output.write_text(content, encoding="utf-8")
                LOGGER.info("Preview saved: %s | %d characters | no database connection", output, len(content))
            else:
                sys.stdout.reconfigure(encoding="utf-8")
                print(content)
            return 0
        except (UnreadableDocument, OSError) as exc:
            LOGGER.error("Preview failed: %s", exc)
            return 1

    jobs = build_jobs(root)
    if args.limit:
        jobs = jobs[:args.limit]
    LOGGER.info("Root: %s | jobs: %d | dry run: %s", root, len(jobs), args.dry_run)
    if not jobs:
        LOGGER.info("No .pdf or .dat documents found; log: %s", log_path)
        return 0

    conn = None
    try:
        conn = mysql_connect(args)
        assert_schema(conn)
    except Exception as exc:
        if conn is not None:
            conn.close()
        LOGGER.error("Database connection/schema failed: %s", exc)
        return 2

    counts: Counter[str] = Counter()
    try:
        for index, job in enumerate(jobs, start=1):
            try:
                conn.ping(reconnect=True)
                record, source = resolve_record(conn, job)
                if record is None:
                    counts["unmatched"] += 1
                    LOGGER.warning("[%d/%d] UNMATCHED %s: %s", index, len(jobs), job.label, source)
                    continue

                holdings_id = str(record["HoldingsID"])
                if is_completed(record):
                    counts["skipped"] += 1
                    LOGGER.info("[%d/%d] SKIP %s -> %s (is_transcribe=1)",
                                index, len(jobs), job.label, holdings_id)
                    continue

                if args.dry_run:
                    counts["ready"] += 1
                    LOGGER.info("[%d/%d] READY %s -> %s [%s], files=%d",
                                index, len(jobs), job.label, holdings_id, source, len(job.files))
                    continue

                content = extract_job(job, args.max_dat_mb * 1024 * 1024, options)
                updated = update_record(conn, holdings_id, content)
                if updated == 1:
                    counts["success"] += 1
                    LOGGER.info("[%d/%d] SUCCESS %s -> %s | %d chars | %d files [%s]",
                                index, len(jobs), job.label, holdings_id,
                                len(content), len(job.files), source)
                elif updated == 0:
                    counts["skipped"] += 1
                    LOGGER.warning("[%d/%d] SKIP %s: record was completed by another process",
                                   index, len(jobs), job.label)
                else:
                    # Multiple rows were updated, indicating an invalid schema.
                    raise DatabaseMatchError(f"update matched {updated} records for {holdings_id}")
            except (UnreadableDocument, DatabaseMatchError, pymysql.MySQLError, OSError) as exc:
                counts["failed"] += 1
                LOGGER.error("[%d/%d] FAILED %s: %s", index, len(jobs), job.label, exc)
            except Exception:
                counts["failed"] += 1
                LOGGER.exception("[%d/%d] FAILED %s (unexpected error)", index, len(jobs), job.label)
    finally:
        conn.close()

    LOGGER.info("Done: success=%d skipped=%d unmatched=%d failed=%d ready=%d | log: %s",
                counts["success"], counts["skipped"], counts["unmatched"],
                counts["failed"], counts["ready"], log_path)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
