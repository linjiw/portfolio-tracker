import csv
import math
import tempfile
import unittest
from pathlib import Path

from scripts.artifact_io import (
    append_csv_row_private,
    atomic_write_bytes,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    ensure_private_directory,
)


class ArtifactIoTests(unittest.TestCase):
    def test_atomic_json_is_strict_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output" / "artifact.json"
            atomic_write_json(path, {"ok": 1})
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "ok": 1\n}\n')
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(ValueError):
                atomic_write_json(path, {"bad": math.nan})
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "ok": 1\n}\n')

    def test_atomic_text_replaces_last_known_good(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "report.md"
            path.write_text("old", encoding="utf-8")
            atomic_write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_atomic_bytes_replaces_private_binary_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "agent.plist"
            atomic_write_bytes(path, b"first")
            atomic_write_bytes(path, bytearray(b"second"))
            self.assertEqual(path.read_bytes(), b"second")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(TypeError):
                atomic_write_bytes(path, "not-bytes")
            self.assertEqual(path.read_bytes(), b"second")

    def test_atomic_csv_replaces_complete_private_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nested" / "rows.csv"
            atomic_write_csv(path, [{"a": 1, "b": "x"}], ["a", "b"])

            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [{"a": "1", "b": "x"}])
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            original = path.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                atomic_write_csv(
                    path,
                    [{"a": 2, "unexpected": True}],
                    ["a"],
                    extrasaction="raise",
                )
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_private_csv_append_keeps_existing_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log" / "events.csv"
            append_csv_row_private(path, {"id": 1}, ["id"])
            append_csv_row_private(path, {"id": 2}, ["id"])

            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [{"id": "1"}, {"id": "2"}])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            original = path.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                append_csv_row_private(path, {"id": 3, "unexpected": True}, ["id"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_explicit_private_directory_hardens_existing_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "secrets"
            path.mkdir(mode=0o755)
            ensure_private_directory(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
