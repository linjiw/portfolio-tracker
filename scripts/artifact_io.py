"""Crash-safe, strict-JSON and private writes for portfolio runtime artifacts."""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix/macOS production path
    fcntl = None


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "output"


def _prepare_parent(target: Path) -> None:
    existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    # Do not unexpectedly change permissions on a caller's arbitrary custom
    # directory.  Repo runtime directories and directories we create are ours.
    if not existed or target.parent == RUNTIME_ROOT or RUNTIME_ROOT in target.parent.parents:
        target.parent.chmod(PRIVATE_DIR_MODE)


def ensure_private_directory(path: str | Path) -> Path:
    """Create an explicitly private directory and enforce mode 0700."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    directory.chmod(PRIVATE_DIR_MODE)
    return directory


def _fsync_parent(target: Path) -> None:
    """Best-effort directory fsync so a completed rename survives a crash."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(target.parent, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_encoded(target: Path, encoded: bytes) -> Path:
    _prepare_parent(target)
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.tmp.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(PRIVATE_FILE_MODE)
        os.replace(temporary, target)
        target.chmod(PRIVATE_FILE_MODE)
        _fsync_parent(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def atomic_write_json(path: str | Path, payload: Any, *, indent: int | None = 2) -> Path:
    """Validate strict JSON, fsync, and atomically replace ``path``.

    Serialization happens before the old artifact is touched.  This prevents a
    producer crash, NaN, or partial disk write from destroying the last-known-
    good research artifact that the dashboard can still safely display.
    """
    target = Path(path)
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=indent, allow_nan=False,
    ) + "\n"
    return _atomic_write_encoded(target, encoded.encode("utf-8"))


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Atomically write a private UTF-8 text/Markdown/HTML artifact."""
    target = Path(path)
    return _atomic_write_encoded(target, text.encode("utf-8"))


def atomic_write_bytes(path: str | Path, payload: bytes | bytearray | memoryview) -> Path:
    """Atomically write private binary content such as a LaunchAgent plist."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    return _atomic_write_encoded(Path(path), bytes(payload))


def atomic_write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
    *,
    extrasaction: str = "raise",
) -> Path:
    """Serialize a complete CSV before atomically replacing the old artifact."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), extrasaction=extrasaction)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return atomic_write_text(path, buffer.getvalue())


def append_csv_row_private(
    path: str | Path,
    row: Mapping[str, Any],
    fieldnames: Sequence[str],
) -> Path:
    """Append one CSV row under an exclusive lock, retaining append semantics.

    This helper never rewrites existing content. Callers that need a schema
    migration should perform that migration with :func:`atomic_write_csv` and
    use this helper for subsequent rows.
    """
    names = list(fieldnames)
    row_buffer = io.StringIO(newline="")
    csv.DictWriter(row_buffer, fieldnames=names).writerow(row)
    header_buffer = io.StringIO(newline="")
    csv.DictWriter(header_buffer, fieldnames=names).writeheader()

    target = Path(path)
    _prepare_parent(target)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(target, flags, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "a", encoding="utf-8", newline="") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                was_empty = os.fstat(handle.fileno()).st_size == 0
                if was_empty:
                    handle.write(header_buffer.getvalue())
                handle.write(row_buffer.getvalue())
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        # fd is normally closed by fdopen; close only if fdopen itself failed.
        try:
            os.close(fd)
        except OSError:
            pass
    target.chmod(PRIVATE_FILE_MODE)
    if was_empty:
        _fsync_parent(target)
    return target
