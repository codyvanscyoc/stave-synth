# Pi4 development native-build evidence

2026-09-14. This is build/load evidence, **not deployment or stage qualification**.

## Protected runtime

The running instrument remains in `/home/codyvanscyoc/stave-synth` at the
preserved `d0142f0` baseline. During the repair/build work,
`stave-synth.service` remained active with MainPID 1106 and NRestarts 0.
No running-service restart, live configuration replacement, physical route
change, generic installer, reboot or hotplug test was performed.

Development uses `/home/codyvanscyoc/stave-synth-pi4-stage`, branch
`pi4-stage-pro`. A checkout is not CPU/JACK/USB isolation; no second complete
instrument or stress load is started by the checks below.

## Faust generation/build

On the actual Raspberry Pi4, the development checkout at `762a259` successfully
ran `nice -n 19 bash faust/build.sh --force`. All 15 Faust shared libraries were
built locally, including full/lite oscillator and sympathetic banks and the
merged shim. Faust DSP sources are unchanged by the second repair batch.
Native flags use the target CPU (`-mcpu=native`), not copied Pi5 binaries.
The build uses double precision for the pad bus and piano chain.

## Second-batch bridge and load checks

Pending the code-publication checkpoint: rebuild the changed C bridge in the
development checkout, check AArch64/dependency identity, then run
`tools/check_native_build.py` for both `STAVE_LOW_RAM=0` and `1` in explicit
isolated runtimes. Record actual results here after execution.

The tool only loads the bridge for ABI symbol lookup and computes one
128-frame block through each Faust wrapper. It never calls `bridge_start`,
starts JACK, opens a listener/device, loads the application or mutates stage
state. The required profile counts are 24 and 12 slots respectively. Missing
artifacts on Linux are failures; a macOS artifact skip is not target evidence.

Use the existing Pi virtual-environment interpreter read-only; do not install
dependencies into the live environment. Set `PYTHONDONTWRITEBYTECODE=1` and
keep the working directory in the separate development checkout.

## Still unproven

Finite smoke output is not proof of musical quality, real-time performance,
proper soundfont availability in an installed profile, physical MIDI/audio
routing, controller-to-analogue latency, render-period reserve, or recovery
after OS/USB failure. Those require the off-stage hardware process in
[ENGINEERING_VALIDATION.md](ENGINEERING_VALIDATION.md).

The preserved runtime archive is not an SD image and does not contain the
target bytes of external soundfont symlinks. Complete required asset backups
and rehearse rollback before a real deployment; see [BASELINE.md](BASELINE.md).
