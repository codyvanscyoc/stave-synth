# Pi4 native audition: target build and muted live test passed

September17,2026. User explicitly approved pause/restart while away, with the
PA/monitors off, to finish preparation for a first listening test. Independent
drone, recorded pads and recording remain deferred. This is a **limited-audition
checkpoint**, not complete feature migration or stage-release approval.

## Target build

Native source: `5a22fbed937e13009b4b786394d7c10369ce43ae`.
Separate directory:
`/home/codyvanscyoc/stave-v2-audition-20260917.zRXTny`.
No dependencies installed; GCC14.2.0, Faust2.79.3, FluidSynth2.4.4,
JACK development metadata1.9.22. Existing generated DSP objects reused only
after source/artifact/toolchain checks. No DSP math, gain or precision changed.

The working service was paused under shell restoration traps and an independent
fallback timer. The unchanged80°C guard remained active. Full current C++ build,
1,400-block instrument guards, audition adapter guards, fake-JACK lifecycle
guards and eight offline timing probes passed. Total recorded build/check/probe
command time226.85seconds. Report temperature71.088→74.01°C, firmware flags0x0.

All3,200 measured offline blocks stayed within nominal cadence. Dense transition
wall mean/max:512=4.047/5.450ms;256=2.025/3.374ms. These remain unpaced
throughput tests. Dense raw-piano full-scale samples4/8 remain an unresolved
upstream headroom warning; successful later musical testing does not erase it.

The target audition executable is `evidence/audition-jack`, SHA256:
`900a753f10108f0dd708f9dd27881c099d4c7d8c0b15aadc6fcb319b4e75ed29`.

## First real callback test —512 only, physical output muted in software

A separate transient user unit launched the native host and temporary browser
control server at private IP192.168.1.203:8082. Exact JACK client was
`stave-v2-audition-listen`; primary MIDI source was
`Midi-Bridge:Yamaha MX Series MIDI 1 (capture)`, with the verified Yamaha MX
Analog Stereo playback_FL/playback_FR sinks. The normal Stave client was stopped.
Only one physical MIDI alias was selected; no broad automatic route selection.

`AuditionSession` rendered on the actual JACK/PipeWire callback, with no Python
render-ahead queue. Python only served/control-tested the browser API. The
master startup gate stayed closed at zero throughout; **no listening, physical
latency or audible-click acceptance is claimed**. All upstream piano/oscillator/
effect processing still ran. No WAV/MP3 or sampled-pad asset was recorded.

The isolated native MIDI peer sent45 musical chord changes over its93-second
fixture:360 note-ons, matching key-ups under sustain, pedal changes and terminal
release,857 packets total, zero write errors. Six-note piano first; both
oscillator faders enabled at20s; hall/shimmer/delay/piano send at40s; continuous
filter movement from42s; twelve-note chords from61s. The HTTP control path
submitted176 absolute changes. Maximum sampled pending-control count3.

Observed wall duration94.525seconds, including final telemetry wait. Status is
sampled at1Hz, so block-counter deltas do not define exactly that same wall
window. Deltas and final maximum:

| Metric | Observed |
| --- | --- |
| Completed native audio blocks | +8,911 |
| Played note-ons | +360, exactly the fixture count |
| JACK xruns | +0 |
| Callbacks over10.667ms block budget | +0 |
| Maximum callback wall duration | 5.25873ms |
| Raw piano full-scale samples | +0 in this fixture |
| Unsupported channel MIDI | +0 |
| Engine fault / stale control owner | none |
| Temperature samples | 70.114–73.036°C |

This is encouraging real-callback evidence for the tested slice at512. It does
not establish full-app CPU/RAM reserve, live256, analogue latency, long-duration
stability, device hotplug/recovery, full feature parity or subjective sound.
The original high-gain comparison gate remains open. Existing click/transition
limitations have not been silently marked fixed.

## Restoration verified

Transient audition stopped and the **unchanged working service restored**:
PID709387, active/running, NRestarts0, same rehearsal directory and source
`f600c7ea5a9170b89fd3ff6298531d47ff6d8542`. Read-only HTTP and WebSocket checks
confirmed UI/native/audio/control healthy,48k/512 and no graph error. All
audition/MIDI-probe JACK ports disappeared. Firmware flags0x0; temperature73.036°C.

Saved current_state.json is byte-identical to the stopped pre-build backup:
SHA256 `2f366d3c5dfad79e5a4e343e2c6800f7d598a1cd8c213b28ab16ef62dad93872`.
No production source, unit/drop-in, preset, output gain, persistent quantum or
device policy was changed. Build and live fallback timers were stopped after
successful restoration. No audition/compiler/MIDI fixture is left running;
8082 is intentionally closed while the player is away. Normal8080 remains Stave.

## Evidence and new tools

Private Mac directory:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-target-audition-20260917`.
The archive was SHA-verified against the Pi before extraction. It retains target
artifacts, command logs, original source snapshot, live reports, exact private
supervisors and pre-maintenance settings/service backup. Do not publish it.

- Archive SHA256: `51dbf0ef7b70e74a67f42a2e86bbb5a36c46eafdaf9fa640b720cf6b7cb390f2`.
- Build report: `a531fc88f22900becd7a3899d89ed9a627b2a05e9bd067df8c5682bb6b2904d9`.
- Live report: `510a09618b0641499f509f927e70e3d3f06a747f11eb327eeacf20823906b8e0`.

`audition_midi_probe.cpp` has a fixed isolated target and explicit live opt-in;
it never sends to the production instrument or keyboard output. Its fake-JACK
test passes on Mac and Pi under UBSan, covering exact note/packet totals,
27-write callback bound, terminal release, bad cadence and write errors.
The first Pi test compile lacked an explicit `<algorithm>` include (Mac's
transitive includes hid it). Corrected only that test header; the failed report
is retained, and the working service had not yet been paused for that attempt.

`probe_native_v2_audition.py` requires an explicit private-IP8082..8090 endpoint,
matching healthy native-v2 identity,512 and master zero, plus explicit live
authorization. It preserves failed evidence and never opens the master gate.
Run only under an authorized supervisor that stops the audition and restores
the working service on any failure/interruption; it does not own those services.
Three new mocked refusal tests cover wrong/public/production endpoints, absent
opt-in, existing evidence and unsafe owner state. Reviewed Mac suite:
**565 tests, zero skips, plus Node checks**,13.784seconds.

## Next: player listening, not more breadth

The target binary is built and ready to activate for a **first limited listening
test**. When the player is at the keyboard with monitoring low/muted, start an
isolated512 audition using the already-verified executable and exact current
ports, with bounded transient-unit lifetime and working-service restoration.
Verify callback progress, routes and master zero; then supply the temporary
private-IP8082 URL and ask the player to raise master gradually.

Listen first to piano attack, quiet/loud notes, sustain/release and perceived
response; then blend OSC1/OSC2 and move filter/effects. The separate audition UI
uses native defaults, **not the user's saved patch or full production UI**.
Do not invite testing on the normal8080 UI and imply it is native-v2. Do not
enable256 until a separate live test is authorized and measured. Bring back
deferred drone/pad/recording work after listening feedback, as requested.
