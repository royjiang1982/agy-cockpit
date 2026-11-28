"""Shared Sidecar paths, bounded caches, file I/O and scalar parsing."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableMapping
from contextlib import contextmanager
import contextlib
from datetime import datetime
import functools
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time

HOME = Path.home()
APP_DIRS = [
    ("App", HOME / ".gemini" / "antigravity"),
    ("CLI", HOME / ".gemini" / "antigravity-cli"),
]
CODEX_SESSIONS_DIR = HOME / ".codex" / "sessions"
PI_SESSIONS_DIR = HOME / ".pi" / "agent" / "sessions"
FACTORY_SESSIONS_DIR = HOME / ".factory" / "sessions"
GROK_SESSIONS_DIR = HOME / ".grok" / "sessions"
SUBAGENT_APP_DIRS = [*APP_DIRS, ("CLI", HOME / ".gemini" / "antigravity-subagent")]

CLI_BIN_DIRS = (HOME / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
_CACHE_STRIPES = [threading.RLock() for _ in range(64)]


class BoundedCache(MutableMapping):
    def __init__(self, capacity=128):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.data = OrderedDict()

    def __getitem__(self, key):
        with self.lock:
            value = self.data[key]
            self.data.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.capacity:
                self.data.popitem(last=False)

    def __delitem__(self, key):
        with self.lock:
            del self.data[key]

    def __iter__(self):
        with self.lock:
            return iter(tuple(self.data))

    def __len__(self):
        with self.lock:
            return len(self.data)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key, default=None):
        with self.lock:
            if key not in self.data:
                self[key] = default
            return self[key]

    def clear(self):
        with self.lock:
            self.data.clear()

    def publish(self, key, value, version):
        """Reject an older computation that finished after a newer one."""
        with self.lock:
            previous = self.data.get(key)
            if previous is not None and version(previous) > version(value):
                return False
            self[key] = value
            return True


def serialized(key_fn):
    def decorate(fn):
        locks = [threading.RLock() for _ in range(64)]
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with locks[hash(key_fn(*args, **kwargs)) % len(locks)]:
                return fn(*args, **kwargs)
        return wrapped
    return decorate


_DIRECTORY_CACHE = BoundedCache(128)
_LOG_LINES_CACHE = BoundedCache(32)


def cached_paths(base: Path, pattern: str, recursive=False, ttl=1.0) -> tuple[Path, ...]:
    key = (str(base), pattern, recursive)
    now = time.monotonic()
    cached = _DIRECTORY_CACHE.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1]
    paths = tuple(base.rglob(pattern) if recursive else base.glob(pattern))
    _DIRECTORY_CACHE[key] = (now, paths)
    return paths


def log_lines(path: Path):
    """Read append-only logs incrementally; retain at most 4 MiB per cached file.

    An incomplete final line is retried after the next append. Large files are
    streamed without retaining a second complete text copy.
    """
    st = path.stat()
    key = str(path)
    signature = (st.st_dev, st.st_ino)
    with _CACHE_STRIPES[hash(('log', key)) % len(_CACHE_STRIPES)]:
        cached = _LOG_LINES_CACHE.get(key)
        lines, offset = [], 0
        if cached and cached[0] == signature and st.st_size >= cached[1] and st.st_mtime_ns >= cached[2]:
            if st.st_size == cached[1] and st.st_mtime_ns == cached[2]:
                return iter(cached[3] + ((cached[5],) if cached[5] is not None else ()))
            if st.st_size > cached[1]:
                lines, offset = list(cached[3]), cached[4]
        retained = sum(len(line) for line in lines)
        final_line = None
        with path.open('rb') as stream:
            stream.seek(offset)
            for raw in stream:
                if not raw.endswith(b'\n'):
                    # A valid final JSON record is usable even without a newline,
                    # but keep its start offset so appends cannot lose a record.
                    text = raw.decode('utf-8', errors='replace')
                    try:
                        json.loads(text)
                    except (ValueError, TypeError):
                        break
                    final_line = text
                    break
                lines.append(raw.decode('utf-8', errors='replace').rstrip('\r\n'))
                offset = stream.tell()
                retained += len(raw)
                if retained > 4 * 1024 * 1024:
                    _LOG_LINES_CACHE.pop(key, None)
                    return _stream_log_lines(path)
        _LOG_LINES_CACHE[key] = (signature, st.st_size, st.st_mtime_ns, tuple(lines), offset, final_line)
        return iter(lines + ([final_line] if final_line is not None else []))


def _stream_log_lines(path):
    with path.open(encoding='utf-8', errors='replace') as stream:
        yield from stream


class _ReadonlyCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "BEGIN" and self.connection.in_transaction:
            return self
        return super().execute(sql, parameters)


class _ReadonlyConnection(sqlite3.Connection):
    def cursor(self, factory=_ReadonlyCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "BEGIN" and self.in_transaction:
            return self.cursor()
        return super().execute(sql, parameters)

    def close(self):
        with contextlib.suppress(sqlite3.Error):
            if self.in_transaction:
                self.rollback()
        super().close()


def connect_readonly(path: str | Path, timeout=0.5):
    p = Path(path).resolve()
    if not p.is_file():
        raise sqlite3.OperationalError(f"unable to open database file: {p}")
    uri = p.as_uri()
    wal = Path(f"{p}-wal")
    shm = Path(f"{p}-shm")
    if shm.exists():
        conn = None
        try:
            conn = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=timeout, factory=_ReadonlyConnection)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except sqlite3.OperationalError:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
    try:
        wal_nonempty = wal.exists() and wal.stat().st_size > 0
    except OSError:
        wal_nonempty = False
    if not wal_nonempty:
        conn = None
        try:
            conn = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=timeout, factory=_ReadonlyConnection)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except sqlite3.OperationalError:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            if not p.is_file():
                raise
    conn = sqlite3.connect(str(p), timeout=timeout, factory=_ReadonlyConnection)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        conn.execute("PRAGMA schema_version").fetchone()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


def get_db_mtime(db_path: Path) -> float:
    m = db_path.stat().st_mtime
    with contextlib.suppress(OSError):
        st = Path(f"{db_path}-wal").stat()
        if st.st_size > 0:
            m = max(m, st.st_mtime)
    return m


def parse_iso_ts(ts_str: str | None) -> float | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def finite_number(value, *, non_negative=False):
    """Coerce finite numeric input; booleans are not numeric observations."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (non_negative and number < 0):
        return None
    return number


@contextmanager
def atomic_text_writer(path, *, mode=0o600, fsync=True):
    """Stream to a private sibling file; publish only after a complete write."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            yield stream
            if fsync:
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def atomic_write_text(path, text, *, mode=0o600, fsync=True):
    with atomic_text_writer(path, mode=mode, fsync=fsync) as stream:
        stream.write(text)


def find_cli_bin(name, *, extra_dirs=CLI_BIN_DIRS):
    """Find an executable on PATH first, then in the caller's fallback directories."""
    found = shutil.which(name)
    if found:
        return found
    for directory in extra_dirs:
        path = Path(directory) / name
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None
