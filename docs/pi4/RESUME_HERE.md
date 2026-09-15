# Resume here — Stave Pi4

Saved 2026-09-14 evening, America/Chicago (2026-09-15 UTC).
This is the current operational handoff, **not stage-release approval**.
Read this first; earlier review/deployment notes describe historical checkpoints.

## Scope and current ownership

- Work only on `pi4-stage-pro`. Do not merge/push/deploy onto Pi5 `main` or
  `mac-port`. The preserved Pi4 baseline is tag
  `pi4-stage-baseline-2026-09-14`, commit `d0142f0`.
- Mac checkout: `/Users/codyvanscyoc/Documents/stave-synth-pi4-stage`.
- Pi: `codyvanscyoc@stavepi4.local`.
- **The normal service runs from the Pi development checkout**
  `/home/codyvanscyoc/stave-synth-pi4-stage`, via installed user-service drop-in
  `90-pi4-stage-candidate.conf`. This checkout is not an idle test sandbox.
- Original `/home/codyvanscyoc/stave-synth` remains preserved; its existing
  `venv/bin/python` is also used by the candidate. Do not install dependencies
  or run the generic installer as a resume step.
- Latest application/DSP source change is **`483d953`**. Later commits
  `88c5a43`, `7a2f7dd`, and `e08175e` add audition tooling, tests and evidence,
  not a new synth DSP build. This checkpoint commit updates documentation only.
- No repair/capture job is left running. The normal stage service is running;
  the disposable audition service is stopped. Recheck live ownership before
  new work. Previous restart permission was for off-stage maintenance, not
  blanket permission to interrupt a future performance.

## Latest verified restart

User authorized a restart after the audition discussion. Normal service reached
active/running at **18:59:08 CDT on September 14**, PID **140111**, invocation
`20a85a4052d94260bfdd8f4a92e1d5a0`, zero automatic restarts at verification.
HTTP stage identity answered from the Mac; WebSocket native/audio/control/UI
health was good. Fluid piano loaded; graph 48 kHz / 512 frames / six ring slots.
Startup-inclusive snapshot: 3,052 callbacks, eight underruns, zero xruns. This
was a readiness check, not a new zero-underrun playing or soak qualification.

Saved `current_state.json` hash before and after restart remained:
`ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
Master remained 1. The Peavey interface and Yamaha keyboard were still absent;
USB enumeration showed root hubs and a VIA hub only.

## What is completed and where the evidence lives

- Full source review and coverage record: [COMPLETE_CODE_REVIEW.md](COMPLETE_CODE_REVIEW.md),
  [REVIEW_COVERAGE.md](REVIEW_COVERAGE.md), and `review-evidence/`.
- Implemented repair batches and finding-by-finding limitations:
  [REPAIR_PLAN.md](REPAIR_PLAN.md), [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
  Do not repeat the entire audit or mistake implementation for qualification.
- Pi native builds/hashes: [TARGET_BUILD.md](TARGET_BUILD.md). Sixteen target
  artifacts were verified. No native artifact changed for the latest GC fix
  or audition-tooling work.
- Startup, normal deployment, clean shutdown and original-source software
  rollback were exercised. The idle GC stall was reproduced, traced to six
  cyclic pointer conversions, corrected in `483d953`, and retested through
  cleanup for 120 seconds with zero new underruns/xruns:
  [STARTUP_CHECKPOINT.md](STARTUP_CHECKPOINT.md).
- Latest full Mac allowlisted suite: **325 tests, zero skips**, plus Node UI
  regression/syntax. Pi passed the 294-test runtime suite, the 29 initial
  audition tests and revised 12-test orchestration module. Do not claim a
  single full 325-test/Node run on Pi; Node is unavailable there.
- Five actual Pi-rendered MIDI recordings, numerical analysis, timing data and
  caveats: [DIGITAL_AUDITION.md](DIGITAL_AUDITION.md). MP3 was emailed to the
  user's chosen personal Gmail account with the attachment verified. The user
  listened and said the sound is close; this is valuable feedback, not a
  full hardware/performance sign-off.

## Still open — do not mark these fixed

1. **Piano continuity:** both final piano-only recordings have four exact
   512-frame (10.667 ms) silent blocks near chord changes. Nonblocking
   FluidSynth/native-lock contention is the leading explanation, **not yet
   instrumented/proven**. Zero bridge underruns do not detect these source
   gaps. Diagnose ownership, add regression evidence and recapture after a
   targeted fix. Preserve tone and sustain semantics.
2. **Worst-case render reserve:** layered tests averaged 7.48–7.77 ms against
   a 10.667 ms period. Shimmer's p99 bucket upper bound was 11.733 ms, with
   112 over-budget cycles; buffering covered them in that short test. The
   desired 30–40% demanding-patch reserve is not demonstrated. These are
   elapsed audio-render timings, not whole-Pi CPU percentages or proof of
   the hardware's absolute limit.
3. **Actual latency:** sub-15 ms controller-to-line-output is an aspiration,
   not a measured result. One 512-frame period is 10.667 ms; the three-block
   refill threshold is 32 ms of sample duration, **not total latency**.
   Measure the real keyboard/interface path. Do not lower buffers, voices,
   unison, sample rate or effects quality merely to make a number look better.
4. **Hardware and release gates:** actual Peavey/Yamaha route selection,
   sustained playing, pedal/USB recovery, Safari sleep/wake, cold boots,
   independent bed/full-core load, service-length rehearsal, eight-hour soak
   and whole-device recovery remain unqualified. The user plans hardware
   tests tomorrow, September 15. Follow [TEST_TONIGHT.md](TEST_TONIGHT.md) and
   [ENGINEERING_VALIDATION.md](ENGINEERING_VALIDATION.md); keep a known-good
   fallback available. No broad redesign is required to resume.

## Confirmed product choices

Preserve the loved sound and familiar five-fader workflow. Prioritize expressive
piano with OSC1/OSC2 blended live through filter/effect movement. A background
pad may be played oscillators, a sampled recording/bed, or tonic-and-fifth
ambience depending on the song; do not silently substitute one for another.
Presets are secondary. Keyboards vary (primarily Yamaha/Dexibell), and controllers
are mainly Apple devices. Peavey/Yamaha is the first reference rig, not a
hard-coded brand restriction. See [PRODUCT_VISION.md](PRODUCT_VISION.md).

The user reconfirmed **open browser control on trusted private Wi-Fi, no pairing
code**. Anyone who can reach Stave can control it; guest/public networks and
Internet exposure are outside the chosen operating boundary.

## Saved artifacts and recovery

- Local audio/evidence directory:
  `/Users/codyvanscyoc/Documents/stave-auditions-20260914.KlQVwd`.
  Includes five 24-bit WAVs, combined 4:35 MP3, listening guide, per-file
  analysis, raw float32 recordings and full capture/state manifests.
- Pi raw evidence:
  `/home/codyvanscyoc/stave-synth-pi4-backups/audition-20260914.LM69y4/captures-v2`.
  First incomplete-orchestration attempt is retained separately in `captures`;
  its audio finished but final health collection failed. Do not substitute it
  for the complete final batch.
- This private save checkpoint is indexed outside Git at
  `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/checkpoint-20260914.zAwHaf/INDEX.md`.
  Pi checkpoint directory:
  `/home/codyvanscyoc/stave-synth-pi4-backups/checkpoint-20260914.v8ZHlX`.
  Consult the private index for exact artifact hashes and restoration scope.
- Earlier baseline Git/runtime backups and the actual FluidR3+venv archive
  remain preserved: [BASELINE.md](BASELINE.md). They complement the latest
  candidate snapshot; none is a bootable SD image or a rehearsed full-OS restore.

Keep credentials, private runtime/settings archives, email metadata and raw
audio out of GitHub. Restore into a new staging location and validate it before
changing an active instrument. Never overwrite the preserved baseline or use
Pi5/Mac branches as a shortcut.
