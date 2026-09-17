# Native-v2 owned motion checkpoint — September 17, 2026

Status: connected offline sound-graph milestone, **not a field-test build**.

## What changed

StageInstrument now owns the previously compared StageMotion and FilterMotion
components. An explicit `owned_motion` configuration enables the joined path;
the older component-fixture mode remains available to preserve prior tests.
No Python application runtime is imported or used to render candidate audio.

- Accepted oscillator note starts invoke key-sync through the source owner's
  hook. Piano-only split notes, velocity rejection and raw note-offs do not
  reset the global clocks. Old ramp/smoother endpoints survive key-sync.
- Motion owns BPM and motion-mix for delay, both global clocks, prepared poly
  modulation and the master LFO sidechain. Motion owns unrounded Haas
  milliseconds: LFO compensation retains fractional time while pad delay taps
  use the original integer-sample conversion. Legacy diagnostic fields
  (`poly_lfo`, pad Haas samples, external master modulation and delay BPM/motion) do not override
  those authorities while owned motion is enabled.
- Poly amplitude modulation uses the original wrapper's **.05..20 Hz clamp**,
  while global tempo clocks retain their wider computed rates. This distinction
  was verified in `faust_osc_bank.py`; do not infer behavior from Faust slider
  metadata alone. Receive masks do not gate the original per-voice poly path.
- One global clock advance precedes filter random draws. Smoothed base cutoff
  receives modulation/drift/wobble before coefficient gating; target changes
  invalidate the shared and wet-filter retune cache even at unchanged cutoff.
- Dry amplitude/pan uses pre-Haas oscillator magnitude ratios where required,
  then the existing FX-bypass carve. Fast and slow reverb sends preserve their
  different routing. Bus modulation gates the post-effects mix **before** dry
  bypass is restored. Piano dry sound is not multiplied by the pad LFO.
- All-muted blocks still advance motion/scalar state while oscillator and pad
  Faust processing pauses. Numerical/random failures stop the whole owner and
  silence final outputs. One owner supplies bounded prepared random draws;
  there is still no production RNG/seed policy or live control API.

Both injected phase and motion random sources must outlive StageInstrument.
They are single-owner, bounded, nonblocking interfaces, not opportunities for
runtime file/network access. Enabling drift/S&H without a usable random source
fails terminally when a draw is required; it does not silently disable a sound.
The graph remains fixed 48 kHz, whole 256/512-frame blocks and exact-current-
boundary commands. It is not yet the sample-sliced M1 Engine backend.

## Evidence and limits

The pinned reference remains Git object
`57bb94cdbd3c9349fc2a47833d659451ebb8738b`.
The full comparison retains the prior ten runs and adds four owned-motion
runs: merged/nonmerged original modulation, each at 512 and 256 frames.
The merged reference runs the actual original Faust pad module's LFO zone
push/readback on supplied source stems. This covers its modulation math and
state handoff, not the old Python/C merged-call scheduling or live bridge.

| Check | Observed result |
| --- | --- |
| Owned motion, merged/nonmerged, 512 | PASS, worst internal-channel difference 4.399e-12 |
| Owned motion, merged/nonmerged, 256 | PASS, worst internal-channel difference 4.613e-12 |
| Motion owner state | Exact phases/held values/endpoints/walks/draw counts; smoother error at most 2.221e-16; effective cutoff error at most 3.638e-12 Hz |
| New musical fixtures per run | 37 raw note-ons, 36 oscillator triggers, splits/pedals, 87/174 muted blocks, 184/368 active-poly blocks, 788/1524 random draws |
| Real int16 piano acquisition | Exact in all fourteen runs |
| Independent original reverb replay with candidate input | Exact wet output in all fourteen runs |
| Identical-input final output/recorder tap | Exact in all fourteen runs |
| Existing source/split fixtures | Still pass; previous results unchanged |
| Existing independent high-gain fixtures | **Still FAIL unchanged 1e-6 gate:** 2.795e-6 at 512 and 2.027e-5 at 256 |
| UBSan / scoped allocation guard | 1,400 owned blocks including motion, source/filter/effect transitions; zero C++ new/new[]; key-sync admission, invalid configuration, random fault and terminal silence checks pass |
| Standalone motion recheck | 25,920 LFO blocks plus 6,000 filter blocks pass; separate 6,000+6,000-block UBSan/allocation guards pass |
| Legacy regression | 549 tests, zero skips, Node checks pass; 132 Python files parsed; 12.644 seconds |

Full runner therefore still returns exit 1 / `sound_difference_review_required`.
No tolerance was relaxed. The high-gain failure's previously traced input-
rounding/reverb-feedback origin is not waived by the new passing fixtures.
Sanitizers/scoped C++ allocation checks do not audit dependency allocations,
locks, Pi4 deadlines, real controller-to-DAC latency or hardware recovery.

Final full-graph report:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-motion-connected-verified-20260917/report.json`

SHA256 `355d79035fb749b7b11012b0439092a9183aec8509c71c3334369dadbe37e66c`.

Standalone recheck report:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-motion-component-recheck-20260917/report.json`

SHA256 `73945eb3a0c0fbb27ea9137b6b4ff2b9152e31675f63a51728d18dfd9d8ec0d3`.

Reproduce with the commands in native_v2/README.md. Reports and actual candidate/
reference internal audio arrays remain private; existing evidence is never
overwritten. Development runs exposed a wrong range order in the new piano-only
test setup, corrected before passing. The last verification also adds fractional
Haas milliseconds and preserves their unrounded LFO compensation separately
from the integer audio delay taps.

## Pi contact and full-testing readiness

Read-only SSH succeeded during this task. Observed host `stavepi4`, active
`stave-synth.service`, PID185009, NRestarts0, working directory
`/home/codyvanscyoc/stave-synth-pi4-rehearsal`. This is a point-in-time service
check, not an audio health or performance measurement. No restart, native-v2
deployment, output routing change, device opening or remote stress test occurred.
The working stage build, Pi5/main, and preserved snapshot remain untouched.

The successor still needs the following before **full** user field testing:

1. Remaining sound/function parity: recorded pad/bed and drones, sympathetic
   resonance and per-voice drift, organ, other unison modes, piano pitch/program
   changes, recorder transfers, macros/scenes and safe transition behavior.
2. Bounded live event/control ownership joined to this whole-block graph;
   native backend, truthful telemetry, existing browser/state/preset/MIDI maps.
   Do not call the whole-block graph once per MIDI slice.
3. Isolated Pi4 native compilation and worst-case timing/memory measurements,
   supported profile selection, sound-difference acceptance, then reversible
   candidate installation and recovery checks during a confirmed maintenance
   window. Do not infer usable 256-frame latency from Mac comparisons.
4. Player rehearsal on the actual connected keyboard/interface/network.
   The declined eight-hour soak remains waived.

Next implementation focus is the independent recorded-bed/drone ownership
and its tail/transition tests, followed by remaining feature/control integration.
Pi performance work additionally awaits confirmation of a current off-stage
window; an asynchronous question was sent during this task. No background
monitoring or autonomous overnight work remains running after these tests.
