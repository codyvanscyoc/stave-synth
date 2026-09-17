# Native-v2 integrated source checkpoint — September 17, 2026

The next M2 step is implemented: `StageSources` combines native stage key/
pedal ownership, dual-envelope voice allocation, the real12-slot Faust bank,
real FluidSynth piano acquisition and native piano processing under one owner.
It emits seven separate source channels: OSC1 L/R, OSC2 L/R, shimmer mono and
processed piano L/R. This is **an offline source slice, not the whole synth**.
M2 remains open. No Pi command, deployment, restart or device opening occurred.

The prior [component checkpoint](NATIVE_V2_M2_COMPONENTS.md) remains evidence
for individual modules; its statement that no combined source exists is now
superseded by this checkpoint. The M1 `SoundBackend`/`render_demo` remains
unchanged and is not the integrated source graph.

## Explicit processing contract

- One owner handles commands, configuration, rendering, telemetry and stop.
  Construction/destruction and SF2 loading happen while stopped.
- Fixed48 kHz and either256 or512 frames; other sizes are refused. The graph
  renders every source and processes the piano compressor once per whole block.
- Commands must name the exact current block boundary. Future, stale or
  sub-block commands are rejected, not rounded. This proves a v1-compatible
  cadence before adding deliberate sub-block semantics. It is NOT wired to
  the M1 sample-slicing scheduler or a live timestamp/driver adapter.
- Stage note ownership now actually drives oscillator and piano sources.
  Both pedals, held/released retriggers, transpose/octave and externally
  computed layer weights retain the tested v1.2 behavior.
- The oscillator uses fixed3-copy unison, waveforms/pan/octaves/blends,
  detune/spread, two polyphonic amplitude LFOs, shimmer and OSC pitch bend.
  There is no Python waveform fallback or supported1/5-unison path here yet.
- Prepared phase data is supplied explicitly; the fixture uses a saved
  512-row phase tape. Exhausted/nonfinite phase data causes terminal silence.
  This tests actual phase handoff, not a production random-generator/seed
  policy, global/key-synced LFOs or analog drift.
- Piano source uses v1-compatible gain1/int16 acquisition,32-voice capacity,
  original velocity shaping and velocity tracker, then the verified native
  chain. The M1 float acquisition is NOT silently substituted. SoundFont/
  program selection happens only at construction; live program changes and
  piano pitch bend remain pending.
- `ReleaseAll` resets stage key/pedal ownership and releases envelopes; piano
  clears pedal controllers, sends CC123 and resets its pitch bend. `stop()`
  instead hard-stops the owner, silences all stems and refuses resume. It is
  owner-only, not a thread-safe substitute for the scheduler's priority latch.

There is no master sum/limiter, piano room, main filter, global modulation,
pad-bus processing, complete effect/bed graph, recorder, browser or device
adapter. Internal oscillator stems can exceed1.0 (peak1.637 in this fixture).
That matches the reference before its downstream gain/limiter; **do not send
these stems directly to a DAC or export a clamped sum as a finished demo**.

## Actual comparisons and guards

Reference commit remains `57bb94cdbd3c9349fc2a47833d659451ebb8738b`.
`tools/compare_native_sources.py` uses pinned original MIDI dispatch,
voice/retrigger/stealing/envelope code, actual original Faust wrapper and
piano velocity/processing methods. Source files are AST-extracted; the old
application/runtime is never imported. Reference and candidate use separate
instances of the same pinned generated Faust DSP and installed FluidSynth.
Thus this verifies native integration/control/processing behavior under
matched dependencies, not cross-compiler/architecture or whole-runtime parity.

Each cadence runs327,680frames (6.827musical seconds), including chord/pedal
overlap,15-note overflow of the12-slot oscillator pool, held and releasing
retriggers, layer-weight changes and suppression, transposition/octave,
velocity-curve changes, oscillator/filter/compressor/brightness/tremolo/LFO/
shimmer controls, release-all and a long enough tail window to retire all
oscillator voices. Both sides consume exactly32 phase starts.

| Signal | 512-frame result | 256-frame result |
| --- | --- | --- |
| All five oscillator/shimmer stems | Sample-exact | Sample-exact |
| Raw stereo int16 piano | Exact,0 LSB difference | Exact,0 LSB difference |
| Processed piano worst absolute sample difference | 1.854e-13 | 1.840e-13 |
| Final active oscillator voices | 0 | 0 |
| Fluid errors, unmatched piano offs, full-scale int16 samples, phase/numeric errors | 0 | 0 |

Predeclared sample tolerance remains1e-6. Each block size matches its own
reference; **this is not a claim that256 equals512 or is playable on Pi4**.

Additional C++ guard tests exercise unsupported construction sizes, invalid
controls/events, layer isolation,20 dense chord/pedal cycles at each cadence,
phase exhaustion/corruption, repeated hard stop, no resume and silent outputs.
Calibrated C++ `new`/`new[]` probes record zero calls during the tested warmed
processing/configuration/stop sequence. C malloc, library locks, filesystem
access and execution deadlines are not certified by this probe.

UBSan passes with the C++ graph, piano-chain and components instrumented.
Generated Faust C and the external FluidSynth library remain uninstrumented.
ASan is still unverified; the previous pre-main issue was not reclassified.
Existing pinned component comparisons pass after sharing their voice-oracle
factory. The reviewed offline regression suite passes549tests, zero skips,
plus Node connection and syntax checks.

## A test assumption corrected, not a sound change

The first guard run expected a suppressed piano to be numerically zero and
failed. Actual idle FluidSynth int16 output has a1-LSB dither floor, confirmed
in the pinned reference. The corrected isolation test requires oscillator-only
piano output to match a separate idle piano exactly, including raw samples.
No noise gate, threshold relaxation on sound parity, or source mute was added.
Explicit terminal stop still requires exact zeros on all seven outputs.

## Saved evidence and reproduction

Mac arm64; Apple Clang17, Faust2.85.5, FluidSynth2.5.4. Private report, source
hashes, build commands, fixtures, phase tape, full reference/native source
arrays and binaries:

`Documents/stave-synth-pi4-backups/native-v2-m2-sources-20260917/report.json`

Report SHA256:
`6c6da3e2f3c53bcf78e1e101c908b2fdd43513a13c2525b65a479c13ffdf5d57`.
No audio/sample/private settings were committed.

```sh
python3 tools/compare_native_sources.py --soundfont /absolute/path/to/existing.sf2
```

Requires already-installed NumPy, CFFI, Faust, C/C++ and FluidSynth development
files. `--fluidsynth-prefix` selects an existing install. No packages are
installed. Reports require a new directory; the source/asset hashes must remain
unchanged throughout the run. Run only on the Mac or an authorized idle target.

## Next work, in order

1. Preserve downstream routing/gain: port the existing pad bus/main filter and
   piano-room path, then compare their actual outputs and release/toggle tails.
   Keep independent source stems; do not substitute one shared effect silently.
2. Complete source/control coverage: piano pitch bend and prepared program
   changes, modulation/drift/phase policy, supported unison paths and split
   calculation. Current explicit slice is not a full preset interpreter.
3. Integrate a bounded event/control adapter with an explicit block-boundary
   versus sub-block policy, priority stop acknowledgement and fail-silent
   telemetry. Preserve the compressor's full-block finalization and test
   boundary/panic interactions before claiming low-latency ownership complete.
4. Add worship FX, bed/drone/freeze, organ/recorder and scene transitions with
   their reference tests. Then arrange a confirmed off-stage Pi4 compilation/
   timing window. Browser parity, hardware response/recovery and player
   rehearsal remain promotion gates; the8-hour soak remains waived.

No test/compile job remains running at the saved checkpoint. The working Pi4
v1.2 snapshot and Pi5 branches remain untouched.
