# Native-v2 shared ambience — September 17, 2026

## Implemented and verified

`StageDelay`, `SharedReverb` and `PadAmbience` now implement the original native
delay, three-backend reverb dispatcher and composed pad/filter/ambience routing
offline. No production source, settings, service, UI or Pi file changed. No Pi
contact, deployment, device opening or live timing test occurred this batch.

This is a substantial downstream slice, **not a complete instrument or final
master output**. The earlier `StageCore` remains unchanged: this new composition
has been exercised with synthetic inputs and saved seven-channel source stems,
not yet joined to live-in-process voice/piano acquisition under that owner.
M2/M3 and later qualification gates remain open.

### Delay

- Retains the float32 Faust boundary, dry-plus-wet output, tempo/free timing,
  rate/polarity, offset, feedback/Oblivion, filtering, drive, stereo width,
  modulation, reverse, reverse feedback and Aurora controls.
- Disabled or sufficiently quiet wet/reverse settings pause native processing
  and control-zone updates, matching v1. Re-enabling can resume stored state;
  this is not a redesigned tail-safe bypass or an oscillator-click fix.
- External piano/organ delay-send dry content is added before native processing
  and subtracted afterward, including the original float-boundary rounding.
- Allocates native state and fixed scratch while stopped. No lazy construction
  or loading on control changes. The original generated reverse-delay arrays
  remain large; no Pi memory or CPU optimization is claimed in this port.

### Shared reverb and freeze

- Wash, Hall, Room, Bloom and Ghost use the retained FDN; Plate and Drone retain
  their distinct native topologies. All three backends are prepared at startup.
  No silent fallback or substitution is added by the new owner.
- Type selection is idempotent. Changed types apply original presets and
  mapping/order rules; subsequent explicit control setters override them.
- Switching topology clears the entered backend instead of replaying its old
  suspended tail. FDN-to-FDN preset changes keep the live FDN tail. Original
  hard-switch behavior is preserved, not claimed to be crossfade/click qualified.
- Freeze preserves its two-second capture window, whole-block sealing boundary,
  desired decay/damp edits while frozen, backend changes, unfreeze and panic.
  This is **reverb freeze**, not the independent recorded sample-bed feature.
- Original compatibility quirks remain, including retaining the plate's prior
  feedback on a standalone zero-decay setter. Deliberate tonal changes must be
  reviewed separately, not hidden in an ownership migration.

### Pad routing and mix

`PadAmbience` owns PadBus, StageDelay and SharedReverb together:

1. Filter/Haas/bypass carving occurs before delay.
2. Unity-send reverb input is built AFTER delay, then shimmer/cloud are added in
   original order. Unequal/bypassed oscillator sends retain the separately
   filtered send path and do not acquire delay taps accidentally.
3. External reverb sends join before the original 0.6 trim and tanh.
4. The original equal-power dry/wet mix, 80 ms block smoother, wet gain, bypass
   add-back and 0.85 pad headroom trim are preserved.
5. Returns contain mixed pad L/R, separate dry L/R and separate FX L/R. These
   six channels are alternative routing representations: do not sum all six.

The split dry snapshot is reconstructed from post-carve dry plus bypass; this
can introduce double-rounding differences. Measured worst composed difference
is 1.665e-16. No numerical tolerance was relaxed.

## Evidence and limits

Host is the Mac, with existing compilers/Faust. Pinned reference remains
`57bb94cdbd3c9349fc2a47833d659451ebb8738b`.

| Test | Result at both 512 and 256 frames |
| --- | --- |
| Delay and reverb components, 2,097,152 frames each cadence (~43.69 s) | All four output channels exactly match original; all compared parameter zones and freeze/type state exact |
| Synthetic composed ambience, 2,097,152 frames each cadence | Worst channel difference 1.111e-16; dry/wet smoother exact |
| Saved actual Faust/FluidSynth source stems, 327,680 frames each cadence (~6.83 s) | Worst channel difference 1.665e-16; dry/wet smoother exact |
| Muted source/frozen native-pad transitions in composition | 40/80 synthetic blocks and 10/20 saved-source blocks; continuing delay/reverb matches reference |
| C++ UBSan and allocation guards | Shared effects: 1,600 transition blocks; composed ambience: 1,400; invalid configuration/pointers/aliasing, finite output and terminal silence pass |
| Prior pad/room bus recheck after oracle hook extension | All four comparisons pass; saved-source channels exact, synthetic worst 6.939e-18 |
| Reviewed legacy runner | 549 Python tests, zero skips; Node UI and syntax checks pass; 127 Python files parsed |

The allocation probes are calibrated and observe zero post-warmup C++
`new`/`new[]` calls in their exercises. They do not intercept C allocation,
measure locks, bound worst execution time or establish dependency real-time
safety. UBSan instruments C++ wrappers/guards, not generated Faust C or external
libraries. ASan remains unverified. These bounded fixtures are not a soak,
all-combinations proof, acoustic listening approval or Pi4 timing result.

The oracle executes explicitly selected original wrapper/engine declarations,
not application startup. Reverb's four relative backend imports are replaced
with the explicitly built offline backend classes; ownership/diagnostic
decorators are bypassed only in the single-thread reference. Actual control and
audio method bodies execute unchanged. Pad routing uses the original delay
call, send logic and mix/split sections. Numeric faults in the new owner are
terminal and silent; this deliberately differs from the old reverb's attempted
local NaN repair. The finite-input comparison does not test equivalence of
those different fault-recovery policies.

## Reproduce and retain

```sh
python3 tools/compare_shared_effects.py
python3 tools/compare_pad_ambience.py --source-dir /absolute/path/to/seven-stem-source-evidence
python3 tools/run_offline_tests.py
```

`--source-dir` is optional; without it the ambience runner checks only synthetic
input. It must contain seven-channel `sources-native-512.npy` and
`sources-native-256.npy`, NOT StageCore's eleven-channel evidence. Source-file
hashes are retained in the report. Source fixtures here come from
`native-v2-source-core-recheck-20260917`, not a new FluidSynth acquisition.

Private evidence under `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/`:

- `native-v2-shared-effects-20260917/report.json`
  SHA256 `e40bf4ba6fe44489ba80747948d9dbe579860488eb6a4a57a5966521d89d8c98`.
- `native-v2-pad-ambience-20260917/report.json`
  SHA256 `0fd3ef578b1551ebaef607ea2bfe4b1c53a6b22e79f26d0c5946f8728183be44`.
- `native-v2-buses-effects-recheck-20260917/report.json`
  SHA256 `4e0b9d390d18f47fc296258ba92eb46203c67b56dfd2affcbadfc4c663a1f6e4`.

Reports retain compiler commands, source/asset hashes and generated artifacts.
No samples, settings archives or private render arrays are committed.

## Next, in dependency order

1. Complete wet-output filtering, remaining global modulation/drift and shared
   sympathetic routing. Port/compare master EQ, shuffler, bus compression,
   saturation, final limiter and gain with dry/FX-bypass routing.
2. Compose actual source/piano acquisition, piano room and these effect returns
   under one owner. Do NOT feed already-filtered StageCore buses back into
   PadAmbience; reuse the source slice and give PadBus exactly one owner.
   Validate piano/organ send tap points and dry mixing against JackEngine.
3. Independent sample beds/drone player, organ, recorder, split calculation,
   macros/scenes and prepared program changes still need migration. The Drone
   reverb above does not replace an independent tonic/fifth or sampled bed.
4. Integrate bounded event/control/telemetry transfer and existing browser/state
   behavior, then build and qualify an isolated Pi4 candidate during an approved
   off-stage window. Live 256 remains unsupported until target evidence passes.

No runtime test or monitoring process was left running by this batch.
