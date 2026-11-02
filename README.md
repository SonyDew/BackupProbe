# BackupProbe

**Status:** Completed MVP 0.1.0 (Day 7, SonyDev GitHub Marathon, 2 November 2026). Not a stable release.

BackupProbe is a read-only command-line checker for ZIP, TAR and SQLite backup files. It reads archive contents to detect corruption and checks SQLite databases with `PRAGMA integrity_check(1)`. It never extracts files or executes content from a backup.

## Install

Requires Python 3.10 or later. No runtime packages are required.

```sh
python -m pip install .
```

Alternatively, run `python -m backup_probe` from the repository directory.

## Usage

```sh
backup-probe backup.zip
backup-probe snapshot.tar.gz database.sqlite --json
backup-probe backup.zip --max-member-bytes 104857600 --max-total-bytes 536870912
backup-probe file.without.extension --type zip
```

File types are selected by extension unless `--type` is specified. Supported extensions: `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, `.txz`, `.db`, `.sqlite`, `.sqlite3`.

Text output shows the positional input index, result and format. JSON includes `index`, `type`, `status`, `members`, `bytes_scanned` and `issue`. Neither format prints input paths or archive member names. Exit codes: `0` all checks passed; `1` at least one backup failed a check; `2` at least one input could not be inspected or arguments were invalid. With multiple inputs, `2` takes precedence.

Default limits: 2 GiB per input, 512 MiB per archive member, 2 GiB total uncompressed data per archive and 10,000 members. Override these with `--max-input-bytes`, `--max-member-bytes`, `--max-total-bytes` and `--max-entries`. Limits are checked against archive metadata and against bytes actually read. The SQLite check has a 15-second progress-handler timeout, adjustable with `--sqlite-timeout`.

## Checks and limitations

- ZIP: reads every file, validates CRCs and local file headers (including directory entries), and rejects encrypted, special, duplicate or unsafe member paths. Directory entries must have zero size and CRC.
- TAR: reads every regular file, checks readable lengths, requires the standard two zero end-of-archive blocks and zero-only trailing padding, and rejects links, special entries, duplicate or unsafe paths. Supports gzip, bzip2 and xz compression.
- SQLite: verifies the header and runs `PRAGMA integrity_check(1)` using a read-only immutable connection. This intentionally ignores uncheckpointed WAL data. Checkpoint the database and make a self-contained backup before inspection.
- Archives are never extracted. Member names are checked against conservative cross-platform path rules, including Windows device names, alternate data streams and trailing dots or spaces. Names that differ only by Unicode case folding are treated as duplicates. A passing result does not establish backup freshness, completeness relative to the original source or restorability of application data.
- ZIP/TAR validation is bounded by byte and member limits. Compressed TAR streams are also drained to verify compression checksums, with an additional decompressed-stream limit of `--max-total-bytes + 4096 * --max-entries + 10240` bytes. Decompression and SQLite integrity checks still consume CPU. Do not run against untrusted files without operating-system resource limits.
- TAR archives without the standard end markers or with nonzero trailing padding are rejected, even if some readers accept them. Concatenated TAR archives are not supported. The check does not compare file contents against the original backup source. For SQLite, `bytes_scanned` reports the database file size, not measured I/O.
- ZIP central-directory metadata is parsed by Python before the entry-count limit is enforced; memory use is not strictly bounded by `--max-entries`. Files changed concurrently during inspection may produce unreliable results. Symbolic links supplied as input are rejected.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Development report — 2 November 2026 (morning)

On Linux with Python 3.13.5, all 32 `unittest` cases passed. A wheel was built using the installed setuptools without network access and installed in an isolated virtual environment. The installed CLI was exercised with valid ZIP, TAR.GZ and SQLite inputs, and exit codes `0`, `1` and `2` were observed. Tests cover corrupt ZIP CRC data, damaged gzip trailers, archive traversal paths, symlinks, duplicate members, truncated archives, SQLite corruption and preservation of input bytes and modification times. Windows and Python 3.10–3.12 have not been tested. This is an MVP under development, not a stable release.

## Development report — 2 November 2026 (day audit)

Reproduced two false passes: an uncompressed TAR truncated immediately after its final file and a ZIP directory entry with a modified central-directory CRC. Both now fail. Compressed TAR files with missing end markers also fail. Member-path checks now reject Windows device names, alternate data streams and trailing dots or spaces.

On Linux with Python 3.13.5, all 44 `unittest` cases passed (12 added). The wheel was built offline with setuptools 82.0.1, installed into a separate virtual environment and exercised with valid ZIP, TAR.GZ and SQLite inputs. The installed command returned `0` for valid inputs, `1` for a member-size violation and `2` for a missing file. JSON output was parsed and checked. Tests include damaged archive trailers, invalid directory metadata, unsafe member names, truncated TAR padding, invalid CLI limits, mixed-result precedence and output that does not reveal input paths.

Windows and Python 3.10–3.12 remain untested. This is an MVP under development, not a stable release.

## Development report — 2 November 2026 (evening)

Reproduced three false passes in the day-audit version: a TAR containing nonzero bytes before its final zero blocks, a ZIP directory with a damaged local header, and archive members whose names differ only by case. TAR validation now checks the full trailing region from the logical end of the last member; ZIP directory entries are opened to validate local headers; duplicate-name detection uses Unicode case folding.

On Linux with Python 3.13.5, all 59 `unittest` cases passed with warnings treated as errors (15 evening tests added). Tests cover empty archives, gzip/bzip2/xz, GNU and PAX long names, damaged padding and ZIP directory headers, case-insensitive duplicate names, SQLite read-only inspection, and output that does not disclose input paths. A wheel was built offline using setuptools 82.0.1 and installed in an isolated virtual environment. The installed CLI was exercised with the four README examples and with exit codes `0`, `1` and `2`; JSON output was parsed.

The main ZIP/TAR/SQLite checks are complete for the stated MVP scope. ZIP central-directory metadata is still parsed before the entry limit is enforced. Concurrent file modification, Windows and Python 3.10–3.12 remain untested. This is not a restore test or a stable release.
