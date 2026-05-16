# spindlecrank_backup

A robust, single-file-at-a-time Linux backup utility written in pure Python.

Designed around one core principle: **a single inaccessible file should never kill the whole backup.** Instead of treating the backup as one monolithic operation, spindlecrank processes every file individually — probing it, writing it, and recording its outcome — so the failure surface is always a list of individual files rather than a catastrophic archive abort.

---

## Features

- **Single-file granularity** — each file is handled independently with its own probe, write, and retry cycle
- **Maximum LZMA/XZ compression** — `preset 9 | EXTREME` via a streaming tar pipeline
- **Three independent file tracking mechanisms** — pre-scan manifest, append-only work queue, and SQLite completion database, cross-referenced in a final verification phase
- **One retry per file** — transient failures (locks, NFS hiccups, busy files) are retried once; only persistent failures are logged
- **Pre-flight space check** — verifies the destination volume has sufficient free space before the archive is opened
- **Per-run log files** — each invocation writes its own verbose `backup_<session_id>.log`; the last five are kept automatically, older ones are deleted at the next startup
- **Verbose file logging** — millisecond timestamps, source function, and line number on every log line; console output stays clean (INFO and above only)
- **INI-based configuration** — a default config is generated on first run
- **Hard-link deduplication** — the same inode is never archived twice
- **Graceful signal handling** — `SIGTERM`/`SIGINT` finishes the current file and closes the archive cleanly
- **Zero external dependencies** — pure Python standard library (Python 3.6+)
- **Startup dependency check** — validates that required C-extension modules (`lzma`, `sqlite3`) are functional before anything else runs, with actionable fix instructions per distro

---

## How It Works

Every run executes three sequential phases.

### Phase 1 — Pre-scan

All configured source directories are walked before the archive is opened. Each file that passes the exclusion filter is written as a JSON record to an immutable manifest file. This creates a snapshot of exactly what *should* be backed up, independent of what actually *gets* backed up.

Skipped files (wrong type, excluded pattern, stat error) are recorded to the work queue and SQLite database at scan time so they are accounted for in Phase 3.

### Phase 2 — Backup

After the space check passes, a streaming LZMA-compressed tar archive is opened and every file from the manifest is processed in sequence:

```
for each file:
    probe (read first 64 KB)
    ├─ probe OK  → attempt archive write
    │              ├─ write OK  → record sha256 + size to queue + DB → next file
    │              └─ write ERR → mark retry, wait retry_delay seconds
    │                             attempt archive write again
    │                             ├─ write OK  → record success
    │                             └─ write ERR → record permanent failure → next file
    └─ probe ERR → mark retry, wait retry_delay seconds
                   probe again
                   ├─ probe OK  → attempt archive write (as above)
                   └─ probe ERR → record permanent failure → next file
```

The probe step reads the first 64 KB of the file before touching the archive. This ensures the archive is never given a partial or unreadable file — the retry is at the readability level, not the archive-write level. Permanently failed files are appended to `failures.log` for administrator review; the backup continues with the next file.

### Phase 3 — Cross-reference Verification

All three tracking mechanisms are compared:

- Every path in the manifest must appear in at least one of: queue DONE, DB completed, DB failed, or DB skipped
- The set of queue DONE paths must match the set of DB completed paths (no ghost entries in either direction)

Any file that is in the manifest but unaccounted for is flagged as `UNACCOUNTED` in both the run log (`backup_<session_id>.log`) and `failures.log`.

---

## Three File Tracking Mechanisms

Each run creates three session files in `session_dir`. They work independently so that a bug or corruption in one cannot hide what happened.

### 1. Pre-scan Manifest (`manifest_<session>.jsonl`)

Written before the archive opens. One JSON record per line:

```json
{"path":"/etc/passwd","size":1842,"mtime":1700000000.0,"inode":12345,"mode":33188}
```

This file is immutable once written. It is the ground truth for "what should have been backed up."

### 2. Work Queue (`queue_<session>.jsonl`)

An append-only audit trail. Every state transition for every file is timestamped and appended:

```json
{"path":"/etc/passwd","status":"pending","ts":"2024-01-01T12:00:00Z"}
{"path":"/etc/passwd","status":"processing","ts":"2024-01-01T12:00:01Z"}
{"path":"/etc/passwd","status":"done","sha256":"a1b2c3...","size":1842,"ts":"2024-01-01T12:00:01Z"}
```

Valid status values: `pending` → `processing` → `done` | `retry` → `done` | `failed` | `skipped`

Because the file is append-only it survives process crashes. Replaying it reveals exactly where an interrupted run stopped.

### 3. SQLite Completion Database (`completion_<session>.db`)

Three tables:

| Table | Contents |
|---|---|
| `completed` | Path, size, SHA-256 checksum, timestamp for every successfully archived file |
| `failed` | Path, error message, attempt number, timestamp for every permanently failed file |
| `skipped` | Path, reason, timestamp for every file excluded before archive processing |

Query example — find all files that changed size since they were last backed up:

```sql
SELECT path, size FROM completed ORDER BY backed_up DESC;
```

---

## Space Check

The space check runs after Phase 1 (so it knows the exact data size) and before Phase 2 (so the archive file is never created if it would fail). Two independent thresholds must both pass:

| Threshold | Config key | Default | Description |
|---|---|---|---|
| Absolute floor | `min_free_mb` | `512` | Volume must have at least this many MB free regardless of data size |
| Proportional estimate | `space_safety_factor` | `1.0` | Volume must have at least `total_raw_bytes × factor` bytes free |

A `space_safety_factor` of `1.0` requires enough space for the full uncompressed data — conservative but safe, since the actual compressed size cannot be known without writing the archive. Lower values (e.g. `0.4`) trust that XZ compression will reduce the size substantially.

On failure the script logs the exact shortfall to both the run log and `failures.log` and exits with code `2`. No archive file is created.

---

## Logging and Log Rotation

### Per-run log files

Each invocation of the script creates one log file:

```
/var/log/spindlecrank/backup_<session_id>.log
```

The session ID (`YYYYMMDD_HHMMSS` UTC) is shared between the log file and all three tracking files, so every artifact from a single run can be correlated by name. The log file captures everything down to DEBUG level in a verbose format:

```
2024-03-15 02:03:44.127 [DEBUG   ] 20240315_020344 │ _process_file:L312 │  OK  /etc/passwd  [1.8 KB]
2024-03-15 02:03:44.298 [WARNING ] 20240315_020344 │ _process_file:L327 │ Probe failed (attempt 1), will retry: /var/lib/app/db.lck — [Errno 11] Resource temporarily unavailable
2024-03-15 02:03:47.301 [DEBUG   ] 20240315_020344 │ _process_file:L352 │  OK  /var/lib/app/db.lck  [4.1 KB]
```

Each line contains: millisecond timestamp · log level · session ID · function name and line number · message.

The console receives only INFO-level and above in a clean format without the source location noise.

### Run log pruning (count-based)

At every startup, all `backup_????????_??????.log` files in the log directory are sorted newest-first by filename. Any file beyond position `log_retain_count` (default `5`) is deleted before the new log file is opened, so the directory contains at most `log_retain_count` completed run logs at any time.

No `logrotate` configuration is needed for run logs.

### `failures.log` — cumulative, size-based rotation

`failures.log` is kept as a cumulative append file across all runs. It rotates by size (when it exceeds `max_log_size_mb`) rather than by run count, because its value comes from being searchable across many runs:

```
failures.log    →  failures.log.1  (when size >= max_log_size_mb)
failures.log.1  →  failures.log.2
...
failures.log.N  →  pruned
```

Up to `log_retain_count` rotated copies are kept before the oldest is deleted.

---

## Automatic Exclusions

The following are always excluded regardless of configuration:

**Directories**

| Path | Reason |
|---|---|
| `/proc`, `/sys`, `/dev` | Virtual kernel filesystems |
| `/run`, `/var/run`, `/var/lock` | Runtime state, cleared on boot |
| `/tmp`, `/var/tmp` | Temporary files |
| `/snap` | Snap mount points |
| `/lost+found` | Filesystem recovery artifacts |

**File name patterns**

| Pattern | Reason |
|---|---|
| `*.sick` | Backup-tool sentinel/marker files |
| `*.swp`, `*.swo` | Vim swap files |
| `~*` | Editor tilde backup files |
| `.Trash*` | Desktop trash |
| `lost+found` | Filesystem recovery directories |

**File types**

Block devices, character devices, named pipes (FIFOs), and Unix domain sockets are silently skipped — they cannot be meaningfully archived. Hard-linked files (same device + inode) are deduplicated so they appear only once in the archive.

Additional patterns and directories can be added in the `[exclusions]` section of the config file.

---

## Requirements

- **Python 3.6+**
- `lzma` C extension (requires `liblzma` at Python build time)
- `sqlite3` C extension (requires `libsqlite3` at Python build time)

Both are included in standard Python installations. On minimal builds they may be absent:

```
# Debian / Ubuntu
apt install python3-lzma python3-sqlite3

# Alpine Linux
apk add python3-dev xz-dev sqlite-dev   # then rebuild Python

# RHEL / CentOS / Fedora
dnf install python3-libs xz-devel sqlite-devel
```

The startup dependency check will diagnose exactly which module is missing and print the correct fix command for common distributions.

---

## Installation

No installer is needed. Copy the script to a convenient location and make it executable:

```bash
curl -O https://raw.githubusercontent.com/your-org/spindlecrank/main/spindlecrank_backup.py
chmod +x spindlecrank_backup.py
```

Create the required directories (the script needs write access to both):

```bash
mkdir -p /backups /var/lib/spindlecrank
```

Generate the default config:

```bash
python3 spindlecrank_backup.py --generate-config
```

Edit `/etc/spindlecrank/backup.ini` to set your source directories and backup destination, then run:

```bash
python3 spindlecrank_backup.py
```

> **Note:** Backing up system directories like `/etc` requires root. Run with `sudo` or as the root user.

---

## Configuration Reference

Config file location: `/etc/spindlecrank/backup.ini` (override with `--config`)

A default config is written automatically if the file does not exist.

### `[general]`

| Key | Default | Description |
|---|---|---|
| `backup_dir` | `/backups` | Directory where archive files are written |
| `backup_prefix` | `spindlecrank` | Prefix for archive filenames |
| `compression` | `xz` | Compression algorithm: `xz`, `bz2`, or `gz` |
| `compression_level` | `extreme` | Compression strength: `extreme` or `1`–`9` |
| `retry_delay` | `3` | Seconds to wait before the retry attempt on a failed file |
| `max_log_size_mb` | `10` | Size threshold (MB) that triggers `failures.log` rotation |
| `log_retain_count` | `5` | Number of per-run log files to keep; also the rotation depth for `failures.log` |
| `session_dir` | `/var/lib/spindlecrank` | Directory for per-run tracking files (manifest, queue, DB) |
| `space_safety_factor` | `1.0` | Fraction of raw data size that must be free on the backup volume; `0` disables |
| `min_free_mb` | `512` | Absolute minimum free space (MB) on the backup volume |

### `[directories]`

| Key | Description |
|---|---|
| `include` | Source paths to back up, one per indented continuation line |

```ini
[directories]
include =
    /etc
    /home
    /var/www
    /srv
```

### `[exclusions]`

| Key | Description |
|---|---|
| `patterns` | Additional fnmatch filename patterns to exclude (built-in patterns always apply) |
| `skip_dirs` | Additional directory paths to skip; fnmatch glob is supported |

```ini
[exclusions]
patterns =
    *.tmp
    *.log.1
    .DS_Store

skip_dirs =
    /home/*/.cache
    /home/*/.local/share/Trash
    /home/*/.mozilla/firefox/*/Cache
    /var/www/*/node_modules
```

---

## Archive Format

Archives are named:

```
<backup_prefix>_<hostname>_<YYYYMMDD_HHMMSS><extension>
```

Example: `spindlecrank_webserver01_20240315_023000.tar.xz`

Files are stored with relative paths (leading `/` stripped), so extraction restores to the current directory by default:

```bash
# List contents
tar -tJf spindlecrank_webserver01_20240315_023000.tar.xz

# Extract to /restore
tar -xJf spindlecrank_webserver01_20240315_023000.tar.xz -C /restore

# Extract a single file
tar -xJf spindlecrank_webserver01_20240315_023000.tar.xz -C /restore etc/passwd
```

---

## CLI Reference

```
usage: spindlecrank_backup.py [-h] [--config PATH] [--dry-run] [--generate-config] [--version]
```

| Flag | Description |
|---|---|
| `--config PATH` | Path to INI config file (default: `/etc/spindlecrank/backup.ini`) |
| `--dry-run` | Walk source directories and log what would be backed up; no archive is created and no space check is performed |
| `--generate-config` | Write a default config file to `--config` path and exit |
| `--version` | Print version and exit |

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Complete success — all in-scope files backed up |
| `1` | Partial success — backup completed but one or more files permanently failed; review `failures.log` |
| `2` | Fatal error — configuration problem, insufficient disk space, unhandled exception, or startup dependency check failure; no usable archive was produced |

---

## File Layout

```
/etc/spindlecrank/
└── backup.ini                          ← configuration

/backups/                               ← backup_dir (configurable)
├── spindlecrank_host_20240315_020000.tar.xz
└── spindlecrank_host_20240316_020000.tar.xz

/var/log/spindlecrank/                       ← LOG_DIR (fixed)
├── backup_20240315_020000.log               ← most recent run (verbose, DEBUG)
├── backup_20240314_020000.log               ─┐
├── backup_20240313_020000.log                │ up to log_retain_count files kept
├── backup_20240312_020000.log                │ older ones deleted at next startup
├── backup_20240311_020000.log               ─┘
├── failures.log                             ← cumulative failures, size-rotated
└── failures.log.1                           ← rotated when size ≥ max_log_size_mb

/var/lib/spindlecrank/                  ← session_dir (configurable)
├── manifest_20240315_020000.jsonl      ← Phase 1: pre-scan manifest
├── queue_20240315_020000.jsonl         ← Phase 2: per-file audit trail
└── completion_20240315_020000.db       ← Phase 3: SQLite completion DB
```

Session tracking files accumulate over time. Prune old sessions periodically:

```bash
# Remove session files older than 30 days
find /var/lib/spindlecrank -mtime +30 -delete
```

---

## Scheduling with Cron

Run nightly at 2 AM:

```bash
# Edit root's crontab
crontab -e

# Add:
0 2 * * * /usr/bin/python3 /usr/local/bin/spindlecrank_backup.py >> /var/log/spindlecrank/cron.log 2>&1
```

Or with systemd timer — create `/etc/systemd/system/spindlecrank.service`:

```ini
[Unit]
Description=Spindlecrank Backup
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/spindlecrank_backup.py
StandardOutput=journal
StandardError=journal
```

And `/etc/systemd/system/spindlecrank.timer`:

```ini
[Unit]
Description=Run Spindlecrank Backup nightly

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now spindlecrank.timer
systemctl status spindlecrank.timer
```

---

## Monitoring Failures

The `failures.log` is the primary triage surface. Each line is self-contained:

```
2024-03-15 02:03:44 | FAILED | probe | /home/user/locked.db | probe failed on retry: [Errno 11] Resource temporarily unavailable
2024-03-15 02:03:51 | FAILED | write | /var/lib/app/journal  | archive write failed on retry: [Errno 5] Input/output error
2024-03-15 02:04:00 | ABORT  | insufficient space (floor) | /backups | backup volume has only 2.1 GB free — below the 512.0 MB absolute floor
2024-03-15 02:04:00 | UNACCOUNTED | not in queue/DB completed/failed/skipped | /srv/data/mystery_file
```

| Prefix | Meaning |
|---|---|
| `FAILED \| probe` | File could not be read after two attempts; likely locked, permissions issue, or transient I/O error |
| `FAILED \| write` | File was readable but could not be written to the archive after two attempts |
| `ABORT \| insufficient space` | Backup was halted before Phase 2 began; free up space and re-run |
| `UNACCOUNTED` | File appeared in the pre-scan manifest but was not recorded in any tracking mechanism; cross-reference with the session's `backup_<session_id>.log` and completion DB |

---

## Querying the Session Database

The SQLite completion DB is a plain file — query it with any SQLite client:

```bash
sqlite3 /var/lib/spindlecrank/completion_20240315_020000.db
```

```sql
-- How many files were backed up?
SELECT COUNT(*) FROM completed;

-- Total bytes archived
SELECT SUM(size) FROM completed;

-- Which files failed?
SELECT path, error, attempt FROM failed;

-- Find the largest files in the backup
SELECT path, size FROM completed ORDER BY size DESC LIMIT 20;

-- Verify a specific file's checksum
SELECT sha256 FROM completed WHERE path = 'etc/passwd';
```

---

## Troubleshooting

**`STARTUP FAILED — dependency check errors`**

The `lzma` or `sqlite3` C extension is absent from your Python build. The error message includes the exact package to install for your distribution. After installing, re-run the script — no other changes needed.

**`Space check FAILED`**

The backup destination does not have enough free space. Either free space on the volume, lower `space_safety_factor` if you trust compression to save significant space, or lower `min_free_mb` if the absolute floor is too conservative for your environment.

**Files appearing in `failures.log` with `[Errno 11] Resource temporarily unavailable`**

These files were held by an advisory lock at backup time. Consider scheduling the backup during a maintenance window, or accepting that these files will be retried on the next run. Since only the individual file fails, the rest of the backup is unaffected.

**`UNACCOUNTED` entries in `failures.log`**

A file appeared in the pre-scan manifest but was not found in the work queue, completion DB, or skipped records. This should not happen under normal operation. Identify the session ID from the `failures.log` timestamp, then open the matching `backup_<session_id>.log` and `completion_<session_id>.db` to see what was happening at that point in the run. Also check that disk space in `session_dir` did not fill up mid-run, which could cause queue writes to silently fail.

**Archive appears truncated or corrupt**

If the process was killed mid-run with `SIGKILL` (not `SIGTERM`) the LZMA stream may not be closed properly. Inspect with:

```bash
xz --test /backups/spindlecrank_host_20240315_020000.tar.xz
```

XZ compression is designed to detect truncation. If the test fails, use the previous session's archive and investigate what killed the process (`dmesg`, `journalctl`).

---

## License

MIT
