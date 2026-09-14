"""Isolated integration checks for runtime paths and routing gates.

No application, JACK server, network listener, or production path is touched.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from stave_synth.runtime import load_runtime


ROOT = Path(__file__).resolve().parent.parent


def _isolated_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "STAVE_INSTANCE": "integration_a",
        "STAVE_INSTANCE_ROOT": str(root),
        "STAVE_HTTP_PORT": "38080",
        "STAVE_WEBSOCKET_PORT": "38765",
    })
    return env


def _main_methods(*names: str, isolated: bool, jack_name: str):
    """Load exact selected StaveSynth methods without importing/starting the app."""

    tree = ast.parse((ROOT / "stave_synth" / "main.py").read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StaveSynth"
    )
    selected = [
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if {node.name for node in selected} != set(names):
        raise AssertionError("requested StaveSynth method missing")
    namespace = {
        # Relative imports may load pure routing helpers, never main/native
        # audio. Every executable command remains the injected mock below.
        "__name__": "stave_synth._isolated_main_methods",
        "__package__": "stave_synth",
        "ISOLATED": isolated,
        "JACK_CLIENT_NAME": jack_name,
        "logger": mock.Mock(),
        "subprocess": mock.Mock(),
        "threading": threading,
        "time": mock.Mock(),
        "save_state": mock.Mock(),
        "_run_with_hard_timeout": mock.Mock(side_effect=AssertionError("route mutation attempted")),
    }
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "stave_synth" / "main.py"), "exec"), namespace)
    return namespace


class InstancePathIntegrationTests(unittest.TestCase):
    def test_config_recorder_and_pad_paths_follow_isolated_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stave-integration-") as directory:
            instance_root = Path(directory) / "instance"
            code = r'''
import ast
import json
import types
from pathlib import Path
from stave_synth import config, recorder
tree = ast.parse(Path("stave_synth/main.py").read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StaveSynth")
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_pad_dir")
module = ast.Module(body=[method], type_ignores=[])
ast.fix_missing_locations(module)
namespace = {"DATA_DIR": config.DATA_DIR, "Path": Path}
exec(compile(module, "stave_synth/main.py", "exec"), namespace)
pad_dir = namespace["_pad_dir"](types.SimpleNamespace())
config.save_state({"master": {"volume": 0.123}})
print(json.dumps({
    "config": str(config.CONFIG_DIR),
    "data": str(config.DATA_DIR),
    "state": str(config.STATE_FILE),
    "recordings": str(recorder.RECORDINGS_DIR),
    "pads": str(pad_dir),
    "http": config.HTTP_PORT,
    "websocket": config.WEBSOCKET_PORT,
    "host": config.WEBSOCKET_HOST,
    "jack": config.JACK_CLIENT_NAME,
    "saved": config.load_state()["master"]["volume"],
}))
'''
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env=_isolated_env(instance_root),
                text=True,
                capture_output=True,
                check=True,
            )
            values = json.loads(result.stdout)
            resolved_root = instance_root.resolve()
            self.assertEqual(str(resolved_root / "config"), values["config"])
            self.assertEqual(str(resolved_root / "data"), values["data"])
            self.assertEqual(str(resolved_root / "config" / "current_state.json"), values["state"])
            self.assertEqual(str(resolved_root / "data" / "recordings"), values["recordings"])
            self.assertEqual(str(resolved_root / "data" / "pad_samples"), values["pads"])
            self.assertEqual((38080, 38765, "127.0.0.1", "StaveSynth_integration_a", 0.123), (
                values["http"], values["websocket"], values["host"], values["jack"], values["saved"]
            ))

    def test_stage_rejects_test_overrides_and_config_data_alias(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit non-stage"):
            load_runtime({"STAVE_INSTANCE": "stage", "STAVE_HTTP_PORT": "38080"})

        with tempfile.TemporaryDirectory(prefix="stave-alias-") as directory:
            root = Path(directory) / "instance"
            root.mkdir()
            (root / "data").mkdir()
            (root / "config").symlink_to(root / "data", target_is_directory=True)
            env = {
                "STAVE_INSTANCE": "alias_test",
                "STAVE_INSTANCE_ROOT": str(root),
                "STAVE_HTTP_PORT": "48080",
                "STAVE_WEBSOCKET_PORT": "48765",
            }
            with self.assertRaisesRegex(ValueError, "must not overlap each other"):
                load_runtime(env, home=Path(directory) / "home")


class RoutingIsolationTests(unittest.TestCase):
    def test_isolated_setup_and_output_handler_cannot_mutate_routes(self) -> None:
        namespace = _main_methods(
            "_setup_midi_bridge", "_handle_set_audio_output",
            isolated=True, jack_name="StaveSynth_integration_a",
        )
        owner = types.SimpleNamespace(state={"master": {"audio_output_pref": "old"}})
        namespace["_setup_midi_bridge"](owner)
        response = namespace["_handle_set_audio_output"](owner, {"name": "physical"})

        self.assertFalse(response["success"])
        self.assertEqual("old", owner.state["master"]["audio_output_pref"])
        namespace["subprocess"].run.assert_not_called()
        namespace["subprocess"].Popen.assert_not_called()
        namespace["_run_with_hard_timeout"].assert_not_called()

    def test_isolated_midi_check_is_read_only_and_uses_instance_identity(self) -> None:
        namespace = _main_methods(
            "_connect_midi_ports", isolated=True, jack_name="StaveSynth_integration_a"
        )
        checked = []
        owner = types.SimpleNamespace(
            _get_port_connections=lambda port: checked.append(port) or ["Controller:capture"]
        )
        self.assertTrue(namespace["_connect_midi_ports"](owner))
        self.assertEqual(["StaveSynth_integration_a:midi_in"], checked)
        namespace["subprocess"].run.assert_not_called()

    def test_failed_stage_jack_connect_is_not_reported_as_success(self) -> None:
        namespace = _main_methods(
            "_connect_midi_ports", isolated=False, jack_name="StaveSynth"
        )
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[1] == "jack_lsp":
                return types.SimpleNamespace(returncode=0, stderr="",
                                             stdout="Controller:capture\nStaveSynth:midi_in\n")
            return types.SimpleNamespace(returncode=1, stderr="injected connect failure", stdout="")

        namespace["_run_with_hard_timeout"].side_effect = run
        owner = types.SimpleNamespace(
            _control_lock=threading.RLock(), _stopping=False,
            _midi_discovery_error=None, _midi_capture_duplicates={},
            _get_midi_capture_ports=lambda: ["Controller:capture"],
        )
        self.assertFalse(namespace["_connect_midi_ports"](owner))
        namespace["subprocess"].run.assert_not_called()
        self.assertEqual(
            [["pw-jack", "jack_lsp", "-c"],
             ["pw-jack", "jack_connect", "Controller:capture", "StaveSynth:midi_in"],
             ["pw-jack", "jack_lsp", "-c"]], commands,
            "failed connect must be re-queried but never assumed successful",
        )


if __name__ == "__main__":
    unittest.main()
