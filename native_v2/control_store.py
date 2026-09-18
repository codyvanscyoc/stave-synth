"""Versioned native-candidate sound snapshots; no runtime/audio imports.

Reuses the reviewed atomic writer. Saves the player's explicit Master target,
but never held keys, freeze, release or fade actions. The owner restores Master
last after routes and tone are healthy, using the engine's output smoothing.
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
TRANSIENT = frozenset(("freeze", "release_all", "bed_key", "bed_fade", "bed_release", "record_start", "record_stop"))


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


class PresetStore:
    """Five bounded native sound slots, intentionally separate from v1 scenes."""
    def __init__(self, path, control_store):
        self.path = Path(path)
        if self.path.name != "native_presets.json" or self.path.is_symlink():
            raise ValueError("Separate native_presets.json required")
        self.controls = control_store
        self.lock = threading.Lock()
        self.slots = [None] * 5
        self.load()

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return self.slots
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError("Invalid/oversized native preset file")
            data = json.loads(stream.read(65537))
        if not isinstance(data, dict) or set(data) != {"schema", "slots"} or type(data["schema"]) is not int or data["schema"] != 1:
            raise ValueError("Unsupported native preset schema")
        if not isinstance(data["slots"], list) or len(data["slots"]) != 5:
            raise ValueError("Exactly five native preset slots required")
        slots = []
        for item in data["slots"]:
            if item is None:
                slots.append(None); continue
            if not isinstance(item, dict) or set(item) != {"name", "controls"} or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 24 or any(ord(c) < 32 for c in item["name"]):
                raise ValueError("Invalid native preset slot")
            slots.append({"name": item["name"], "controls": self.controls.validated(item["controls"])})
        self.slots = slots
        return self.slots

    def summary(self):
        return [None if slot is None else {"name": slot["name"]} for slot in self.slots]

    def controls_for(self, slot):
        if type(slot) is not int or not 0 <= slot < 5 or self.slots[slot] is None:
            raise ValueError("Choose a saved preset slot")
        return dict(self.slots[slot]["controls"])

    def save_slot(self, slot, name, values):
        if type(slot) is not int or not 0 <= slot < 5 or not isinstance(name, str):
            raise ValueError("Choose preset slot 1–5")
        name = name.strip()
        if not 1 <= len(name) <= 24 or any(ord(c) < 32 for c in name):
            raise ValueError("Preset name must be 1–24 printable characters")
        next_slots = [None if item is None else {"name": item["name"], "controls": dict(item["controls"])} for item in self.slots]
        next_slots[slot] = {"name": name, "controls": self.controls.validated(values)}
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("Symlink preset destination refused")
            atomic.atomic_write_json(self.path, {"schema": 1, "slots": next_slots}, max_bytes=65536)
        self.slots = next_slots
        return self.summary()
