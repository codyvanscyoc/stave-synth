# Pi4 rehearsal candidate — September 15, 2026

The latest repairs are now **running**, not merely stored in an isolated test
checkout. This is permission to proceed to hardware rehearsal, not a claim of
flawless live operation or measured playing latency.

## Running build and preserved fallback

- Application: `bd6a51760d6fe10e2b61e462a276ee0604465cb0`.
- Normal unit: `stave-synth.service`, new working directory
  `/home/codyvanscyoc/stave-synth-pi4-rehearsal`.
- Only added service override: `95-pi4-continuity-candidate.conf`.
  It selects that directory and disables detailed diagnostics. Existing base
  unit, 90 override, Faust flags and memory drop-in are retained.
- Immediate fallback: preserved clean `ce15cfb` checkout
  `/home/codyvanscyoc/stave-synth-pi4-stage`. Original `stave-synth` is also intact.
- Verified PID 374463, invocation `c09b79d7548f4acc87bc81b92d7205fc`, zero restarts.
  Recheck these before any later intervention; they are a snapshot, not a handle
  to reuse blindly. Later documentation-only commits do not change app code.

Native profile remains 48 kHz, 512 frames, six ring slots, unchanged voices,
unison and effect quality. Fifteen native hashes match TARGET_BUILD.md; organ
uses the previously parity-verified Leslie STOP build:
`d7b1898096c0c9a0ec5d5e53e39bf85187ac2253fb95802e65bec7ff9b1d7455`.
No algebra/vector/silent-voice experimental organ library is installed.
Process maps confirm native libraries loaded from the new rehearsal directory.

## What was actually verified

- Full 546 Python tests on Mac and Pi, zero skips; Node UI/syntax on Mac.
- Native piano release comparison: 12 cases with sample-exact tails; details
  and full-app recordings in [PIANO_RELEASE_BOUNDS.md](PIANO_RELEASE_BOUNDS.md).
- Repeated ten-minute full-core musical run: zero buffer/source/event failures,
  finite/nonzero musical output, no clipping or throttling. Strict FAIL retains
  25 musical over-budget cycles and one later cycle. Post-ready RSS sampled
  312220→313116 KiB, maximum 313236 KiB; temperature 68.166–70.114 C, no swap.
  These short observations do not establish long-duration memory stability.
- Read-only candidate preflight: 88 passes, eight warnings, zero failures.
  Warnings include absent physical devices/preferred output, absent memory
  controller, pre-deployment checkout difference and static-vs-runtime limits.
- Corrected normal-service startup, actual rollback to ce15cfb, and corrected
  restart all reached verified HTTP/WS/native/audio/control/UI readiness.
- Final 120-second corrected normal-service idle observation: callback count
  71→11316; startup-inclusive underruns stayed 7→7, xruns 0→0; zero new
  over-budget cycles. It was idle, not a physical performance test.
- Normal current-state bytes remained unchanged throughout:
  `ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.

## Backup and rollback scope

Before activation, the stopped normal service's settings, data folders, base
unit and all drop-ins were archived, and the archive hash verified on Mac/Pi:
`16a91aaccdecdb8e1e6a43c0c21231297f536cb996d91dd26e1489916d8ef93d`.
This small snapshot retains the SoundFont symlink; the actual SoundFont and
venv are in the previously verified baseline backup. No SD-image recovery is
claimed. No test pad or private preset bank was installed into normal data.

In an authorized off-stage window, rollback means stop the normal service,
move **only the owned 95 override** into the private backup directory, reload
the user manager and restart. The retained 90 override then selects ce15cfb.
Verify file hash/ownership and destination first. Do not delete all drop-ins,
reset a repository or overwrite user settings. This exact software path was
exercised during activation. Old capture wrappers still assume ce15cfb is
active and must be adapted before reuse.

## Remaining boundaries

The player declined the eight-hour soak for this release effort. It is not
scheduled, required from the player, or recorded as passed. Use bounded
automated tests and actual rehearsal; retain long-run reliability uncertainty.

Heavy organ computation remains approximately 9.9 ms per 10.67 ms block.
Broader scheduling/headroom optimization is unfinished. Existing envelope,
selective-bus and engine-spillover design work remains outside the pre-show
tonal freeze, not silently completed. Smaller buffers have not been qualified.

Next: connect the actual compatible USB keyboard/interface and verify route,
sustain, busiest layered patch, STOP, recorded bed, Safari reconnect and
physical playing latency. See [the hands-on sequence](TEST_TONIGHT.md).
Open control on trusted private Wi-Fi remains the chosen policy.
