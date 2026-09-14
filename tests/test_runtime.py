"""Standard-library tests for runtime isolation and ownership."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stave_synth.runtime import InstanceLock, load_runtime


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="stave-runtime-")
        self.base = Path(self.tempdir.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def isolated_env(self, **updates: str) -> dict[str, str]:
        env = {
            "STAVE_INSTANCE": "review_a",
            "STAVE_INSTANCE_ROOT": str(self.base / "instances" / "review_a"),
            "STAVE_HTTP_PORT": "18080",
            "STAVE_WEBSOCKET_PORT": "18765",
        }
        env.update(updates)
        return env

    def test_stage_defaults_are_unchanged_and_side_effect_free(self) -> None:
        runtime = load_runtime({}, home=self.home)
        self.assertEqual("stage", runtime.instance)
        self.assertFalse(runtime.isolated)
        resolved_home = self.home.resolve()
        self.assertEqual(resolved_home / ".config" / "stave-synth", runtime.config_dir)
        self.assertEqual(resolved_home / ".local" / "share" / "stave-synth", runtime.data_dir)
        self.assertEqual(("0.0.0.0", 8080, 8765, "StaveSynth"), (
            runtime.host, runtime.http_port, runtime.websocket_port, runtime.jack_client_name
        ))
        self.assertFalse(runtime.config_dir.exists())
        self.assertFalse(runtime.data_dir.exists())

    def test_isolated_values_and_no_directory_creation(self) -> None:
        runtime = load_runtime(self.isolated_env(), home=self.home)
        root = (self.base / "instances" / "review_a").resolve()
        self.assertTrue(runtime.isolated)
        self.assertEqual(root / "config", runtime.config_dir)
        self.assertEqual(root / "data", runtime.data_dir)
        self.assertEqual("127.0.0.1", runtime.host)
        self.assertEqual("StaveSynth_review_a", runtime.jack_client_name)
        self.assertFalse(root.exists())

    def test_invalid_identity_root_and_ports_are_rejected(self) -> None:
        cases = [
            {"STAVE_INSTANCE": "Stage"},
            {"STAVE_INSTANCE": "1test"},
            {"STAVE_INSTANCE": "a" * 33},
            {"STAVE_INSTANCE_ROOT": "relative/path"},
            {"STAVE_INSTANCE_ROOT": str(self.home)},
            {"STAVE_INSTANCE_ROOT": str(self.home.parent)},
            {"STAVE_HTTP_PORT": "8080"},
            {"STAVE_WEBSOCKET_PORT": "8765"},
            {"STAVE_HTTP_PORT": "80"},
            {"STAVE_HTTP_PORT": "70000"},
            {"STAVE_HTTP_PORT": "18765"},
            {"STAVE_HTTP_PORT": "not-a-port"},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    load_runtime(self.isolated_env(**updates), home=self.home)

        for required in ("STAVE_INSTANCE_ROOT", "STAVE_HTTP_PORT", "STAVE_WEBSOCKET_PORT"):
            env = self.isolated_env()
            del env[required]
            with self.subTest(missing=required), self.assertRaises(ValueError):
                load_runtime(env, home=self.home)

    def test_resolved_symlinks_cannot_alias_production_paths(self) -> None:
        stage_config = self.home / ".config" / "stave-synth"
        stage_data = self.home / ".local" / "share" / "stave-synth"
        stage_config.mkdir(parents=True)
        stage_data.mkdir(parents=True)

        alias_root = self.base / "alias-root"
        alias_root.mkdir()
        (alias_root / "config").symlink_to(stage_config, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            load_runtime(
                self.isolated_env(STAVE_INSTANCE_ROOT=str(alias_root)), home=self.home
            )

        inside_root = stage_data / "test-instance"
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            load_runtime(
                self.isolated_env(STAVE_INSTANCE_ROOT=str(inside_root)), home=self.home
            )

        ancestor_root = self.base / "ancestor-alias-root"
        ancestor_root.mkdir()
        (ancestor_root / "config").symlink_to(self.home / ".config", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            load_runtime(
                self.isolated_env(STAVE_INSTANCE_ROOT=str(ancestor_root)), home=self.home
            )

    def test_describe_cli_emits_only_json_without_creating_paths(self) -> None:
        env = os.environ.copy()
        env.update(self.isolated_env())
        proc = subprocess.run(
            [sys.executable, "-m", "stave_synth.runtime", "--describe"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        described = json.loads(proc.stdout)
        self.assertEqual("review_a", described["instance"])
        self.assertEqual("127.0.0.1", described["host"])
        self.assertEqual("", proc.stderr)
        self.assertFalse(Path(env["STAVE_INSTANCE_ROOT"]).exists())


class InstanceLockTests(unittest.TestCase):
    def test_lock_io_failure_is_not_reported_as_another_running_instance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stave-lock-error-") as directory:
            runtime = load_runtime({}, home=Path(directory))
            owner = InstanceLock(runtime)
            failure = OSError("injected filesystem failure")
            with mock.patch("stave_synth.runtime.fcntl.flock", side_effect=failure), \
                    mock.patch("stave_synth.runtime.os.close", wraps=os.close) as close:
                with self.assertRaisesRegex(RuntimeError, "cannot acquire lock") as raised:
                    owner.acquire()
                self.assertIs(raised.exception.__cause__, failure)
                close.assert_called_once()
                self.assertIsNone(owner._fd)
            # Failure must release its descriptor and allow an ordinary retry.
            with owner:
                pass

    def test_lock_is_non_reentrant_and_released_with_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stave-lock-") as directory:
            root = Path(directory) / "instance"
            runtime = load_runtime(
                {
                    "STAVE_INSTANCE": "lock_test",
                    "STAVE_INSTANCE_ROOT": str(root),
                    "STAVE_HTTP_PORT": "28080",
                    "STAVE_WEBSOCKET_PORT": "28765",
                },
                home=Path(directory) / "unrelated-home",
            )
            with InstanceLock(runtime):
                self.assertTrue((runtime.config_dir / "instance.lock").exists())
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    InstanceLock(runtime).acquire()

            with InstanceLock(runtime):
                pass


if __name__ == "__main__":
    unittest.main()
