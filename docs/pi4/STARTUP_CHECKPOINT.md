# Pi4 application startup checkpoint — 2026-09-14

This is **not stage qualification**. The user confirmed off-stage maintenance
with only Wi-Fi connected. Peavey audio and Yamaha MIDI were absent.

## Tested candidate

- Source: `f7a4e02106d26e921a85a77766ea29e1b9b025ce`, `pi4-stage-pro`.
- Native artifacts: all 16 hashes still match [TARGET_BUILD.md](TARGET_BUILD.md).
- Existing Python 3.13.5 live venv used without installing/changing dependencies.
- Mac and Pi: **286 Python tests, zero skips**. Node UI regression/syntax tests
  passed on Mac; Node was unavailable on Pi and is not claimed there.
- Target preflight: 84 passes, 11 warnings, zero failed prerequisites. Warnings
  include intentionally stopped baseline, missing physical devices/preferred
  Yamaha output, absent memory controller, and inventory-vs-runtime limitations.
- Tracked service unit passed `systemd-analyze --user verify` on the Pi.
- Baseline service stopped cleanly with `Result=success`, PID zero. No live
  source was overwritten. Additional verified private backup is in [BASELINE.md](BASELINE.md).

## First isolated full application run

Transient unit `stave-pi4-qa.service`, invocation
`40d80be07932427fa48488320345b8de`, owned PID 116305. Separate copied config/data,
instance `qa`, JACK name `StaveSynth_qa`, loopback HTTP 18080 / WebSocket 18765.
No automatic audio/MIDI connections. Systemd Type=exec with no restart policy;
the launcher's complete native defaults and strict check were active.

- Process launched at 17:44:56 CDT; app's final running marker at
  17:44:59.042. This was a warm filesystem/process start, **not a cold boot**.
- FluidR3 loaded as `Fluid`, program 0, soundfont ID 1. Piano owner alive and enabled.
- All required native backends ready, no missing modules; graph 48,000 Hz /
  512 frames, six ring slots (saved low-latency setting).
- Render thread reported FIFO priority 80, pinned to CPU 3.
- Both listening sockets confirmed owned by PID 116305. HTTP UI and no-store
  runtime identity served correctly. `get_state` and `debug` answered on WS.
- Control worker, HTTP/WS listener, handler and event-loop health were good.
  No state warning, control error, graph error, MIDI drops or UI recovery attempt.
- Ten-second idle progress check: accepted render count 3940 → 4879; consumed
  JACK callbacks 3945 → 4884. Underruns stayed **7 → 7**, xruns **0 → 0**.
  Startup underruns are retained as evidence, not erased or called zero.
- Cumulative startup-inclusive render measurements at sample 4880:
  p95 bucket 5.547–5.653 ms; p99 5.867–5.973 ms; max 25.794 ms;
  seven cycles exceeded the 10.667 ms block period; no rejected writes or missed
  telemetry samples. The ten-second window added zero over-budget cycles.

These are **idle software measurements**, not maximum-layer capacity, physical
latency or audible quality. Piano's sample render counter stopped at 400 during
intentional idle sleep; mixer and JACK continued, as expected. No notes were
played. USB recovery, Safari controls, room/PA output, favorite-patch comparison,
service rehearsal and extended soak remain required.
