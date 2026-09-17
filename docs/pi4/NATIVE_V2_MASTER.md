# Native-v2 master chain — September 17, 2026

## Implemented and verified offline

`StageMaster` owns the stereo shuffler, three-band EQ, selectable high-pass,
pre-gain, self/piano/LFO/BPM bus compression, FX bypass add-back, saturation
and lookahead limiter. Fixed 48 kHz, whole blocks of 512 or 256 frames only.
This is a separate tested component, **not a complete replacement instrument**.
No Pi contact, restart, deployment, hardware audio/MIDI or production state
change occurred. M2/M3 and live qualification remain open.

Six pad inputs are PadAmbience's mixed L/R, dry L/R and FX L/R. Only compression
with FX bypass selects the dry/FX representation; otherwise the mixed pair is
used. Optional piano/organ input is already-prepared dry stereo. These channels
must not all be summed. The output is post-limiter but **before bridge float32
conversion, master fader and device gain**. The 0.98 ceiling is a sample-peak
limit at this internal boundary, not an analogue/true-peak guarantee.

The original two paths are retained: actual Faust master processing when
compression is off; double-precision EQ/HP/pre-gain and compression-before-FX
add-back when enabled. Actual Faust self compression is preserved, along with
the original block-RMS fallback/external sidechains. Suspended paths retain
their state. Original native ratio clamping and inactive controls are not
silently redesigned. The BPM retrigger method requires the future caller to
qualify the note and retrigger setting. Limiter reset is only the old master's
local panic slice, not full-instrument panic.

Construction prepares native state and fixed buffers while stopped. Invalid
arguments are refused without modifying output/state; nonfinite audio or a
numeric failure faults the owner and silences output until reconstruction.
STOP is terminal. This fault policy is not a promise of glitch-free recovery.

## Explicit corrections, not hidden sound-parity claims

1. The original limiter's release branch can exceed its declared ceiling.
   Constant stereo input 2.0 reproduces 0.9803541051865141 at a 0.98 ceiling.
   Default `CeilingSafe` caps released gain at the current lookahead target and
   clamps output roundoff to the ceiling. `Reference` retains the old limiter
   behavior solely for offline comparison, not as a supported stage profile.
   The corrected oracle is explicitly modified; it is not labeled untouched
   v1.2 parity. Neither this tiny overshoot nor its correction is established
   as the cause of a heard stage problem.
2. The fallback compressor's zero-knee, exactly-at-threshold scalar calculation
   raises `ZeroDivisionError` in the original. The native implementation uses
   the continuous hard-knee limit: zero reduction. The original exception and
   native value/neighbouring limits are separately tested. Other comparison
   fixtures do not intentionally hit this singularity.

The limiter now finds the same inclusive 73-sample lookahead maxima using a
fixed monotonic index queue instead of rescanning overlapping windows. Each
sample is inserted/removed at most once. It retains the original 72-sample
(1.5 ms at 48 kHz) delay. This is an algorithmic work reduction, **not a measured
Pi4 speedup or physical latency result**.

## Evidence

Pinned reference: `57bb94cdbd3c9349fc2a47833d659451ebb8738b`.
Mac-only tests build actual reference Faust DSP and selected original C filter/
limiter kernels. The oracle extracts only original wrapper/filter/compressor
declarations and the master routing body; it does not import/start the app.
Diagnostics/meters are excluded. The RMS oracle uses actual SciPy `lfilter`.
An isolated private Mac venv supplies SciPy; no global Python or Pi dependency
was changed. Dependency versions, commands and hashes are retained in report.

| Test | Result |
| --- | --- |
| Master synthetic, 524,288 frames (~10.92 s) per cadence/policy | Worst audio difference 8.621e-14 |
| Archived pad ambience, 327,680 frames (~6.83 s) per cadence/policy | Worst audio difference 5.106e-10 |
| All eight master comparisons | Pass unchanged 1e-6 tolerance; worst compared state difference 2.286e-13; safe peak at most 0.98 |
| Direct limiter, 768 blocks per cadence/policy | Exact sample equality to respective oracle, including reset, impulses, random peaks, constant and quiet signals |
| UBSan guard, 2,000 master transition blocks | All sidechains, finite/alias/config/STOP behavior, limiter ceiling and hard-knee corrections pass |
| Calibrated allocation probe | Zero post-warmup C++ new/new[] calls in the guarded master exercise |
| Reviewed legacy regression | 549 Python tests, zero skips, Node UI/syntax checks; 128 Python files parsed |

Cadences are compared to their respective original processing, not claimed
identical to each other. Saved ambience is fed into the master offline; it is
not yet an owned source-to-output render. Piano in this master fixture is a
synthetic dry input; prior FluidSynth/source evidence remains separately scoped.
UBSan covers C++ owner/guards, not generated Faust C or external dependencies.
The allocation probe does not intercept C malloc, audit locks or prove deadlines.
ASan, physical listening and Pi4 headroom/latency remain unverified here.

Reproduce with existing C/C++/Faust, NumPy, CFFI and SciPy:

```sh
python3 tools/compare_stage_master.py --ambience-dir /absolute/path/to/six-channel-ambience-evidence
python3 tools/run_offline_tests.py
```

Without `--ambience-dir`, only synthetic master inputs are tested. It must
contain `ambience-sources-native-512.npy` and `ambience-sources-native-256.npy`,
not seven-channel source stems or eleven-channel StageCore outputs.

Private evidence:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/native-v2-master-20260917/report.json`
SHA256 `8e648f634f2ded3c68513c6710e5838d07a1db7281012f58c87ee2b294c88700`.
The directory retains native binaries, reference/candidate arrays, fixture and
source/artifact hashes. No private render arrays or samples are committed.

## Next

1. Complete wet-output filter, global modulation/drift and sympathetic routing.
2. Join source acquisition, piano room/sends, PadAmbience and StageMaster under
   one owner without processing PadBus twice; migrate the final bridge gain
   boundary. Compare the entire routing, not just sequential saved arrays.
3. Migrate remaining beds/organ/recorder/splits/macros/scenes/prepared-program
   behavior and integrate bounded controls/events/telemetry with existing UI.
4. Build an isolated Pi4 candidate and qualify target timing, hardware recovery
   and player acceptance in an approved off-stage window. Live 256 remains
   unsupported. No hardware monitoring process was started or left running.
