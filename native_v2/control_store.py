"""Versioned native-candidate knob snapshots; no runtime/audio imports.

Reuses the reviewed atomic writer. Never saves/replays output gain, held keys,
freeze, release or fade actions. Not an importer for legacy presets/scenes.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import threading

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stave_atomic_state", ROOT / "stave_synth/state_store.py")
atomic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atomic)
TRANSIENT = frozenset(("master", "freeze", "release_all", "bed_key", "bed_fade", "bed_release"))


class ControlStore:
    def __init__(self, path, validate):
        self.path = Path(path)
        if self.path.name != "native_controls.json" or self.path.is_symlink():
            raise ValueError("Separate native_controls.json required; no legacy state overwrite")
        self.validate = validate
        self.lock = threading.Lock()

    def validated(self, values):
        if not isinstance(values, dict) or len(values) > 64:
            raise ValueError("Bounded control object required")
        result = {}
        for key, value in values.items():
            if key in TRANSIENT:
                raise ValueError("Transient/output actions cannot be restored")
            k, v = self.validate({"key": key, "value": value})
            result[k] = v
        # Expand legacy shared AR only where independent values were not saved.
        # Do not replay aliases after independent values; retain schema1 reading.
        for name in ("attack", "release"):
            if name in result:
                value = result.pop(name)
                for oscillator in ("1", "2"):
                    result.setdefault(name + oscillator, value)
        return result

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("Invalid/oversized native control file")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Oversized native control file")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"schema", "controls"} or type(data["schema"]) is not int or data["schema"] != 1:
            raise ValueError("Unsupported native control schema")
        return self.validated(data["controls"])

    def save(self, values):
        validated = self.validated(values)
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("Symlink state destination refused")
            atomic.atomic_write_json(self.path, {"schema": 1, "controls": validated}, max_bytes=16384)
        return validated
