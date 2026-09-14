#!/usr/bin/env python3
"""Load and smoke-test native audio artifacts without starting JACK or the app.

Run this once per desired memory profile in an explicitly isolated runtime.
It installs nothing, opens no ports or devices, and performs one 128-frame
compute through each Faust wrapper. Missing artifacts are tolerated on macOS
development hosts; they are failures on target Linux systems.
"""

from __future__ import annotations

import ctypes
import platform
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAUST_LIBRARIES = (
    "libstave_reverb.so",
    "libstave_ping_pong.so",
    "libstave_osc_bank.so",
    "libstave_sympathetic.so",
    "libstave_master_fx.so",
    "libstave_bus_comp.so",
    "libstave_organ.so",
    "libstave_plate.so",
    "libstave_drone.so",
    "libstave_piano_room.so",
    "libstave_pad_bus.so",
    "libstave_piano_chain.so",
    "libstave_osc_bank_lite.so",
    "libstave_sympathetic_lite.so",
    "libstave_merged_shim.so",
)

BRIDGE_SYMBOLS = (
    "bridge_start",
    "bridge_start_named",
    "bridge_stop",
    "bridge_write_stereo",
    "bridge_set_master_volume",
    "bridge_read_midi",
    "bridge_get_sample_rate",
    "bridge_get_buffer_size",
    "bridge_get_callback_count",
    "bridge_get_peak_output",
    "bridge_get_xrun_count",
    "bridge_get_underrun_count",
    "bridge_get_midi_event_count",
    "bridge_get_midi_drop_count",
    "bridge_get_midi_recovery_count",
    "bridge_get_ring_fill",
    "bridge_set_btl_mode",
    "bridge_set_ring_slots",
    "bridge_get_ring_slots",
    "bridge_clear_ring",
    "bridge_is_shutdown",
    "bridge_get_graph_error",
    "bridge_get_btl_mode",
)


def _check_finite(name: str, value, shape: tuple[int, ...]) -> None:
    output = np.asarray(value)
    if output.shape != shape:
        raise RuntimeError(f"{name}: shape {output.shape}, expected {shape}")
    if not np.isfinite(output).all():
        raise RuntimeError(f"{name}: non-finite output")


def _check_bridge(path: Path) -> None:
    bridge = ctypes.CDLL(str(path))
    missing = [name for name in BRIDGE_SYMBOLS if not hasattr(bridge, name)]
    if missing:
        raise RuntimeError(f"{path.name}: missing ABI symbols: {', '.join(missing)}")


def _smoke_faust() -> int:
    # Imports are intentionally local: isolation and artifact inventory are
    # checked before any native library is loaded.
    from stave_synth.config import LOW_RAM_MODE
    from stave_synth.faust_bus_comp import FaustBusComp
    from stave_synth.faust_drone import FaustDrone
    from stave_synth.faust_master_fx import FaustMasterFX
    from stave_synth.faust_merged import FaustMergedShim
    from stave_synth.faust_organ import FaustOrganEngine
    from stave_synth.faust_osc_bank import FaustOscBank, NVOICES
    from stave_synth.faust_pad_bus import FaustPadBus
    from stave_synth.faust_piano_chain import FaustPianoChain
    from stave_synth.faust_piano_room import FaustPianoRoom
    from stave_synth.faust_ping_pong import FaustPingPong
    from stave_synth.faust_plate import FaustPlate
    from stave_synth.faust_reverb import FaustReverb
    from stave_synth.faust_sympathetic import FaustSympathetic, N_SLOTS

    frames = 128
    stereo = np.zeros((2, frames), dtype=np.float64)
    stereo[:, 0] = 0.01

    _check_finite("reverb", FaustReverb().process(stereo.copy()), (2, frames))
    _check_finite("plate", FaustPlate().process(stereo.copy()), (2, frames))
    _check_finite("drone", FaustDrone().process(stereo.copy()), (2, frames))

    left, right = stereo[0].copy(), stereo[1].copy()
    FaustPingPong().process_inplace(left, right)
    _check_finite("ping_pong", np.stack((left, right)), (2, frames))

    bank = FaustOscBank()
    _check_finite("osc_bank", bank.process(frames), (5, frames))
    _check_finite("sympathetic", FaustSympathetic().process(frames), (2, frames))

    value = stereo.copy()
    FaustMasterFX().process_inplace(value)
    _check_finite("master_fx", value, (2, frames))
    value = stereo.copy()
    FaustBusComp().process_inplace(value)
    _check_finite("bus_comp", value, (2, frames))

    organ = FaustOrganEngine()
    organ.update_params({"enabled": True})
    organ.note_on(60, 0.5)
    _check_finite("organ", organ.render_block(frames), (2, frames))
    organ.hard_panic()
    organ.render_block(frames)

    _check_finite(
        "piano_room", FaustPianoRoom().process(stereo.copy()), (2, frames)
    )

    pad = FaustPadBus()
    pad_input = np.zeros((5, frames), dtype=np.float64)
    pad_input[:, 0] = 0.01
    _check_finite("pad_bus", pad.process(pad_input), (9, frames))

    chain = FaustPianoChain()
    chain.in_buffer(frames)[:] = stereo
    chain.set_block_params(
        gain=1.0,
        lowcut_hz=40.0,
        highcut_hz=18000.0,
        eq_bands=[
            {"freq_hz": 1000.0, "gain_db": 0.0, "q": 1.0, "enabled": False}
            for _ in range(4)
        ],
    )
    _check_finite("piano_chain", chain.process_in_place(frames), (3, frames))

    merged = FaustMergedShim(bank, pad)
    merged_bus, merged_osc = merged.process(frames)
    _check_finite("merged_bus", merged_bus, (9, frames))
    _check_finite("merged_osc", merged_osc, (5, frames))

    expected_slots = 12 if LOW_RAM_MODE else 24
    if NVOICES != N_SLOTS or NVOICES != expected_slots:
        raise RuntimeError(
            "profile mismatch: "
            f"LOW_RAM_MODE={LOW_RAM_MODE}, expected {expected_slots} slots, "
            f"osc bank has {NVOICES}, sympathetic has {N_SLOTS}"
        )
    return NVOICES


def main() -> int:
    from stave_synth.runtime import load_runtime

    runtime = load_runtime()
    if not runtime.isolated:
        raise RuntimeError(
            "refusing native build check without an explicit isolated STAVE_INSTANCE"
        )

    system = platform.system()
    machine = platform.machine() or "unknown"
    print(f"Platform: {system} {machine}; isolated instance: {runtime.instance}")

    paths = [ROOT / "faust" / name for name in FAUST_LIBRARIES]
    bridge_path = ROOT / "stave_synth" / "jack_bridge.so"
    missing = [path for path in (*paths, bridge_path) if not path.is_file()]
    if missing and system == "Darwin":
        print("SKIP: native target artifacts are not present on this macOS host")
        for path in missing:
            print(f"  missing: {path.relative_to(ROOT)}")
        return 0
    if missing:
        raise RuntimeError(
            "missing native artifacts: "
            + ", ".join(str(path.relative_to(ROOT)) for path in missing)
        )

    _check_bridge(bridge_path)  # dlopen + symbol lookup only; never bridge_start.
    slots = _smoke_faust()
    print(f"PASS: bridge ABI and 15 Faust artifacts; active slot profile={slots}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
