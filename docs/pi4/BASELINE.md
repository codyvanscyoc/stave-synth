# Pi4 baseline — 2026-09-14

Baseline commit: `d0142f0b811baa4ba4fa216d55c6d5e4b0ac7dbd`.

Baseline tag: `pi4-stage-baseline-2026-09-14`.

Development branch: `pi4-stage-pro`.

This is the preserved, previously used Pi4 build, not a newly qualified release. The tag contains the Pi4 history that was 25 commits ahead of the original remote `main` at inspection. Do not merge or push this work onto `main` or `mac-port` as part of the Pi4 effort. Share improvements with other hardware lines only through a later deliberate review.

The original stage checkout and running service remain the baseline. A separate Pi4 development worktree exists alongside it; the development worktree is not a separately running synth. The Mac development clone also uses `pi4-stage-pro`.

## Preserved artifacts

Two artifacts are stored in the user's `stave-synth-pi4-backups` directory on the Pi and in `Documents/stave-synth-pi4-backups` on the Mac:

| Artifact | Contains | SHA-256 |
| --- | --- | --- |
| `pi4-stage-baseline-2026-09-14.bundle` | Complete history reachable from the Pi4 branch and baseline tag | `ab6c1650f835553ce6b05fc90be1347cfc0a1de38f4e3deae3236ee048ad3da5` |
| `pi4-runtime-baseline-2026-09-14.tar` | Stage source, native `.so` builds, current app settings/data, user service/drop-ins and user PipeWire configuration | `25c0e5a885b4ab7cf6da6d5529972f4e3e73f88b679b4da027080bfce9f83c07` |

Both transferred hashes match the Pi originals. Git bundle verification reports complete history. The runtime archive excludes `.git`, the Python virtual environment, and Python bytecode caches. It preserves soundfont symlinks, not the externally installed `/usr/share/sounds/sf2/FluidR3_GM.sf2` contents. It also does not include system packages, the full boot/system network configuration, or an SD-card image.

These artifacts preserve source history and the app/runtime configuration available during inspection. They do not constitute a tested whole-device restore. Keep private runtime archives out of GitHub. A complete system/dependency/sound-asset manifest and a recovery image are later release tasks.

## Observed environment

- Raspberry Pi 4 Model B Rev 1.5, approximately 2 GB RAM; headless.
- Linux `6.18.34+rpt-rpi-v8`, aarch64; Python `3.13.5`.
- Relevant installed Python package versions: NumPy `2.2.4`, SciPy `1.15.3`, CFFI `2.1.0`, pyFluidSynth `1.4.0`, websockets `16.1.1`, psutil `7.2.2`.
- Stage process started 2026-08-27; service still active with zero systemd restarts during the 2026-09-14 inspection.
- PipeWire-JACK at 48 kHz, reported 512-frame blocks; saved low-latency mode enabled and unison set to 3. The current small-RAM code profile constrains other resources independently.
- Native Faust modules were enabled in service configuration; compiled assets and source predated process startup.
- CPU governor reported performance, render scheduling was realtime, no throttling was reported. RT allowance was 95 and memlock unlimited.
- Memory controller disabled in the kernel command line; service memory accounting unavailable despite configured memory limits.
- Saved output preference named a Yamaha output. No physical USB audio/MIDI device was enumerated at the earlier inspection; PipeWire exposed Dummy Output.
- USB descriptor/address errors were recorded on 2026-09-14. The cause within the physical USB path has not been isolated.
- Pad sample and recording directories were empty in the captured app data; current-state JSON was present. Favorite stage presets/performances may need to be collected from the player or another device.

The source and runtime were inspected and basic syntax checks passed. No audio certification, physical latency measurement, fault injection, or stage soak test has been completed in this professionalization phase.

## Restoration discipline

Inspect/extract backups into a new staging directory first. Validate state JSON and external sound assets, rebuild/install the documented dependencies on the target architecture, and verify correct service paths before changing a running service. Test whole-device restoration on spare media. Do not overwrite the live checkout, current settings, or Pi5/Mac branches as a shortcut.

See [product vision](PRODUCT_VISION.md) and [engineering validation](ENGINEERING_VALIDATION.md) for intended behavior and release gates.
