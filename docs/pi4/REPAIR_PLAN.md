# Pi4 repair priorities and implementation status

2026-09-14. Branch: `pi4-stage-pro`. This is a development repair record, **not a
stage-release approval**. The complete baseline source audit is preserved at
`185a8f5`; its application source is still the stage baseline `d0142f0`.
Historical defect probes intentionally assert the old bugs and are not the
current passing regression suite.

## Order of work

| Order | Outcome | Current status |
| --- | --- | --- |
| 1 | Protect the working instrument and establish safe regression tests | Baseline audit/backups preserved; isolated identity and explicit offline test runner implemented locally. No deployment. |
| 2 | Correct held notes, STOP, state saves, recording and connection truth | First targeted repair batch implemented; coverage and remaining gaps below. This priority is not fully closed. |
| 3 | Bound control and audio work; finish state, recorder and native ownership | Open: schema/transactions, queues, sampled-pad limits, MIDI overflow, graph size/rate, GC and reset coordination. |
| 4 | Prove sound, latency and reserve on Pi4 | Not measured: use piano + OSC1 + OSC2 + effects + optional recorded bed, with the actual controllers/interface. Preserve favorite sounds. |
| 5 | Refine the Stage UI and pass release qualification | Safari/portrait review, cold boots, reconnects, fault tests, eight-hour soak, rehearsal and rollback remain open. |

Do not lower unison, layer count, sample rate or sound quality merely to produce
a smaller CPU number. Make profile decisions after matched-level listening and
controller-to-analogue latency/render-time measurements.

## First targeted repair batch

All entries below are local implementation, not verified behavior of the
currently running Pi. DSP control repairs intentionally change defective
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

Observed combined result on the Mac development checkout: **76 Python tests
passed, zero skips**, plus the JavaScript connection regression script. All 52
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

## Next implementation gate

Before live qualification, finish:

1. Validated finite/bounded settings before state mutation; explicit scene versus
   global-library data, complete recall and failure-aware multi-file operations.
2. One owned recorder session/queue/file lifecycle; truthful drop/error status;
   bounded pad decode/preparation, invalid/tiny sample rejection and slot-local
   replacement without interrupting every bed.
3. Server-owned MIDI macro behavior; bounded/coalesced slow-client work; truthful
   device/UI health and a deliberate stage-network access boundary.
4. Release-safe MIDI overflow, supported JACK size/rate contract and coordinated
   ring resets; render-owned native resets; measured GC/allocation behavior.

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
