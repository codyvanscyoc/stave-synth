"""Offline file/engine pad transaction tests with disposable WAVs and faults.

Runs the real handlers/helper and extracted real sampler/bank methods without
starting the application, native DSP, network listeners or recording devices.
Faults cover disk stages, install rejection, rollback and cleanup. No claim
of power-cut journaling or measured Pi4 performance is made.
"""

import ast
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from test_pad_preparation import ROOT, TrackingLock, audio_namespace, wav_bytes
from test_recorder_lifecycle import load_recorder


def load_store():
    state_path = ROOT / "stave_synth/state_store.py"
    state = {"__name__": "isolated_pad_store_state"}
    exec(compile(state_path.read_text(), str(state_path), "exec"), state)
    path = ROOT / "stave_synth/pad_store.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if not (isinstance(node, ast.ImportFrom) and node.level == 1)]
    namespace = {"__name__": "isolated_pad_store", "_fsync_directory": state["_fsync_directory"]}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


def load_handlers(directory, recorder, store):
    path = ROOT / "stave_synth/main.py"
    tree = ast.parse(path.read_text())
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StaveSynth")
    methods = {"_pad_dir", "_handle_list_pad_slots", "_handle_save_to_pad_slot", "_handle_clear_pad_slot"}
    app_class.body = [node for node in app_class.body if (
        isinstance(node, ast.FunctionDef) and node.name in methods) or (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_PAD_NOTE_FILENAMES"
                                             for target in node.targets))]

    class RemoveLocalImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.level == 1 and node.module in {"recorder", "pad_store"}:
                return ast.copy_location(ast.Pass(), node)
            return node

    app_class = RemoveLocalImports().visit(app_class)
    namespace = {"DATA_DIR": directory, "_R": recorder,
                 "MAX_SOURCE_BYTES": store["MAX_SOURCE_BYTES"],
                 "save_pad_slot": store["save_pad_slot"], "clear_pad_slot": store["clear_pad_slot"],
                 "_midi_to_note_label": lambda note: f"note-{note}",
                 "logger": logging.getLogger("isolated_pad_handlers")}
    exec(compile(ast.Module(body=[app_class], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["StaveSynth"]()


class PadTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stave-pad-transaction-")
        self.directory = Path(self.temporary.name)
        self.audio = audio_namespace()
        self.engine = self.audio["SynthEngine"]()
        self.engine.sample_rate = 48000
        self.engine._render_lock = TrackingLock()
        self.engine._pad_samples = {}
        self.recording = load_recorder(self.directory, STOP_TIMEOUT_SECONDS=0.3)
        self.Recorder = self.recording["Recorder"]
        self.recorder = self.Recorder()
        self.store = load_store()
        self.app = load_handlers(self.directory, self.Recorder, self.store)
        self.app.synth = self.engine
        self.pad_dir = self.app._pad_dir()
        self.target = self.pad_dir / "pad_C.wav"
        self.other_path = self.pad_dir / "pad_D.wav"
        self.target.write_bytes(wav_bytes(np.tile([-2000, 2000], 32)))
        self.other_path.write_bytes(wav_bytes(np.tile([-4000, 4000], 32)))
        self.engine.load_pad_samples(self.pad_dir)
        self.old = self.engine._pad_samples[60]
        self.old_bytes = self.target.read_bytes()
        self.other = self.engine._pad_samples[62]
        self.other.trigger()
        self.other.env = 0.6
        self.other.read_pos = 3
        self.source_meta = self.recorder.start({"test": "source"})
        block = np.full(128, 0.5, dtype=np.float32)
        self.recorder.feed(block, -block)
        self.assertTrue(self.recorder.stop()["complete"])
        self.source = Path(self.source_meta["path"])

    def tearDown(self):
        take = self.recorder._take
        self.recorder.stop()
        if take is not None:
            self.assertTrue(take.done.wait(2))
            self.recorder.stop()
        self.assertEqual(self.recording["_READ_CLAIMS"], {})
        self.assertIsNone(self.engine.pad_sample_status()["preparing_note"])
        self.temporary.cleanup()

    def save(self, note=60, source=None):
        return self.app._handle_save_to_pad_slot({"note": note, "source": source or self.source.name})

    def clear(self, note=60):
        return self.app._handle_clear_pad_slot({"note": note})

    def assert_other_untouched(self):
        self.assertIs(self.engine._pad_samples[62], self.other)
        self.assertTrue(self.other.active)
        self.assertEqual(self.other.env, 0.6)
        self.assertEqual(self.other.env_target, 1.0)
        self.assertEqual(self.other.read_pos, 3)

    def assert_old_preserved(self, result):
        self.assertEqual(result["type"], "error")
        self.assertEqual(self.target.read_bytes(), self.old_bytes)
        self.assertIs(self.engine._pad_samples[60], self.old)
        self.assertEqual(set(self.pad_dir.iterdir()), {self.target, self.other_path})
        self.assert_other_untouched()

    def test_save_commits_matching_file_and_player_without_reloading_other_slots(self):
        result = self.save()
        self.assertEqual(result["type"], "pad_slot_saved")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertIsNot(self.engine._pad_samples[60], self.old)
        self.assertEqual(self.engine._pad_samples[60].length, 128)
        np.testing.assert_array_equal(self.engine._pad_samples[60].samples_l, np.full(128, 16383 / 32768.0))
        self.assertEqual(set(self.pad_dir.iterdir()), {self.target, self.other_path})
        self.assert_other_untouched()

    def test_claim_covers_copy_and_preparation_and_releases_afterward(self):
        copy = self.store["_copy_bounded"]
        prepare = self.engine.prepare_pad_sample

        def checked_copy(*args):
            self.assertFalse(self.Recorder.delete_take(self.source.name))
            self.assertEqual(self.recording["prune_recordings"](0), 0)
            self.assertFalse(self.Recorder.is_take_active(self.source.name))
            return copy(*args)

        def checked_prepare(*args, **kwargs):
            self.assertFalse(self.Recorder.delete_take(self.source.name))
            return prepare(*args, **kwargs)

        with patch.dict(self.store, {"_copy_bounded": checked_copy}), patch.object(
                self.engine, "prepare_pad_sample", side_effect=checked_prepare):
            self.assertEqual(self.save()["type"], "pad_slot_saved")
        self.assertTrue(self.Recorder.delete_take(self.source.name))

    def test_copy_partial_write_and_fsync_failures_preserve_old_pair(self):
        def partial_copy(_source, output, _stat):
            output.write(b"partial staged data")
            raise OSError("injected copy failure")

        with patch.dict(self.store, {"_copy_bounded": partial_copy}):
            self.assert_old_preserved(self.save())
        with patch.object(os, "fsync", side_effect=OSError("injected sync failure")):
            self.assert_old_preserved(self.save())

    def test_invalid_preparation_and_hardlink_failure_preserve_old_pair(self):
        invalid = self.source.parent / "tiny-legacy.wav"
        invalid.write_bytes(wav_bytes([0, 0]))
        self.assert_old_preserved(self.save(source=invalid.name))
        with patch.object(os, "link", side_effect=OSError("injected backup link failure")):
            self.assert_old_preserved(self.save())

    def test_replace_failure_restores_old_target_and_releases_reservation(self):
        replace = os.replace

        def fail_commit(source, target):
            if Path(source).suffix == ".stage":
                raise OSError("injected replace failure")
            return replace(source, target)

        with patch.object(os, "replace", side_effect=fail_commit):
            self.assert_old_preserved(self.save())

    def test_install_failure_rolls_back_existing_and_absent_targets(self):
        with patch.object(self.engine, "install_pad_sample", side_effect=RuntimeError("injected install failure")):
            self.assert_old_preserved(self.save())
            absent = self.pad_dir / "pad_Cs.wav"
            result = self.save(note=61)
            self.assertEqual(result["type"], "error")
            self.assertFalse(absent.exists())
            self.assertNotIn(61, self.engine._pad_samples)
            self.assertEqual(set(self.pad_dir.iterdir()), {self.target, self.other_path})

    def test_replace_that_completes_then_raises_still_rolls_back(self):
        replace = os.replace

        def late_failure(source, target):
            result = replace(source, target)
            if Path(source).suffix == ".stage":
                raise OSError("injected post-rename failure")
            return result

        with patch.object(os, "replace", side_effect=late_failure):
            self.assert_old_preserved(self.save())
            self.assertEqual(self.save(note=61)["type"], "error")
        self.assertFalse((self.pad_dir / "pad_Cs.wav").exists())

    def test_failed_rollback_retains_original_recovery_file_and_reports_path(self):
        replace = os.replace

        def fail_restore(source, target):
            if Path(source).parent.suffix == ".recovery":
                raise OSError("injected rollback failure")
            return replace(source, target)

        with patch.object(os, "replace", side_effect=fail_restore), patch.object(
                self.engine, "install_pad_sample", side_effect=RuntimeError("injected install failure")):
            result = self.save()
        self.assertEqual(result["type"], "error")
        self.assertIn("rollback failed", result["message"])
        backups = list(self.pad_dir.glob("*.recovery/pad_C.wav"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old_bytes)
        self.assertIn(str(backups[0]), result["message"])
        self.assertIs(self.engine._pad_samples[60], self.old)
        self.assert_other_untouched()

    def test_cleanup_failure_after_success_is_warning_not_false_failure(self):
        unlink = Path.unlink

        def fail_backup_cleanup(path, *args, **kwargs):
            if path.parent.suffix == ".recovery":
                raise OSError("injected cleanup failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_backup_cleanup):
            result = self.save()
        self.assertEqual(result["type"], "pad_slot_saved")
        self.assertTrue(result["warnings"])
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertIsNot(self.engine._pad_samples[60], self.old)
        self.assertEqual(len(list(self.pad_dir.glob("*.recovery/pad_C.wav"))), 1)

    def test_clear_commits_only_target_and_missing_target_is_safe(self):
        result = self.clear()
        self.assertEqual(result["type"], "pad_slot_cleared")
        self.assertEqual(result["warnings"], [])
        self.assertFalse(self.target.exists())
        self.assertNotIn(60, self.engine._pad_samples)
        self.assertEqual(set(self.pad_dir.iterdir()), {self.other_path})
        self.assert_other_untouched()
        self.assertEqual(self.clear()["type"], "pad_slot_cleared")

    def test_clear_rename_and_install_failures_preserve_old_pair(self):
        with patch.object(os, "replace", side_effect=OSError("injected move failure")):
            self.assert_old_preserved(self.clear())
        with patch.object(self.engine, "install_pad_sample", side_effect=RuntimeError("injected install failure")):
            self.assert_old_preserved(self.clear())

    def test_clear_failed_rollback_retains_original_and_reports_recovery(self):
        replace = os.replace

        def fail_restore(source, target):
            if Path(source).parent.suffix == ".recovery":
                raise OSError("injected clear rollback failure")
            return replace(source, target)

        with patch.object(os, "replace", side_effect=fail_restore), patch.object(
                self.engine, "install_pad_sample", side_effect=RuntimeError("injected clear install failure")):
            result = self.clear()
        self.assertEqual(result["type"], "error")
        self.assertFalse(self.target.exists())
        backup = next(self.pad_dir.glob("*.recovery/pad_C.wav"))
        self.assertEqual(backup.read_bytes(), self.old_bytes)
        self.assertIn(str(backup), result["message"])
        self.assertIs(self.engine._pad_samples[60], self.old)

    def test_active_target_is_rejected_before_copy_or_file_move(self):
        self.old.trigger()
        with patch.dict(self.store, {"_copy_bounded": Mock()}) as _patch:
            copy = self.store["_copy_bounded"]
            result = self.save()
            copy.assert_not_called()
        self.assert_old_preserved(result)
        self.assertIn("Stop this pad", result["message"])
        self.assert_old_preserved(self.clear())

    def test_oversized_legacy_source_rejects_before_any_staging_file(self):
        oversized = self.source.parent / "oversized.wav"
        with oversized.open("wb") as stream:
            stream.truncate(self.store["MAX_SOURCE_BYTES"] + 1)
        with patch.object(tempfile, "mkstemp", wraps=tempfile.mkstemp) as reserve:
            result = self.save(source=oversized.name)
        reserve.assert_not_called()
        self.assertIn("64 MiB", result["message"])
        self.assert_old_preserved(result)

    def test_bounded_copy_rejects_growth_without_writing_beyond_limit(self):
        original_stat = self.source.stat()
        with self.source.open("rb") as actual:
            source = SimpleNamespace(read=io.BytesIO(b"x" * 130).read, fileno=actual.fileno)
            output = io.BytesIO()
            with patch.dict(self.store, {"MAX_SOURCE_BYTES": 128, "COPY_CHUNK_BYTES": 16}):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    self.store["_copy_bounded"](source, output, original_stat)
            self.assertLessEqual(len(output.getvalue()), 128)

    def test_active_and_known_incomplete_sources_cannot_be_saved(self):
        active = self.recorder.start()
        self.assert_old_preserved(self.save(source=active["filename"]))
        self.recorder.stop()
        metadata_path = self.source.with_suffix(".meta.json")
        import json
        metadata = json.loads(metadata_path.read_text())
        metadata.update(complete=False, status="incomplete", dropped_blocks=1, dropped_frames=4)
        self.recording["atomic_write_json"](metadata_path, metadata)
        self.assert_old_preserved(self.save())

    def test_slot_list_reports_actual_resident_bank_and_explicit_file_errors(self):
        invalid = self.pad_dir / "pad_E.wav"
        invalid.write_bytes(b"not loaded")
        self.other_path.unlink()
        self.engine._pad_load_errors = {64: "Invalid WAV header"}
        result = self.app._handle_list_pad_slots({})
        slots = {item["note"]: item for item in result["slots"]}
        self.assertTrue(slots[60]["loaded"])
        self.assertTrue(slots[62]["loaded"])
        self.assertFalse(slots[62]["file_present"])
        self.assertTrue(slots[62]["active"])
        self.assertFalse(slots[64]["loaded"])
        self.assertTrue(slots[64]["file_present"])
        self.assertEqual(slots[64]["error"], "Invalid WAV header")
        self.assertEqual(result["memory_bytes"], self.old.memory_bytes + self.other.memory_bytes)
        self.assertEqual(result["max_bank_bytes"], 192 * 1024 * 1024)
        self.assertEqual(result["max_slot_bytes"], 32 * 1024 * 1024)
        self.assertEqual(result["max_source_bytes"], 64 * 1024 * 1024)

    def test_missing_engine_rejects_mutations_and_does_not_claim_files_loaded(self):
        self.app.synth = None
        self.assertEqual(self.save()["type"], "error")
        self.assertEqual(self.clear()["type"], "error")
        result = self.app._handle_list_pad_slots({})
        self.assertFalse(any(slot["loaded"] for slot in result["slots"]))
        self.assertEqual(self.target.read_bytes(), self.old_bytes)


if __name__ == "__main__":
    unittest.main()
