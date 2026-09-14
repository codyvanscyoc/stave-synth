"""Python-side checks for the fixed native bridge contract.

Methods are extracted exactly from JackEngine so the test does not import DSP
dependencies or load a native/audio runtime.
"""

from __future__ import annotations

import ast
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "stave_synth" / "jack_engine.py"


def _methods(*names: str, **namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "JackEngine"
    )
    selected = [
        node for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in selected} != set(names):
        raise AssertionError("requested JackEngine method missing")
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class JackEngineContractTests(unittest.TestCase):
    def test_start_rejects_wrong_rate_before_workers_or_audio_controls(self) -> None:
        namespace = _methods(
            "start",
            JACK_CLIENT_NAME="StaveSynth_test",
            ISOLATED=True,
            SAMPLE_RATE=48000,
            BTL_MODE=False,
            threading=mock.Mock(),
            logger=mock.Mock(),
        )
        bridge = mock.Mock()
        bridge.bridge_start_named.return_value = 0
        bridge.bridge_get_sample_rate.return_value = 44100
        bridge.bridge_get_buffer_size.return_value = 512
        bridge.bridge_get_graph_error.return_value = 0
        owner = types.SimpleNamespace(_bridge=bridge, running=False,
                                      _stop_event=mock.Mock())

        with self.assertRaisesRegex(RuntimeError, "unsupported JACK graph"):
            namespace["start"](owner)

        bridge.bridge_stop.assert_called_once_with()
        self.assertFalse(owner.running)
        bridge.bridge_set_btl_mode.assert_not_called()
        namespace["threading"].Thread.assert_not_called()

    def test_start_rejects_latched_initial_graph_error(self) -> None:
        namespace = _methods(
            "start",
            JACK_CLIENT_NAME="StaveSynth_test",
            ISOLATED=True,
            SAMPLE_RATE=48000,
            BTL_MODE=False,
            threading=mock.Mock(),
            logger=mock.Mock(),
        )
        bridge = mock.Mock()
        bridge.bridge_start_named.return_value = 0
        bridge.bridge_get_sample_rate.return_value = 48000
        bridge.bridge_get_buffer_size.return_value = 512
        bridge.bridge_get_graph_error.return_value = 1
        owner = types.SimpleNamespace(_bridge=bridge, running=False,
                                      _stop_event=mock.Mock())

        with self.assertRaisesRegex(RuntimeError, "graph_error=1"):
            namespace["start"](owner)
        bridge.bridge_stop.assert_called_once_with()
        namespace["threading"].Thread.assert_not_called()

    def test_ring_depth_changes_only_after_native_acknowledgement(self) -> None:
        namespace = _methods("set_low_latency_mode", logger=mock.Mock())
        bridge = mock.Mock()
        bridge.bridge_set_ring_slots.return_value = -3
        owner = types.SimpleNamespace(_bridge=bridge, ring_threshold=8)

        self.assertFalse(namespace["set_low_latency_mode"](owner, True))
        self.assertEqual(8, owner.ring_threshold)

        bridge.bridge_set_ring_slots.return_value = 0
        self.assertTrue(namespace["set_low_latency_mode"](owner, True))
        self.assertEqual(3, owner.ring_threshold)

    def test_stop_joins_all_workers_before_releasing_bridge(self) -> None:
        namespace = _methods("stop", time=time, threading=threading, logger=mock.Mock())
        release = threading.Event()
        workers = [threading.Thread(target=lambda: release.wait(2)) for _ in range(3)]
        for worker in workers:
            worker.start()
        bridge = mock.Mock()
        synth = mock.Mock()
        owner = types.SimpleNamespace(
            synth=synth, running=True, _stop_event=release,
            _render_thread=workers[0], _midi_thread=workers[1], _gc_thread=workers[2],
            _bridge=bridge,
        )
        self.assertTrue(namespace["stop"](owner, timeout=1.0))
        synth.all_notes_off.assert_called_once_with()
        bridge.bridge_stop.assert_called_once_with()
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_stop_retains_bridge_when_worker_does_not_quiesce(self) -> None:
        namespace = _methods("stop", time=time, threading=threading, logger=mock.Mock())
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(2))
        worker.start()
        bridge = mock.Mock()
        owner = types.SimpleNamespace(
            synth=mock.Mock(), running=True, _stop_event=mock.Mock(),
            _render_thread=worker, _midi_thread=None, _gc_thread=None, _bridge=bridge,
        )
        try:
            self.assertFalse(namespace["stop"](owner, timeout=0.01))
            bridge.bridge_stop.assert_not_called()
        finally:
            release.set()
            worker.join(2)

    def test_gc_idle_gate_includes_tails_beds_freeze_and_recorder(self) -> None:
        namespace = _methods("_gc_sources_idle", time=time)
        lock = threading.RLock()
        synth = types.SimpleNamespace(
            _render_lock=lock, _panic_pending=False, freeze_enabled=False,
            reverb=types.SimpleNamespace(frozen=False), voices={}, _pad_samples={},
        )
        recorder = mock.Mock()
        recorder.current_status.return_value = {"recording": False, "writer_pending": False}
        now = time.monotonic()
        owner = types.SimpleNamespace(
            _last_audio_activity_ts=now - 100, recorder=recorder,
            _piano_notes_active=set(), piano_player=types.SimpleNamespace(_active_notes=0),
            synth=synth,
        )
        idle = namespace["_gc_sources_idle"]
        self.assertTrue(idle(owner, now=now))
        owner._last_audio_activity_ts = now - 1
        self.assertFalse(idle(owner, now=now))
        owner._last_audio_activity_ts = now - 100
        synth._pad_samples[60] = types.SimpleNamespace(active=True)
        self.assertFalse(idle(owner, now=now))
        synth._pad_samples.clear()
        owner.piano_player = types.SimpleNamespace(_active_notes=0, voices={60: object()})
        self.assertFalse(idle(owner, now=now))
        owner.piano_player = types.SimpleNamespace(_active_notes=0)
        synth.freeze_enabled = True
        self.assertFalse(idle(owner, now=now))
        synth.freeze_enabled = False
        recorder.current_status.return_value = {"recording": False, "writer_pending": True}
        self.assertFalse(idle(owner, now=now))


if __name__ == "__main__":
    unittest.main()
