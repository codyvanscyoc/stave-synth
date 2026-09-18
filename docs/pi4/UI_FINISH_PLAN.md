# Stave UI finishing plan

September 17, 2026. Supersedes the proposed mode/workflow language and broad
implementation sequence in ANALOG_EDITOR_PLAN.md. That file remains a useful
source ledger for the eleven original screenshots.

## Product decision

One instrument, one continuous sound state. MIDI supplies notes and hardware
gestures; plugging in a keyboard does not unlock editing. Device-free control
is automatic. Do not add Prepare, Setup, Performance, Live, or Offline modes.
Stage and Sound are views of the same values, not separate patches/workflows.
Save remains the familiar optional sound-memory action, independent of MIDI.
It is not necessary for editing or connecting a keyboard in the current session.

The original screenshots are the visual reference: black/brown background,
green fine outlines, orange filter, white serif S with colored staff lines,
long filled faders, pointer-style knobs, paired oscillator panels and readable
envelope plots. Avoid generic dashboard cards, decorative gauges, tutorial
paragraphs and implementation details on the playing surface.

## 1. Stage and interaction — current delivery

- Reuse the exact original `ui/logo.png` asset.
- Long faders: OSC1, OSC2, piano, filter, FX, master. Keep OSC2 visible instead of
  hiding a live layer behind ALT. Tablet fader travel scales with screen height;
  phone uses two rows of three generous controls. Keep independent piano tone.
- Compact connection status: browser connection, MIDI presence, audio running.
  No preparation banner. Sound controls work without devices automatically.
- Preserve relative pickup: touching a value does not move it. Knobs show a
  pointer, support keyboard input, and use 240px full-travel drags for precision.
- Keep familiar shimmer/freeze, note release and mute actions. Colored recorded
  keys remain beneath the mixer. Defer optional macros/favorites from this pass.
- Normal controls communicate musical names and units. Diagnostics stay in
  System. An error or acknowledged Save may briefly show a message.

## 2. Oscillator instrument panel — implemented, player check next

September17 candidate implemented: eight independent ADSR controls, acknowledged
schematic diagrams, musical time-knob scaling, and native single-command LINK.
Legacy shared AR is expanded on load; explicit independent values take precedence.
Restore applies LINK last so enabling it never rewrites unequal contours.
Local UBSan/native PCM equivalence,604 reviewed regressions, actual browser
mouse/touch/device-free/save checks passed. Pi target guards/build passed and
the idle-only installation preserved saved/current tone. See RESUME_HERE for
hashes and evidence. Real Safari/keyboard transition checks remain open.

Reference screenshot 3. Full ADSR is an essential part of the requested sound
editor, not optional decoration. Finish this before adding more menu categories.

For each oscillator: waveform choice, envelope diagram, Attack / Decay /
Sustain / Release knobs. Show OSC1 and OSC2 side by side on tablets; stack full
panels on phones. Each diagram uses the acknowledged values of its oscillator.
Diagrams are schematic rather than a claim of exact analog envelope timing.

Expose the existing `StageInstrumentConfig.env1/env2` and `EnvelopeConfig`
through the bounded native control queue. Add eight explicit per-OSC controls.
Keep old shared attack/release as migration aliases; do not let old aliases and
new defaults overwrite a player's saved contour. Migration must preserve the
current equal envelopes, including existing decay1500ms/sustain80% defaults.
Use musical time travel and show ms/seconds or percent, while preserving exact
stored values. Startup and missing-device editing use the same parameter schema.

LINK is an explicit editing relationship. Enabling it preserves current values;
subsequent envelope changes apply to both oscillators as one validated native
transaction. Validate/persist the relationship, reject a whole invalid batch,
and reconcile both controls after acknowledgment. Never imitate linking by
racing two browser requests. Waveform linking is not implied by envelope link.

Place shared filter cutoff/resonance in a clearly separate strip. Mirror an
existing control through one state owner; do not create a second independent
FX/cutoff value. Per-OSC pan, octave and detune are secondary additions after
the envelope panel is complete and proven; fixed unison voice count stays fixed.

Acceptance: independent ADSR edits and linked edits, old-sound restoration,
save/reboot, two browsers, device-free edits→device connection, held-note edits,
release/pedal behavior, and no dropped/late controls. Compare unchanged settings
against the accepted PCM path; build and test the control owner on Pi4 with the
same DSP objects and 48k/512 profile. Do not change the envelope algorithm,
voice count, routing graph or render scheduling for this editor.

## 3. Finish the remaining musical menus

September17 candidate now exposes the existing first-line Piano/Reverb/Delay
tone operations without adding DSP stages: piano room size/damping; reverb
decay, pre-delay, low/high cut and damping; delay free time and low/high cut.
Existing room/send, character, shimmer/freeze, feedback and mix remain present.
Paired tone filters reject crossed cutoffs before audio ownership. Real Chrome,
native exact-PCM and Pi target checks pass; `9403664` is installed. Player
listening and sustained-note transition checks remain.
Musical tempo-sync selection, advanced motion, presets and recording stay later
milestones rather than decorative controls.

| Reference | Finished first-release surface | Leave out of this UI pass |
| --- | --- | --- |
| Piano, screenshot 6 | Piano tone/brightness, room, ambience send; then a small clearly named voicing/EQ section using existing piano processing | PERFECT button, organ/soundfont switching, dense compressor controls |
| Reverb, screenshot 4 | Character, decay, pre-delay, tone/damping, source sends, shimmer and freeze | Ambiguous SPACE/RESO/CLOUD macros and arbitrary routing switches |
| Delay, screenshot 11 | Time, feedback, mix, tone; add musical sync when tempo control is connected | Oblivion, reverse/Aurora complexity in the first menu |
| Motion, screenshot 5 | Add a compact rate/depth/destination section only after a native LFO control contract is implemented | Two full matrices and duplicate per-source routing |
| Global, screenshots 8/10 | Connections, audio diagnostics; existing master EQ gain at200/1000/5000Hz (±6dB), existing12dB/oct low cut20–200Hz; essential MIDI preferences only when implemented | Old latency toggle/claims, general bus-processing laboratory; compression remains unexposed/off |
| Bank, screenshots 1/9 | Existing Save; optional small favorites when native presets exist | Macros and replacing entire setlists/banks |
| Recording, screenshot 7 | Complete real capture→take→pad-key assignment as its own backend milestone | Empty record buttons, pretending a saved tone is a recorded sample |

Each newly exposed control must name its existing native owner, persistence
mapping and held-note transition behavior. Source support alone is not evidence
that its live UI integration works. Do not automatically add processing or alter
the loved sound merely because an old screenshot contains a knob.

Global tone candidate uses `MasterConfig.eq/highpass/cutoff` in the existing
master chain, not added processing. All gains default0dB and low cut stays off;
no changes to pre-gain, limiter, compression, DSP sources or48k/512 scheduling.
The existing low-cut enable is a hard topology selection, not a crossfade:
set it between songs. Existing envelope sustain updates can change held levels;
PCM equivalence proves preservation, not inaudibility of every extreme gesture.
Real listening/transition qualification remains required before claiming click-free.

## Completion criteria

The Stage surface is usable by a volunteer without reading technical guidance.
The Sound surface provides recognizable synthesis controls and actual envelope
feedback. Every interactive item has a working state owner; absent hardware does
not lock tone editing. Touch targets and relative pickup work on real iPad/iPhone
Safari. Defaults and saved sound retain the accepted native tone and 512-frame
response. The UI is not declared finished while ADSR is still a shared AR pair
or recording controls have no working backend.
