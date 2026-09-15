# Render-owned piano program changes — Pi4 candidate evidence

Saved September 14, 2026. This is a scoped repair, not release approval.
Application change: `f89f127`; later `8beeca9`/`88dfc86` change test tools only.
Normal stage remains `ce15cfb` (application/DSP `483d953`).

## Reproduced problem and correction

Full-bank FluidSynth residency removed the roughly 226 ms cold Fluid→Rhodes
load stall without reducing sample/voice quality. A return program change
still held the native lock against an audio block. The new repair moves
preloaded program selection into the bounded render-owned MIDI queue.

The program marker orders preceding MIDI, program/velocity metadata and
following MIDI under the same native owner. No sample loading occurs on the
render thread. A pending request times out by being removed before it can
execute later; a claimed native call must complete before acknowledgement.
Panic, overflow, detach and failures resolve affected waiters. The stable
piano control owner is pumped even when piano is disabled or organ is routed.
Shutdown still requires actual producer quiescence before detaching ownership.

No claimed native C call is presented as safely cancellable. A wedged native
render still depends on the existing process-level watchdog/recovery policy.
No voices, unison, effects, sample rate, buffers or musical levels were reduced.

## Tests and actual Pi recordings

The repair passed 15 focused ownership regressions and the full 530-test suite
on Mac and Pi (zero skips); Mac Node UI/syntax checks also passed. Test-tool
additions subsequently passed 541 Mac tests, 19 focused Pi tests, and the
telemetry compatibility fix passed 30 core/extended tests on both machines.
The final tools/evidence checkout `ba8b68a` subsequently passed all 543 Python
tests on both machines, zero skips (Pi 57.203 seconds). Node ran on Mac only.

Two 55-second native Pi performance recordings used `f89f127`, 48 kHz,
512 frames, six ring slots and the full native profile. The only native
difference from the original manifest was the already-verified Leslie STOP
library `d7b1898096c0c9a0ec5d5e53e39bf85187ac2253fb95802e65bec7ff9b1d7455`.

| Observation | Take 3 | Take 4 |
| --- | ---: | ---: |
| Fluid→Rhodes acknowledgement | 10.600 ms | 13.523 ms |
| Rhodes→Fluid acknowledgement | 4.588 ms | 4.600 ms |
| Piano lock misses before final panic | 0 | 0 |
| Total piano lock misses | 0 | 1 |
| Total render-deadline increases | 86 | 78 |
| Total bridge-underrun increases | 1 | 1 |
| Functional sequence and state restoration | Complete | Complete |
| Strict overall result | FAIL | FAIL |

Both captures have finite stereo samples. Their only joint exact-zero run of
at least 128 frames is one 512-frame block at 48.010667–48.021333 seconds,
in the deliberate terminal panic region. Mixed-output continuity cannot prove
every individual source is continuous. Take 4's one piano miss records native
owner `all_notes_off`, not `program_change`; all program acknowledgements had
completed and reported `program_state=done`, without pending/error/overflow.

The pre-panic strict results contain only render-deadline growth (84/77),
not bridge underruns or piano misses. The remaining total failures are retained;
do not label every post-snapshot counter increase as proven panic-caused.

## Remaining bottlenecks

Organ-selected windows each contain 937 blocks. Mean wall/calling-thread CPU
remains 9.909/9.841 ms and 9.901/9.831 ms per 10.667 ms period. This is genuine
organ workload pressure; the piano ownership repair does not fix it.

The earlier interpreter-interval A/B/B/A experiment did not establish a useful
improvement. Only 1–2 of 33–43 retained render overruns overlap each autosave;
most do not overlap any retained control span. Short control spans below the
1 ms retention threshold are not individually available. Autosave, GIL and
scheduling must not be conflated as a single proven cause.

Private algebraic organ experiments are not installed: double/triple-angle
and triple-only alternatives fail the predeclared numerical-parity gates.
The triple-only version also misses the full-registration speed threshold.
Vector builds timed out without producing an installed library. Preserve these
failed experiments rather than weakening acceptance criteria to promote them.

The combined-core smoke/recovery/ten-minute results are recorded separately
in the private profiling index. Physical keyboard/interface recovery, analogue
latency, service-length player rehearsal and eight-hour qualification remain
open. A successful small test does not close those release gates.

Complete raw reports and WAVs are in the private Mac/Pi profiling directories,
under `performance-probe-3` and `performance-probe-4`.
