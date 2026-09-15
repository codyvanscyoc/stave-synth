# Bounded piano release — September 15

Application correction `bd6a517`. This is a continuity/overhead repair, not a
new sound, increased polyphony or proof of hardware latency.

## Correction and direct native comparison

The synchronous piano release held native ownership across 128 individual
note-off calls plus pedal/pitch resets. It now clears sustain and sostenuto,
sends channel-zero CC123, then resets pitch: four checked native calls.
Queue cancellation, native ownership, failure latching, room/comp cleanup and
error reporting remain unchanged. It does not substitute hard sound-off CC120.
FluidSynth documents channel-wide note release separately from immediate
[all-sounds-off](https://www.fluidsynth.org/api/group__midi__messages.html).

Two standalone native FluidSynth instances on the Pi used the existing FluidR3
asset, 48 kHz, unchanged 32-voice profile, gain 1, resident samples and disabled
internal reverb/chorus (the application's configuration). No audio/MIDI driver
or Stave service was started by the comparison. Programs 0/4 each exercised
held notes, sustain, sostenuto, both pedals, repeated notes and range extremes.
Four repeats per case compared the old loop with the compact release.

All 12 cases had sample-exact pre-release audio and sample-exact 2.048-second
int16 release tails; maximum error 0, matching accumulated tail hashes.
Per-case median release thread CPU dropped from 0.555–0.596 ms to 0.047–0.078 ms.
These numbers time the direct release operations, not the entire panic path,
lock scheduling, DSP processing or physical controller latency.

The full 546 Python regression tests passed on Mac and Pi, no skips (Pi 57.203 s).
Mac Node checks passed. Three added tests cover exact ordered calls/no CC120,
native status failures and exceptions without skipping the remaining reset.

## Actual full-app recordings

Two 55-second private native performance captures used the corrected app and
the existing full-quality 48 kHz/512-frame/six-slot profile. Neither had any
piano-source lock misses. All functional steps and state restoration completed.

| Observation | Performance5 | Performance6 |
| --- | ---: | ---: |
| Piano source misses, entire capture |0|0|
| Bridge underruns before deliberate terminal panic |0|0|
| Over-budget cycles before terminal panic |65|66|
| Total over-budget cycles |68|68|
| Total bridge-underrun growth |1|2|
| Strict result |FAIL|FAIL|

Do not hide total failures. The remaining bridge growth occurred after the
pre-panic checkpoint, but its window alone does not prove every event's cause.
The app deliberately clears queued output at panic. Mixed output cannot prove
each individual voice's audibility. No causal claim that the small release
repair fixes general scheduling or organ computation follows from these takes.

## Interpreting an over-budget cycle

One 512-frame block lasts 10.6667 ms at 48 kHz. `render.over_budget_count` counts
cycles whose wall time exceeds that period. This is not itself a count of
missed DAC deadlines: queued audio can cover occasional slow cycles, provided
the producer catches up. Preserve over-budget measurements alongside actual
bridge underruns, source failures, queue fill, CPU time and hardware latency.
Do not rewrite strict failures into passes or equate every period overrun with
an audible dropout. Heavy organ windows remain approximately 9.9 ms/block.

## Repeated ten-minute combined-core run

`core-rehearsal-2` used the same musical fixture, independent sampled bed,
two control peers and bounded filter/effect movement with diagnostics disabled.
All 56,250 musical blocks were nonzero and finite, with no clipped samples.
The driver emitted 600 musical MIDI events plus three terminal releases.
Application piano events enqueued/applied: 500/500, with no discarded events.

The whole observation (including terminal release) had zero bridge underruns,
xruns, source-lock misses, native errors, event drops, invalid render samples
or rejected writes. Strict FAIL retains 25 musical over-budget cycles and one
additional terminal-window over-budget cycle. This is not a zero-overrun pass.
Peak was 0.0575649589. No swap or throttling was reported. Scene restoration
and automatic normal-service restoration completed successfully.

The tool's historical long-duration memory gate remains insufficient for a
ten-minute run; it was not relabeled passed to satisfy the revised scope.

The player declined the eight-hour soak for this release effort. Shorter
automated runs plus an actual hardware rehearsal remain the current scope;
long-duration production reliability is unqualified, not silently passed.
