# Native-v2 final PCM output and divergence replay — September 17, 2026

## Implemented

StageInstrument now renders through `StageOutput` to final float32 stereo PCM,
not only internal double-precision master buses. The output owner preserves
the original successful-block bridge math:

- Double-to-float conversion, with a pre-volume/pre-BTL recording tap.
- Initial gain 0.85 and the original per-sample float gain smoother
  (`1.0f - 0.99584f`), target volume 0..1.
- Original final ±1 sample clamp and pre-BTL peak-meter value.
- Explicit legacy BTL mode: mono sum followed by inverted right channel.
  Normal stereo remains default. This is not generic automatic mono/device
  selection or a new recommendation for a stage interface.

`pcm(c)` exposes final PCM; `recording_tap(c)` exposes the pre-volume tap.
Existing `channel(c)` and ten `stem(c)` diagnostics retain their old pre-output
meaning. Buffers are borrowed owner storage, overwritten next block. A future
recorder/driver must transfer them with bounded lifetime/ownership semantics.
The tap is not a completed disk recorder or file-writing implementation.

This is the original bridge's successful-audio-block processing, **not its
JACK callback, ring, underrun handling, atomic control transfer, routing or
driver**. No hardware gain or DAC was touched. These remain offline Mac tests;
the working Pi/v1.2 is unchanged and no native successor is deployed.

Configuration is validated before mutation. Invalid pointers/aliases are
refused; nonfinite or float-overflow input faults and silences the whole block
until reconstruction. That terminal numeric-fault policy deliberately differs
from the old bridge's per-sample NaN repair. STOP is terminal and silences both
PCM and recording tap. BTL switching is preserved, not click-free qualified.

## Verification

The output oracle extracts and compiles the original gain loop from pinned
`57bb94cdbd3c9349fc2a47833d659451ebb8738b`, without JACK headers, callbacks or
application imports. Native and reference use float operations with contraction
disabled. No changed formula is used as the reference.

| Check | Result |
| --- | --- |
| Output component, 1,000 blocks at each 512/256 cadence | Exact PCM, recorder tap, smoothed volume and peak meter; changing volume/BTL, silence and clipping inputs |
| Component UBSan and scoped allocation guard | 2,000 blocks; zero C++ new/new[]; pointer/config/numeric fault/STOP checks pass |
| Connected instrument, eight original/overload/diagnostic runs | Output and recorder tap exactly match original gain loop when fed the candidate master signal |
| Connected C++ UBSan/allocation guard | 1,400 blocks including output volume/BTL, recorder tap and coupled STOP; pass |
| Reference reverb replay using native reverb input | Exact wet output in all eight runs |
| Reviewed legacy suite | 549 Python tests, zero skips, Node checks pass; 130 Python files parsed |

The first legacy run was blocked by sandbox loopback-bind permission; the
approved rerun above passed. UBSan excludes generated Faust C and FluidSynth.
Allocation guards intercept C++ new/new[], not dependency malloc or locks.
Neither compiler tests nor this output math establish live deadlines, actual
latency, hardware safety qualification or supported live 256.

## Overload discrepancy: downstream mechanism now demonstrated

The previous connected-instrument checkpoint's strict 1e-6 comparison still
fails the high-gain piano fixture; **the threshold was not widened and no
sound-affecting DSP change was made to hide it**. The original source fixture
still passes, and the new output stage does not fix or worsen the upstream
master difference. Independent final PCM peak differences with this fixture's
volume automation are 8.382e-7 at 512 and 6.080e-6 at 256.

New read-only, test-only tracing captures post-delay, reverb input and raw wet
output. At the first differing reverb input float:

| Cadence | Frame / channel | Native input float | Reference input float |
| --- | --- | --- | --- |
| 512 | 19,438 / R | 6.253156243474223e-6 | 6.253156698221574e-6 |
| 256 | 19,427 / R | 3.2970649044727907e-5 | 3.2970652682706714e-5 |

Each pair is adjacent float32 values. Underlying double inputs differ only
~1.47e-13 / ~3.18e-13. Subsequent raw wet differences reach 1.617e-6 / 7.742e-6
before wet filtering/mixing/master processing. The earlier independent
prepared-piano differences remain around 3e-12, with exact int16 acquisition.

A third, independently allocated **original** reverb receives the candidate's
captured input and the same ordered type/control/freeze commands. It reproduces
the candidate wet output exactly throughout every fixture. Alongside the
common-prepared-piano test, this demonstrates input-rounding/feedback divergence
in the downstream reverb, rather than a different reverb port/control result
on identical input. The exact upstream double-operation origin has not been
made bit-identical; the trace is not a universal proof of numerical stability.

The full instrument runner intentionally still returns exit 1 with
`sound_difference_review_required`. Reference replay proves localization, not
independent whole-chain equality or perceptual acceptance. This is not a
claim of an audible fault, CPU overload or real Pi crackling. Settle the
remaining sound-acceptance question separately with justified numerical and
matched-level listening criteria, not repeated tolerance increases.

## Reproduce and saved evidence

```sh
python3 tools/compare_stage_output.py
python3 tools/compare_stage_instrument.py --soundfont /absolute/path/to/existing.sf2
python3 tools/run_offline_tests.py
```

Output component requires the existing C/C++ compiler and NumPy. Full graph
also requires the previous Faust/FluidSynth/CFFI/SciPy dependencies. No runner
installs anything, opens physical devices or contacts the Pi.

Private reports under `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/`:

- `native-v2-output-20260917/report.json`, component PASS:
  SHA256 `423628aa35fe5670948eeddca1f68742719092a1f7681cd419896cdc24a6d501`.
- `native-v2-first-divergence-20260917/report.json`, initial trace:
  SHA256 `dcb03788d6af422fb9a0a07713cfe7a7034abaddca1c21e6c6e9a9bfed47dac8`.
- `native-v2-output-integrated-final-20260917/report.json`, final eight-run
  matrix including exact replay/output wiring and retained overload failure.
  SHA256 `6b205457d7bbda87c7c1ad1c2b3b911c16300908b30561d5329182f308244190`.

Reports retain commands, source/artifact hashes, test arrays and trace values.
No private audio arrays or SoundFonts are committed.

## Remaining work, not a percentage-complete estimate

The implemented core now reaches PCM. Remaining breadth is still substantial:
global modulation/drift/sympathetic, independent sampled bed/drone, organ,
remaining unison/program/split/control compatibility, recorder/scene behavior,
bounded live events/control/telemetry, browser/native protocol, backend/recovery
and Pi4 timing plus practical player acceptance. Existing v1.2 retains those
features while the successor is unfinished. Continue offline feature migration;
do not deploy this checkpoint or claim all sound/live gates have passed.
