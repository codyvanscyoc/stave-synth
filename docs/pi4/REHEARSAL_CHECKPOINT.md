# Pi4 rehearsal candidate — September 15, 2026

The latest repairs are now **running**, not merely stored in an isolated test
checkout. This is permission to proceed to hardware rehearsal, not a claim of
flawless live operation or measured playing latency.

## Running build and preserved fallback

- Application: `bd6a51760d6fe10e2b61e462a276ee0604465cb0`.
- Normal unit: `stave-synth.service`, new working directory
  `/home/codyvanscyoc/stave-synth-pi4-rehearsal`.
- Only added service override: `95-pi4-continuity-candidate.conf`.
  It selects that directory and disables detailed diagnostics. Existing base
  unit, 90 override, Faust flags and memory drop-in are retained.
- Immediate fallback: preserved clean `ce15cfb` checkout
  `/home/codyvanscyoc/stave-synth-pi4-stage`. Original `stave-synth` is also intact.
- Verified PID 374463, invocation `c09b79d7548f4acc87bc81b92d7205fc`, zero restarts.
  Recheck these before any later intervention; they are a snapshot, not a handle
  to reuse blindly. Later documentation-only commits do not change app code.

Native profile remains 48 kHz, 512 frames, six ring slots, unchanged voices,
unison and effect quality. Fifteen native hashes match TARGET_BUILD.md; organ
uses the previously parity-verified Leslie STOP build:
`d7b1898096c0c9a0ec5d5e53e39bf85187ac2253fb95802e65bec7ff9b1d7455`.
No algebra/vector/silent-voice experimental organ library is installed.
Process maps confirm native libraries loaded from the new rehearsal directory.

## What was actually verified

- Full 546 Python tests on Mac and Pi, zero skips; Node UI/syntax on Mac.
- Native piano release comparison: 12 cases with sample-exact tails; details
  and full-app recordings in [PIANO_RELEASE_BOUNDS.md](PIANO_RELEASE_BOUNDS.md).
- Repeated ten-minute full-core musical run: zero buffer/source/event failures,
  finite/nonzero musical output, no clipping or throttling. Strict FAIL retains
  25 musical over-budget cycles and one later cycle. Post-ready RSS sampled
  312220→313116 KiB, maximum 313236 KiB; temperature 68.166–70.114 C, no swap.
  These short observations do not establish long-duration memory stability.
- Read-only candidate preflight: 88 passes, eight warnings, zero failures.
  Warnings include absent physical devices/preferred output, absent memory
  controller, pre-deployment checkout difference and static-vs-runtime limits.
- Corrected normal-service startup, actual rollback to ce15cfb, and corrected
  restart all reached verified HTTP/WS/native/audio/control/UI readiness.
- Final 120-second corrected normal-service idle observation: callback count
  71→11316; startup-inclusive underruns stayed 7→7, xruns 0→0; zero new
  over-budget cycles. It was idle, not a physical performance test.
- Normal current-state bytes remained unchanged throughout:
  `ed8566e5ef128f6546dfdb1182f0271b08ada0f4aecbb066c0154495ab58cb3e`.

## Backup and rollback scope

Before activation, the stopped normal service's settings, data folders, base
unit and all drop-ins were archived, and the archive hash verified on Mac/Pi:
`16a91aaccdecdb8e1e6a43c0c21231297f536cb996d91dd26e1489916d8ef93d`.
This small snapshot retains the SoundFont symlink; the actual SoundFont and
venv are in the previously verified baseline backup. No SD-image recovery is
claimed. No test pad or private preset bank was installed into normal data.

In an authorized off-stage window, rollback means stop the normal service,
move **only the owned 95 override** into the private backup directory, reload
the user manager and restart. The retained 90 override then selects ce15cfb.
Verify file hash/ownership and destination first. Do not delete all drop-ins,
reset a repository or overwrite user settings. This exact software path was
exercised during activation. Old capture wrappers still assume ce15cfb is
active and must be adapted before reuse.

## Remaining boundaries

The player declined the eight-hour soak for this release effort. It is not
scheduled, required from the player, or recorded as passed. Use bounded
automated tests and actual rehearsal; retain long-run reliability uncertainty.

Heavy organ computation remains approximately 9.9 ms per 10.67 ms block.
Broader scheduling/headroom optimization is unfinished. Existing envelope,
selective-bus and engine-spillover design work remains outside the pre-show
tonal freeze, not silently completed. Smaller buffers have not been qualified.

Next: connect the actual compatible USB keyboard/interface and verify route,
sustain, busiest layered patch, STOP, recorded bed, Safari reconnect and
physical playing latency. See [the hands-on sequence](TEST_TONIGHT.md).
Open control on trusted private Wi-Fi remains the chosen policy.

## Player usability follow-up — September 15 rehearsal

The player initially could not find OSC2's fader and associated options, then
found them through the alternate button. The player accepts that interaction
but requests better discoverability later. This is a usability finding, not
evidence of a missing oscillator or failed audio: the player confirmed OSC2
produces sound. In a later UI review, make alternate-function access and the
currently selected oscillator clearer, especially on phones, while preserving
the familiar five-fader layout. No UI change was made during this rehearsal.

## Yamaha hardware rehearsal — September 15 morning

The player reports good sound and no perceptible playing delay after output
gain correction. This is subjective playing feedback, not measured analogue
latency. The ART USB DI Project Series remains absent from the Pi's USB/ALSA
inventory after the player's port/cable changes; its power light is on. Cause
remains unresolved, and generic-interface qualification is not passed.

Yamaha MX provides both MIDI input and the selected stereo USB audio output.
Its PipeWire output was displayed as 40%, with actual soft/channel multipliers
0.063997 (approximately -24 dB). Stave master and piano faders were already 1.
The player set Yamaha DAW Level to 127, then authorized raising the Pi output.
The verified Yamaha node was ramped to 100% and read back at 1.00; the player
confirmed sufficient gain. No DSP gain/code change was made. Reconnect/reboot
persistence of this output level is not yet verified. Follow-up: expose a
separate interface-output level and attenuation warning, with per-device recall
and a safe policy for newly connected outputs; do not silently force all
unknown interfaces to maximum during performance.

A read-only 600.01-second monitor subsequently completed with 21 resource and
counter snapshots, plus existing meter broadcasts. Source was clean f600c7e;
normal PID 1099 remained active with zero automatic restarts. No application
settings, routing or services were changed during monitoring. MIDI note count
increased 252, but several intervals had no new attacks: this was mixed player
activity and idle/tail observation, not ten minutes of continuous or worst-case
playing. Final check was at 09:17 CDT.

- Bridge underruns 9 -> 9; JACK xruns 0 -> 0. The pre-existing nine events are
  not explained by this later observation and must not be erased from history.
- Render count +56,240; over-budget count 100 -> 129 (29 new, about 0.052%).
  Window mean render duration 6.053 ms; block budget 10.667 ms. Buffer continuity
  was clean, but the strict zero-over-budget timing gate was not passed.
- MIDI drops/recoveries, piano native errors/render-lock misses/overflows,
  invalid/missed samples, rejected writes and control drops stayed zero.
  Piano enqueued/applied events both 1,628 -> 1,974; unmatched note-offs stayed
  at one. No live counter was reset by the monitor.
- Process RSS 312,928 -> 313,252 KiB (+324 KiB), zero process swap. Minimum
  system available memory 1,198,096 KiB. Sampled temperatures 67.192–69.627 C;
  end-of-monitor throttled=0x0.
- Mean process CPU about 62.57% of one core; highest approximately 30-second
  interval 69.06%. Neither figure is instantaneous render-thread headroom or
  a percentage of the whole four-core CPU.
- Existing meter peak maximum 0.14147. This was not a recorded waveform or
  an upstream-clipping, headphone/PA-level or analogue-latency measurement.

No Claude/Codex/Node/compiler/test processes matched the process-name inventory
before or after monitoring. Separate enabled fluidsynth.service PID 1082 was
running, about 174,832 KiB RSS at the initial check. Its necessity/ownership
must be checked before disabling it; it was deliberately left untouched while
the player rehearsed. Merely leaving the Mac terminal open does not imply a
coding process is running on the Pi.

Evidence summary, baseline counters and final health are saved privately on
the Mac as `player-monitor-20260915.json` in the rehearsal deployment backup
directory. The top-bar xrun badge sums bridge underruns and JACK xruns, not
render-over-budget events; improve their presentation without hiding faults.

### FluidSynth ownership clarification — same-morning read-only follow-up

The earlier ENGINEERING_VALIDATION system-profile finding already recorded
the separate FluidSynth process and explicitly deferred removal pending
ownership checks. This was an open finding, not a new discovery that Stave
uses FluidSynth or evidence that its piano engine should be removed.

Stave's `fluidsynth_player.py` creates its own `fluidsynth.Synth` object and
pulls samples with `self.fs.get_samples`; `jack_engine.py` mixes the returned
piano blocks into Stave's output. Live PID 1099 maps libfluidsynth.so.3.3.4.
This in-process library is required and must be retained.

Separate PID 1082 belongs to the packaged `/usr/lib/systemd/user/fluidsynth.service`
(package ownership confirmed), enabled via default.target. Its command loads
`/usr/share/sounds/sf3/default-GM.sf3`. Stave's unit has no dependency on that
service; no repository references launching/controlling it were found, and the
current JACK graph shows Stave's stereo outputs connected directly to Yamaha,
not through the daemon. The daemon exposes a separate MIDI destination via the
MIDI bridges, but no source-to-that-destination JACK connection was shown.

Conclusion: the daemon is not part of the inspected Stave piano/audio path and
is a candidate for reclaiming roughly 171 MiB resident memory. Its CPU use was
small in the earlier process snapshot. No stop, disable, uninstall, or package
removal was performed: validate a reversible service-only stop and unchanged
piano/MIDI/routing in an authorized maintenance window before making a boot
policy change. Do not promise recovery of exactly its RSS, since shared pages
and caches affect available-memory changes.

## September 16 — player requests quicker piano response

After longer playing, the player reports piano latency is just tolerable and
wants quicker response. This supersedes treating the earlier "no perceptible
delay" feedback as final acceptance. No buffer/rate/routing/service changes
were made during this read-only diagnosis.

Fresh SSH inspection after a new boot (about 41 minutes uptime) found Yamaha
MX output at 100%, normal rehearsal checkout, PID 1099 and zero restarts.
The reused PID number is not evidence of the same invocation as September 15.
Stave reports low_latency_mode=true, 48,000 Hz, 512 frames, six ring slots.
Twenty-five debug samples at roughly 0.2-second intervals found ring fill one
block four times, two blocks twenty times and three blocks once. Each block
contains 10.667 ms; the common two-block occupancy contains 21.333 ms of
already-rendered audio. This is not a key-to-analogue latency measurement.
During these samples MIDI note count advanced 2120 -> 2158, bridge underruns
stayed 6, and JACK xruns stayed zero. Cumulative render metrics included 114
over-budget cycles at the initial snapshot; no reset or strict timing pass.

Actual Yamaha hardware now enumerates as ALSA card1, PipeWire output node79
(IDs must be resolved anew before use). `/proc/asound/card1/stream0` advertises
only 44,100 Hz, stereo 24-bit playback/capture. Hardware hw_params confirms
44,100 Hz, period_size256, buffer_size32768, while PipeWire graph metadata is
48,000 Hz/512. Rate conversion is therefore in the path, not proof of a
misconfigured Yamaha or permission to force unsupported 48 kHz on it.

Thirty read-only ALSA playback status samples at 0.1-second intervals report
delay780–1258 frames, or 17.687–28.526 ms at the hardware rate. This is queued
output delay reported by the driver, NOT the 32768-frame capacity and NOT
measured MIDI-to-sound latency. The first probe failed parsing padded field
names; trimming names fixed the reader, with no runtime mutation.

Source also has a 1.5-ms master limiter lookahead. These findings support
investigating pipeline buffering as a cause of playing delay, but must not be
summed into a claimed calibrated end-to-end measurement. Next clarify whether
the player monitors via Yamaha headphones, PA/speakers or another path; then
plan a reversible, authorized lower-latency A/B with underrun/headroom checks
and actual timing measurement where possible. Do not change the running graph
quantum casually: the engine guards against incompatible live graph changes.

The player confirms monitoring through a mixer/PA, not directly through Yamaha
headphones. Compare those monitoring paths before attributing all perceived
delay to Stave or its interface; mixer processing and acoustic travel remain
unmeasured. A 48-kHz-capable external interface could avoid this Yamaha's
44.1-kHz conversion path, but is not automatically faster and would retain
Stave's current render-ahead buffering. ART detection remains unresolved.

The player also reports audible pops/clicks when toggling oscillators off/on
and explicitly accepts a non-instant transition to avoid extra CPU burden.
Static inspection: UI buttons set oscillator fader targets to zero/restored
level; Python applies block-rate ~5-ms blend smoothing plus an exact-zero
snap/skip policy. Faust osc_bank already applies si.smoo to both blends, so
do not misdiagnose this as universally absent smoothing. Investigate interaction
between native smoothing tails and render/routing/Haas skip gates, as well as
possible underruns at the toggle, with captured reproduction on the actual
native profile. Candidate behavior: short audio-thread-controlled mute/unmute
ramp, reversible mid-ramp, preserving effect tails and only suspending safe
work after silence. Player accepted a short transition, not extra latency on
every piano note. No fix, deployment, restart or buffer change performed yet.

### Authorized output-buffer trial — September 16, 10:33 CDT

The player clarified the delay was subtle, then confirmed muted/off-stage
and authorized changes. Started with device-side padding instead of reducing
Stave's render safety margin. No application/native source changes.

Temporary Yamaha node Props api.alsa.period-size=128 (original0/automatic).
After stopping Stave, suspending the verified Yamaha node and starting Stave,
hardware period/headroom became64/64 (previous256/256). Hardware stays44100Hz,
24-bitstereo; graph stays48000Hz/512, Stave six slots/refill3. Setting the live
property alone did not reopen the device. First Suspend invocation lacked its
required JSON argument and failed; corrected invocation with '{}' succeeded.
Two intentional Stave stop/start cycles occurred; zero automatic restarts.

Readiness run120.01seconds: bridge underruns5->5, xruns0->0, piano misses0->0,
MIDI drops0->0, callbacks+11246, renders+11245, late renders8->23 (+15).
MIDI note count0->300, with notes only after roughly60seconds; not a continuous
full-load test. Native/UI/audio health remained good. The initial home-made
monitor failed after not draining broadcasts; the corrected saved reader uses
the existing CommandSocket and completed. Do not mistake that probe failure
for a synth failure or hide it from the evidence record.

Driver delay sampled30times before:780–1258frames, mean22.9667ms. Trial1185
samples:510–1086frames (11.565–24.626ms), mean18.8606ms. This suggests about4ms
less device-side buffering, not a calibrated key-to-sound improvement or a
matched-load A/B result. Stave's own render-ahead is unchanged. Await player
feel comparison and playing checks before keeping/persisting this option.

Active PID28914, invocation71fec46c941b4ca6b050f018a7d4fba6, Yamaha stereo links
verified, output volume1.00, throttled0x0. State SHA256 unchanged across trial:
`3a60d840e99586f10ed1ef78a11088abb05d72d272ecf64537a1c7d24cd7f12a`.
Monitor finished; no diagnostic loop left running. No click repair yet.

Private backup on Pi: `stave-synth-pi4-backups/latency-20260916.bDF3jl`;
Mac: `Documents/stave-synth-pi4-backups/latency-20260916.jH9NwW`.
Runtime archive SHA25699980878c20a54d99742053091e80869601365bd25a7dd4c87145acaa4155b09
verified both sides. Mac INDEX includes scope, temporary-setting caveat and
rollback: re-resolve Yamaha identity, set period-size0, stopped-Stave node
Suspend '{}', restart and verify original period256 plus route/health.
Never reuse numeric node79/card1 without identity checks. No new service
drop-in or permanent WirePlumber rule was installed;95/90 remain untouched.

Player comparison result: "It feels just right now." The player accepts the
trial's response; further latency reduction is not requested. Preserve this
target and test busiest layered playing before a separate, reversible
Yamaha-specific persistence change. The accepted runtime setting is still
temporary, with no new code/config deployment from this feedback alone.

### Persistent Yamaha profile — September 16, after player approval

The player explicitly requested keeping the accepted response permanently.
Installed the Yamaha-only WirePlumber0.5 rule documented in
[YAMAHA_LATENCY_PROFILE.md](YAMAHA_LATENCY_PROFILE.md). Stopped Stave before
archiving its state/configuration and restarting WirePlumber; restarted Stave
afterward. Rule SHA256 and stopped backup SHA256 are recorded in that document.
The backup was copied to the Mac and its checksum verified. Existing95/90
service overrides, application/native source, voices and graph remain untouched.

Fresh output node51 (formerly79), ALSA card1, hardware period64/headroom64,
44100Hz24-bitstereo; verified Stave L/R links and output volume1.00. Service
PID33382, invocation53a9b6f0697e48879705dfac04bfb9ee, active/zero restarts.
State SHA256 remains3a60d840e99586f10ed1ef78a11088abb05d72d272ecf64537a1c7d24cd7f12a.
Resolve numeric IDs again before future operations.

Read-only60.09-second idle observation: underruns6->6, xruns0->0,
late renders9->9, piano misses0->0, MIDI drops0->0, notes0->0;
callbacks+5628/renders+5627, native/UI/audio healthy. Startup-inclusive
counters were not reset or represented as all-zero. Driver delay593 samples,
576–1084frames, mean830.0877frames (18.823ms at44100Hz), not key-to-sound
latency. This idle check does not replace loaded playing. Monitor completed.

Three new device-free scope tests plus complete Mac offline suite passed:
549 Python tests, zero skips, and Node checks. Actual device recreation by
WirePlumber restart verified persistence; no physical power-cycle/replug was
performed. Rollback must now remove only the owned persistent rule before
the authorized stopped-Stave WirePlumber restart, not merely set a temporary
node parameter. No oscillator-click correction is included.
