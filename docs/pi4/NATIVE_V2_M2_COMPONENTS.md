# Native-v2 M2 component checkpoint — September 16, 2026

The player authorized continuing overnight. This batch implements and tests
native musical components on the Mac. **M2 is in progress, not complete.**
The components are not yet a combined replacement instrument. No Pi command,
restart, deployment, device opening, buffer change or stage-state write was
made in this batch. No background test is left running at the checkpoint.

## Implemented and compared

Pinned reference: `57bb94cdbd3c9349fc2a47833d659451ebb8738b`, the preserved
v1.2 source tag. `tools/compare_native_v2.py` extracts explicit AST declarations
and methods from that Git object, not current working source. It never imports
the application, JACK runtime, browser, service or device setup. It compiles
only the original standalone biquad C function for the piano oracle.

| Native component | Passing comparison | Scope |
| --- | --- | --- |
| `BlockEnvelope` | 100 cases, 11,340 blocks; max difference 0 | Last sample, level and stage; 44.1/48/96 kHz and 1–4096-frame blocks |
| `StageNotes` | 10,652 commands; exact ownership, destinations and velocities | Repeated keys, transpose/octave, minimum velocity, sustain and sostenuto |
| `StageVoices` | 7,101 commands; max state/gate difference 0 | 1/2/3/12 voices; retrigger, dual envelopes, oldest/quietest-release stealing, slot reuse and cleanup |
| `PianoChain` | 524,288 channel samples per cadence; peak error 2.193e-13 at512 and 1.732e-13 at256 | Identical raw piano input through fader, HP/LP/EQ, compressor, velocity brightness and stereo tremolo |

Predeclared absolute tolerances remain 2e-12 for envelopes/voices and 1e-6 for
piano. No threshold was loosened to obtain these passes. Each piano cadence
was compared to its own reference: **the v1 compressor is block-dependent;
256 and512 are not asserted to produce identical processed piano.**

The piano fixture is 262,144 frames of actual FluidR3 int16 source at48 kHz:
six triads, different registers/velocities, releases, filter/EQ/compressor,
brightness, tremolo and fader transitions. Source capture reported zero
note-off misses and zero full-scale int16 samples. This is not a general
clipping or whole-piano parity qualification. Raw-input SHA256:
`b640045c92782a2ff1c75d4233f8457525e5b816d9e8f83b6c41996353c78dbe`.
The existing private SoundFont was reused, not downloaded or committed.

## Musical details deliberately retained

- Held same-pitch retrigger keeps envelope level and previously latched layer
  weights. Re-striking an already-releasing pitch may use another voice.
- Stealing prefers the quietest releasing OSC1 envelope, otherwise oldest;
  ties retain active-list order. The native pool is fixed capacity.
- Either oscillator envelope can keep a voice alive. The original sustain
  fast path updates its gate without overwriting stored envelope level; this
  matters when sustain is edited before release.
- Existing dead-voice slot clearing occurs after the following DSP render.
  The explicit `begin_block` / `end_block` contract preserves that ordering.
  No command/configuration mutation is accepted between those calls.
- Stage note ownership collapses MIDI channels as v1 does; it does not use
  M1's per-channel repeated-key counters. Sustain/sostenuto withhold note-offs
  instead of forwarding ordinary sustain to FluidSynth CC64.
- The piano fader retains its 10 ms smoothing and -40 dB curve. Disabled EQ
  state, compressor ramping, brightness and tremolo retain reference behavior.

These are compatibility choices, not claims that every old behavior is ideal.
Intentional click fixes or envelope redesign require separate comparisons and
listening approval; do not silently bundle them into a purported neutral port.

## Failure handling and allocation checks

`Engine::request_stop()` is a priority terminal latch independent of queue
capacity and future timestamps. The audio owner alone calls backend panic;
future events cannot delay the stop. Tests also request stop inside a render
preceding a note event and verify that event is not dispatched. Reconstruct
only while stopped. This is not tail-preserving musical release or a browser
STOP implementation, and cannot preempt a backend call already executing.

Guard tests cover invalid configuration/pointers/frame lengths, output overlap,
sentinels, in-place piano processing, nonfinite input fault silence, terminal
fault preservation, full-key cleanup, voice lifecycle/slot uniqueness and
recycling. Calibrated C++ `new`/`new[]` probes report zero calls during tested
engine, envelope, key/voice owner and piano-chain operations. This does NOT
prove absence of C malloc, third-party allocation, logging or blocking.

UBSan-only now **passes for scheduler and envelope/note/voice component tests**.
It does not instrument the generated Faust, FluidSynth or piano-chain tests.
ASan remains unverified because the prior combined sanitizer program stalled
before main; that earlier failure was not erased or converted into a pass.

The existing device-free regression runner also passes: **549 tests, zero
skips**, plus Node UI connection and source/shell/JavaScript syntax checks.
The M1 actual-Faust/FluidSynth tests and diagnostic WAV checks still pass;
they remain simplified M1 sounds, not renders of these combined M2 components.

## Reproducible evidence

Host: Mac arm64, Apple Clang17, Faust2.85.5, FluidSynth2.5.4. Not Pi4 results.

```sh
python3 tools/compare_native_v2.py --soundfont /absolute/path/to/existing.sf2
python3 tools/run_native_v2.py --ubsan --with-sound --soundfont /absolute/path/to/existing.sf2
python3 tools/run_offline_tests.py
```

New directories only; do not run compilers on a playing Pi. Private reports,
commands, source hashes, command fixtures, raw captures and binaries are under
`Documents/stave-synth-pi4-backups/` on the Mac:

- `native-v2-m2-components-final-20260916/report.json`
  SHA256 `43554f109ef2c59b5c96e187abfce532d8ac574c8ec112aaf7b14ce3f34ffd11`.
- `native-v2-m2-guards-final-20260916/report.json`
  SHA256 `5606b6bfb10e2c7dcc64ea1861886c742e8feddbd553d87bb8ae1ddfc2ea03e9`.

Final comparison additionally verifies five malformed voice-probe input
refusals. Earlier non-final successful reports are retained, not overwritten.

First envelope comparison exposed compiler floating-point contraction changing
an attack-boundary decision. C++ component builds now use `-ffp-contract=off`;
the reference tolerance was retained. An initial piano oracle attempted to
use unavailable SciPy; the final oracle instead uses the pinned original
biquad kernel actually used by the Pi. No dependency installation was needed.

## Exact next integration work

1. Build an offline stage-compatible source graph with preserved OSC1/OSC2/
   shimmer/piano stems. Do not simply connect `StageNotes` to the M1 backend:
   its repeat counters, phase initialization, gates, gain and float piano
   acquisition are intentionally different from v1.
2. Define source-event slicing versus complete-block finalization explicitly.
   Piano RMS compression must see the whole reference block; advancing the
   ADSR for each event slice would alter the demonstrated compatibility.
   Add block-boundary reference fixtures first, then test/document deliberate
   sub-block MIDI semantics, panic interruption and tail ownership.
3. Wire actual oscillator phase seeding/randomization, pitch bend, unison,
   modulation and drift to voice ownership. Current voice comparisons test
   gate/state/callback order, not oscillator waveform or random-phase parity.
4. Match actual Fluid note velocity, program/bank and source precision/gain.
   The piano-chain test feeds the same int16 capture to both sides; it does
   not approve replacing source acquisition with native float or prove actual
   piano release/pedal integration. Preserve room/shared effects separately.
5. Compare dry stems and the layered instrument at matched levels; then port
   worship FX, bed/drone/freeze and other features (M3) with transition tests.
   Complete real-time dependency audits and ASan on a working environment.
6. Only after source sound proof, arrange a confirmed Pi4 off-stage window for
   isolated target compilation/timing (M4). Browser parity and hardware/
   rehearsal/rollback qualification (M5/M6) remain required before promotion.

No claim of256-frame stability, speedup, measured analogue latency, complete
instrument parity or stage qualification follows from these component passes.
