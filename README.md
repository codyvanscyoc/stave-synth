# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 — worship ambient pad with piano layer.

## Install

There are two install modes depending on how you plan to use the synth:

### Casual / "just want to try it" — no auto-start

```bash
git clone https://github.com/codyvanscyoc/stave-synth.git
cd stave-synth
./install.sh --no-autostart
```

When you want to play, launch it manually from the repo:

```bash
./stave-synth.sh
```

The UI opens at `http://localhost:8080` — or `http://<pi-ip>:8080` from any other device on the network. Stop it with Ctrl-C in the terminal (or `pkill -f stave_synth.main`).

### Live performance / kiosk — auto-start on boot

```bash
git clone https://github.com/codyvanscyoc/stave-synth.git
cd stave-synth
./install.sh
```

Reboot and the synth starts automatically. Plug in a USB MIDI keyboard and play.

### Hardware you need

- **Raspberry Pi 5** (Bookworm or Trixie, 64-bit). 4 GB works; 8 GB is comfier.
- **USB MIDI keyboard** (Akai MPKmini, Launchkey, any class-compliant USB MIDI controller).
- **Audio out** — any USB audio interface, or the Pi's HDMI / Bluetooth. Built-in analog is not recommended.

### What install.sh does

- Installs JACK2, PipeWire, FluidSynth, Python deps, WebKit (auto-detects Bookworm vs Trixie), Faust compiler
- Sets up real-time audio permissions + audio group
- Locks CPU governor to performance (prevents audio stutter)
- Disables screen blanking + USB autosuspend (prevents dropouts)
- Copies the bundled TimGM6mb soundfont for piano (offline-proof)
- Builds the C audio bridge + Faust DSP modules
- Creates a systemd user service that auto-starts on boot *(skipped with `--no-autostart`)*
- Prints a summary of detected audio/MIDI devices at the end

## First 60 seconds

1. **Open the UI.** After install, browse to `http://<your-pi-ip>:8080` from any device on your network (phone, tablet, laptop). Or open a browser on the Pi itself.
2. **Plug in a USB MIDI keyboard.** The synth auto-connects; the `MIDI` indicator in the top bar flashes on each note.
3. **Play a note.** You should hear piano + a soft pad underneath.
4. **Try the faders.** OSC 1 (pad volume), PIANO (piano volume), FILTER (pad lowpass cutoff), FX (reverb mix), MASTER (output).
5. **Tap SHIM.** Octave-up shimmer sines bloom in the reverb. Tap again to turn off.

## Knobs & what they do

### Faders (tap ALT for secondary function)

| Fader | Normal | ALT |
|-------|--------|-----|
| **OSC 1** | OSC1 volume, octave ± buttons | OSC2 volume, ± shifts OSC2 |
| **PIANO / ORGAN** | Piano/organ volume | Tone (high cut) → Compressor or Leslie depth |
| **FILTER** | Lowpass cutoff (log-scale sweep) | Low cut (highpass) |
| **FX** | Reverb dry/wet | Shimmer volume |
| **MASTER** | Master output | — |

### Buttons under faders

| Button | Where | Function |
|--------|-------|----------|
| **ALT** | Under each fader | Toggles the ALT mode listed above |
| **± / octave display** | Under OSC 1, PIANO | Shift that instrument by octaves |
| **RESO** | Under FILTER | Sympathetic resonance — piano notes bloom into the pad |
| **+12** | Under FILTER (only when SHIM is on) | Shimmer pitches up another octave (+24 total) |
| **FADE** | Under MASTER | Musical 5-second fade out/in, preserves current master position |

### Top bar

| Button | Function |
|--------|----------|
| **OSC1 / OSC2** | Mute/unmute each oscillator |
| **PIANO** | Cycle: piano → organ → off |
| **SHIM** | Shimmer on/off |
| **FRZ** | Freeze reverb tail (appears when shimmer is on) |
| **DRONE** | Sustained root + fifth one octave below |
| **SAT** | Saturation / asymmetric drive |
| **STOP** | Panic — kills all notes, flushes reverb, resets fade |
| **T: −/+** | Transpose all MIDI input in semitones |
| **MENU** | Settings modal (detailed tuning) |
| **CONN** | Tap to switch audio output (USB, Bluetooth, HDMI) |

### Reverb tab knobs

- **SPACE** — Larger/smaller reverb size
- **SHIMMER** — Mix level of the shimmer sines
- **RESO** — Sympathetic resonance amount (how much piano bleeds into pad)
- **CLOUD** — Wet level of pre-reverb multi-tap bouncing delay on shimmer

## Troubleshooting

**I can't hear anything.**
- Check the MASTER fader is up and the clip indicator isn't stuck on.
- Check the CONN indicator in the top bar — tap it to see the audio output menu.
- Open the audio dropdown and pick your actual output (USB DAC, HDMI, etc.).

**Piano is silent but pad works.**
- The soundfont didn't install. Check `ls ~/.local/share/stave-synth/soundfonts/` — you should see `TimGM6mb.sf2`. Re-run `./install.sh` or drop any `.sf2` into that folder and restart.

**MIDI keyboard doesn't do anything.**
- Tap the `MIDI` indicator in the top bar — it should flash green on each note. If not, check `aconnect -i` on the Pi; the keyboard should show up as a client. The synth auto-connects.
- Unplug and replug the USB cable. The audio service will reconnect on the next note.

**Reverb tail ends abruptly / clicks / pops.**
- Lower the MASTER fader a few dB; the limiter might be engaging too hard.
- Check CPU: open the MENU → sys stats. Over ~85% can cause xruns.

**The service won't start.**
- `journalctl --user -u stave-synth -f` shows the last error.
- Most common: JACK/PipeWire wasn't ready. Wait 10 seconds and `systemctl --user restart stave-synth`.

## Manual control

### If installed with `--no-autostart` (casual mode)

```bash
cd stave-synth
./stave-synth.sh      # starts the synth (Ctrl-C to stop)
```

Or run directly for debugging:

```bash
pw-jack ./venv/bin/python -m stave_synth.main --no-gui
```

### If installed with auto-start (live / kiosk mode)

```bash
# Start / stop / restart
systemctl --user start stave-synth
systemctl --user stop stave-synth
systemctl --user restart stave-synth

# Live logs
journalctl --user -u stave-synth -f

# Run directly (for debugging)
pw-jack ./venv/bin/python -m stave_synth.main --no-gui
```

## Features

- **Dual Oscillators** — OSC1 + OSC2 with 5 waveforms (sine / square / saw / triangle / saturated), independent octave (−3 to +3), independent or shared filters, hard-pan WIDE mode with Haas delay, MIRROR to mirror per-OSC params
- **Per-OSC ADSR envelopes** — OSC1 and OSC2 each carry their own ADSR, so plucky top + sustained pad within the same note is native. MIRROR extends to ADSR knobs.
- **Unison** — 1–5 voices per OSC with tunable detune + stereo spread
- **Lowpass Filter** — 12 or 24 dB/oct, smooth log-space sweep, per-osc or shared, ALT-flipped to highpass (low cut)
- **7 Reverb Types** — WASH / HALL / ROOM / PLATE / BLOOM / DRONE / GHOST. Plate (Dattorro) and drone (tuned-resonator topology) are dedicated Faust modules; the rest share a 8-line FDN with Hadamard mixing, stereo early reflections, modulated delays, hi/lo cut on feedback
- **Per-OSC reverb sends + FX bypass** — OSC1 dry while OSC2 drenches in the tail (and vice versa)
- **Shimmer** — Octave-up sines into reverb, 1.2 kHz highpass, `+12` toggle adds another octave (+24 total)
- **CLOUD** — Pre-reverb multi-tap stereo bouncing delay on shimmer for atmospheric motion
- **Sympathetic Resonance (RESO)** — Piano notes subtly excite the pad through the reverb, cubic-curve level control
- **Chord Drone** — Sustained root + fifth one octave below with portamento
- **Freeze** — Infinite reverb tail sustain
- **Dual LFO with LINK** — Two independent control-rate LFOs (sine / triangle / square / saw / ramp / peak / S&H) with Invert flip per LFO. Targets: filter / amp / pan. Tempo-synced to BPM or free-Hz. Speed multiplier (½x / 1x / 2x). Key Sync resets phase on note-on. Bipolar ms offset with Haas Comp. Visual alignment scope shows both shapes overlaid
- **AMP target** uses a gate formula that delivers full 0..1 mute at depth=1 (sidechain-pump flavor) with unity-average makeup
- **Tempo-synced Ping-Pong Delay** — 1/2 through 1/16, triplet + dotted, or FREE-ms; stereo offset, feedback, wet control
- **Piano Layer** — FluidSynth GM soundfont, 24 dB/oct hi/lo cut EQ, authentic LA-2A–style compressor (soft knee, DRIVE input, parallel wet/dry, PERFECT preset), plus FluidSynth's internal room reverb and an optional send to the global reverb bus
- **B3 Organ Engine** — Tonewheel drawbars + split Leslie (chorale/fast), Faust-native
- **SSL G-style Bus Compressor** — feedback-flavor, self / piano / LFO / BPM sidechain, GLUE / PUNCH / PUMP presets, ∞-ratio brickwall mode, GR LED with peak hold
- **Master EQ** — 3-band parametric + configurable low cut (6 / 12 / 24 dB)
- **Saturation (SAT)** — Asymmetric soft drive pre-limiter
- **Sustain Pedal** — CC64, transpose-safe note tracking
- **Preset Crossfade** — 800 ms musical morphing between 10 color-coded slots (2 layers of 5)
- **MIDI Learn** — Map any CC to any fader
- **Macros (4 slots)** — Learn any parameter, ride via macro fader
- **Master FADE** — 5-second musical fade out/in for song endings
- **True Stereo** — Full stereo pipeline, pre-limiter trim, tanh soft limiter
- **Audio Output Selector** — Switch USB / Bluetooth / HDMI from the UI
- **Faust-native DSP** on hot paths (reverb, osc bank, sympathetic, ping-pong, master FX, bus comp, organ, plate, drone) for stable CPU under full load on the Pi 5

## Hardware

- Raspberry Pi 5 (8 GB)
- USB MIDI keyboard
- Audio output: any USB audio interface (tested: ART USB DI, TTGK USB-C), or Bluetooth / HDMI sink
- 5" DSI touchscreen (optional — UI works from any browser)

## Architecture

```
stave_synth/
  jack_bridge.c        — C bridge for JACK audio I/O (ring buffer, master smoother)
  jack_engine.py       — MIDI dispatch, audio render loop, master fade, SSL shuffler
  synth_engine.py      — Oscillators, filters, reverb, shimmer, cloud, voices
  fluidsynth_player.py — Piano via FluidSynth soundfont
  organ_engine.py      — B3 tonewheel + Leslie
  main.py              — Message router, state management, preset crossfade
  websocket_server.py  — WebSocket + HTTP UI server
  midi_handler.py      — Transpose, sustain, note tracking
  preset_manager.py    — JSON preset persistence (atomic writes)
  config.py            — Defaults, paths, state load/save

ui/
  index.html / style.css / script.js — Touch UI

soundfonts/
  TimGM6mb.sf2         — Bundled GM soundfont (public domain, ~6MB)
```

## Config & data paths

- Presets: `~/.config/stave-synth/presets/`
- State: `~/.config/stave-synth/current_state.json` (resumed on every launch)
- Soundfonts: `~/.local/share/stave-synth/soundfonts/`

## License

MIT
