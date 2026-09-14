"""Device-free schema/storage/scene tests. All writes are private temp files."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stave_synth.config import DEFAULT_STATE, LOW_RAM_MODE
from stave_synth import state_persistence as storage
from stave_synth.state_schema import (
    SCENE_SECTIONS, ValidationError, normalize_state, validate_message,
    validate_setting, validate_assignment,
)
from stave_synth.preset_manager import PresetManager
from stave_synth.state_store import atomic_write_json


def legacy_nested_state(cycles=5):
    """Model old full-state preset saves followed by setlist-bank snapshots."""
    state = copy.deepcopy(DEFAULT_STATE)
    state["master"]["audio_output_pref"] = "stage-dac"
    state["midi_cc_map"] = {"7": {"kind": "fader", "id": 4, "alt": 0}}
    state["ui"]["preset_labels"][0] = "Current sound"
    state["setlists"][3] = {"name": "Other service", "labels": ["Soft"] + [""] * 9,
                            "presets": [{"master": {"volume": .23}}] + [None] * 9}
    for cycle in range(cycles):
        state["master"]["volume"] = .2 + cycle * .1
        state["synth_pad"]["filter_cutoff_hz"] = 1000 + cycle * 500
        state["piano"]["volume"] = .3 + cycle * .05
        preset = copy.deepcopy(state)
        state["setlists"][0] = {"name": f"Service {cycle}", "presets": [preset] + [None] * 9,
                                "labels": [f"Sound {cycle}"] + [""] * 9}
    # The current sound can differ from the most recently saved scene.
    state["master"]["volume"] = .81
    return state


class SchemaTests(unittest.TestCase):
    def test_every_default_is_supported_with_explicit_profile_overrides(self):
        normalized = normalize_state(DEFAULT_STATE)
        for section in SCENE_SECTIONS[:4]:
            for param, value in DEFAULT_STATE[section].items():
                with self.subTest(section=section, param=param):
                    # Pi4's documented native fast path pins unison to three;
                    # the generic (non-profile) dictionary default stays one.
                    expected = 3 if LOW_RAM_MODE and (section, param) == ("synth_pad", "unison_voices") else value
                    self.assertEqual(expected, normalized[section][param])
                    self.assertEqual(expected, validate_setting(section, param, value))

    def test_rejects_nonfinite_before_numeric_clamp(self):
        for value in (float("nan"), float("inf"), -float("inf"), "NaN", "Infinity", "-Infinity"):
            for section, param in (("master", "volume"), ("synth_pad", "filter_cutoff_hz")):
                with self.subTest(value=value, param=param), self.assertRaises(ValidationError):
                    validate_message({"type": "setting", "section": section, "param": param, "value": value})

    def test_invalid_nested_shapes_and_enums_are_rejected(self):
        bad = [None, [], 4, {"master": []}, {"piano": {"soundfont": []}},
               {"organ": {"drawbars": [4] * 10}}, {"synth_pad": {"filter_slope": 18}},
               {"macros": [None]}, {"setlists": [None]}, {"midi_cc_map": {"128": {"id": 0}}},
               {"ui": {"preset_labels": [""] * 11}}, {"master": {"eq_bands": []}}]
        for state in bad:
            with self.subTest(state=state), self.assertRaises((ValidationError, TypeError)):
                normalize_state(state)

    def test_boolean_false_string_is_not_true(self):
        with self.assertRaises(ValidationError):
            validate_setting("master", "pitch_bend_enabled", "false")
        self.assertIs(validate_setting("master", "pitch_bend_enabled", 0), False)

    def test_unknown_public_controls_do_not_silently_ack(self):
        for msg in ({"type": "unknown"}, {"type": "setting", "section": "master", "param": "anything", "value": 1},
                    {"type": "fader", "id": 5}, {"type": "preset_load", "slot": -1},
                    {"type": "macro_value", "idx": 8}, {"type": "clear_pad_slot", "note": 59},
                    {"type": "delete_recording", "filename": "../take.wav"}):
            with self.subTest(msg=msg), self.assertRaises(ValidationError):
                validate_message(msg)

    def test_numeric_strings_and_clamps_return_canonical_units(self):
        self.assertEqual(validate_setting("master", "piano_octave", "100"), 3)
        self.assertEqual(validate_message({"type": "fader", "id": "4", "value": 5})["value"], 1)
        with self.assertRaises(ValidationError):
            validate_setting("master", "piano_octave", 1.5)

    def test_legacy_names_envelopes_and_short_arrays_migrate(self):
        state = normalize_state({"synth_pad": {"adsr": {"attack_ms": 987}, "lfo_enabled": False},
                                 "piano": {"voicing": "bright_studio", "soundfont": "FluidR3_GM"},
                                 "ui": {"preset_labels": ["old"]}, "macros": []})
        for key in ("adsr_osc1", "adsr_osc2"):
            self.assertEqual(state["synth_pad"][key]["attack_ms"], 987)
        self.assertNotIn("lfo_enabled", state["synth_pad"])
        self.assertEqual(state["piano"]["voicing"], "bright")
        self.assertEqual(state["piano"]["soundfont"], "Fluid")
        self.assertEqual(len(state["ui"]["preset_labels"]), 10)
        self.assertEqual(len(state["macros"]), 8)

    def test_scenes_strip_global_libraries_before_recursing(self):
        old = copy.deepcopy(DEFAULT_STATE)
        old["setlists"] = old  # Malformed recursive old library is not part of a scene.
        old["master"]["audio_output_pref"] = "stage-dac"
        old["synth_pad"]["drone_enabled"] = True
        old["synth_pad"]["drone_key"] = 60
        scene = normalize_state(old, scene=True)
        self.assertEqual(set(scene), set(SCENE_SECTIONS))
        self.assertNotIn("audio_output_pref", scene["master"])
        self.assertNotIn("low_latency_mode", scene["master"])
        self.assertFalse(scene["synth_pad"]["drone_enabled"])
        self.assertEqual(scene, normalize_state(scene, scene=True))

    def test_full_legacy_state_preserves_sound_and_top_level_scenes(self):
        for cycles in (3, 5):
            with self.subTest(cycles=cycles):
                old = legacy_nested_state(cycles)
                original = json.dumps(old, sort_keys=True)
                expected_sound = normalize_state(old, scene=True)
                expected_scenes = {slot: normalize_state(old["setlists"][slot]["presets"][0], scene=True)
                                   for slot in (0, 3)}
                migrated = normalize_state(old)
                self.assertEqual(normalize_state(migrated, scene=True), expected_sound)
                for key in ("midi_cc_map", "ui"):
                    self.assertEqual(migrated[key], old[key])
                self.assertEqual(migrated["master"]["audio_output_pref"], "stage-dac")
                for slot, expected in expected_scenes.items():
                    self.assertEqual(migrated["setlists"][slot]["presets"][0], expected)
                    self.assertEqual(migrated["setlists"][slot]["name"], old["setlists"][slot]["name"])
                    self.assertEqual(migrated["setlists"][slot]["labels"], old["setlists"][slot]["labels"])
                self.assertEqual(json.dumps(old, sort_keys=True), original)
                self.assertEqual(normalize_state(migrated), migrated)

    def test_legacy_projection_still_rejects_invalid_retained_data_and_bounds(self):
        for bad_library in ([{}] * 11, [{"presets": [{}] * 11}], [{"presets": [False]}]):
            with self.subTest(library=bad_library), self.assertRaises(ValidationError):
                normalize_state({"setlists": bad_library})
        for bad_control in (float("nan"), float("inf"), []):
            old = legacy_nested_state()
            old["setlists"][0]["presets"][0]["master"]["volume"] = bad_control
            with self.subTest(control=bad_control), self.assertRaises(ValidationError):
                normalize_state(old)
        deep = 0
        for _ in range(13):
            deep = {"nested": deep}
        with self.assertRaises(ValidationError):
            normalize_state({"setlists": [{"presets": [{"master": {"unexpected": deep}}]}]})

    def test_inverted_fader_ranges_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_state({"piano": {"tone_range_min": 5000, "tone_range_max": 1000}})

    def test_macro_assignment_limits_and_synthetic_eq_indices(self):
        with self.assertRaises(ValidationError):
            validate_assignment({"section": "piano", "param": "eq_band4_gain"})
        with self.assertRaises(ValidationError):
            normalize_state({"macros": [{"assignments": [{"kind": "fader", "fader_id": 1}] * 33}]})
        self.assertEqual(validate_assignment({"section": "piano", "param": "eq_band3_gain", "min": -120, "max": 120})["max"], 120)

    def test_deep_or_huge_public_messages_reject_boundedly(self):
        for value in ([0] * 129, 10 ** 400, {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                validate_message({"type": "get_state", "extra": value})


class StateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stave-schema-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_last_good_recovery_and_rejected_original_preserved(self):
        storage.save(self.path, {"master": {"volume": .3}})
        storage.save(self.path, {"master": {"volume": .7}})
        atomic_write_json(self.path, {"piano": []})
        original = self.path.read_bytes()
        recovered = storage.load(self.path)
        self.assertEqual(recovered["master"]["volume"], .3)
        self.assertIsNotNone(storage.last_load_warning)
        storage.save(self.path, recovered)
        rejected = list(self.path.parent.glob("state.rejected-*.json"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_bytes(), original)

    def test_deep_legacy_state_loads_without_default_fallback_or_original_mutation(self):
        old = legacy_nested_state()
        atomic_write_json(self.path, old)
        original = self.path.read_bytes()
        recovered = storage.load(self.path)
        self.assertIsNone(storage.last_load_warning)
        self.assertEqual(recovered["master"]["volume"], .81)
        self.assertEqual(recovered["setlists"][0]["presets"][0]["master"]["volume"],
                         old["setlists"][0]["presets"][0]["master"]["volume"])
        self.assertEqual(recovered["setlists"][3]["presets"][0]["master"]["volume"], .23)
        self.assertEqual(self.path.read_bytes(), original)
        storage.save(self.path, recovered)
        self.assertEqual(storage.load(self.path), recovered)
        self.assertEqual(storage.load(storage.previous_path(self.path)), recovered)

    def test_invalid_candidate_does_not_touch_current_or_backup(self):
        storage.save(self.path, {})
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            storage.save(self.path, {"master": {"volume": float("nan")}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(storage.previous_path(self.path).exists())

    def test_failed_preservation_refuses_overwrite(self):
        atomic_write_json(self.path, {"piano": []})
        before = self.path.read_bytes()
        with mock.patch.object(storage.os, "link", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                storage.save(self.path, {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_oversize_serialized_candidate_leaves_current_and_history_untouched(self):
        storage.save(self.path, {})
        before = self.path.read_bytes()
        with mock.patch.object(storage, "MAX_JSON_BYTES", 16):
            with self.assertRaises(ValueError):
                storage.save(self.path, {})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(storage.previous_path(self.path).exists())

    def test_atomic_writer_bounds_encoded_not_character_length(self):
        with self.assertRaises(ValueError):
            atomic_write_json(self.path, {"name": "é" * 10}, max_bytes=32)
        self.assertFalse(self.path.exists())

    def test_missing_and_corrupt_files_return_valid_defaults(self):
        self.assertEqual(storage.load(self.path), normalize_state({}))
        atomic_write_json(self.path, ["bad"])
        self.assertEqual(storage.load(self.path), normalize_state({}))
        self.assertEqual(json.loads(self.path.read_bytes()), ["bad"])

    def test_replace_failure_keeps_previous_current(self):
        storage.save(self.path, {"master": {"volume": .3}})
        before = self.path.read_bytes()
        with mock.patch.object(storage, "atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                storage.save(self.path, {"master": {"volume": .9}})
        self.assertEqual(self.path.read_bytes(), before)


class PresetTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stave-bank-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.manager = PresetManager(directory=self.directory)

    def test_legacy_migration_retains_files_and_global_labels(self):
        legacy = self.directory / "preset_1.json"
        atomic_write_json(legacy, {"master": {"volume": .3}})
        before = legacy.read_bytes()
        self.manager.init_defaults(["one"] + [""] * 9)
        self.assertEqual(self.manager.load_checked(0)["master"]["volume"], .3)
        self.assertTrue(self.manager.save(1, {"master": {"volume": .8}}))
        self.assertEqual(legacy.read_bytes(), before)
        fresh = PresetManager(directory=self.directory).snapshot()
        self.assertEqual(fresh["labels"][0], "one")
        self.assertEqual(fresh["presets"][1]["master"]["volume"], .8)

    def test_swap_commits_scenes_and_labels_together(self):
        self.manager.save(0, {"master": {"volume": .3}})
        self.manager.label(0, "piano")
        self.manager.swap(0, 9)
        bank = PresetManager(directory=self.directory).snapshot()
        self.assertIsNone(bank["presets"][0])
        self.assertEqual(bank["labels"][0], "")
        self.assertEqual(bank["labels"][9], "piano")
        self.assertEqual(bank["presets"][9]["master"]["volume"], .3)

    def test_failed_bank_commit_leaves_disk_and_memory_unchanged(self):
        self.manager.save(0, {})
        before = self.manager.snapshot()
        disk = self.manager.bank_path.read_bytes()
        with mock.patch("stave_synth.preset_manager.atomic_write_json", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                self.manager.replace_bank([None] * 10, [""] * 10)
            with self.assertRaises(OSError):
                self.manager.swap(0, 9)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.bank_path.read_bytes(), disk)

    def test_corrupt_bank_is_not_confused_with_empty_or_overwritten(self):
        atomic_write_json(self.manager.bank_path, {"version": 999})
        with self.assertRaises(ValidationError):
            self.manager.load_checked(0)
        self.assertFalse(self.manager.save(0, {}))
        self.assertEqual(json.loads(self.manager.bank_path.read_bytes()), {"version": 999})

    def test_repeated_setlist_snapshots_do_not_embed_libraries(self):
        state = normalize_state({})
        for _ in range(5):
            self.assertTrue(self.manager.save(0, state))
            state["setlists"][0] = {"name": "service", **self.manager.snapshot()}
        scene = self.manager.load_checked(0)
        self.assertEqual(set(scene), set(SCENE_SECTIONS))
        self.assertNotIn("setlists", scene)
        self.assertLess(self.manager.bank_path.stat().st_size, 30000)

    def test_delete_keeps_authoritative_empty_slot_across_reload(self):
        atomic_write_json(self.directory / "preset_1.json", {})
        self.manager.init_defaults()
        self.assertTrue(self.manager.delete(0))
        self.assertIsNone(PresetManager(directory=self.directory).load_checked(0))
        self.assertTrue((self.directory / "preset_1.json").exists())


if __name__ == "__main__":
    unittest.main()
