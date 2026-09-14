"""Offline source-level lifecycle regressions for optional native engines."""

import ast
import logging
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def class_method(relative, class_name, method_name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == method_name)
    ast.fix_missing_locations(method)
    exec(compile(ast.Module(body=[method], type_ignores=[]), relative, "exec"), namespace)
    return namespace[method_name]


class FakePreload:
    def __init__(self, remains_alive):
        self.remains_alive = remains_alive
        self.joined = False

    def join(self, timeout=None):
        self.joined = True

    def is_alive(self):
        return self.remains_alive


class FluidLifecycleTests(unittest.TestCase):
    def test_program_select_nonzero_or_exception_is_truthful_failure(self):
        select = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer",
            "_program_select_checked", {},
        )
        for behavior in (lambda *args: -1, mock.Mock(side_effect=OSError("native"))):
            with self.subTest(behavior=behavior):
                owner = types.SimpleNamespace(
                    fs=types.SimpleNamespace(program_select=behavior)
                )
                with self.assertRaisesRegex(RuntimeError, "program selection failed"):
                    select(owner, 3, 4, "test preset")

    def test_all_program_select_paths_use_checked_helper(self):
        tree = ast.parse((ROOT / "stave_synth/fluidsynth_player.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "FluidSynthPlayer")
        for method_name, expected in (("start", 2), ("set_soundfont", 1)):
            method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                          and n.name == method_name)
            calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "_program_select_checked"]
            raw_calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "program_select"]
            self.assertEqual(len(calls), expected, method_name)
            self.assertEqual(raw_calls, [], method_name)

    def test_stop_handles_constructed_but_unstarted_preload(self):
        stop = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "stop",
            {"threading": threading, "logger": logging.getLogger(__name__)},
        )
        events = []
        owner = types.SimpleNamespace(
            _closing=False, enabled=True, _preload_stop=threading.Event(),
            _preload_thread=threading.Thread(target=lambda: None),
            fs=types.SimpleNamespace(delete=lambda: events.append("delete")),
            _lock=threading.RLock(), all_notes_off=lambda: events.append("panic"),
        )
        self.assertTrue(stop(owner, timeout=0.1))
        self.assertEqual(events, ["panic", "delete"])

    def test_preload_wait_reports_quiescence(self):
        wait = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "wait_for_preload",
            {"threading": threading, "logger": logging.getLogger(__name__)},
        )
        for remains_alive, expected in ((False, True), (True, False)):
            with self.subTest(remains_alive=remains_alive):
                preload = FakePreload(remains_alive=remains_alive)
                owner = types.SimpleNamespace(_preload_thread=preload)
                self.assertIs(wait(owner, timeout=0.25), expected)
                if remains_alive:
                    self.assertTrue(preload.joined)

    def test_preload_wait_before_start_is_already_quiescent(self):
        wait = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "wait_for_preload",
            {"threading": threading, "logger": logging.getLogger(__name__)},
        )
        self.assertTrue(wait(types.SimpleNamespace(_preload_thread=None)))

    def test_missing_soundfont_search_is_finite_not_recursive(self):
        tree = ast.parse((ROOT / "stave_synth/fluidsynth_player.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "FluidSynthPlayer")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_find_soundfont")
        recursive_calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute)
                           and n.func.attr == "_find_soundfont"]
        self.assertEqual(recursive_calls, [])

    def test_live_preset_switch_never_calls_sfload(self):
        tree = ast.parse((ROOT / "stave_synth/fluidsynth_player.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "FluidSynthPlayer")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                      and n.name == "set_soundfont")
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr == "sfload"
                             for n in ast.walk(method)))

    def test_unavailable_live_preset_raises_without_partial_state(self):
        presets = {"Rhodes": {"file": "FluidR3_GM", "program": 4,
                               "tremolo_hz": 5.0, "tremolo_depth": 0.6,
                               "velocity_curve": 1.4}}
        switch = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "set_soundfont",
            {"SOUNDFONT_PRESETS": presets, "logger": logging.getLogger(__name__)},
        )
        owner = types.SimpleNamespace(
            fs=types.SimpleNamespace(), _lock=threading.RLock(), _closing=False,
            _sfid_by_file={}, current_soundfont="Salamander", tremolo_hz=0.0,
            tremolo_depth=0.0, velocity_curve=1.0, _tremolo_phase=0.25,
        )
        with self.assertRaisesRegex(RuntimeError, "unavailable or still preloading"):
            switch(owner, "Rhodes")
        self.assertEqual(owner.current_soundfont, "Salamander")
        self.assertEqual(owner.tremolo_depth, 0.0)
        self.assertEqual(owner.velocity_curve, 1.0)

    def test_stop_retains_native_object_when_preload_is_not_quiescent(self):
        stop = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "stop",
            {"threading": threading, "logger": logging.getLogger(__name__)},
        )
        fs = types.SimpleNamespace(delete=lambda: self.fail("native object deleted"))
        preload = FakePreload(remains_alive=True)
        owner = types.SimpleNamespace(
            _closing=False, enabled=True, _preload_stop=threading.Event(),
            _preload_thread=preload, fs=fs, _lock=threading.RLock(),
            all_notes_off=lambda: self.fail("panic raced active preload"),
        )
        self.assertFalse(stop(owner, timeout=0.01))
        self.assertIs(owner.fs, fs)
        self.assertTrue(preload.joined)

    def test_stop_deletes_only_after_preload_has_joined(self):
        stop = class_method(
            "stave_synth/fluidsynth_player.py", "FluidSynthPlayer", "stop",
            {"threading": threading, "logger": logging.getLogger(__name__)},
        )
        events = []
        preload = FakePreload(remains_alive=False)
        fs = types.SimpleNamespace(delete=lambda: events.append("delete"))
        owner = types.SimpleNamespace(
            _closing=False, enabled=True, _preload_stop=threading.Event(),
            _preload_thread=preload, fs=fs, _lock=threading.RLock(),
            all_notes_off=lambda: events.append("panic"),
        )
        self.assertTrue(stop(owner, timeout=0.1))
        self.assertFalse(preload.joined)  # already quiescent; no join required
        self.assertEqual(events, ["panic", "delete"])
        self.assertIsNone(owner.fs)

    def test_room_native_calls_are_guarded(self):
        tree = ast.parse((ROOT / "stave_synth/fluidsynth_player.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "FluidSynthPlayer")
        for method_name in ("all_notes_off", "render_block", "update_params"):
            method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                          and n.name == method_name)
            guarded = [n for n in ast.walk(method) if isinstance(n, ast.With)
                       and any(isinstance(item.context_expr, ast.Attribute)
                               and item.context_expr.attr == "_piano_room_lock"
                               for item in n.items)]
            self.assertTrue(guarded, method_name)


class PythonOrganOwnershipTests(unittest.TestCase):
    def test_stateful_entrypoints_share_render_lock(self):
        tree = ast.parse((ROOT / "stave_synth/organ_engine.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "OrganEngine")
        expected = {"note_on", "note_off", "all_notes_off", "midi_callback",
                    "set_volume", "set_highcut", "set_lowcut", "set_tone_tilt",
                    "set_width", "set_preset", "render_block", "update_params"}
        decorated = set()
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            if any(isinstance(dec, ast.Name) and dec.id == "_organ_owned"
                   for dec in method.decorator_list):
                decorated.add(method.name)
        self.assertTrue(expected <= decorated)

    def test_dead_reaper_checks_rendered_voice_identity(self):
        source = (ROOT / "stave_synth/organ_engine.py").read_text()
        self.assertIn("if self.voices.get(n) is rendered.get(n):", source)


class ResetIdempotencyTests(unittest.TestCase):
    def test_identical_scene_ticks_do_not_reset_stateful_filters(self):
        update = class_method(
            "stave_synth/synth_engine.py", "SynthEngine", "update_params",
            {"LOW_RAM_MODE": False},
        )
        reset_l = mock.Mock()
        reset_r = mock.Mock()
        mellow_l = mock.Mock()
        mellow_r = mock.Mock()
        owner = types.SimpleNamespace(
            _render_lock=threading.RLock(),
            filter_slope=12,
            filter2_l=types.SimpleNamespace(reset=reset_l),
            filter2_r=types.SimpleNamespace(reset=reset_r),
            pad_mellow_enabled=True,
            _pad_mellow_lp_l=types.SimpleNamespace(reset=mellow_l),
            _pad_mellow_lp_r=types.SimpleNamespace(reset=mellow_r),
        )
        update(owner, {"filter_slope": 12, "pad_mellow_enabled": True})
        reset_l.assert_not_called()
        reset_r.assert_not_called()
        mellow_l.assert_not_called()
        mellow_r.assert_not_called()

        owner.filter_slope = 24
        owner.pad_mellow_enabled = False
        update(owner, {"filter_slope": 12, "pad_mellow_enabled": True})
        reset_l.assert_called_once_with()
        reset_r.assert_called_once_with()
        mellow_l.assert_called_once_with()
        mellow_r.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
