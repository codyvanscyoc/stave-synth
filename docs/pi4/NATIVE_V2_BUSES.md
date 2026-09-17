# Native-v2 downstream buses checkpoint — September17,2026

Implemented `PadBus` and composed it with `PianoRoom` in single-owner
`StageBuses`. It consumes the seven source-format channels and emits eleven
internal bus channels. No oscillator DSP behavior, browser, driver, production
settings, Pi service or preserved branch was changed. **Not a playable native
successor yet.** M2 remains open; no promise of morning native field readiness.

## Implemented scope

- Existing double Faust pad-bus DSP, with its original80ms log cutoff and
  highpass smoothing, independent-filter smoother gates and main cutoff's
  .1Hz coefficient-update threshold.
- Shared12/24dB and independent filters, brightness/gain compensation,
  highpass, Haas, separate effects sends, send fast/slow decisions.
- Existing magnitude-ratio dry FX-bypass carve. This is an approximation for
  two simultaneous oscillators, not newly separated exact postfilter stems.
  It uses pre-Haas magnitudes as the existing merged path does.
- Shimmer HP/mix and CLOUD taps, exact gate thresholds, update/freeze behavior
  and original add order. Hard clear fully reinitializes the Faust table;
  instanceClear alone would leave old cloud audio stored.
- Piano room on the two independent piano stems, retaining its verified
  control/tail behavior. Room and pad numerical failures silence the whole
  composed output, not only the failing branch.

| Channel | Internal signal |
| --- | --- |
| 0/1 | Filtered dry oscillator bus after bypass carve |
| 2/3 | Reverb input, including shimmer/cloud, NOT processed reverb output |
| 4/5 | Dry FX-bypass bus to be added after effects |
| 6 | Shimmer mono diagnostic, already included in2/3 |
| 7/8 | Cloud stereo diagnostic, already included in2/3 |
| 9/10 | Room-processed piano |

Do not sum all channels: that would double-count diagnostic sends. No master
gain, output protection/limiter, shared reverb, delay, full FX/bed/organ/recorder
or physical audio output is present. Global LFO amp/pan/filter/bus modulation,
filter drift/wobble and ping-pong are explicitly disabled on BOTH comparison
sides. Poly amp modulation already in saved source stems remains in the input.
These omissions describe an offline slice, not silent feature cuts in v1.2.

`PadBlockFlags` are supplied explicitly per block: whether OSC2 is audible from
the smoothed blend, whether voices were rendered, and effective pan separation
for Haas. Tests manipulate these flags; they do not claim a complete source
control adapter. Fast-path reverb input currently copies the post-carve dry
bus; when ping-pong is added this copy must occur AFTER that processor.

## Ownership and safety contract

Fixed48kHz, either fixed256 or512 whole frames. Configure, render, clear, state
and stop are owner-only, not concurrent browser/callback APIs. All input
storage belongs to the caller and must cover the complete block. Null inputs
and input aliases with owned output storage are rejected before mutation;
invalid-call silence remains the future driver's responsibility.

Configuration validates all pad and room values before either component is
changed. Buffers are preallocated. Numerical faults and explicit stop cause
terminal silence across all11channels; neither configure, clear nor render
can resume a stopped graph. Reconstruct only while the owner is stopped.
Hard clear retains parameter smoothers, matching the old per-module clear;
it is NOT equivalent to a musical note release or tail-preserving bypass.

## Actual verification

`tools/compare_native_buses.py` AST-extracts the pinned original scalar
filter/zone logic, bypass carve and reverb-send/shimmer adds, then runs the
actual pinned Faust wrapper. It also reuses the pinned room oracle. Original
Python fallback coefficient setters are inert placeholders because the
reference's active Faust branch consumes the zones, not those filters.
No app/runtime imports, devices, network or settings paths are opened.

Reference:57bb94cdbd3c9349fc2a47833d659451ebb8738b. Both sides use separately
instantiated copies of the same pinned DSP under matched compiler flags.
This tests C++ routing/control/composition against actual original logic;
it does not prove full-runtime parity or cross-architecture equivalence.

Four runs each compare327,680frames across11channels: synthetic stereo
signals and explicitly reused native source stems, at512 and256. Unlike the
earlier room-only experiment, this uses the respective source file for each
cadence. Source rendering itself remains separately verified, not performed
by this runner. Flags and bus automation are independent stress fixtures,
not claimed coherent saved stage presets.

- Real source stems: all11outputs and all9checked control-state values exact
  at both cadences.
- Synthetic inputs: worst output difference6.939e-18; largest state difference
  4.441e-16 from magnitude-sum rounding. Audio tolerance remained1e-6;
  state tolerance1e-8. No tolerance was relaxed after observing results.
- Nonzero tails captured before clear; exact silence after final hard clear
  and terminal stop. Coverage includes slope/routing/send/bypass flips,
  opposite range endpoints, filter/resonance extremes, highpass thresholds,
  shimmer enable/mix/cloud thresholds, Haas on/off, and room transitions.
- C++ UBSan guards pass: invalid configs/pointers/aliases,2,000control/render
  blocks across both cadences, CLOUD flush, coupled failures, terminal stop.
- Calibrated C++ new/new[] probe records zero allocations during the exercised
  warmed configure/render/clear loops. No claim about C malloc, hidden library
  locks or deadlines. Generated Faust C is uninstrumented; ASan still unverified.
- Reviewed legacy suite:549tests, zero skips, Node/UI and syntax checks pass;
  123Python files parsed.

Mac evidence (not Pi timing):

`Documents/stave-synth-pi4-backups/native-v2-m2-buses-20260917/report.json`

SHA256 `d6be3c60a5f8390bfed6134028881f20966d5bacaf889f23bd32e5ea254095f2`.
The report contains source/input/generated-code/binary/fixture hashes and
commands. Generated audio arrays and binaries remain private, not in Git.

```sh
python3 tools/compare_native_buses.py
python3 tools/compare_native_buses.py --source-dir /absolute/path/to/source-comparison
```

Only installed dependencies are used. Output directories must be new. Never
run compilers/stress tools on a playing Pi merely because they open no devices.

## Source adapter gap made explicit during integration review

`StagePatch.blend1/2` currently mean prepared Faust LINEAR amplitudes. The
previous source comparison supplied those prepared values to both wrappers;
it did NOT cover the old browser-fader preprocessing. Passing browser faders
directly would over-gain the oscillators: original0.6 maps to approximately
0.331amplitude after its5ms blend smoother and -24dB curve. The header now
documents this explicitly; no source DSP behavior was silently changed.

Before joining source and buses under a complete stage-control owner, port
and compare this preprocessing, hard-pan override, shimmer mix gate and the
all-oscillators-off path. The original skips Faust and uses its fallback bus
on all-source-muted blocks; blindly always running Faust is not established
as equivalent for freeze/resume/filter-tail behavior. The current source
component and bus component tests deliberately do not prove that boundary.

## Next and field-test gate

1. Implement/verify the source control adapter above, including mute/re-enable
   trajectories. Then compose sources/buses under one acknowledged owner.
2. Finish global modulation, ping-pong/shared reverb, master gain/limiter and
   the worship functions. Preserve independent bed/drone/freeze and existing
   program/scene behavior; no hidden substitutions.
3. Complete bounded control/MIDI adapter, browser/preset integration and
   artifact/build manifest before an isolated Pi4 timing candidate.
4. Confirm off-stage maintenance and actual output configuration before any
   Pi4 build/device test. Only measured hardware results can qualify256.

For morning playing, the preserved512-frame application is the existing
working option, not this native successor. Its prior successful light-use
session does not establish full-load/recovery qualification. No Pi readiness
check occurred this batch. No tests remain running at the saved checkpoint.
