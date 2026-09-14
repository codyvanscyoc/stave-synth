"""Native-wrapper ownership/control tests without loading any native library.

Executes the real AST-extracted classes using NumPy and fake Faust calls.
No audio device, service, application config, CFFI or SciPy is imported.
These tests prove state handling, not sound quality or target timing.
"""

import ast
import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def extract(relative, names, namespace, strip_init=()):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names:
            if isinstance(node, ast.ClassDef):
                node.body = [item for item in node.body
                             if not isinstance(item, ast.FunctionDef)
                             or (item.name != "__del__"
                                 and not (node.name in strip_init and item.name == "__init__"))]
            selected.append(node)
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in names for target in node.targets):
                selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative, "exec"), namespace)


class FakeFFI:
    NULL = None

    @staticmethod
    def new(signature):
        return [None] * int(signature.rsplit("[", 1)[1].rstrip("]"))

    @staticmethod
    def cast(signature, address):
        return address


class OrganLibrary:
    def __init__(self):
        self.owner = None
        self.clear_count = 0
        self.computing = False
        self.on_compute = None

    def newStaveOrgan(self):
        return object()

    def initStaveOrgan(self, *args):
        pass

    def computeStaveOrgan(self, *args):
        self.computing = True
        try:
            if self.on_compute:
                self.on_compute()
            self.owner._out_l.fill(0)
            self.owner._out_r.fill(0)
        finally:
            self.computing = False

    def instanceClearStaveOrgan(self, *args):
        if self.computing:
            raise AssertionError("clear overlapped compute")
        self.clear_count += 1


def make_organ():
    library = OrganLibrary()

    def install_ui(dsp, zones):
        for prefix in ("freq_v", "gate_v", "phase_v", "pan_v"):
            for slot in range(16):
                zones[f"{prefix}{slot}"] = [0.0]
        for index in range(9):
            zones[f"amp_d{index}"] = [0.0]
        for key in ("drive", "leslie_depth", "leslie_target_hz", "highcut_hz",
                    "lowcut_hz", "volume", "tone_tilt"):
            zones[key] = [0.0]
        return []

    namespace = {
        "np": np, "threading": threading, "SAMPLE_RATE": 48000,
        "_lib": library, "_ffi": FakeFFI(),
        "_install_ui_callbacks": install_ui,
        "_generate_click_sample": lambda rate: np.zeros(int(0.003 * rate)),
    }
    extract("stave_synth/faust_organ.py", {
        "FaustOrganEngine", "_Voice", "N_SLOTS", "ORGAN_PRESETS",
        "CROSSTALK_LEVEL", "LESLIE_FAST_HZ", "LESLIE_SLOW_HZ",
    }, namespace)
    organ = namespace["FaustOrganEngine"]()
    organ.enabled = True
    organ.click_enabled = False
    library.owner = organ
    return organ, library


class OrganOwnershipTests(unittest.TestCase):
    def assert_slot_ownership(self, organ):
        active = {voice.slot for voice in organ.voices.values()}
        self.assertEqual(len(active), len(organ.voices))
        self.assertEqual(active, set(organ._slot_to_voice))
        self.assertEqual(len(set(organ._free_slots)), len(organ._free_slots))
        self.assertFalse(active & set(organ._free_slots))
        self.assertEqual(active | set(organ._free_slots), set(range(16)))
        for voice in organ.voices.values():
            self.assertIs(organ._slot_to_voice[voice.slot], voice)

    def test_releasing_retrigger_reuses_slot_without_orphans(self):
        organ, library = make_organ()
        organ.note_on(60, 1.0)
        organ.render_block(512)
        slot = organ.voices[60].slot
        for _ in range(64):
            previous = organ.voices[60]
            organ.note_off(60)
            organ.note_on(60, 0.8)
            self.assertIsNot(organ.voices[60], previous)
            self.assertEqual(organ.voices[60].slot, slot)
            self.assertFalse(organ.voices[60].releasing)
            self.assert_slot_ownership(organ)
            organ.render_block(512)
        organ.all_notes_off()
        for _ in range(4):
            organ.render_block(512)
        self.assertEqual(organ.voices, {})
        self.assertTrue(all(zone[0] == 0 for zone in organ._gate_zones))
        self.assertEqual(library.clear_count, 0)
        self.assert_slot_ownership(organ)

    def test_held_retrigger_preserves_voice_identity(self):
        organ, _ = make_organ()
        organ.note_on(60, 1.0)
        previous = organ.voices[60]
        organ.note_on(60, 0.4)
        self.assertIs(organ.voices[60], previous)
        self.assertEqual(previous.velocity, 0.4)
        self.assert_slot_ownership(organ)

    def test_full_pool_steal_remains_bounded(self):
        organ, _ = make_organ()
        for note in range(48, 80):
            organ.note_on(note, 1.0)
            self.assert_slot_ownership(organ)
        self.assertEqual(len(organ.voices), 16)
        self.assertEqual(organ._free_slots, [])

    def test_stale_reaper_keeps_replacement_identity(self):
        organ, library = make_organ()
        organ.note_on(60, 1.0)
        old = organ.voices[60]
        organ.note_off(60)
        old.release_remaining = 0
        # Re-entrant mock callback deliberately replaces the snapshot entry.
        # Real cross-thread allocation is serialized by the render lock.
        library.on_compute = lambda: organ.note_on(60, 0.5)
        organ.render_block(512)
        library.on_compute = None
        self.assertIsNot(organ.voices[60], old)
        self.assertFalse(organ.voices[60].releasing)
        self.assert_slot_ownership(organ)
        organ.render_block(512)
        self.assertGreater(organ._gate_zones[organ.voices[60].slot][0], 0)

    def test_normal_all_notes_off_still_uses_release_envelope(self):
        organ, library = make_organ()
        organ.release_ms = 100
        organ.note_on(60, 1.0)
        organ.render_block(512)
        slot = organ.voices[60].slot
        organ.all_notes_off()
        self.assertEqual(organ._gate_zones[slot][0], 1.0)
        self.assertTrue(organ.voices[60].releasing)
        organ.render_block(512)
        self.assertGreater(organ._gate_zones[slot][0], 0)
        self.assertLess(organ._gate_zones[slot][0], 1)
        self.assertEqual(library.clear_count, 0)

    def test_hard_panic_only_clears_on_render_and_covers_orphan_slots(self):
        organ, library = make_organ()
        organ.note_on(60, 1.0)
        organ.render_block(512)
        organ._gate_zones[15][0] = 1.0  # emulate orphan from an older runtime
        organ.hard_panic()
        self.assertEqual(library.clear_count, 0)
        self.assertEqual(len(organ.voices), 1)
        organ.render_block(512)
        self.assertEqual(library.clear_count, 1)
        self.assertEqual(organ.voices, {})
        self.assertTrue(all(zone[0] == 0 for zone in organ._gate_zones))
        self.assertTrue(all(zone[0] == 0 for zone in organ._freq_zones))
        self.assert_slot_ownership(organ)
        organ.note_on(62, 0.8)
        organ.render_block(512)
        self.assertGreater(organ._gate_zones[organ.voices[62].slot][0], 0)

    def test_panic_drains_before_disabled_or_zero_block_return(self):
        for enabled, count in ((False, 512), (True, 0)):
            with self.subTest(enabled=enabled, count=count):
                organ, library = make_organ()
                organ.note_on(60, 1.0)
                organ.hard_panic()
                organ.enabled = enabled
                output = organ.render_block(count)
                self.assertEqual(output.shape, (2, count))
                self.assertEqual(library.clear_count, 1)
                self.assertEqual(organ.voices, {})
                self.assert_slot_ownership(organ)

    def test_panic_requested_during_compute_waits_for_next_render(self):
        organ, library = make_organ()
        organ.note_on(60, 1.0)
        library.on_compute = organ.hard_panic
        organ.render_block(512)
        self.assertEqual(library.clear_count, 0)
        self.assertTrue(organ._panic_pending)
        library.on_compute = None
        organ.render_block(512)
        self.assertEqual(library.clear_count, 1)
        self.assertFalse(organ._panic_pending)

    def test_render_serializes_other_thread_note_mutation(self):
        organ, library = make_organ()
        entered = threading.Event()
        release = threading.Event()
        note_started = threading.Event()
        note_done = threading.Event()
        errors = []

        def compute_hook():
            entered.set()
            if not release.wait(2):
                errors.append("render release timed out")

        def add_note():
            note_started.set()
            organ.note_on(62, 1.0)
            note_done.set()

        library.on_compute = compute_hook
        renderer = threading.Thread(target=organ.render_block, args=(512,))
        controller = threading.Thread(target=add_note)
        try:
            renderer.start()
            self.assertTrue(entered.wait(1))
            controller.start()
            self.assertTrue(note_started.wait(1))
            self.assertFalse(note_done.wait(0.05))
            self.assertNotIn(62, organ.voices)
        finally:
            release.set()
            renderer.join(2)
            if controller.ident is not None:
                controller.join(2)
        self.assertFalse(renderer.is_alive())
        self.assertFalse(controller.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(note_done.is_set())
        self.assert_slot_ownership(organ)


class Backend:
    def __init__(self):
        self.zones = {key: value for key, value in
                      (("feedback", 0.8), ("damp", 0.5), ("freeze_input", 1.0))}
        self.clears = []

    def set_zone(self, key, value):
        self.zones[key] = value

    def clear(self):
        # Match instanceClear: records control state but does not reset it.
        self.clears.append(dict(self.zones))

    def process(self, samples):
        return np.zeros_like(samples)


def make_reverb():
    clears = []
    namespace = {
        "np": np, "threading": threading,
        "logger": logging.getLogger(__name__), "_ffi": FakeFFI(),
        "_lib": SimpleNamespace(instanceClearStaveReverb=lambda dsp: clears.append(True)),
    }
    extract("stave_synth/faust_reverb.py", {
        "FaustReverb", "_native_owned", "_set_zone", "_plate_decay_from_seconds",
        "_drone_fb_from_seconds", "REVERB_PRESETS",
    }, namespace, strip_init={"FaustReverb"})
    reverb = namespace["FaustReverb"]()
    reverb._native_lock = threading.RLock()
    reverb.sample_rate = 48000
    reverb._dsp = object()
    reverb._zones = {key: [value] for key, value in {
        "feedback": 0.8, "damp": 0.5, "freeze_input": 1.0, "er_scale": 0.4,
        "predelay_ms": 25.0, "low_cut_hz": 80.0, "high_cut_hz": 7000.0,
        "shimmer_fb": 0.0, "noise_mod": 0.0,
    }.items()}
    reverb._feedback_target = reverb._normal_feedback = 0.8
    reverb._damp_target = reverb._normal_damp = 0.5
    reverb._freeze_capture_remaining = 0
    reverb.frozen = False
    reverb.type = "wash"
    reverb._plate = Backend()
    reverb._drone = Backend()
    reverb.plate_available = reverb.drone_available = True
    reverb._buf_n = 512
    reverb.set_decay(6.0)
    return reverb, namespace, clears


class FreezeRecoveryTests(unittest.TestCase):
    def assert_normal(self, reverb, namespace, damp):
        self.assertFalse(reverb.frozen)
        self.assertEqual(reverb._freeze_capture_remaining, 0)
        self.assertEqual(reverb._zones["freeze_input"][0], 1.0)
        self.assertEqual(reverb._zones["feedback"][0], reverb._normal_feedback)
        self.assertEqual(reverb._zones["damp"][0], damp)
        for backend in (reverb._plate, reverb._drone):
            if backend:
                self.assertEqual(backend.zones["freeze_input"], 1.0)
                self.assertEqual(backend.zones["damp"], damp)
        if reverb._plate:
            self.assertEqual(reverb._plate.zones["feedback"],
                             namespace["_plate_decay_from_seconds"](reverb.decay_seconds))
        if reverb._drone:
            self.assertEqual(reverb._drone.zones["feedback"],
                             namespace["_drone_fb_from_seconds"](reverb.decay_seconds))

    def seal(self, reverb):
        reverb.set_freeze(True)
        reverb._freeze_capture_remaining = 512  # final capture block
        reverb.process(np.zeros((2, 512)))
        self.assertEqual(reverb._plate.zones["freeze_input"], 0.0)
        self.assertEqual(reverb._drone.zones["freeze_input"], 0.0)

    def test_panic_reopens_and_restores_both_backends_before_clear(self):
        for backend_type in ("plate", "drone"):
            with self.subTest(backend_type=backend_type):
                reverb, namespace, clears = make_reverb()
                reverb.set_type(backend_type)
                self.seal(reverb)
                reverb.panic()
                self.assert_normal(reverb, namespace, 0.3)
                self.assertEqual(clears, [True])
                for backend in (reverb._plate, reverb._drone):
                    self.assertEqual(backend.clears[-1]["freeze_input"], 1.0)
                    self.assertEqual(backend.clears[-1]["damp"], 0.3)

    def test_edits_while_frozen_restore_current_settings_on_panic_or_unfreeze(self):
        for action in ("panic", "unfreeze"):
            with self.subTest(action=action):
                reverb, namespace, clears = make_reverb()
                reverb.set_type("plate")
                self.seal(reverb)
                reverb.set_decay(10.0)
                reverb.set_damp(0.27)
                self.assertEqual(reverb._feedback_target, 0.999)
                self.assertEqual(reverb._damp_target, 0.05)
                for backend in (reverb._plate, reverb._drone):
                    self.assertEqual(backend.zones["feedback"], 0.999)
                    self.assertEqual(backend.zones["damp"], 0.05)
                if action == "panic":
                    reverb.panic()
                else:
                    reverb.set_freeze(False)
                    self.assertEqual(clears, [])
                    self.assertEqual(len(reverb._plate.clears), 1)  # type entry only
                    self.assertEqual(reverb._drone.clears, [])
                self.assert_normal(reverb, namespace, 0.27)
                expected = 10 ** (-3 * (sum([63.7, 79.3, 95.3, 111.7,
                                             131.9, 153.1, 177.7, 200.9]) / 8000) / 10)
                self.assertAlmostEqual(reverb._zones["feedback"][0], expected)

    def test_panic_does_not_restore_stale_settings_from_previous_freeze(self):
        reverb, namespace, _ = make_reverb()
        reverb.set_freeze(True)
        reverb.set_freeze(False)
        reverb.set_decay(12.0)
        reverb.set_damp(0.73)
        desired_feedback = reverb._feedback_target
        reverb.panic()
        self.assert_normal(reverb, namespace, 0.73)
        self.assertEqual(reverb._feedback_target, desired_feedback)

    def test_recovery_preserves_type_early_reflection_level(self):
        for name in ("hall", "room", "bloom"):
            for action in ("panic", "unfreeze"):
                with self.subTest(name=name, action=action):
                    reverb, namespace, _ = make_reverb()
                    reverb.set_type(name)
                    reverb.set_freeze(True)
                    self.assertEqual(reverb._zones["er_scale"][0], 0.0)
                    if action == "panic":
                        reverb.panic()
                    else:
                        reverb.set_freeze(False)
                    self.assertEqual(reverb._zones["er_scale"][0],
                                     namespace["REVERB_PRESETS"][name]["er_scale"])

    def test_type_change_during_sealed_freeze_preserves_override_until_recovery(self):
        reverb, namespace, _ = make_reverb()
        reverb.set_type("plate")
        self.seal(reverb)
        reverb.set_type("drone")
        self.assertTrue(reverb.frozen)
        for backend in (reverb._plate, reverb._drone):
            self.assertEqual(backend.zones["feedback"], 0.999)
            self.assertEqual(backend.zones["damp"], 0.05)
            self.assertEqual(backend.zones["freeze_input"], 0.0)
        reverb.panic()
        self.assert_normal(reverb, namespace, 0.3)
        self.assertEqual(reverb.decay_seconds, 10.0)

    def test_repeated_panic_is_safe_with_unavailable_optional_backends(self):
        reverb, namespace, clears = make_reverb()
        reverb._plate = reverb._drone = None
        reverb.set_damp(0.7)
        reverb.set_freeze(True)
        reverb.panic()
        reverb.panic()
        self.assert_normal(reverb, namespace, 0.7)
        self.assertEqual(len(clears), 2)

    def test_expert_fdn_zone_setters_are_owned_and_clamped(self):
        reverb, _, _ = make_reverb()
        reverb.set_shimmer_feedback(3.0)
        reverb.set_noise_mod(-2.0)
        self.assertEqual(reverb._zones["shimmer_fb"][0], 1.0)
        self.assertEqual(reverb._zones["noise_mod"][0], 0.0)

    def test_panic_waits_until_native_process_returns(self):
        reverb, _, clears = make_reverb()
        entered = threading.Event()
        release = threading.Event()
        panic_done = threading.Event()

        def blocked_process(samples):
            entered.set()
            if not release.wait(2):
                raise AssertionError("process release timed out")
            return np.zeros_like(samples)

        reverb.type = "plate"
        reverb._plate.process = blocked_process
        renderer = threading.Thread(target=reverb.process,
                                    args=(np.zeros((2, 512)),))
        panicker = threading.Thread(target=lambda: (reverb.panic(), panic_done.set()))
        try:
            renderer.start()
            self.assertTrue(entered.wait(1))
            panicker.start()
            self.assertFalse(panic_done.wait(0.05))
            self.assertEqual(clears, [])
        finally:
            release.set()
            renderer.join(2)
            if panicker.ident is not None:
                panicker.join(2)
        self.assertFalse(renderer.is_alive())
        self.assertFalse(panicker.is_alive())
        self.assertTrue(panic_done.is_set())
        self.assertEqual(clears, [True])


if __name__ == "__main__":
    unittest.main()
