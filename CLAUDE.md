# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
# Run with default config (/etc/spindlecrank/backup.ini)
python3 spindlecrank_backup.py

# Use an alternate config
python3 spindlecrank_backup.py --config /path/to/backup.ini

# Dry run — scan and log without writing an archive
python3 spindlecrank_backup.py --dry-run

# Generate a default config file and exit
python3 spindlecrank_backup.py --generate-config
```

No build step, no external dependencies. Requires Python 3.6+ with `lzma` and `sqlite3` C extensions (both are standard in most Python builds).

## Two script versions

There are two files that implement the same utility:

- **`spindlecrank_backup.py`** — the canonical version. Uses `_HashingReader` to compute SHA-256 checksums in a single streaming pass during archive write. Supports batched SQLite commits (`db_commit_batch`), CPU/IO priority settings (`nice_level`, `ionice_class`), and probes files with a 1-byte read.
- **`backup.py`** — an older, simpler version. Computes checksums with a separate `sha256_file()` call after archive write (two reads per file). No batching, no process priority controls, probes files with a 64 KB read.

New work should target `spindlecrank_backup.py`.

## Architecture

The entire utility is a single self-contained Python file with no PyPI dependencies. The execution flow is:

```
main()
  └─ check_runtime_dependencies()   # validates lzma, sqlite3, tarfile before anything else
  └─ BackupConfig                   # parses INI; generates default if absent
  └─ set_process_priority()         # nice + ionice before any real work
  └─ LogManager.setup()             # prunes old run logs, opens per-run log + failures.log
  └─ BackupEngine.run()
       ├─ Phase 1: _scan_source_dirs() → FileManifest (JSONL)
       ├─ Space check (_check_space)   # aborts before touching the archive if insufficient
       ├─ WorkQueue seeding            # marks all files pending
       ├─ Phase 2: per-file loop       # _process_file: probe → archive write → retry
       │    └─ _HashingReader          # SHA-256 computed during the tar streaming write
       └─ Phase 3: _verify()           # cross-references manifest vs queue vs DB
```

### Three independent tracking mechanisms

Every run creates three session files in `session_dir` (default `/var/lib/spindlecrank`), all sharing the same `YYYYMMDD_HHMMSS` session ID:

| File | Class | Purpose |
|---|---|---|
| `manifest_<session>.jsonl` | `FileManifest` | Immutable pre-scan ground truth — written before the archive opens |
| `queue_<session>.jsonl` | `WorkQueue` | Append-only per-file state machine audit trail — survives crashes |
| `completion_<session>.db` | `CompletionDB` | SQLite — queryable outcomes with SHA-256; batched commits for perf |

Phase 3 (`_verify`) cross-references all three: every manifest path must appear in queue-done, DB-completed, DB-failed, or DB-skipped; queue-done and DB-completed must match exactly.

### Key design invariants

- **The archive is never opened if space check fails.** `_check_space` enforces two independent thresholds (absolute floor `min_free_mb` and proportional `space_safety_factor × raw_bytes`).
- **A single file failure never aborts the archive.** `_process_file` retries once after `retry_delay` seconds; permanent failures go to `failures.log` and processing continues.
- **SHA-256 is computed in the same pass as the archive write** (via `_HashingReader`), avoiding a second full file read.
- **Hard-linked files are deduplicated** via `(device, inode)` tracking in `_scan_source_dirs`.
- **`SIGTERM`/`SIGINT` sets `_abort`** — the engine finishes the current file then exits cleanly (the LZMA stream is properly closed by the `with` block).
- **`dirnames[:]` is modified in-place** during `os.walk` to prune excluded directories from traversal — this is intentional and critical; don't change it to reassignment.

### Logging

Two loggers share the `spindlecrank` namespace:
- `spindlecrank` → per-run `backup_<session>.log` (DEBUG, verbose) + console (INFO, clean)
- `spindlecrank.failures` → cumulative `failures.log` (size-rotated, append across runs)

Run logs are pruned by count (`log_retain_count`, default 5). `failures.log` is rotated by size (`max_log_size_mb`, default 10 MB).

## Configuration

Config lives at `/etc/spindlecrank/backup.ini` (override with `--config`). It is auto-generated with defaults on first run. Key tuning parameters:

- `compression_level = extreme` — maximum LZMA/XZ; lower to `6` for faster backups
- `space_safety_factor = 1.0` — conservative; lower to `0.4` if trusting XZ compression savings
- `db_commit_batch = 50` — SQLite batch size; lower for crash-recovery granularity
- `nice_level / ionice_class` — CPU/IO priority (only in `spindlecrank_backup.py`)

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All files backed up successfully |
| `1` | Partial success — some files permanently failed; check `failures.log` |
| `2` | Fatal — config error, insufficient space, dependency failure, or unhandled exception |
