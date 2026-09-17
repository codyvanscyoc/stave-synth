# Native-v2 limited listening candidate — September 17, 2026

User reprioritized: **defer independent drone, recorded pad and recording until
after the morning audio test**. These remain product requirements; do not
restart their migration before the requested audition. This checkpoint adds
the missing live-adapter source, not a deployed replacement or full M5/M6 pass.

## Implemented for the limited audition

- `AuditionSession`: one native whole-block audio owner, raw-key MIDI including
  velocity-zero release, sustain/sostenuto, CC123 musical release and CC120
  terminal STOP. Same collapsed channel/key ownership as the existing graph.
- Fixed128-entry SPSC absolute-control queue, at most32 commands per block,
  monotonic applied IDs, explicit backlog rejection and priority terminal stop.
  MIDI never shares that control queue. Malformed MIDI or more than256 packets
  in a callback is a terminal silent fault, not a dropped-release continuation.
- `audition_jack`: explicitly opt-in JACK/PipeWire host, exact isolated client
  prefix, no server autostart, explicit stereo sink and MIDI source ports.
  Refuses the ordinary `StaveSynth` client if still present. No normal service,
  graph quantum, output hardware gain, saved settings or preset changes.
- Fixed48k/512 or256 contract; mismatch is refused, never changed implicitly.
  Audio runs directly on the native callback: there is **no Python render-ahead
  queue**. Third-party real-time safety and actual deadline behavior still need
  target evidence; C++ alone is not qualification.
- Callback wall maximum/over-budget counters, xruns, played-note count,
  unsupported/quantized MIDI counts, raw piano full-scale warnings and applied
  control IDs. Atomics are individually safe, not transactional snapshots.
- Output starts with a separate exact-zero startup gate. The old output smoother
  starts at.85, so merely targeting zero was insufficient. First explicit
  nonzero master command opens the audition gate. Later master moves retain
  existing smoothing; this is not a redesign of oscillator mute/click behavior.
- Temporary private-IP browser interface with piano/OSC1/OSC2/filter/reverb/
  master plus envelope, wave, shimmer, delay, piano-room/send and freeze controls.
  Acknowledged versus queued values are separate. No stale gesture replay;
  bounded backlog; same-origin/Host checks; no pairing on trusted private LAN.
  MIDI/audio continue if only the browser closes. Loss of the control **process**
  ends this temporary audition. This is not the production browser recovery model.
- Explicit device route loss, graph error or three seconds without advancing
  audio blocks ends the audition. Cleanup deactivates/closes only its own client.
  No automatic device fallback, restart or production-service restoration inside
  the host: restoration belongs to an authorized external maintenance guard.

## Deliberate limitations

This is a separate listening surface, **not the familiar five-fader production
UI or a preset-compatible migrated instrument**. It starts with a documented
native default patch (piano only, master muted), not the user's saved sound.
Fixed12 oscillator slots / three-copy unison /32 piano voices remain unchanged.
The phase/random stream has an explicit reproducible audition seed; production
randomness remains a separate decision.

MIDI packets for one callback are processed in order at the block's beginning.
Offsets are retained/countable but quantized, advancing them by less than one
block on the render timeline. We do not slice the graph: the compared piano
compressor/envelopes and effects depend on whole-block processing. **Not sample-
accurate timing or a measured keyboard-to-DAC latency claim.**

Organ, pitch bend, live piano program changes, presets/MIDI maps, macros/scenes,
remaining per-voice motion/unison paths and full browser parity are not present
in this limited audition. Unsupported channel MIDI is counted, not advertised
as working. Independent drone/pad/recording are deferred by the user; the shared
reverb effect named Drone is a different, already-implemented effect.

No core DSP math/gain/precision was changed. The earlier strict independent
high-gain sound comparison still fails, and raw piano full-scale warnings still
need review. An adapter PCM comparison does **not** close either sound finding.
No claim of click-free oscillator toggles, live256, complete app performance,
actual device recovery or stage qualification follows from this checkpoint.

## Checks and private evidence

Mac device-free checks use actual existing Faust/FluidSynth and the real JACK
headers copied read-only from the Pi, but fake JACK functions—no server/device.
`tools/check_native_audition.py` verifies saved object hashes, specialized DSP
source identity and compiler identity, rebuilds current C++ with UBSan, and
records its manifest/commands in a new private output directory.

- Audition output equals directly driven StageInstrument PCM exactly in the
  fixture at512/256 (180 musical blocks each plus setup), including note order,
  sustain and filter edits. This is adapter preservation, not v1.2 sound parity.
- All exposed control endpoints render; invalid/duplicate controls rejected;
  full queue rejected without false acknowledgment; fixed drain bound verified;
  STOP prevents pending-control replay;4,000 concurrent SPSC controls complete.
- Fake JACK tests cover512/256 callbacks, inactive/fault silence, bounded MIDI,
  graph size/rate changes, shutdown/xruns, signed/overflow CLI validation and ten
  startup/route/disconnect/EOF/production-client refusal/cleanup paths.
- UBSan covers current C++ code, not generated C/external library internals.
  No new claim of complete malloc/lock-freedom or real-time timing.
- Reviewed regression allowlist now includes seven audition control/HTTP/UI
  tests (ephemeral loopback only).562 tests plus Node checks pass on Mac.

Latest private native report directory:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-audition-production-guard-20260917/`.
Report SHA256:
`7534192bef90fa152c0fe8fdebe7441efc835babdde97365def358cb3fb0095c`.
The copied real JACK headers are retained in its `jack/` directory; the report
records their individual hashes. Latest562-test run completed in13.498seconds.
Earlier reports remain separate, including the first reuse refusal caused by
the comparator's DSP provenance being stored as generated artifacts rather than
source-manifest entries. No failed report was overwritten.

## Current Pi state and next gate

Only read-only SSH checks and copying installed JACK headers to Mac occurred in
this batch. Working service remained active PID683040 in the rehearsal checkout;
JACK development metadata reports1.9.22, observed temperature71.575°C. These are
point-in-time readings, not ongoing monitoring. No build/test/driver was started
on the Pi and no service, route or settings changed.

Before moving to physical audio:

1. Obtain current confirmation that the keyboard/interface are connected, sound
   is muted and temporary pause/restore is safe. The earlier maintenance job was
   completed; do not treat its pause approval as perpetual.
2. Inventory actual named MIDI/stereo ports and working service identity/config.
   Install no dependencies or service changes. Use a new private source directory.
3. With guarded restoration of the same working service, build on Pi using
   `tools/benchmark_native_instrument.py --build-audition` and an explicit SF2,
   new evidence directory, source label and verified DSP reuse. The original
   reusable compiled-object report is
   `/home/codyvanscyoc/stave-native-probe-20260917.gpQ10J/evidence`.
   The later reused-object report is not itself first-generation compile evidence.
   Preserve80°C guard, serial work and bounded durations; never compile while
   the player is using the working synth.
4. Require target guard/build success. Start the isolated host at **512 first**,
   master muted, on an explicit private-IP URL/unused8082..8090 port. Normal UI
   on8080 is not this audition. Verify advancing callbacks and correct routes
   before inviting the player to raise master gradually.
5. Observe piano articulation/pedal, then blend both oscillators and move
   filters/effects while collecting native telemetry. Judge sound/headroom and
   continuity before a separately authorized256 experiment. Preserve fallback.

Until those target checks/activation happen, **the new build is not yet ready
for the player to test through the normal Stave UI**.
