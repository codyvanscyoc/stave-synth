"""Offline regression tests for selected main-controller guards.

AST extraction executes the production handlers and EQ regex without importing
main, starting services, or loading any audio/native dependency. This covers
the repaired branches, not the application's complete input schema.
"""

import ast
import copy
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _handlers():
    source = Path(__file__).resolve().parents[1] / "stave_synth" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "StaveSynth")
    regex = next(node for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "_EQ_BAND_RE"
                         for target in node.targets))
    methods = [node for node in owner.body
               if isinstance(node, ast.FunctionDef)
               and node.name in ("_handle_setting", "_handle_ws_message")]
    assert len(methods) == 2
    namespace = {"_re": re, "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[regex, *methods], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


_HANDLERS = _handlers()


class Controller:
    _handle_setting = _HANDLERS["_handle_setting"]
    _handle_ws_message = _HANDLERS["_handle_ws_message"]

    def __init__(self):
        self.state = {
            "master": {"piano_octave": 0, "volume": 0.7},
            "piano": {"volume": 0.5, "eq_bands": [
                {"freq_hz": 1000.0, "gain_db": 0.0, "q": 1.0, "enabled": True}
                for _ in range(4)
            ]},
            "synth_pad": {"osc1_octave": 1, "osc2_octave": -1},
            "organ": {},
        }
        self.jack = SimpleNamespace(piano_octave=0)
        self.piano = SimpleNamespace(update_params=Mock())
        self.organ = None
        self.ws_server = None


class SettingGuardTests(unittest.TestCase):
    def test_master_piano_octave_updates_state_engine_and_canonical_ack(self):
        for value, expected in ((-100, -3), (-3, -3), (-1, -1), (0, 0),
                                (2, 2), (3, 3), (100, 3), ("2", 2)):
            with self.subTest(value=value):
                app = Controller()
                result = app._handle_setting({"section": "master", "param": "piano_octave", "value": value})
                self.assertEqual(app.state["master"]["piano_octave"], expected)
                self.assertEqual(app.jack.piano_octave, expected)
                self.assertIsInstance(app.jack.piano_octave, int)
                self.assertEqual(result, {"type": "setting_ack", "section": "master",
                                          "param": "piano_octave", "value": expected})
                self.assertEqual(app.state["master"]["volume"], 0.7)
                self.assertEqual(app.state["synth_pad"], {"osc1_octave": 1, "osc2_octave": -1})

    def test_master_piano_octave_updates_state_without_live_jack(self):
        app = Controller()
        app.jack = None
        result = app._handle_setting({"section": "master", "param": "piano_octave", "value": -20})
        self.assertEqual(app.state["master"]["piano_octave"], -3)
        self.assertEqual(result["value"], -3)

    def test_out_of_range_piano_eq_indices_reject_without_expansion_or_dispatch(self):
        for index in (4, 16, 1000000):
            for suffix, value in (("freq", 1000), ("gain", 3), ("q", 0.7), ("enabled", False)):
                with self.subTest(index=index, suffix=suffix):
                    app = Controller()
                    bands = app.state["piano"]["eq_bands"]
                    before = copy.deepcopy(app.state)
                    result = app._handle_setting({"section": "piano", "param": f"eq_band{index}_{suffix}",
                                                  "value": value})
                    self.assertEqual(result["type"], "error")
                    self.assertEqual(app.state, before)
                    self.assertIs(app.state["piano"]["eq_bands"], bands)
                    self.assertEqual(len(bands), 4)
                    app.piano.update_params.assert_not_called()

    def test_invalid_eq_index_does_not_even_create_missing_band_list(self):
        app = Controller()
        del app.state["piano"]["eq_bands"]
        result = app._handle_setting({"section": "piano", "param": "eq_band1000000_gain", "value": 3})
        self.assertEqual(result["type"], "error")
        self.assertNotIn("eq_bands", app.state["piano"])
        app.piano.update_params.assert_not_called()

    def test_highest_valid_eq_index_initializes_only_four_bands(self):
        app = Controller()
        del app.state["piano"]["eq_bands"]
        result = app._handle_setting({"section": "piano", "param": "eq_band3_freq", "value": "1200"})
        self.assertEqual(result["type"], "setting_ack")
        self.assertEqual(len(app.state["piano"]["eq_bands"]), 4)
        self.assertEqual(app.state["piano"]["eq_bands"][3]["freq_hz"], 1200.0)
        app.piano.update_params.assert_called_once_with({"eq_band3_freq": "1200"})


class DebugReadOnlyTests(unittest.TestCase):
    def debug_app(self):
        app = Controller()
        get_samples = Mock(side_effect=AssertionError("debug must never consume audio"))
        app.piano = SimpleNamespace(
            enabled=True, volume=0.5, fs=SimpleNamespace(get_samples=get_samples), sfid=1,
            _note_on_count=7, _render_count=19, _last_raw_peak=0.4,
        )
        app.synth = SimpleNamespace(voices=[object()], osc1_blend=0.6, osc2_blend=0.4)
        app.jack = SimpleNamespace(_bridge=SimpleNamespace(
            bridge_get_callback_count=Mock(return_value=20),
            bridge_get_peak_output=Mock(return_value=0.3),
            bridge_get_underrun_count=Mock(return_value=0),
            bridge_get_xrun_count=Mock(return_value=0),
            bridge_get_ring_fill=Mock(return_value=2),
        ))
        return app, get_samples

    def test_debug_returns_cached_piano_counters_without_get_samples(self):
        app, get_samples = self.debug_app()
        before = copy.deepcopy(app.state)
        result = app._handle_ws_message({"type": "debug"})
        get_samples.assert_not_called()
        self.assertEqual(result["type"], "debug")
        self.assertEqual(result["piano"]["note_on_count"], 7)
        self.assertEqual(result["piano"]["render_count"], 19)
        self.assertEqual(result["piano"]["last_raw_peak"], 0.4)
        self.assertIsNone(result["piano"]["test_peak"])
        self.assertNotIn("test_error", result["piano"])
        self.assertEqual(result["bridge_callbacks"], 20)
        self.assertEqual(app.state, before)

    def test_debug_does_not_consume_audio_when_optional_counters_are_missing(self):
        app, get_samples = self.debug_app()
        del app.piano._last_raw_peak
        result = app._handle_ws_message({"type": "debug"})
        get_samples.assert_not_called()
        self.assertEqual(result["type"], "debug")
        self.assertIn("test_error", result["piano"])

    def test_debug_handles_absent_piano_and_jack_without_audio_calls(self):
        app, get_samples = self.debug_app()
        app.piano = None
        app.jack = None
        result = app._handle_ws_message({"type": "debug"})
        get_samples.assert_not_called()
        self.assertEqual(result["piano"], {"exists": False})
        self.assertEqual(result["bridge_callbacks"], 0)


if __name__ == "__main__":
    unittest.main()
