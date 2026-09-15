# Piano continuity and native preparation — Pi4

This is a focused reliability/efficiency batch following saved checkpoint
`ce15cfb`. It is not a new sound design, buffer profile or stage-release sign-off.
Keep the five-fader performance surface, existing patches, oscillator count,
sample rate, effects algorithms and musical gain structure unchanged.

## Reproduced mechanism

The September 14 piano-only captures exposed complete 512-frame zero blocks
near chord onsets despite no new bridge underruns. An independent offline
reproducer extracted the actual `note_on` and `render_block` methods from
`ce15cfb`, held a fake native note-on with an event on the MIDI thread, then
called the production renderer. It returned exactly 512 stereo-zero frames
without calling `get_samples`. This proves the lock-contention failure path;
the original target recordings were not instrumented to prove each gap's cause.

## Candidate behavior

- Musical piano note-on/off/pitch commands use a bounded FIFO, applied before
  sampling and before the idle gate. The render path must not wait for the
  event-queue lock; a short contention defers commands, not existing audio.
- The native lock still coordinates loading, program selection and shutdown.
  Rare native-transition lock misses remain possible and are now counted.
  Seamless arbitrary program changes are not certified by this patch.
- Synchronous panic keeps its existing ordering before JACK ring clearing and
  cancels stale pending musical commands. Overflow/error recovery must be
  bounded, explicit and observable; it is a failed qualification condition,
  not evidence that losing musical events is acceptable.
- [FluidSynth's MIDI API](https://www.fluidsynth.org/api/group__midi__messages.html)
  allows note-off failure to mean no matching voice. That case must not be
  confused with an exception or an unsuccessful channel recovery.
- The native oscillator path skips fallback-only detune/pan, phase-increment,
  shimmer-scratch and gain-array preparation. Native parameters, random draws,
  envelopes and Python fallback formulas remain unchanged. Tests of extracted
  preparation do not prove full hardware-output parity or target speedup.

## Qualification rules

The repaired-candidate audition tool requires piano-source counters. Missing,
malformed, reset or growing failure counters cannot become implicit zeros.
Manifest format 2 records that requirement. Use the preserved older tool for
old-build baseline captures; older recordings remain valid historical evidence,
not evidence that newly required source counters were measured.

Retain terminal panic/setup effects in raw counters and report them separately
from continuous playing. Do not discard a native-lock miss because its cause
was an intentional stop. Queue-lock deferrals are not event loss, but physical
latency and heavy-event render cost still need measurement.

Before stage deployment, repeat piano-only and layered/automated recordings on
the actual Pi in an isolated instance, compare continuity, source/bridge
counters and timing distributions, and preserve original saved state. Actual
Peavey/Yamaha playing, latency, recovery and rehearsal remain required.

The shimmer take changes three settings per automation tick; its previous
112 over-budget cycles cannot be attributed to shimmer DSP alone. If reserve
remains insufficient, split wall-time from render-thread CPU time and profile
individual stages before reducing quality or changing a buffer setting.

## Evidence locations

Pre-repair private checkpoint:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/checkpoint-20260914.zAwHaf/INDEX.md`.

Reserved candidate evidence directories (results must be verified separately):

- Mac: `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/continuity-20260914.MvFj78`.
- Pi: `/home/codyvanscyoc/stave-synth-pi4-backups/continuity-20260914.bSKT8q`.

No raw recordings, copied private settings or credentials belong in Git.
