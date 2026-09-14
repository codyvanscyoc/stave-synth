"""Atomic, schema-checked ten-slot preset bank with legacy file migration.

One manifest owns scenes AND labels, so a swap or setlist replacement has one
atomic commit point. Old preset_N.json files remain untouched for recovery.
"""

import copy
import logging
import threading
from pathlib import Path

from .config import PRESETS_DIR, ensure_dirs
from .state_schema import MAX_JSON_BYTES, ValidationError, index, normalize_state, text
from .state_persistence import read_json
from .state_store import atomic_write_json

logger = logging.getLogger(__name__)


class PresetManager:
    def __init__(self, num_slots=10, *, directory=None):
        if num_slots != 10:
            raise ValueError("the stage preset bank has exactly ten slots")
        self.num_slots = num_slots
        self.directory = Path(directory) if directory is not None else PRESETS_DIR
        if directory is None:
            ensure_dirs()
        self._lock = threading.RLock()
        self._bank = None
        self._legacy_labels = [""] * num_slots

    @property
    def bank_path(self):
        return self.directory / "preset_bank.json"

    def _slot_path(self, slot):
        return self.directory / f"preset_{index(slot, self.num_slots) + 1}.json"

    def _validated_bank(self, presets, labels):
        if not isinstance(presets, list) or len(presets) != self.num_slots:
            raise ValidationError("preset bank must contain exactly ten slots")
        if not isinstance(labels, list) or len(labels) != self.num_slots:
            raise ValidationError("preset bank must contain exactly ten labels")
        return {"version": 1,
                "presets": [None if p is None else normalize_state(p, scene=True) for p in presets],
                "labels": [text(label, 16) for label in labels]}

    def _read_bank(self):
        if self._bank is None:
            try:
                raw = read_json(self.bank_path)
            except FileNotFoundError:
                presets = []
                for slot in range(self.num_slots):
                    try:
                        presets.append(read_json(self._slot_path(slot)))
                    except FileNotFoundError:
                        presets.append(None)
                raw = {"version": 1, "presets": presets, "labels": self._legacy_labels}
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValidationError("unsupported or invalid preset bank")
            self._bank = self._validated_bank(raw.get("presets"), raw.get("labels"))
        return self._bank

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._read_bank())

    def _commit(self, bank):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.bank_path, bank, max_bytes=MAX_JSON_BYTES)
        self._bank = bank

    def replace_bank(self, presets, labels):
        bank = self._validated_bank(copy.deepcopy(presets), copy.deepcopy(labels))
        with self._lock:
            # A corrupt existing bank is not silently overwritten by a
            # setlist operation. Recovery must first resolve the bad source.
            self._read_bank()
            self._commit(bank)
        return True

    def load_checked(self, slot):
        with self._lock:
            return copy.deepcopy(self._read_bank()["presets"][index(slot, self.num_slots)])

    def load(self, slot):
        try:
            return self.load_checked(slot)
        except (ValueError, TypeError, OSError) as exc:
            logger.error("Failed to load preset %s: %s", slot, exc)
            return None

    def save(self, slot, state):
        try:
            slot = index(slot, self.num_slots)
            scene = normalize_state(copy.deepcopy(state), scene=True)
            with self._lock:
                bank = copy.deepcopy(self._read_bank())
                bank["presets"][slot] = scene
                self._commit(bank)
            return True
        except (ValueError, TypeError, OSError) as exc:
            logger.error("Failed to save preset %s: %s", slot, exc)
            return False

    def delete(self, slot):
        try:
            slot = index(slot, self.num_slots)
            with self._lock:
                bank = copy.deepcopy(self._read_bank())
                bank["presets"][slot] = None
                bank["labels"][slot] = ""
                self._commit(bank)
            return True
        except (ValueError, TypeError, OSError) as exc:
            logger.error("Failed to delete preset %s: %s", slot, exc)
            return False

    def label(self, slot, label):
        slot, label = index(slot, self.num_slots), text(label, 16)
        with self._lock:
            bank = copy.deepcopy(self._read_bank())
            bank["labels"][slot] = label
            self._commit(bank)

    def swap(self, source, target):
        source, target = index(source, self.num_slots), index(target, self.num_slots)
        with self._lock:
            bank = copy.deepcopy(self._read_bank())
            for key in ("presets", "labels"):
                bank[key][source], bank[key][target] = bank[key][target], bank[key][source]
            self._commit(bank)
        return True

    def init_defaults(self, labels=None):
        with self._lock:
            if labels is not None:
                self._legacy_labels = [text(label, 16) for label in labels]
            self._read_bank()
