# Stave native Pi4 — analog editor and recording plan

**Current execution plan:** [UI_FINISH_PLAN.md](UI_FINISH_PLAN.md). The player
rejected preparation-mode language and considers paired full ADSR diagrams and
controls essential. This document remains the detailed source/screenshot ledger;
its original broad sequence and mode proposals are superseded by that plan.

September 17, 2026. This began as a planning ledger against `83fa36c`. The
focused, browser-only first pass is now deployed as `46a8aadc…cc379c`: Stage
faders are unchanged, and Sound has OSC / Piano / Space / Delay tabs. OSC has
paired wave panels plus *shared* attack/release and resonance; the displayed
knobs do not add DSP or protocol work. The Pi service, binary, routes, saved
controls and 48 kHz / 512-frame profile were deliberately left untouched.

## Current scope decision

Do not port the legacy application wholesale. The user explicitly chose the
accepted responsive engine over feature parity. This document is therefore a
menu-by-menu **ledger**, not a backlog to implement all at once:

- Keep and refine browser presentation for acknowledged controls already in the
  running protocol, with relative pickup and no added audio work.
- Treat every missing legacy control as a separate engine feature: its protocol,
  state migration, transition/click policy, CPU cost and Pi4 listening test must
  be specified before it is drawn.
- Keep record-to-pad separate. It is an important missing workflow, but it is not
  silently coupled to the UI polish pass or represented as ready while its live
  native handoff is absent.

The immediate next task is player feedback on the deployed controls/layout, not
an engine rebuild. Future DSP work begins only when a musical need justifies its
risks to the accepted latency and sound.

## Direction

Keep the six-fader Stage surface. Build a focused **Sound** editor with the old
paired oscillator panels, rotary controls, envelope drawings, green/orange
outlines and colored pad keys. A group of generic sliders is not the final sound
editor. Knobs and diagrams run in the browser and do not require changing the
audio algorithms. Their control semantics still need engine validation.

Navigation today is **Stage / Sound / System**. Sound currently contains **OSC /
Piano / Space / Delay**. Record, Motion and Output are proposals only, not
navigation promises. On iPad, show OSC1 and OSC2 side by side; on phones,
use clearly labeled OSC1/OSC2 tabs with a visible link state. Avoid a modal that
hides Stage status or traps a phone in a wide, clipped layout. Sound sections
can use a tabbed panel like the legacy menu without duplicating its entire UI.

Maintain a persistent, compact connection/output indication and access to mute.
Distinguish Release notes (tails may continue), Release pad, Freeze, and Mute
output. Do not label the current release action STOP/Panic: its behavior is not
the old all-audio STOP. A future all-sound emergency action needs its own tested
native command and deliberate presentation.

## Screenshot-by-screenshot decisions

“Native present” means source support exists; it does not mean the running
protocol exposes it or that extreme live combinations are qualified.

| Screenshot | Intent to preserve | Native status and proposed simplification |
| --- | --- | --- |
| 1: original Stage | Large tactile faders, colored sounds/pads, quick performance actions | Current six-fader layout is accepted. Keep OSC2 visible; omit ALT switching between hidden layers. Keep favorites optional; defer macro row and multi-bank navigation. |
| 2: later Stage | Piano/synth blend, pad rise/fade, compact status | Keep independent piano brightness and pad shaping. Add layer on/off only with engine-owned ramped mute and remembered level; do not implement it as a browser-only memory of a previous fader. |
| 3: OSC menu | Two oscillator panels, waveform/pan, ADSR knobs + drawing, LINK, filter/send controls, detune/width | Separate native envelopes, pan, octaves, detune/spread, per-OSC reverb sends and shared-filter participation exist. Live API only exposes waveforms and shared attack/release today. Restore full ADSR and pan first; put per-OSC sends/filter participation in Advanced. Omit duplicate trim and arbitrary routing switches initially. Fixed three-copy unison stays fixed; detune/spread are distinct from a voice-count selector. |
| 4: Reverb | Named spaces, decay/predelay, darkening, shimmer, freeze, musical character | All seven reverb types and detailed controls exist natively; only type/shimmer mix/freeze are exposed. Keep one shared ambience with clear source sends. Decay, predelay, low/high cut and damping are first-line knobs. Put noise, shimmer feedback and cloud detail in Advanced. SPACE (master width), filter resonance and piano sympathetic resonance must not share ambiguous labels. Sympathetic resonance is not connected in the native instrument. |
| 5: LFO | Visible waveform, destination, rate/depth, sync, second modulator | Two native LFOs and routing already exist but are not live controls. Start with one clearly presented LFO and expandable LFO2. Keep shape, rate, depth, destination and musical sync. Put phase/spread, retrigger, smoothing and per-OSC reception in Advanced. Defer poly modes and bus sidechain modulation until their practical need and cost are demonstrated. |
| 6: Piano | Warm/dark control, purposeful voicing, room/send, EQ graph and useful dynamics | Brightness, room and reverb send work now. Four-band EQ, compressor and velocity shaping exist below the live protocol. Add a few documented voicing recipes and expandable EQ/dynamics. Preserve the accepted saved sound as its own baseline. Remove the ambiguous PERFECT button. Defer soundfont swapping and organ integration. |
| 7: Record | Record a sound, save its settings, assign it to a concert-key pad, mellow/rise | Essential unfinished workflow. Capture transport and prepared playback exist; live recorder attachment, WAV worker, take lifecycle and slot replacement do not. Implement the complete flow below. Empty slots must remain truthful. |
| 8: Global tone | Analog character, bus dynamics, output EQ/low cut | Native master EQ/compressor/drive and filter drift/wobble exist. Expose a restrained Output page; place detailed compressor settings in Advanced. Keep current defaults unchanged. Voice drift is not a verified live native control; never relabel detune as voice drift. Defer modulation-driven sidechain variants and Haas timing until mono/transition checks pass. |
| 9: Bank | Save/reuse a loved sound | Keep one native sound library and optional small favorites row. Defer setlists that replace entire preset banks. Import only explicitly mapped legacy parameters with a visible report; never claim full preset equivalence. |
| 10: Global/MIDI | Hardware control, status, transpose, practical preferences | Keep honest device/status controls. MIDI learn, pitch bend and clock following need native input integration, not just switches. Prioritize useful CC mapping after core editor/recording. Do not copy the old low-latency toggle or its latency claim. Keep 512 fixed for this release. |
| 11: Delay | Musical sync, feedback, tone, width, expressive reverse effects | Native delay supports these, but live UI has only mix and feedback. Expose time/free-or-sync, feedback, mix, tone and piano send first. Add width/modulation as Advanced. Reverse/Aurora can be a later creative option after capture/listening and CPU checks. Defer Oblivion's unity-feedback override from normal performance controls. |

## Interaction and parameter contract

- Rotary knobs use vertical relative dragging, the same pickup principle as
  accepted faders. No jump to finger angle. Keyboard arrows and a deliberate
  numeric-entry option provide precision. Frequency/time travel uses a useful
  nonlinear curve; wire values retain Hz/ms/seconds/dB, not arbitrary UI units.
- Double-click reset is not a stage default; place Reset inside the sound editor
  and distinguish a single-control reset from restoring a whole saved sound.
- ADSR diagrams show acknowledged parameters and are schematic. The current
  envelope is a compatibility implementation; do not claim an exact analog curve
  or exact time-to-silence from the drawn release segment.
- LINK is explicit: enabling it does not silently overwrite the other oscillator.
  Subsequent linked edits apply one validated transaction to both. Provide a
  separate, deliberate Copy OSC1 to OSC2 action if wanted.
- Both linked and unlinked parameters persist in a versioned native schema.
  Existing shared attack/release snapshots migrate to equal OSC1/OSC2 values.
  Reverb-type changes apply their recipe before any saved per-type overrides.
  Validate complete batches before mutation; rejected/stale batches change none.
- Display requests as pending until acknowledged. No misleading “saved” status
  while a batch is pending. Another browser's changes must update released knobs.
  Device restart/disconnect cancels old gestures and never replays record/slot actions.
- Audit smoothing per parameter. Fader pickup prevents accidental jumps but does
  not establish click-free waveform changes, time-delay retuning, mute/bypass or
  reverb-type switches. Define each transition policy and test sustained notes.
  Reverb spillover using two engines is not assumed affordable on Pi4.
- Define a compact parameter registry for UI units/ranges, backend identifiers,
  grouping, step/curve, persistence, defaults and transition policy. The current
  64-value snapshot bound cannot simply be exceeded by adding every old control.
  Expand bounds deliberately with size validation and migration tests.
- Do not narrow or replace a saved value merely to fit a friendlier knob range.
  Separate comfortable control travel from backend validity. Keep exact DSP
  resonance units internally; any musician-facing scale must have a defined map.

## Recording and pad slots — required completion

Proposed flow: **choose key → Record pad → play/hold sound → Stop → audition
loop → Save to key**. Also allow an existing eligible take to be assigned from
the recording library. Explicitly show Empty, Recording, Finalizing, Preparing,
Ready and Failed. Replace an occupied slot deliberately and preserve the previous
asset for Undo. “Ready” requires durable file completion AND engine acknowledgment.

1. Connect the tested capture queue to a bounded worker that writes/finalizes a
   WAV and records the acknowledged sound snapshot. Queue overflow, slow/full
   storage and writer errors fail the take without stopping the instrument.
   Handle repeated takes with a defined ownership/lifetime protocol; the existing
   one-take queue cannot be reset while producer and consumer are using it.
2. Distinguish recording taps before implementation. The existing tap is after
   master processing but before output-volume/BTL conversion, and includes a
   sounding bed. Proposed default Pad capture records played piano/oscillators
   with their effects, excluding the existing bed, before global master processing.
   This needs a new explicit tap so pad playback through the master does not
   apply its EQ/compression twice or recursively record the old pad. An optional
   full performance take can use the existing tap. Label the capture mode clearly;
   changing headphone/system volume must not change recording gain.
3. Prepare a steady loop away from the audio callback: editable start/end region,
   bounded crossfade and conservative level treatment. Sustain a root/fifth for
   a harmonically open pad, or record another voicing deliberately. No automatic
   chord/key inference, no silent retuning of empty keys. One file per concert key
   remains independent of keyboard transpose and sustain pedal.
4. Retain native memory limits: **32 MiB decoded per slot / 128 MiB bank**, not
   the legacy screenshot's 192 MB. Account separately for preparation scratch,
   recorder queue and an old asset still sounding during replacement. Current
   decoded storage is stereo float64, so compressed/file size is not the budget.
   Do not auto-boost quiet/noisy takes without a visible level policy.
5. Add a bounded live slot handoff. Prepare files/PCM off the audio thread; apply
   the slot selection at an audio boundary; reclaim retired data off audio only
   after all readers/voices release it. The sealed startup bank and immutable
   session bed mask currently cannot do this. Prefer postponing replacement of
   a currently sounding slot with a clear message over interrupting piano or
   allocating another complete bank. Persist the asset manifest transactionally.
6. Acceptance: record through actual instrument MIDI, replay the new slot without
   restarting piano, hear multiple loop wraps, switch keys with release tails,
   test failed/full storage and rejected oversized assets, replace/undo, and reboot
   to the same files. A take must never be marked complete before WAV finalization.

The missing recorder is an implementation gap, not a failure to press the right
pad button. Recording takes and making a useful loop are separate steps.

## Routing to expose

Use the existing source/FX/master topology, with one obvious control owner:
piano has its own tone/room plus sends; OSC1/OSC2 use the shared synth filter
by default and feed the shared effects; recorded pads carry their baked sound
and keep independent level/mellow/rise/fade before the master.

Expose source sends once in the relevant source panel, with short read-only
summaries elsewhere. Avoid a patch matrix, duplicate wet-gain controls and a
“filter affects everything” default. Keep shared-filter participation as an
advanced exception. Do not silently change the current FX fader into a combined
delay/reverb macro; it presently controls synth reverb mix. A combined atmosphere
control would require an explicit musical decision and auditioned mapping.

## Implementation sequence and completion gates

1. **Control foundation:** parameter registry, versioned snapshot migration,
   atomic linked edits, acknowledged knob widgets and per-control transition
   review. Preserve the accepted patch exactly. No broad parameter expansion yet.
2. **Record-to-pad:** finish capture, WAV/take worker, bounded preparation,
   transactional slot assignment and safe live activation. This is the next
   missing musical workflow, ahead of extra effects and preset machinery.
3. **Analog essentials:** paired OSC panels with full ADSR/link/pan/octave and
   detune/width; piano tone/voicing/EQ; Space decay/predelay/damping/tone and clear
   sends. Existing component support requires protocol, persistence and target
   rebuilds; merely drawing the knobs is insufficient.
4. **Movement:** first LFO, optional second LFO, useful delay time/sync/tone and
   sends. Tempo appears when an active feature uses it. Add MIDI CC learning after
   the parameter contract is stable; clock follow/pitch bend need separate MIDI tests.
5. **Finishing:** restrained Output controls, optional favorites, device selection
   polish, iPad/phone gestures, mono checks, saved-state recovery and player audition.
   Defer organ, arbitrary soundfont/program swaps, macros/scenes, multi-bank
   setlists, sympathetic bank, unison-count switching and feedback-stunt modes.
   These are proposals to leave out of this release, not deletion of legacy assets.

At each engine-affecting step, capture/compare the accepted sound and test the
new combinations on Pi4 at 512 frames. Validate audio-buffer deadlines, memory,
control floods and intended transition semantics in bounded isolated tests.
Use actual listening to decide musical results. Browser artwork is inexpensive
for the Pi; activating additional processing still needs measurement.

The later analog-output xrun count is a separate open investigation (3 → 36
before UI deployment; 0 over-budget callbacks). Do not equate a clean source
benchmark, a fixed fader gesture, or current player acceptance with a fully
qualified device path. No eight-hour soak is required by this plan.

## Inspected source anchors

- Live controls: `tools/native_v2_audition.py`,
  `native_v2/include/stave/audition_session.hpp`.
- Native graph/config: `stage_core.hpp`, `stage_instrument.hpp`, `pad_bus.hpp`,
  `stage_motion.hpp`, `shared_effects.hpp`, `piano_chain.hpp`, `stage_master.hpp`.
- Recorder gap: `recording_capture.hpp`; `native_v2/src/audition_jack.cpp` builds
  `AuditionSession session(graph)` without a capture object.
- Pad lifetime: `sampled_bed.hpp`, `bed_assets.hpp`, `native_v2/bed_assets.py`;
  `tools/native_v2_stage.py` prepares the bank at startup.
- Recording location: `stage_output.hpp` and `stage_instrument.hpp`.
- Persistence: `native_v2/control_store.py`; tests/docs of native bed/capture
  milestones are component evidence, not proof of an operational recorder.
