"""Regression checks without live MySQL, model downloads, or archive writes."""

import contextlib
import gzip
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pdf = load_module("pdf_transcriber", "pdf/starbooks_transcribe.py")
video = load_module("video_transcriber", "video/video_transcriber_mysql.py")


class SQLiteCursor:
    """Run the production guarded UPDATE against a real transactional database."""

    def __init__(self, database):
        self.cursor = database.cursor()

    def execute(self, sql, parameters):
        sql = sql.replace("starbooks.", "").replace("`starbooks`.", "")
        self.cursor.execute(sql.replace("%s", "?"), parameters)

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def close(self):
        self.cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SQLiteConnection:
    def __init__(self):
        self.database = sqlite3.connect(":memory:", isolation_level=None)
        self.database.execute(
            "CREATE TABLE records (HoldingsID TEXT, text_conversion TEXT, is_transcribe INTEGER)"
        )
        self.ping = Mock()

    def cursor(self):
        return SQLiteCursor(self.database)

    def begin(self):
        self.database.execute("BEGIN")

    start_transaction = begin

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.database.rollback()

    def insert(self, status=None):
        self.database.execute("INSERT INTO records VALUES ('BOOK', 'original', ?)", (status,))

    def rows(self):
        return self.database.execute("SELECT text_conversion, is_transcribe FROM records").fetchall()


class GuardedWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = SQLiteConnection()
        self.addCleanup(self.conn.database.close)

    def writers(self):
        return (
            lambda content: pdf.update_record(self.conn, "BOOK", content),
            lambda content: video.save_transcript(self.conn, "`starbooks`", "BOOK", content),
        )

    def test_success_commits_text_and_completion(self):
        for writer in self.writers():
            with self.subTest(writer=writer):
                self.conn.database.execute("DELETE FROM records")
                self.conn.insert()
                self.assertEqual(writer("new transcript"), 1)
                self.assertEqual(self.conn.rows(), [("new transcript", 1)])
                self.assertFalse(self.conn.database.in_transaction)
        self.assertEqual(self.conn.ping.call_count, 2)

    def test_completed_record_is_never_overwritten(self):
        self.conn.insert(1)
        for writer in self.writers():
            self.assertEqual(writer("replacement"), 0)
            self.assertEqual(self.conn.rows(), [("original", 1)])

    def test_duplicate_rows_roll_back_every_change(self):
        self.conn.insert()
        self.conn.insert(0)
        for writer in self.writers():
            with self.assertRaises((pdf.DatabaseMatchError, ValueError)):
                writer("must not persist")
            self.assertEqual(self.conn.rows(), [("original", None), ("original", 0)])
            self.assertFalse(self.conn.database.in_transaction)

    def test_database_error_rolls_back(self):
        self.conn.database.execute("DROP TABLE records")
        for writer in self.writers():
            with self.assertRaises(sqlite3.OperationalError):
                writer("text")
            self.assertFalse(self.conn.database.in_transaction)

    def test_empty_text_rejected_before_database_access(self):
        for writer in self.writers():
            with self.assertRaises(ValueError):
                writer(" \n\t")
        self.conn.ping.assert_not_called()


class MatchingTests(unittest.TestCase):
    def test_pdf_conflicting_legacy_files_fail(self):
        job = pdf.Job("folder", "folder", (Path("a.dat"), Path("b.dat")))
        with patch.object(pdf, "lookup_record", return_value=None), patch.object(
            pdf, "lookup_old_id", side_effect=["A", "B"]
        ):
            with self.assertRaises(pdf.DatabaseMatchError):
                pdf.resolve_record(Mock(), job)

    def test_pdf_consistent_legacy_files_resolve(self):
        job = pdf.Job("folder", "folder", (Path("a.dat"), Path("b.dat")))
        record = {"HoldingsID": "A", "is_transcribe": 0}
        with patch.object(pdf, "lookup_record", side_effect=[None, record]), patch.object(
            pdf, "lookup_old_id", side_effect=["A", "A"]
        ):
            self.assertEqual(pdf.resolve_record(Mock(), job)[0], record)

    def test_pdf_primary_match_does_not_query_legacy(self):
        record = {"HoldingsID": "A", "is_transcribe": 1}
        with patch.object(pdf, "lookup_record", return_value=record), patch.object(
            pdf, "lookup_old_id"
        ) as legacy:
            self.assertEqual(pdf.resolve_record(Mock(), pdf.Job("A", "A", ()))[0], record)
            legacy.assert_not_called()

    def test_video_duplicate_current_ids_are_rejected(self):
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = [{"HoldingsID": "A"}] * 2
        with self.assertRaises(ValueError):
            video.get_record(conn, "`starbooks`", "A")
        conn.cursor.return_value.close.assert_called_once()

    def test_video_completed_record_skips_legacy(self):
        record = {"HoldingsID": "A", "is_transcribe": 1}
        conn = Mock()
        with patch.object(video, "get_record", return_value=record):
            self.assertEqual(video.resolve_video(conn, "A.dat", "A", "s", "old")[0], "already_done")
        conn.cursor.assert_not_called()

    def test_video_ambiguous_legacy_mapping_is_skipped(self):
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = [("A",), ("B",)]
        with patch.object(video, "get_record", return_value=None):
            self.assertEqual(video.resolve_video(conn, "file.dat", "file", "s", "old")[0], "ambiguous")


class ExtractionTests(unittest.TestCase):
    def test_pdf_pages_grouped_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("BOOK.pdf10.dat", "BOOK.pdf2.dat", "BOOK.pdf1.dat"):
                (root / name).touch()
            (root / ".venv").mkdir()
            (root / ".venv" / "ignore.pdf").touch()
            jobs = pdf.build_jobs(root)
            self.assertEqual(len(jobs), 1)
            self.assertEqual([p.name for p in jobs[0].files], ["BOOK.pdf1.dat", "BOOK.pdf2.dat", "BOOK.pdf10.dat"])

    def test_dat_decodes_text_and_gzip(self):
        text = "A complete paragraph of archived document text."
        for data in (text.encode(), gzip.compress(text.encode())):
            self.assertEqual(pdf.decode_dat_payload(data, 1024), text)

    def test_dat_decompression_limit_is_enforced(self):
        with self.assertRaises(pdf.UnreadableDocument):
            pdf.decode_dat_payload(gzip.compress(b"large archive text " * 1000), 512)

    def test_video_normalizes_segments_and_preserves_language(self):
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(text=" Hello   world "), SimpleNamespace(text=" "), SimpleNamespace(text="Next line")]),
            SimpleNamespace(language="en"),
        )
        self.assertEqual(video.transcribe(model, Path("a.mp4"), "auto", 5, True), "Hello world\nNext line")
        self.assertEqual(model.transcribe.call_args.kwargs["task"], "transcribe")
        self.assertIsNone(model.transcribe.call_args.kwargs["language"])

    def test_video_lazy_inference_failure_is_not_partial_success(self):
        def segments():
            yield SimpleNamespace(text="partial text")
            raise RuntimeError("decoder failed")

        model = Mock()
        model.transcribe.return_value = segments(), SimpleNamespace(language="en")
        with self.assertRaises(RuntimeError):
            video.transcribe(model, Path("bad.dat"), "en", 5, True)


class ArgumentTests(unittest.TestCase):
    def test_invalid_ports_report_cli_error_without_traceback(self):
        for module, variable in ((pdf, "STARBOOKS_DB_PORT"), (video, "MYSQL_PORT")):
            for value in ("invalid", "0", "65536"):
                with self.subTest(module=module.__name__, value=value), patch.dict(
                    os.environ, {variable: value}
                ), contextlib.redirect_stderr(io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as result:
                        module.parse_args([])
                    self.assertEqual(result.exception.code, 2)
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_pdf_invalid_limits_and_output_rejected(self):
        for args in (["--limit", "-1"], ["--max-dat-mb", "0"], ["--output", "a.txt"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                pdf.parse_args(args)

    def test_video_rejects_unsafe_schema_names(self):
        with self.assertRaises(ValueError):
            video.safe_identifier("starbooks; DROP TABLE records")


if __name__ == "__main__":
    unittest.main()
