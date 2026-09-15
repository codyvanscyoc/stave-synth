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

Target measurements and restored-service state must be appended after they
actually happen. At preparation time, the normal service was still PID151143,
active with zero restarts; no profiling/soak service had started.
