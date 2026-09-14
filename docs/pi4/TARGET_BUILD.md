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

Code commit `4b70e4634fcc2ba32f1fc5490e3551b2722a405b` was published to the
Pi4 branch and fast-forwarded into the separate target checkout. The bridge
was rebuilt successfully into a unique temporary build directory, then moved
to that checkout only, with GCC flags:

```text
-std=gnu11 -shared -fPIC -O2 -Wl,-z,defs -ljack -lpthread -lm
```

Observed target toolchain: Linux aarch64, GCC 14.2.0 (Debian 14.2.0-19),
Faust 2.79.3, Python 3.13.5, NumPy 2.2.4, CFFI 2.1.0 and SciPy 1.15.3.
All 16 libraries were identified as ELF64 ARM aarch64. The bridge's `ldd`
listing contained no missing dependency.

Both isolated runs of `tools/check_native_build.py` **passed**:

| Environment | Observed active profile | Result |
| --- | --- | --- |
| `STAVE_LOW_RAM=0` | 24 oscillator/sympathetic slots | Bridge ABI, wrapper shape and finite output passed |
| `STAVE_LOW_RAM=1` | 12 oscillator/sympathetic slots | Bridge ABI, wrapper shape and finite output passed |

These profile numbers are compiled voice-bank capacities, not a measured
musical-polyphony guarantee. Checks used instance `nativecheck`, root
`/tmp/stave-nativecheck`, HTTP 18080 and WS 18765; no listener was started.

The tool only loads the bridge for ABI symbol lookup and computes one
128-frame block through each Faust wrapper. It never calls `bridge_start`,
starts JACK, opens a listener/device, loads the application or mutates stage
state. The required profile counts are 24 and 12 slots respectively. Missing
artifacts on Linux are failures; a macOS artifact skip is not target evidence.

Use the existing Pi virtual-environment interpreter read-only; do not install
dependencies into the live environment. Set `PYTHONDONTWRITEBYTECODE=1` and
keep the working directory in the separate development checkout.

### Target Python regressions and saved-state compatibility

At `26b9a7f2d0ae02c06d1c0ba02bfbc062d088571c`, all **257 Python tests passed
on the Pi4 with zero skips** in 24.923 seconds. Only the exact `SAFE_TESTS`
allowlist from `tools/run_offline_tests.py` was loaded, with bytecode writes
disabled and reduced process priority. These tests use temporary data, fake
JACK/native objects or mock-JACK C executables, and ephemeral loopback
listeners—not the production service or physical devices.

The first Pi run exposed one test-only desktop assumption: it expected the
generic unison default of one even though the documented low-memory profile
pins it to three. The expectation now explicitly accounts for that profile;
no application/sound change was made to pass it. The full suite was rerun,
not just the failing assertion. The Mac also passed all 257 Python tests plus
the Node UI suite after this correction. Node was not present in the Pi shell,
so the browser tests were run on the Mac, not claimed as Pi/Safari tests.

The live `current_state.json` also passed a read-only schema validation on the
Pi (piano mode, Fluid soundfont, unison three). No save/migration was written
back to production. After the tests the live service remained active with
MainPID 1106 and NRestarts 0.

### Artifact SHA-256 manifest

Paths are relative to the Pi4 development checkout. Generated binaries remain
ignored build artifacts; these hashes identify the tested outputs.

```text
07b5e389e98dff3c26166ac5b2f0d90d63ebd51af80ca96f9576fa51f32a2471  stave_synth/jack_bridge.so
4c95e5f4937c9d336ef17cc3736039e47bc89eeda660474140681636ebde5573  faust/libstave_bus_comp.so
b14d73be7800c9aeec680bf6f4d65a801c5928af0c162840028a04e00f95ebcd  faust/libstave_drone.so
ac6e8a3dec55f7d3dea3151c4ab62a6033ad207807769812426bd0dfb3c6b46c  faust/libstave_master_fx.so
958fb0a0b63c651af3af6b2d6e37df446ea209af519af20271aac03f21734d11  faust/libstave_merged_shim.so
330bf3f9b3f19af7051e2c644e3d5f640e6a428c410c2d463270c43e8ab65c82  faust/libstave_organ.so
284c7122e9f8093822b1a0a59d6367d67d746c8a84991059547a7c5c1a958d0c  faust/libstave_osc_bank.so
a7d352a32c96e802e5ce3b3bbe2b111396ecabe1088e35e4633fc98833d6b46a  faust/libstave_osc_bank_lite.so
fe6f4a0e69e57c8b6ae5479f02c74c078461ebebdd62ce72eda3c5006e210a0c  faust/libstave_pad_bus.so
e84ffc0f45aaf67e27b01b6a8c9105c882627edca6c3a26b68185b30faf264d0  faust/libstave_piano_chain.so
8711c873f715b962585bb057611d4cf521e7b4de4936a581f88d7aed8c2b299c  faust/libstave_piano_room.so
beae7ba2cb9b63203ac1e0344357b9c1b9c0d0dbe1997ddb9e87cf8beb92e65f  faust/libstave_ping_pong.so
cc1a8bffece826f26cf338aa5ddb4ff4f71f79e64e7d1ed461c754f3bedf30bd  faust/libstave_plate.so
07e4dcbbe6abaf7c50fd8c2b2d1dc08a9740c9f7cf75095bfd1f96285abd906d  faust/libstave_reverb.so
9501ce31a171602edb04fdf04bb81d3e633b048ab99899c26548751640c03dbd  faust/libstave_sympathetic.so
62b1ec190a9a0868a54310e2e9230512d9dde8fcf17e8f9bd9ea099e5b6def21  faust/libstave_sympathetic_lite.so
```

## Still unproven

Finite smoke output is not proof of musical quality, real-time performance,
proper soundfont availability in an installed profile, physical MIDI/audio
routing, controller-to-analogue latency, render-period reserve, or recovery
after OS/USB failure. Those require the off-stage hardware process in
[ENGINEERING_VALIDATION.md](ENGINEERING_VALIDATION.md).

The preserved runtime archive is not an SD image and does not contain the
target bytes of external soundfont symlinks. Complete required asset backups
and rehearse rollback before a real deployment; see [BASELINE.md](BASELINE.md).
