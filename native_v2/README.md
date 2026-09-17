# Stave native-v2: offline foundation, not the stage application

This is an opt-in C++17 prototype on `pi4-native-engine-v2`. The working
instrument is preserved separately as `pi4-v1.2-stage-snapshot-20260916` on the
Pi4 stage line. Nothing here installs a service or changes the normal synth.
See [the implementation plan](../docs/pi4/NATIVE_V2_PLAN.md) for promotion gates.
See [the saved checkpoint](../docs/pi4/NATIVE_V2_CHECKPOINT.md) for actual test
results, artifact locations and the unresolved sanitizer check.

## Run only offline

From this checkout, using already-installed dependencies:

```sh
python3 tools/run_native_v2.py
python3 tools/run_native_v2.py --sanitize
python3 tools/run_native_v2.py --ubsan
python3 tools/run_native_v2.py --with-sound
python3 tools/run_native_v2.py --with-sound --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_native_v2.py --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_native_sources.py --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_native_room.py
python3 tools/compare_native_buses.py
python3 tools/compare_source_mix.py
python3 tools/compare_native_core.py --soundfont /absolute/path/to/existing.sf2
python3 tools/compare_shared_effects.py
python3 tools/compare_pad_ambience.py --source-dir /absolute/path/to/seven-stem-source-evidence
python3 tools/compare_stage_master.py --ambience-dir /absolute/path/to/six-channel-ambience-evidence
python3 tools/compare_stage_instrument.py --soundfont /absolute/path/to/existing.sf2
```

Core tests require a C++17 compiler and lock-free 64-bit atomics. Sound tests
also require Faust, its C headers and a C compiler. An explicit SoundFont
requires FluidSynth headers/library; no sample is bundled or downloaded. Use
`--faust-prefix` / `--fluidsynth-prefix` for nonstandard installed prefixes.
Missing requested dependencies fail explicitly: a requested piano is never
silently replaced with an oscillator. Nothing opens an audio/MIDI device,
network listener, normal state path or existing application module.

Artifacts go into a new temporary directory (printed at startup), or a NEW
`--output-dir` whose parent already exists. Existing paths are refused. Keep
`report.json` and its binaries/WAVs together. The report records commands,
source hashes and host identity, and rejects source changes during the run.
The two eight-second float WAVs are dry diagnostic fixtures, not v1.2 demos.
`--sanitize` and `--ubsan` instrument scheduler and envelope/note/voice tests
only, not generated Faust/FluidSynth/piano-chain tests. They are mutually
exclusive; UBSan-only has passed, ASan remains unverified.
The separate source-graph runner instruments its C++ graph and piano chain
with UBSan too; its generated C and external FluidSynth remain uninstrumented.
The room comparator similarly instruments its C++ wrapper/guards, not Faust C.
The buses comparator composes pad/filter/send routing with room and instruments
that C++ path/guards; generated Faust remains uninstrumented.
The composed-core comparator also covers actual sources, faders, buses, room
and fixed-three-unison all-muted/re-entry behavior; its C++ guards use UBSan.
Shared-effects and pad-ambience runners cover delay, all reverb types/freeze and
pad routing; their C++ guards also use UBSan, not their generated Faust C.
The master runner additionally requires SciPy for the original RMS oracle;
it covers both limiter policies, sidechains and master routing before bridge
gain. Its C++ owner/guards use UBSan, not generated Faust C.
The connected-instrument runner also requires SciPy and an explicit SF2.
It currently exits 1: the original-source fixture passes, but a high-gain
piano stress case exceeds the unchanged strict sound-comparison tolerance.
It retains the full independent/common-input diagnostic matrix; see
[integration evidence](../docs/pi4/NATIVE_V2_INSTRUMENT.md). This is not an
all-tests-pass or field-testable successor checkpoint.
Never run these compilers/tests on a playing Pi: offline means no devices,
not zero CPU or memory contention.

## Command and ownership contract

The following is the M1 `Engine` contract. New `StageSources` and `StageCore` admit
only exact current-block-boundary commands and renders complete blocks. It
does not implement `Backend` and must not be called once per event slice.
See [source integration](../docs/pi4/NATIVE_V2_SOURCE_GRAPH.md) for that contract.

- One producer calls `Engine::enqueue`; one audio owner calls `process`.
  Backend construction/destruction happens while that owner is stopped.
- The fixed 256-entry SPSC queue accepts nondecreasing absolute frame times;
  equal-time events retain submission order. Each process call consumes at
  most its initial queue snapshot. Later arrivals wait for the next call.
- Supported commands: note-on/off, sustain, OSC1/OSC2 blend and scheduled
  panic. MIDI velocity-zero note-on releases. Blend/sustain values are finite
  and in `[0,1]`; external signed/JSON fields must be validated before casting.
- Sample-position dispatch subdivides each block. An event exactly at its end
  belongs to the next block; late events clamp to its beginning and are counted.
  FluidSynth's internal render block still constrains piano onset. Sample-timed
  command delivery is NOT a physical latency or sample-exact piano claim.
- Queue overflow is a terminal engine fault: the audio owner panics and
  silences output; no unsafe concurrent reset or stale-event resume. Reconstruct
  only while stopped. Invalid process arguments return false without touching
  buffers; a future driver must implement its own invalid-call silence.
- Scheduled panic remains an ordered musical command. `request_stop()` is a
  separate priority terminal fault latch which bypasses future event ordering;
  only the audio owner touches the backend. It cannot preempt an executing
  backend call. It requires stopped reconstruction, not live reset. Full-source
  tail policy and the browser STOP adapter still need integration.
- Engine telemetry is atomic but not a transactionally coherent snapshot.
  `SoundBackend::stats`, health and slot counts belong to the audio owner;
  future UI access needs an explicit telemetry transfer, not concurrent reads.
- Backend failures and engine faults are separate. The offline harness checks
  both. A live adapter must propagate both truthfully into readiness/recovery.

## Feature ledger

| Area | Implemented in this prototype | Not yet migrated or qualified |
| --- | --- | --- |
| Timing | Bounded event scheduling, priority terminal stop, fault tests, single audio owner | Live MIDI timestamps, control coalescing, driver/recovery, whole-block postprocessing integration |
| Oscillators | Integrated v1.2 voices/envelopes and actual12-slot/3-unison Faust; prepared phases/amplitudes and poly amp LFOs; StageCore owns faders, mute/re-entry and filter/Haas/send/bypass/shimmer buses | Global modulation/drift, production phase policy,1/5-unison paths, click fixes, full FX graph |
| Piano | StageCore owns real int16 FluidSynth, velocity/pedal ownership, matched dry piano chain and downstream room; separate M1 float experiment | Pitch bend, live prepared program changes, library-internal real-time audit |
| Shared effects | Native delay/reverse/Aurora; seven reverb types/freeze; source-owned pad/filter/returns, wet filter and piano sends | Global modulation/sympathetic, overload sound-difference review, click/transition redesign |
| Stage keys | Integrated raw-key/transpose/sustain/sostenuto ownership and supplied layer weights | Split-weight calculation and live timestamped MIDI protocol |
| Output | StageInstrument owns source/FX/master composition; original-source fixture passes; explicit limiter/zero-knee corrections retained | High-gain stress sound gate, final bridge gain, live backend, end-to-end latency |
| Worship functions | None silently removed from the preserved working build | Independent sampled bed/drone, freeze, organ, recorder, splits, macros, scenes |
| Browser/state | Existing implementation retained as reference | Versioned native protocol, preset conversion, five-fader UI integration |
| Qualification | Offline tests and diagnostic render only | Pi4 build/timing, sound parity, actual hardware/rehearsal acceptance |

StageCore produces eleven internal channels, not a finished stereo master.
Its bounded post-warmup exercise observes zero C++ `new`/`new[]` allocations;
this does not audit dependency C allocation, locks or real-time deadlines.

The prototype uses deterministic oscillator phase, simplified gates, no v1.2
effects, and different piano gain/precision. Do not compare its raw loudness or
timbre with the stage instrument and call the difference an improvement.
Reuse of DSP source alone does not preserve a complete sound.

That paragraph describes the still-existing M1 `SoundBackend` / `render_demo`.
`BlockEnvelope`, `StageNotes`, `StageVoices` and `PianoChain` now combine in
`StageSources`, not in that demo. Its seven stems are internal signals, not a
finished master output. See the [source checkpoint](../docs/pi4/NATIVE_V2_SOURCE_GRAPH.md)
for measured comparison scope, compatibility quirks and remaining integration.

## What the tests establish

The core suite checks exact event placement, equal/future/end/late ordering,
256/512 scheduling equivalence, validation, full-queue fault silence, frame
overflow, bounded initial snapshots and a 20,000-event concurrent SPSC exercise.
Its allocation probe checks C++ `new`/`new[]` in the engine plus a fixed fake
backend only. It does not prove third-party code never calls malloc, blocks,
logs or misses a deadline. Sanitizers are correctness checks, not timing tests.

Sound tests exercise actual generated Faust, optional actual FluidSynth,
note/pedal/channel handling, repeated-note stealing, render bounds, reported
clipping, 256/512 fixture equivalence, and post-panic silence. Missing-SF2
diagnostic errors are expected in the explicit dependency-failure test.
The wrapper also checks direct WAV overwrite/symlink refusal.

Keep the existing device-free regression suite separate:

```sh
python3 tools/run_offline_tests.py
```

Do not discover/run arbitrary legacy stress tests. See
[engineering validation](../docs/pi4/ENGINEERING_VALIDATION.md).

## Next: prove musical compatibility before adding breadth

The owned source/fader/filter/room composition and its all-muted transitions
pass; see [core evidence](../docs/pi4/NATIVE_V2_CORE.md). Separate native shared
effects and composed pad routing now also pass; see
[shared-effects evidence](../docs/pi4/NATIVE_V2_SHARED_EFFECTS.md). The separate
master chain also passes; see [master evidence](../docs/pi4/NATIVE_V2_MASTER.md).
StageInstrument now joins actual sources and returns without duplicate PadBus
processing, including wet-output filtering and piano sends. Its added overload
sound gate remains open; do not replace it with the passing common-input
diagnostic. See [integration evidence](../docs/pi4/NATIVE_V2_INSTRUMENT.md).
Next resolve that difference, then complete global modulation, final output and
control coverage and the event adapter. Keep whole-block piano
processing separate from MIDI event slicing; do not silently change the proven
cadence semantics. Bring effects and beds across only with their
tail/transition tests. Pi4 speed measurements and any deployment require a
separate off-stage window; no claim here establishes that 256 frames will work.
