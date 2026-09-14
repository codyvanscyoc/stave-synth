"""Bounded, schema-checked state storage with last-good recovery.

Rejected state is hard-linked before replacement, retaining the original bytes
without making a second, potentially huge copy on a small Pi filesystem.
"""

import copy
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from .state_schema import MAX_JSON_BYTES, normalize_state
from .state_store import atomic_write_json

logger = logging.getLogger(__name__)
_lock = threading.RLock()
last_load_warning = None


def read_json(path):
    with Path(path).open("rb") as source:
        payload = source.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds the 4 MiB storage limit")
    return json.loads(payload)


def previous_path(path):
    return Path(path).with_name(Path(path).stem + ".previous.json")


def load(path):
    global last_load_warning
    with _lock:
        last_load_warning = None
        for candidate in (Path(path), previous_path(path)):
            try:
                result = normalize_state(read_json(candidate))
                if candidate != Path(path):
                    logger.warning("Recovered last-good state from %s", candidate)
                return result
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, OSError, RecursionError) as exc:
                last_load_warning = f"Rejected saved state {candidate.name}: {exc}"
                logger.error("%s (original retained)", last_load_warning)
        return normalize_state({})


def save(path, state):
    # Validate the entire detached snapshot before touching any persisted data.
    snapshot = normalize_state(copy.deepcopy(state))
    # Match the on-disk formatting/encoding, so a successful save is readable
    # at the next boot even when compact incoming JSON expands substantially.
    if len(json.dumps(snapshot, indent=2, allow_nan=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds the 4 MiB storage limit")
    path = Path(path)
    with _lock:
        try:
            previous = normalize_state(read_json(path))
        except FileNotFoundError:
            previous = None
        except (ValueError, TypeError, OSError, RecursionError):
            # If preservation fails, fail the save as well. Never destroy the
            # only recoverable original merely to get autosave working again.
            rejected = path.with_name(f"{path.stem}.rejected-{uuid.uuid4().hex}.json")
            os.link(path, rejected)
            logger.error("Retained rejected state at %s", rejected)
            previous = None
        if previous is not None:
            atomic_write_json(previous_path(path), previous, max_bytes=MAX_JSON_BYTES)
        atomic_write_json(path, snapshot, max_bytes=MAX_JSON_BYTES)
