# Native-v2 composed core — September 17, 2026

## Outcome

`StageCore` now owns the source-fader adapter, keys/pedals/voices, actual Faust
oscillators, actual FluidSynth piano, piano chain, pad/filter/send buses and
piano room in one offline, whole-block render path. It renders eleven internal
channels. This replaces the previous saved-stem-only composition test with a
live-in-process source-to-bus comparison, **not a complete master output**.

M2 remains open. There is no new playable application, Pi deployment or live
256-frame qualification. All work and verification in this checkpoint ran on
the Mac without contacting the Pi, opening physical audio/MIDI or changing
production state. The preserved stage instrument is unchanged.

## Ownership and transitions

- Stage-facing oscillator controls are raw faders; SourceMix advances once
  per render and supplies the prepared linear amplitudes, pan and eligibility.
  Shared shimmer settings have one authority in `StageCoreConfig::buses.pad`.
- Configuration is validated before mutation and note-routing/envelope edits
  precede subsequent boundary commands. Commands must target the exact current
  block boundary. This is a single-owner API, not a concurrent UI API.
- All-muted oscillator blocks still advance envelopes and render piano.
  Oscillator and pad Faust processing pause, and the nine pad/filter/send
  outputs become exactly zero. Their native DSP state resumes on re-entry.
  Filter scalar smoothing continues, matching the original application.
- The original all-muted fallback uses separate biquads. Within this fixed
  three-unison, native-when-active slice, they have only ever received zeros,
  so their output and state remain zero. The reference executes those original
  filters and checks this; the candidate need not compute duplicate silent
  filters. This argument does NOT cover one/five-unison fallback paths, native
  loading failure or other signal injections. It is preservation, not a new
  click-free transition or tail policy; stored native ambience may resume.
- Source rendering now explicitly clears inactive Faust slot gates BEFORE
  compute, as the original does, in addition to post-render retirement. The
  pinned oracle extraction now includes that original pre-compute branch.
  Existing source comparisons still pass; this is not evidence of a newly
  observed audible defect in the prior implementation.
- Phase exhaustion or a downstream processing fault stops both sources and
  buses. Stop is terminal and silent; reconstruction must occur while stopped.
  The separate M1 scheduler's priority-stop/browser acknowledgement path is
  still not integrated here. Source statistics are not whole-system telemetry.

## Verification

`tools/compare_native_core.py` uses pinned original scalar/voice/piano/routing
code and actual generated Faust, with explicit locally backed-up FluidR3_GM.
It drives notes, pedals, controls, hard-pan, filters, shimmer, muted passages,
release/re-entry and room together. Each cadence renders 327,680 frames.

| Comparison | 512 frames | 256 frames |
| --- | --- | --- |
| First nine internal channels, maximum difference | 0 | 0 |
| Piano-room left, maximum difference | 5.327e-14 | 4.708e-14 |
| Piano-room right, maximum difference | 1.112e-13 | 1.103e-13 |
| Raw int16 piano difference | 0 LSB | 0 LSB |
| All-muted blocks checked | 87 | 174 |
| Prepared phase starts / final active voices | 32 / 0 | 32 / 0 |

Each cadence matches its own original reference; this does not assert that
256 and 512 produce identical output or meet Pi4 deadlines. Original idle
piano dither of one LSB remains present. No tolerance was relaxed.

Additional verification this batch:

- Composed-core guards pass 500 blocks at each cadence, covering invalid
  configuration, event boundaries, mute/shimmer/re-enable, continued piano,
  finite output, coupled phase failure and terminal silence.
- A calibrated C++ `new`/`new[]` probe observes zero allocations across that
  post-warmup configuration/event/render/stop exercise. It does not intercept
  C allocation, prove dependency lock-freedom or establish real-time safety.
- UBSan passes for the C++ core and source/bus guard paths. Generated Faust C
  and external FluidSynth are not instrumented. ASan remains unverified.
- Source recheck passes both cadences: five oscillator stems exact, dry piano
  worst difference 1.854e-13, raw piano exact.
- Bus recheck passes all four saved-source/synthetic comparisons: saved-source
  channels exact, synthetic worst difference 6.939e-18.
- Component comparisons pass: 11,340 envelope blocks, 10,652 key/pedal commands
  and 7,101 voice commands, all exact within their existing fixture scope.
- Reviewed legacy regression runner: **549 Python tests, zero skips**, Node UI
  checks and syntax checks pass; 125 Python files parsed.

These are bounded correctness/compatibility fixtures, not long-duration,
all-presets, acoustic quality, CPU-headroom or end-to-end latency qualification.

## Reproduce and retain evidence

```sh
python3 tools/compare_native_core.py --soundfont /absolute/path/to/existing.sf2
python3 tools/run_offline_tests.py
```

The first command builds only offline probes and never downloads samples.
Keep its printed report and generated artifacts together. The composed probe
reuses source fixture infrastructure: its `sources-native-*.npy` and
`sources-reference-*.npy` files contain **eleven channels**, not the seven raw
source stems expected as input by the older bus/room comparators. These are
internal buses with diagnostic/send channels; do not simply sum them as audio.

Private evidence under `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/`:

- Final core: `native-v2-m2-core-20260917-verified/report.json`
  SHA256 `9ebf1aa9ddb6cdde93488a2735d53e9964450ea1b74fff699a7731ee52e2c847`.
- Source recheck: `native-v2-source-core-recheck-20260917/report.json`.
- Bus recheck: `native-v2-buses-core-recheck-20260917/report.json`.

The core report records source/asset and generated-artifact hashes. Explicit
SoundFont SHA256 is
`74594e8f4250680adf590507a306655a299935343583256f3b722c48a1bc1cb0`.
No private samples or runtime/settings archives are committed.

## Next implementation and release gates

1. Port and compare ping-pong/shared reverb and master routing, gain and limiter
   with controls, tails and transitions. Complete remaining global modulation
   and drift coverage; do not silently replace them with fixed settings.
2. Preserve the independent sampled bed, tonic/fifth drone, freeze, organ,
   recorder, splits/macros/scenes and prepared program changes. They remain in
   the working application but are not yet implemented in this native owner.
3. Integrate bounded event/control transfer, telemetry, browser/state protocol
   and familiar five-fader UI without violating whole-block piano semantics.
4. Only then build an isolated target candidate and measure actual Pi4 worst
   render time, discontinuities, sustained workload and hardware recovery.
   Field listening and keyboard/interface qualification remain release gates.

Do not deploy this checkpoint as a replacement instrument or enable live 256
based on these Mac results. No new Pi job or monitoring process was started.
