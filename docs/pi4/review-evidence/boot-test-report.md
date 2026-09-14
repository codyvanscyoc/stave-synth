# Stave Synth Pi4 stage — boot, install, test, tooling, and documentation audit

Scope: commit `4c64f33` on `pi4-stage-pro`, reviewed read-only on 2026-09-14. This report covers the installer, launch/service definitions, packaging/dependency metadata, every test and tool, distribution/licensing metadata, and the Pi4/project documentation named in the assignment. It does not repeat the separately delegated application/audio-engine review.

Validation performed: full source read of every covered file, Python AST parsing of every `tests/*.py` and `tools/*.py`, `bash -n install.sh stave-synth.sh`, Git inventory/status checks, and static cross-checking of environment flags and tracked assets. No installer, package download, live service, JACK/MIDI disruption, benchmark, or production integration test was run.

## Executive result

The current checkout is not release-gate-ready as a reproducible Pi4 appliance. The most consequential defects are: the manual launcher can kill the production instance; a clean install omits three Pi4-native fast-path flags that the project history says are live/required; the live tests are not isolated and several can pass without exercising or validating their stated failure; and installer upgrades preserve stale machine configuration rather than reconciling it. The Pi4 documentation correctly acknowledges most qualification gaps, but README/legacy planning claims still overstate what a clean clone installs and what the existing tests prove.

Known target qualifications are preserved: the inspected live Pi reportedly had effective RT priority allowance 95, unlimited memlock, and linger enabled. Those observations do not prove a fresh install will reproduce them. The same captured target had `cgroup_disable=memory`, so its systemd `MemoryHigh`/`MemoryMax` controls were ineffective.

## Prioritized findings

### P1 — manual launcher kills any Stave Synth process, including the stage service

- Evidence: `stave-synth.sh:22-24` runs `pkill -f "stave_synth.main"` before launching its own checkout. README repeats the same broad stop command at `README.md:28`.
- Trigger: run the casual launcher or follow README from a development checkout while the production user service is active.
- Failure: every matching instance is terminated without checking checkout, service ownership, JACK identity, ports, or data paths. This directly defeats the isolation contract in `AGENTS.md:9` and `docs/pi4/ENGINEERING_VALIDATION.md:5-9` and can interrupt a live instrument.
- Action: remove generic process killing. Give each instance an explicit identity/PID/service and refuse conflicts; make development/test launchers require isolated config/data, ports, JACK client name, and routing.
- Confidence: high; direct command behavior.

### P1 — clean installs do not reproduce the native Pi4 configuration documented as deployed

- Evidence: `config.py` defines `STAVE_FAUST_PAD_BUS`, `STAVE_FAUST_PIANO_CHAIN`, and `STAVE_FAUST_MERGED` as opt-in flags, but neither `systemd/stave-synth.service.d/faust.conf:5-12` nor `stave-synth.sh:8-20` exports them. `FAUST_PORT_PLAN.md:418-425` says piano-chain was deployed, and `FAUST_PORT_PLAN.md:621-629` says all four port phases were live on Pi4, including merged. `README.md:239-247` claims Faust is enabled by default in both install modes.
- Trigger: fresh clone plus `./install.sh`, or casual launch, without an older hand-edited runtime drop-in.
- Failure: pad-bus, piano-chain, and merged paths remain disabled. The fresh install therefore differs from the measured/deployed Pi4 configuration and retains more Python glue on the CPU-constrained target. The installer builds these libraries but does not activate them.
- Action: make the tracked drop-in and launcher the authoritative, complete flag set for the qualified Pi4 profile; verify engagement from logs/health before READY. If any path is intentionally not yet qualified, update the deployment claims and profile accordingly.
- Confidence: high; direct flag cross-check. Exact performance impact requires Pi4 measurement.

### P1 — connection-chaos can pass without performing any disruption

- Evidence: if no audio pairs are found, `tests/test_3_connection_chaos.py:91-95` only appends an `ERROR` tuple and returns. The final failure expression at `:196` ignores that error, `disc_count == 0`, disconnect/reconnect return-code counts, and the absence of recovery observations.
- Trigger: wrong JACK graph, wrong instance, missing ports, or parsing failure.
- Failure: the script can report a passing exit code with zero disconnect cycles. Even when it runs, reconnect failures are printed but do not necessarily fail; `recoveries_fail > recoveries_ok` at `:196` allows a 50% recovery rate to pass.
- Action: fail preflight unless exact isolated instance/ports and at least the required cycle count exist; require every operation and recovery deadline to pass.
- Confidence: high; deterministic control flow.

### P1 — the three live stress tests can target production or combine evidence from the wrong instance

- Evidence: generic process discovery in `tests/test_2_param_flood.py:35-40`, `tests/test_3_connection_chaos.py:28-33`, and `tests/test_4_boundary_sweep.py:33-38`; fixed WebSocket `localhost:8765` in test 2 line 29 and test 3 line 117; fixed JACK names in tests 3/4; fixed systemd journal unit in test 2 lines 43-59 and tests 3/4 lines 36-40/41-45; fixed MIDI destination `14:0` in test 3 lines 74-81 and test 4 lines 92-98.
- Trigger: production service is running, multiple worktrees/instances exist, ALSA/JACK numbering changes, or the first matching process is not the endpoint/unit being exercised.
- Failure: the tests mutate production settings/routing, may observe PID/RSS from one process while sending to another, and may attribute another service's journal entries to the run.
- Action: implement the isolation prerequisite already specified in `ENGINEERING_VALIDATION.md:5-9`: unique instance paths, ports, client name, unit, disposable state, exact PID identity/start-time verification, and a hard refusal of production identifiers.
- Confidence: high; explicitly acknowledged by the project validation document.

### P1 — parameter-flood does not fail on xruns or missing replies

- Evidence: the stated checks include journal xruns and acknowledgement throughput at `tests/test_2_param_flood.py:4-15`; the script counts/prints them at `:142-162`, but the pass expression at `:166` excludes xrun count, underruns, received-message count/rate, and send/receive disparity.
- Trigger: audio underruns during a flood, or a connected server that stops replying while keeping the socket open.
- Failure: the test returns success despite the core real-time failure it claims to watch or zero useful replies.
- Action: read authoritative before/after bridge counters from the isolated process, fail on any post-ready increment, require expected acknowledgements or explicitly defined coalesced responses, and bound latency/backlog.
- Confidence: high.

### P1 — boundary sweep is destructive, incompletely restored, and lacks `finally` cleanup

- Evidence: it pushes invalid/extreme settings at `tests/test_4_boundary_sweep.py:48-78`, toggles state at `:129-138`, and restores only seven fields at `:289-303`. Capture/process/websocket failures before that block skip restoration and can leave ffmpeg/MIDI children running. The validation document already warns about this at `docs/pi4/ENGINEERING_VALIDATION.md:9`.
- Trigger: normal completion after the broad sweep, exception, Ctrl-C, endpoint loss, or capture failure.
- Failure: dozens of parameters, toggles, fade/panic state, and autosaved values remain changed; on the shared endpoint this can poison the next live startup.
- Action: snapshot the complete isolated state before testing and restore transactionally in `finally`; track and terminate every child; never permit the production endpoint.
- Confidence: high.

### P1 — installer upgrades are “create once,” so tracked fixes do not reach existing appliances

- Evidence: limits, CPU service, screen blanking, USB cmdline, CPU isolation, watchdog, PipeWire drop-in, Wi-Fi config, journald limits, sysctls, and IRQ service are generally guarded only by file existence or substring checks (`install.sh:136-145`, `:151-171`, `:173-190`, `:192-225`, `:231-250`, `:273-291`, `:295-320`, `:327-338`, `:355-376`). Existing content is neither compared with the desired version nor repaired. Options such as `--no-isolcpus` and `--keep-onboard-audio` also do not undo prior installer changes.
- Trigger: rerun installer after a tracked tuning/service change, change hardware profile/options, or inherit a partially configured file.
- Failure: the installer prints “already configured” while stale/wrong content remains. A clone/update is not a repeatable convergence operation, and rollback of invasive global tuning is undefined.
- Action: render managed files deterministically with backups and content/version checks; distinguish managed from user-owned settings; implement explicit removal/rollback and a post-install verification report.
- Confidence: high.

### P1 — the configured Pi4 memory guard is known ineffective on the inspected target

- Evidence: installer writes `MemoryHigh=1200M`/`MemoryMax=1500M` at `install.sh:497-507`. `docs/pi4/BASELINE.md:34-35` and `docs/pi4/ENGINEERING_VALIDATION.md:59` record that the kernel memory controller is disabled, making service memory accounting/guards ineffective.
- Trigger: leak, accidental large soundfont load, or memory spike on that kernel configuration.
- Failure: the protection described by installer comments does not throttle/kill the service before whole-system memory pressure/OOM.
- Action: preflight the memory controller and fail or warn truthfully; either enable/test the controller in the supported boot profile or replace the claim with measured application-side controls. Do not infer effectiveness from the unit property alone.
- Confidence: high for the inspected Pi; other devices require verification.

### P1 — native fallback can silently violate the supported Pi4 CPU profile while the service still becomes ready

- Evidence: the drop-in enables native modules (`systemd/stave-synth.service.d/faust.conf:1-12`), but the unit has no native artifact/version/hash preflight (`systemd/stave-synth.service:25-44`). Its comment says a missing Faust `.so` is a transient startup failure at lines 5-7, while wrappers are designed to catch load failure and use Python fallback. `FAUST_PORT_PLAN.md:630-640` explicitly records that leaving the Faust path by changing unison caused a Pi4 “glitch storm.”
- Trigger: stale/missing/incompatible native build after source update, incomplete copy, or ABI mismatch.
- Failure: the service can report READY in an unqualified slower mode rather than restart/degrade truthfully; on Pi4 that can become live xruns.
- Action: define required native modules for the Pi4 stage profile, verify source/binary build identity and active backend before READY, and refuse the live profile when a required fast path is absent.
- Confidence: high for configuration behavior; resulting xruns are load-dependent.

### P2 — installer can claim boot autostart even when linger enabling failed

- Evidence: `loginctl enable-linger` errors are discarded at `install.sh:515-516`; summary checks only `systemctl --user is-enabled` at `:581-587` and then promises next-boot startup at `:593-600`.
- Trigger: policy/authentication/loginctl failure or environment without linger support.
- Failure: unit is enabled but user manager may not start at boot without login; installer reports success.
- Action: verify `loginctl show-user "$USER" -p Linger`, make failure explicit, and include a reboot qualification. The inspected live target did have linger enabled; this is a fresh-install/recovery risk.
- Confidence: high.

### P2 — RT/memlock setup is conditional and not verified end-to-end

- Evidence: user is added to `audio` only inside the branch that creates a missing limits file (`install.sh:136-145`). If the file already exists but the user is absent, membership is not repaired. The unit itself declares neither `LimitRTPRIO` nor `LimitMEMLOCK` (`systemd/stave-synth.service:14-45`), and installer only says a re-login “may be needed.”
- Trigger: pre-existing distro audio limits, fresh group membership, linger user manager started before membership, or restored install.
- Failure: render thread may lack FIFO/memlock permissions despite an apparently successful install.
- Action: independently reconcile group membership and limits, restart/reexec the user manager as appropriate, and verify effective `/proc/<pid>/limits` plus actual scheduler after reboot. The inspected live process already showed RT95 and unlimited memlock, so this is not a claim that current production lacks them.
- Confidence: medium-high; effective systemd/PAM inheritance varies by distro/session.

### P2 — dependency and asset acquisition are not reproducible or supply-chain pinned

- Evidence: minimum-only Python ranges in `requirements.txt:1-6`, duplicated separately in `setup.py:11-20`, unversioned apt installs at `install.sh:100-128`, unconditional `pip --upgrade pip` at `:404-406`, and a large executable soundfont download with no checksum/signature at `:423-450`. The baseline records exact observed versions at `docs/pi4/BASELINE.md:28-30`, but no lock/manifest enforces them.
- Trigger: reinstall after repository/package updates, resolver changes, mirror compromise, or upstream archive replacement.
- Failure: a rebuilt stage image is not dependency-identical; compatibility/performance can drift, and the downloaded asset is not integrity-authenticated.
- Action: maintain a Pi4-tested lock/constraints file and system package manifest/snapshot, record native compiler/Faust versions and hashes, verify the Salamander archive checksum and license payload, and test controlled upgrades separately.
- Confidence: high.

### P2 — `--no-gui-deps` does not prevent the GUI Python package install attempt

- Evidence: the flag skips apt GTK/WebKit packages at `install.sh:120-131`, but `pip install -r requirements-gui.txt` is unconditional at `:408-413`.
- Trigger: default small-Pi/headless install.
- Failure: pywebview is still downloaded/installed or partially installed, contrary to the headless/minimal-memory promise; failure is silently treated as success.
- Action: guard the pip GUI requirements with `INSTALL_GUI_DEPS`, and report whether native GUI support is actually usable.
- Confidence: high.

### P2 — PipeWire readiness preflight always succeeds, including definite failure

- Evidence: `systemd/stave-synth.service:25-28` loops for about three seconds and ends with `exit 0` even if all `pw-cli` probes fail.
- Trigger: user PipeWire unavailable, late, or broken.
- Failure: the service proceeds into a known-unready graph and relies on process crash/restart rather than expressing dependency readiness. This increases boot-loop time and obscures the actual fault.
- Action: either fail the preflight after its bounded retry or remove it and rely on a tested application readiness/retry state; report PipeWire readiness separately from engine/UI readiness.
- Confidence: high.

### P2 — connection-chaos does not test automatic recovery and its recovery signal is unrelated to physical routing

- Evidence: the test itself reconnects every saved pair at `tests/test_3_connection_chaos.py:97-112`; recovery uses WebSocket master `peak_level` at `:115-128` and `:169-182`. That meter can be nonzero while the external sink remains disconnected. MIDI connect/disconnect return codes are ignored at `:103-111`. This limitation is documented at `docs/pi4/ENGINEERING_VALIDATION.md:9`.
- Trigger: run the script as evidence for hotplug/output recovery.
- Failure: it proves, at best, that internal rendering continues after the test manually repairs the graph. It does not prove device disappearance/re-enumeration, preferred-sink watcher behavior, or analogue output recovery.
- Action: create a separate isolated automatic-recovery test that removes/recreates the sink/profile, performs no manual reconnect, validates exact graph routes, and captures the selected sink/analogue output.
- Confidence: high.

### P2 — boundary-sweep pass criteria do not implement the stated assertions

- Evidence: header promises no silence longer than two seconds and watches clipping (`tests/test_4_boundary_sweep.py:1-13`). Scanner merely counts independent silent 100 ms windows at `:202-225`; final check permits up to 30% aggregate silence at `:329-337`—18 seconds in a 60-second run—and never fails on `clip_samples`. ffmpeg client discovery and both `jack_connect` return codes are ignored at `:245-264`.
- Trigger: disconnected capture, long but sub-30%-of-run dropout, heavily clipped output, or failed MIDI injection.
- Failure: false pass or misleading failure attribution; it can scan unrelated silence/content without proving the chord/capture path.
- Action: require successful capture registration/routes/MIDI injection, calculate maximum contiguous silent duration inside verified active material, fail at the specified two-second bound (prefer much tighter audio gates), and define separate upstream/master clipping limits.
- Confidence: high.

### P2 — parameter-flood/chaos journal logic can miss or misattribute failures

- Evidence: journal subprocess return codes are ignored (`tests/test_2_param_flood.py:43-59`, `tests/test_3_connection_chaos.py:36-40`, `tests/test_4_boundary_sweep.py:41-45`). Test 2 removes “new” exceptions by exact string membership against a two-second baseline at `:119-144`; a repeated identical error line can be hidden. Tests 3/4 search broad case-sensitive substrings, while xrun handling is absent from chaos and final boundary criteria.
- Trigger: inaccessible journal, restarted unit, repeated message text/timestamp formatting, unrelated unit instance, or lowercase error wording.
- Failure: empty journal output is treated as clean; relevant failures can be missed or another run's failures counted.
- Action: validate journal command success, use cursor/invocation ID and exact isolated unit, combine authoritative process counters with structured markers, and fail on restart/PID identity change.
- Confidence: high.

### P2 — process survival checks do not prove the original synth survived

- Evidence: tests capture only a PID and later use `psutil.pid_exists(pid)` (`tests/test_3_connection_chaos.py:130-158`, `tests/test_4_boundary_sweep.py:228-284`). They do not retain/check process create time, executable, command line, or systemd invocation/restart count.
- Trigger: service restart or PID reuse during the run.
- Failure: a restarted/replaced process can be reported as the original surviving process; conversely later `memory_info()` may refer to a different process.
- Action: bind evidence to PID plus create time and unit invocation ID; explicitly fail any restart unless the specific recovery test expects one.
- Confidence: high.

### P2 — `polish_shimmer.py` can compare the same backend twice and still exits successfully

- Evidence: `SynthEngine` is imported at `tools/polish_shimmer.py:17` before `build_engine` changes the environment/config at `:20-27`. `synth_engine.py` imports its own module-global Faust flags, so mutating only `config.USE_FAUST_OSC_BANK` does not switch that already-imported global. The tool prints the actual path at `:41-42` but does not assert it differs; `main` has no numeric acceptance or nonzero exit at `:56-78`.
- Trigger: normal invocation without a preconfigured environment, or any environment that fixes both constructions to one path.
- Failure: “Python” and “Faust” outputs can come from the same backend, yielding a convincing 1.0 ratio with exit 0.
- Action: mutate the owning `synth_engine.USE_FAUST_OSC_BANK` global before each construction or use isolated subprocesses, assert backend identity, and return failure outside a declared tolerance.
- Confidence: high.

### P2 — `render_compare.py` is not an apples-to-apples oscillator comparison

- Evidence: Python note-on receives `int(VEL * 127)` at `tools/render_compare.py:120-123`, while `SynthEngine.note_on` consumes a normalized float directly and multiplies it into audio. It assigns `e.adsr_config` at `:108`, but the engine uses per-oscillator ADSR configs. The Faust side receives block-final scalar envelope gates at `:75-84`, while the real path has its own smoothing/architecture. The Python run includes the whole engine and final 0.85 trim/reverb machinery at `:94-137`, while Faust run mixes raw bank channels at `:47-91`. Each WAV is independently peak-normalized at `:140-146`, erasing level differences.
- Trigger: any normal run/listening comparison.
- Failure: output differences cannot be attributed to Faust versus Python oscillator implementation, and severe amplitude mismatches are hidden.
- Action: feed identical normalized velocity, phase, envelope samples, gain law, routing, and final scaling to both sides; write matched-level float artifacts and calculate aligned deltas/metrics with a failing threshold.
- Confidence: high.

### P2 — microbenchmarks are not representative Pi4 capacity evidence

- Evidence: both benches hardcode 128-sample blocks (`tools/bench_reverb.py:15-17`, `tools/bench_ping_pong.py:15-18`) while the captured Pi4 profile is 512 frames (`docs/pi4/BASELINE.md:31-32`). They report only one aggregate elapsed mean (`bench_reverb.py:25-37`, `bench_ping_pong.py:33-46,56-69`), with no p95/p99/max, scheduler gaps, affinity/governor/thermal state, repetitions, confidence range, backend/build hash, or end-to-end load. `bench_ping_pong` constructs `SynthEngine` without forcing its Python ping-pong flag off, so an inherited `STAVE_FAUST_PING_PONG=1` can make the “Python” benchmark call Faust through `_process_ping_pong`.
- Trigger: use printed “% of real-time budget” as live capacity/headroom proof.
- Failure: numbers are environment/path dependent and omit the tail behavior required by `PRODUCT_VISION.md:91-97`; they can benchmark the wrong implementation.
- Action: verify paths, benchmark the supported 512 profile plus candidates, record distributions and device state over repeated runs, and reserve these scripts for component micro-cost—not release headroom.
- Confidence: high.

### P2 — Python/package distribution metadata does not contain the runnable appliance

- Evidence: `setup.py:8-10` discovers only packages and declares `package_data={"": ["ui/*"]}`, but `ui/` is top-level, not under `stave_synth`. No MANIFEST/config includes UI, native bridge, Faust DSP/sources, service units, or launcher. The console entry point at `setup.py:26-29` therefore does not imply those runtime assets exist. Description still says Pi5 at `setup.py:6`.
- Trigger: build/install wheel or sdist instead of running from Git checkout.
- Failure: installed command lacks required UI/native assets and is not equivalent to documented appliance install.
- Action: either mark Python packaging unsupported/internal and remove the misleading entry point, or explicitly package resources and provide a tested native-build/install strategy per architecture.
- Confidence: high from tracked layout; no package build was run because the audit was non-mutating.

### P3 — atomic-save test is useful but narrower and less robust than its presentation suggests

- Evidence: `tests/test_1_atomic_save.py:35-58` waits on blocking `readline()` with no timeout; a broken child can hang the harness. Each child first commits successfully and then repeatedly writes the same payload (`tests/_save_worker.py:16-31`), so it does not cover first-write absence, competing values/writers, directory durability, ENOSPC/I/O errors, or actual power loss. Orphan temp files are explicitly non-failing at test lines 79-88, and the temp directory is not cleaned.
- Trigger: treating 500 SIGKILL iterations as full persistence qualification.
- Failure: important fixed-temp-name/concurrent-writer and SD-card durability risks remain untested.
- Action: retain this as a single-writer rename test, add bounded child startup/cleanup, unique concurrent writers with distinct payloads, malformed/recovery tests, injected write/fsync/rename failures, and spare-media power-cut qualification.
- Confidence: high; limitations are also acknowledged in `ENGINEERING_VALIDATION.md:11`.

### P3 — installer accepts unknown options silently

- Evidence: argument case at `install.sh:32-63` has no default/error branch.
- Trigger: typo such as `--no-autstart`.
- Failure: installer performs the default invasive/autostart behavior instead of rejecting the typo.
- Action: reject every unknown argument before mutations.
- Confidence: high.

### P3 — installer/reporting can claim FluidR3 installation without verifying the asset

- Evidence: apt fallback chains package install, symlink creation, and success echo at `install.sh:460-469`; `ln -sf` succeeds even if the assumed source path does not exist. No final required-font preflight gates service enablement; summary only counts matching paths at `:551-557`.
- Trigger: distro package installs the asset at a different path, package metadata exists but payload is unavailable, or a broken symlink remains.
- Failure: apparent successful install can start with silent piano.
- Action: resolve and validate a readable nonempty soundfont after install, verify FluidSynth can load/select its required program in a non-live preflight, and fail or declare degraded state explicitly.
- Confidence: medium-high; standard Debian path normally matches.

### P3 — documentation contains stale/conflicting operational claims

- `README.md:3-8,40-48,205-212` interleaves Pi4-stage and original Pi5 hardware claims, making supported hardware/profile ambiguous.
- `README.md:54-62` says all ten Faust modules are built; current `faust/build.sh` builds twelve DSP modules, two additional lite variants and the merged C shim (15 shared-library outputs), while activation is incomplete.
- `README.md:131-134` says “The audio service will reconnect on the next note” inside MIDI troubleshooting; MIDI recovery is polled and unrelated to the next note, while audio recovery follows a different preferred-sink watcher.
- `CLAUDE.md:3-12,40-44` remains Pi5-centric and refers to a private `~/.claude/.../memory` history that a clean clone/recovery operator cannot access.
- `FAUST_PORT_PLAN.md:39,59-62` still labels Phase 1 “NEXT” and “awaiting soak,” while its later journal records deployment; `:480-492` says Phase 4 is not committed/deployed, while `:621-629` says it was deployed. This is valuable historical material but not a trustworthy current runbook without a status header/archive split.
- Action: make the Pi4 docs the landing page for this branch, separate historical journal from current supported configuration, and ensure install/activation/module counts and recovery language match executable files.
- Confidence: high.

## Tool-specific trust assessment

- `tools/compare_pad_bus.py`: strongest harness in the directory. It asserts required backend engagement (`:233-246`), uses common seeded scenario/input, covers transition segments, reports per-segment and full-run deltas, and returns nonzero on tolerance failure (`:298-349`). Remaining gaps: fixed 256 rather than supported Pi4 512, no native/source hashes or performance distribution, scenario ordering deliberately avoids known Haas/fast-to-slow divergences (`:151-159`), and direct attribute mutation does not cover production control concurrency.
- `tools/compare_piano_chain.py`: sound parity methodology is strong: one FluidSynth source stream is replayed identically (`:64-127`), backend identity is asserted (`:184-203`), and tolerance controls exit (`:264-301`). Reproducibility gaps: hardcoded `/usr/share/sounds/sf2/FluidR3_GM.sf2` with no content hash (`:61,89-99`), fixed 256 frames, and no dependency/native-build identity.
- `tools/bench_reverb.py`: acceptable as a local component mean-time smoke benchmark only; not capacity or tail-latency evidence.
- `tools/bench_ping_pong.py`: not trustworthy until backend identity is forced/asserted; also only a mean component benchmark.
- `tools/polish_shimmer.py`: informational output currently cannot prove cross-backend parity because backend selection is broken and there is no failure threshold.
- `tools/render_compare.py`: listening artifact generator only, not parity evidence; current two paths are materially mismatched and independently normalized.

## Release tests still required

1. Isolated test-instance launcher with disposable XDG/config/data roots, unique HTTP/WS ports, unique JACK client/MIDI destination, unique systemd unit, exact PID/invocation identity, and hard refusal of stage identifiers.
2. Installer convergence tests on clean Bookworm/Trixie Pi4 images and upgrade tests from the preserved baseline: repeat runs, option changes/rollback, missing network, failed apt/pip/download, paths with spaces, absent devices, late PipeWire, failed linger, reboot, and exact active native flags/hashes.
3. Boot matrix from `ENGINEERING_VALIDATION.md`: 20 controlled cold boots each with device present/late and network unavailable; timestamp PipeWire, engine, MIDI route, physical output route, HTTP bind, WebSocket bind, truthful READY, and first playable output separately.
4. Effective-profile verification after boot: CPU model/RAM, governor, temperature/throttle, actual scheduler/affinity, effective RT95 and unlimited memlock, PipeWire 48 kHz/512, memory-controller availability, active Faust modules, soundfont/program, selected sink, and no stray FluidSynth/JACK owners.
5. Automatic MIDI/audio hotplug recovery without test-side reconnect; exact preferred route and real captured output, including USB re-enumeration/name changes and failed `jack_connect` return codes.
6. Parameter/control soak with authoritative pre/post bridge xrun/underrun/MIDI-drop counters, send/ack/backlog latency, two clients including a non-reader, process restart identity, p95/p99/p99.9/max render and scheduler gaps, and complete state rollback.
7. Persistence suite: concurrent distinct writers, malformed/non-finite input rejection, fixed-temp collision, fsync/rename/directory durability and recovery, ENOSPC/read-only/I/O injection, then real spare-SD power interruption.
8. Audio boundary/click validity on the actual active native profile with verified input/capture routing, contiguous-gap and local-statistics click detection, upstream stage captures, matched levels, 512-frame baseline plus controlled quantum candidates, and listener acceptance.
9. Eight-hour representative Pi4 soak and full service-length rehearsal on the inventoried Yamaha/Dexibell controllers, actual interface/hub/power/network/Apple clients, followed by tested whole-device rollback on spare media.

## File-by-file coverage

| File | Review result |
| --- | --- |
| `AGENTS.md` | Read fully; isolation, Pi4 branch, baseline, native rebuild, and non-equivalence of ring capacity/latency were used as audit constraints. |
| `install.sh` | Read fully; findings on convergence, flags, GUI conditional, RT/group verification, linger, memory cgroup, readiness, dependency/assets, option validation, and soundfont verification. Syntax passed. |
| `stave-synth.sh` | Read fully; generic destructive `pkill`, incomplete Faust flag parity, and launch behavior reviewed. Syntax passed. |
| `setup.py` | Read fully; package/resource incompleteness, duplicated dependency policy, stale Pi5 description. |
| `requirements.txt` | Read fully; unpinned minimum ranges and duplicate metadata. |
| `requirements-gui.txt` | Read fully; incorrectly attempted even in headless mode. |
| `systemd/stave-synth.service` | Read fully; readiness preflight, restart/watchdog, paths, RT/memlock, and native-profile truthfulness reviewed. |
| `systemd/stave-synth.service.d/faust.conf` | Read fully; missing pad-bus, piano-chain, and merged flags. |
| `tests/_midi_util.py` | Read fully; valid small Type-0 generator, but callers hardcode destination and do not validate playback. AST passed. |
| `tests/_save_worker.py` | Read fully; valid isolated target monkey patch; identical repeated payload limits coverage. AST passed. |
| `tests/test_1_atomic_save.py` | Read fully; useful SIGKILL/JSON test, missing timeout/concurrency/durability/cleanup. AST passed. |
| `tests/test_2_param_flood.py` | Read fully; unsafe targeting and false-pass criteria for xruns/replies/journal. AST passed. |
| `tests/test_3_connection_chaos.py` | Read fully; zero-disruption pass, weak threshold, manual rather than automatic recovery, unsafe targeting. AST passed. |
| `tests/test_4_boundary_sweep.py` | Read fully; destructive incomplete restore, unchecked setup, silence/clipping criteria mismatch, unsafe targeting/cleanup. AST passed. |
| `tools/bench_ping_pong.py` | Read fully; backend contamination risk, fixed obsolete block size, mean-only microbenchmark. AST passed. |
| `tools/bench_reverb.py` | Read fully; component mean only, fixed obsolete block size, insufficient metadata/distribution. AST passed. |
| `tools/compare_pad_bus.py` | Read fully; strong deterministic backend assertions/delta exit, with stated transition and production-profile gaps. AST passed. |
| `tools/compare_piano_chain.py` | Read fully; strong common-source parity design, fixed asset/path/profile reproducibility gaps. AST passed. |
| `tools/polish_shimmer.py` | Read fully; backend toggle bug and no acceptance exit. AST passed. |
| `tools/render_compare.py` | Read fully; normalized-velocity/ADSR/path/level mismatches make A/B attribution invalid. AST passed. |
| `.gitignore` | Read fully; generated/native/soundfont exclusions are sensible; hand-written merged shim exception is present. Native rebuild remains mandatory. |
| `LICENSE` | Read fully; standard MIT license for project code. |
| `soundfonts/SALAMANDER-LICENSE.txt` | Read fully; CC-BY 3.0 attribution/source text is present. Installer-downloaded copy still needs integrity verification and distribution attribution checks. |
| `README.md` | Read fully; branch/hardware, install/module, recovery, packaging, and current-profile claims cross-checked; several stale/conflicting claims noted. |
| `CLAUDE.md` | Read fully; useful legacy architecture principles but Pi5/private-memory assumptions are not a portable Pi4 runbook. |
| `FAUST_PORT_PLAN.md` | Read fully; valuable detailed parity history, but status sections conflict with later deployment journal and tracked activation. |
| `docs/pi4/BASELINE.md` | Read fully; appropriately distinguishes source/runtime backup from qualified recovery and records RT95, unlimited memlock, linger context, and disabled memory controller. |
| `docs/pi4/ENGINEERING_VALIDATION.md` | Read fully; strong proposed gates and candid limitations; no gates are represented as completed. |
| `docs/pi4/PRODUCT_VISION.md` | Read fully; coherent Pi4 musical/performance contract and appropriately labels targets as provisional. |

## Strengths

- The new Pi4 product, baseline, and engineering-validation documents are unusually honest about observed facts versus proposed targets, especially isolation, real latency measurement, memory-controller limits, and the difference between test-side reconnect and automatic recovery.
- The service uses `Type=notify`, a render-progress-gated watchdog, bounded restart policy, and a long cold-start allowance—good appliance primitives once component readiness becomes truthful.
- Installer force-rebuilds native code on the target architecture, preserves the separate soundfont license, caps journals, disables USB/Wi-Fi power saving, and records explicit small-memory defaults.
- `compare_pad_bus.py` and `compare_piano_chain.py` have substantially better input identity, backend assertions, scenario documentation, and exit behavior than the older diagnostic tools.
- The atomic-save harness uses a disposable target rather than production state, and all covered shell/Python files passed static syntax parsing.
