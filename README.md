# BackupProbe

**Status:** In Development (Day 7, SonyDev GitHub Marathon, 2 November 2026)

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

- ZIP: reads every file, validates CRCs and rejects encrypted, special, duplicate or unsafe member paths.
- TAR: reads every regular file, checks readable lengths and rejects links, special entries, duplicate or unsafe paths. Supports gzip, bzip2 and xz compression.
- SQLite: verifies the header and runs `PRAGMA integrity_check(1)` using a read-only immutable connection. This intentionally ignores uncheckpointed WAL data. Checkpoint the database and make a self-contained backup before inspection.
- Archives are never extracted. A passing result does not establish backup freshness, completeness relative to the original source or restorability of application data.
- ZIP/TAR validation is bounded by byte and member limits. Compressed TAR streams are also drained to verify compression checksums, with an additional decompressed-stream limit of `--max-total-bytes + 4096 * --max-entries + 10240` bytes. Decompression and SQLite integrity checks still consume CPU. Do not run against untrusted files without operating-system resource limits.
- Files changed concurrently during inspection may produce unreliable results. Symbolic links supplied as input are rejected.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Development report — 2 November 2026 (morning)

On Linux with Python 3.13.5, all 32 `unittest` cases passed. A wheel was built using the installed setuptools without network access and installed in an isolated virtual environment. The installed CLI was exercised with valid ZIP, TAR.GZ and SQLite inputs, and exit codes `0`, `1` and `2` were observed. Tests cover corrupt ZIP CRC data, damaged gzip trailers, archive traversal paths, symlinks, duplicate members, truncated archives, SQLite corruption and preservation of input bytes and modification times. Windows and Python 3.10–3.12 have not been tested. This is an MVP under development, not a stable release.
