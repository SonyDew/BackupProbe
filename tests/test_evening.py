"""Regression checks for archive boundaries and portable member names."""
import gzip
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from backup_probe.core import inspect, main

LIMITS = {"input": 2**31, "member": 2**29, "total": 2**31, "entries": 10000}


class EveningRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def probe(self, name, kind):
        return inspect(self.root / name, kind, LIMITS, 5.0)

    def make_tar(self, name="data.tar", mode="w", format=tarfile.PAX_FORMAT):
        path = self.root / name
        with tarfile.open(path, mode, format=format) as archive:
            item = tarfile.TarInfo("folder/data.txt")
            item.size = 2
            archive.addfile(item, io.BytesIO(b"ok"))
        return path

    def test_tar_nonzero_padding_before_last_two_blocks(self):
        path = self.make_tar()
        raw = bytearray(path.read_bytes())
        raw[2048] = 0x58
        path.write_bytes(raw)
        self.assertEqual(self.probe(path.name, "tar")["issue"], "invalid_tar_terminator")

    def test_tar_nonzero_first_end_block(self):
        path = self.make_tar()
        raw = bytearray(path.read_bytes())
        raw[1024] = 0x58
        path.write_bytes(raw)
        self.assertEqual(self.probe(path.name, "tar")["issue"], "invalid_tar_terminator")

    def test_tar_nonzero_after_first_end_block(self):
        path = self.make_tar()
        raw = bytearray(path.read_bytes())
        raw[1536] = 0x58
        path.write_bytes(raw)
        self.assertEqual(self.probe(path.name, "tar")["issue"], "invalid_tar_terminator")

    def test_gzip_tar_hidden_bytes(self):
        raw = bytearray(self.make_tar().read_bytes())
        raw[2048] = 0x58
        (self.root / "data.tar.gz").write_bytes(gzip.compress(raw))
        self.assertEqual(self.probe("data.tar.gz", "tar")["issue"], "invalid_tar_terminator")

    def test_empty_tar(self):
        with tarfile.open(self.root / "empty.tar", "w"):
            pass
        self.assertEqual(self.probe("empty.tar", "tar")["status"], "ok")

    def test_tar_pax_and_gnu_long_names(self):
        for fmt in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
            with self.subTest(fmt=fmt):
                p = self.root / f"long-{fmt}.tar"
                with tarfile.open(p, "w", format=fmt) as archive:
                    item = tarfile.TarInfo("deep/" + "segment" * 25 + "/a.txt")
                    item.size = 3
                    archive.addfile(item, io.BytesIO(b"abc"))
                self.assertEqual(self.probe(p.name, "tar")["status"], "ok")

    def test_compressed_tar_pax_long_name(self):
        path = self.root / "pax.tar.gz"
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            item = tarfile.TarInfo("nested/" + "part" * 50 + "/file.txt")
            item.size = 2
            archive.addfile(item, io.BytesIO(b"ok"))
        self.assertEqual(self.probe(path.name, "tar")["status"], "ok")

    def test_gzip_tar_nonzero_first_terminator_block(self):
        raw = bytearray(self.make_tar().read_bytes())
        raw[1024] = 0x58
        (self.root / "bad.tar.gz").write_bytes(gzip.compress(raw))
        self.assertEqual(self.probe("bad.tar.gz", "tar")["issue"], "invalid_tar_terminator")

    def test_zip_directory_local_header_corruption(self):
        p = self.root / "dir.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("folder/", b"")
        raw = bytearray(p.read_bytes())
        raw[0] ^= 0xFF
        p.write_bytes(raw)
        self.assertEqual(self.probe(p.name, "zip")["issue"], "invalid_zip")

    def test_zip_valid_directory(self):
        p = self.root / "dir.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("folder/", b"")
        self.assertEqual(self.probe(p.name, "zip")["status"], "ok")

    def test_zip_casefold_duplicate(self):
        p = self.root / "dup.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("Report.txt", b"x")
            z.writestr("report.TXT", b"y")
        self.assertEqual(self.probe(p.name, "zip")["issue"], "duplicate_member")

    def test_tar_casefold_duplicate(self):
        p = self.root / "dup.tar"
        with tarfile.open(p, "w") as z:
            for name in ("A.txt", "a.TXT"):
                item = tarfile.TarInfo(name)
                item.size = 1
                z.addfile(item, io.BytesIO(b"x"))
        self.assertEqual(self.probe(p.name, "tar")["issue"], "duplicate_member")

    def test_no_mutation_and_no_secret_output(self):
        p = self.make_tar("secret-backup.tar")
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        cp = subprocess.run([sys.executable, "-m", "backup_probe", str(p), "--json"], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(json.loads(cp.stdout)["results"][0]["status"], "ok")
        self.assertNotIn("secret-backup", cp.stdout + cp.stderr)
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)

    def test_bzip2_xz(self):
        for mode, name in (("w:bz2", "a.tar.bz2"), ("w:xz", "a.tar.xz")):
            with self.subTest(mode=mode):
                self.make_tar(name, mode=mode)
                self.assertEqual(self.probe(name, "tar")["status"], "ok")

    def test_sqlite_read_only(self):
        p = self.root / "data.sqlite"
        with sqlite3.connect(p) as db:
            db.execute("CREATE TABLE items (id INTEGER)")
            db.execute("INSERT INTO items VALUES (1)")
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        self.assertEqual(self.probe(p.name, "sqlite")["status"], "ok")
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        self.assertFalse(list(self.root.glob("*.sqlite-*")))


if __name__ == "__main__":
    unittest.main()
