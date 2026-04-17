# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 — worship ambient pad with piano layer.

## Install

```bash
git clone https://github.com/codyvanscyoc/stave-synth.git
cd stave-synth
./install.sh
```

That's it. The script installs everything, sets up auto-start on boot, and starts the synth. Plug in a USB MIDI keyboard and play.

### What install.sh does

- Installs JACK2, FluidSynth, Python dependencies
- Sets up real-time audio permissions
- Downloads a GM soundfont for piano
- Creates a systemd user service (auto-start on boot, no login needed)
- Builds the C audio bridge

## Usage

After install, the synth runs automatically on boot. Access the UI from any device on your network:

```
http://<your-pi-ip>:8080
```

### Manual control

```bash
# Start/stop
systemctl --user start stave-synth
systemctl --user stop stave-synth

# Logs
journalctl --user -u stave-synth -f

# Run directly (for debugging)
cd stave-synth
pw-jack ./venv/bin/python -m stave_synth.main --no-gui
```

## Features

- **Dual Oscillators** — OSC1 + OSC2 with independent octave shift (-3 to +3) and independent filter cutoffs
- **Waveforms** — Sine, square, sawtooth, triangle, saturated
- **Lowpass Filter** — 12dB or 24dB/oct, smooth log-space sweep, per-osc or shared
- **Cathedral Reverb** — 8-line FDN with Hadamard mixing, stereo early reflections, all-pass diffusion, modulated delays, hi/lo cut on feedback
- **Shimmer** — Octave-up sines into reverb, 2kHz highpass, no ceiling
- **Reverb Freeze** — Infinite sustain on current reverb tail
- **Piano Layer** — FluidSynth GM soundfont, 6dB/oct low-cut and high-cut EQ, compressor
- **Sustain Pedal** — CC64, holds pad and piano notes
- **10 Presets** — Color-coded, two layers of 5 (L1/L2), tap to save/load, long-press to overwrite, double-tap to delete, rename/rearrange via EDIT mode
- **MIDI Learn** — Map any CC to any fader
- **Audio Output Selector** — Switch between USB, Bluetooth, HDMI from the UI
- **True Stereo** — Full stereo pipeline from render to output
- **Soft Limiter** — tanh limiting throughout, clip indicator in UI

## Hardware

- Raspberry Pi 5
- USB MIDI keyboard
- Audio output: ART USB DI (primary) or any USB audio interface / Bluetooth / HDMI sink
- 5" DSI touchscreen (optional — UI works from any browser)

## UI

4 touch faders with ALT modes:

| Fader | Normal | ALT (tap ALT) |
|-------|--------|----------------|
| OSC 1 | OSC1 volume | OSC2 volume |
| PIANO | Piano volume | Tone / Compressor |
| FILTER | Filter cutoff | Reverb mix / Shimmer mix |
| MASTER | Master volume | — |

Octave +/- buttons under OSC 1 fader shift OSC1 (or OSC2 when ALT is on).

Top bar: MENU (settings), MIDI indicator, CLIP indicator, CONN (tap to switch audio output).

## Architecture

```
stave_synth/
  jack_bridge.c        — C bridge for JACK audio I/O (ring buffer)
  jack_engine.py       — MIDI dispatch, audio render loop
  synth_engine.py      — Oscillators, filters, reverb, shimmer, voices
  fluidsynth_player.py — Piano via FluidSynth soundfont
  main.py              — Message router, state management
  websocket_server.py  — WebSocket + HTTP UI server
  midi_handler.py      — Transpose, note tracking
  preset_manager.py    — JSON preset persistence
  config.py            — Defaults, paths

ui/
  index.html / style.css / script.js — Touch UI
```

## Config

- Presets: `~/.config/stave-synth/presets/`
- State: `~/.config/stave-synth/current_state.json`
- Soundfonts: `~/.local/share/stave-synth/soundfonts/`

## License

MIT
