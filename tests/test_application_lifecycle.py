"""Offline ownership tests for main.py application startup and cleanup."""

from __future__ import annotations

import ast
import os
import signal as signal_module
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "stave_synth" / "main.py"


def _exact_functions():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    run_application = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_application"
    )
    stave_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "StaveSynth"
    )
    stop = next(
        node for node in stave_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "stop"
    )
    return run_application, stop


_RUN_APPLICATION_NODE, _STOP_NODE = _exact_functions()


class _Time:
    def __init__(self, effect):
        self.effect = effect
        self.calls = 0

    def sleep(self, seconds):
        self.calls += 1
        if self.effect is not None:
            raise self.effect


class _App:
    def __init__(self, start_effect=None, stop_effect=None):
        self.start_effect = start_effect
        self.stop_effect = stop_effect
        self.start_calls = 0
        self.stop_calls = []

    def start(self):
        self.start_calls += 1
        if callable(self.start_effect):
            self.start_effect()
        elif self.start_effect is not None:
            raise self.start_effect

    def stop(self, *, save_final_state=True):
        self.stop_calls.append(save_final_state)
        if self.stop_effect is not None:
            raise self.stop_effect


def _run_namespace(app, *, sleep_effect=KeyboardInterrupt()):
    handlers = {}
    fake_signal = SimpleNamespace(
        SIGINT=signal_module.SIGINT,
        SIGTERM=signal_module.SIGTERM,
        signal=lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    namespace = {
        "StaveSynth": lambda: app,
        "signal": fake_signal,
        "os": SimpleNamespace(environ={}),
        "sys": SimpleNamespace(argv=["stave-synth", "--no-gui"], exc_info=sys.exc_info),
        "time": _Time(sleep_effect),
        "LOW_RAM_MODE": False,
        "HTTP_PORT": 8080,
        "RUNTIME": SimpleNamespace(host="127.0.0.1"),
        "logger": mock.Mock(),
    }
    module = ast.Module(body=[_RUN_APPLICATION_NODE], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace, handlers


class ApplicationLifecycleTests(unittest.TestCase):
    def test_failed_piano_start_retains_owner_and_does_not_continue_to_audio(self):
        tree = ast.parse(MAIN_PATH.read_text())
        stave = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StaveSynth")
        start = next(n for n in stave.body if isinstance(n, ast.FunctionDef) and n.name == "start")
        piano = mock.Mock(start=mock.Mock(side_effect=RuntimeError("program failed")))
        namespace = {"logger": mock.Mock(), "ensure_jack_running": lambda: True,
                     "FluidSynthPlayer": lambda: piano}
        module = ast.Module(body=[start], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(MAIN_PATH), "exec"), namespace)
        owner = SimpleNamespace(_controls=mock.Mock(), presets=mock.Mock(),
                                _rebuild_preset_saved=mock.Mock(),
                                state={"ui": {"preset_labels": [""] * 10}}, piano=None)
        with self.assertRaisesRegex(RuntimeError, "piano startup failed: program failed"):
            namespace["start"](owner)
        self.assertIs(owner.piano, piano)
        piano.update_params.assert_not_called()

    def test_start_failure_cleans_without_saving_and_preserves_failure(self):
        failure = RuntimeError("listener bind failed")
        app = _App(start_effect=failure)
        namespace, _ = _run_namespace(app)

        with self.assertRaisesRegex(RuntimeError, "listener bind failed") as raised:
            namespace["_run_application"]()

        self.assertIs(raised.exception, failure)
        self.assertEqual(app.stop_calls, [False])

    def test_signal_during_start_uses_single_finally_cleanup_owner(self):
        app = _App()
        namespace, handlers = _run_namespace(app)

        def interrupt_start():
            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)

        app.start_effect = interrupt_start
        with self.assertRaises(SystemExit) as raised:
            namespace["_run_application"]()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(app.stop_calls, [False])
        self.assertEqual(set(handlers), {signal_module.SIGINT, signal_module.SIGTERM})

    def test_normal_headless_keyboard_interrupt_saves_once_on_shutdown(self):
        app = _App()
        namespace, _ = _run_namespace(app, sleep_effect=KeyboardInterrupt())

        namespace["_run_application"]()

        self.assertEqual(app.start_calls, 1)
        self.assertEqual(app.stop_calls, [True])
        self.assertEqual(namespace["time"].calls, 1)

    def test_original_start_failure_survives_cleanup_failure(self):
        startup_failure = ValueError("original startup failure")
        app = _App(
            start_effect=startup_failure,
            stop_effect=RuntimeError("secondary cleanup failure"),
        )
        namespace, _ = _run_namespace(app)

        with self.assertRaisesRegex(ValueError, "original startup failure") as raised:
            namespace["_run_application"]()

        self.assertIs(raised.exception, startup_failure)
        self.assertEqual(app.stop_calls, [False])
        namespace["logger"].exception.assert_called_once_with("Application cleanup failed")

    def test_stop_flag_skips_only_final_state_save(self):
        save_state = mock.Mock()
        logger = mock.Mock()
        namespace = {"save_state": save_state, "logger": logger, "time": time, "threading": threading}
        module = ast.Module(body=[_STOP_NODE], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(MAIN_PATH), "exec"), namespace)

        owner = SimpleNamespace(
            state={"partially": "initialized"},
            _running=True,
            _crossfade_cancel=mock.Mock(),
            _stop_event=threading.Event(),
            _controls=mock.Mock(),
            _ui_lifecycle_lock=threading.Lock(),
            _control_lock=threading.RLock(),
            jack=mock.Mock(),
            piano=mock.Mock(),
            organ=mock.Mock(),
            ws_server=mock.Mock(stop=mock.Mock(return_value=True)),
        )
        namespace["stop"](owner, save_final_state=False)

        save_state.assert_not_called()
        self.assertFalse(owner._running)
        owner._crossfade_cancel.set.assert_called_once_with()
        owner.jack.stop.assert_called_once()
        owner.piano.stop.assert_called_once()
        self.assertGreaterEqual(owner.jack.stop.call_args.kwargs["timeout"], 0)
        owner.organ.all_notes_off.assert_called_once_with()
        owner.ws_server.stop.assert_called_once_with()

    def test_launcher_refuses_implicit_stage_before_python_command(self):
        with tempfile.TemporaryDirectory(prefix="stave-launcher-refusal-") as directory:
            sentinel = Path(directory) / "must-not-run"
            env = os.environ.copy()
            env.pop("STAVE_INSTANCE", None)
            env["STAVE_PYTHON"] = str(sentinel)
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "stave-synth.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Choose --stage explicitly", result.stderr)
        self.assertNotIn("No such file", result.stderr)


if __name__ == "__main__":
    unittest.main()
