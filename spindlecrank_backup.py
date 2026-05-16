#!/usr/bin/env python3
"""
backup.py
======================
Robust single-file-at-a-time Linux backup utility with maximum LZMA compression.

Three-layer file tracking
--------------------------
  1. Pre-scan JSON Lines manifest  — immutable source-of-truth; every file that
                                     *should* be backed up, written before the
                                     archive is opened.
  2. Append-only JSONL work queue  — per-file audit trail (pending → processing
                                     → done | retry → done | failed | skipped).
                                     Survives crashes; supports resume analysis.
  3. SQLite completion database    — queryable record of every outcome with SHA-256
                                     checksums.  Cross-referenced against the other
                                     two mechanisms in a final verification phase.

Failure strategy
-----------------
  • Each file is probed for readability before being touched by the archive writer.
  • If the probe (or the archive write) fails on the first attempt, the file is
    retried once after a configurable delay.
  • On a second failure the file is logged to /var/log/spindlecrank/failures.log
    for administrator review and the backup continues with the next file.
  • The archive is NEVER aborted because of a single-file problem.

Usage
-----
    python3 spindlecrank_backup.py [--config PATH] [--dry-run] [--generate-config]
"""

import argparse
import configparser
import fnmatch
import hashlib
import json
import logging
import lzma
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Generator, List, Optional, Set, Tuple

# ─── Version & constants ──────────────────────────────────────────────────────

VERSION        = "1.0.0"
SCRIPT_NAME    = "spindlecrank"
DEFAULT_CONFIG = "/etc/spindlecrank/backup.ini"
LOG_DIR        = Path("/var/log/spindlecrank")

# Virtual / volatile directories — always excluded regardless of config
BUILTIN_SKIP_DIRS: Set[str] = {
    "/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp",
    "/snap", "/lost+found", "/var/lock", "/var/run",
}

# Filename patterns — always excluded regardless of config (fnmatch style)
BUILTIN_SKIP_PATTERNS: List[str] = [
    "*.sick",           # backup-tool sentinel marker files
    "*.swp", "*.swo",   # Vim swap files
    "~*",               # editor tilde backups
    ".Trash*",          # desktop trash
    "lost+found",       # filesystem recovery artefacts
]

# File types that cannot meaningfully be archived (not regular files)
SKIP_STAT_TYPES: Set[int] = {
    stat.S_IFBLK,   # block device
    stat.S_IFCHR,   # character device
    stat.S_IFIFO,   # named pipe / FIFO
    stat.S_IFSOCK,  # Unix domain socket
}

# Minimum Python version required
MIN_PYTHON = (3, 6)

# ─── Runtime dependency check ─────────────────────────────────────────────────
#
# All imports are Python standard library — no PyPI packages are needed.
# However, two stdlib modules are C extensions that CAN be absent on minimal
# or stripped Python builds (e.g. Alpine Linux, some Docker base images, or
# CPython compiled without the matching system dev libraries):
#
#   lzma    — requires liblzma / xz-utils dev headers at CPython build time
#   sqlite3 — requires libsqlite3 dev headers at CPython build time
#
# The check below tests each at startup and prints actionable fix instructions
# rather than letting the script crash with a confusing ImportError mid-run.

def check_runtime_dependencies() -> None:
    """
    Verify Python version and that all required stdlib C-extensions are
    functional.  Prints a clear diagnosis and exits with code 2 on any failure.

    This runs before argument parsing so that even ``--help`` on a broken
    interpreter gives a useful message rather than a traceback.
    """
    errors: List[str] = []

    # ── Python version ────────────────────────────────────────────────────────
    if sys.version_info < MIN_PYTHON:
        errors.append(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; "
            f"running {sys.version.split()[0]}.\n"
            f"  Fix: upgrade Python — e.g.  apt install python3"
        )

    # ── lzma (XZ compression) ─────────────────────────────────────────────────
    # The module itself may import but fail to compress if liblzma is stub-only.
    try:
        import lzma as _lzma  # noqa: F401 — tested below
        _lzma.compress(b"probe", format=_lzma.FORMAT_XZ, preset=0)
    except ImportError:
        errors.append(
            "The 'lzma' module is missing from this Python build.\n"
            "  This module requires liblzma to be present when CPython is compiled.\n"
            "  Fix (Debian/Ubuntu):  apt install python3-lzma  (or rebuild Python)\n"
            "  Fix (Alpine):         apk add python3-dev xz-dev  (then rebuild Python)\n"
            "  Fix (RHEL/CentOS):    dnf install python3-libs xz-devel"
        )
    except Exception as exc:
        errors.append(
            f"The 'lzma' module imported but is not functional: {exc}\n"
            "  Fix: ensure liblzma (xz-utils) is installed and rebuild/reinstall Python."
        )

    # ── sqlite3 ───────────────────────────────────────────────────────────────
    try:
        import sqlite3 as _sqlite3  # noqa: F401
        _conn = _sqlite3.connect(":memory:")
        _conn.execute("CREATE TABLE _probe (x INTEGER)")
        _conn.close()
    except ImportError:
        errors.append(
            "The 'sqlite3' module is missing from this Python build.\n"
            "  Fix (Debian/Ubuntu):  apt install python3-sqlite3\n"
            "  Fix (Alpine):         apk add python3-dev sqlite-dev  (then rebuild Python)\n"
            "  Fix (RHEL/CentOS):    dnf install python3-sqlite3 sqlite-devel"
        )
    except Exception as exc:
        errors.append(
            f"The 'sqlite3' module imported but is not functional: {exc}\n"
            "  Fix: ensure libsqlite3 is installed and rebuild/reinstall Python."
        )

    # ── tarfile (should always be present, but guard against exotic builds) ───
    try:
        import tarfile as _tarfile  # noqa: F401
        _ = _tarfile.ENCODING  # access an attribute to confirm the module loaded
    except (ImportError, AttributeError) as exc:
        errors.append(
            f"The 'tarfile' module is unavailable or broken: {exc}\n"
            "  This is a core stdlib module — reinstall Python to fix."
        )

    # ── Report ────────────────────────────────────────────────────────────────
    if errors:
        print(
            f"\n[{SCRIPT_NAME}] STARTUP FAILED — dependency check errors:\n",
            file=sys.stderr,
        )
        for i, msg in enumerate(errors, 1):
            print(f"  {i}. {msg}\n", file=sys.stderr)
        sys.exit(2)

# ─── Default INI content ──────────────────────────────────────────────────────

DEFAULT_INI = """\
; spindlecrank backup configuration
; Generated automatically — customise as needed.
; Location: /etc/spindlecrank/backup.ini

[general]
; Directory where backup archives are written
backup_dir       = /backups

; Prefix used in archive filenames
backup_prefix    = spindlecrank

; Compression algorithm: xz (recommended, highest ratio), bz2, gz
compression      = xz

; Compression strength: 1-9 or extreme.
;   6   — good ratio, moderate CPU (recommended default)
;   9   — high ratio, high CPU
;   extreme — maximum ratio, very high CPU (use for archival storage)
compression_level = 6

; Seconds to wait before the single retry attempt on a failed file
retry_delay      = 3

; Maximum size of each log file (MB) before rotation at next run
max_log_size_mb  = 10

; Number of rotated log files to keep (oldest are pruned automatically)
log_retain_count = 5

; Working directory for session tracking files (manifest, queue, db)
session_dir      = /var/lib/spindlecrank

; Space check: fraction of raw (uncompressed) data size that must be free
; on the backup volume before the archive is opened.  1.0 = full raw size
; (conservative/safe).  Lower values (e.g. 0.4) trust compression to save space.
; Set to 0 to disable the proportional check entirely.
space_safety_factor = 1.0

; Absolute minimum free space (MB) required regardless of data size.
; The backup is aborted if the volume has less than this even before the
; proportional check is applied.
min_free_mb      = 512

; Process priority (Linux).  Keeps the backup from starving other workloads.
;   nice_level   0  = normal priority  /  10 = noticeably lower  /  19 = lowest
;   ionice_class 0  = disabled  /  2 = best-effort (reduced)  /  3 = idle only
nice_level       = 10
ionice_class     = 3

; SQLite commit batch size.  The completion DB is flushed every N successfully
; backed-up files rather than after every single one.  Lower values give more
; crash-recovery granularity; higher values reduce fsync overhead.
db_commit_batch  = 50

[directories]
; Source paths to back up, one per indented line.
include =
    /etc
    /home

[exclusions]
; Additional fnmatch filename patterns to exclude (built-ins always apply).
patterns =
    *.tmp
    .DS_Store
    Thumbs.db

; Additional directory paths to skip (fnmatch glob supported).
skip_dirs =
    /home/*/.cache
    /home/*/.local/share/Trash
    /home/*/.mozilla/firefox/*/Cache
"""

# ─── Utility functions ────────────────────────────────────────────────────────

def sha256_file(path: Path, chunk: int = 65_536) -> Optional[str]:
    """Return hex SHA-256 of a file's contents, or None on any error."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                h.update(data)
        return h.hexdigest()
    except OSError:
        return None


def human_size(nbytes: int) -> str:
    """Convert a byte count to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} PB"


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Log manager ──────────────────────────────────────────────────────────────

class LogManager:
    """
    Per-run verbose logging to /var/log/spindlecrank/ with count-based rotation.

    Files managed
    -------------
      backup_<session_id>.log  — one file per run; full DEBUG verbosity with
                                 millisecond timestamps, source file, and line
                                 numbers; rotated by run count (keep last N)
      failures.log             — cumulative one-line-per-failure record; rotated
                                 by size so it stays searchable across runs

    Rotation strategy
    -----------------
      Run logs    — at startup, all backup_*.log files are sorted newest-first
                    by filename (which is already a UTC timestamp).  Any file
                    beyond position <retain> is deleted immediately.  This means
                    the directory always contains at most <retain> completed run
                    logs plus the one currently being written.

      failures.log — size-based: rotated to failures.log.1/.2/… when the file
                    exceeds max_size_mb.  Kept as a cumulative record so admins
                    can grep across multiple runs.
    """

    def __init__(
        self,
        log_dir: Path,
        session_id: str,
        max_size_mb: int,
        retain: int,
    ) -> None:
        self.log_dir    = log_dir
        self.session_id = session_id
        self.max_bytes  = max_size_mb * 1_048_576
        self.retain     = retain

        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Per-run log — new file every invocation
        self.main_log    = log_dir / f"backup_{session_id}.log"
        # Cumulative failure log — appended across runs, size-rotated
        self.failure_log = log_dir / "failures.log"

    # ── run-log pruning (count-based) ─────────────────────────────────────────

    def _prune_run_logs(self) -> None:
        """
        Keep the <retain> most-recent backup_*.log files; delete the rest.

        Files are sorted by name descending — because the session_id embedded
        in each filename is a UTC timestamp (YYYYMMDD_HHMMSS), lexicographic
        order is identical to chronological order.  The file currently being
        opened has not been created yet, so it is not present in the glob and
        will not be accidentally pruned.
        """
        candidates = sorted(
            self.log_dir.glob("backup_????????_??????.log"),
            key=lambda p: p.name,
            reverse=True,   # newest first
        )
        # Keep (retain - 1) existing logs to leave a slot for the one about to
        # be opened; after this run completes there will be exactly <retain> files.
        for old in candidates[self.retain - 1:]:
            try:
                old.unlink()
            except OSError:
                pass  # best-effort; don't let cleanup abort the backup

    # ── failures.log size-based rotation ─────────────────────────────────────

    def _rotate_failures_log(self) -> None:
        """Rotate failures.log if it has reached the size threshold."""
        path = self.failure_log
        if not path.exists() or path.stat().st_size < self.max_bytes:
            return

        for i in range(self.retain, 0, -1):
            src = Path(f"{path}.{i}")
            dst = Path(f"{path}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)

        rotated = Path(f"{path}.1")
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)

        # Prune beyond retention window
        for i in range(self.retain + 1, self.retain + 50):
            stale = Path(f"{path}.{i}")
            if stale.exists():
                stale.unlink()
            else:
                break

    def rotate_all(self) -> None:
        """Apply all rotation/pruning rules.  Called once at startup."""
        self._prune_run_logs()
        self._rotate_failures_log()

    # ── initialise ────────────────────────────────────────────────────────────

    def setup(self) -> Tuple[logging.Logger, logging.Logger]:
        """
        Prune/rotate logs, attach handlers, and return (main_logger, fail_logger).

        Log formats
        -----------
          File (verbose):   timestamp.ms [LEVEL   ] session │ file:line │ message
          Console (clean):  timestamp [LEVEL   ] message
          Failures:         timestamp | message
        """
        self.rotate_all()

        ts_fmt = "%Y-%m-%d %H:%M:%S"

        # Verbose format written to the per-run log file:
        #   2024-03-15 02:03:44.127 [DEBUG   ] 20240315_020344 │ _process_file:L312 │ OK /etc/passwd
        verbose_fmt = logging.Formatter(
            fmt=(
                "%(asctime)s.%(msecs)03d [%(levelname)-8s] "
                f"{self.session_id} │ %(funcName)s:L%(lineno)d │ %(message)s"
            ),
            datefmt=ts_fmt,
        )

        # Clean format for the console — no noise, INFO and above only
        console_fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-8s] %(message)s",
            datefmt=ts_fmt,
        )

        # ── Main logger ───────────────────────────────────────────────────────
        log = logging.getLogger(SCRIPT_NAME)
        log.setLevel(logging.DEBUG)
        log.propagate = False

        # Per-run file handler — full DEBUG verbosity
        fh = logging.FileHandler(self.main_log, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(verbose_fmt)
        log.addHandler(fh)

        # Console handler — INFO and above, clean format
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(console_fmt)
        log.addHandler(ch)

        # ── Failure logger ────────────────────────────────────────────────────
        fail_log = logging.getLogger(f"{SCRIPT_NAME}.failures")
        fail_log.setLevel(logging.DEBUG)
        fail_log.propagate = False  # never bubble up to the main logger

        fail_fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt=ts_fmt)
        ffh = logging.FileHandler(self.failure_log, encoding="utf-8")
        ffh.setLevel(logging.DEBUG)
        ffh.setFormatter(fail_fmt)
        fail_log.addHandler(ffh)

        return log, fail_log


# ─── Configuration ────────────────────────────────────────────────────────────

class BackupConfig:
    """Loads and validates the backup INI file.  Creates a default if absent."""

    def __init__(self, ini_path: str) -> None:
        self.ini_path = Path(ini_path)
        self._ensure_config_exists()
        self._parse()

    def _ensure_config_exists(self) -> None:
        if not self.ini_path.exists():
            self.ini_path.parent.mkdir(parents=True, exist_ok=True)
            self.ini_path.write_text(DEFAULT_INI)
            print(f"[{SCRIPT_NAME}] Default config written to {self.ini_path}")

    def _parse(self) -> None:
        cp = configparser.ConfigParser(allow_no_value=True)
        cp.read(str(self.ini_path))

        g = cp["general"] if "general" in cp else {}

        self.backup_dir       = Path(g.get("backup_dir",       "/backups"))
        self.backup_prefix    = g.get("backup_prefix",          SCRIPT_NAME)
        self.compression      = g.get("compression",            "xz").lower()
        self.comp_level       = g.get("compression_level",      "6").lower()
        self.retry_delay          = float(g.get("retry_delay",          "3"))
        self.max_log_size_mb      = int(g.get("max_log_size_mb",        "10"))
        self.log_retain           = int(g.get("log_retain_count",       "5"))
        self.session_dir          = Path(g.get("session_dir", "/var/lib/spindlecrank"))
        self.space_safety_factor  = float(g.get("space_safety_factor",  "1.0"))
        self.min_free_mb          = int(g.get("min_free_mb",            "512"))
        self.nice_level           = int(g.get("nice_level",             "10"))
        self.ionice_class         = int(g.get("ionice_class",           "3"))
        self.db_commit_batch      = int(g.get("db_commit_batch",        "50"))

        # Source directories
        raw = cp.get("directories", "include", fallback="")
        self.source_dirs: List[Path] = [
            Path(p.strip()) for p in raw.splitlines() if p.strip()
        ]

        # Extra exclusion patterns
        raw_pats = cp.get("exclusions", "patterns", fallback="")
        self.extra_patterns: List[str] = [
            p.strip() for p in raw_pats.splitlines() if p.strip()
        ]

        # Extra skip directories
        raw_skip = cp.get("exclusions", "skip_dirs", fallback="")
        self.extra_skip_dirs: List[str] = [
            p.strip() for p in raw_skip.splitlines() if p.strip()
        ]

    # ── derived properties ────────────────────────────────────────────────────

    @property
    def lzma_preset(self) -> int:
        """Return the LZMA integer preset (0–9, with optional EXTREME flag)."""
        if self.comp_level == "extreme":
            return 9 | lzma.PRESET_EXTREME
        try:
            return max(0, min(9, int(self.comp_level)))
        except ValueError:
            return 9 | lzma.PRESET_EXTREME

    @property
    def archive_extension(self) -> str:
        return {"xz": ".tar.xz", "bz2": ".tar.bz2", "gz": ".tar.gz"}.get(
            self.compression, ".tar.xz"
        )


# ─── Exclusion filter ─────────────────────────────────────────────────────────

class ExclusionFilter:
    """
    Decides whether a path should be excluded from the backup.
    Combines built-in rules with user-configured patterns from the INI file.
    """

    def __init__(self, config: BackupConfig) -> None:
        self.skip_dirs: Set[str] = set(BUILTIN_SKIP_DIRS)
        for d in config.extra_skip_dirs:
            self.skip_dirs.add(d.rstrip("/"))

        self.patterns: List[str] = BUILTIN_SKIP_PATTERNS + config.extra_patterns
        # Pre-compile patterns once; fnmatch.translate() recompiles on every call
        self._pattern_res = [re.compile(fnmatch.translate(p)) for p in self.patterns]

    def should_skip_dir(self, dirpath: str) -> bool:
        """Return True if the directory (or any parent) should be skipped."""
        norm = dirpath.rstrip("/")
        basename = os.path.basename(norm)
        for skip in self.skip_dirs:
            if (
                fnmatch.fnmatch(norm, skip)
                or norm == skip
                or norm.startswith(skip + "/")
            ):
                return True
        for pat_re in self._pattern_res:
            if pat_re.match(basename):
                return True
        return False

    def should_skip_file(
        self, filepath: str, st: os.stat_result
    ) -> Tuple[bool, str]:
        """
        Return (True, reason) if the file should be excluded;
        (False, "") if it should be backed up.
        """
        # Reject by file type
        file_type = stat.S_IFMT(st.st_mode)
        if file_type in SKIP_STAT_TYPES:
            labels = {
                stat.S_IFBLK:  "block device",
                stat.S_IFCHR:  "character device",
                stat.S_IFIFO:  "named pipe",
                stat.S_IFSOCK: "socket",
            }
            return True, f"excluded file type: {labels.get(file_type, hex(file_type))}"

        # Reject by name pattern
        name = os.path.basename(filepath)
        for i, pat_re in enumerate(self._pattern_res):
            if pat_re.match(name):
                return True, f"matches exclusion pattern '{self.patterns[i]}'"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
#  TRACKING MECHANISM 1 — Pre-scan JSON Lines Manifest
# ═══════════════════════════════════════════════════════════════════════════════

class FileManifest:
    """
    Immutable pre-scan record of every file that *should* be backed up.

    Written as a JSON Lines (.jsonl) file before the archive is opened.
    Each line is a JSON object with the file's path and key stat metadata.
    This file is the definitive answer to "what was in scope for this run?"
    and is used in Phase 3 to verify complete coverage.
    """

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.path = session_dir / f"manifest_{session_id}.jsonl"
        self._fh: Optional[object] = None

    def open_for_write(self) -> None:
        self._fh = open(self.path, "w", encoding="utf-8")

    def write_entry(self, filepath: str, st: os.stat_result) -> None:
        record = {
            "path":  filepath,
            "size":  st.st_size,
            "mtime": st.st_mtime,
            "inode": st.st_ino,
            "mode":  st.st_mode,
        }
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")  # type: ignore[union-attr]

    def close(self) -> None:
        if self._fh:
            self._fh.close()  # type: ignore[union-attr]
            self._fh = None

    def count(self) -> int:
        n = 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        n += 1
        except FileNotFoundError:
            pass
        return n

    def iter_paths(self) -> Generator[str, None, None]:
        """Yield every file path recorded in this manifest."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)["path"]
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  TRACKING MECHANISM 2 — Append-only JSONL Work Queue
# ═══════════════════════════════════════════════════════════════════════════════

class WorkQueue:
    """
    Append-only per-file processing audit trail.

    Every state transition is recorded as a timestamped JSON line:

        pending  → processing → done
                             → retry → done
                                     → failed
                → skipped

    Because the file is append-only it survives process crashes and can be
    replayed to determine exactly where a run left off.
    """

    PENDING    = "pending"
    PROCESSING = "processing"
    DONE       = "done"
    RETRY      = "retry"
    FAILED     = "failed"
    SKIPPED    = "skipped"

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.path = session_dir / f"queue_{session_id}.jsonl"
        self._fh  = open(self.path, "a", encoding="utf-8")

    # ── write helpers ─────────────────────────────────────────────────────────

    def _append(self, record: dict) -> None:
        record["ts"] = utcnow_str()
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def mark_pending(self, filepath: str)                            -> None:
        self._append({"path": filepath, "status": self.PENDING})

    def mark_processing(self, filepath: str)                         -> None:
        self._append({"path": filepath, "status": self.PROCESSING})

    def mark_done(self, filepath: str, checksum: str, size: int)     -> None:
        self._append({"path": filepath, "status": self.DONE,
                      "sha256": checksum, "size": size})

    def mark_retry(self, filepath: str, error: str)                  -> None:
        self._append({"path": filepath, "status": self.RETRY, "error": error})

    def mark_failed(self, filepath: str, error: str)                 -> None:
        self._append({"path": filepath, "status": self.FAILED, "error": error})

    def mark_skipped(self, filepath: str, reason: str)               -> None:
        self._append({"path": filepath, "status": self.SKIPPED, "reason": reason})

    # ── read helpers ──────────────────────────────────────────────────────────

    def completed_paths(self) -> Set[str]:
        """Return the set of paths whose last recorded status is DONE."""
        done: Set[str] = set()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    p   = rec.get("path", "")
                    s   = rec.get("status", "")
                    if s == self.DONE:
                        done.add(p)
                    elif s in (self.FAILED, self.SKIPPED):
                        done.discard(p)
        except FileNotFoundError:
            pass
        return done

    def close(self) -> None:
        self._fh.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  TRACKING MECHANISM 3 — SQLite Completion Database
# ═══════════════════════════════════════════════════════════════════════════════

class CompletionDB:
    """
    Queryable SQLite database of every per-file outcome for a backup session.

    Tables
    ------
      completed — successfully archived files (with SHA-256 checksum)
      failed    — files that could not be archived after all retry attempts
      skipped   — files excluded before archive processing began

    Used in Phase 3 to cross-reference against the manifest and work queue,
    ensuring no file is silently unaccounted for.
    """

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.path  = session_dir / f"completion_{session_id}.db"
        self._conn = sqlite3.connect(str(self.path))
        self._create_schema()

    def _create_schema(self) -> None:
        self._batch   = 0
        self._batch_n = 50   # overridden by set_batch_size() after construction
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS completed (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT    NOT NULL UNIQUE,
                size        INTEGER,
                sha256      TEXT,
                backed_up   TEXT
            );
            CREATE TABLE IF NOT EXISTS failed (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT    NOT NULL,
                error       TEXT,
                attempt     INTEGER,
                failed_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS skipped (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT    NOT NULL UNIQUE,
                reason      TEXT,
                skipped_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_completed_path ON completed(path);
            CREATE INDEX IF NOT EXISTS idx_failed_path    ON failed(path);
        """)
        self._conn.commit()

    # ── write ─────────────────────────────────────────────────────────────────
    # Commits are batched to reduce fsync overhead.  Failures and skips are
    # committed immediately (low frequency); successes are batched and flushed
    # either when the batch fills or when flush() is called at run end.

    def set_batch_size(self, n: int) -> None:
        self._batch   = 0
        self._batch_n = max(1, n)

    def _maybe_commit(self) -> None:
        self._batch += 1
        if self._batch >= self._batch_n:
            self._conn.commit()
            self._batch = 0

    def flush(self) -> None:
        """Force a commit of any pending batched writes."""
        if self._batch:
            self._conn.commit()
            self._batch = 0

    def record_success(self, filepath: str, size: int, sha256: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO completed (path, size, sha256, backed_up) "
            "VALUES (?, ?, ?, ?)",
            (filepath, size, sha256, utcnow_str()),
        )
        self._maybe_commit()

    def record_failure(self, filepath: str, error: str, attempt: int) -> None:
        self._conn.execute(
            "INSERT INTO failed (path, error, attempt, failed_at) VALUES (?, ?, ?, ?)",
            (filepath, error, attempt, utcnow_str()),
        )
        self._conn.commit()   # failures are infrequent — commit immediately

    def record_skipped(self, filepath: str, reason: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO skipped (path, reason, skipped_at) VALUES (?, ?, ?)",
            (filepath, reason, utcnow_str()),
        )
        self._conn.commit()   # skips happen at scan time — commit immediately

    # ── read ──────────────────────────────────────────────────────────────────

    def completed_paths(self) -> Set[str]:
        return {r[0] for r in self._conn.execute("SELECT path FROM completed")}

    def failed_paths(self) -> Set[str]:
        return {r[0] for r in self._conn.execute("SELECT path FROM failed")}

    def skipped_paths(self) -> Set[str]:
        return {r[0] for r in self._conn.execute("SELECT path FROM skipped")}

    def summary(self) -> Dict[str, int]:
        return {
            "completed": self._conn.execute("SELECT COUNT(*) FROM completed").fetchone()[0],
            "failed":    self._conn.execute("SELECT COUNT(*) FROM failed").fetchone()[0],
            "skipped":   self._conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0],
        }

    def close(self) -> None:
        self._conn.close()


# ─── Process priority ────────────────────────────────────────────────────────

def set_process_priority(nice_level: int, ionice_class: int) -> None:
    """
    Lower CPU and I/O priority so the backup does not starve other workloads.

    CPU niceness
    ------------
    Uses os.setpriority() (POSIX) to set an absolute nice value.  0 = normal,
    10 = noticeably lower, 19 = lowest possible.  Silently skipped on platforms
    that do not support it (e.g. Windows).

    I/O scheduling (Linux only)
    ---------------------------
    Calls the ionice(1) utility to move the process into the requested I/O
    scheduling class:
      2 = best-effort (reduced priority within normal I/O pool)
      3 = idle  (only gets I/O time when no other process needs the disk)
    Set ionice_class = 0 in the config to leave I/O scheduling unchanged.
    ionice is skipped silently if the utility is not installed.
    """
    # ── CPU niceness ──────────────────────────────────────────────────────────
    try:
        os.setpriority(os.PRIO_PROCESS, 0, max(0, min(19, nice_level)))
    except (AttributeError, OSError):
        pass  # not POSIX, or permission denied (e.g. trying to raise priority)

    # ── I/O scheduling ────────────────────────────────────────────────────────
    if ionice_class > 0:
        import subprocess as _sp
        try:
            _sp.run(
                ["ionice", "-c", str(ionice_class), "-p", str(os.getpid())],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, OSError, _sp.TimeoutExpired):
            pass  # ionice not available — not a fatal condition


# ─── Hashing file wrapper ─────────────────────────────────────────────────────

class _HashingReader:
    """
    Wraps a binary file object and computes a SHA-256 digest as data flows
    through it.  This lets tarfile stream the file into the archive while the
    checksum is produced in the same pass — eliminating the second full read
    that a separate sha256_file() call would require.

    Only the read() method is implemented because that is all tarfile.addfile()
    calls on the fileobj.
    """

    __slots__ = ("_fh", "_h")

    def __init__(self, fh) -> None:
        self._fh = fh
        self._h  = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._fh.read(n)
        if data:
            self._h.update(data)
        return data

    @property
    def hexdigest(self) -> str:
        return self._h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
#  Backup Engine
# ═══════════════════════════════════════════════════════════════════════════════

class BackupEngine:
    """
    Orchestrates the three-phase backup workflow:

      Phase 1 — Pre-scan:   walk source dirs, build manifest (Mechanism 1),
                             seed work queue (Mechanism 2).
      Phase 2 — Backup:     open LZMA-compressed streaming tar, process each
                             file with probe → archive-write → retry loop.
                             Each outcome recorded to all three trackers.
      Phase 3 — Verify:     cross-reference manifest vs. work queue vs. DB;
                             report any unaccounted files.
    """

    def __init__(
        self, config: BackupConfig, session_id: str, dry_run: bool = False
    ) -> None:
        self.config     = config
        self.dry_run    = dry_run
        self.excl       = ExclusionFilter(config)
        self.session_id = session_id
        self.hostname   = socket.gethostname()

        config.backup_dir.mkdir(parents=True, exist_ok=True)
        config.session_dir.mkdir(parents=True, exist_ok=True)

        archive_name      = (
            f"{config.backup_prefix}_{self.hostname}"
            f"_{self.session_id}{config.archive_extension}"
        )
        self.archive_path = config.backup_dir / archive_name

        # Loggers (handlers attached by LogManager.setup() before we are created)
        self.log  = logging.getLogger(SCRIPT_NAME)
        self.flog = logging.getLogger(f"{SCRIPT_NAME}.failures")

        # Tracking objects — instantiated in run()
        self.manifest: Optional[FileManifest] = None
        self.queue:    Optional[WorkQueue]    = None
        self.db:       Optional[CompletionDB] = None

        # Counters
        self.stats: Dict[str, int] = {
            "scanned":   0,
            "backed_up": 0,
            "retried":   0,
            "failed":    0,
            "skipped":   0,
        }

        self._abort = False
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT,  self._on_signal)

    # ── signal handling ───────────────────────────────────────────────────────

    def _on_signal(self, signum: int, frame: object) -> None:
        self.log.warning(
            "Signal %d received — stopping after current file …", signum
        )
        self._abort = True

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 1 helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_source_dirs(
        self,
    ) -> Generator[Tuple[str, os.stat_result], None, None]:
        """
        Walk every source directory and yield (filepath, stat_result) for each
        file that passes the exclusion filter.

        Hard-linked files (same device + inode) are deduplicated so they are
        stored only once in the archive.
        """
        seen_inodes: Set[Tuple[int, int]] = set()

        for src_dir in self.config.source_dirs:
            src = str(src_dir)
            if not os.path.isdir(src):
                self.log.warning("Source directory not found, skipping: %s", src)
                continue
            self.log.info("Scanning: %s", src)

            for dirpath, dirnames, filenames in os.walk(
                src, topdown=True, followlinks=False
            ):
                # Prune traversal in-place — this is critical for correctness
                dirnames[:] = [
                    d for d in dirnames
                    if not self.excl.should_skip_dir(os.path.join(dirpath, d))
                ]

                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)

                    try:
                        st = os.lstat(fpath)
                    except OSError as exc:
                        reason = f"cannot stat: {exc}"
                        self.log.debug("Skip %s — %s", fpath, reason)
                        self.stats["skipped"] += 1
                        if self.queue:
                            self.queue.mark_skipped(fpath, reason)
                        if self.db:
                            self.db.record_skipped(fpath, reason)
                        continue

                    skip, reason = self.excl.should_skip_file(fpath, st)
                    if skip:
                        self.log.debug("Skip %s — %s", fpath, reason)
                        self.stats["skipped"] += 1
                        if self.queue:
                            self.queue.mark_skipped(fpath, reason)
                        if self.db:
                            self.db.record_skipped(fpath, reason)
                        continue

                    # Deduplicate hard links
                    inode_key = (st.st_dev, st.st_ino)
                    if inode_key in seen_inodes:
                        self.log.debug("Skip hard-link duplicate: %s", fpath)
                        continue
                    seen_inodes.add(inode_key)

                    self.stats["scanned"] += 1
                    yield fpath, st

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 2 helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _probe_file(filepath: str) -> Optional[str]:
        """
        Verify the file can be opened and read before the archive is touched.
        A minimal 1-byte read is sufficient — its purpose is to surface
        permission errors and hard locks without pulling data into the page
        cache needlessly.  Returns None on success, error string on failure.
        """
        try:
            with open(filepath, "rb") as fh:
                fh.read(1)
            return None
        except OSError as exc:
            return str(exc)

    def _archive_add(
        self, tar: tarfile.TarFile, filepath: str
    ) -> Tuple[Optional[str], str]:
        """
        Stream one file into the open TarFile and compute its SHA-256 in the
        same pass using _HashingReader.

        Returns (error_string, checksum):
          • On success: (None, hex_sha256)
          • On failure: (error_string, "")

        Computing the checksum here eliminates the separate sha256_file() call
        that would otherwise read every file a second time.
        """
        try:
            info = tar.gettarinfo(name=filepath)
            info.name = filepath.lstrip("/")   # store as relative path in archive
            with open(filepath, "rb") as raw:
                reader = _HashingReader(raw)
                tar.addfile(tarinfo=info, fileobj=reader)
            return None, reader.hexdigest
        except Exception as exc:  # noqa: BLE001
            return str(exc), ""

    def _process_file(
        self,
        tar: tarfile.TarFile,
        filepath: str,
        st: os.stat_result,
    ) -> None:
        """
        Process one file through the full probe → add → retry cycle.

        Attempt 1
        ---------
          • Probe the file for readability.
          • If probe succeeds, attempt to add it to the archive.
          • If either step fails, log a retry notice and pause.

        Attempt 2 (retry — only if attempt 1 failed)
        ----------------------------------------------
          • Repeat the probe + archive-add sequence after retry_delay seconds.
          • On success, record as normal.
          • On second failure, record permanently to the failures log and the
            completion DB; skip the file and continue.

        All three tracking mechanisms are updated at every transition.
        """
        assert self.queue is not None
        assert self.db    is not None

        self.queue.mark_processing(filepath)

        for attempt in range(1, 3):     # attempts 1 and 2
            if attempt == 2:
                time.sleep(self.config.retry_delay)
                self.stats["retried"] += 1

            # ── Probe ─────────────────────────────────────────────────────────
            probe_err = self._probe_file(filepath)
            if probe_err:
                if attempt == 1:
                    self.log.warning(
                        "Probe failed (attempt 1), will retry: %s — %s",
                        filepath, probe_err,
                    )
                    self.queue.mark_retry(filepath, f"probe: {probe_err}")
                    continue
                else:
                    msg = f"probe failed on retry: {probe_err}"
                    self.log.error("PERM FAIL: %s — %s", filepath, msg)
                    self.queue.mark_failed(filepath, msg)
                    self.db.record_failure(filepath, msg, attempt)
                    self.flog.error("FAILED | probe | %s | %s", filepath, msg)
                    self.stats["failed"] += 1
                    return

            # ── Archive write ─────────────────────────────────────────────────
            write_err, chk = self._archive_add(tar, filepath)
            if write_err:
                if attempt == 1:
                    self.log.warning(
                        "Archive write failed (attempt 1), will retry: %s — %s",
                        filepath, write_err,
                    )
                    self.queue.mark_retry(filepath, f"write: {write_err}")
                    continue
                else:
                    msg = f"archive write failed on retry: {write_err}"
                    self.log.error("PERM FAIL: %s — %s", filepath, msg)
                    self.queue.mark_failed(filepath, msg)
                    self.db.record_failure(filepath, msg, attempt)
                    self.flog.error("FAILED | write | %s | %s", filepath, msg)
                    self.stats["failed"] += 1
                    return

            # ── Success — checksum already computed during the archive write ──
            self.queue.mark_done(filepath, chk, st.st_size)
            self.db.record_success(filepath, st.st_size, chk)
            self.stats["backed_up"] += 1
            self.log.debug("  OK  %s  [%s]", filepath, human_size(st.st_size))
            return  # success — exit the attempt loop

    # ─────────────────────────────────────────────────────────────────────────
    #  Space check — runs between Phase 1 and Phase 2
    # ─────────────────────────────────────────────────────────────────────────

    def _check_space(self, file_list: List[Tuple[str, os.stat_result]]) -> bool:
        """
        Verify the backup volume has sufficient free space before opening the
        archive.  Returns True if it is safe to proceed, False to abort.

        Two independent thresholds are enforced (both must pass):

          1. Absolute floor — the volume must have at least ``min_free_mb`` MB
             free regardless of how much data is to be backed up.

          2. Proportional estimate — the volume must have at least
             ``(total_raw_bytes * space_safety_factor)`` bytes free.
             A factor of 1.0 (default) requires enough space for the full
             uncompressed data, which is the safest conservative estimate.
             Lower values (e.g. 0.4) trade safety for permissiveness by
             trusting that XZ compression will save significant space.

        On failure the reason is logged to both the main log and failures.log
        and the caller should return exit code 2.
        """
        # ── Gather facts ──────────────────────────────────────────────────────
        try:
            usage = shutil.disk_usage(str(self.config.backup_dir))
        except OSError as exc:
            msg = f"cannot stat backup volume '{self.config.backup_dir}': {exc}"
            self.log.error("Space check FAILED — %s", msg)
            self.flog.error("ABORT | space check | %s", msg)
            return False

        free_bytes      = usage.free
        total_raw_bytes = sum(st.st_size for _, st in file_list)
        min_free_bytes  = self.config.min_free_mb * 1_048_576
        required_bytes  = int(total_raw_bytes * self.config.space_safety_factor)

        self.log.info("─" * 60)
        self.log.info("Space check")
        self.log.info("  Backup volume   : %s", self.config.backup_dir)
        self.log.info("  Volume free     : %s  (total %s, used %s)",
                      human_size(free_bytes),
                      human_size(usage.total),
                      human_size(usage.used))
        self.log.info("  Raw data size   : %s  (%d files)",
                      human_size(total_raw_bytes), len(file_list))
        self.log.info("  Safety factor   : %.2f  →  need %s",
                      self.config.space_safety_factor, human_size(required_bytes))
        self.log.info("  Absolute floor  : %s", human_size(min_free_bytes))

        # ── Check 1: absolute floor ───────────────────────────────────────────
        if free_bytes < min_free_bytes:
            msg = (
                f"backup volume has only {human_size(free_bytes)} free — "
                f"below the {human_size(min_free_bytes)} absolute floor "
                f"(min_free_mb={self.config.min_free_mb})"
            )
            self.log.error("Space check FAILED — %s", msg)
            self.flog.error("ABORT | insufficient space (floor) | %s | %s",
                            self.config.backup_dir, msg)
            return False

        # ── Check 2: proportional estimate ───────────────────────────────────
        if self.config.space_safety_factor > 0 and free_bytes < required_bytes:
            shortage = required_bytes - free_bytes
            msg = (
                f"backup volume has {human_size(free_bytes)} free but "
                f"{human_size(required_bytes)} is required "
                f"({human_size(total_raw_bytes)} raw × {self.config.space_safety_factor:.2f} "
                f"safety factor) — short by {human_size(shortage)}"
            )
            self.log.error("Space check FAILED — %s", msg)
            self.flog.error("ABORT | insufficient space (estimate) | %s | %s",
                            self.config.backup_dir, msg)
            return False

        self.log.info("  Result          : OK — %s headroom above estimate",
                      human_size(free_bytes - required_bytes))
        return True

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 3 — Cross-reference verification
    # ─────────────────────────────────────────────────────────────────────────

    def _verify(self) -> None:
        """
        Cross-reference the three tracking mechanisms.

        Checks performed
        ----------------
          1. Every manifest path is accounted for in at least one of:
             queue-done, DB-completed, DB-failed, or DB-skipped.
          2. queue-done paths match DB-completed paths (no ghost entries).
          3. Report any unaccounted files as errors and add them to the
             failures log for administrator investigation.
        """
        assert self.manifest is not None
        assert self.queue    is not None
        assert self.db       is not None

        manifest_paths = set(self.manifest.iter_paths())
        queue_done     = self.queue.completed_paths()
        db_completed   = self.db.completed_paths()
        db_failed      = self.db.failed_paths()
        db_skipped     = self.db.skipped_paths()

        self.log.info("─" * 60)
        self.log.info("Phase 3: Cross-reference verification")
        self.log.info("─" * 60)
        self.log.info("  Manifest entries  : %d", len(manifest_paths))
        self.log.info("  Queue → DONE      : %d", len(queue_done))
        self.log.info("  DB completed      : %d", len(db_completed))
        self.log.info("  DB failed         : %d", len(db_failed))
        self.log.info("  DB skipped        : %d", len(db_skipped))

        # Check 1: everything from the manifest must be accounted for
        accounted = queue_done | db_completed | db_failed | db_skipped
        missed    = manifest_paths - accounted

        if missed:
            self.log.error(
                "VERIFICATION FAILED — %d manifest file(s) are unaccounted for!",
                len(missed),
            )
            for p in sorted(missed)[:50]:
                self.log.error("  unaccounted: %s", p)
                self.flog.error(
                    "UNACCOUNTED | not in queue/DB completed/failed/skipped | %s", p
                )
            if len(missed) > 50:
                self.log.error("  … and %d more (see failures.log)", len(missed) - 50)
        else:
            self.log.info("  Result: ALL manifest entries accounted for.")

        # Check 2: queue-done vs DB-completed parity
        q_not_db = queue_done - db_completed
        db_not_q = db_completed - queue_done

        if q_not_db:
            self.log.warning(
                "  Discrepancy: %d file(s) marked DONE in queue but absent from DB",
                len(q_not_db),
            )
        if db_not_q:
            self.log.warning(
                "  Discrepancy: %d file(s) in DB but absent from queue DONE records",
                len(db_not_q),
            )

        if not missed and not q_not_db and not db_not_q:
            self.log.info("  All three tracking mechanisms are consistent.")

    # ─────────────────────────────────────────────────────────────────────────
    #  Main entry point
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> int:
        """
        Execute the full backup workflow.

        Returns
        -------
          0 — complete success, all files backed up
          1 — partial success, one or more files permanently failed
          2 — fatal configuration or I/O error
        """
        t_start = time.monotonic()

        self.log.info("╔══════════════════════════════════════════════╗")
        self.log.info("║  %-44s║", f"{SCRIPT_NAME.capitalize()} Backup  v{VERSION}")
        self.log.info("║  Session : %-34s║", self.session_id)
        self.log.info("║  Host    : %-34s║", self.hostname)
        self.log.info("║  Config  : %-34s║", str(self.config.ini_path))
        self.log.info("╚══════════════════════════════════════════════╝")

        if not self.config.source_dirs:
            self.log.error(
                "No [directories] configured.  Edit %s and add source paths.",
                self.config.ini_path,
            )
            return 2

        # ── Initialise tracking objects ───────────────────────────────────────
        self.queue = WorkQueue(self.config.session_dir, self.session_id)
        self.db    = CompletionDB(self.config.session_dir, self.session_id)
        self.db.set_batch_size(self.config.db_commit_batch)

        try:
            # ── Phase 1: Pre-scan ─────────────────────────────────────────────
            self.log.info("─" * 60)
            self.log.info("Phase 1: Pre-scan — building file manifest")
            self.log.info("─" * 60)

            self.manifest = FileManifest(self.config.session_dir, self.session_id)
            self.manifest.open_for_write()

            file_list: List[Tuple[str, os.stat_result]] = []
            for filepath, st in self._scan_source_dirs():
                self.manifest.write_entry(filepath, st)
                file_list.append((filepath, st))
                if len(file_list) % 5_000 == 0:
                    self.log.info("  … %d files enumerated …", len(file_list))

            self.manifest.close()
            self.log.info(
                "Pre-scan complete: %d files in scope, %d pre-excluded",
                len(file_list), self.stats["skipped"],
            )

            # ── Space check — abort before touching the archive ───────────────
            if not self.dry_run and not self._check_space(file_list):
                return 2

            # Seed the work queue with every pending path
            self.log.info("Seeding work queue …")
            for filepath, _ in file_list:
                self.queue.mark_pending(filepath)

            # ── Phase 2: Backup ───────────────────────────────────────────────
            self.log.info("─" * 60)
            self.log.info("Phase 2: Backup — writing archive")
            self.log.info("  Archive     : %s", self.archive_path)
            self.log.info("  Files       : %d", len(file_list))
            self.log.info("  Compression : %s (preset %d)",
                          self.config.compression.upper(),
                          self.config.lzma_preset)
            self.log.info("─" * 60)

            if self.dry_run:
                self.log.info("[DRY RUN] Archive would be: %s", self.archive_path)
                for fp, st in file_list:
                    self.log.info("  [DRY] %s  [%s]", fp, human_size(st.st_size))
                    self.stats["backed_up"] += 1
            else:
                # Open LZMA stream → streaming tar (no random access needed)
                with lzma.open(
                    str(self.archive_path),
                    mode="wb",
                    format=lzma.FORMAT_XZ,
                    preset=self.config.lzma_preset,
                ) as lzma_fh:
                    with tarfile.open(fileobj=lzma_fh, mode="w|") as tar:
                        total = len(file_list)
                        for idx, (filepath, st) in enumerate(file_list, 1):
                            if self._abort:
                                self.log.warning(
                                    "Abort flag set — halting at %d / %d", idx, total
                                )
                                break

                            if idx == 1 or idx % 1_000 == 0:
                                pct = idx * 100 // total
                                self.log.info(
                                    "Progress: %d/%d (%d%%)  "
                                    "ok=%d  retry=%d  fail=%d  skip=%d",
                                    idx, total, pct,
                                    self.stats["backed_up"],
                                    self.stats["retried"],
                                    self.stats["failed"],
                                    self.stats["skipped"],
                                )

                            self._process_file(tar, filepath, st)

            # Flush any remaining batched DB writes before verification
            if self.db:
                self.db.flush()

            # ── Phase 3: Verify ───────────────────────────────────────────────
            if not self.dry_run:
                self._verify()

        except Exception:
            self.log.exception("Fatal unhandled error during backup run")
            return 2

        finally:
            if self.queue:
                self.queue.close()
            if self.db:
                self.db.close()
            if self.manifest:
                self.manifest.close()

        # ── Final summary ─────────────────────────────────────────────────────
        elapsed = time.monotonic() - t_start

        self.log.info("═" * 60)
        self.log.info("BACKUP COMPLETE — SESSION %s", self.session_id)
        if not self.dry_run and self.archive_path.exists():
            arc_sz = self.archive_path.stat().st_size
            self.log.info("  Archive     : %s", self.archive_path)
            self.log.info("  Size        : %s", human_size(arc_sz))
        self.log.info("  Scanned     : %d", self.stats["scanned"])
        self.log.info("  Backed up   : %d", self.stats["backed_up"])
        self.log.info("  Retried     : %d", self.stats["retried"])
        self.log.info("  Failed      : %d", self.stats["failed"])
        self.log.info("  Skipped     : %d", self.stats["skipped"])
        self.log.info("  Elapsed     : %.1f s  (%.1f min)", elapsed, elapsed / 60)
        if self.stats["failed"]:
            self.log.warning(
                "  %d file(s) permanently failed — review %s/failures.log",
                self.stats["failed"], LOG_DIR,
            )
        self.log.info("  Session dir : %s", self.config.session_dir)
        self.log.info("═" * 60)

        return 1 if self.stats["failed"] else 0


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    check_runtime_dependencies()

    parser = argparse.ArgumentParser(
        prog="spindlecrank_backup.py",
        description=(
            f"{SCRIPT_NAME} — Robust single-file-at-a-time Linux backup utility "
            f"v{VERSION}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              Run backup with default config
  %(prog)s --config /my/backup.ini      Use an alternate config file
  %(prog)s --dry-run                    Scan and list without writing an archive
  %(prog)s --generate-config            Write a default config and exit
""",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help=f"Path to INI config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan source directories and log what would be backed up; do not write an archive",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="Write a default config to --config path and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    args = parser.parse_args()

    if args.generate_config:
        dest = Path(args.config)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(DEFAULT_INI)
        print(f"Default config written to {dest}")
        sys.exit(0)

    # Load config (creates a default if absent)
    try:
        config = BackupConfig(args.config)
    except Exception as exc:
        print(f"[ERROR] Cannot load config '{args.config}': {exc}", file=sys.stderr)
        sys.exit(2)

    # Lower CPU and I/O priority before any real work begins
    set_process_priority(config.nice_level, config.ionice_class)

    # Single session ID shared by the log filename and all tracking files
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Initialise logging — prunes old run logs, rotates failures.log if needed
    log_mgr = LogManager(
        log_dir=LOG_DIR,
        session_id=session_id,
        max_size_mb=config.max_log_size_mb,
        retain=config.log_retain,
    )
    log_mgr.setup()

    # Run the backup
    engine = BackupEngine(config, session_id=session_id, dry_run=args.dry_run)
    sys.exit(engine.run())


if __name__ == "__main__":
    main()
