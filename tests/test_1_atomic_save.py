"""
Test 1 — state file atomic-save torture.

Hypothesis: after ANY kill (-9 mid-write), the target file is either
  (a) still the previous valid JSON, or
  (b) absent on the very first iteration, or
  (c) valid JSON matching a state we wrote earlier.

It must NEVER be corrupt / half-written.
"""
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "_save_worker.py"
ITERATIONS = 500


def run():
    tmpdir = Path(tempfile.mkdtemp(prefix="stave-atomic-"))
    target = tmpdir / "state.json"
    failures = []
    saw_final_rename = 0
    saw_pre_rename = 0
    saw_missing = 0
    leftover_tmps = 0

    for i in range(ITERATIONS):
        env = os.environ.copy()
        env["STAVE_TEST_TARGET"] = str(target)
        env["STAVE_TEST_SEED"] = str(i)
        proc = subprocess.Popen(
            [sys.executable, str(CHILD)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Wait for READY (first save committed) before the kill window.
        line = proc.stdout.readline()
        if line.strip() != "READY":
            failures.append((i, "child didn't become ready", line))
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            continue
        # Kill at a random microsecond window so we hit pre/mid/post-rename.
        delay = random.uniform(0.0, 0.010)
        time.sleep(delay)
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        # Check state.
        if not target.exists():
            saw_missing += 1
        else:
            try:
                with open(target) as f:
                    data = json.load(f)
                if not isinstance(data, dict) or "iter" not in data:
                    failures.append((i, "bad shape", data))
                else:
                    # iter stamp came from a prior successful save (<=i).
                    if data["iter"] > i:
                        failures.append((i, "future iter", data["iter"]))
                    elif data["iter"] == i:
                        saw_final_rename += 1
                    else:
                        saw_pre_rename += 1
            except json.JSONDecodeError as e:
                failures.append((i, "corrupt json", str(e)))

        # Check for orphan .tmp files — not a correctness failure, just a leak note.
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        if tmp_path.exists():
            leftover_tmps += 1

    print(f"Iterations:          {ITERATIONS}")
    print(f"Killed post-rename:  {saw_final_rename}")
    print(f"Killed pre-rename:   {saw_pre_rename}")
    print(f"File absent:         {saw_missing}")
    print(f"Orphan .tmp files:   {leftover_tmps} (not a correctness issue)")
    print(f"FAILURES:            {len(failures)}")
    for f in failures[:10]:
        print("  ", f)

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(run())
