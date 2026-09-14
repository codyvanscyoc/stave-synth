# Pi4 engineering validation

Status: proposed release gates, 2026-09-14. These are acceptance criteria and planned tests, not completed test results. Keep sound-affecting work behind the product review in [PRODUCT_VISION.md](PRODUCT_VISION.md).

## Qualification setup

Use a dedicated instance with separate configuration/data directories, ports, JACK client identity, process/service ownership, and output routing. A separate Git worktree alone does not isolate runtime: current code still uses the same home-directory state and fixed ports. Before running an integration test, it must identify its own instance explicitly and refuse the production one. Preserve settings even on interruption; use disposable data for malformed-input and power-loss tests.

Do not run `test_2_param_flood.py`, `test_3_connection_chaos.py`, or `test_4_boundary_sweep.py` against the stage service as currently written. They exercise live endpoints/routing and generic process discovery. The boundary sweep can persist extreme settings and restores only seven fields without a full `finally` restore. The connection-chaos test reconnects JACK itself; it does not prove automatic hotplug recovery or browser reconnection.

The atomic-save test does target a temporary file in a subprocess and monkey-patches directory creation; it is suitable for an isolated test environment. It covers single-writer process-kill behavior, not concurrent writers or real SD-card power loss.

## Musical load matrix

| Case | Exercise | Evidence to collect |
| --- | --- | --- |
| Piano alone | Quiet/loud repeated notes, dense chords, sustain overlaps, pedal release | Velocity/attack, voice stealing, upstream clipping, latency, render timing |
| Piano + OSC1 + OSC2 | Sustained chord changes, low/high registers, both filters, supported unison | Worst render times, queue minima, musical balance, stereo/mono output |
| Full core instrument | Above plus independent tonic/fifth or sampled bed and normal effects | Voice/memory reserve, tail continuity, bed independence, latency |
| Live movement | Continuous MIDI/browser filter, FX and layer faders; rapid release/re-touch | Clicks, zipper noise, control backlog, duplicate/stale commands |
| Atmosphere | Long reverb/shimmer, freeze, drone/bed with no held keys | GC/scheduling impact, finite output, stable memory, intentional release |
| Sound changes | Cold/warm program, preset and effect-type changes | Loading/lock stalls, old/new tail behavior, output discontinuity, temporary CPU cost |
| Extended functions | Organ, splits, macros, transpose, MIDI learn, recorder | Compatibility, bounded load, safe transitions, file completeness |

Use real musically valid settings for capacity qualification. Keep malformed/absurd input tests separate so they cannot define the advertised polyphony or change favorite patches.

## Proposed release gates

| Area | Gate | Conditions and limits |
| --- | --- | --- |
| Preservation | Baseline source and runtime/state backup validated; rollback rehearsed on spare media before release | A code tag is not an SD image or soundfont backup. |
| Boot | 20/20 controlled cold boots to truthful usable state | Test device present, device late, and network unavailable. Initially target UI/engine readiness within 60 s; report device/network readiness separately. |
| MIDI | No lost note-offs/stuck notes; recover intended devices after 20 disconnect/reconnect cycles | Include repeated notes, sustain, transpose/split changes, duplicate port aliases, Yamaha and Dexibell models actually used. Target reconnect within 5 s of OS/graph device availability. |
| Audio routing | Correct stereo route restored after 20 reconnect/profile cycles | Target within 5 s after sink availability. OS failure to enumerate must be visible; do not count hardware absence as an app reconnection pass. No arbitrary fallback sink. |
| UI lifecycle | Bind conflict and worker failure produce truthful degraded state and bounded recovery | During an established performance, preserve healthy audio while recovering the control component where feasible. |
| Safari connection | Sleep/wake, black-hole, Wi-Fi roam, reload, Pi restart, repeated Retry and two clients | On an active browser, target stale detection within 3 s and reconciliation within 5 s after reachability returns. Suspended browsers are judged from resume, not while iOS prevents execution. No stale one-shot replay or duplicate sockets. |
| Persistence | Concurrent writes plus malformed input cannot corrupt or poison persisted state | Unique temporary file/serialization semantics, validated snapshots, previous-state recovery; test restart after rejected values. Real power-cut tests use spare media. |
| Audio validity | No NaN/Inf, unintended silence, or audible clicks in supported gestures | Capture actual output and selected internal stages. Test mono and match listening levels. Distinguish intended silence and saturation from defects. |
| Latency/headroom | Publish repeatable latency distributions and render/scheduler measurements for each supported profile | Controller-to-analogue measurement, including maximum core musical load. No inference of end-to-end latency from ring capacity. |
| Soak | Eight hours with zero post-ready underruns/dropped note events and stable post-warm-up memory | Include full core load, silent/tail periods, controller gestures and two browsers; no deliberate device removal in this continuous-audio run. |
| Fault recovery | Separate fault run recovers within defined deadlines without stuck notes or unsafe output | Deliberate USB/audio loss necessarily interrupts physical output; record interruption and recovery instead of claiming uninterrupted audio during removal. |
| Rehearsal | Full service-length rehearsal accepted by the player | Actual keyboard, interface, hub/power, Safari device, monitoring/PA and intended network. |

A passing eight-hour run is release evidence for that tested configuration, not a proof of zero future failures. Record commit, native binary hashes, profile, dependencies, controller/interface IDs, test program, date, and all exceptions with results.

## Revalidated review findings and qualifications

- **UI false readiness:** server binds happen inside daemon threads; startup does not await bind success, and audio watchdog heartbeats do not supervise UI liveness. Confirmed code behavior; no evidence proves it caused a particular historical browser outage. Reproduce a port conflict and worker failure in isolation.
- **MIDI false success:** `_connect_midi_ports` sets success without checking `jack_connect` return code. Reproduce failed and already-connected cases; health should reflect actual routing.
- **USB disappearance:** kernel errors `-32/-71` and failed enumeration were observed during the earlier live inspection; no physical USB MIDI/audio device was then present. This proves a lower-level device problem, not which cable/hub/device caused it or every past outage.
- **GC timing:** the 30 s worker checks held-note/voice sets before collecting, which does not establish that reverb, freeze, piano release, or a bed is silent. A bridge underrun was observed at the time of a 32.4 ms collection; Dummy Output was active, so an audible dropout was not demonstrated. Find cyclic-allocation sources and ensure bounded memory; do not simply disable GC forever.
- **Queue labels:** normal/low-latency capacity and refill thresholds differ. At 512/48k, three blocks contain 32 ms and eight contain 85.33 ms. Occupancy/phase vary; these are neither total latency nor guaranteed stall protection. Existing 16/43 ms labels assume 256 frames.
- **JACK size/rate:** Python snapshots the size once while the C callback uses current `nframes`; there is no registered size-change handling. Startup sample-rate mismatch only logs. Isolated tests should require safe fail/rebuild semantics for unsupported changes before lower-quantum experiments.
- **Ring reset:** sleeping 20 ms is not an acknowledgement that producer and callback left their critical regions. Reproduce an intentionally stalled callback/producer while changing depth or clearing; define an actual coordination protocol.
- **Native state ownership:** piano-room clear and processing can occur in different threads without a shared owner. Review all DSP reset/backend swaps against active native paths. The Python Haas-buffer concern is primarily a fallback-path risk when Faust pad-bus bypasses those buffers.
- **FluidSynth:** missing the Python lock around `sfload` alone does not prove native corruption; FluidSynth normally has API thread protection. More concrete concerns include render-lock blocking loads, preload/switch/shutdown coordination, and Pi4 dynamic program sample loading under the render lock. Measure the actual Pi4 path, where normal programs share FluidR3.
- **Input/state:** settings can be stored before successful conversion, and saving uses a shared fixed temporary filename across callers. Wrong types/non-finite values plus concurrent saves need dedicated tests before accepting arbitrary browser control traffic.
- **Slow clients:** repeated untracked broadcast futures and sequential sends can accumulate pending work. Test a client that stops reading while another continues controlling the instrument; bound queues and coalesce meter state.
- **System profile:** `cgroup_disable=memory` makes configured memory guards ineffective on the inspected Pi. Actual RT limits currently are `LimitRTPRIO=95` and unlimited memlock, so RT permission failure was not observed here. Linger was enabled. A separate standalone FluidSynth process was active although its audio driver had failed; establish ownership before removing it.
- **Sound quality:** the piano currently receives int16 samples before downstream float processing. Inspect high-level chord capture for pre-conversion clipping before choosing a float path or gain change. No current audible clipping is proven by source inspection alone.

## Implementation order after product review

1. Test-instance isolation, baseline fixtures, and low-overhead observability.
2. Input schema/finite-value validation, transactional state application, serialized persistence and recovery.
3. Truthful health, UI readiness/reconnect, bounded slow-client handling, correct MIDI routing results.
4. Device recovery, supported graph size/rate contract, process supervision, explicit Pi4 hardware profile.
5. Render-owned DSP operations, sample preparation, GC/allocation analysis, ring-reset coordination.
6. Sound comparison and controlled latency/profile optimization.
7. Performance UI refinement, fault/soak qualification, real rehearsal and rollback verification.

Investigations may reorder a fix when evidence shows a dependency. Each patch should state the reproduced trigger, resulting behavior, sound impact, and tests. Avoid bundling tonal redesign with a reliability repair.

## Primary technical references

- [JACK client callbacks](https://jackaudio.org/api/group__ClientCallbacks.html): buffer-size/sample-rate change notification.
- [JACK MIDI API](https://jackaudio.org/api/group__MIDIAPI.html): event frame offsets within process cycles.
- [FluidSynth synth settings](https://www.fluidsynth.org/api/settings_synth.html): API thread protection and dynamic sample-loading behavior.
- [CFFI overview](https://cffi.readthedocs.io/en/stable/overview.html): native calls and Python threading behavior.
