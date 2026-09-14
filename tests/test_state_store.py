"""Isolated tests for atomic JSON persistence."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from stave_synth import state_store


def _process_writer(path: str, writer: int, iterations: int) -> None:
    for sequence in range(iterations):
        state_store.atomic_write_json(
            path,
            {
                "writer": writer,
                "sequence": sequence,
                "payload": [writer] * (writer + 17),
            },
        )


class AtomicWriteJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="stave-state-store-")
        self.directory = Path(self.tempdir.name)
        self.target = self.directory / "state.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def assert_no_owned_temps(self) -> None:
        self.assertEqual([], list(self.directory.glob(f".{self.target.name}.*.tmp")))

    def test_thread_writers_leave_one_complete_snapshot(self) -> None:
        expected = []
        stop_reader = threading.Event()

        def writer(writer_id: int) -> None:
            for sequence in range(40):
                payload = {
                    "writer": writer_id,
                    "sequence": sequence,
                    "payload": [writer_id] * (writer_id + 23),
                }
                expected.append(payload)
                state_store.atomic_write_json(self.target, payload)

        def reader() -> None:
            while not stop_reader.is_set():
                try:
                    with self.target.open(encoding="utf-8") as saved:
                        payload = json.load(saved)
                except FileNotFoundError:
                    continue  # Valid before the first writer's replace.
                self.assertIsInstance(payload, dict)
                self.assertIn(payload.get("writer"), range(6))
                self.assertIn(payload.get("sequence"), range(40))
                self.assertEqual(
                    [payload["writer"]] * (payload["writer"] + 23),
                    payload.get("payload"),
                )

        with ThreadPoolExecutor(max_workers=7) as executor:
            reader_future = executor.submit(reader)
            writer_futures = [executor.submit(writer, i) for i in range(6)]
            try:
                # result() propagates assertion/I/O failures from worker threads.
                for future in writer_futures:
                    future.result(timeout=15)
            finally:
                stop_reader.set()
            reader_future.result(timeout=5)

        with self.target.open(encoding="utf-8") as saved:
            self.assertIn(json.load(saved), expected)
        self.assert_no_owned_temps()

    def test_process_writers_leave_one_complete_snapshot(self) -> None:
        processes = [
            multiprocessing.Process(target=_process_writer, args=(str(self.target), i, 30))
            for i in range(4)
        ]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(15)
                self.assertEqual(0, process.exitcode)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)

        with self.target.open(encoding="utf-8") as saved:
            payload = json.load(saved)
        self.assertIn(payload["writer"], range(4))
        self.assertIn(payload["sequence"], range(30))
        self.assertEqual(
            [payload["writer"]] * (payload["writer"] + 17), payload["payload"]
        )
        self.assert_no_owned_temps()

    def test_write_failure_preserves_old_file_and_cleans_temp(self) -> None:
        old = {"generation": "old"}
        state_store.atomic_write_json(self.target, old)
        real_write = os.write
        calls = 0

        def fail_after_partial_write(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[: max(1, len(data) // 2)])
            raise OSError("injected write failure")

        with mock.patch.object(state_store.os, "write", side_effect=fail_after_partial_write):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                state_store.atomic_write_json(self.target, {"generation": "new", "data": [1] * 100})

        self.assertEqual(old, json.loads(self.target.read_text(encoding="utf-8")))
        self.assert_no_owned_temps()

    def test_replace_failure_preserves_old_file_and_cleans_temp(self) -> None:
        old = {"generation": "old"}
        state_store.atomic_write_json(self.target, old)
        with mock.patch.object(state_store.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                state_store.atomic_write_json(self.target, {"generation": "new"})

        self.assertEqual(old, json.loads(self.target.read_text(encoding="utf-8")))
        self.assert_no_owned_temps()

    def test_invalid_json_is_rejected_before_touching_files(self) -> None:
        old = {"generation": "old"}
        state_store.atomic_write_json(self.target, old)

        for invalid in ({"value": float("nan")}, {"value": float("inf")}, {"value": object()}):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    state_store.atomic_write_json(self.target, invalid)
                self.assertEqual(old, json.loads(self.target.read_text(encoding="utf-8")))
                self.assert_no_owned_temps()

    def test_permissions_are_private_for_new_file_and_preserved_afterward(self) -> None:
        state_store.atomic_write_json(self.target, {"generation": 1})
        self.assertEqual(0o600, stat.S_IMODE(self.target.stat().st_mode))

        os.chmod(self.target, 0o640)
        state_store.atomic_write_json(self.target, {"generation": 2})
        self.assertEqual(0o640, stat.S_IMODE(self.target.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
