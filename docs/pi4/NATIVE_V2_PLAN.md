# Stave Pi4 native successor: preserve the instrument, replace the timing core

Decision: September 16, 2026, explicitly authorized by the player after a
successful worship session with light playing and few control changes.
Status: implementation in a separate branch; NOT a replacement stage build.

## What we decided

Preserve the current working instrument as a v1.2 **stage snapshot**, then
evolve a native engine on `pi4-native-engine-v2`. Do not discard the verified
repairs, loved sounds, presets or browser experience to obtain a cleaner tree.
The existing first-party source remains in this worktree as a reference and
regression oracle; the new engine is opt-in under `native_v2/`. It must not
import/start the old runtime or install anything onto the Pi by default.

The user authorized implementation, not a claim that a total rewrite is needed
or guaranteed to make256 work. Pi4 performance must be measured on Pi4, not
inferred from Mac benchmarks, language choice, native compilation or queue size.
The Pi5/main and mac-port lines remain untouched.

## Preserved v1.2 reference

- Annotated tag: `pi4-v1.2-stage-snapshot-20260916`, source snapshot57bb94c.
- Working line: `pi4-stage-pro`; actual Pi app checkout remains f600c7e with
  application correction bd6a517. Later config/documentation commits are not
  claims of changed native DSP on the Pi.
- Graph48000/512, low-latency queue6/refill3; Yamaha-only output period policy.
- Player reports successful worship use, but light load: not full qualification.
- Known limits retained: oscillator-toggle clicks; failed256 trial; occasional
  late renders; heavy organ reserve; incomplete physical latency, boot/hotplug,
  alternate-interface and recovery qualification. Eight-hour soak remains waived.
- Git bundle plus post-worship config archive retained privately on Mac/Pi.
  This is not a bootable SD image or completed whole-device recovery test.

## Musical requirements: keep the product identity

1. Expressive sampled piano with predictable velocity, sustain and release.
2. Two note-following oscillator layers with independent balance and shaping.
3. Independent recorded bed, tonic/fifth ambience and held/frozen textures,
   distinguished from the played oscillators, not silently substituted for them.
4. Smooth filter, fader, mute and effect movements; preserved tails and safe STOP.
5. Familiar five-fader performance UI, clear OSC2/alternate/bed state, detailed
   editing separate from stage operation. Presets remain secondary.
6. Existing organ, recorder, splits, MIDI maps, macros and scene functions remain
   available in v1.2 until successor equivalents pass explicit feature gates.

## Architecture decisions

### One native audio owner

C++17 owns event timing, note/voice allocation, envelopes, parameter smoothing,
Faust/FluidSynth calls and mixing. Retain sample-position event information;
never confuse dispatch timing with physical keyboard-to-DAC latency. DSP runs
with bounded work and preallocated buffers. No filesystem/network/console I/O,
unbounded queues, loading, allocation or blocking control locks on this path.
Library-internal behavior must be measured; a C++ interface is not proof that
every dependency is allocation-free or meets its deadline.

First prove a single well-defined audio owner. Parallel DSP workers are a later
measured option, not an assumption that four cores imply fourfold speed. Queue
overflow and invalid processing contracts are explicit faults with safe release;
never silently drop a note-off or synth layer to appear healthy.

### Control and asset preparation outside audio

Retain Python for HTTP/WebSocket, schemas, persistence, presets, device policy,
asset preparation and maintenance. It submits bounded commands; it does not
have to wake up to deliver the next audio block. A browser disconnect never
stops playing. Coalesce continuous controls separately from ordered note events.
Prepared assets and scene graphs hand off at an acknowledged audio boundary;
retired objects are freed away from audio processing. This is future integration,
not something the offline prototype claims to implement already.

### Reuse sound; make routing explicit

Retain Faust algorithms and sample assets where appropriate. Begin with the
existing12-slot oscillator DSP and native FluidSynth; then port voice/envelope,
piano-chain, layer routing and effects behavior with reference comparisons.
Keep dry attack paths distinct from ambience sends. Shared effects are used
where musically appropriate, not substituted globally as a supposedly neutral
CPU optimization. Distinct oscillator buses must remain available for independent
processing. The existing oscillator bank already exposes separate OSC1/OSC2
stereo outputs plus a shimmer output; M1 mixes the dry stems without the v1.2
effects graph. Retain those stems when porting independent processing in M2/M3.

Native floating-point piano rendering is a deliberate candidate difference from
v1.2's int16 acquisition. Check gain/clipping and matched-level sound explicitly.
Do not claim exact audio parity after changing precision, envelopes or routing.

### Capacity and device policy

Define a supported Pi4 workload from measurements: sustained voices, unison,
simultaneous layers, effect tails, bed decode/residency and transition overlap.
No hidden adaptive removal of audible voices/effects beyond a disclosed tested
voice-allocation policy. Performance modes come AFTER reliable workload profiles.
Qualify at least a reference interface and later alternate compatible devices;
no promise of universal USB compatibility or identical latency on every device.
Keep audio backend replaceable. Do not assume direct ALSA beats PipeWire without
an otherwise comparable timing/recovery test.

## Milestones and promotion gates

| Milestone | Deliverable | Required evidence |
| --- | --- | --- |
| M0 Preservation | Named working snapshot, complete source bundle, runtime identity/config archive and rollback map | Checksum verified; service ownership unchanged; limits recorded |
| M1 Offline native slice | Bounded timestamped event engine plus existing Faust oscillator and optional native piano, offline WAV only | Queue/fault/boundary tests, block scheduling equivalence, finite bounded output, explicit dependency failures; no devices opened |
| M2 Sound compatibility | Native voice/ADSR/pedals, piano chain, layer routing and gain match | Deterministic stimuli, per-source and mixed captures, amplitude/spectral/tail comparisons and player listening; every difference intentional |
| M3 Worship graph | Both played layers, independent bed, filters/ambience/freeze, tail-safe mute and scene changes | Correctness at transitions, memory bounds and representative max workload; no synthetic substitution for missing features |
| M4 Pi4 timing | Isolated target build and measured native render/backend prototype | Bounded sustained/transition cases, underruns, wall/thread CPU, queue/dispatch timing; separately measure analogue response |
| M5 Browser/control parity | Existing UI wired to acknowledged native state/commands | Reconnect, slow peers, MIDI mapping, presets, recorder/pads, STOP and no stale command replay |
| M6 Stage candidate | Reversible service candidate and practical hardware qualification | Target artifact manifest, supported profile, boot/MIDI/audio/UI recovery, player rehearsal and rollback; v1.2 remains available |

M1 is deliberately NOT a production synth: simplified oscillator voice/envelope
policy, no existing complete effects chain, no browser integration, no live
backend and no preset compatibility claim. A successful offline WAV is not a
latency benchmark or proof of callback safety. Its purpose is to prove ownership,
event boundaries and direct sound-library integration before a large migration.

## Migration/reuse ledger

| Existing component | Decision |
| --- | --- |
| Faust DSP source and sample assets | Reuse with per-module provenance, build flags and sound comparisons |
| Voice/envelope/event coordination | Port deliberately into native owner; preserve pedal/retrigger semantics |
| Python render-ahead coordinator | Keep in v1.2; replace only in isolated successor |
| Native bridge | Reuse lessons/safety tests; do not hot-patch its production callback |
| State schemas/atomic persistence/control validation | Retain validated semantics behind a versioned engine contract |
| Browser layout and reconnect model | Retain, improve discoverability after audio proof |
| Device discovery and stage policy | Retain intent, qualify selected backend/profile behavior |
| Recorder and sample library | Keep disk/decode work outside audio; bounded transfers and ownership |
| Historical tests and evidence | Keep; never weaken512-only guards to make256 pass |

## Next decision, not an automatic deployment

Once M1 works, review the prototype evidence and port a sound-compatible slice
for M2. Before porting, freeze reference fixtures: complete patch state, asset
hashes, MIDI timestamps/pedals, sample rate, random/phase seeds where available,
gain alignment, and predeclared numerical/listening tolerances. A source tag
alone is not a repeatable sound oracle. Use the explicit feature ledger in
`native_v2/README.md`, not broad 'engine complete' labels.
Before any Pi compilation/stress test or service interruption, confirm
an off-stage maintenance window: a separate checkout does not isolate CPU/USB.
No production install command is part of this milestone. Record incomplete gates
honestly. A prototype may be rejected if its measured benefit does not justify
the migration cost.
