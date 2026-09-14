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
