# Audio, sampled pads, piano, organ and Faust review

Reviewed 2026-09-14 at `4c64f3305a611462c01faa3ad4233f40042f3dcc`. The audio reviewer read **all 31 assigned files, 11,468 lines**, including the complete synth/piano/organ engines, all 13 Faust wrappers and all 15 tracked files in `faust/`. Root cross-checked critical integration paths and reran the corrected isolated evidence fixture. No production code changes, Pi contact, Faust build or acoustic/performance tests were performed in this pass.

P1 = fix before release of the affected supported workflow. P2 = important correctness/hardening or configuration-dependent issue. Some concurrency findings have severity dependent on measured likelihood/impact. A mock reproduction proves control/ownership behavior, not native sound or a historical stage failure.

## Prioritized findings

### A01 — P1: STOP does not stop recorded pads

`synth_engine.py:2136` `_apply_panic()` never resets/releases `_pad_samples`; later render mixes active players around `:3865-3890`. `main.py:1111` likewise omits sample termination and resets master fade to unity. Thus an active recorded bed survives the emergency-stop transaction. **High confidence, static integration confirmed**, not an acoustic test. Hard-reset every player/filter history on the render owner and cancel competing bed/preset fades. See also core C03.

### A02 — P1: organ restrikes orphan sounding native slots

`faust_organ.py:280-307` allocates another slot when restriking a releasing note and replaces `voices[note]` without retiring the old slot. Render (`:358`) and all-notes-off (`:315`) only visit the replacement dictionary entry. The old native gate can stay nonzero indefinitely. **Reproduced with exact methods and the actual 16-slot configuration:** after all-notes-off and repeated render calls, no tracked voices remain but the old gate remains 1 and its slot is not free. Fix slot ownership/reuse; panic must zero every native slot, not only tracked notes.

### A03 — P1: freeze/capture/panic leaves plate and drone inputs closed

`faust_reverb.py:482-487` closes all backends' input after capture. Panic (`:222-241`) restores FDN control zones but only clears history in plate/drone (`faust_plate.py:90`, `faust_drone.py:90`). Their `freeze_input` controls remain 0, preventing the cleared tanks from filling with new wet sound. **Reproduced with exact wrapper logic and mock native instances.** Reset control state as well as DSP history for every backend. Faust separates state clearing from control reset in its [DSP interface](https://github.com/grame-cncm/faust/blob/master-dev/architecture/faust/dsp/interpreter-dsp.h).

### A04 — P1/P2: organ reaping can delete a freshly retriggered note

`faust_organ.py:358-424` snapshots voices under lock, computes unlocked, then reaps by note key at `:430` without checking object identity. A replacement arriving between snapshot and reap is removed instead of the old voice. **Deterministically reproduced** by triggering the replacement in the mock compute boundary. Python fallback shares this pattern (`organ_engine.py:209-212`, `:283-284`, `:373-377`). Native setters also lack complete serialization against compute. Use render-owned commands or properly scoped synchronization and identity-aware reclamation.

### A05 — P1/P2: piano-room clear can overlap native compute

`fluidsynth_player.py:553` clears the room outside `_lock`; render processes the same room at `:744`, also outside that lock. MIDI/UI panic and instrument changes can therefore clear recursive state while native processing is active. **High-confidence static concurrency finding; audible/target stress reproduction pending.** Queue clearing and state changes to the DSP owner.

### A06 — P1: recorded-pad loading has no Pi4 memory budget and interrupts the whole bank

`synth_engine.py:997` onward decodes/resamples the entire WAV into float64 channel arrays without a duration/decoded-byte budget. A maximum-length 30-minute stereo take at 48 kHz alone requires 1,382,400,000 bytes for its two float64 channels; decoding/resampling, other slots and an old bank increase peak memory further. `load_pad_samples` (`:2550-2580`) prepares fresh players for all slots, then replaces the entire bank, resetting active playback even when saving a different slot. `main.py:830-856` invokes this bank reload after assigning one recording. **Static data-size/lifecycle proof; no OOM test run on the Pi.** Validate limits before allocating, prepare only the changed slot, and define musical hot replacement. This preserves the intentional recorded-bed workflow rather than replacing it with a different synth.

### A07 — P2: missing-soundfont fallback infinitely recurses

`fluidsynth_player.py:462-497` alternates missing Salamander and FluidR3_GM recursively, never reaching available default-GM. `_load_one()` invokes resolution before its exception handler (`:377`). **Reproduced:** RecursionError with both preferred assets absent while a default-GM path is available. LOW_RAM_MODE also leaves Salamander in this fallback chain despite removing it from the small-RAM preset table, potentially selecting a large font. Use a finite, deduplicated, profile-aware resolver and report actual resolved identity.

### A08 — P2: native organ zero fader is not mute

`faust/organ.dsp:137-144` maps zero fader to 0.01 amplitude (−40 dB); `faust_organ.py:237-239` passes zero directly and render (`:330`) has no volume gate. Root verified the JACK caller has no volume gate either. Python fallback explicitly mutes (`organ_engine.py:482`). **Static numeric fact, not an acoustic measurement.** Preserve the desired curve but add a smoothed exact-zero endpoint.

### A09 — P2: decay edits during freeze disagree across backends

`faust_reverb.py:254-278` changes plate/drone feedback while frozen but not FDN's live freeze feedback. Unfreeze (`:447-465`) restores stale FDN `_normal_feedback`, whereas plate/drone use current decay. Panic can reuse stale saved feedback; unfreeze/panic also hardcode early-reflection scale 0.4 instead of restoring hall/room/bloom values (`:231`, `:454`). The fixture's **informative state output**, not an extra asserted acoustic check, showed plate feedback 0.97 during freeze and FDN restoration 0.8 versus the requested decay's calculated ≈0.916199. Separate desired normal settings from freeze overrides.

### A10 — P2: native block-end envelope gates omit short transients

`synth_engine.py:2840-2851` reduces each ADSR block to its final value before native synthesis. A positive transient within the block can therefore send a zero gate. **Exact envelope reproduction:** Python envelope peak 1, final native gate 0. Organ scalar release handling (`faust_organ.py:366-400`) also differs at exactly one block of remaining release. At 512/48 kHz, blocks last 10.667 ms: changing quantum can change envelope behavior, not just transport latency. Validate supported envelopes/quanta and compare matched sound before changing architecture.

### A11 — P2: selective oscillator routing reconstructs a mixture, not isolated sources

`synth_engine.py:3487` onward estimates OSC contributions from already combined output using a block-wide magnitude ratio; FX bypass does likewise around `:3570`. Different pitches remain present in both reconstructed portions, so selective modulation/bypass cannot fully isolate sources. Native polyphonic AMP LFO has no per-OSC receive mask. Reverb's unity-send path copies post-LFO/post-delay output, while nonunity sends use pre-LFO/pre-delay weighted filtering (`:3610` onward), so moving off unity changes routing topology as well as level. **Static routing finding; distinct-frequency signal tests pending.** Preserve real source buses or make the supported control meaning explicit; audition any correction.

### A12 — P2/P3: invalid or tiny samples violate playback assumptions

`synth_engine.py:989-1038` accepts empty/non-finite WAV data. `:1142-1162` indexes empty sample arrays. Only two wrap subtractions are performed, so very short loops leave positions out of range and clip at the end rather than repeating correctly (`:1208`). **Reproduced:** empty-buffer IndexError; an eight-frame sample rendered in a 512-frame block leaves read position494 and repeats a clipped endpoint. Ordinary long-file EOF crossfade is structurally sensible: no universal EOF infinite-loop claim is supported. Validate loaded assets and use arbitrary-length modulo wrapping.

### A13 — P2/P3: sleep and wet-zero bypass pause residual DSP history

`fluidsynth_player.py:602-610` stops rendering after 400 blocks without a tracked key, regardless of remaining sound. That is approximately 4.27 s at 512/48 kHz but 1.07 s at 128 frames. Tails/state may resume on a later note. Piano-room wet-zero (`:739`) and disabled/wet-zero delay (`synth_engine.py:2246`) similarly skip processing without consistently draining/clearing. **Static behavior; material-specific audible reproduction pending.** Define sleep/tail policy using time and actual state, preserving intended freeze behavior.

## Additional risks and qualifications

- **Python fallback sympathetic capacity:** scratch holds 64 entries but note-map growth is not capped (`synth_engine.py:1591`, `:2503`, `:3719`). More than 64 tracked/releasing notes can index past scratch. Native fixed-slot processing avoids this specific path. Static only.
- **Native allocation:** some wrapper constructors do not check NULL allocations before initialization, so memory pressure may crash rather than fall back gracefully. Validate allocation and required backend engagement; do not inject OOM on production.
- **Build identity/deployment:** `faust/build.sh:38`, `:64`, `:124` uses timestamps, not compiler/Faust/options hashes. Documented `--force` exists. Output libraries are written directly (`:47`, `:87`, `:128`); future deployment needs staged artifacts, safe replacement, ABI checks and a controlled service transition. No native builds were performed here.
- **Merged path checks that held up:** owner references are retained (`faust_merged.py:70-77`), and float32 bank → float64 bus contracts match the build flags (`build.sh:106`, `:112`). No demonstrated pointer-lifetime or precision mismatch was found in this bridge.
- **Memory commentary:** `ping_pong.dsp:56` understates the stereo reverse-buffer size; two 3-million float32 channels already need 24 million bytes before implementation rounding. Exact generated/native allocation remains to be inspected/measured.
- **Acoustic qualification remains open:** high-octave oscillator/organ aliasing, FluidSynth int16 pre-chain headroom, clipping, stereo/mono compatibility, filter gestures and long effect transitions need spectral/capture/listening tests. None of these source questions proves currently enjoyed patches sound poor.

## Claims narrowed or rejected

- Missing the Python lock around FluidSynth `sfload` alone does **not** establish native memory corruption: FluidSynth normally supplies public-API thread protection. Dynamic sample loading (`fluidsynth_player.py:318`) and program selection under the render lock (`:962`) still do not promise realtime-safe or instantaneous switching. Measure the actual Pi4 path, where normal programs share FluidR3. See [FluidSynth settings](https://www.fluidsynth.org/api/settings_synth.html).
- A normal one-way reverb switch clears the backend being entered before assigning `type` (`faust_reverb.py:407-423`); it does not necessarily clear the currently computing backend. Rapid overlapping switches remain an ownership concern.
- Python Haas-buffer mutations do not prove a defect on blocks using native pad-bus processing; those buffers are bypassed (`synth_engine.py:2974`).
- Ring capacity, quantum and average CPU are not measured controller-to-analogue latency or deadline reserve.
- Recorded-pad slots are intentionally sampler-only. Missing samples are silent; old comments promising a synthetic fallback are stale, not permission to restore a new source.

## Complete file coverage

Read in full: `stave_synth/synth_engine.py`, `fluidsynth_player.py`, `organ_engine.py`; all 13 wrappers: `faust_bus_comp.py`, `faust_drone.py`, `faust_master_fx.py`, `faust_merged.py`, `faust_organ.py`, `faust_osc_bank.py`, `faust_pad_bus.py`, `faust_piano_chain.py`, `faust_piano_room.py`, `faust_ping_pong.py`, `faust_plate.py`, `faust_reverb.py`, `faust_sympathetic.py`; and all 15 `faust/` files: `build.sh`, `faust_cprelude.h`, `merged_shim.c`, `bus_comp.dsp`, `drone.dsp`, `master_fx.dsp`, `organ.dsp`, `osc_bank.dsp`, `pad_bus.dsp`, `piano_chain.dsp`, `piano_room.dsp`, `ping_pong.dsp`, `plate.dsp`, `reverb.dsp`, `sympathetic.dsp`.

The [coverage ledger](../REVIEW_COVERAGE.md) supplies exact file hashes and line counts. Generated C, third-party internals and deployed ARM binaries were not exhaustively read/audited.

## Executed evidence and remaining tests

The corrected durable [audio fixture](audio_repro.py) passed seven reproduction checks: organ orphan, organ stale reap, panic freeze, tiny sample, empty sample, recursive font fallback and ADSR end gate. The extra freeze-decay output is informative, not an eighth asserted test. Root corrected a draft fixture parenthesis and matched its mock slot count to 16, then reran successfully; no app code was edited. These checks use exact Python definitions and mocked native calls, not native DSP sound or performance.

Still required: compiled-backend tests and binary identity; matched-level signal/spectral comparisons; source-selective modulation/send isolation; full panic/tail/pedal matrix; sample RAM/file bounds and hot replacement; owner-thread/stalled-work tests; buffer-size/rate contracts; worst-patch render/scheduler distributions, real controller-to-output latency, eight-hour soak and stage rehearsal. See the [engineering gates](../ENGINEERING_VALIDATION.md).
