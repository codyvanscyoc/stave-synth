"""Explicit offline recorder lifecycle tests; no app, device, or home data.

The real recorder and atomic writer sources execute with only relative
imports replaced. Every WAV/JSON lives in a TemporaryDirectory. Event gates
reproduce stalled I/O and producer races without timing-dependent sleeps.
These are integrity/ownership checks, not Pi4 throughput or power-loss tests.
"""

import ast
import json
import queue
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def load_recorder(data_dir, **overrides):
    store_path = ROOT / "stave_synth/state_store.py"
    store_namespace = {"__name__": "isolated_recorder_store"}
    exec(compile(store_path.read_text(), str(store_path), "exec"), store_namespace)
    path = ROOT / "stave_synth/recorder.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.level == 1
        and node.module in {"config", "state_store"})]
    namespace = {
        "__name__": "isolated_recorder_lifecycle", "DATA_DIR": data_dir,
        "SAMPLE_RATE": 48000, "atomic_write_json": store_namespace["atomic_write_json"],
    }
    exec(compile(tree, str(path), "exec"), namespace)
    namespace.update(overrides)
    return namespace


class ControlledWave:
    """An actual file-backed WAV with optional event-gated fault injection."""
    def __init__(self, handle, *, block_write=False, write_error=False, close_error=False):
        self.handle = handle
        self.write_entered = threading.Event()
        self.allow_write = threading.Event()
        if not block_write:
            self.allow_write.set()
        self.write_error = write_error
        self.close_error = close_error
        self.write_calls = 0
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def writeframes(self, data):
        self.write_calls += 1
        self.write_entered.set()
        if not self.allow_write.wait(3):
            raise TimeoutError("test write gate was not released")
        if self.write_error:
            raise OSError("injected disk write failure")
        self.handle.writeframes(data)

    def close(self):
        self.close_calls += 1
        self.handle.close()
        if self.close_error:
            raise OSError("injected WAV close failure")


class RecorderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stave-recorder-lifecycle-")
        self.directory = Path(self.temporary.name)
        self.namespace = load_recorder(self.directory, STOP_TIMEOUT_SECONDS=0.3)
        self.Recorder = self.namespace["Recorder"]
        self.recorder = self.Recorder()
        self.recorders = [self.recorder]
        self.release_events = []
        self.test_threads = []
        self.handles = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for thread in self.test_threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "test producer/control thread leaked")
        for recorder in self.recorders:
            take = recorder._take
            recorder.stop()
            if take is not None:
                self.assertTrue(take.done.wait(3), "test writer leaked")
            recorder.stop()
        self.assertEqual(self.namespace["_LIVE_TAKES"], {})
        self.assertEqual(self.namespace["_READ_CLAIMS"], {})
        self.temporary.cleanup()

    def wave_factory(self, **options):
        real_open = wave.open

        def open_wave(filename, mode):
            handle = real_open(filename, mode)
            if mode != "wb":
                return handle
            controlled = ControlledWave(handle, **options)
            self.handles.append(controlled)
            self.release_events.append(controlled.allow_write)
            return controlled

        return patch.object(self.namespace["wave"], "open", side_effect=open_wave)

    def feed(self, count=8, value=0.25, recorder=None):
        recorder = recorder or self.recorder
        block = np.full(count, value, dtype=np.float32)
        recorder.feed(block, -block)

    def finish(self, recorder=None):
        recorder = recorder or self.recorder
        take = recorder._take
        result = recorder.stop()
        if result and result["writer_pending"]:
            self.assertTrue(take.done.wait(3))
            result = recorder.stop()
        return result

    def pcm(self, path):
        with wave.open(str(path), "rb") as audio_file:
            return np.frombuffer(audio_file.readframes(audio_file.getnframes()), dtype="<i2")

    def test_clean_take_has_matching_audio_and_atomic_final_metadata(self):
        state = {"master": {"volume": 0.5}, "notes": [1, 2]}
        started = self.recorder.start(state)
        state["master"]["volume"] = 0.9
        self.feed(128)
        stopped = self.finish()
        path = Path(started["path"])
        self.assertTrue(stopped["complete"])
        self.assertTrue(stopped["finalized"])
        self.assertFalse(stopped["writer_pending"])
        self.assertEqual(stopped["frames"], 128)
        self.assertEqual(stopped["input_frames"], 128)
        self.assertEqual(stopped["dropped_blocks"], 0)
        self.assertEqual(stopped["status"], "complete")
        self.assertEqual(json.loads(path.with_suffix(".meta.json").read_text()), stopped)
        self.assertEqual(self.Recorder.load_state_snapshot(path.name)["master"]["volume"], 0.5)
        np.testing.assert_array_equal(self.pcm(path), np.tile(np.array([8191, -8191]), 128))
        self.assertEqual(self.Recorder.list_takes()[0]["status"], "complete")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_stalled_writer_rejects_restart_and_later_takes_have_distinct_ownership(self):
        self.namespace["STOP_TIMEOUT_SECONDS"] = 0.03
        with self.wave_factory(block_write=True):
            started = self.recorder.start({"take": 1})
            first_take = self.recorder._take
            self.feed()
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            began = time.monotonic()
            pending = self.recorder.stop()
            self.assertLess(time.monotonic() - began, 0.5)
            self.assertEqual(pending["status"], "stopping")
            self.assertFalse(pending["complete"])
            self.assertTrue(pending["writer_pending"])
            self.assertEqual(handle.close_calls, 0)
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError, "still finalizing"):
                    self.recorder.start({"take": "must not start"})
            self.assertIs(self.recorder._take, first_take)
            self.assertEqual(len(self.handles), 1)
            self.assertEqual(len(list((self.directory / "recordings").glob("*.wav"))), 1)
            self.assertTrue(self.Recorder.is_take_active(started["filename"]))
            self.assertFalse(self.Recorder.delete_take(started["filename"]))
            handle.allow_write.set()
            self.assertTrue(first_take.done.wait(2))
            finished = self.recorder.stop()
            self.assertTrue(finished["complete"])
            old_bytes = Path(started["path"]).read_bytes()
            second = self.recorder.start({"take": 2})
            self.assertIsNot(self.recorder._take.queue, first_take.queue)
            self.assertIsNot(self.recorder._take.wav, first_take.wav)
            self.handles[-1].allow_write.set()
            self.feed(value=-0.5)
            self.assertTrue(self.finish()["complete"])
            self.assertEqual(Path(started["path"]).read_bytes(), old_bytes)
            self.assertNotEqual(self.pcm(second["path"])[0], self.pcm(started["path"])[0])
            self.assertEqual(handle.close_calls, 1)

    def test_producer_copy_racing_stop_cannot_enqueue_into_finished_or_new_take(self):
        copied = threading.Event()
        release_copy = threading.Event()
        self.release_events.append(release_copy)
        block = np.ones(8, dtype=np.float32)

        class SlowCopy:
            ndim = 1
            size = 8

            def copy(self):
                copied.set()
                if not release_copy.wait(3):
                    raise TimeoutError("test producer was not released")
                return block.copy()

        first = self.recorder.start()
        old_take = self.recorder._take
        producer = threading.Thread(target=self.recorder.feed, args=(SlowCopy(), block))
        self.test_threads.append(producer)
        producer.start()
        self.assertTrue(copied.wait(1))
        self.assertEqual(self.finish()["frames"], 0)
        second = self.recorder.start()
        release_copy.set()
        producer.join(1)
        self.assertFalse(producer.is_alive())
        self.assertTrue(old_take.queue.empty())
        self.feed(4, value=0.5)
        self.assertEqual(self.finish()["frames"], 4)
        self.assertEqual(self.pcm(first["path"]).size, 0)
        self.assertEqual(self.pcm(second["path"]).size, 8)

    def test_last_enqueue_after_empty_timeout_is_drained_before_exit(self):
        timed_out = threading.Event()
        release_get = threading.Event()
        self.release_events.append(release_get)

        class TimeoutRaceQueue(queue.Queue):
            first = True

            def get(self, *args, **kwargs):
                if self.first:
                    self.first = False
                    timed_out.set()
                    if not release_get.wait(3):
                        raise TimeoutError("test queue was not released")
                    raise queue.Empty
                return super().get(*args, **kwargs)

        self.namespace["queue"] = SimpleNamespace(Queue=TimeoutRaceQueue, Empty=queue.Empty, Full=queue.Full)
        self.namespace["MAX_TAKE_SECONDS"] = 1
        self.recorder = self.Recorder(sample_rate=4)
        self.recorders.append(self.recorder)
        self.recorder.start()
        take = self.recorder._take
        self.assertTrue(timed_out.wait(1))
        self.feed(4)
        self.assertFalse(self.recorder.is_recording())
        release_get.set()
        self.assertTrue(take.done.wait(2))
        result = self.finish()
        self.assertTrue(result["complete"])
        self.assertEqual(result["frames"], 4)

    def test_queue_loss_is_counted_and_persisted_not_claimed_complete(self):
        self.namespace["MAX_QUEUE"] = 1
        with self.wave_factory(block_write=True):
            self.recorder.start()
            self.feed()
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            self.feed()
            self.feed()
            self.feed()
            handle.allow_write.set()
            result = self.finish()
        self.assertEqual(result["frames"], 16)
        self.assertEqual(result["input_frames"], 32)
        self.assertEqual(result["dropped_frames"], 16)
        self.assertEqual(result["dropped_blocks"], 2)
        self.assertFalse(result["complete"])
        self.assertTrue(result["finalized"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.Recorder.list_takes()[0]["dropped_blocks"], 2)

    def test_duration_cap_trims_last_block_without_any_helper_threads(self):
        self.namespace["MAX_TAKE_SECONDS"] = 1
        self.recorder = self.Recorder(sample_rate=10)
        self.recorders.append(self.recorder)
        real_thread = threading.Thread
        with self.wave_factory(block_write=True), patch.object(
                self.namespace["threading"], "Thread", wraps=real_thread) as constructor:
            self.recorder.start()
            take = self.recorder._take
            self.feed(6)
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            self.feed(6)
            for _ in range(100):
                self.feed(6)
            self.assertFalse(self.recorder.is_recording())
            self.assertEqual(constructor.call_count, 1)
            handle.allow_write.set()
            self.assertTrue(take.done.wait(2))
            result = self.finish()
        self.assertEqual(result["frames"], 10)
        self.assertEqual(result["input_frames"], 10)
        self.assertEqual(result["stop_reason"], "duration_limit")
        self.assertTrue(result["complete"])

    def test_duration_cap_counts_lost_input_not_only_writer_progress(self):
        self.namespace.update(MAX_TAKE_SECONDS=2, MAX_QUEUE=1)
        self.recorder = self.Recorder(sample_rate=10)
        self.recorders.append(self.recorder)
        with self.wave_factory(block_write=True):
            self.recorder.start()
            self.feed(8)
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            self.feed(8)
            self.feed(8)
            self.assertFalse(self.recorder.is_recording())
            handle.allow_write.set()
            result = self.finish()
        self.assertEqual(result["frames"], 16)
        self.assertEqual(result["input_frames"], 20)
        self.assertEqual(result["dropped_frames"], 4)
        self.assertEqual(result["stop_reason"], "duration_limit")
        self.assertFalse(result["complete"])

    def test_write_failure_stops_once_and_accounts_for_abandoned_queue(self):
        with self.wave_factory(block_write=True, write_error=True):
            self.recorder.start()
            take = self.recorder._take
            self.feed()
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            self.feed()
            self.feed()
            with self.assertLogs("isolated_recorder_lifecycle", "WARNING"):
                handle.allow_write.set()
                self.assertTrue(take.done.wait(2))
            result = self.finish()
        self.assertEqual(handle.write_calls, 1)
        self.assertEqual(handle.close_calls, 1)
        self.assertEqual(result["write_errors"], 1)
        self.assertEqual(result["dropped_blocks"], 3)
        self.assertEqual(result["dropped_frames"], 24)
        self.assertEqual(result["frames"], 0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stop_reason"], "write_error")
        self.assertIn("disk write failure", result["error"])
        self.assertFalse(result["complete"])

    def test_close_error_is_not_reported_as_finalized_or_complete(self):
        with self.wave_factory(close_error=True):
            self.recorder.start()
            self.feed()
            with self.assertLogs("isolated_recorder_lifecycle", "WARNING"):
                result = self.finish()
        self.assertEqual(result["frames"], 8)
        self.assertFalse(result["finalized"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "error")
        self.assertIn("close failure", result["error"])

    def test_final_metadata_failure_remains_incomplete_after_a_new_module_load(self):
        started = self.recorder.start()
        self.feed()
        with patch.dict(self.namespace, {"atomic_write_json": lambda *_args: (_ for _ in ()).throw(
                OSError("injected final metadata failure"))}):
            with self.assertLogs("isolated_recorder_lifecycle", "WARNING"):
                result = self.finish()
        self.assertFalse(result["metadata_saved"])
        self.assertTrue(result["finalized"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(json.loads(Path(started["path"]).with_suffix(".meta.json").read_text())["status"], "recording")
        reloaded = load_recorder(self.directory)["Recorder"].list_takes()[0]
        self.assertEqual(reloaded["status"], "incomplete")
        self.assertFalse(reloaded["complete"])

    def test_audio_sync_failure_is_reported_even_when_final_metadata_saves(self):
        self.recorder.start()
        self.feed()
        real_fsync = self.namespace["os"].fsync
        sync_calls = []

        def failed_audio_sync(fd):
            sync_calls.append(fd)
            if len(sync_calls) == 1:
                raise OSError("injected audio sync failure")
            return real_fsync(fd)

        with patch.object(self.namespace["os"], "fsync", side_effect=failed_audio_sync):
            with self.assertLogs("isolated_recorder_lifecycle", "WARNING"):
                result = self.finish()
        self.assertTrue(result["finalized"])
        self.assertTrue(result["metadata_saved"])
        self.assertFalse(result["audio_synced"])
        self.assertFalse(result["complete"])
        self.assertIn("audio sync failure", result["error"])

    def test_final_metadata_stall_keeps_file_protected_until_last_file_operation(self):
        self.namespace["STOP_TIMEOUT_SECONDS"] = 0.03
        started = self.recorder.start()
        take = self.recorder._take
        self.feed()
        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        atomic_write = self.namespace["atomic_write_json"]

        def stalled_metadata(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test metadata gate was not released")
            return atomic_write(*args)

        with patch.dict(self.namespace, {"atomic_write_json": stalled_metadata}):
            result = self.recorder.stop()
            self.assertTrue(entered.wait(1))
            self.assertTrue(result["writer_pending"])
            self.assertFalse(result["complete"])
            self.assertTrue(self.Recorder.is_take_active(started["filename"]))
            self.assertFalse(self.Recorder.delete_take(started["filename"]))
            release.set()
            self.assertTrue(take.done.wait(2))
        self.assertTrue(self.finish()["complete"])
        self.assertFalse(self.Recorder.is_take_active(started["filename"]))

    def test_delete_and_prune_skip_active_take_across_recorder_instances(self):
        old = self.recorder.start({"old": True})
        self.feed()
        self.finish()
        other = self.Recorder()
        self.recorders.append(other)
        active = other.start({"active": True})
        self.feed(recorder=other)
        self.assertFalse(self.Recorder.delete_take(active["filename"]))
        self.assertEqual(self.namespace["prune_recordings"](0), 1)
        self.assertFalse(Path(old["path"]).exists())
        self.assertTrue(Path(active["path"]).exists())
        self.assertTrue(Path(active["path"]).with_suffix(".state.json").exists())
        self.finish(other)
        self.assertTrue(self.Recorder.delete_take(active["filename"]))
        self.assertEqual(list((self.directory / "recordings").iterdir()), [])

    def test_reserved_file_is_protected_and_stop_reports_busy_initialization(self):
        self.namespace["STOP_TIMEOUT_SECONDS"] = 0.03
        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        real_open = wave.open
        result = {}

        def delayed_open(filename, mode):
            if mode == "wb":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test initialization was not released")
            return real_open(filename, mode)

        def start_take():
            try:
                result.update(self.recorder.start())
            except Exception as exc:
                result["failure"] = exc

        with patch.object(self.namespace["wave"], "open", side_effect=delayed_open):
            thread = threading.Thread(target=start_take)
            self.test_threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(1))
            path = next((self.directory / "recordings").glob("*.wav"))
            self.assertTrue(self.Recorder.is_take_active(path.name))
            self.assertFalse(self.Recorder.delete_take(path.name))
            self.assertEqual(self.Recorder.list_takes()[0]["status"], "starting")
            began = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "stop not confirmed"):
                self.recorder.stop()
            self.assertLess(time.monotonic() - began, 0.5)
            release.set()
            thread.join(1)
            self.assertNotIn("failure", result)
            self.finish()

    def test_invalid_state_is_rejected_before_stopping_or_opening_any_take(self):
        started = self.recorder.start({"preserve": True})
        files_before = set((self.directory / "recordings").iterdir())
        for invalid in ([], {"nested": [float("nan")]}, {"v": float("inf")}, {"v": object()}):
            with self.subTest(state=repr(invalid)):
                with patch.object(self.namespace["wave"], "open") as opener:
                    with self.assertRaises((TypeError, ValueError)):
                        self.recorder.start(invalid)
                opener.assert_not_called()
                self.assertTrue(self.recorder.is_recording())
                self.assertEqual(self.recorder.current_status()["filename"], started["filename"])
                self.assertEqual(set((self.directory / "recordings").iterdir()), files_before)
        self.finish()

    def test_initial_sidecar_or_thread_start_failure_removes_only_owned_new_files(self):
        old = self.recorder.start({"preserved": True})
        self.feed()
        self.finish()
        old_files = {path: path.read_bytes() for path in (self.directory / "recordings").iterdir()}
        atomic_write = self.namespace["atomic_write_json"]
        for suffix in (".state.json", ".meta.json", "thread"):
            with self.subTest(failure=suffix):
                def failed_sidecar(path, value):
                    if path.name.endswith(suffix):
                        raise OSError("injected initial sidecar failure")
                    return atomic_write(path, value)

                context = (patch.object(threading.Thread, "start", side_effect=RuntimeError("thread unavailable"))
                           if suffix == "thread" else patch.dict(self.namespace, {"atomic_write_json": failed_sidecar}))
                with context:
                    with self.assertRaises((OSError, RuntimeError)):
                        self.recorder.start({"must": "not remain"})
                self.assertFalse(self.recorder.is_recording())
                self.assertEqual(self.namespace["_LIVE_TAKES"], {})
                self.assertEqual({path: path.read_bytes() for path in (self.directory / "recordings").iterdir()}, old_files)
        self.assertTrue(Path(old["path"]).exists())

    def test_state_load_rejects_nonobjects_nonfinite_values_and_unsafe_names(self):
        started = self.recorder.start({"valid": [1, 2]})
        self.finish()
        state_path = Path(started["path"]).with_suffix(".state.json")
        for contents in ('[]', '{"x": NaN}', '{"x": [Infinity]}', '{"partial":'):
            with self.subTest(contents=contents):
                state_path.write_text(contents)
                with self.assertLogs("isolated_recorder_lifecycle", "WARNING"):
                    self.assertIsNone(self.Recorder.load_state_snapshot(started["filename"]))
        for filename in (None, "../take.wav", "x\\take.wav", "", "file.json"):
            self.assertIsNone(self.Recorder.load_state_snapshot(filename))
            self.assertFalse(self.Recorder.is_take_active(filename))
            self.assertFalse(self.Recorder.delete_take(filename))

    def test_legacy_takes_are_not_given_unearned_complete_status(self):
        path = self.directory / "recordings" / "old.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00" * 16)
        legacy = self.Recorder.list_takes()[0]
        self.assertEqual(legacy["status"], "legacy")
        self.assertIsNone(legacy["complete"])
        self.assertIsNone(legacy["finalized"])
        self.namespace["atomic_write_json"](path.with_suffix(".meta.json"), {
            "status": "complete", "complete": True, "finalized": False,
        })
        self.assertFalse(self.Recorder.list_takes()[0]["complete"])

    def test_malformed_or_nonfinite_audio_stops_with_error_without_render_exception(self):
        for left, right in ((np.ones(8), np.ones(4)), (np.array([np.nan]), np.array([0.0]))):
            with self.subTest(left=left, right=right):
                self.recorder.start()
                with patch.object(self.namespace["logger"], "warning"):
                    self.recorder.feed(left, right)
                    result = self.finish()
                self.assertFalse(result["complete"])
                self.assertEqual(result["status"], "error")
                self.assertTrue(result["error"])

    def test_library_rejects_invalid_final_metadata_and_truncated_payload(self):
        started = self.recorder.start()
        self.feed(16)
        result = self.finish()
        path = Path(started["path"])
        metadata_path = path.with_suffix(".meta.json")
        for change in ({"frames": "sixteen"}, {"dropped_frames": 1},
                       {"complete": False}, {"duration_seconds": []},
                       {"frames": 17, "input_frames": 17, "accepted_frames": 17}):
            with self.subTest(change=change):
                self.namespace["atomic_write_json"](metadata_path, dict(result, **change))
                item = self.Recorder.list_takes()[0]
                self.assertFalse(item["complete"])
                self.assertIn(item["status"], {"error", "incomplete"})
        self.namespace["atomic_write_json"](metadata_path, result)
        with path.open("r+b") as damaged:
            damaged.truncate(path.stat().st_size - 4)
        item = self.Recorder.list_takes()[0]
        self.assertFalse(item["complete"])
        self.assertEqual(item["status"], "error")
        self.assertIn("does not match", item["error"])

    def test_claimed_complete_take_survives_delete_and_prune_until_last_reader_exits(self):
        started = self.recorder.start()
        self.feed()
        self.finish()
        filename = started["filename"]
        with self.Recorder.claim_take(filename) as path:
            self.assertEqual(path, Path(started["path"]))
            self.assertFalse(self.Recorder.is_take_active(filename))
            with self.Recorder.claim_take(filename):
                self.assertEqual(self.namespace["_READ_CLAIMS"][path], 2)
                self.assertFalse(self.Recorder.delete_take(filename))
                self.assertEqual(self.namespace["prune_recordings"](0), 0)
            self.assertFalse(self.Recorder.delete_take(filename))
            self.assertTrue(path.exists())
        self.assertTrue(self.Recorder.delete_take(filename))

    def test_claim_rejects_active_finalizing_incomplete_and_damaged_takes(self):
        self.namespace["STOP_TIMEOUT_SECONDS"] = 0.03
        with self.wave_factory(block_write=True):
            started = self.recorder.start()
            filename = started["filename"]
            with self.assertRaisesRegex(RuntimeError, "wait for finalization"):
                with self.Recorder.claim_take(filename):
                    self.fail("active take was claimed")
            self.feed()
            handle = self.handles[-1]
            self.assertTrue(handle.write_entered.wait(1))
            self.assertTrue(self.recorder.stop()["writer_pending"])
            with self.assertRaises(RuntimeError):
                with self.Recorder.claim_take(filename):
                    self.fail("finalizing take was claimed")
            handle.allow_write.set()
            result = self.finish()
        metadata_path = Path(started["path"]).with_suffix(".meta.json")
        for change in ({"complete": False, "status": "incomplete", "dropped_frames": 8, "dropped_blocks": 1},
                       {"complete": False, "status": "recording"}):
            with self.subTest(change=change):
                self.namespace["atomic_write_json"](metadata_path, dict(result, **change))
                with self.assertRaisesRegex(ValueError, "not a verified complete take"):
                    with self.Recorder.claim_take(filename):
                        self.fail("incomplete take was claimed")
                self.assertEqual(self.namespace["_READ_CLAIMS"], {})
        self.namespace["atomic_write_json"](metadata_path, result)
        with Path(started["path"]).open("r+b") as damaged:
            damaged.truncate(44)
        with self.assertRaisesRegex(ValueError, "does not match"):
            with self.Recorder.claim_take(filename):
                self.fail("damaged take was claimed")

    def test_legacy_claim_is_allowed_and_exception_releases_ownership(self):
        started = self.recorder.start()
        self.feed()
        self.finish()
        filename = started["filename"]
        Path(started["path"]).with_suffix(".meta.json").unlink()
        with self.assertRaisesRegex(OSError, "injected copy failure"):
            with self.Recorder.claim_take(filename) as path:
                self.assertTrue(path.exists())
                raise OSError("injected copy failure")
        self.assertEqual(self.namespace["_READ_CLAIMS"], {})
        self.assertTrue(self.Recorder.delete_take(filename))


if __name__ == "__main__":
    unittest.main()
