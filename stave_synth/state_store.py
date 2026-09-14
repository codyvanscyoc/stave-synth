"""Durable, atomic JSON-file replacement helpers.

Callers must pass a stable snapshot.  This module guarantees that one complete
JSON serialization is installed atomically; it cannot make a concurrently
mutated Python object into a logically consistent snapshot.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Union


PathLike = Union[str, os.PathLike[str]]


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync for rename durability.

    Some supported filesystems/platforms do not allow opening or syncing a
    directory.  The data file itself is always fsynced; failure of this extra
    durability step is intentionally non-fatal after the atomic replacement.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def atomic_write_json(path: PathLike, value: Any, *, max_bytes: int | None = None) -> None:
    """Validate, serialize, and atomically replace *path* with JSON.

    Serialization happens before a temporary file is created.  NaN and
    infinity are rejected.  The temporary file is unique and in the target
    directory, so concurrent writers never share or unlink each other's work.
    A new target is private (0600); an existing target's permission bits are
    preserved.  Failures before ``os.replace`` leave the old target untouched
    and remove only this invocation's temporary file.
    """

    target = Path(path)
    payload = json.dumps(value, indent=2, allow_nan=False).encode("utf-8")
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError(f"JSON exceeds the {max_bytes}-byte storage limit")

    try:
        target_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        target_mode = 0o600

    fd = -1
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.fchmod(fd, target_mode)

        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while saving JSON")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1

        os.replace(temp_name, target)
        temp_name = None
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
