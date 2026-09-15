# Pi4 application startup checkpoint — 2026-09-14

This is **not stage qualification**. The user confirmed off-stage maintenance
with only Wi-Fi connected. Peavey audio and Yamaha MIDI were absent.

## Tested candidate

- Source: `f7a4e02106d26e921a85a77766ea29e1b9b025ce`, `pi4-stage-pro`.
- Native artifacts: all 16 hashes still match [TARGET_BUILD.md](TARGET_BUILD.md).
- Existing Python 3.13.5 live venv used without installing/changing dependencies.
- Mac and Pi: **286 Python tests, zero skips**. Node UI regression/syntax tests
  passed on Mac; Node was unavailable on Pi and is not claimed there.
- Target preflight: 84 passes, 11 warnings, zero failed prerequisites. Warnings
  include intentionally stopped baseline, missing physical devices/preferred
  Yamaha output, absent memory controller, and inventory-vs-runtime limitations.
- Tracked service unit passed `systemd-analyze --user verify` on the Pi.
- Baseline service stopped cleanly with `Result=success`, PID zero. No live
  source was overwritten. Additional verified private backup is in [BASELINE.md](BASELINE.md).

## First isolated full application run

Transient unit `stave-pi4-qa.service`, invocation
`40d80be07932427fa48488320345b8de`, owned PID 116305. Separate copied config/data,
instance `qa`, JACK name `StaveSynth_qa`, loopback HTTP 18080 / WebSocket 18765.
No automatic audio/MIDI connections. Systemd Type=exec with no restart policy;
the launcher's complete native defaults and strict check were active.

- Process launched at 17:44:56 CDT; app's final running marker at
  17:44:59.042. This was a warm filesystem/process start, **not a cold boot**.
- FluidR3 loaded as `Fluid`, program 0, soundfont ID 1. Piano owner alive and enabled.
- All required native backends ready, no missing modules; graph 48,000 Hz /
  512 frames, six ring slots (saved low-latency setting).
- Render thread reported FIFO priority 80, pinned to CPU 3.
- Both listening sockets confirmed owned by PID 116305. HTTP UI and no-store
  runtime identity served correctly. `get_state` and `debug` answered on WS.
- Control worker, HTTP/WS listener, handler and event-loop health were good.
  No state warning, control error, graph error, MIDI drops or UI recovery attempt.
- Ten-second idle progress check: accepted render count 3940 → 4879; consumed
  JACK callbacks 3945 → 4884. Underruns stayed **7 → 7**, xruns **0 → 0**.
  Startup underruns are retained as evidence, not erased or called zero.
- Cumulative startup-inclusive render measurements at sample 4880:
  p95 bucket 5.547–5.653 ms; p99 5.867–5.973 ms; max 25.794 ms;
  seven cycles exceeded the 10.667 ms block period; no rejected writes or missed
  telemetry samples. The ten-second window added zero over-budget cycles.

These are **idle software measurements**, not maximum-layer capacity, physical
latency or audible quality. Piano's sample render counter stopped at 400 during
intentional idle sleep; mixer and JACK continued, as expected. No notes were
played. USB recovery, Safari controls, room/PA output, favorite-patch comparison,
service rehearsal and extended soak remain required.

## Normal service and software rollback

The next commit, `c509b8f`, adds deployment configuration/docs/tests without
changing the tested application source. **291** tests now pass on Mac, including
five additional reversible-unit contract tests.

Installed only `90-pi4-stage-candidate.conf` into the existing user service's
drop-in directory. The installed base unit, original source and original
Faust/memory drop-ins were retained. The effective unit passed systemd verification:
candidate WorkingDirectory, bounded precheck with no mixer mutation, existing
venv, stage identity, strict native flags, inherited notify/watchdog lifecycle.

First normal candidate process PID 116388, invocation
`37cfb3c0db854f079fbd95a06b9a990d`, reached app/systemd readiness at
17:47:55 CDT. HTTP identity was fetched successfully from the Mac via
`stavepi4.local:8080`. State, recorder, native profile and audio health were good;
no graph/control errors. Startup-inclusive counters showed eight underruns and
zero xruns; no audible claim is made without the interface.

Both isolated and first normal-candidate shutdowns returned success/PID zero.
The isolated run closed HTTP/WS, JACK and FluidSynth and finished shutdown in
approximately 115 ms according to its journal.

Software rollback was then exercised: moved only the installed candidate drop-in
into the private backup directory, reloaded systemd, verified WorkingDirectory
returned to the original checkout, and started the original build. PID 116814,
invocation `3c04860c9c5841a79265604592551178`, reached readiness at 17:49:03 CDT.
Original HTTP/WS state and debug answered; Fluid piano remained selected/alive,
and render/JACK callback counts advanced with no reported engine error.
This demonstrates **software/service rollback**, not an SD-card recovery or
physical-audio qualification. No user presets were intentionally changed.

## Follow-up finding — idle garbage collection

After returning to the candidate (PID 117029, invocation
`455601ee6736454aba73f50b577183f4`), a 35-second idle window spanning the first
idle garbage collection recorded 3277 render samples and 3283 bridge callbacks,
mean render 4.856 ms, p95 bucket 5.013–5.120 ms and p99 6.400–6.507 ms.
There were **six additional underruns**, one over-budget render sample,
zero xruns, no dropped MIDI, and no graph/control/UI/native-profile failure.

At 17:51:31 CDT the journal records gen-0 collection of **68,398 objects in
84.6 ms**, alongside 35.2/58.5 ms render-loop gaps. This needs correction and
retest; the earlier short idle pass did not cover this event. The Pi reported
no throttling, 66.2 °C and approximately 180 MiB synth RSS. These measurements do
not establish performance under playing load or exclude a future thermal risk.

### Targeted allocation correction

Offline reproduction identified the four unconditional NumPy `data_as` calls
in the limiter and final ring write as the dominant source: two unreachable
cyclic objects per conversion. Four calls × two objects × 93.75 blocks/s ×
90 seconds predicts 67,500 objects, close to the observed 68,398. The two
per-call input conversions in fallback C IIR paths had the same behavior.

Replaced these **six ephemeral conversions only** with typed integer-address
casts. Named local arrays own the memory throughout the synchronous native
calls; native functions finish copying/processing before return. Cached owning
pointers remain unchanged. DSP math, sample values, buffers, latency setting,
and collection policy are unchanged.

New regression evaluates the six exact production expressions: 12,000 new
conversions yield zero collectible cycles; 2,000 old conversions yield 4,000.
Typed-pointer and synchronous biquad results are checked. The complete Mac
suite now passes **294 tests, zero skips**, plus Node UI regression/syntax.
Target retest through the 90-second collection boundary is required next.

### Retest result — correction verified on Pi

Runtime source **`483d953ccaa7bf16af31eaf9275b6018612d4faa`** passed all
**294 tests with zero skips on Pi** (25.552 s) and Mac. No native source/binary,
dependency, kernel, sample-rate or buffer setting changed for this correction.

The normal service restarted to readiness at 17:58:31.994 CDT, PID **120053**,
invocation `de9572b0bbc24f2aa90336138a6e0346`. It remains the candidate process
at this checkpoint; the isolated test unit is stopped.

A measured 120-second idle window, 17:59:04–18:01:04 CDT, spans the original
90-second cleanup boundary and the following collection:

- 11,263 accepted render blocks and 11,263 consumed JACK callbacks advanced.
- **Zero new underruns**, zero xruns, zero dropped MIDI events, zero missed
  telemetry samples, and healthy native/UI/control/graph status throughout.
- Seven initial startup underruns remained **7 → 7**, rather than being reset.
  Two render cycles exceeded the block period, but neither exhausted the ring.
- Window mean render 4.841 ms; p95 bucket 5.120–5.227 ms; p99 6.400–6.507 ms.
  These are software render times, **not physical latency or full playing load**.
- First gen-0 collection: **487 objects, 5.9 ms** at 18:00:01.
  Next: **103 objects, 0.2 ms** at 18:00:31. This contrasts with the prior
  68,398 objects / 84.6 ms pause. Collection policy was not disabled or retimed.
- Service remained active with zero automatic restarts and advancing watchdog
  heartbeat. Point-in-time RSS approximately 169 MiB; 68.1 °C; throttling flags zero.
- A LAN WebSocket client also connected during this run. Its actual browser
  type, touch behavior and musical use are not inferred from the connection log.

**Next required gate:** [TEST_TONIGHT.md](TEST_TONIGHT.md). Connect the actual
Peavey/Yamaha setup, verify selected routes, then play/listen/rehearse. This
short idle success does not certify tomorrow's show, cold boots, hot-plug
recovery, maximum layered load, or an eight-hour soak.

## Pre-continuity restart and saved handoff (historical)

After the digital audition and listening discussion, the user explicitly
confirmed off-stage restart safety. Normal `stave-synth.service` restarted to
active/running at **18:59:08 CDT, 2026-09-14**, PID **140111**, invocation
`20a85a4052d94260bfdd8f4a92e1d5a0`, zero automatic restarts. The runtime app/DSP
source remained `483d953`; intervening commits at that checkpoint were
tooling/tests/evidence. The later application candidate is recorded below.

Mac HTTP and Pi WebSocket checks succeeded. Native profile, Fluid piano,
audio/control/UI health were good; 48 kHz / 512 frames / six slots remained
unchanged. A startup-inclusive snapshot reported 3,052 callbacks, eight
underruns and zero xruns; no new continuous-playing interval was qualified.
Saved-state SHA256 stayed
`ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
Peavey/Yamaha remained absent. The piano-continuity and shimmer-timing findings
from [DIGITAL_AUDITION.md](DIGITAL_AUDITION.md) are still open.

The user has listened to the emailed MP3, considers the sound close, and plans
physical tests September 15. Follow [RESUME_HERE.md](RESUME_HERE.md) for the saved
handoff; do not interpret earlier historical PIDs or pre-deployment notes as
the current running state. The save operation does not restart or retune Stave.

## After continuity-candidate testing

Normal stage service was restored at **19:37:04 CDT**, PID 151143, invocation
`8dc33d08129f4b678d3572e0ff39e9e3`, active/running with zero restarts. Source is
still `ce15cfb` / app `483d953`; the new `27522fd` repair was tested in a separate
private worktree and **has not been promoted**. HTTP stage identity from Pi/Mac,
WS native/audio/control/UI health, saved Fluid/master 1 and the unchanged state
hash were verified. The isolated unit is stopped. Follow
[CONTINUITY_REPAIR.md](CONTINUITY_REPAIR.md) for measured improvements and
remaining timing/source/control failures; neither build is stage-qualified.
