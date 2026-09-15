#!/usr/bin/env python3
"""Run ONLY the reviewed, device-free Pi4 regression suite.

Never discover tests/test_*.py: legacy stress scripts can control a live synth.
Requires existing NumPy, websockets, Node and a C compiler; installs nothing.
The listener tests bind ephemeral loopback ports, not stage ports. Native DSP
and JACK are mocked; no soundfont, device, Pi or running service is accessed.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAFE_TESTS = (
    "test_runtime.py",
    "test_state_store.py",
    "test_instance_integration.py",
    "test_native_bridge_identity.py",
    "test_midi_notes.py",
    "test_native_controls.py",
    "test_websocket_lifecycle.py",
    "test_pad_and_recording.py",
    "test_control_guards.py",
    "test_application_lifecycle.py",
    "test_state_schema.py",
    "test_control_mapping.py",
    "test_control_transactions.py",
    "test_native_bridge_contract.py",
    "test_jack_bridge_contract.py",
    "test_native_lifecycle.py",
    "test_recorder_lifecycle.py",
    "test_pad_preparation.py",
    "test_native_allocation_guards.py",
    "test_ui_recovery.py",
    "test_pad_transactions.py",
    "test_native_profile.py",
    "test_retired_stress_guards.py",
    "test_routing.py",
    "test_organ_zero_mute.py",
    "test_service_contract.py",
    "test_stage_candidate_unit.py",
    "test_stage_preflight.py",
    "test_render_metrics.py",
    "test_render_metrics_integration.py",
    "test_render_pointer_cycles.py",
    "test_native_audition.py",
    "test_audition_plan.py",
    "test_analyze_audition.py",
    "test_piano_event_ownership.py",
    "test_native_render_preparation.py",
    "test_render_diagnostics.py",
    "test_piano_lock_diagnostics.py",
    "test_reverb_diagnostics.py",
    "test_drone_fade.py",
    "test_feature_probe.py",
    "test_osc_modulation_identity.py",
    "test_organ_leslie_stop.py",
    "test_synth_diagnostics.py",
    "test_organ_stop_check.py",
    "test_performance_probe.py",
    "test_native_soak.py",
    "test_core_soak.py",
    "test_soundfont_realtime_loading.py",
    "test_control_diagnostics.py",
    "test_piano_program_owner.py",
    "test_organ_variants.py",
)


def main() -> int:
    for command in ("node", "cc", "bash", "git"):
        if shutil.which(command) is None:
            print(f"Required offline-test tool is missing: {command}", file=sys.stderr)
            return 2

    # Parse source without importing app modules or running historical probes.
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode().split("\0")
    python_files = sorted({name for name in tracked if name.endswith(".py")})
    for name in python_files:
        ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)
    print(f"Parsed {len(python_files)} Python files (no application imports).", flush=True)
    for name in ("stave-synth.sh", "install.sh", "faust/build.sh"):
        subprocess.run(["bash", "-n", str(ROOT / name)], check=True, cwd=ROOT)
    subprocess.run(["node", "--check", str(ROOT / "ui/script.js")], check=True, cwd=ROOT)

    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for name in SAFE_TESTS:
        if not (ROOT / "tests" / name).is_file():
            raise FileNotFoundError(f"Required regression module is absent: {name}")
        suite.addTests(loader.discover(str(ROOT / "tests"), pattern=name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        print("Offline qualification failed or required tests were skipped.", file=sys.stderr)
        return 1

    subprocess.run(["node", str(ROOT / "tests/test_ui_connection.js")], check=True, cwd=ROOT)
    print("Offline regression suite passed. This is NOT Pi4 hardware/audio qualification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
