"""Offline AST-extracted tests for opt-in piano lock attribution."""

import ast
import logging
import math
import os
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import MethodType
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "stave_synth/fluidsynth_player.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
BODY = []
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id.startswith(("_MIDI_EVENT_", "_LOCK_DIAGNOSTIC_"))
            for target in node.targets):
        BODY.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "FluidSynthPlayer":
        BODY.append(node)
ENV = {
    "logging": logging,
    "logger": logging.getLogger(__name__),
    "math": math,
    "os": os,
    "threading": threading,
    "time": time,
    "deque": deque,
    "np": np,
    "Path": Path,
    "SAMPLE_RATE": 48000,
    "LOW_RAM_MODE": False,
    "USE_FAUST_PIANO_CHAIN": False,
    "SOUNDFONT_PRESETS": {},
    "PIANO_VOICINGS": {},
    "SOUNDFONT_DIR": ROOT / "soundfonts",
}
exec(compile(ast.Module(body=BODY, type_ignores=[]), str(SOURCE), "exec"), ENV)
FluidSynthPlayer = ENV["FluidSynthPlayer"]
DIAGNOSTIC_CAPACITY = ENV["_LOCK_DIAGNOSTIC_CAPACITY"]


class FakeFluidSynth:
    def get_samples(self, count):
        raw = np.empty(count * 2, dtype=np.int16)
        raw[0::2] = 1200
        raw[1::2] = -900
        return raw


def make_player(*, diagnostics=True):
    player = FluidSynthPlayer.__new__(FluidSynthPlayer)
    player.sample_rate = 48000
    player.fs = FakeFluidSynth()
    player.enabled = True
    player._closing = False
    player._lock = threading.RLock()
    player._midi_event_lock = threading.Lock()
    player._midi_events = deque()
    player._midi_recovery_pending = False
    player._midi_recovery_failed_latched = False
    player._midi_events_discarded = 0
    player._midi_queue_lock_deferrals = 0
    player._native_render_lock_misses = 0
    player._active_notes = 1
    player._silent_blocks = 0
    player._render_count = 0
    player.volume = player._volume_cur = 1.0
    player._faust_chain = object()
    player._render_chain_faust = MethodType(
        lambda self, raw, count: (
            raw[0::2].astype(np.float64) / 32768.0,
            raw[1::2].astype(np.float64) / 32768.0,
        ), player,
    )
    player.vel_bright_enabled = False
    player.vel_bright_amount = 0.0
    player.tremolo_depth = 0.0
    player.tremolo_hz = 0.0
    player._piano_room = None
    player._diagnostics_enabled = diagnostics
    player._diagnostic_lock = threading.Lock()
    player._diagnostic_native_owner = None
    player._diagnostic_owner_generation = 0
    player._diagnostic_render_block_index = 0
    player._diagnostic_lock_misses = (
        deque(maxlen=DIAGNOSTIC_CAPACITY) if diagnostics else None
    )
    player._diagnostic_records_overwritten = 0
    player._diagnostic_records_dropped = 0
    return player


class PianoLockDiagnosticTests(unittest.TestCase):
    def test_opt_in_requires_exact_environment_value(self):
        cls = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
                   and node.name == "FluidSynthPlayer")
        init = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name == "__init__")
        assignment = next(
            node for node in ast.walk(init)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Attribute)
                    and target.attr == "_diagnostics_enabled"
                    for target in node.targets)
        )
        self.assertEqual(
            ast.unparse(assignment.value),
            "os.environ.get('STAVE_DIAGNOSTICS') == '1'",
        )

    def test_controlled_all_notes_off_overlap_has_stable_attribution(self):
        player = make_player()
        owner_entered = threading.Event()
        release_owner = threading.Event()

        def blocked_release(self):
            owner_entered.set()
            release_owner.wait(2.0)
            return True

        player._release_all_native_locked = MethodType(blocked_release, player)
        panic_result = []
        panic = threading.Thread(target=lambda: panic_result.append(
            player.all_notes_off()
        ))
        panic.start()
        self.assertTrue(owner_entered.wait(1.0))
        try:
            rendered = player.render_block(512)
        finally:
            release_owner.set()
            panic.join(timeout=2.0)

        self.assertTrue(np.array_equal(rendered, np.zeros((2, 512))))
        self.assertEqual(panic_result, [True])
        diagnostics = player._diagnostic_snapshot()
        self.assertEqual(len(diagnostics["records"]), 1)
        record = diagnostics["records"][0]
        self.assertEqual(record["native_owner_operation"], "all_notes_off")
        self.assertEqual(record["render_block_index"], 1)
        self.assertEqual(record["owner_generation"], 1)
        self.assertLessEqual(record["owner_acquired_monotonic_ns"],
                             record["monotonic_ns"])
        self.assertIsNone(diagnostics["current_owner"])

    def test_disabled_path_uses_no_clock_and_records_nothing(self):
        player = make_player(diagnostics=False)
        locked = threading.Event()
        release = threading.Event()

        def hold_native():
            with player._lock:
                locked.set()
                release.wait(2.0)

        owner = threading.Thread(target=hold_native)
        owner.start()
        self.assertTrue(locked.wait(1.0))
        try:
            with mock.patch.object(time, "monotonic_ns",
                                   side_effect=AssertionError("clock used")):
                rendered = player.render_block(512)
        finally:
            release.set()
            owner.join(timeout=2.0)
        self.assertTrue(np.array_equal(rendered, np.zeros((2, 512))))
        self.assertEqual(player._diagnostic_render_block_index, 0)
        self.assertEqual(player._diagnostic_snapshot()["records"], [])

    def test_owner_change_across_try_acquire_is_reported_unknown(self):
        player = make_player()
        old_owner = ("sfload", 100, 1)
        new_owner = ("program_change", 200, 2)
        player._diagnostic_native_owner = old_owner

        class TransitioningLock:
            def acquire(inner_self, blocking=False):
                self.assertFalse(blocking)
                player._diagnostic_native_owner = new_owner
                return False

        player._lock = TransitioningLock()
        player.render_block(512)
        record = player._diagnostic_snapshot()["records"][0]
        self.assertEqual(record["native_owner_operation"], "unknown")
        self.assertIsNone(record["owner_acquired_monotonic_ns"])
        self.assertIsNone(record["owner_generation"])

    def test_owner_exception_cleanup_does_not_leave_stale_attribution(self):
        player = make_player()
        player._release_all_native_locked = MethodType(
            lambda self: (_ for _ in ()).throw(OSError("native fault")), player
        )
        with self.assertRaisesRegex(OSError, "native fault"):
            player.all_notes_off()
        self.assertIsNone(player._diagnostic_native_owner)

        player._lock = type("AlwaysBusy", (), {
            "acquire": lambda self, blocking=False: False,
        })()
        player.render_block(512)
        record = player._diagnostic_snapshot()["records"][0]
        self.assertEqual(record["native_owner_operation"], "unknown")

    def test_records_are_bounded_and_count_overwrites(self):
        player = make_player()
        owner = ("shutdown", 100, 9)
        player._diagnostic_native_owner = owner
        for block in range(DIAGNOSTIC_CAPACITY + 7):
            player._record_native_lock_miss(block, owner)
        diagnostics = player._diagnostic_snapshot()
        self.assertEqual(len(diagnostics["records"]), DIAGNOSTIC_CAPACITY)
        self.assertEqual(diagnostics["overwritten"], 7)
        self.assertEqual(diagnostics["records"][0]["render_block_index"], 7)

    def test_record_path_never_waits_for_diagnostic_snapshot_lock(self):
        player = make_player()
        owner = ("sfload", 100, 1)
        player._diagnostic_native_owner = owner
        self.assertTrue(player._diagnostic_lock.acquire(blocking=False))
        completed = threading.Event()
        try:
            worker = threading.Thread(target=lambda: (
                player._record_native_lock_miss(1, owner), completed.set()
            ))
            worker.start()
            self.assertTrue(completed.wait(1.0), "record waited on diagnostic lock")
        finally:
            player._diagnostic_lock.release()
            worker.join(timeout=2.0)
        diagnostics = player._diagnostic_snapshot()
        self.assertEqual(diagnostics["records"], [])
        self.assertEqual(diagnostics["dropped"], 1)

    def test_all_native_control_operations_publish_named_owner(self):
        cls = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
                   and node.name == "FluidSynthPlayer")
        expected = {
            "start": "sfload",
            "set_soundfont": "program_change",
            "all_notes_off": "all_notes_off",
            "stop": "shutdown",
        }
        for method_name, operation in expected.items():
            method = next(node for node in cls.body
                          if isinstance(node, ast.FunctionDef)
                          and node.name == method_name)
            literals = [node.value for node in ast.walk(method)
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)]
            self.assertIn("_diagnostic_owner_enter", literals, method_name)
            self.assertIn(operation, literals, method_name)


if __name__ == "__main__":
    unittest.main()
