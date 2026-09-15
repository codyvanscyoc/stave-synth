"""Actual controller branches, extracted without launching a synth or thread."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock
from stave_synth.state_schema import validate_message


def handlers():
    path = Path(__file__).resolve().parents[1] / "stave_synth/main.py"
    cls = next(node for node in ast.parse(path.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == "StaveSynth")
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)
               and node.name in {"_handle_drone_fade", "_dispatch_ws_message", "_handle_panic",
                                 "_start_drone_fade_ramp"}]
    namespace = {"logger": logging.getLogger(__name__), "LOW_RAM_MODE": True,
                 "FluidSynthPlayer": SimpleNamespace(list_available_soundfonts=lambda: ["Fluid"])}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


_HANDLERS = handlers()


class App:
    _handle_drone_fade = _HANDLERS["_handle_drone_fade"]
    _dispatch_ws_message = _HANDLERS["_dispatch_ws_message"]
    _handle_panic = _HANDLERS["_handle_panic"]

    def __init__(self):
        self.synth = SimpleNamespace(_drone_fade_scale=1.0, panic=Mock(), reverb=None)
        self._drone_fade_target = 1.0
        self._start_drone_fade_ramp = Mock()
        self.state = {"synth_pad": {"drone_enabled": True, "drone_key": 60}}
        self.jack = None
        self.ws_server = None
        self.piano = self.organ = None
        self.midi = SimpleNamespace(all_notes_off=Mock())
        self._health_status = lambda: {}
        self._crossfade_cancel = threading.Event()


class DroneFadeTests(unittest.TestCase):
    def test_two_early_toggle_presses_reverse_target_not_current_gain(self):
        app = App()
        self.assertTrue(app._handle_drone_fade({})["faded_out"])
        app.synth._drone_fade_scale = 0.98
        self.assertFalse(app._handle_drone_fade({})["faded_out"])
        self.assertEqual([call.args[0] for call in app._start_drone_fade_ramp.call_args_list], [0.0, 1.0])

    def test_absolute_requests_preserve_endpoint_and_honor_duration(self):
        app = App()
        for faded in (True, True, False, False):
            message = validate_message({"type": "drone_fade", "faded_out": faded, "duration_s": 2})
            self.assertEqual(app._handle_drone_fade(message)["faded_out"], faded)
            app._start_drone_fade_ramp.assert_called_with(0.0 if faded else 1.0, 2.0)

    def test_schema_optional_null_retains_toggle_semantics(self):
        app = App()
        message = validate_message({"type": "drone_fade", "faded_out": None})
        self.assertTrue(app._handle_drone_fade(message)["faded_out"])
        self.assertFalse(app._handle_drone_fade(message)["faded_out"])

    def test_reconnect_hydrates_target_even_while_gain_has_not_reached_it(self):
        app = App()
        app._handle_drone_fade({})
        response = app._dispatch_ws_message({"type": "get_state"})
        self.assertTrue(response["drone_faded_out"])
        self.assertEqual(app.synth._drone_fade_scale, 1.0)
        self.assertFalse(response["faded_out"])

    def test_panic_cancels_ramp_and_resets_target_and_hydration(self):
        app = App()
        app._drone_fade_cancel = threading.Event()
        app._drone_fade_target = 0.0
        self.assertEqual(app._handle_panic()["type"], "panic_ack")
        self.assertTrue(app._drone_fade_cancel.is_set())
        self.assertEqual(app._drone_fade_target, 1.0)
        self.assertFalse(app._dispatch_ws_message({"type": "get_state"})["drone_faded_out"])
        self.assertFalse(app.state["synth_pad"]["drone_enabled"])

    def test_failed_ramp_start_does_not_claim_new_target(self):
        app = App()
        app._start_drone_fade_ramp.side_effect = RuntimeError("thread unavailable")
        with self.assertRaises(RuntimeError):
            app._handle_drone_fade({})
        self.assertEqual(app._drone_fade_target, 1.0)

    def test_actual_ramp_cancels_and_reverses_from_current_gain(self):
        class Synth:
            changed = threading.Event()
            _gain = 1.0

            @property
            def _drone_fade_scale(self):
                return self._gain

            @_drone_fade_scale.setter
            def _drone_fade_scale(self, gain):
                self._gain = gain
                self.changed.set()

        app = App()
        app.synth = Synth()
        app._control_lock = threading.RLock()
        app._stopping = False
        app._start_drone_fade_ramp = _HANDLERS["_start_drone_fade_ramp"].__get__(app)
        with app._control_lock:
            app._handle_drone_fade({"duration_s": 0.5})
        first_thread = app._drone_fade_thread
        first_cancel = app._drone_fade_cancel
        try:
            self.assertTrue(app.synth.changed.wait(0.5))
            with app._control_lock:
                self.assertLess(app.synth._drone_fade_scale, 1.0)
                app._handle_drone_fade({"duration_s": 0.02})
            first_thread.join(1)
            app._drone_fade_thread.join(1)
            self.assertTrue(first_cancel.is_set())
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(app._drone_fade_thread.is_alive())
            self.assertEqual(app.synth._drone_fade_scale, 1.0)
        finally:
            first_cancel.set()
            app._drone_fade_cancel.set()
            first_thread.join(1)
            app._drone_fade_thread.join(1)


if __name__ == "__main__":
    unittest.main()
