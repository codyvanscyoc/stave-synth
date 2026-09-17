# Native-v2 motion components — September 17, 2026

## Scope: implemented and compared, not yet connected

`StageMotion` now implements the two global LFO clocks and their prepared
filter, amplitude, pan and post-FX bus modulation. `FilterMotion` implements
the original global filter drift and resonance-gated wobble. Both are offline
single-owner components. **StageInstrument does not use them yet.** This batch
changes no running instrument, physical device, preset or production state.

The components preserve the current sound algorithms instead of treating a
language migration as permission to redesign motion:

- All seven waveforms, FREE and eight tempo subdivisions, multiplier, stereo
  spread, inversion, absolute time offset and Haas compensation.
- Independent phase/sample-and-hold state, key-sync, depth/motion gating and
  the original 0.7 application-depth cap.
- Inclusive per-sample endpoint ramps and one-pole smoothing, stacked
  amplitude gates/pan additions, selective oscillator receive routing and
  the separate post-effects bus gate.
- Midpoint filter modulation and target-change invalidation, with separate
  filter random walks and the original 20..20,000 Hz final cutoff clamp.
- Finite/range validation before configuration mutation, fixed arrays, no
  native file/network/console calls, and terminal random-source failure/STOP.

Sample-and-hold and filter walks consume an explicit bounded `MotionRandom`
source. Tests use prepared tapes, not a hidden random device. No production
seed/generator policy is selected here. The supplied source must outlive its
consumer, be single-owner, bounded and nonblocking. `StageMotion` buffers are
borrowed read-only storage, valid until the next operation overwrites them.
Only the first `block_frames()` values belong to a block.

The filter component admits a tiny numerical margin around the nominal input
cutoff limits because `exp(log(20))` and `exp(log(20000))` can fall a few ULP
outside them. It retains the original value for computation and final clamp;
this is not a wider user control range or changed audio-comparison tolerance.

## Compatibility details not to accidentally change during integration

1. LFO phase advancement is gated by depth × motion, not `active`; `active`
   gates application depth. Below 0.001 the clock pauses and old ramp endpoints
   become zero, while unused smoother state remains held. Equality at the
   threshold intentionally has different clock/application decisions.
2. Key-sync resets phase and held random values, **not** the previous ramp or
   smoother. Invoke it where the original oscillator note-on occurs, not for
   every raw MIDI packet, rejected note or piano-only split.
3. A target change resets only that LFO's ramp/smoother endpoints and requests
   filter-cache invalidation. The phase and held random state survive.
4. The original selective receive path can step a shared LFO's smoother twice
   in one block. This behavior is explicitly compared, not silently corrected.
5. Original pan modulation uses the A ramp for both channels with opposite
   polarity, even though B has its own spread/smoother state. Preserve it.
6. Sample-and-hold draws on `new_phase < old_phase`, including the existing
   behavior at tempo/multiplier settings that cross multiple cycles per block.
7. Faust-owned poly amplitude modulation suppresses the global AMP path, but
   does not suppress PAN. This component honors that decision; it does not
   configure or render Faust's per-voice LFOs itself.
8. Filter drift/wobble retain state when disabled and draw only when their
   original gates are active. Wobble's resonance scale is not capped at 1.

These are compatibility facts, not claims that every historical policy is ideal.
Any later click/behavior correction needs a separately identified sound change.

## Evidence

The oracle extracts named methods and guarded AST spans from preserved Git
object `57bb94cdbd3c9349fc2a47833d659451ebb8738b`. Original waveform, filter,
receive-routing, ramp, smoothing and target/key reset bodies execute unchanged.
There is no application import/constructor or device access. Synthetic stereo
audio and supplied oscillator magnitudes exercise the modulation multipliers;
this does not constitute a complete instrument capture.

The comparison deliberately uses the **non-merged** original modulation path.
The existing Faust merged-LFO state handoff is not qualified by this test and
must be addressed explicitly during native graph integration.

| Check | Result |
| --- | --- |
| LFO/routing, 12,960 blocks at each 512/256 cadence | PASS; worst synthetic-audio difference 1.077e-14, state difference 3.120e-14; predeclared tolerance 2e-12 |
| All 16 target pairs and all 16 receive masks | Covered, including stacked amp/pan/bus and twice-stepped selective smoothing |
| Clock/control transitions per cadence | 4,992 selective blocks; 269 target resets; 172 key-trigger calls; exact random draw ownership |
| Filter-motion comparison | 6,000 blocks; cutoff worst3.638e-12 Hz (tolerance2e-9); walk states/draw counts exact; both±1 clamps covered |
| C++ UBSan and calibrated allocation guard | 6,000 LFO + 6,000 filter blocks; zero C++ new/new[]; invalid values, random failure and terminal STOP guards pass |
| Reviewed legacy regression | 549 tests, zero skips; Node checks pass;132 Python files parsed |

The C++ allocation probe does not audit a future external random generator,
third-party allocators, locks or real-time deadlines. This is fixed 48 kHz
component correctness at each cadence, not equivalence between cadences,
Pi4 timing/headroom, measured latency, or supported live 256.

## Reproduce

```sh
python3 tools/compare_stage_motion.py
python3 tools/run_offline_tests.py
```

Requires existing C++17 compiler, NumPy and SciPy. No Faust or SoundFont needed
for these motion-component tests. Reports go into a new output directory;
existing directories are refused. Reports retain source/artifact hashes,
compiler commands, fixtures, random tapes, native LFO state traces and
native/reference filter arrays. Complete synthetic-audio streams are compared
in memory and hashed, not saved as listening WAVs.

Final private report:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-motion-final-20260917/report.json`
SHA256 `46384743f7cfe715be206836cd025bed439d74981dbad24d8a25874657dfbfcd`.
Final legacy run:12.590seconds. All testing is complete; no monitor was started.

## Next integration, with explicit boundaries

- Wire one owner into the graph in the original order: source/poly decisions,
  global LFO, smoothed-cutoff modulation, dry amp/pan, delay/reverb, post-FX
  bus gate, dry/FX split and master sidechain. Do not modulate piano dry audio
  just because it shares the final master.
- Preserve fast/slow reverb-send behavior, magnitude-ratio approximation,
  bypass-carve ordering, all-muted transitions and wet-filter retune gating.
- Connect key-sync through accepted oscillator note-on ownership and handle
  target-change filter invalidation. Avoid independently running duplicate
  clocks, cutoff smoothers or poly modulation.
- Compare actual Faust paths and the joined instrument with matched stimuli;
  keep the independent high-gain sound gate open until separately resolved.
- Per-voice pitch drift, sympathetic resonance, bed/drone, organ and other
  remaining features/live protocol/backend/qualification are still pending.

The last full-graph evidence remains [the split checkpoint](NATIVE_V2_SPLITS.md).
The joined graph was not modified or rerun in this component-only batch.
That runner's high-gain comparison still fails its original 1e-6 threshold;
passing these new components does not waive it. No field-testable successor
or stage-release approval is claimed.
