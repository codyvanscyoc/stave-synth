# Resume here — Stave Pi4

Saved 2026-09-14 evening, America/Chicago (2026-09-15 UTC).
This is the current operational handoff, **not stage-release approval**.
Read this first; earlier review/deployment notes describe historical checkpoints.

## Latest remote continuation — read before historical PIDs below

Newest published software candidate is `c231f16`, not deployed to normal
stage. Full-bank residency (`ca62b8f`) reduced actual Fluid→Rhodes switching
from 225.798 to 3.714ms and removed its 21 source-lock misses. Return to Fluid
still produced one program-change source miss; do not mark continuity fixed.
The repeated performance sequence completed/restored every functional step,
but strict FAIL remains: 74 deadline increases, two bridge underruns, one
piano miss. Organ-plus-synth costs ~9.903ms wall/9.846ms thread CPU per
10.667ms period. No quality, voice, sample-rate or buffer reduction was made.

507 Mac tests plus Node passed; Pi passed 489 tests on `e3d74c7` and the next
18 control-attribution tests on `c231f16`. Four isolated interpreter-handoff
A/B/B/A captures completed (5000/1000/1000/5000us). All strict results FAIL:
deadline increases 38/33/40/43, no new bridge underruns, source misses 1/0/1/0.
This does not establish a useful improvement; detailed attribution is pending.
Normal stage restored active, PID197443, invocation
`b78c3129f6da4f78873822c4b25bb2ab`, zero restarts, state hash unchanged.
No capture or soak remains running. No permanent handoff change or core-soak
run is claimed. The core driver has only compiled on target.
Organ scalar/vector and extended-feature test tools are under development on
Mac; uncommitted tools are not target validation. Normal stage remains clean
`ce15cfb`, with its saved-state hash unchanged after completed experiments.

### Earlier same-evening checkpoint (superseded by the paragraph above)

The player is away tonight and requires all existing functions tomorrow night.
Follow [OVERNIGHT_REPAIR.md](OVERNIGHT_REPAIR.md) for the subsequent focused
generic-MIDI/bed-fade repairs and opt-in timing attribution. No features or
sound quality are being removed. Physical qualification remains unavailable
tonight; that does not block software work. The older checkpoint below is
preserved as evidence. Candidate `133fe04` is published on Pi4 only and lives
in the private profiling worktree, not normal stage. 460 Mac tests plus Node;
445 Pi tests on prior `8ef73e7` plus 32 focused tests on `133fe04` passed.
Recorder→sampled bed/fade/reconnect and performance-controls sequences both
completed, but strict real-time results still fail. Leslie STOP native parity
is verified; its rebuilt library is private only. Four fine diagnostics-on/off
captures show remaining scheduling/contending work, not only DSP CPU load.
Performance testing newly localized a ~226 ms Fluid→Rhodes program-change
source interruption to LOW_RAM dynamic sample loading; repair is in progress.

Latest normal restoration: clean `ce15cfb`, PID 185984, invocation
`9811c1d89eef41b1bc96764930db6879`, active, zero restarts; saved-state hash
unchanged; native/audio/control/UI health rechecked. No capture/soak is running
at this checkpoint. Bounded soak tools and safer supervisor are in local
development, not yet target-qualified. Consult overnight/private INDEX before
source synchronization or launch. No stage release or latency goal is passed.

## Continuation after the save

The focused batch **`27522fd`** is saved and tested as a separate candidate:
[CONTINUITY_REPAIR.md](CONTINUITY_REPAIR.md). It adds bounded piano MIDI/render
ownership and removes unused native-path preparation. Both piano-only recordings
no longer show the prior internal silent blocks; layered mean render time
improved about 12%. But every strict timing result still fails, one mixed take
has an unlocalized piano source-lock miss, and a control sequence ran late.
**Do not automatically promote it for tomorrow's live use.**

Normal Pi service is restored on saved `ce15cfb` (app/DSP source `483d953`),
active since **19:37:04 CDT**, PID 151143, invocation
`8dc33d08129f4b678d3572e0ff39e9e3`, zero restarts; HTTP/WS/native/audio/UI healthy.
Saved-state hash remains unchanged. The isolated audition is stopped; no repair
or capture job is left running. Candidate source lives in the private Pi
`continuity-20260914.bSKT8q/source` worktree, not the normal service checkout.

Latest verification: **350 Mac tests, zero skips**, plus Node UI/syntax;
**62 targeted Pi tests**, all 16 unchanged native hashes, five digital captures.
The 325/294 counts below describe the earlier saved build. Physical playing,
analogue latency, sustained headroom and release qualification remain open.
Private new evidence/index:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/continuity-20260914.MvFj78/INDEX.md`.

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
- Saved normal-stage application/DSP source change is **`483d953`**. Later commits
  `88c5a43`, `7a2f7dd`, and `e08175e` add audition tooling, tests and evidence,
  not a new synth DSP build. Save commit `ce15cfb` updates documentation only;
  the subsequent `27522fd` candidate changes application code as described above.
- No repair/capture job is left running. The normal stage service is running;
  the disposable audition service is stopped. Recheck live ownership before
  new work. Previous restart permission was for off-stage maintenance, not
  blanket permission to interrupt a future performance.

## Pre-continuity saved restart (historical)

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
- At the pre-continuity checkpoint: **325 tests, zero skips**, plus Node UI
  regression/syntax. Pi passed the 294-test runtime suite, the 29 initial
  audition tests and revised 12-test orchestration module. Do not claim a
  single full 325-test/Node run on Pi; Node is unavailable there.
- Five actual Pi-rendered MIDI recordings, numerical analysis, timing data and
  caveats: [DIGITAL_AUDITION.md](DIGITAL_AUDITION.md). MP3 was emailed to the
  user's chosen personal Gmail account with the attachment verified. The user
  listened and said the sound is close; this is valuable feedback, not a
  full hardware/performance sign-off.

## Still open — do not mark these fixed

1. **Piano continuity:** both earlier piano-only recordings have four exact
   512-frame (10.667 ms) silent blocks near chord changes. The old recordings
   did not prove each gap's cause; the nonblocking native-lock contention
   failure path is now reproduced offline and repaired/tested in candidate
   `27522fd`. Zero bridge underruns do not detect these source gaps. Both
   new piano-only captures have no internal ≥128-frame stereo-zero runs. One
   native transition-lock miss remains unlocalized in a mixed capture; see the
   newer evidence before closing this finding. Preserve tone/sustain semantics.
2. **Worst-case render reserve:** layered tests averaged 7.48–7.77 ms against
   a 10.667 ms period. Shimmer's p99 bucket upper bound was 11.733 ms, with
   112 over-budget cycles; buffering covered them in that short test. The
   desired 30–40% demanding-patch reserve is not demonstrated. These are
   elapsed audio-render timings, not whole-Pi CPU percentages or proof of
   the hardware's absolute limit. Candidate layered means are now 6.55–6.81 ms;
   shimmer p99 upper 10.347 ms with 33 overruns still does not establish reserve.
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
