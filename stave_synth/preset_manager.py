"""Preset manager: save/load JSON presets to ~/.config/stave-synth/presets/."""

import json
import copy
import logging
from pathlib import Path

from .config import PRESETS_DIR, ensure_dirs
from .state_store import atomic_write_json

logger = logging.getLogger(__name__)


class PresetManager:
    """Manages preset persistence — save, load, list, and label editing."""

    def __init__(self, num_slots: int = 10):
        self.num_slots = num_slots
        ensure_dirs()
        # Never sweep another writer's files. Each atomic save owns its temp.

    def _slot_path(self, slot: int) -> Path:
        return PRESETS_DIR / f"preset_{slot + 1}.json"

    def save(self, slot: int, state: dict):
        """Save a state dict to a preset slot."""
        if slot < 0 or slot >= self.num_slots:
            logger.warning("Invalid preset slot: %d", slot)
            return False

        path = self._slot_path(slot)
        try:
            atomic_write_json(path, copy.deepcopy(state))
            logger.info("Saved preset to slot %d: %s", slot, path)
            return True
        except (OSError, ValueError, TypeError) as e:
            logger.error("Failed to save preset %d: %s", slot, e)
            return False

    def load(self, slot: int) -> dict | None:
        """Load a preset from a slot. Returns state dict or None."""
        if slot < 0 or slot >= self.num_slots:
            logger.warning("Invalid preset slot: %d", slot)
            return None

        path = self._slot_path(slot)
        if not path.exists():
            logger.info("Preset slot %d is empty", slot)
            return None

        try:
            with open(path) as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("preset must be a JSON object")
            logger.info("Loaded preset from slot %d", slot)
            return state
        except (ValueError, OSError) as e:
            logger.error("Failed to load preset %d: %s", slot, e)
            return None

    def delete(self, slot: int) -> bool:
        """Delete a preset from a slot."""
        if slot < 0 or slot >= self.num_slots:
            logger.warning("Invalid preset slot: %d", slot)
            return False

        path = self._slot_path(slot)
        if not path.exists():
            logger.info("Preset slot %d already empty", slot)
            return True

        try:
            path.unlink()
            logger.info("Deleted preset from slot %d", slot)
            return True
        except OSError as e:
            logger.error("Failed to delete preset %d: %s", slot, e)
            return False

    def init_defaults(self):
        """Initialize preset system."""
        ensure_dirs()
