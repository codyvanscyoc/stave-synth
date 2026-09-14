# Stave Synth Pi4 — complete source audit

**Result:** the complete tracked first-party source review is finished. Stave has a substantial, working musical foundation worth preserving, but this baseline is not yet qualified as a professional stage appliance. Several reproducible correctness defects affect notes, emergency stop, persistence and recording; important deployment and test gaps can hide failures.

Reviewed 2026-09-14 on `pi4-stage-pro`, commit `4c64f3305a611462c01faa3ad4233f40042f3dcc`. Application source remains the preserved Pi4 baseline. Four reviewers read all 74 tracked files: 73 text files totaling 30,720 lines, plus the logo inspected visually. See the [per-file coverage and hashes](REVIEW_COVERAGE.md).

This corrects the earlier overly broad use of “complete review.” The claim now means every tracked first-party file was accounted for and read, with subsystem cross-checks and selected isolated reproductions. It does not mean every possible bug has been found, third-party dependencies have been audited internally, or the Pi has passed real-hardware qualification.

## What the instrument already is

The code implements the intended headless live instrument: piano, two oscillator layers, substantial Faust DSP and effects, an organ, MIDI controls/pedals, split zones, browser mixing, presets/setlists, recorder and sampled beds. The familiar five-fader workflow and the sound already enjoyed on stage should remain the baseline. No framework rewrite or new synth engine is required by this review.

Your original **record the sound, then use that recording as a pad** concept is present: record a take, select it in the library, assign it to one of the twelve C–B slots, then trigger/fade its looping player. Assignment copies that WAV to that slot; it does not automatically extract a seamless sustaining region, detect its root, transpose it, or fill all twelve keys. A root-and-fifth recording can be the bed. Empty slots are currently silent; the removed live-synth fallback is not automatically a bug to restore. The reverb algorithm called DRONE is separate from the sampled bed.

The main problems are lifecycle, ownership, bounded resource use and trustworthy state—not a lack of musical features.

## Release-blocking priorities

P1 below means resolve before release for the affected supported feature; it does not mean every issue occurs on every song. Detailed reports distinguish reproduced defects from static risks and previous observations.

| Priority area | Concrete finding | Evidence and stage consequence |
| --- | --- | --- |
| Emergency stop / held notes | Panic does not stop sampled beds or fully clear pedal state; native organ retriggers can orphan gated voices | Core pedal and audio organ logic reproduced; sample-panic gap statically traced. STOP must have one authoritative meaning. |
| Piano / pedal correctness | Octave-only retrigger of a sustained piano note can strand the old pitch (P1); related sustain/sostenuto overlap can release notes prematurely (P2) | Exact MIDI-loop reproductions. These affect musical playing, not just unusual settings. |
| Persistence | Simultaneous saves can expose partial JSON; settings lack complete validation; presets include global setlist libraries | Concurrent-save and bounded EQ-index growth reproduced. Bad state can survive restart; setlist/preset workflows can compound storage. |
| Recorded pads | Long takes decode fully into float64 RAM; replacing one slot reloads and stops the whole bank; invalid/empty/very short samples are unsafe | Whole-bank lifecycle and memory paths traced; empty/tiny sample cases reproduced. One 30-minute stereo 48 kHz take alone occupies about 1.38 GB of decoded sample data, before temporary copies and the rest of the app. |
| Recording integrity | Two takes started within one second reuse and overwrite the same filename; queue/write failures do not provide truthful recording status | Timestamp overwrite reproduced with real Recorder methods and disposable WAVs. |
| Startup / connection truth | HTTP/WS bind failures can coexist with READY and healthy audio; MIDI routing reports success without checking connection results | Static lifecycle tracing. Matches a possible audio-alive/UI-dead failure shape, but is not proof of a historical incident's cause. |
| Browser / headless control | MIDI macros depend on a connected browser and duplicate commands with multiple browsers; reconnect callbacks can act on a newer socket | Client/server control flow traced. Phone/iPad sleep and two-screen use must not alter instrument semantics. |
| Real-time pipeline | JACK block-size changes can emit stale audio or discard samples; ring resets rely on a 20 ms sleep; GC's idle detector ignores sounding beds/tails | Block-size failures reproduced against unchanged C. Ring reset and GC require controlled timing tests and ownership fixes. |
| Native DSP lifecycle | Organ voice-slot reclamation, freeze/panic state, and clear/process ownership have concrete defects or races | Exact wrapper/state reproductions plus full native-source/bridge review. Required fast-path engagement must be checked before readiness. |
| MIDI overload | A full event ring silently drops note-offs; expensive controls share the MIDI dispatcher | Unchanged C reproduction. Add bounded work, explicit overflow telemetry and release-safe recovery. |
| Deployment / tests | A dev launcher can kill the live process; tracked service/launcher omit PAD_BUS, PIANO_CHAIN and MERGED flags from the documented native profile; stress tests can mutate production and falsely pass | All installer, service, test and tool source reviewed. Existing green results are not sufficient release evidence. |
| Network safety | Any reachable peer can control settings, change routes and delete recordings without authorization | All-interface HTTP/WS exposure and message handlers traced. Define a stage-network trust/pairing boundary before shared-network use. |

Other important findings include incomplete scene recall, partial-failure preset swaps/setlist loads, inconsistent two-screen/recording state, weak shutdown/recorder ownership, non-finite native gain recovery, fixed-quantum latency labels, a missing-soundfont recursive fallback, and phone portrait layout gaps. Details below include exact source references and qualifications.

## Detailed review package

- [Core / MIDI / JACK / persistence / recorder](review-evidence/core-report.md)
- [Audio / piano / organ / Faust / sampled pads](review-evidence/audio-report.md)
- [Browser / WebSocket / presets](review-evidence/ui-report.md)
- [Boot / installer / services / every test and tool / documentation](review-evidence/boot-test-report.md)
- [Coverage ledger: all baseline files, reviewers, line counts and SHA-256](REVIEW_COVERAGE.md)
- [Reproduction fixtures and safe invocation](review-evidence/README.md)

## What was actually verified

- Every tracked source/configuration/documentation file was read; the asset was inspected. No application code changes were made.
- All 37 tracked Python files passed AST parsing; browser JavaScript and all three shell scripts passed syntax checks. Syntax success is not functional proof.
- Eight core reproduction checks completed with exact AST-extracted methods and disposable data; four native-bridge checks completed by compiling unchanged bridge code against mock JACK. Seven audio-specific checks reproduced exact Python/wrapper failures with native calls mocked, not acoustic output. The core fixture was also independently rerun by the UI reviewer.
- Reports include source paths/lines, triggers, confidence, active-versus-fallback distinctions and missing tests. Reproduction assertions intentionally expect the current bug; a successful probe is not a passing application regression test.
- The Pi4 baseline, running stage checkout, installed configuration and Pi5/Mac branches were not changed. This turn added only local review documents and isolated evidence fixtures. No report was pushed or deployed.

Previous live inspection is supporting context only: the Pi showed USB enumeration errors, no physical USB MIDI/audio device and Dummy Output. RT allowance 95, unlimited memlock and realtime rendering were observed, so missing RT permission is not established as the current fault. Memory cgroups were disabled despite configured service limits. A GC pause and underrun increment coincided, but Dummy Output prevents calling that a demonstrated audible dropout. See [baseline](BASELINE.md). Separate earlier inspection also found linger enabled; that observation is not a fresh-install boot test.

## What remains before a stage-ready claim

1. Establish a genuinely isolated test instance: separate state/data, ports, JACK identity, service/process and routing. A second checkout alone is unsafe.
2. Repair reproduced note/panic/persistence/recording failures and add deterministic regressions. Keep tonal changes separate from reliability changes.
3. Establish truthful listener/MIDI/output readiness and bounded browser/device recovery; verify the exact required native Pi4 build/profile.
4. Qualify sample sizes, voice limits, control work, native ownership, GC/scheduling and graph-size/rate behavior against the busiest musical patch.
5. Capture favorite sounds and compare matched-level output before accepting changes to envelopes, piano gain, effects, velocity policy or limiter behavior.
6. Measure controller-to-analogue latency and render/scheduler distributions on the real Pi4/interface. Then choose the lowest stable buffer profile; do not infer latency or reserve from average CPU or ring capacity.
7. Run the [planned boot, USB/audio/MIDI recovery, Safari, eight-hour soak, rehearsal and rollback gates](ENGINEERING_VALIDATION.md) with the actual keyboards, hub/power, interface and network.

No arbitrary reduction in unison, layer count or audio quality is recommended merely to claim more headroom. The desired piano + OSC1 + OSC2 + effects + optional independent bed must be the reference load. The next implementation step is isolation and regression fixtures, followed by small evidence-backed fixes on the Pi4 branch—not a live deployment of a large rewrite.
