# Native-v2 checkpoint — September 16, 2026

## Latest: M2 components implemented, integration pending

See [NATIVE_V2_M2_COMPONENTS.md](NATIVE_V2_M2_COMPONENTS.md) for the overnight
batch, persistent evidence and exact next work. Native envelopes, key/pedal
ownership, voice/retrigger/stealing and dry piano processing pass pinned-v1.2
component comparisons. Priority terminal STOP is implemented in the scheduler.
UBSan-only passes for core/component guards; ASan remains unverified. All549
legacy tests plus Node checks pass again. No Pi contact/deployment this batch.

M2 is NOT complete: these components are not yet a combined sound-compatible
instrument or integrated UI. The M1 render demo still uses its simplified
backend. Full FX/bed and Pi4 timing/hardware gates remain open. The sections
below are the preserved M0/M1 evidence, not the latest implementation status.

## Historical M0/M1 checkpoint

## Outcome and limits

Preservation and the first offline native slice are implemented. This is **not
a finished successor, deployed repair, sound-parity pass or Pi4 timing pass**.
M2 through M6 in [NATIVE_V2_PLAN.md](NATIVE_V2_PLAN.md) remain open. The
[feature ledger](../../native_v2/README.md) identifies every missing function.

The working stage instrument remains available on the Pi. Final read-only
service check: active, MainPID185009, invocation
4e958aafdb1341d7958af803acd8ae4d, NRestarts0, rehearsal working directory.
No restart, source/DSP deployment, output routing or buffer change this batch.
Pi5/main and mac-port were not touched.

## Preservation evidence

- Annotated source tag `pi4-v1.2-stage-snapshot-20260916` at57bb94c.
- Complete verified Git bundle plus post-worship configuration archive on Mac
  under `Documents/stave-synth-pi4-backups/v1.2-worship-20260916.WfPhVY`.
- Pi configuration archive under
  `/home/codyvanscyoc/stave-synth-pi4-backups/v1.2-worship-20260916.ajiZZl`.
- Config archive SHA256:
  `bfd5501fb7705b3c686d173ca8fff0e608bdb82e18579e94f5e85a2f9c111e74`,
  verified on both machines. Config captured while service was active, not a
  quiesced transaction, full disk image or demonstrated whole-device recovery.
- Actual running app remains f600c7e/bd6a517; tag57bb94c also includes later
  policy/docs. A later docs-only stage commit points to this successor; it
  does not change the preserved tag or imply new deployed code.

## What ran and passed

Host: Mac arm64, macOS26.6.2, Apple Clang17.0.0, Faust2.85.5;
installed FluidSynth runtime/executable2.5.4. These are **Mac**, not Pi versions.

1. Existing reviewed regression runner: **549 Python tests, zero skips**, plus
   Node UI connection checks, Python parsing, shell/JavaScript syntax checks.
2. Native core: strict optimized C++17 build; timing/boundary/validation/fault
   checks, bounded-snapshot behavior, allocation probe and 20,000-event SPSC test.
3. Actual generated 12-slot Faust backend: note/release/sustain/channel behavior,
   repeat/steal cases, render bounds, visible overload clamp and panic silence.
4. Actual FluidR3 piano: piano-only and piano-plus-oscillator fixtures, missing
   asset refusal, 32-voice overflow/note-off-miss semantics. Missing-SF2 logs
   during the explicit negative test are expected, not fallback piano success.
5. Oscillator-only, piano-only and mixed scheduled comparisons: **max sample
   delta0 between 512- and 256-frame runs of these fixtures**. This does not
   compare with v1.2, every patch or live hardware.
6. Eight-second stereo48k float WAV fixture:27 dispatched events; peak0.035304,
   RMS0.00431549; no rejected/late events, native errors, nonfinite samples or
   export clamps. Both block-size files have SHA256
   `11d432e78afd729d3d42b2cf520f59f59414b817344cbbcc8abb96ce5f4ced76`.
7. Runner also passed with no FluidSynth linked, clearly reporting piano NOT
   exercised. Direct WAV output refuses existing files and dangling symlinks.

Raw commands, stdout/stderr, source hashes and binaries are retained privately:

- `Documents/stave-synth-pi4-backups/native-v2-m1-20260916/report.json`
  SHA256 `2cac7acf7a2ac68de14da767102870ae1ced0638382028820645ca246b15fbf6`.
- `Documents/stave-synth-pi4-backups/native-v2-m1-osc-only-20260916/report.json`
  SHA256 `88a72f3475d210cf311a0b77a0586aebe0cad15b51ee553928080dd743b91b4b`.

The SoundFont was reused from the existing private Pi backup, not downloaded
or committed. SHA256:
`74594e8f4250680adf590507a306655a299935343583256f3b722c48a1bc1cb0`.
Diagnostic WAVs intentionally have different gain/envelopes and no v1.2 FX.
They are not a representation of finished product loudness or quality.

## Failures encountered, resolved and still open

- First overload test failed because the blend smoother had not settled in its
  one-block stimulus. Extended the deliberate overload stimulus to32 blocks;
  retained the requirement for nonzero reported clamp count. Passing ordinary
  musical fixtures must still report zero clamps.
- Independent review identified unsafe existing-WAV detection and possible
  mid-build source-hash mismatch. Added exclusive file creation with refusal
  tests, and pre/post-build source-hash verification; both final runs passed.
- **ASan/UBSan is NOT passed.** Sanitized engine and an independent minimal
  sanitizer executable stalled before their first main marker inside the
  execution environment. Targeted escalation was requested but its invocation
  was aborted; no outside-sandbox result. Environment/startup is suspected,
  not proven. Do not describe this as clean sanitizer evidence. The core
  sanitizer runner times out after30 seconds; normal core tests pass.
- The allocation test covers engine plus fixed probe and C++ new/new[] only,
  not FluidSynth/Faust internals, malloc, logging, locking or scheduling.
- No speedup, sustained Pi4 CPU margin, reduced analogue latency, full voice
  semantics, click correction, production sound parity or live readiness claim.

## Exact next work

M2: create pinned v1.2 reference fixtures with state/assets/MIDI/pedals/seeds,
explicit sample rate and gain alignment. Port native ADSR/voice behavior and
piano processing; compare dry sources and layers before migrating the worship
effects/independent bed graph. Preserve stems and tails. The current scheduled
panic is not yet the priority emergency STOP required for live use.

Finish library real-time/allocation verification and sanitizer checks on a
working test environment before a live adapter. A Pi4 compilation or load test
requires a confirmed off-stage maintenance window even in a separate checkout.
Keep v1.2 playing until sound compatibility, native timing, control integration
and the practical hardware gates are actually accepted.
