# Stave Synth — Pi4 stage edition

Status: product specification draft, 2026-09-14. Product direction is confirmed by the player; engineering targets below are proposed and have not passed qualification.

Stave is a headless worship instrument: play an expressive piano, blend two oscillator layers underneath it, and optionally sustain a tonic-and-fifth drone or sampled pad bed. Shape the music continuously with the filter and effects from a MIDI controller or an Apple phone/tablet browser. The instrument must keep playing dependably throughout a service.

## What the player has confirmed

- Stave has already been used and enjoyed on stage. Preserve its musical character and familiar controls.
- Piano playing and live pad blending are the primary workflow. Presets and setlists are secondary.
- A representative demanding patch is piano + OSC1 + OSC2 + effects, sometimes with a separate background pad/drone sounding simultaneously.
- Filter sweeps and effects moving in and out are performance gestures, not merely editing operations.
- “Pad underneath” can mean the oscillators following the played piano notes, or an independent tonic-and-fifth drone, depending on the song.
- The existing 12-key sampled pad player, a held synth/frozen reverb layer, and combinations of these are all part of the intended song-dependent workflow.
- MIDI keyboards vary, mostly Yamaha and Dexibell. Browser controllers are mostly Apple products.
- The USB hub may be Peavey; its exact identity, power arrangement, and audio interface still need to be inventoried.
- This work targets Raspberry Pi 4. Pi5 work will be handled later. Development and GitHub publishing use `pi4-stage-pro`.

## The musical contract

| Part | Role on stage | Required behavior |
| --- | --- | --- |
| Piano | Expressive foreground instrument | Preserve dynamics, sustain, voicings, tuning, and attack; avoid clipping before the master bus. |
| OSC1 and OSC2 | Played synth/pad layers | Independent balance and existing tone controls; smooth filter/effect movement while piano and sustain continue. |
| Pad/drone bed | Optional background independent of played notes | Clear key and playing state; independent level, rise/fade, and stop; support the intended tonic-and-fifth sound. |
| Effects | Live movement and atmosphere | Smooth wet/dry and filter changes; predictable freeze, release, tails, and bypass. |
| Master | Consistent stage output | One authoritative output-gain control, dependable fade and panic, useful level/limiter indication. |

Piano, played pads, and a held bed must coexist within an explicitly tested Pi4 operating profile. A new feature cannot quietly displace the headroom required for this core combination.

The existing organ, electric-piano sounds, splits, macros, recorder, presets, and setlists remain available. Their behavior needs regression coverage; they do not displace the main live mixing workflow or create a requirement to add more synthesis engines.

### Drone behavior needs an explicit source decision

Current `main.py:_handle_drone_key` is sampler-only. It says the former live root-plus-fifth fallback was removed; a key with no WAV returns `no-sample`. A startup comment still claims a fallback exists. The inspected Pi4 pad-sample directory is empty.

The musical requirement includes sampled beds, held/frozen ambience, and an independent tonic-and-fifth sound. Before implementing a new drone path, compare the existing sampled-bed approach with a small dedicated synthesized drone, including their sound, memory, render cost, tuning, and key-change behavior. A sampled root/fifth bed could satisfy that sound requirement; restoring a synth fallback is not automatically the chosen solution. The reverb named DRONE is a separate effect and should not be confused with the bed instrument.

Candidate behavior for review: a deliberate key selection starts the bed; it holds across played chords and sustain changes; a new key changes it with a bounded musical crossfade; a dedicated fade releases it. Panic silences it. Document concert-key versus keyboard-transpose behavior before implementation.

## Performance interface

Keep the existing five-fader locations and learned gestures as the starting point: OSC, PIANO, FILTER, FX, MASTER. Preserve the existing alternate functions until a hands-on review justifies a change. OSC2 and the independent bed need clear level/status access while performing; prototype any extra visibility before rearranging the main surface.

The Stage screen should support a complete service without opening detailed settings: balance piano and oscillators, shape the filter, move effects, start/fade the bed, freeze/release ambience, adjust master, and panic. Current key, layer mute states, active alternate functions, and any pending control update must be unambiguous.

Use three task areas:

- **Stage:** the familiar performance mixer, bed controls, a compact preset area, fade/panic, and health.
- **Edit:** existing oscillator, envelope, filter, piano, organ, reverb, motion, split, and macro detail.
- **System:** output selection, MIDI setup, connection/recovery information, storage and maintenance.

This is a navigation proposal, not a mandate for a framework rewrite. The existing plain HTML/CSS/JavaScript remains a suitable implementation starting point.

For iPad landscape, retain a complete mixer. On phone portrait, prioritize the same core musical actions with deliberate scrolling or grouping instead of shrinking every touch target. Full editing stays accessible. Test real Safari touch behavior, screen locking, orientation changes, browser restoration, and two clients. Retain compatibility with the legacy 800x480 layout where practical, without installing a GUI on the headless Pi4.

Primary touch controls should target at least 44 CSS pixels with enough separation to avoid accidental panic, mute, or destructive edits. Show value and active mode in text as well as color. Keep destructive preset/recording actions in Edit/System with deliberate confirmation; normal musical gestures remain immediate.

Presets remain quick starting points and optional performance changes. Do not reorganize the instrument around song/scene navigation unless the player's workflow changes. Existing slot names, MIDI mapping, and saved data need compatibility.

### Honest health and recovery

Show four distinct facts: browser control connection; audio-engine progress; connected MIDI device/activity; and the selected physical output/route. Meter traffic alone does not prove that the DAC is connected or audible.

A lost tablet connection must leave MIDI playing and existing audio unchanged. Reconnection obtains authoritative state and marks stale values clearly. Never replay an old panic, key trigger, toggle, or preset change unexpectedly after reconnecting. For absolute fader edits, define pending/acknowledged behavior and avoid jumps when a second controller moves the same parameter. Review MIDI soft-takeover behavior before changing existing mappings.

If the UI worker fails while audio is healthy, attempt bounded recovery of the UI component first. Restarting the entire instrument solely because a browser path failed could interrupt a service. Initial readiness must truthfully describe missing components rather than claiming full readiness.

When a physical output disappears, keep control/status available and recover the chosen device when it returns. Route only to the selected output or an explicitly configured fallback; never silently redirect a stage instrument to an arbitrary newly visible sink. Treat operating-system device enumeration failure separately from app routing failure.

## Sound quality and transitions

Before DSP changes, capture favorite patches and repeatable MIDI performances from the current build. Compare changes at matched levels through headphones and the intended PA/monitor path. Include stereo and mono checks, especially for wide/Haas/unison effects.

Reference passages should include quiet piano, hard sustained chords, repeated notes, pedal overlap, piano+both oscillators, slow and rapid filter sweeps, effects entering/leaving, shimmer/freeze, and the independent bed. Inspect finite samples, peak level, clipping before each major gain stage, release continuity, voice stealing, and unwanted noise while also listening for musical differences.

Preserve current voicings and favorite preset behavior. Any proposed tonal change is an explicit, auditioned change. Limiting the master cannot repair upstream clipping. A brighter or louder output is not automatically a better piano sound.

Normal fader/filter/effect-mix movement should preserve ongoing notes and tails without clicks. Replacing a reverb algorithm, soundfont/program, or entire preset is a different operation: define and test its tail/loading policy. The existing 800 ms parameter morph does not establish seamless spillover between different engines. A second effect engine for crossfading must have measured temporary CPU headroom before being enabled on Pi4.

## Pi4 performance contract

The inspected target is a Raspberry Pi 4 Model B Rev 1.5 with approximately 2 GB RAM, using a headless 48 kHz PipeWire-JACK/FluidSynth/Faust pipeline. Specify CPU generation and RAM independently; a Pi4 with more RAM still has Pi4 CPU limits.

Report two separate experiences: MIDI key-to-analogue-output latency, and browser gesture-to-engine response. Also separate keyboard scanning/USB transport, engine queueing, interface buffering, and the instrument's intentional attack envelope. Browser controls are outside the note transport path when the keyboard is connected directly to the Pi.

Current 512-frame blocks at 48 kHz are 10.67 ms each. The three-block refill threshold represents 32 ms of queued sample duration; it is not a measured total latency or a guaranteed scheduling margin. Some labels assume 256-frame blocks and need correction with the implementation work.

First measure the existing sound/profile. Fix avoidable stalls and correctness issues, then compare 512, 256, and potentially 128 frames with identical patches and performances. Smaller blocks can increase overhead. Preserve musical quality while reducing measured bottlenecks; do not silently lower unison, polyphony, or effect quality during a performance.

Provisional engineering goals:

- Seek 30–40% render-period reserve in the demanding supported patch after warm-up, and report p95/p99/p99.9/max durations, scheduling gaps, lock waits, and minimum queue fill. Average system CPU alone cannot qualify the instrument.
- Aim toward sub-15 ms controller-to-line-output latency if this hardware and sound profile can sustain it. This is an aspiration pending external measurement, not a promised specification.
- No post-ready bridge underruns, dropped note events, unbounded queues, active swapping, or thermal throttling in the eight-hour representative qualification run.
- Bounded voice stealing and stable memory after warm-up, including bed playback, long tails, browser reconnects, and repeated sound changes.
- Choose supported stage profiles from measured latency/headroom results and player audition. During a set, the selected profile stays stable; buffer/rate changes require a controlled transition.

## Release sequence

1. Preserve source, current state, native builds, and service configuration; establish the Pi4-only branch. Record the limits of those backups.
2. Review this musical/workflow specification, capture favorite sounds, identify the actual audio/USB path, and settle bed/key/tail semantics.
3. Add an isolated test instance and low-overhead timing/health measurements. Confirm or reproduce review findings before assigning fixes.
4. Repair input validation, state saving, truthful connectivity, and bounded UI/device recovery in small tested changes.
5. Address GC/allocation stalls, native DSP state ownership, sound preparation, and JACK size/rate handling while comparing sound against the baseline.
6. Optimize the measured Pi4 bottlenecks and select the lowest qualified latency profile.
7. Refine the Stage UI around live blending and Apple-device use, preserving the current musical controls.
8. Run the defined fault, audio, latency, and soak tests, rehearse on the actual stage setup, and prepare a tested rollback/recovery image before release.

See [engineering validation](ENGINEERING_VALIDATION.md) for the test contract and [baseline](BASELINE.md) for what has actually been preserved and observed.
