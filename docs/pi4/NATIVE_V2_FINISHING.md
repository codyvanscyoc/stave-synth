# After the accepted native listening test

September17,2026. The player reported "phenomenal" sound, effectively instant
response, and no heard issues in the first limited512 audition. Keep512 as
the default;256 is optional future exploration, not the next release gate.
The user now permits UI redesign to serve musical control and playability.
Independent sampled beds/drone and recording are no longer deferred.

## Latest result: maintenance and target tests completed

The user subsequently confirmed finished playing/muted. Piano brightness now
passes the target build and a94.299-second muted real-driver sweep. The bed
is integrated before the native master/limiter and passes combined Pi offline
benchmarks, but the live host still does NOT load a pad library or expose bed
controls. Recording has a tested native capture transport connected optionally
to the graph tap, NOT a finished file writer or UI. The working service is
restored; temporary8082 audition is not running. See the authoritative
[bed/capture checkpoint](NATIVE_V2_BED_CAPTURE.md).

Steps1 and the graph-mixing part of2 below are complete. Continue with immutable
asset preparation/handoff, recorder finalization, product UI and qualification.
The previous local-only results below are retained as historical evidence.

## Preserved listening reference

Annotated tag `pi4-native-listening-pass-20260917` points to8c1a88a (source and
earlier target evidence). Actual audition engine/UI source5a22fbe; unchanged
target executable SHA256
`900a753f10108f0dd708f9dd27881c099d4c7d8c0b15aadc6fcb319b4e75ed29`.
The original v1.2 tag/stage branch and Pi5 branches are untouched.

A read-only snapshot after the player's feedback recorded1,145 note-ons,
92,230 blocks,441 acknowledged controls,0 xruns,0 over-budget callbacks,
0 raw-piano full-scale samples, maximum callback4.90224ms, no fault/staleness.
265 unsupported channel-MIDI messages were counted, not implemented. These
are cumulative sampled counters, not an exact wall duration or a physical
latency measurement. The private `player-listening-accepted.json` preserves
the settings and counters in the target-audition backup directory. Never
blindly replay the saved release action or unmute master automatically.

## Earlier local implementation batch (superseded by target tests above)

Piano-brightness commit8b7cdc1; sampled-bed component commit8db3254.
Reviewed Mac offline suite:567 tests, zero skips, plus Node UI checks,
14.502seconds. The initial restricted run failed because the sandbox disallowed
localhost binds; rerun with that permission passed. No test was skipped to
obtain a green result. Additional real-graph C++/UBSan audition/fake-JACK/piano
guards passed using hash-verified existing DSP objects, rebuilt current owners.
Private report `native-v2-piano-tone-final-20260917/report.json` SHA256:
`372c8ec9b25e9ac6b7db52c3d3750b4c2a0377f91f37b2690ec90783c814bc9a`.
These new changes remain LOCAL/SOURCE ONLY until a separate target build/test.

### Piano brightness

`piano_tone` is an acknowledged0..1 control:200Hz at dark,2kHz at midpoint,
20kHz at bright. It reuses the existing piano-only24dB/oct high-cut, before
piano room/sends. It does not move the oscillator filter.80ms log-frequency
smoothing is enabled only for the new audition; legacy component configurations
default to instantaneous changes, retaining their reference behavior.
No added voice, DSP effect, render queue, file access or allocation on this path.
The fully bright/untouched setting has exact PCM equality to the old default
in the component tests. A live sweep still needs target/listening confirmation.

Mac UBSan tests pass: exact unchanged default, monotonic smoothing at512/256,
frequency attenuation, unchanged legacy immediate mode, invalid input and
zero scoped C++new/new[] while sweeping. Real-graph audition commands match
the explicitly configured reference, including darker/brighter changes; fake
JACK lifecycle guards pass. The test tools now include the piano guard, so
future target builds must exercise it rather than relying on a Mac result.

### Sampled bed component (not live integration)

`sampled_bed.hpp` ports the original SamplePlayer loop,4s attack/release,
500ms-or-quarter-length equal-power overlap and rise volume/filter envelope.
Twelve independent concert-key sample slots; key changes release other slots;
no keyboard voice allocation or transposition. No invented synth replacement
for a missing recording. Native-rate48k double PCM is prepared before sealing
the bank. Overlap is baked once, removing runtime sin/cos per overlap sample.
32MiB/slot,128MiB bank cap; a lower caller-specified bank cap is supported.
These bound resident sample storage, not total application/preparation memory.
Double stereo storage may accept shorter assets than the old float32 path at
the same byte cap; format/storage policy remains an integration decision.

After sealing, loading/replacing slots is refused; performance methods never
allocate/free sample assets. No file decoder, recorder, live asset handoff,
main-graph mixing, control protocol or UI for this component is claimed yet.
Existing v1.2 remains the full-feature fallback until those are integrated.

Independent original Python SamplePlayer/BiquadLowpass source is extracted for
an offline oracle without importing the application. Twelve fixtures cover
512/256,4/37/100,000-frame samples, normal/rise modes, multiple wraps per block,
key changes, release during rise, retrigger and STOP:850 blocks each,
10,200 blocks total. Each passes a predeclared max absolute1e-10 comparison.
The native fixture uses UBSan and checks invalid size/rate/PCM, memory caps,
transactional ownership, seal refusal, missing slots, overlapping buffers,
terminal numerical faults and zero scoped C++new/new[] during playback.
This is sound/component evidence, not Pi4 combined-workload qualification.

## Finish in this order

1. Build/test piano brightness on Pi in a confirmed muted maintenance window,
   using a NEW isolated directory. Preserve the accepted binary and settings.
2. Integrate sampled bed into the native graph BEFORE master/limiter, preserving
   existing dry/FX routing and independent level/rise/mellow/fade behavior.
   Add bounded immutable asset handoff with off-audio preparation/retirement;
   reuse validated WAV/file-transaction semantics, not runtime AST extraction.
3. Add recorder using the existing native recording tap, a bounded producer
   queue and a disk worker. Slow/full storage must report failure without
   blocking audio. Connect recorded-take-to-pad workflow with explicit key and
   transactional save; do not claim this works because playback alone passes.
4. Replace the temporary panel with acknowledged Stage/Edit/System controls,
   saved-state/preset compatibility and clear OSC2/bed/brightness visibility.
   Port remaining required controls (organ/programs, pitch bend, mappings,
   macros/scenes) explicitly; keep a feature ledger rather than silently drop
   them. No framework rewrite is required for audio responsiveness.
5. Add a reversible512 stage candidate and test missing/late/reconnected MIDI,
   selected audio output, network/UI recovery and startup. Browser/server
   recovery must not unnecessarily restart a healthy audio owner. Restore
   normal hostname access with an explicit Host/Origin policy.
6. Repeat bounded core-load/transition tests with bed/recorder/UI together,
   then player rehearsal. No eight-hour soak requested. Keep the old working
   service recoverable until the combined candidate passes.

Do not label the successor finished/stage-qualified while3–6 remain open.
Independent high-gain numerical parity and dense raw-piano headroom warnings
remain documented; the successful musical test does not erase those cases.
