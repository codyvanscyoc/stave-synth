# Native-v2 raw-key layer routing — September 17, 2026

## Implemented contract

`StageInstrumentConfig::splits` now owns independent OSC1, OSC2, shimmer and
piano key ranges. `StageInstrument::key_command` accepts raw-key events and
computes the original smoothstep crossfade weights before forwarding to the
existing key/pedal owner. This is an offline, single-owner, current-boundary
entry point, not a MIDI device driver, timestamp scheduler or browser API.

- Splits are evaluated on the physical MIDI key, **before** transpose or piano
  octave. Disabled splits give all four layers weight 1.
- Low/high endpoints remain 0..127; crossfade width is 0..24 keys. Each endpoint
  is validated independently, preserving the original behavior even for a
  reversed saved range. No silent sorting or range-normalization change.
- Weights latch at note-on. Editing a range does not reweight held voices.
  Note-off, velocity-zero note-on and pedal release use the original mapped
  destinations, not the new split range. This preserves release ownership.
- Piano weight scales note velocity as before; it is not a new post-piano
  volume control. Fully excluded oscillator layers do not start a voice.
- Invalid configuration is refused before mutation, including while splits
  are disabled. Invalid raw keys are refused. STOP remains terminal.
- Existing `command(frame, StageCommand)` retains its explicit prepared-weight
  diagnostic contract. It does not also apply splits. A caller must choose one
  entry point for each event, never submit through both.

The original same-pitch retrigger/release policies remain unchanged. Musical
ReleaseAll starts releases; it is not an immediate voice-slot purge or hard
STOP. This work does not add organ routing, preset octave-stash conversion,
MIDI channel zoning, scene transitions or live control transport.

## Independent comparison and safety tests

`compare_stage_splits.py` extracts only the pinned v1.2 `split_weight` method
from Git object `57bb94cdbd3c9349fc2a47833d659451ebb8738b`. It never imports the
application. The native implementation is checked against that unchanged
Python method, not another hand-translated formula.

856 configurations × all 128 raw keys = 109,568 cases / 438,272 layer weights,
all exactly equal. Includes every crossfade width, singleton/edge/full/reversed
ranges, independent seeded ranges and enabled/disabled changes. UBSan and a
calibrated C++ new/new[] guard cover validation, output non-mutation on refusal
and 256,000 weight calculations with no C++ allocation.

The connected instrument runner retains its original eight comparisons and
adds two independent raw-key split fixtures (512/256). Candidate events carry
raw keys only; the original MIDI/voice/piano reference receives weights from
the pinned split method. Fixtures exercise split edits during sustain/sostenuto,
silent layers, partial piano velocity, positive/negative transpose, independent
piano octave and velocity-zero release. Actual Faust/FluidSynth and the full
room/filter/FX/master/output path run, with voice/phase ownership and eventual
voice retirement checked. The 1,400-block C++ guard also exercises key routing,
rejected configuration, transpose-before/after distinction, pedal release and
terminal silence with scoped zero C++ allocation.

An intermediate guard incorrectly expected ReleaseAll to remove a voice slot
immediately. The guard was corrected to preserve the existing release-tail
contract; no production release behavior was changed to satisfy the test.

## Reproduce

Final connected results: split fixtures PASS the unchanged 1e-6 sound gate,
with worst internal-channel differences 2.059e-11 at 512 and 1.885e-9 at 256;
final PCM differences are 4.657e-10 / 1.863e-9. Each has 37 note-ons, 13 partial
layer weights and 47 zero weights. Raw piano acquisition is exact, phase/voice
ownership agrees and no oscillator voices remain at the end. Same-input output
and original-reverb replay are exact across all ten runs. C++ guards pass.

The original source fixture still passes. The original independent overload
fixture still FAILS 1e-6 at 2.795e-6 / 2.027e-5, unchanged from the previous
checkpoint. The complete runner exits 1 with `sound_difference_review_required`;
this is not an all-tests-pass milestone. Reviewed legacy regression: 549 tests,
zero skips, Node checks pass; 131 Python files parsed.

```sh
python3 tools/compare_stage_splits.py
python3 tools/compare_stage_instrument.py --soundfont /absolute/path/to/existing.sf2
python3 tools/run_offline_tests.py
```

Private component evidence:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-splits-20260917/report.json`
SHA256 `c424f1615b7022c87e4d37066f7d554f0db930f4b5c1e81e0ec6e5d8721e05de`.

Final connected evidence:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-splits-integrated-verified-20260917/report.json`
SHA256 `13881ade162014e282212ef9d9a72c3a9979c6180bf15b3a351d94dfdea4a99c`.
All source fixture variants, phase tape, native/reference arrays, commands and
source hashes remain private alongside the reports; no sample assets committed.

The high-gain independent sound gate remains separate and must not be waived
because split routing passes. See [output diagnosis](NATIVE_V2_OUTPUT.md).
No Pi contact, deployment, service changes or production state edits occur in
these tests. Mac correctness is not a Pi4 deadline, latency or live-256 result.

## Next work

Remaining musical breadth includes global modulation/drift/sympathetic,
independent sampled beds/drone, organ, remaining unison/program/pitch controls,
recorder and scene behavior. After that, integrate bounded events/control,
browser state and the backend/recovery path, then qualify on Pi4 and with the
player. The working v1.2 remains available; native-v2 is not field-test ready.
