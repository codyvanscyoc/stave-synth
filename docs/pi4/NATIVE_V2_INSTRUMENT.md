# Native-v2 connected instrument — September 17, 2026

## Connected, not release-qualified

`StageInstrument` now owns actual StageSources (Faust oscillators and FluidSynth
piano), source-fader preparation, piano room, piano shared filter/soft clip/
sends, PadAmbience and StageMaster in one offline render call. It constructs no
StageCore/StageBuses instance: PadAmbience owns the only pad filter. Output is
stereo post-limiter, **before bridge float32 conversion/master fader/device
gain**. Current-boundary commands and complete 256/512 blocks at 48 kHz remain
the contract, not sample-sliced Engine/driver integration.

The existing source fixture passes the full independent comparison. An added
high-gain piano stress fixture does NOT pass the strict sound-difference gate.
This checkpoint is explicitly **work in progress**, not a fully verified sound
replacement or a native-v2 field-test candidate. No Pi contact, deployment,
restart, native target build, hardware I/O or production state change occurred.

## Routing preserved

- Real source acquisition runs every block, including piano while both played
  oscillators are muted. Source fader smoothing and re-entry semantics remain.
- Piano room precedes the original shared lowpass and 0.85 soft-clipping knee.
  The shared piano filter uses the previous pad cutoff, matching JackEngine's
  piano-before-synth order. Both delay/reverb sends tap the clipped piano,
  admitted only above 0.001, with separate fixed buffers. The same piano dry
  signal reaches the master and piano sidechain.
- Pad processing/filter/bypass/shimmer occurs once, then delay and shared
  reverb. Optional wet-reverb filtering now runs before dry/wet mixing, without
  duplicate resonance compensation. Original coefficient-update and pause
  gates remain, including the stale-coefficient quirk when enabling the wet
  filter/changing slope without a cutoff/resonance update. This is not a
  newly redesigned click-free filter transition.
- Master preserves the established dry/FX compression-bypass split. Its
  previously documented ceiling correction and zero-knee correction remain
  explicit differences from v1.2, not hidden comparison changes.
- One owner issues configuration, note/pedal and reverb/freeze/BPM operations.
  Invalid configuration is refused before mutation; dependent numeric/phase
  faults stop the whole graph. STOP is terminal and silences exported buses.
  No concurrent UI API or live soft-panic/tail policy is implied.

Ten diagnostic channels: master L/R, pad mixed L/R, pad dry L/R, pad FX L/R,
prepared piano L/R. Do not feed these arrays to a seven-source or eleven-core
fixture reader. The first pair is the scoped stereo output, not a sum of all
diagnostic channels.

## Evidence and the open sound gate

Pinned source remains `57bb94cdbd3c9349fc2a47833d659451ebb8738b`. Tests render
fresh actual FluidR3 samples in both native and independent reference graphs,
not saved source stems. Original pure routing/filter/soft-clip/send bodies are
extracted without importing the application. The sole relative constants
import is replaced by its exact constants in the isolated oracle.

Eight comparisons: two cadences × two source fixtures × independent versus
common-prepared-piano diagnostic. Each is 327,680 frames (~6.83 s), not a soak.
All seven reverb types, freeze/type edits, both piano sends, piano/wet filters,
source mute/re-entry, room and compression/sidechain changes are exercised.

| Comparison | 512 frames | 256 frames |
| --- | --- | --- |
| Original source fixture, independent full chain | PASS, worst 6.001e-12 | PASS, worst 3.030e-10 |
| Added piano overload, independent master peak difference | **FAIL strict 1e-6**, 2.795e-6 | **FAIL strict 1e-6**, 2.027e-5 |
| Overload master RMS difference, worst channel | 1.762e-8 | 8.409e-7 |
| Overload with identical prepared-piano input to downstream reference | PASS, worst 3.895e-12 | PASS, worst 4.388e-12 |
| Raw int16 FluidSynth acquisition | Exact | Exact |
| Independent prepared piano in overload case | Worst 2.901e-12 | Worst 3.068e-12 |

The additional overload fixture uses valid controls: piano volume 1 and 24 dB
compressor makeup. Pre-routing piano peaks reach ~3.50/~3.44 and trigger the
original soft knee. It is intentionally distinct from the existing source
fixture; no failing fixture was removed or relabeled as passing.

The common-piano diagnostic replaces ONLY downstream reference piano input
with the candidate's prepared piano; it still compares the independently
prepared piano diagnostic channels. Its purpose is localization, not a claim
of independent whole-instrument parity. Results localize the larger difference
to propagation of very small upstream piano differences through the effects
path. Mixed double/float boundaries and feedback sensitivity are a plausible
explanation, **not yet a proven exact first-divergence mechanism or listening
acceptance**. The dry pad split remains exact. Output remains finite and within
the corrected 0.98 sample ceiling in all comparisons.

A trial separate full-chain budget of 1e-5 peak/1e-7 RMS passed 512 but failed
256; it was NOT adopted. The final runner retains 1e-6 and returns exit 1 with
`sound_difference_review_required`. It collects all matrix results instead of
aborting at the first mismatch, so diagnostic passes cannot hide sound failure.
Do not publish this as an all-tests-pass checkpoint or solve it by repeatedly
relaxing a threshold. No audible defect, Pi4 timing failure or CPU exhaustion
has been established by these offline differences.

Other verification:

- UBSan and calibrated scoped zero-C++-new/new[] guard: 1,400 integrated blocks,
  all reverb backends, sends/filter/freeze/master transitions, phase failure,
  boundary/configuration refusal and terminal silence pass.
- Rebuilt source/bus/effects/master component guards pass.
- Prior core recheck passes: nine channels exact, piano-room worst 1.112e-13.
- Prior ambience recheck passes, worst 1.665e-16 with wet filter disabled.
- Reviewed regression: 549 Python tests, zero skips, Node UI/syntax checks;
  129 Python files parsed.

UBSan excludes generated Faust C/FluidSynth; C malloc, dependency locks,
real-time deadlines, ASan and physical listening are not qualified. Repeated
short renders do not substitute for a musical rehearsal. No live 256 support
or CPU saving is claimed.

## Reproduce and resume

```sh
python3 tools/compare_stage_instrument.py --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_native_core.py --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_pad_ambience.py --source-dir /absolute/path/to/seven-source-evidence
python3 tools/run_offline_tests.py
```

First command currently exits 1 for the documented overload sound gate. It
requires existing compilers/Faust/FluidSynth, NumPy, CFFI and SciPy. The Mac's
isolated `native-v2-test-env/bin/python` under the private backup root supplies
SciPy. The runner never installs dependencies or opens devices.

Private evidence under `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/`:

- `native-v2-instrument-final-20260917/report.json`: final eight-run matrix,
  source/artifact hashes, commands, arrays, first-maximum frames and counts.
  SHA256 `4cc6b74e9df316534e86cbaaa10f2d84073f12f3bbd85d82df09e7d23a8c38d2`.
- `native-v2-instrument-20260917/report.json`: retained unsuccessful trial
  numerical budget; SHA256 `a217a20b4c952d6d56ce23c30f79c6d0494edf478c3d3147155158ce19cdda01`.
- `native-v2-core-instrument-recheck-20260917/report.json`:
  SHA256 `c939cd1a9b04d79a1a441660576280bbe78e5def216cb4d762fec363bf5f73ac`.
- `native-v2-ambience-instrument-recheck-20260917/report.json`:
  SHA256 `87b0081f3e05ba8f956c5222974cee98698f20c507f8a8dd56f374a39c686edb`.

Next: trace the overload's first downstream divergence and settle its sound
acceptance separately; port final output/bridge gain, remaining global
modulation/drift/sympathetic, sample beds, organ and other feature coverage.
Then integrate bounded events/controls/telemetry and browser/driver ownership.
An isolated Pi4 build/timing run and actual player acceptance still require an
approved off-stage window. The working v1.2 remains the playable instrument.
