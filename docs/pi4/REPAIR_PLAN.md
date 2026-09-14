# Pi4 repair priorities and implementation status

2026-09-14. Branch: `pi4-stage-pro`. This is a development repair record, **not a
stage-release approval**. The complete baseline source audit is preserved at
`185a8f5`; its application source is still the stage baseline `d0142f0`.
Historical defect probes intentionally assert the old bugs and are not the
current passing regression suite.

## Latest operational checkpoint

The tested Pi4 candidate is now deployed through a removable service drop-in,
with the original source preserved. Full native application startup, normal
HTTP/WS access, clean shutdown and software rollback were exercised off-stage.
See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) and
[STARTUP_CHECKPOINT.md](STARTUP_CHECKPOINT.md) for current evidence and next steps.
**Next gate: connect the Peavey interface/Yamaha keyboard and test the actual
playing setup.** No physical-audio or full-stage qualification is yet claimed.

## Order of work

| Order | Outcome | Current status |
| --- | --- | --- |
| 1 | Protect the working instrument and establish safe regression tests | Baseline audit/backups preserved; Pi4 candidate deployed through a removable override. Original-source software rollback exercised. |
| 2 | Correct held notes, STOP, state saves, recording and connection truth | Two software repair batches implemented with offline regression coverage; real devices and acoustic behavior still require qualification. |
| 3 | Bound control and audio work; finish state, recorder and native ownership | Schema/transactions, bounded queues/pads, MIDI overflow, graph contract, native lifecycle and UI recovery implemented. Target timing and fault qualification remain open. |
| 4 | Prove sound, latency and reserve on Pi4 | Not measured: use piano + OSC1 + OSC2 + effects + optional recorded bed, with the actual controllers/interface. Preserve favorite sounds. |
| 5 | Refine the Stage UI and pass release qualification | Software rollback passed; Safari/portrait, cold boots, hardware reconnects, fault tests, eight-hour soak, rehearsal and SD recovery remain open. |

Do not lower unison, layer count, sample rate or sound quality merely to produce
a smaller CPU number. Make profile decisions after matched-level listening and
controller-to-analogue latency/render-time measurements.

## First targeted repair batch (historical, through `762a259`)

All entries below are local implementation, not verified behavior of the
currently running Pi. Open items in this historical table are superseded where
explicitly addressed in the second batch below. DSP control repairs intentionally change defective
freeze/retrigger/STOP behavior; they are not an intentional tonal redesign.

| Area | Change | Regression evidence and limits |
| --- | --- | --- |
| Instance safety | Separate validated config/data roots, loopback-only test ports, exact JACK identity, advisory single-owner lock; test routing/auto-MIDI/physical-output connection disabled | Runtime/path/symlink/lock tests, isolated config+recorder subprocess, routing mocks, actual C compiled against mock JACK. No hardware isolation is claimed. |
| Launcher/native contract | Manual stage launch requires `--stage`; removed broad process killing and global mixer writes; add missing documented native flags; require rebuilt bridge with named-start support | Launcher refusal/syntax checks; mock-JACK duplicate-name, no-autoconnect and failed-start cleanup/retry. Target binary/profile verification remains open. |
| Atomic JSON saves | Unique sibling temporary files, complete serialization before write, finite JSON only, file fsync and atomic replacement; detached state/preset snapshots | Concurrent threads/processes, continuous JSON reader, write/replace faults and permission tests. This does not make multi-handler state changes transactional or prove SD power-cut durability. |
| MIDI notes/pedals | Retire old piano pitch after octave-only retrigger; sustain/sostenuto hold union and rising-edge capture; panic clears both pedals; silent pad split avoids phantom active tracking | Exact MIDI-method tests with mock bridge/engines. Channels, overlapping transposed destinations and event-ring overflow remain open. |
| Native organ | Reuse releasing same-note slot; serialize render/allocation and identity-check reclamation; render-owned hard-panic request clears every gate/history | Exact wrapper/native-mock tests including controlled thread overlap. ARM timing and acoustic behavior still need testing. |
| Freeze recovery | Restore current normal decay/damp and type-specific early reflections; reopen plate/drone input gates during unfreeze/panic | Exact wrapper tests with native zone mocks, including edits/type changes while frozen. No acoustic comparison yet. |
| Recorded-pad STOP | Panic hard-stops loaded sampled players, clears rise/mellow history, cancels fade requests, retains WAV buffers for retrigger | Exact sample/panic tests. Pending MIDI, already-in-flight fade commands, other native resets and ring clearing are not yet one atomic STOP transaction. |
| Recordings | Reserve unique take names even within one second; failed WAV setup removes only its own artifact | Real temporary WAV/sidecar regression tests. Writer timeout/queue loss, storage errors and active-take deletion remain open. |
| Listener readiness | Synchronous HTTP bind, acknowledged WS bind, startup rollback, timeout-driven HTTP teardown, explicit terminal server lifecycle; application entry cleanup on start failure without final state save | Real ephemeral loopback binds/conflicts/runtime-config plus mocked entry/cleanup tests. Ongoing UI-worker health, in-flight handlers and complete native shutdown are not qualified. |
| Browser reconnect | Only the authoritative socket handles lifecycle/messages; retired errors cannot close its replacement; single retry timer; runtime-provided WS port | Exact JavaScript functions with fake sockets/timers, hydration/queue tests. Real Safari sleep/roam and network black-hole tests remain open. |
| Small control guards | Reject non-object WS JSON and out-of-range piano EQ-band indices; apply piano octave on generic recall; debug reads counters without consuming FluidSynth audio | Listener/control tests. Complete schema, preset recall and server-side macro semantics remain open. |

## Safe local verification

From the development checkout, with existing NumPy, `websockets`, Node and a C
compiler available:

```sh
python3 tools/run_offline_tests.py
git diff --check
```

The runner enumerates approved test filenames, parses Python without launching
the app, checks shell/browser syntax, runs the isolated tests and fails on skips.
It never runs the legacy parameter-flood, connection-chaos or boundary-sweep
scripts. Do not substitute broad `pytest` or unittest discovery.

Native checks compile the real bridge against fake JACK. Native DSP tests use
mocks; Python method tests extract exact definitions to avoid importing/starting
audio engines. Listener tests use disposable recording directories and ephemeral
loopback sockets. They do not target `8080`/`8765`, contact the Pi or load physical
MIDI/audio. Missing dependencies are reported, not installed automatically.

Observed combined result for the second repair batch on the Mac development checkout: **257 Python tests
passed, zero skips**, plus the JavaScript control/connection regression script. All 75
Python files parsed, browser JavaScript and the three shell scripts passed
syntax checks, and `git diff --check` was clean. The initial sandbox denied
loopback socket binding; the complete suite passed after permission for those
temporary localhost listeners. No dependencies were installed.

Test environment: Python 3.13.2, NumPy 2.4.6, websockets 16.0, Node 24.14.1,
Apple clang 17.0.0 on arm64 macOS. These versions are test evidence, not a newly
chosen or validated Pi deployment dependency profile.

## What isolation does and does not mean

Runtime identity can be inspected without creating application state or starting
the engine:

```sh
stave_test_root=$(mktemp -d)
STAVE_INSTANCE=qa STAVE_INSTANCE_ROOT="$stave_test_root" \
STAVE_HTTP_PORT=18080 STAVE_WEBSOCKET_PORT=18765 \
python3 -m stave_synth.runtime --describe
```

That command is description only. A non-stage instance must have all four
explicit values, distinct non-production ports and non-overlapping resolved
paths. Production keeps legacy paths/ports. Test overrides without a non-stage
identity fail closed. No test instance may start a global MIDI bridge, change
output routing, auto-connect physical ports or send a production systemd
watchdog notification.

This is **not OS, CPU, memory, USB or PipeWire isolation**. Never stress a second
synth instance on a Pi that is performing. Advisory locks protect cooperating
new processes, not older deployed code or arbitrary other programs. A dedicated
spare test device or an approved off-stage maintenance window is still required
for full native/audio testing.

## Second repair batch: implemented, not deployed

| Area | Implementation and evidence | Qualification boundary |
| --- | --- | --- |
| State and scenes | Finite, bounded, typed public controls and saved-state schema; whole-scene validation before mutation; last-good state recovery and retained rejected originals; serialized saves capped at the readable 4 MiB limit | Previous-state recovery is not an SD power-loss test. Existing out-of-range saved values normalize to actual engine limits. |
| Presets/setlists | One atomic ten-slot bank owns scenes and labels; legacy files retained; scene snapshots exclude global libraries/routing; full apply with pending/completed events and failure rollback | Engine/type changes still need listening for tails and transition cost; an 800 ms morph is not seamless cross-engine spillover. |
| Controls | Server-owned macros work with zero or multiple browsers; bounded MIDI control worker; queue generation invalidation on panic; detached responses; shared mutation events fan out | Physical MIDI gestures and soft-takeover behavior need audition. |
| Recorder | One owned take/writer/queue; bounded finalization, explicit incomplete/dropped/error metadata; active/finalizing/read-claimed take protections; reconnect hydration | Real SD throughput, full-disk and power interruption need spare-media tests. |
| Sampled pads | Bounded WAV validation/decode and single preparation reservation; transactional file/player replacement and rollback; real resident-slot status; short-loop safety | 32 MiB resident per slot, 192 MiB bank, 64 MiB source, 128 MiB preparation estimate. Stop the target pad before replacing/clearing it; other slots keep playing. Actual memory reserve still needs measurement. |
| Native audio | Fixed 48 kHz/startup block-size contract; graph changes fail silent with an explicit fault; bounded ring transition coordination; MIDI overflow releases notes/pedals; final non-finite DAC guard; native allocation checks | Rebuild on Pi4. Mock-JACK concurrency tests are not scheduling or acoustic qualification. Runtime graph changes require controlled restart. |
| Native ownership/startup | Join owners before native free; retain resources on nonquiescence; soundfont loading completed before render; checked program selection; strict requested-Faust and loaded-piano readiness; pads prepared before audio | Strict failures intentionally prevent a silently degraded ready state. Fresh install, service timeout, RT/linger and OS recovery remain deployment work. |
| GC | Collection only after long measured mixed-output inactivity with no voices, bed, freeze, recorder or pending panic | This reduces a known live-tail risk, but is not a proof that Python scheduling/GC cannot cause an underrun. |
| Web/UI | Bounded producer and per-client queues, independent senders, bounded HTTP workers; listener/handler health; bounded UI-only recovery that refuses overlapping owners; truthful pending/record/pad/health displays; minimal portrait/short-landscape CSS | Physical Safari sleep/roam, touch layout and two-client performance tests remain open. Healthy audio is not restarted solely for a control-component failure. |
| Devices | Typed stereo discovery (FL/FR and numbered pairs), verify new pair before old unlink, known-failure rollback, saved-preference recovery, per-device MIDI alias handling, own-client-only writes | Ambiguous identical device names are retained. Timed-out uninterruptible commands are uncertain, not cancelled; at most two admitted workers, with further admission blocked while timed-out work remains. |
| Test safety | Three historical production-targeting stress scripts now unconditionally refuse direct execution and skip discovery before dangerous imports | Continue using the explicit offline allowlist; do not treat broad discovery as the test contract. |

The player explicitly chose **open control on trusted private Wi-Fi**. There is
no pairing or authentication boundary: anyone able to reach Stave's network
ports can control it. Use an isolated trusted stage LAN, not shared guest/public
Wi-Fi, and do not forward these ports to the Internet. The Connection settings
state this policy; the repair does not claim to make an exposed server safe.

The archived current-state snapshot was read-only validated against the new
schema. Six previously out-of-range values normalize to existing engine limits
(spread, two independent cutoffs, reverb wet gain/space/predelay); the original
archive is unchanged. No stored preset/recorded-pad library was present in that
snapshot, so synthetic legacy fixtures—not a populated personal library—cover
those migrations.

## Next gate: target checks, then hands-on qualification

The separate Pi4 checkout has passed native rebuild/load checks for both slot
profiles and all 257 Python regressions. Those checks did not start a second
synth or connect physical devices. Native hashes, test scope and the remaining
limits are recorded in [TARGET_BUILD.md](TARGET_BUILD.md).

Then confirm an off-stage window and prepare a reversible test deployment with
complete required soundfont/state backups. Connect the reference hardware and
test real startup, routes, notes/pedals, recorder-to-pad flow and Safari before
latency optimization. Capture favorite patches and matched-level baseline audio
before sound-affecting decisions. Audit findings involving envelope/voice/tail
semantics are not all declared fixed: audition and measured native-path evidence
determine which require changes. Cold-boot/hotplug loops, physical latency and
render distributions, eight-hour soak and a service-length rehearsal remain
mandatory release gates.

The player has confirmed a Peavey USB audio interface and Yamaha keyboard for
the first hardware test, and can connect them when software is ready. Exact
models, hub and Pi power supply still need to be inventoried. Support is not
brand-specific: discover compatible OS-exposed devices, allow deliberate
selection and recover intended routes; do not silently switch to an arbitrary
output or claim every possible device has been qualified. Prior USB enumeration errors require hardware/OS
recovery checks as well as application tests; a code repair cannot establish
that a missing USB device is healthy.

## Deployment boundary

No running Pi service, installed configuration or Pi5/Mac branch was changed by
this repair batch. GitHub publication of `pi4-stage-pro` is not deployment.

**Rebuild native components on the Pi4 target architecture before attempting
this branch.** The Python bridge now requires `bridge_start_named`; an old
`jack_bridge.so` deliberately fails startup instead of silently using the stage
identity. Copying Python alone is not a supported update. Verify the intended
native modules actually engage; copying the new service flags alone is not
proof of acceleration or available Pi4 headroom.

Deployment needs an approved off-stage window, validated backup/rollback scope,
explicit artifact/profile records, isolated native checks and staged routing.
Do not run the generic installer over the working system as a test. The existing
backups are not a full SD image and do not contain external soundfonts; see
[BASELINE.md](BASELINE.md). Only the measured gates in
[ENGINEERING_VALIDATION.md](ENGINEERING_VALIDATION.md) can support a stage-ready
claim.
