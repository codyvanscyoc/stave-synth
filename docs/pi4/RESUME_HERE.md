# Resume here — Stave Pi4

Saved 2026-09-14 evening, America/Chicago (2026-09-15 UTC).
This is the current operational handoff, **not stage-release approval**.
Read this first; earlier review/deployment notes describe historical checkpoints.

## September17: recorded-pad startup/control path passes target test

User asked to continue and whether playing was needed first. No player test
requested yet. Implemented explicit read-only WAV-library preparation and
immutable startup bank loading plus acknowledged12-key bed level/rise/mellow/
fade/release controls (eee5b25). Actual panel execution guards19aca73.581
reviewed Mac tests, zero skips, Node and full C++/UBSan graph/host guards pass.
Pi source/build `/home/codyvanscyoc/stave-v2-library-20260917.HHKSwP`: all asset
tests and4,000 measured offline blocks pass with no deadline misses. Combined
MUTED real-driver512 test passes94.311s/360 notes/353 controls,0 xruns/over-budget/
raw-piano-full-scale, max5.30067ms, peak two active sampled beds and clean release.

Working stage restoredPID739029,NRestarts0, same cleanf600c7e rehearsal checkout;
HTTP/WS/native/audio health verified48k/512; no candidate/probe JACK client.
Saved current-state bytes unchanged (SHA2f366d3c...), firmware0x0. User pad
library remains empty: both current Pi and inspected September14 backup had
no assigned recordings. Only isolated synthetic C/G TEST fixtures were created;
none installed to user data.8082 test panel is not active. Keep512.

See [library checkpoint](NATIVE_V2_LIBRARY.md). Next: disk worker/WAV durability,
record-to-pad and live immutable-asset replacement, then product UI/persistence/
remaining features and startup/device qualification. Startup bank loading is
now complete; hot replacement and recording are NOT. Recheck current ownership
before any next Pi operation; previous entries below are historical.

## September17: muted finishing window, stage restored

User finished playing and explicitly authorized maintenance. The temporary
listening session was ended, not left running. Piano brightness passed a new
94.299-second MUTED real-JACK512 test:360 notes,341 acknowledged controls,
8,849 callback-block delta,0 xruns/over-budget/full-scale piano samples;
worst5.17846ms. Native sampled-bed graph integration committedcc38d7f and built
on the Pi: ten offline cases/4,000 measured blocks, no deadline misses. Dense
512 with bed mean4.100814/max5.558649ms; this is NOT physical latency evidence.
Original sampled-bed oracle also passes on Pi. Keep512; no live256 switch.

Working `stave-synth.service` restoredPID722136,NRestarts0, same cleanf600c7e
rehearsal checkout; HTTP/WS/audio/native health verified48k/512. Saved current
state byte-identical (SHA2f366d3c...), firmware0x0, no native test client left.
Accepted listening reference and Pi5 branches untouched. Private Pi build/log/
settings archive saved and SHA-verified on Mac. Recheck actual service on resume.

Recorder transport commit8fb7cb3: bounded lock-free SPSC capture optionally
connected to the actual pre-volume/BTL tap; overflow ends only recording.
569 reviewed Mac tests, zero skips, Node UI and full native C++/UBSan guards
pass. Disk worker/WAV finalization and record-to-pad integration NOT done.
The standalone capture guard also passes on Pi/ARM64/UBSan inPqAL9z without
restarting stage (PID722136 unchanged,73.523°C afterward,firmware0x0).
Mac ThreadSanitizer exits139 even for a minimal unrelated thread smoke test;
this is not a successful race-sanitizer check. See
[bed/capture checkpoint](NATIVE_V2_BED_CAPTURE.md) for evidence and exact next
steps. Earlier entries below are historical; do not reactivate a listening
session or repeat a completed build simply because an older paragraph says to.

## September17 after listening: finish integration authorized

Player accepted the limited native512 audition as phenomenal/near-instant with
no heard issues. Keep512; user permits UI changes to prioritize playability.
See [NATIVE_V2_FINISHING.md](NATIVE_V2_FINISHING.md) for the preserved reference,
current local piano-brightness/sampled-bed code, evidence and exact remaining
integration sequence. Listening tag `pi4-native-listening-pass-20260917` keeps
8c1a88a; private accepted settings/telemetry snapshot saved alongside the prior
target evidence. No new code has been deployed to the playing Pi in this batch.
Source commits8b7cdc1 (piano tone) and8db3254 (bed component).567 reviewed Mac
tests, zero skips, Node checks and extra C++/UBSan full-audition guards pass.
Asked asynchronously whether player is finished/muted before future build or
restart; no response yet at this checkpoint. Continue LOCAL work meanwhile.
The time-limited audition below may still run until09:05CDT; recheck ownership.

## September17 08:04 CDT: PLAYER LISTENING SESSION ACTIVE

Initial player feedback: "phenomenal so far" and response "feels instant";
subjective report, not measured end-to-end latency or completed acceptance.
Requested an independent piano filter/brightness slider for a softer, more
muffled tone by song. User explicitly prefers uninterrupted testing if this
is not a quick temporary-screen addition. Source inspection: native graph has
an optional piano filter linked to the pad cutoff, but the running audition
control protocol exposes neither that toggle nor an independent piano cutoff.
Deferred this request until after listening: needs native control/DSP wiring,
smoothing and tests, then a rebuild/restart; not just an HTML slider. No live
engine/UI changes made for this request.

Player arrived and requested access. The old service is now intentionally
inactive (MainPID0). Isolated `stave-v2-listen-zrxtny.service` is active,
PID717421, started08:04:43CDT, using the previously verified binary/source below.
Audition URL: http://192.168.1.203:8082 (numeric IP required). Yamaha MIDI1 and
stereo playback explicitly routed,512 frames, master0 at readiness. Callback
progress verified;187 blocks,4 physical note-ons,0 xruns/over-budget/faults,
max3.26963ms at initial check. No generated MIDI, rebuild, persistent config
change or production source change. The player will raise master gradually.

One-hour native lifetime: expected end around09:04:43CDT. Transient unit has
RuntimeMaxSec3650 and ExecStopPost restoring unchanged `stave-synth.service`.
Independent `stave-v2-listen-failsafe-zrxtny.timer` at65min stops this audition
before starting the old service. Do NOT restart or run stimulus while the
player listens. Recheck actual state on every resume; this entry will expire.
Launcher and log are `listening-start.py` and `listening-ui.log` under the
isolated zRXTny target directory below. This is a temporary controls/defaults
audition, not the saved patch, full product migration or stage qualification.

## September16 post-worship: native-v2 implementation authorized

September17 latest: user authorized current paused maintenance. Target build and
first MUTED real-JACK/PipeWire512 test PASS. See
[NATIVE_V2_TARGET_AUDITION.md](NATIVE_V2_TARGET_AUDITION.md).45 chords/360 notes,
176 HTTP controls,8,911 callback-block delta, zero xruns/over-budget/raw-piano
full-scale/unsupported-channel MIDI; worst callback5.25873ms vs10.667ms budget.
94.525s wall with1Hz telemetry; no audible/analogue/full-feature qualification.
Native source5a22fbe; target executable ready in
`/home/codyvanscyoc/stave-v2-audition-20260917.zRXTny/evidence/audition-jack`.
Working stage restoredPID709387,NRestarts0,cleanf600c7e/same rehearsal checkout;
HTTP/WS/native/audio healthy48k/512, current_state bytes unchanged, firmware0x0.
No candidate/test/compiler remains,8082 closed;565 reviewed tests plus Node pass.
Next is activating the limited512 listening session WHEN THE PLAYER ARRIVES,
then giving the separate8082 URL. Don't mistake normal8080 for native-v2.
Drone/pad/recording deferred until afterward. High-gain parity/full-piano stress
warnings and other full-feature/physical gates remain open. Saved evidence and
restoration procedure in the linked document; no new rebuild needed to listen.

September17 latest priority: user deferred independent drone/recorded-pad/
recording until AFTER the morning audio test. See
[NATIVE_V2_AUDITION.md](NATIVE_V2_AUDITION.md). Implemented a bounded whole-block
control/MIDI adapter, opt-in isolated JACK host and temporary private-IP listening
UI (not full production UI/preset parity). Offline real-graph adapter PCM exact
at512/256, UBSan/session/fake-JACK lifecycle guards and562 reviewed tests plus
Node pass. Starts muted, explicit routes, production-client refusal, truthful
callback-progress health. No core sound change: high-gain parity/piano headroom
findings remain open. Read-only Pi check: service activePID683040, same rehearsal
checkout; no restart/build/deployment. Await current muted/connected maintenance
confirmation, then target build/guards and temporary512 audition with restoration.
Do NOT resume drone/recording migration first or claim field-test readiness yet.

September17 latest Pi result: [NATIVE_V2_PI_PROBE.md](NATIVE_V2_PI_PROBE.md).
User authorized isolated build/testing with PA off, then explicitly authorized
pause/restore of Stave. First build hit our80°C guard. Bounded cooling restored
service; verified DSP-object reuse reduced retry heat. Fixed GCC output-clamp
format warning without math changes. Final Pi build/1,400-block UBSan guards and
eight offline probes completed. Zero over-budget blocks among3,200 measured:
dense transitions512 mean4.064/max5.436ms;256 mean2.027/max3.314ms. NOT live
latency/complete-graph qualification. Dense raw piano full-scale counts4/8 need
headroom review.555legacy/harness tests plus Node pass, output remains exact.
Working stage restoredPID683040,NRestarts0,cleanf600c7e/rehearsal checkout;
UI/native/audio state healthy,48k/512,ring6. Firmware flags0x0. No candidate
deployment or audio routing change, and no test/compiler remains. Next remaining
features/live integration, piano headroom, then expanded Pi qualification.

Latest sound integration: [NATIVE_V2_MOTION_CONNECTED.md](NATIVE_V2_MOTION_CONNECTED.md).
StageInstrument now owns global motion/filter drift, accepted-oscillator key-sync,
poly rate/depth decisions and master sidechain. Four joined merged/nonmerged
reference comparisons pass at512/256, worst4.613e-12, with exact random ownership.
The prior ten runs remain unchanged: independent high-gain1e-6 gate STILL FAILS;
full runner exits1 sound_difference_review_required. UBSan/scoped zero-new guards,
standalone motion recheck and549legacytests plus Node pass. Read-only Pi SSH
succeeded: stage service active,PID185009,NRestarts0,rehearsal checkout. No Pi
write/restart/deployment/stress test. New build remains offline, NOT field-test
ready. Next: recorded beds/drone and remaining functions, bounded live control/
backend, sound acceptance, Pi4 timing and hardware qualification. The separate
Pi build attempt above supersedes this checkpoint's pending maintenance question.

Previous standalone checkpoint: [NATIVE_V2_MOTION.md](NATIVE_V2_MOTION.md).
Standalone StageMotion (two global LFOs/routing/ramps) and FilterMotion
(drift/wobble) pass pinned-original NON-MERGED component comparisons:
25,920 LFO blocks, worst synthetic-audio1.077e-14;6,000 filter blocks, cutoff
worst3.638e-12Hz, exact walk states/draw counts. UBSan/scoped zero-new guards
and549legacytests plus Node pass. These components are NOT connected to
StageInstrument yet; next integrate them in original source/filter/FX order,
including key-sync, poly ownership and merged-path comparisons. No Pi contact
or deployment. Prior full-graph high-gain1e-6 gate still open; no field-test or
live256 claim. See the new document's explicit integration boundaries.

Previous split checkpoint: [NATIVE_V2_SPLITS.md](NATIVE_V2_SPLITS.md).
StageInstrument now owns raw-key piano/OSC1/OSC2/shimmer split ranges and
smoothstep crossfades through key_command(). 438,272 weights match the pinned
original exactly. New full-chain split fixtures pass at512/256, including
positive/negative transpose, octave, silent/partial layers and pedal releases;
worst internal difference1.885e-9. Original eight comparisons retained: original
source passes, high-gain independent1e-6 still fails unchanged; full runner
exits1 (sound_difference_review_required). UBSan/scoped zero-new guards and
549legacytests plus Node pass. No Pi contact/deployment. Next: remaining
modulation/bed/organ/control features, bounded live integration and qualification;
not field-test ready. Musical ReleaseAll preserves tails; hard STOP is separate.

Previous output checkpoint: [NATIVE_V2_OUTPUT.md](NATIVE_V2_OUTPUT.md).
StageInstrument now reaches final float32 PCM through the original master
volume smoother/clamp/BTL math, with the pre-volume recorder tap preserved.
Output component and connected same-input output match exactly. Overload
discrepancy is traced to adjacent float32 reverb-input values; an independent
original reverb fed native input reproduces native wet output EXACTLY in all
eight runs. Original-source sound fixture passes; overload strict 1e-6 gate
still fails and full runner exits 1 (sound_difference_review_required).
UBSan/zero-new guards and 549 legacy tests plus Node pass. No Pi contact or
deployment. Next: remaining feature/event/control/backend integration and
separate sound acceptance; not a field-testable replacement or live 256 claim.

Previous connected checkpoint: [NATIVE_V2_INSTRUMENT.md](NATIVE_V2_INSTRUMENT.md).
StageInstrument connects actual Faust/FluidSynth sources, piano room/filter/
soft clip/sends, pad/delay/reverb/wet filter and master under one offline owner.
Original-source full-chain comparisons PASS at 512/256 (worst 3.030e-10), but
added high-gain piano stress FAILS strict 1e-6: 2.795e-6 / 2.027e-5. Identical
prepared-piano isolation reduces downstream difference below 4.4e-12. Final
runner exits 1, sound_difference_review_required; do NOT call it fully passed.
UBSan/zero-new guards, old core/ambience rechecks and 549 legacy tests plus Node
pass. PRE bridge gain; remaining features/live control/driver/UI still open.
Next: trace stress-case first divergence, final output and remaining features.
No Pi contact/deployment; no field-testable replacement or live 256 claim.

Previous master checkpoint: [NATIVE_V2_MASTER.md](NATIVE_V2_MASTER.md).
Native master EQ/HP/shuffler, compression/sidechains, FX bypass, saturation and
limiter pass eight offline comparisons at 512/256; worst audio difference
5.106e-10. Default limiter ceiling and exact-threshold zero-knee corrections
are explicit, independently tested differences from v1.2. UBSan, scoped zero-new
guards and 549 legacy tests plus Node pass. Output remains PRE bridge gain;
source/FX/master ownership is not yet joined. Next: wet filter/modulation and
owned full graph/remaining features. No Pi contact or deployment; NOT a
field-testable instrument, and live 256 remains unsupported.

Previous effects checkpoint: [NATIVE_V2_SHARED_EFFECTS.md](NATIVE_V2_SHARED_EFFECTS.md).
Native delay and all seven shared reverb types pass exact output/control-state
comparisons, including freeze and backend changes. Composed pad/filter/delay/
reverb/split routing passes at 512/256 with worst difference 1.665e-16, including
muted sources and external sends. UBSan, scoped zero-new guards and all 549
legacy tests plus Node pass. Saved source stems are used for composition; the
new effects are NOT yet joined to StageCore's source acquisition. Next: wet
filter/modulation and master processing, then full source/output ownership.
No Pi contact/deployment; still not a field-testable replacement instrument.

Previous core checkpoint: [NATIVE_V2_CORE.md](NATIVE_V2_CORE.md).
StageCore now owns source faders, voices/Faust/FluidSynth/piano processing,
pad/filter/send buses and piano room. Composed eleven-channel comparisons pass
at 512/256: first nine exact, piano-room worst difference 1.112e-13. All-muted
pause/re-entry and continued piano are tested. UBSan, a scoped zero-new probe,
component/source/bus rechecks and 549 legacy tests plus Node pass. Next: shared
effects and master routing, remaining modulation and control/feature coverage.
No Pi contact/deployment; still not a field-testable complete instrument.

Previous scalar checkpoint: [NATIVE_V2_SOURCE_MIX.md](NATIVE_V2_SOURCE_MIX.md).
SourceMix now preserves stage fader smoothing/dB mapping, hard-pan and source/
shimmer/Haas scalar decisions:5,504pinned-reference blocks pass, worst1.111e-16;
UBSan passes. Separate component, not yet the full source/bus/control owner.
The composed-core checkpoint above now covers that owned composition and its
fixed-three-unison all-muted Faust/fallback transition tests.
No Pi contact/deployment; not a morning native-v2 field-test candidate yet.

Previous checkpoint in this batch: [NATIVE_V2_BUSES.md](NATIVE_V2_BUSES.md).
Native PadBus + PianoRoom now compose as StageBuses:11 internal channels,
verified against pinned scalar/routing code and actual Faust at512/256.
Saved source-stem results are exact; synthetic worst difference6.939e-18.
UBSan guards and549legacytests+Node pass. No Pi contact/deployment. NOT a
field-testable successor; no complete master/FX/control/browser/device path.
The scalar fader adapter above advances that checkpoint's next step; owned
source/bus integration and its all-muted-path gap are covered by StageCore above.

Earlier room checkpoint: [NATIVE_V2_PIANO_ROOM.md](NATIVE_V2_PIANO_ROOM.md).
The native piano-room downstream component passes exact pinned-reference
output/smoother comparisons at512/256, including actual piano stems, controls,
tails and clears. UBSan guards and549legacytests+Node pass. It is not yet wired
into StageSources or a full effects/master graph. No Pi contact or deployment.
The bus composition above supersedes this earlier next-step note.

Earlier integrated-source checkpoint: [NATIVE_V2_SOURCE_GRAPH.md](NATIVE_V2_SOURCE_GRAPH.md).
StageSources now integrates keys/pedals/voices, actual Faust and FluidSynth,
and the piano chain into a single-owner OFFLINE boundary-cadence source graph.
Seven stems pass pinned-reference comparisons at512/256: oscillator outputs
exact, piano worst difference1.854e-13. This is not the downstream FX/master
graph or live scheduler/UI integration. M2 remains open. UBSan graph/chain
guards and549legacytests+Node pass; no Pi contact/restart/deployment this batch.
The separate room comparison above advances part of its downstream work;
pad/filter routing, composition and remaining control/event integration remain.

Earlier overnight checkpoint: [NATIVE_V2_M2_COMPONENTS.md](NATIVE_V2_M2_COMPONENTS.md).
M2 components now pass pinned-v1.2 comparisons: envelope11,340blocks,
key/pedal10,652commands, voice7,101commands, dry piano524,288samples per
cadence512/256. Scheduler priority terminal STOP implemented; UBSan-only core/
components pass, ASan still unverified. Existing549tests and Node checks pass.
No Pi contact/restart/deployment in that batch. M2 integration remains open:
the new components are NOT yet wired into a complete native instrument.
Start with the exact integration work in that checkpoint, not a live deployment.

This worktree is `pi4-native-engine-v2`, separate from preserved pi4-stage-pro.
The user reports tonight's512-frame build played well under a beginner/light
workload, then explicitly authorized preserving v1.2 and implementing a unified
successor. Read [NATIVE_V2_PLAN.md](NATIVE_V2_PLAN.md) and native_v2/README.md.
The historical freeze below is not a prohibition on this offline development;
it remains a reminder not to interrupt the normal Pi without a maintenance window.

Preserved tag pi4-v1.2-stage-snapshot-20260916 targets57bb94c; complete Git bundle
and post-worship config archive are private. Actual Pi app remains cleanf600c7e,
PID185009/invocation4e958aafdb1341d7958af803acd8ae4d, graph512/48000 and Yamaha
policy unchanged at21:43CDT. No native-v2 deployment, service restart, audio
device opening or live stress test is authorized by prototype test commands.
Source/compiler tests and offline rendering on Mac do not qualify Pi4 timing.

Implementation: native_v2 has a bounded sample-position event scheduler and a
separate offline whole-block StageCore owning voices/ADSR, piano/Faust sources
and filter/room buses. They are not yet integrated with each other or the full
FX/bed/organ/scene behavior and UI; follow milestone-specific evidence.

Earlier M0/M1 checkpoint: [NATIVE_V2_CHECKPOINT.md](NATIVE_V2_CHECKPOINT.md). Core plus
actual Faust/FluidR3 offline tests and both diagnostic WAVs pass; fixtures are
identical at512/256, NOT live Pi4 qualification. Existing549-test suite and
Node checks passed. Combined ASan/UBSan was unverified due to pre-main stalls;
the later UBSan-only component pass is scoped above. M2-M6 remain open.

## Tonight's freeze and deferred architecture question

User deferred further optimization until AFTER the September16 worship
session. Leave512 and the saved Yamaha output tuning alone; no further
deployment, buffer changes or restarts during the session.

Saved user question for afterward: How do dedicated keyboards such as the
user's Dexibell or a Nord achieve their responsiveness and instrument quality
within their hardware resources? Do samples cost less than synthesis, and did
we choose the right Stave architecture/languages (Faust, Python, native code,
FluidSynth) for a software instrument on available Pi4 hardware?

Research and discuss later, not tonight. Verify model-specific public facts;
do not assume these keyboards actually use little CPU/RAM or invent proprietary
internals. Compare sampling/streaming, DSP/voice/effect costs, real-time audio
scheduling and Stave's measured bottlenecks. Distinguish appropriate language
choices from implementation/queue overhead. No redesign is authorized solely
by saving this question; preserve the loved sound and revisit after feedback.

## September 16 afternoon — 256 trial rejected, 512 restored

Player reported "Already crackles everywhere. Can't handle it." A fresh
60.03-second observation initially had no new notes, then received106 MIDI
note-count increments. Underruns28->72 (+44), late renders345->1479 (+1134),
xruns0, piano misses0, MIDI drops0. This mixed idle/playing result FAILS audio
continuity; first idle-only success did not predict playable stability.

Restored clock.force-quantum0 with Stave stopped, then restarted normally;
default512 remains configured. Yamaha output improvement remains installed,
volume1.00. PID185009, invocation4e958aafdb1341d7958af803acd8ae4d, zero restarts.
No presets, effects, voices, source/native code or persistent files changed.
Do not re-enable256 or add it as a supported UI mode based on these tests.
Further latency work needs profiling/optimization and separately qualified
profiles; this test does not prove which component is the limiting cause.
Private evidence: quantum256-20260916.W4UHIL/recheck-readiness.json.
Rollback check30.06sec:50 new MIDI note counts (first10sec), zero new
underruns/xruns/piano misses/MIDI drops,6 late renders, callbacks/renders+2812.
Actual512/native/audio/UI healthy. Monitor finished; no diagnostic loop left.
This short mixed playing/idle check is not a full rehearsal qualification.

## September 16 afternoon — historical 256-frame trial (rolled back above)

Player requested faster response after more playing and explicitly authorized
the256 trial/restart. Stave now runs48000Hz/256frames, unchanged six-slot/refill3
queue and Yamaha output tuning. Only runtime PipeWire clock.force-quantum=256
changed from0, with Stave stopped before the change. Persistent default512
configuration is untouched; PipeWire restart/reboot returns to its default.
Application remains f600c7e/bd6a517; no new DSP/UI/source deployment.

PID180575, invocation69818ed02ffd45c6af5b4009319f7668, active/zero restarts.
60.11-second idle check: underruns15->15, xruns0->0, late renders68->108 (+40),
callbacks/renders each+11258; no notes played, no piano misses or MIDI drops,
native/audio/UI healthy. This is NOT full-load qualification or a strict
timing pass. Leave only for the requested listening test, pending feedback;
do not silently approve tonight's performance on the strength of idle results.

Stopped-runtime backup before-256.tar is SHA256
3af8dd38f8010a84811f810fd4a225df68726075bc0d5bc1ec3b696cb5715480,
verified on Pi in stave-synth-pi4-backups/quantum256-20260916.e6YKWV and Mac
Documents/stave-synth-pi4-backups/quantum256-20260916.W4UHIL (INDEX/readiness).
Rollback in a muted authorized window: stop Stave, set clock.force-quantum0
with pw-metadata, start Stave, verify actual512 and route/health. Keep Yamaha
rule and95/90 overrides. Never change quantum while Stave is running.

## September 16 morning — Yamaha latency profile is persistent

The player approved the quicker response and requested persistence. Installed
`config/pi4/51-stave-yamaha-mx-latency.conf` into the Pi user's WirePlumber
configuration. It matches only the observed Yamaha MX stereo output and sets
period-size128; no generic-device, sound, voice or Stave buffer changes.
See [YAMAHA_LATENCY_PROFILE.md](YAMAHA_LATENCY_PROFILE.md) for scope and rollback.

Verified a stopped-Stave WirePlumber restart recreated the output and reapplied
hardware period64/headroom64. Normal Stave PID33382, zero automatic restarts,
correct stereo links and volume1.00. State bytes unchanged. A subsequent
60-second IDLE check had zero new underruns/xruns/late renders/piano misses/MIDI
drops; callbacks+5628/renders+5627. No notes played during this check, so it is
not a full-load playing pass. Physical power-cycle/replug remains untested.
Mac offline suite:549 Python tests, zero skips, plus Node checks. Runtime
application remains f600c7e/bd6a517; only the external audio policy was deployed.

Private stopped-runtime backup `before-persistence.tar` is on both Mac and Pi
in the latency-20260916 directories below. Monitors have ended. Oscillator
click repair is still pending; do not infer it was included in this change.

## September 16 — historical temporary Yamaha trial (superseded above)

Latest runtime application is still unchanged f600c7e/bd6a517 in the rehearsal
checkout; do not assume newer local notes imply a deployed source change.
After the player reported subtle piano delay even through Yamaha headphones,
they authorized muted/off-stage changes. A runtime-only PipeWire setting for
the verified Yamaha output changes api.alsa.period-size from automatic0 to128;
after a stopped-Stave output suspend/reopen this gives hardware period64 and
headroom64 instead of256/256. Graph48000/512 and Stave low-latency6/refill3,
voices, effects and saved settings remain unchanged. This is TEMPORARY and
may reset on output recreation/session-manager restart/reboot. Await player
comparison and longer playing before deciding persistence; no permanent rule.
Player subsequently confirmed: "It feels just right now." This is subjective
acceptance of the trial response, not full-load qualification or permission to
claim measured end-to-end latency. Keep this target; busiest-patch continuity
and a reversible persistent Yamaha-only rule remain next steps.

At 10:33 CDT, active PID28914, invocation71fec46c941b4ca6b050f018a7d4fba6,
zero restarts, Yamaha stereo routed at volume1.00. A120-second check saw300
MIDI note-count increments (playing only in the latter part), zero new bridge
underruns/xruns/piano misses/MIDI drops, but15 late renders. Driver delay sample
mean22.97ms before versus18.86ms after is NOT calibrated MIDI-to-analogue latency.
See REHEARSAL_CHECKPOINT.md for player findings, caveats and rollback details.
No monitor/compiler/test process remains running after this readiness check.

Mac private backup/evidence:
`Documents/stave-synth-pi4-backups/latency-20260916.jH9NwW/INDEX.md`.
Pi archive: `stave-synth-pi4-backups/latency-20260916.bDF3jl/runtime-before.tar`.
Original stage95/90 overrides are untouched. The previous documentation's
service PIDs and saved-state hashes are historical, not current handles.

New player issues: oscillator-toggle pops/clicks (short fade acceptable), OSC2
alternate-control discoverability, and interface-level UI/recall. ART USB DI
still fails OS enumeration; no root cause proven. Required in-process piano
FluidSynth remains; separate packaged daemon is an unremoved cleanup candidate.
No oscillator-click fix has been implemented or deployed. Eight-hour soak
remains waived. The player continues testing; never infer maintenance permission
for a later performance from today's authorized window.

## September 15 — current rehearsal runtime, supersedes all older entries

The corrected application **`bd6a517` is now active** in the normal service,
from `/home/codyvanscyoc/stave-synth-pi4-rehearsal`, through removable user
drop-in `95-pi4-continuity-candidate.conf`. The existing 90 override, clean
`ce15cfb` stage checkout and original checkout are preserved. Actual software
rollback to ce15cfb was exercised, then the corrected build was selected again.
Read [REHEARSAL_CHECKPOINT.md](REHEARSAL_CHECKPOINT.md) before remote work.

Verified PID **374463**, invocation `c09b79d7548f4acc87bc81b92d7205fc`, active,
zero restarts. Final 120-second normal-service observation had zero new
underruns/xruns/over-budget cycles, with advancing render/callback counts.
Stage HTTP/WS, native/audio/control/UI health passed. Normal saved-state SHA256
remains `ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
No generated test pads or private preset fixtures were copied to normal data.

Latest small correction replaces 128 native piano note-off crossings with a
pedal-safe channel release. Twelve actual native comparisons have sample-exact
release tails, at much lower release CPU cost. **546 Python tests pass on Mac
and Pi, zero skips**, plus Mac Node checks. See
[PIANO_RELEASE_BOUNDS.md](PIANO_RELEASE_BOUNDS.md).

Performance5/6 completed with zero piano misses throughout and zero pre-panic
bridge underruns; strict totals retain 68/68 over-budget cycles and 1/2 terminal-
window bridge underruns. Core rehearsal2 completed 600 musical seconds with
zero bridge/source/event/invalid/zero-music-block failures, but 25 musical and
one later over-budget cycle. Strict failures remain recorded. Organ headroom
and hardware latency are still unqualified; no sound/voice/buffer reductions.

**The player explicitly declined the eight-hour soak.** It is no longer a task
for this release effort; do not call it passed or start it unattended. Continue
with bounded tests and real keyboard/interface/Safari/player rehearsal. No
test, capture or compiler is left running at this checkpoint. The remaining
organ/headroom work is not certified complete by these software successes.

Old private capture wrappers hard-code the normal ce15cfb checkout and must
NOT be reused unchanged now that the 95 override selects rehearsal. Their
restoration source/PID/commit guards must be updated/reviewed before another
test. Documentation-only save commits may follow application bd6a517; verify
the actual checkout and service ownership rather than guessing from branch HEAD.

## September 14 remote continuation — historical, superseded above

Latest application repair is `f89f127`, with tools/evidence through `ba8b68a`;
both are published on Pi4 only. Neither is deployed to normal stage. The
render-owned preloaded piano program handoff passed two actual captures with
zero pre-panic piano misses or bridge underruns. All musical operations and
state restoration completed. Strict total deadline growth remains86/78;
take4 has one terminal `all_notes_off` source miss. See
[PIANO_PROGRAM_CONTINUITY.md](PIANO_PROGRAM_CONTINUITY.md).

Full 543 Python tests passed on both Mac and Pi, zero skips. The final Pi run
on `ba8b68a` completed in 57.203 seconds. Mac Node UI/syntax checks also passed;
Node is not installed on the Pi.
No voice, unison, sample-rate, buffer or effect-quality reduction was made.

The actual combined-core20s smoke and600s rehearsal now completed. Ten-minute
music: zero underruns/xruns/source misses/dropped events/invalid or zero music
blocks, but27 render-deadline increases, hence strict FAIL. Mean render6.546ms,
p99 upper histogram bound8.747ms against10.667ms period for this fixture.
RSS313484→314272KiB, sampledmax314396KiB, no swap/throttle,68.653–69.627C.
This is not physical latency, eight-hour memory or player qualification.

Owned supervisor SIGKILL recovery restored normal service with verified health
and unchanged normal state. Coordinator could not restore its private scene
while the whole group stopped; failures were retained, interrupted private
bytes backed up and the stopped disposable state recovered from verified
original evidence. Normal saved settings were not replaced.

Extended preset/macro/hall/plate/wash sequence completed and restored, including
two-peer convergence and current/library byte preservation. Musical window:
zero bridge/source misses,8 deadlines. Total strict FAIL retains2 terminal
underruns,2 source misses,9 deadlines. Autosave history legitimately advanced
and its original bytes were backed up; no false all-history-bytes claim.

Scheduling A/B/B/A experiments completed on private source `ba8b68a`.
Background CPU0–3 versus CPU0–2 (render CPU3/FIFO80, unchanged5ms interpreter
interval) produced deadline increases52/32/50/39 and piano misses1/0/1/0;
bridge underrun growth was zero. No consistent improvement was established,
so neither affinity nor interpreter changes were adopted. Most earlier stalls
do not overlap retained autosave/control spans; causality remains unresolved.

Organ-heavy workloads still cost about9.9ms/block. Algebra, triple-only and
exact-zero guard variants failed the predeclared numerical-parity gates;
vector compiles timed out. A reference rebuild of unchanged source is sample
exact, validating the comparison baseline. The zero guard's three-active-voice
microbenchmark is promising (~74% faster), but is NOT an accepted optimization,
full-polyphony result or installed library. No sound-preservation gate was relaxed.

Private `CORE_REHEARSAL_EVIDENCE.md`, `EVENING_RESULTS.md` and INDEX record
evidence paths and recovery details. Core batch1 and the extended/affinity/organ
archive are complete, hash-verified on Mac/Pi. Later archive SHA256:
`55ccf1399d5741f7a7025e4a093ad550bdc8ce7c958cdcbea520656f1b003d5a`.
Mac extraction: `Documents/stave-synth-pi4-backups/evening-evidence-20260914.x1vHxy`.

At 23:04 CDT, normal service was active, PID219776, invocation
`43fdcaa4e27241ebbd71886c69bcab0e`, zero restarts. Stage HTTP/WS, native profile,
audio/control/UI health rechecked; pending/dropped controls and MIDI drops zero.
Normal source is clean `ce15cfb`, saved-state SHA remainsed8566e5…58cb3e.
No capture, soak or compiler is left running. Recheck owners before resuming.

Next work is bounded timing/continuity repair and repeat evidence, then a
reversible candidate activation for hardware rehearsal. Latest repairs are
NOT the current normal-stage runtime. Do not silently promote failed numerical
organ experiments or promise stage readiness. Eight-hour soak, analogue
latency, actual USB/Safari/rehearsal gates remain open. No eight-hour job is
running unattended and no full-project completion is claimed.

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
