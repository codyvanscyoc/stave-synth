# Pi4 pre-show release checkpoint

Status: **open checkpoint, not a release approval**. Updated 2026-09-14 for the
`pi4-stage-pro` repair line through `fd5cfd6`, plus the release-preparation batch:
corrected `get_state` recorder owner, A08 native-organ exact-zero endpoint,
graph-sized saturation scratch, whole-cycle bounded render telemetry, read-only
target preflight, and pinned/bounded stage service startup. The batch passed
**286 Python tests, zero skips**, Node UI regression tests, Python parsing,
shell/JS syntax and diff checks on the Mac. Full target startup is next.

This page is the short operational index. The evidence and qualifications remain
in [REPAIR_PLAN.md](REPAIR_PLAN.md), [TARGET_BUILD.md](TARGET_BUILD.md), the
[complete review](COMPLETE_CODE_REVIEW.md), the
[engineering gates](ENGINEERING_VALIDATION.md), and the original
[core](review-evidence/core-report.md), [audio](review-evidence/audio-report.md),
[UI](review-evidence/ui-report.md), and
[boot/tooling](review-evidence/boot-test-report.md) reports.

## Status meanings

- **Implemented/offline-tested** — repaired in the Pi4 line with isolated or
  mocked regression evidence. This is not physical-stage qualification.
- **Still needs code** — a known behavior remains unresolved or deliberately
  deferred.
- **Requires hardware/audition** — source work is present, but its live result
  needs the real Pi4, interface, controller, output and/or listening test.
- **Policy/mitigation** — accepted operating boundary or a safe limitation,
  rather than a claim that software eliminated the risk.

## Tomorrow's narrow release path

- [x] Freeze broad feature and tonal work. Do not redesign the five-fader
  workflow, envelopes, oscillator routing, tails, latency profile, gain staging,
  voicing or effects the day before use.
- [x] Commit the tested release-preparation batch (`f7a4e02`). The explicit offline suite
  passed for the corrected recorder
  hydration owner and A08 organ exact-zero endpoint. Do not deploy an uncommitted
  working tree.
- [x] Verify the private app/runtime rollback archive on both Pi and Mac,
  including the actual FluidR3 soundfont and existing venv. Hashes and scope are
  recorded in [BASELINE.md](BASELINE.md). A full SD image/OS restore is still open.
- [ ] Deploy reversibly in an off-stage window: matching Python source, locally
  built aarch64 bridge/Faust artifacts, service file and complete native-profile
  drop-in. Record hashes and keep the old runtime ready for rollback.
- [x] Perform one isolated full-application process start (not an OS cold boot).
  `f7a4e02` started with native JACK/FluidSynth/DSP, copied state, and HTTP/WS.
  See [STARTUP_CHECKPOINT.md](STARTUP_CHECKPOINT.md) for exact observations and limits.
- [ ] Connect the actual **Peavey USB audio interface and Yamaha keyboard**.
  The user confirmed an off-stage, Wi-Fi-only maintenance window. They are not
  connected at this checkpoint; earlier target inspection
  found no physical USB MIDI/audio device present. No release claim can substitute
  for this gate.
- [ ] Verify, separately: service active without restart loop; required native
  profile engaged; correct Fluid soundfont/program; exact Peavey stereo route;
  Yamaha MIDI route/activity; notes, releases and sustain; STOP; five faders;
  preset load; browser reload/reconnect; and recorder-to-pad once if it will be
  used tomorrow.
- [ ] Listen through the intended monitor/PA at conservative level. If startup,
  route, stuck-note/STOP, silence, severe glitching, or control recovery fails,
  stop and roll back; do not improvise a broad repair at the venue.

## Original core findings C01–C17

| IDs | Current state | Checkpoint |
| --- | --- | --- |
| C01–C02 | **Implemented/offline-tested** | Serialized unique-temp state writes, bounded finite schema, validation-before-mutation and last-good recovery have regression coverage. SD-card power loss and full-disk behavior remain hardware/media gates. |
| C03 | **Implemented/offline-tested; requires hardware** | Panic invalidates queued controls and ramps and clears pedals, notes, native organ state, sampled pads, freeze and ring state. Exercise STOP under piano + both oscillators + bed + sustain on the real rig. |
| C04–C05 | **Implemented/offline-tested** | Octave retrigger ownership and combined sustain/sostenuto behavior have exact MIDI-method regressions. Physical Yamaha pedal behavior still needs rehearsal. |
| C06–C07 | **Implemented/offline-tested; policy/mitigation** | Startup enforces the fixed 48 kHz graph, later incompatible graph changes fail silent/restart, and ring transitions use bounded ownership rather than a sleep. Do not change PipeWire quantum tomorrow. |
| C08 | **Implemented/offline-tested; requires hardware** | Native MIDI overflow counters and release/pedal recovery plus bounded control work are covered with mock JACK. Burst behavior on the real controller/graph is unqualified. |
| C09–C10 | **Implemented/offline-tested; requires media test** | Recorder and pad replacement have unique ownership, bounded queues/finalization, error truth, size/memory limits, transactional replacement and joined shutdown. Real SD throughput, ENOSPC and power interruption remain open. |
| C11 | **Partly implemented; requires hardware** | Listener readiness, health warnings, verified MIDI/output transactions and bounded UI recovery are implemented. Physical output, MIDI presence and full repaired-app startup are not yet proven; READY alone does not prove audible Peavey output. |
| C12–C13 | **Implemented/offline-tested** | Complete validated scene application, pending/completion truth, global-field preservation and one atomic preset bank replace the partial multi-file behavior. Audition engine/type changes and tails. |
| C14 | **Implemented mitigation; requires soak/audition** | GC now waits for measured mixed-output inactivity and excludes active bed/freeze/recorder/panic conditions. Only a representative long Pi4 run can establish scheduler reserve and absence of audible interruption. |
| C15–C16 | **Implemented/offline-tested** | Debug no longer consumes piano samples; health labels were corrected; non-finite control/state input and final native output are guarded. Physical-meter meaning remains limited to internal engine signal, not PA audibility. |
| C17 | **Policy/mitigation; still needs product work** | Current minimum-velocity, omni/channel merge and pedal policy are preserved for tomorrow. Multi-channel note identity, original MIDI timestamps/clock accuracy and the velocity threshold need explicit design plus Yamaha/Dexibell audition before code changes. |

## Original audio findings A01–A13

| IDs | Current state | Checkpoint |
| --- | --- | --- |
| A01 | **Implemented/offline-tested; requires hardware** | STOP hard-stops sampled pads and resets their playback/filter state without unloading the library. Verify with an audible bed. |
| A02–A04 | **Implemented/offline-tested** | Native organ restrike ownership, identity-aware reap and render-owned hard panic were repaired with controlled overlap tests. ARM timing and musical release behavior still need playing. |
| A05 | **Implemented/offline-tested; requires soak** | Piano-room native clear/process ownership is serialized. Stress and listen to panic/instrument changes with a real active room tail. |
| A06 | **Implemented/offline-tested; requires Pi4 memory check** | Per-source, per-slot, bank and preparation limits plus single-slot transactional replacement prevent the original unlimited/all-bank path. Confirm resident memory with the pads intended for the show. |
| A07 | **Implemented/offline-tested** | Soundfont lookup is finite, profile-aware and checked; strict startup rejects a silent/invalid piano. Confirm the actual Fluid asset and program during the full-app start. |
| A08 | **Implemented/offline-tested; requires audition** | Native organ exact zero now fades for one block then mutes; strictly positive gain behavior is unchanged. Three focused regressions and the full offline suite pass. Actual organ output still needs listening. |
| A09 | **Implemented/offline-tested; requires audition** | Freeze/unfreeze/panic restore current decay/damp and type-specific control state across native backends. Listen to each effect type actually used. |
| A10 | **Still needs code/design and audition** | Block-end scalar envelope behavior can omit a short within-block transient. Do not change envelope architecture or quantum before tomorrow; document and audition the supported 512-frame profile later. |
| A11 | **Still needs code/design and audition** | Selective per-oscillator routing still reconstructs mixed sources on some native paths. Preserve the familiar sound tomorrow; a true-source-bus change needs matched-level A/B approval. |
| A12 | **Implemented/offline-tested** | WAV validation, finite samples, decoded-size bounds and arbitrary short-loop wrapping cover the original empty/tiny-file failures. Test the user's actual recording once. |
| A13 | **Requires hardware/audition; may still need code** | Piano sleep and wet-zero/tail semantics need deliberate product policy and listening. Do not broadly change residual DSP history immediately before the show. |

## Browser, control and startup findings

### Implemented/offline-tested

- One authoritative browser socket; stale callbacks cannot replace/close the
  current connection, retry timers are deduplicated, one-shot commands are not
  replayed, and absolute pending controls coalesce.
- HTTP and WebSocket bind success are required before startup continues;
  listener, handler, HTTP-worker and event-loop health is exposed. Outgoing
  queues are bounded/coalesced and a slow client cannot stall all clients.
- Server-owned macros work headless and do not multiply their engine commands
  by browser count. Shared mutating results fan out while private get/list/debug
  responses remain private.
- Current state, preset bank/labels, fade, soundfont/profile and recorder status
  hydrate a reconnect. The newly corrected recorder owner must still enter the
  frozen commit and suite noted above.
- Preset/setlist recursive embedding was removed; scene snapshots exclude
  global libraries, routing and controller maps.
- Non-object/oversized WS messages are rejected safely. Pad residency/errors,
  control drops, native-profile failures and graph/UI errors are shown rather
  than inferred from CPU/meter activity.
- A minimal phone-portrait/short-landscape layout was added without replacing
  the five-fader stage surface.

### Policy/mitigation or hardware gate

- Browser control intentionally remains open on **trusted private stage Wi-Fi**.
  Anyone on that network can control Stave. Do not use guest/public Wi-Fi or
  forward ports to the Internet; no authentication protection is claimed.
- Real Safari sleep/wake, roam, black-hole recovery, orientation/touch targets
  and two-client performance are not qualified. Test an actual iPad/phone after
  the full-app start.
- Loaded-preset marking is a last-loaded scene indicator, not a complete dirty
  comparison. This is a UI semantic limitation, not a tomorrow blocker.
- PWA icon metadata and some fixed latency/help wording remain low-priority
  polish; neither proves audio/control readiness.

## Work intentionally kept out of tomorrow's freeze

### Installer and appliance recovery

The generic installer still needs convergence/upgrade behavior, option rollback,
unknown-option rejection, verified linger/RT/memlock/memory-controller state,
headless dependency handling, pinned dependencies/assets, soundfont integrity,
packaging clarity and a clean-image reboot exercise. Do not run the generic
installer over the working appliance as tomorrow's deployment mechanism.

### Benchmarks and old stress tools

The retired live flood/chaos/boundary scripts are guarded because their old
targeting and pass criteria were unsafe. The legacy shimmer/render comparison
and mean-only microbenchmarks are not release evidence. Future qualification
needs exact isolated identity, verified stimulus/capture/routes, authoritative
xrun/drop/restart counters, latency distributions and complete rollback.

### MIDI policy and timing

Omni/multi-channel note ownership, per-controller pedal scope, minimum velocity,
soft takeover and preservation of native MIDI timestamps for clock/gesture
timing remain separate design and hardware-audition work. Preserve present feel
for the show unless the Yamaha gate exposes a concrete safety failure.

### Sound, tails and performance

A10/A11/A13, reverb/engine replacement spillover, selective source routing,
high-register aliasing, FluidSynth pre-chain headroom, mono compatibility,
matched-level favorite-patch comparison, controller-to-analogue latency,
render p95/p99/max reserve, fault cycles, eight-hour soak and a complete service
rehearsal remain open. No offline suite closes these musical/hardware gates.

## Decision record after the physical gate

Record commit and native hashes, Pi model/RAM, PipeWire rate/quantum, exact
Peavey/Yamaha identities, selected JACK routes, browser/device used, startup
time, service restart count, required-native status, xrun/MIDI-drop deltas and
each checkbox result. End with one of: **deploy for the show**, **deploy with a
specific documented limitation**, or **roll back to the preserved baseline**.
