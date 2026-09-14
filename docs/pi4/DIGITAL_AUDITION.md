# Pi-rendered digital audition

This is a short, repeatable listening test, not stage or physical-device
qualification. It sends MIDI into the actual Pi application and captures the
final JACK stereo output, including native master/fade and underrun silence.
The built-in recorder is an earlier signal tap and is not used here.

## Safety and execution

Use an authorized off-stage maintenance window. Preserve the stage state and
data, stop the normal service, and launch an isolated `audition` instance with
private copied config/data, loopback HTTP 18080 / WebSocket 18765, and the same
strict native profile and existing venv as the stage service. No generic
installer, dependency update, DSP rebuild or buffer-size change is needed.

Compile `tools/native_audition.c` on the Pi with its installed JACK headers:

```sh
cc -std=c11 -O2 -Wall -Wextra -Werror -o /explicit/private/native_audition tools/native_audition.c -ljack -lm
pw-jack /home/codyvanscyoc/stave-synth/venv/bin/python -B tools/run_audition.py --driver /explicit/private/native_audition --output-dir /explicit/private/NEW-captures
```

The script rejects any runtime identity other than `audition`. The native
driver requires exactly `StaveSynth_audition:midi_in`, `:out_L`, and `:out_R`,
with no pre-existing connections. Separate MIDI-source and audio-capture JACK
clients avoid a dependency cycle. Audio is captured into bounded pre-touched
RAM and written after callbacks stop, to a new stereo float32 WAV. No physical
output is selected or connected. Complete recordings with failed counters are
retained as failed evidence; they are not silently treated as passing.

Afterward stop the isolated instance, verify the original stage state was not
changed, and restart/verify the normal service. Do not leave the stage system
stopped or point it at audition config/data. Private manifests contain saved
state and belong outside Git.

## Five comparable takes

Each take is 55 seconds: two-second lead-in, 45 seconds of the same sustained
Cadd9 / G/B / Am7 / Fadd9 progression (repeated, then C/E / Fadd9), and an
eight-second release/effect tail. Bass velocity is 66 and other voices 72.
Every chord is 4.5 seconds; note release is at 4.15 seconds, pedal release at
4.35 seconds. Final sustain/sostenuto release and all-notes-off are explicit.

1. Saved Fluid piano voicing, oscillator layers muted, external piano
   reverb/delay sends zero. Saved piano room/filter/compressor retained.
2. Piano plus sine/triangle OSC layers, three-voice unison, warm wash reverb.
3. Same layers, with a slow logarithmic filter sweep 800 → 6500 → 800 Hz.
4. Same layers, gradually increasing wash wet mix, shimmer, and piano reverb
   send. Automation uses absolute controls with acknowledged values at 5 Hz.
5. Piano-only comparison, as take 1 except the piano high-cut opens to 12 kHz.

All takes use the same master level 0.75. There is no loudness normalization.
These are deliberately labeled audition variations, not alterations to the
saved stage patch or a reference-vs-repaired DSP null comparison. The original
saved piano high-cut is approximately 753 Hz, which should sound substantially
darker than the explicit open-tone take.

## Evidence and limits

`tools/analyze_audition.py /explicit/finalized.wav` provides read-only numerical
screening of WAV format, file hash, finite samples, peaks, RMS/DC, stereo/mono
behavior and threshold activity. It does not claim subjective sound quality,
true-peak headroom, upstream clipping absence, or MIDI-to-speaker latency.

Per-case JSON retains setup, states, driver events, automation timing, render
metrics and before/after underrun/xrun/MIDI counters. Counter intervals include
driver graph setup/teardown as well as capture; the driver's armed-interval
counters are narrower. Neither counter should be silently discarded.

Listen to lossless WAV first for detailed judgment. MP3 is a convenience
preview only. Format conversion must preserve level; do not normalize, EQ,
compress, denoise or repair captured glitches. USB audio, physical keyboard,
pedal, Safari recovery, cold boot and full-service rehearsal remain separate
required checks in [TEST_TONIGHT.md](TEST_TONIGHT.md).

## Actual target result — 2026-09-14

Tooling source `7a2f7dd`; application/DSP runtime remains `483d953` with the
previously verified native artifacts. **325 tests pass on Mac, zero skips**,
plus Node UI tests/syntax. The 29 initial audition tests passed on Pi; the
revised 12-test orchestration module, including real loopback keepalive,
also passed on Pi. No application, native DSP, dependency or latency-profile
changes were made for this recording batch.

Final isolated invocation `83200f7991e9472ab79fdcf77f41b801`, instance `audition`,
48 kHz / 512 frames / six ring slots. Each of five WAVs contains **2,640,000
stereo frames (55 seconds)** and all **129 MIDI events**. Every native capture
completed with zero xruns, invalid samples or errors. Each before/after capture
interval added zero bridge underruns, xruns, MIDI drops, control drops, rejected
writes or invalid/missed render telemetry samples. However, the stricter
render-deadline gate did not pass all takes:

| Take | Mean render ms | p95 bucket upper ms | p99 bucket upper ms | Blocks over 10.667 ms |
| --- | ---: | ---: | ---: | ---: |
| Piano reference | 4.770 | 5.760 | 6.293 | 1 |
| Warm layers | 7.481 | 8.320 | 8.853 | 1 |
| Filter movement | 7.611 | 8.640 | 9.920 | 8 |
| Shimmer build | 7.770 | 9.600 | 11.733 | 112 |
| Open piano | 4.768 | 5.653 | 6.293 | 0 |

These are before/after histogram differences, not whole-process quantiles.
Intervals also include driver setup/teardown. Existing underruns are retained:
9→9, 11→11, 14→14, 16→16 and 18→18 respectively; counts grew outside those
intervals during setup/panic transitions, so the entire session is not described
as zero-underrun. Filter automation sent 226 acknowledged settings, with maximum
send lateness 2.198 ms. The shimmer build sent 678, maximum 16.645 ms (three
serialized settings per tick). These are control timings, not physical latency.

### Open finding: piano waveform continuity

Despite zero bridge underruns, the final piano reference has four exact
**512-frame stereo-zero blocks**, starting near 11.030833, 15.532167, 33.526833
and 42.529500 seconds. The open-piano take, which passed the timing counters,
also has four, near 11.030833, 24.524167, 29.025500 and 42.529500 seconds.
Each starts 72 samples after a block boundary, matching the limiter's 1.5 ms
lookahead. These are 10.667 ms source interruptions, not proof of a JACK xrun.
The tested boundary samples are low level; audibility has not been established.

The strongest current explanation is `FluidSynthPlayer.render_block` returning
a zero block when its nonblocking native lock is held by `note_on`/`note_off`.
Different chord locations across repeat takes support a scheduling-dependent
cause. The 400-block idle-sleep threshold cannot explain 150 ms gaps between
pedal-up and the next chord. **Causation is not instrumented yet**; do not label
the lock as conclusively proven or the waveform continuity as passing.
Diagnose and correct this ownership path with regression/target evidence before
sign-off; the shimmer timing reserve also needs attention. No envelope, voicing,
unison or sample-rate reduction was used to conceal these findings.

All five transferred raw WAV hashes match the target originals. Numerical
screening finds no non-finite/full-scale samples, negligible DC, and global
mono-sum loss approximately 1.4–1.6 dB. Highest peaks by take are −26.20,
−24.15, −22.96, −24.17 and −23.51 dBFS. Mixed takes 2–4 have no exposed long
stereo-zero gaps after their lead-in; their layers can mask a missing piano
block, so that does not resolve the piano-only finding. Lossless listening
copies are 24-bit PCM WAV; the concatenated MP3 retains the original level.

### Preservation and service restoration

Private capture root on Pi:
`/home/codyvanscyoc/stave-synth-pi4-backups/audition-20260914.LM69y4/captures-v2`.
Local listening/evidence root:
`/Users/codyvanscyoc/Documents/stave-auditions-20260914.KlQVwd`.
The first attempt is retained separately: its piano WAV completed, but an
undrained WebSocket client queue blocked keepalive and prevented final health
collection. The recording client was corrected and regression-tested; server
keepalive was not disabled or changed.

The isolated service stopped successfully. Stage `current_state.json` SHA256
before/after remained
`ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
Normal service restored: PID **127464**, invocation
`a0cba514feb74e80b258070cbd4603eb`, active/running, zero restarts at verification.
HTTP stage identity verified from Pi and Mac; WS native/audio/control/UI health
good, original master 1 and Fluid piano restored. Post-test temperature 66.2 °C,
throttle flags zero. Peavey/Yamaha remained absent. **Not stage-qualified.**
