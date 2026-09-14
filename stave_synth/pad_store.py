"""Recoverable control-plane pad-file transactions; no audio/device imports.

The synth's opaque reservation pins an inactive target between preparation
and install. A source recording must additionally be held with claim_take().
File operations happen off the render lock. Rollback failures retain the old
file in a named recovery directory instead of destroying the last good copy.
This is exception recovery, not an on-disk journal for arbitrary power loss.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

from .state_store import _fsync_directory


logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 64 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


def _signature(stat_result):
    return (stat_result.st_dev, stat_result.st_ino,
            stat_result.st_size, stat_result.st_mtime_ns)


def _remove_owned(path, warnings):
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        message = f"Temporary pad file retained at {path}: {exc}"
        warnings.append(message)
        logger.warning(message)


def _remove_directory(path, warnings):
    if path is None:
        return
    try:
        path.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        message = f"Pad recovery directory retained at {path}: {exc}"
        warnings.append(message)
        logger.warning(message)


def _reject_busy_target(synth, note):
    status = synth.pad_sample_status()
    if status.get("preparing_note") is not None:
        raise RuntimeError("Another pad update is already being prepared")
    if status.get("slots", {}).get(note, {}).get("active"):
        raise RuntimeError("Stop this pad before replacing it")


def _copy_bounded(source_stream, output_stream, source_stat):
    """Bound reads/writes even if an external process grows the claimed file."""
    copied = 0
    while True:
        data = source_stream.read(min(COPY_CHUNK_BYTES, MAX_SOURCE_BYTES - copied + 1))
        if not data:
            break
        if copied + len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Pad source exceeds the 64 MiB file limit")
        written = output_stream.write(data)
        if written != len(data):
            raise OSError("Short write while staging pad WAV")
        copied += written
    if copied != source_stat.st_size or _signature(os.fstat(source_stream.fileno())) != _signature(source_stat):
        raise ValueError("Recording changed during the pad copy")
    output_stream.flush()
    os.fsync(output_stream.fileno())


def _same_inode(path, original_stat):
    try:
        current = path.stat()
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (original_stat.st_dev, original_stat.st_ino)


def save_pad_slot(synth, note: int, source: Path, target: Path) -> list[str]:
    """Copy/prepare/replace/install one slot, returning any cleanup warnings."""
    source, target = Path(source), Path(target)
    _reject_busy_target(synth, note)
    source_stat = source.stat()
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > MAX_SOURCE_BYTES:
        raise ValueError("Pad source must be a regular WAV no larger than 64 MiB")
    staging = backup = recovery_dir = prepared = None
    staging_stat = None
    commit_attempted = False
    preserve_backup = False
    warnings = []
    failure = None
    fd = -1
    try:
        with source.open("rb") as original:
            opened_stat = os.fstat(original.fileno())
            if _signature(opened_stat) != _signature(source_stat):
                raise ValueError("Recording changed before the pad copy")
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".stage", dir=target.parent)
            staging = Path(name)
            output = os.fdopen(fd, "wb")
            fd = -1
            with output:
                _copy_bounded(original, output, opened_stat)
        # Only the changed slot is decoded, before any existing file is moved.
        prepared = synth.prepare_pad_sample(note, staging)
        staging_stat = staging.stat()
        if target.exists():
            recovery_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".recovery", dir=target.parent))
            backup = recovery_dir / target.name
            os.link(target, backup)
            _fsync_directory(recovery_dir)
        commit_attempted = True
        os.replace(staging, target)
        _fsync_directory(target.parent)
        synth.install_pad_sample(note, prepared)
    except Exception as exc:
        failure = exc
        if commit_attempted:
            try:
                if backup is not None:
                    os.replace(backup, target)
                elif staging_stat is not None and _same_inode(target, staging_stat):
                    # A new slot had no old file. Remove only the inode this
                    # transaction staged, never an unrelated concurrent file.
                    target.unlink()
                _fsync_directory(target.parent)
            except Exception as rollback_error:
                preserve_backup = backup is not None and backup.exists()
                recovery = f"; original retained at {backup}" if preserve_backup else ""
                failure = RuntimeError(f"{exc}; pad rollback failed: {rollback_error}{recovery}")
    finally:
        if fd >= 0:
            os.close(fd)
        if prepared is not None:
            synth.discard_prepared_pad(prepared)
        _remove_owned(staging, warnings)
        if not preserve_backup:
            _remove_owned(backup, warnings)
            _remove_directory(recovery_dir, warnings)
        _fsync_directory(target.parent)
    if failure is not None:
        if warnings:
            raise RuntimeError(f"{failure}; {'; '.join(warnings)}") from failure
        raise failure
    return warnings


def clear_pad_slot(synth, note: int, target: Path) -> list[str]:
    """Reserve then recoverably clear one inactive file/player pair."""
    target = Path(target)
    prepared = synth.prepare_pad_sample(note, None)
    backup = recovery_dir = None
    preserve_backup = False
    warnings = []
    failure = None
    try:
        if target.exists():
            recovery_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".recovery", dir=target.parent))
            backup = recovery_dir / target.name
            os.replace(target, backup)
            _fsync_directory(recovery_dir)
            _fsync_directory(target.parent)
        synth.install_pad_sample(note, prepared)
    except Exception as exc:
        failure = exc
        if backup is not None and backup.exists():
            try:
                os.replace(backup, target)
                _fsync_directory(target.parent)
            except Exception as rollback_error:
                preserve_backup = True
                failure = RuntimeError(f"{exc}; pad rollback failed: {rollback_error}; original retained at {backup}")
    finally:
        synth.discard_prepared_pad(prepared)
        if not preserve_backup:
            _remove_owned(backup, warnings)
            _remove_directory(recovery_dir, warnings)
        _fsync_directory(target.parent)
    if failure is not None:
        if warnings:
            raise RuntimeError(f"{failure}; {'; '.join(warnings)}") from failure
        raise failure
    return warnings
