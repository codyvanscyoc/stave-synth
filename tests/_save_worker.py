"""Child process for test_1: imports the real save_state and writes under SIGKILL pressure."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stave_synth import config as cfg  # noqa: E402

target = Path(os.environ["STAVE_TEST_TARGET"])
iter_id = int(os.environ["STAVE_TEST_SEED"])

cfg.STATE_FILE = target
cfg.ensure_dirs = lambda: None

payload = {
    "iter": iter_id,
    "pad": {f"k{i}": i * 0.123 for i in range(500)},
    "piano": {f"k{i}": "x" * 20 for i in range(500)},
    "blob": "y" * 40000,
}

# Do one successful save so the target file exists with a known-good state.
cfg.save_state(payload)

# Signal readiness to parent, then hammer saves so the kill lands mid-write.
sys.stdout.write("READY\n")
sys.stdout.flush()

while True:
    cfg.save_state(payload)
