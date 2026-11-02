import contextlib
import io
import json
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from backup_probe.core import archive_name, inspect, main

LIMITS = {"input": 2**31, "member": 2**29, "total": 2**31, "entries": 10000}


class BackupProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def probe(self, name, kind="auto", limits=None):
        return inspect(self.root / name, kind, limits or LIMITS, 5.0)

    def zip(self, name="data.zip", entries=None):
        with zipfile.ZipFile(self.root / name, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for key, value in (entries or {"folder/a.txt": b"hello"}).items():
                z.writestr(key, value)

    def tar(self, name="data.tar", entries=None, mode="w"):
        with tarfile.open(self.root / name, mode) as t:
            for key, value in (entries or {"folder/a.txt": b"hello"}).items():
                item = tarfile.TarInfo(key)
                item.size = len(value)
                t.addfile(item, io.BytesIO(value))

    def db(self, name="data.sqlite"):
        con = sqlite3.connect(self.root / name)
        con.execute("CREATE TABLE data(value TEXT)")
        con.execute("INSERT INTO data VALUES ('hello')")
        con.commit()
        con.close()

    def test_zip_valid(self):
        self.zip()
        r = self.probe("data.zip")
        self.assertEqual((r["status"], r["members"], r["bytes_scanned"]), ("ok", 1, 5))

    def test_zip_empty(self):
        with zipfile.ZipFile(self.root / "empty.zip", "w"):
            pass
        self.assertEqual(self.probe("empty.zip")["status"], "ok")

    def test_zip_truncated(self):
        self.zip()
        p = self.root / "data.zip"
        p.write_bytes(p.read_bytes()[:30])
        self.assertEqual(self.probe("data.zip")["status"], "failed")

    def test_zip_bad_crc(self):
        with zipfile.ZipFile(self.root / "data.zip", "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("a", b"ABCDEF")
        p = self.root / "data.zip"
        raw = p.read_bytes()
        # Flip one stored data byte; the central directory CRC stays unchanged.
        start = 30 + len("a")
        p.write_bytes(raw[:start] + bytes([raw[start] ^ 1]) + raw[start + 1:])
        self.assertEqual(self.probe("data.zip")["status"], "failed")

    def test_zip_unsafe_path(self):
        self.zip(entries={"../secret.txt": b"x"})
        self.assertEqual(self.probe("data.zip")["issue"], "unsafe_member")

    def test_zip_duplicate(self):
        with zipfile.ZipFile(self.root / "data.zip", "w") as z:
            z.writestr("a", b"1")
            with __import__("warnings").catch_warnings():
                __import__("warnings").simplefilter("ignore", UserWarning)
                z.writestr("./a", b"2")
        self.assertEqual(self.probe("data.zip")["issue"], "duplicate_member")

    def test_zip_symlink(self):
        with zipfile.ZipFile(self.root / "data.zip", "w") as z:
            item = zipfile.ZipInfo("link")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(item, "target")
        self.assertEqual(self.probe("data.zip")["issue"], "special_member")

    def test_zip_limits(self):
        self.zip()
        for key, value, issue in (("member", 4, "member_limit"), ("total", 4, "total_limit"), ("entries", 0, "entry_limit")):
            limits = {**LIMITS, key: value}
            self.assertEqual(self.probe("data.zip", limits=limits)["issue"], issue)

    def test_tar_valid(self):
        self.tar()
        self.assertEqual(self.probe("data.tar")["bytes_scanned"], 5)

    def test_tar_gzip(self):
        self.tar(name="data.tar.gz", mode="w:gz")
        self.assertEqual(self.probe("data.tar.gz")["status"], "ok")

    def test_tar_gzip_trailer_corruption(self):
        self.tar(name="data.tar.gz", mode="w:gz")
        p = self.root / "data.tar.gz"
        data = bytearray(p.read_bytes())
        data[-8] ^= 1
        p.write_bytes(data)
        self.assertEqual(self.probe("data.tar.gz")["issue"], "invalid_tar")

    def test_tar_bzip2(self):
        self.tar(name="data.tar.bz2", mode="w:bz2")
        self.assertEqual(self.probe("data.tar.bz2")["status"], "ok")

    def test_tar_xz(self):
        self.tar(name="data.tar.xz", mode="w:xz")
        self.assertEqual(self.probe("data.tar.xz")["status"], "ok")

    def test_tar_compressed_stream_limit(self):
        import gzip
        self.tar(name="data.tar")
        raw = (self.root / "data.tar").read_bytes()
        (self.root / "data.tar.gz").write_bytes(gzip.compress(raw + b"\0" * 100000))
        limits = {**LIMITS, "total": 5, "entries": 1}
        self.assertEqual(self.probe("data.tar.gz", limits=limits)["issue"], "tar_stream_limit")

    def test_tar_truncated(self):
        self.tar()
        p = self.root / "data.tar"
        p.write_bytes(p.read_bytes()[:515])
        self.assertEqual(self.probe("data.tar")["status"], "failed")

    def test_tar_unsafe_path(self):
        self.tar(entries={"/etc/secret": b"x"})
        self.assertEqual(self.probe("data.tar")["issue"], "unsafe_member")

    def test_tar_symlink(self):
        with tarfile.open(self.root / "data.tar", "w") as t:
            item = tarfile.TarInfo("link")
            item.type = tarfile.SYMTYPE
            item.linkname = "target"
            t.addfile(item)
        self.assertEqual(self.probe("data.tar")["issue"], "special_member")

    def test_tar_duplicate(self):
        self.tar(entries={"a": b"1", "./a": b"2"})
        self.assertEqual(self.probe("data.tar")["issue"], "duplicate_member")

    def test_sqlite_valid(self):
        self.db()
        self.assertEqual(self.probe("data.sqlite")["status"], "ok")

    def test_sqlite_invalid(self):
        (self.root / "data.sqlite").write_bytes(b"not a database")
        self.assertEqual(self.probe("data.sqlite")["issue"], "invalid_sqlite")

    def test_sqlite_corrupt(self):
        self.db()
        p = self.root / "data.sqlite"
        p.write_bytes(p.read_bytes()[:100] + b"\x00" * 100)
        self.assertEqual(self.probe("data.sqlite")["status"], "failed")

    def test_no_file_mutation(self):
        self.zip()
        self.tar()
        self.db()
        for name in ("data.zip", "data.tar", "data.sqlite"):
            p = self.root / name
            before = (p.read_bytes(), p.stat().st_mtime_ns)
            self.probe(name)
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        self.assertFalse(list(self.root.glob("*.sqlite-*")))

    def test_unknown_format(self):
        (self.root / "file.dat").write_bytes(b"x")
        self.assertEqual(self.probe("file.dat")["issue"], "unknown_format")

    def test_missing_file(self):
        self.assertEqual(self.probe("missing.zip")["status"], "error")

    def test_directory(self):
        (self.root / "directory.zip").mkdir()
        self.assertEqual(self.probe("directory.zip")["issue"], "not_regular_file")

    def test_symlink_input(self):
        self.zip()
        (self.root / "link.zip").symlink_to(self.root / "data.zip")
        self.assertEqual(self.probe("link.zip")["issue"], "not_regular_file")

    def test_input_limit(self):
        self.zip()
        self.assertEqual(self.probe("data.zip", limits={**LIMITS, "input": 1})["issue"], "input_limit")

    def test_archive_name_rejections(self):
        for name in ("", "..", "a/../b", "C:/file", "\\server\\file", "a\\b", "/absolute", "a\x00b"):
            with self.subTest(name=name), self.assertRaises(Exception):
                archive_name(name)

    def test_cli_json_and_exit_codes(self):
        self.zip()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([str(self.root / "data.zip"), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["results"][0]["status"], "ok")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.root / "data.zip"), "--max-member-bytes", "1"]), 1)
            self.assertEqual(main([str(self.root / "missing.zip")]), 2)

    def test_cli_no_sensitive_paths(self):
        secret = self.root / "secret-token-value.zip"
        secret.write_bytes(b"invalid")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main([str(secret), "--json"])
        self.assertNotIn("secret-token-value", out.getvalue())
        self.assertNotIn(str(self.root), out.getvalue())

    def test_cli_bad_arguments_do_not_echo(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out), self.assertRaises(SystemExit) as ctx:
            main(["--type", "secret-123", "dummy.zip"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertNotIn("secret-123", out.getvalue())

    def test_module_entrypoint(self):
        self.zip()
        p = subprocess.run([sys.executable, "-m", "backup_probe", str(self.root / "data.zip"), "--json"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["results"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
