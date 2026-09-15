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

## Subsequent work in progress — not deployed

Investigating ignored organ Leslie STOP (both wrappers accepted only slow/fast)
and finer opt-in CPU/wall attribution. STOP must use0Hz through unchanged rotor
ramps, with declared native slider minimum0; it requires an organ-only target
rebuild and native parity checks. Do not claim new organ binaries are tested
until that rebuild completes. Source/library changes stay in the isolated
candidate; normal ce15cfb source and production settings remain protected.
