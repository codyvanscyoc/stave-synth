# Pi4 isolated native build and first throughput probe — September 17, 2026

User confirmed the Pi is available on stage with the sound system off. This
authorized isolated compilation/load tests, not replacement of the stage
service. After the thermal stop below, the user explicitly approved temporarily
pausing/restoring Stave. The working service was paused for the retries and
restored each time. No candidate physical audio/MIDI driver was opened and no
working source/configuration/output route was changed.

## Latest outcome: target build and bounded offline probe completed

Target: Raspberry Pi4 Model B Rev1.5, aarch64, Debian GCC14.2.0,
Faust2.79.3, FluidSynth2.4.4. Post-test governor `performance`, observed ARM
clock1.800457088GHz (not a per-block clock trace). No dependencies installed.

The first retry's three-minute cooling gate never reached68°C (last68.1°C),
so its EXIT guard restored Stave without starting the build. The next retry
reused only source/artifact-hash and toolchain-matched DSP objects from the
first attempt, reducing redundant compiler heating. GCC then rejected the
existing same-line output-clamp `if` statements under `-Werror=misleading-indentation`.
The fix puts each condition on its own line, with **no audio-math change**.
Mac output comparisons remain exact over2,000 blocks, with UBSan/zero-new guards.

The final paused-service retry completed the C++ build,1,400-block UBSan/scoped
allocation/key-sync/random-fault instrument guard, and all eight throughput
cases. Temperature began71.575°C and ended74.01°C; firmware flags remained0x0.
Total recorded build/check/probe command wall time90.996seconds. The unchanged
80°C monitor remained active. Objects reused from the initial attempt retain
their provenance report hash and were copied into new evidence, not modified.

Each cell below is mean / maximum **render wall time in milliseconds** from
400 measured blocks after100 warm-up blocks per case:

| Offline workload | 512 frames (10.667ms budget) | 256 frames (5.333ms budget) |
| --- | --- | --- |
| Piano, six held notes | 1.146 / 1.271 | 0.595 / 0.677 |
| Piano + two oscillators, six notes | 3.442 / 3.734 | 1.711 / 1.852 |
| Twelve-note pool + shimmer/delay/reverb/motion/compression | 4.008 / 4.190 | 1.991 / 2.177 |
| Dense pool plus note/control/reverb-type transitions | 4.064 / 5.436 | 2.027 / 3.314 |

All3,200 measured blocks fit their nominal block duration; measured audio
duration25.6seconds, generated unpaced. The dense-transition p99 was4.878ms at512
and2.749ms at256. Maximum per-process RSS205,504KiB (about201MiB), including
the explicitly loaded FluidR3 asset. These figures do not include the future
complete control/driver/bed/organ deployment or demonstrate total Pi RAM reserve.

This is encouraging **throughput**, not live low-latency qualification. The
normal Stave service was stopped during measurement, the graph is incomplete,
and there was no real-time audio callback, live MIDI, UI workload, interface
output or scheduling/analogue-latency measurement. No512-vs256 live switch was
performed. Missing functions can consume this measured reserve.

Final PCM was finite/bounded and terminal STOP silent. Dense transitions recorded
4 raw piano full-scale samples at512 and8 at256; other cases recorded0. This is
an upstream headroom warning to investigate, not proof of audible distortion
or permission to call sound quality fully passed. The independent high-gain
sound-comparison gate also remains open separately.

Working build restored and verified: PID683040, NRestarts0, active/running,
clean checkout `f600c7ea5a9170b89fd3ff6298531d47ff6d8542` in the same rehearsal
directory. HTTP runtime config returned the stage instance/port8765. Read-only
WebSocket state showed UI healthy, native profile ready/no issues, control and
audio error null,48k/512, graph error0, MIDI dropped0, ring slots6. ALSA listed
the Yamaha MX Series. The service restart is not a player-audition pass.
No compiler/benchmark remained, and the SSH shell was closed after checks.

Latest private report:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-pi4-offline-benchmark-20260917/evidence-format-fix/report.json`

SHA256 `98f205485935484491b2922851c8d12849bd697e85e0e5a12b429aa7e8245f1e`.
Remote report is under
`/home/codyvanscyoc/stave-native-paused-20260917.sFOvtl/evidence-format-fix/`.
Local archive `native-v2-pi4-offline-benchmark-20260917.tar.gz` contains the failed
GCC retry, final target artifacts/report, original transferred snapshot and
the final modified harness/output header. Individual report hashes distinguish
these from the earlier archived source; no old report was overwritten.

## First attempt: thermal guard stopped build, no timing from that attempt

- Pi is aarch64; initial available memory about 1,190 MiB, swap unused, disk
  about 49 GiB available. Installed Faust2.79.3 and FluidSynth2.4.4 were used;
  no dependencies were installed. These differ from Mac comparison versions.
- Working stage service remained PID185009, active/running, NRestarts0,
  `/home/codyvanscyoc/stave-synth-pi4-rehearsal` before and after the attempt.
- Initial temperature74.01°C; firmware throttle flags0x0. Ten original Faust
  modules generated and compiled serially in a new private directory.
- At the C++ UBSan instrument-guard compilation, the monitor observed80.341°C
  and terminated its own compiler process group. That command ran46.623seconds.
  It never ran the instrument guard or the benchmark on Pi. Report `runs=[]`.
- Immediately afterward temperature76.9°C; firmware throttle flags still0x0.
  No compiler or benchmark process remained. This is our conservative thermal
  cutoff, not evidence that firmware throttling or stage-audio dropout occurred.
- Existing Python synth was concurrently running (ps lifetime average59.7%
  of one CPU at initial inspection); a separate FluidSynth process also existed.
  Neither was modified. No speed/headroom/latency conclusion follows from this
  aborted build. Build load is not the instrument's render workload.

Remote isolated directory:
`/home/codyvanscyoc/stave-native-probe-20260917.gpQ10J`

Private local evidence:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-pi4-build-thermal-stop-20260917/evidence/report.json`

Report SHA256
`b226c3067981e63a427bcab06e84e2b7d0da25e630bd49a0cdac286dcd0f7ead`.
Archive of source, generated objects, command logs and report also retained at
`native-v2-pi4-build-thermal-stop-20260917.tar.gz` in the same private backup parent.
Transferred source archive SHA256
`1eb4754c8df106572a2a37938d126c7a66d6c7be0beee770163d9bcf6fc4f2ae`.
The first source label is `95db5ad-plus-benchmark`; individual hashes identify the
then-uncommitted harness. Subsequent edits add interruption cleanup/reuse. The
successful final report label is `95db5ad-plus-benchmark-format-fix`; final source
hashes identify its precise measured build, before this checkpoint was committed.

## Harness now available

`tools/benchmark_native_instrument.py` builds only within a NEW explicitly
specified evidence directory. Existing compiler/Faust/pkg-config/FluidSynth and
an explicit existing SF2 are required; absence fails without installation.
Original DSP files match preserved v1.2, with the already-compared12-slot
oscillator-bank specialization. Flags remain O2, contraction off, no fast-math.
UBSan guard success is required before the uninstrumented performance binary.

Four cases at each cadence512/256: six-note piano alone; six-note piano + two
oscillators; twelve-note capacity stress with shimmer, delay/reverb, filters and
compression; and the same dense setup with per-block parameter changes and
periodic note/reverb-type transitions. This is an incomplete graph: no sampled
bed, organ or live event/backend/browser path. The dense whole-tone stack is a
capacity stimulus, not a claim of representative worship voicing.

Each case has100 warm-up blocks and a bounded100..2000 measured blocks (default
400). Wall and thread-CPU distributions, blocks exceeding nominal block duration,
peak/RMS, oscillator count, raw piano full-scale samples and process maxRSS are
reported. Renders are unpaced at normal scheduling priority, with no driver.
Over-budget counts are **not xruns**; below-budget counts do not establish live
latency or supported256. Final PCM finite/bounded checks and terminal silence are
required; this is not a perceptual sound-parity test or full capacity certificate.

The monitor checks temperature during owned compiler/probe execution and stops
that process group at80°C or timeout. It never kills the working synth. Local
interruption cleanup also handles INT/TERM/HUP. The report preserves failures.

Local C++ smoke validation on Mac used existing compiled reference objects:
all eight cases ran100 measured blocks with finite/bounded non-silent output and
terminal silence. One dense512 transition run counted2 raw piano full-scale
samples; this is retained as an upstream gain/clipping investigation, not hidden
by the downstream limiter or called a sound-quality pass. The all-in-one runner
itself refused Mac execution because pkg-config is absent; no tool was installed.
Six new mocked harness-safety tests join the reviewed legacy allowlist;
555 total tests plus Node pass in13.276seconds. These include bounded arguments,
existing-evidence refusal, missing dependencies, thermal-stop process ownership
and interruption cleanup/handler restoration, plus refusing DSP reuse across
toolchain changes.

## Next action

Continue remaining function/control/backend integration and investigate dense-
transition piano headroom; preserve the working stage build. Expand qualification
only after those features are present. Cooling remains a production concern:
the initial background-service-plus-compiler load crossed our80°C limit, even
though the later isolated render probe did not. Do not infer universal safe
headroom from a short unpaced test or raise the guard to force a result.

Native-v2 remains not field-test ready; prior sound-comparison limitations and
remaining feature/live-control milestones remain in NATIVE_V2_MOTION_CONNECTED.md.
