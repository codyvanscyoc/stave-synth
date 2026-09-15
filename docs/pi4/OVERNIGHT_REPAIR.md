# Remote continuation — September 14 evening

The player is away from the hardware tonight; all existing functions are in
scope for tomorrow night. Continue remote software/native integration work.
Device discovery must use OS capabilities, not Yamaha/Peavey allowlists. This
does not certify every interface, driver, hub/power path or analogue latency.
Preserve the normal service and baseline; do not redesign or cut sound quality.

## Implemented candidate changes

- Generic MIDI selection admits explicitly typed physical output ports beyond
  the recognized bridge names. Inputs, conflicting direction, untyped or
  nonphysical arbitrary application sources, MIDI Through and the exact own
  client are excluded. Existing bridge alias policy remains. Audio stereo-pair
  selection is unchanged; arbitrary AUX/multichannel names are not guessed.
- Bed FADE toggles its intended endpoint, not its instantaneous ramp gain;
  explicit boolean targets are honored, optional null retains toggle semantics.
  The target hydrates on reconnect and resets after panic. UI clicks remain
  server-owned toggles; old acknowledgements cannot overwrite later intent.
  Existing S-curve, duration, sound and five-fader layout remain unchanged.
- Exact `STAVE_DIAGNOSTICS=1` enables bounded piano native-owner miss records,
  reverb lock/wall/calling-thread CPU statistics, and whole-render/stage timing.
  Default operation is unprofiled. Diagnostic writers never wait for snapshots.
- The isolated MIDI capture driver records separate MIDI/capture first-callback
  CLOCK_MONOTONIC timestamps, including failed reports. Python automation also
  records send/ack monotonic timestamps. The audio/MIDI stimulus is unchanged.

## Evidence interpretation

Piano owner attribution must bracket the failed native try-lock with the same
immutable owner generation; races stay unknown. Records can be overwritten or
dropped and their counters must remain visible. A miss caused by deliberate
STOP is not the same as one during playing, but neither is silently removed
from the raw report or retroactively used to pass an old failed capture.

Calling-thread CPU excludes helper threads. Wall minus CPU alone does not
distinguish scheduler, GIL and mutex delay. Render stages are coarse:
`piano_and_sends` includes pre-piano housekeeping; `synth` includes the synth
engine; `master_and_write` stops before telemetry bookkeeping. Inter-render
gaps include intentional ring-fill sleeps, not just scheduling delay.

The separate native callback timestamps are execution instants, not exact JACK
cycle-start or DAC timestamps. MIDI frame zero and capture frame zero have
different anchors; retain their difference and up-to-block callback uncertainty.
They are not a physical key-to-output latency measurement. Profiled timings are
perturbed by instrumentation; compare enabled/disabled runs before any reserve
claim. No quality/buffer/rate/unison/profile change is part of this batch.

## Target work

Use a fresh detached private worktree and copied configuration/data. Normal
stage stays at `ce15cfb` / application `483d953` until explicit promotion.
Rebuild only the changed capture executable; verify the 16 unchanged native
DSP/bridge hashes. Re-run exact targeted regressions before an isolated start.
Repeat filter captures and heavy shimmer with diagnostics, retain all raw
counters, then use evidence to choose the narrow timing correction. Exercise
recorder-to-pad, bed fade, freeze, presets, organ/splits/macros in that isolated
native application; do not substitute schema tests for audible integration.

Private evidence roots:

- Mac: `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/profiling-20260914.mMzp18`.
- Pi: `/home/codyvanscyoc/stave-synth-pi4-backups/profiling-20260914.J0bzpM`.

## Completed remote checkpoints

Candidate `0272c3d` completed seven diagnostics-on 55-second captures: strict
PASS for piano reference and warm layers; strict FAIL for filter, shimmer,
open-tone and both filter repeats. Their render-over-budget deltas were
0/0/4/48/1/6/3. All seven measured take intervals had zero bridge-underrun,
xrun and dropped-MIDI growth. **The session was not zero-underrun:** counters
rose between takes/setup operations, which remain separate evidence.

The only new piano source-lock miss was positively attributed to
`all_notes_off` at MIDI-callback-relative +46.999558 seconds, matching the
deliberate terminal CC123. Both filter repeats had no new source misses.
No new exact-zero runs of at least128 frames began in the musical [2,47)
second interval in any WAV; layered output can conceal an individual silent
source, so this is not an unconditional continuity or quality pass.
Initial digital onset silence extended roughly28–39ms beyond the first MIDI
callback-relative scheduled note in six takes. This is not analogue latency,
but it is additional evidence that the sub15ms aspiration is not established.

Filter mean render wall/thread CPU were approximately6.70/6.61ms, shimmer
6.86/6.69ms per10.667ms block. Rare wall spikes still exceeded the block period.
The worst repeated-filter control acknowledgement took40.645ms; a render
stall overlapped it, but causality is not established. No voice/quality/buffer
cut was made. Private detailed measurements/hashes: `SEVEN_TAKE_EVIDENCE.md`
in the Mac profiling root above.

`ec12fb0` adds a bounded native recorder-to-pad feature probe; `829a3c0` skips
only mathematically identity per-OSC recombination when no routed Python-side
amp/pan modulation consumes it. Active modulation/DSP/tails/RNG remain intact.
413 Mac tests plus Node UI/syntax and **413 Pi Python tests, zero skips**
(45.214s) passed; all16 unchanged native hashes verified.

Two829a3c0 filter captures each had two over-budget cycles and no other strict
failure. The first actual recorder/bed integration completed every functional
step: recorded a settled chord, verified finalized audio/metadata, loaded the
new take into an empty C slot, triggered the sampled bed beneath played layers,
reversed its fade, checked fresh-connection hydration, and verified panic.
It restored all touched settings with no restoration failures and retained
the private new take/slot. Its WAV is55seconds, entirely finite, peak0.053928.
**Its raw verdict remains FAIL:** eight over-budget cycles and one bridge
underrun. The pre-panic snapshot had seven over-budget cycles and no new
underrun; precise attribution of the later underrun is not yet established.
This is functional integration evidence, not stage-release approval.

Both bounded experiment services stopped and restored the normal service.
Latest restoration: normal source clean at`ce15cfb`, active PID170746,
invocation`8855c114ca2a42c9ad0590e98b85a68d`, zero restarts. Normal saved-state
SHA256 remains`ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
HTTP/WS recheck and newer work checkpoints may supersede this PID.

## Subsequent isolated verification — not deployed

The Leslie STOP correction and organ-only native rebuild are complete.
[ORGAN_STOP.md](ORGAN_STOP.md) records exact target slow/fast sample parity,
finite zero-speed target behavior, and the single native-manifest delta.
Only the private audition source has the rebuilt organ; normal stage remains
`ce15cfb` with its original native libraries and saved state.

`8ef73e7` passed 445 Mac and 445 Pi Python tests, zero skips. Four subsequent
55-second filter/shimmer captures compare diagnostics on/off. All fail only
render-over-budget growth: on +1/+23, off +2/+29. Each take has zero new bridge
underruns/xruns and no new reported piano source miss. Mean render-metric
times were on 6.592/6.782 ms and off 6.432/6.625 ms; extra diagnostics are not
the whole cause of remaining spikes. Fine spans indicate much smaller CPU
than elapsed time on the worst blocks. No GIL/autosave cause is proven yet.
Private `DETAILED_TIMING_EVIDENCE.md` preserves exact boundaries and caveats.

`133fe04` adds the 55-second performance-controls probe and organ evidence.
460 Mac tests plus Node UI/syntax and 32 focused Pi tests passed. Its actual
Pi integration completed Fluid/Rhodes, piano/organ, Leslie slow/fast/stop,
held-note transpose, raw-key splits, freeze and panic. Full state restoration
succeeded. WAV: 2,640,000 finite stereo frames, peak 0.068927. **Strict FAIL:**
84 over-budget cycles, two bridge underruns, 23 piano source-lock misses.
Before the terminal panic: 80 over-budget cycles, no new bridge underrun,
22 source-lock misses. All failures remain in the raw evidence.

Twenty-one source misses are positively attributed to `program_change`
during Fluid→Rhodes (225.798 ms command acknowledgement); one to the return
to Fluid (21.338 ms). The remaining miss is `all_notes_off` at terminal CC123.
The Pi4 LOW_RAM profile enables FluidSynth dynamic sample loading, which
loads/unloads samples when changing programs. Its memory-allocation path is
[documented as non-real-time-safe](https://github.com/FluidSynth/fluidsynth/blob/master/doc/fluidsettings.xml).
Installed target FluidSynth is 2.4.4. Full-bank residency is the next narrowly
scoped repair to verify; do not confuse a loaded bank index with resident
program samples or claim seamless program switching from this failed take.

After this take, normal stage restored active at PID 185984, invocation
`9811c1d89eef41b1bc96764930db6879`, zero restarts, clean `ce15cfb`, saved-state
hash unchanged. Its native/audio/control/UI health was rechecked successfully.
Performance unit stopped. No eight-hour soak has started or passed yet.
