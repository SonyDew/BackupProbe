"""Bounded, read-only checks for ZIP, TAR and SQLite backups."""

import argparse
import bz2
import gzip
import json
import lzma
import zlib
import re
import sqlite3
import stat
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

MAX_INPUT = 2 * 1024**3
MAX_MEMBER = 512 * 1024**2
MAX_TOTAL = 2 * 1024**3
MAX_ENTRIES = 10000
CHUNK = 1024 * 1024


class ProbeFailure(Exception):
    """The backup failed a validation check."""


class ProbeError(Exception):
    """The input could not be inspected."""


def archive_name(name):
    """Reject archive paths unsafe to extract; no extraction is performed."""
    if (not name or name.startswith(("/", "\\")) or "\\" in name
            or "\x00" in name or re.match(r"^[A-Za-z]:", name)
            or any(part == ".." for part in name.split("/"))):
        raise ProbeFailure("unsafe_member")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise ProbeFailure("unsafe_member")
    return "/".join(parts)


def add_member(name, size, seen, state, limits):
    key = archive_name(name)
    if key in seen:
        raise ProbeFailure("duplicate_member")
    seen.add(key)
    state["members"] += 1
    if state["members"] > limits["entries"]:
        raise ProbeFailure("entry_limit")
    if size < 0 or size > limits["member"]:
        raise ProbeFailure("member_limit")
    if state["bytes_scanned"] + size > limits["total"]:
        raise ProbeFailure("total_limit")


def read_member(stream, expected, state, limits):
    count = 0
    while True:
        block = stream.read(min(CHUNK, limits["member"] - count + 1))
        if not block:
            break
        count += len(block)
        if count > limits["member"] or state["bytes_scanned"] + count > limits["total"]:
            raise ProbeFailure("size_limit")
    if count != expected:
        raise ProbeFailure("truncated_member")
    state["bytes_scanned"] += count


def check_zip(path, state, limits):
    seen = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                add_member(item.filename, item.file_size, seen, state, limits)
                mode = (item.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ProbeFailure("special_member")
                if item.flag_bits & 1:
                    raise ProbeFailure("encrypted_member")
                if item.is_dir():
                    if item.file_size:
                        raise ProbeFailure("invalid_directory")
                    continue
                if kind == stat.S_IFDIR:
                    raise ProbeFailure("invalid_directory")
                with archive.open(item) as stream:
                    read_member(stream, item.file_size, state, limits)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError, ValueError,
            OSError, NotImplementedError, zlib.error, lzma.LZMAError) as exc:
        raise ProbeFailure("invalid_zip") from exc


def check_tar(path, state, limits):
    seen = set()
    try:
        with tarfile.open(path, "r:*") as archive:
            for item in archive:
                add_member(item.name, item.size if item.isfile() else 0, seen, state, limits)
                if item.isdir():
                    continue
                if not item.isfile():
                    raise ProbeFailure("special_member")
                stream = archive.extractfile(item)
                if stream is None:
                    raise ProbeFailure("invalid_tar")
                with stream:
                    read_member(stream, item.size, state, limits)
        # tarfile stops at the TAR end marker and may not read the compression
        # trailer. Drain compressed streams to verify their checksums.
        with path.open("rb") as source:
            magic = source.read(6)
        opener = (gzip.open if magic.startswith(b"\x1f\x8b") else
                  bz2.open if magic.startswith(b"BZh") else
                  lzma.open if magic == b"\xfd7zXZ\x00" else None)
        if opener:
            maximum = limits["total"] + 4096 * limits["entries"] + 10240
            count = 0
            with opener(path, "rb") as decoded:
                while True:
                    block = decoded.read(min(CHUNK, maximum - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > maximum:
                        raise ProbeFailure("tar_stream_limit")
    except (tarfile.TarError, EOFError, OSError, ValueError, lzma.LZMAError) as exc:
        raise ProbeFailure("invalid_tar") from exc


def check_sqlite(path, state, timeout):
    try:
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise ProbeFailure("invalid_sqlite")
        # immutable=1 avoids journal creation and deliberately ignores WAL files.
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            started = time.monotonic()
            connection.set_progress_handler(lambda: 1 if time.monotonic() - started > timeout else 0, 1000)
            row = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if row != ("ok",):
                raise ProbeFailure("integrity_check_failed")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        if "interrupted" in str(exc).lower():
            raise ProbeFailure("sqlite_timeout") from None
        raise ProbeFailure("invalid_sqlite") from None
    state["members"] = 1
    state["bytes_scanned"] = path.stat().st_size


def detect_type(path):
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    if name.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite"
    raise ProbeError("unknown_format")


def inspect(path, kind, limits, timeout):
    state = {"members": 0, "bytes_scanned": 0}
    result = {"type": kind, "status": "ok", "members": 0, "bytes_scanned": 0, "issue": None}
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ProbeError("not_regular_file")
        if info.st_size > limits["input"]:
            raise ProbeFailure("input_limit")
        if kind == "auto":
            kind = detect_type(path)
        result["type"] = kind
        if kind == "zip":
            check_zip(path, state, limits)
        elif kind == "tar":
            check_tar(path, state, limits)
        else:
            check_sqlite(path, state, timeout)
    except ProbeFailure as exc:
        result.update(status="failed", issue=str(exc))
    except ProbeError as exc:
        result.update(status="error", issue=str(exc))
    except (OSError, OverflowError, ValueError):
        result.update(status="error", issue="read_error")
    result["members"] = state["members"]
    result["bytes_scanned"] = state["bytes_scanned"]
    return result


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "backup-probe: invalid arguments\n")


def main(argv=None):
    parser = SafeParser(prog="backup-probe", description="Check ZIP, TAR and SQLite backups without extracting files.")
    parser.add_argument("paths", nargs="+", type=Path, help="backup files to inspect")
    parser.add_argument("--type", choices=("auto", "zip", "tar", "sqlite"), default="auto")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    parser.add_argument("--max-input-bytes", type=int, default=MAX_INPUT)
    parser.add_argument("--max-member-bytes", type=int, default=MAX_MEMBER)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL)
    parser.add_argument("--max-entries", type=int, default=MAX_ENTRIES)
    parser.add_argument("--sqlite-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    if (any(value <= 0 for value in (args.max_input_bytes, args.max_member_bytes, args.max_total_bytes, args.max_entries))
            or not 0 < args.sqlite_timeout <= 3600):
        parser.error("invalid limits")
    limits = {"input": args.max_input_bytes, "member": args.max_member_bytes,
              "total": args.max_total_bytes, "entries": args.max_entries}
    results = []
    for index, path in enumerate(args.paths, 1):
        item = inspect(path, args.type, limits, args.sqlite_timeout)
        item = {"index": index, **item}
        results.append(item)
    if args.json:
        print(json.dumps({"results": results}, sort_keys=True))
    else:
        for item in results:
            print(f"{item['index']}: {item['status']} ({item['type']}; {item['issue'] or 'checked'})")
    if any(item["status"] == "error" for item in results):
        return 2
    if any(item["status"] == "failed" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
