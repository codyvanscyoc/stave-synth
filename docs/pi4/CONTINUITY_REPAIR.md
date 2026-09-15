# Piano continuity and native preparation — Pi4

This is a focused reliability/efficiency batch following saved checkpoint
`ce15cfb`. It is not a new sound design, buffer profile or stage-release sign-off.
Keep the five-fader performance surface, existing patches, oscillator count,
sample rate, effects algorithms and musical gain structure unchanged.

## Reproduced mechanism

The September 14 piano-only captures exposed complete 512-frame zero blocks
near chord onsets despite no new bridge underruns. An independent offline
reproducer extracted the actual `note_on` and `render_block` methods from
`ce15cfb`, held a fake native note-on with an event on the MIDI thread, then
called the production renderer. It returned exactly 512 stereo-zero frames
without calling `get_samples`. This proves the lock-contention failure path;
the original target recordings were not instrumented to prove each gap's cause.

## Candidate behavior

- Musical piano note-on/off/pitch commands use a bounded FIFO, applied before
  sampling and before the idle gate. The render path must not wait for the
  event-queue lock; a short contention defers commands, not existing audio.
- The native lock still coordinates loading, program selection and shutdown.
  Rare native-transition lock misses remain possible and are now counted.
  Seamless arbitrary program changes are not certified by this patch.
- Synchronous panic keeps its existing ordering before JACK ring clearing and
  cancels stale pending musical commands. Overflow/error recovery must be
  bounded, explicit and observable; it is a failed qualification condition,
  not evidence that losing musical events is acceptable.
- [FluidSynth's MIDI API](https://www.fluidsynth.org/api/group__midi__messages.html)
  allows note-off failure to mean no matching voice. That case must not be
  confused with an exception or an unsuccessful channel recovery.
- The native oscillator path skips fallback-only detune/pan, phase-increment,
  shimmer-scratch and gain-array preparation. Native parameters, random draws,
  envelopes and Python fallback formulas remain unchanged. Tests of extracted
  preparation do not prove full hardware-output parity or target speedup.

## Qualification rules

For a disposable isolated systemd unit, use **Type=exec, no systemd watchdog**
and a bounded runtime. Isolated mode deliberately sends neither READY nor
WATCHDOG notifications, so Type=notify is incompatible. Check HTTP/WS identity
and engine health explicitly; process creation alone is not readiness.

The repaired-candidate audition tool requires piano-source counters. Missing,
malformed, reset or growing failure counters cannot become implicit zeros.
Manifest format 2 records that requirement. Use the preserved older tool for
old-build baseline captures; older recordings remain valid historical evidence,
not evidence that newly required source counters were measured.

Retain terminal panic/setup effects in raw counters and report them separately
from continuous playing. Do not discard a native-lock miss because its cause
was an intentional stop. Queue-lock deferrals are not event loss, but physical
latency and heavy-event render cost still need measurement.

Before promoting any further repair, repeat piano-only and layered/automated
recordings on the actual Pi in an isolated instance, compare continuity,
source/bridge counters and timing distributions, and preserve original saved
state. This candidate's completed run is reported below. Actual
Peavey/Yamaha playing, latency, recovery and rehearsal remain required.

The shimmer take changes three settings per automation tick; its previous
112 over-budget cycles cannot be attributed to shimmer DSP alone. If reserve
remains insufficient, split wall-time from render-thread CPU time and profile
individual stages before reducing quality or changing a buffer setting.

## Evidence locations

Pre-repair private checkpoint:
`/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/checkpoint-20260914.zAwHaf/INDEX.md`.

Completed candidate evidence directories (see measured results below):

- Mac: `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/continuity-20260914.MvFj78`.
- Pi: `/home/codyvanscyoc/stave-synth-pi4-backups/continuity-20260914.bSKT8q`.

No raw recordings, copied private settings or credentials belong in Git.

## Actual target results — September 14 evening

Candidate **`27522fd`**: 350 Mac allowlisted tests, no skips, plus Node UI/syntax;
62 targeted regressions on Pi. All 16 target native hashes matched. The candidate
ran from the detached private worktree, not the normal stage checkout.

Five complete 55-second / 2,640,000-frame captures, each with 129 MIDI messages,
finished under invocation `3574c036e4f940cdaba493acfaeff8ab`, PID 151016. Profile
remained 48 kHz / 512 frames / six slots, with matched audition settings/levels.

| Take | Mean ms | p95 upper ms | p99 upper ms | p99.9 upper ms | Over 10.667 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Piano | 4.793 | 5.760 | 6.400 | 8.000 | 1 |
| Warm layers | 6.549 | 7.360 | 8.000 | 9.600 | 2 |
| Filter | 6.653 | 7.573 | 8.853 | 9.600 | 3 |
| Shimmer | 6.809 | 8.427 | 10.347 | 12.267 | 33 |
| Open piano | 4.801 | 5.760 | 6.293 | 7.893 | 1 |

These are before/after histogram differences, including setup/teardown, not
whole-device CPU percentages. Layered means improved approximately 12.4–12.6%
versus the previous short batch. Shimmer p99 upper improved 11.733→10.347 ms and
over-budget cycles 112→33. Piano alone was essentially unchanged. No isolated
single-change attribution or sustained 30–40% demanding-patch reserve is proven;
shimmer p99 has only about 3% period reserve and p99.9 still exceeds budget.

All 530 queued piano events were applied; no discarded events, overflows,
recovery failures, native errors or pending commands. No capture errors, xruns,
nonfinite samples or truncated reports. Bridge underruns stayed 9→9, 11→11,
13→13, 15→15 and 17→17 within the five intervals, but grew between takes.
**The filter interval added one native render-lock miss.** It is unlocalized;
the interval includes terminal panic, but attributing it to panic is not proven.
All five strict results remain FAIL. Raw counters were not filtered to make
the candidate pass.

### Waveform continuity and control timing

Neither piano-only recording repeats the previous four internal 512-frame
stereo-zero blocks. A sample-level screen found **no internal exact stereo-zero
run ≥128 frames** in any of the five musical intervals, excluding initial
lead-in and the deliberate post-music stop/tail. This supports the targeted
piano repair, not a promise that all dropouts are impossible. Mixed layers can
mask a missing piano block; the filter's source-lock counter remains unresolved.

All five Mac WAV hashes match Pi. Numerical analysis found no nonfinite or
full-scale samples, negligible DC and mono-sum loss about 1.34–1.60 dB. The
audio was not normalized or repaired. This is not a full null comparison,
subjective listening approval, upstream-clipping proof or analogue qualification.

Filter automation maximum send lateness was 2.158 ms (previous 2.198).
Shimmer's was **69.507 ms** (previous 16.645): three serialized commands at 24 s
had acknowledgement intervals 15.240, 53.684 and 4.645 ms. That is control-path
timing evidence, not controller-to-audio latency. Investigate it alongside
render CPU/wall-time and lock attribution before further performance claims.

### Restoration and next boundary

The candidate is **saved/published but not promoted to the normal stage service**.
The isolated unit stopped. Normal stage checkout stays clean at `ce15cfb`,
app/DSP source `483d953`; service active since **19:37:04 CDT**, PID 151143,
invocation `8dc33d08129f4b678d3572e0ff39e9e3`, zero restarts. HTTP stage identity
verified from Pi/Mac; WS native/audio/control/UI healthy, saved Fluid/master 1
restored. Production state SHA256 remains
`ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.
Isolated manifest restoration exactly matches its copied original state.
Post-test temperature 69.6°C, throttle flags 0; Peavey/Yamaha still absent.

Resolve/localize the remaining source miss and timing/control outliers; then
use the actual Peavey/Yamaha rig for off-stage A/B, latency, recovery and
service-length rehearsal. The preserved stage build is a fallback, not a newly
qualified release. No Pi5/Mac branch, dependency, buffer setting or saved sound
was changed. No capture/repair job remains running.

Test setup exception retained: the first disposable launch used Type=notify
incorrectly. The engine ran but intentionally did not notify in isolated mode;
systemd stopped it after 120 s, with clean native shutdown and no MIDI captures.
The successful recording invocation used Type=exec as required above.
