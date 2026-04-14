# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 with touchscreen control.

## Features

- **Synth Pad Engine** — Sine + square oscillators, highpass filter, ADSR envelope, Schroeder reverb with shimmer
- **Piano Layer** — FluidSynth-powered piano/e-piano via soundfont (GM compatible)
- **Low Latency** — JACK2 audio server, <10ms MIDI-to-audio target
- **Terminal UI** — Dark, minimal, touch-friendly interface via native window (pywebview)
- **5 Preset Slots** — Save/load/rename with JSON persistence
- **Autostart** — Systemd user service, crash recovery

## Hardware

- Raspberry Pi 5
- USB MIDI keyboard
- 3.5mm headphone output (or USB audio interface)
- 5" DSI touchscreen (optional — works on any display)

## Quick Install

```bash
git clone https://github.com/YOUR_USERNAME/stave-synth.git
cd stave-synth
./install.sh
```

The install script handles:
- JACK2 + FluidSynth system packages
- Python virtual environment + dependencies
- Real-time audio permissions
- Soundfont download
- Systemd service (autostart on boot)

## Manual Run

```bash
cd stave-synth
source venv/bin/activate
python -m stave_synth.main
```

Or after install: `systemctl --user start stave-synth`

## UI Controls

| Control | Function |
|---------|----------|
| Fader 1 | Synth pad volume |
| Fader 2 | Piano volume |
| Fader 3 | Highpass filter cutoff (Alt: resonance) |
| Fader 4 | Master volume (Alt: reverb dry/wet) |
| [0]/[1] buttons | Toggle alt mode per fader |
| +/- buttons | Transpose (-12 to +12 semitones) |
| SHIMMER | Toggle shimmer reverb effect |
| MENU | Open settings (ADSR, oscillator blend, EQ, etc.) |
| Preset buttons | Tap to load, double-tap to rename |
| SAVE | Save current state to active preset slot |

## Architecture

```
stave_synth/
  main.py             — Entry point, wires components together
  synth_engine.py     — Oscillators, filter, ADSR, reverb
  jack_engine.py      — JACK audio I/O + MIDI input
  fluidsynth_player.py — Piano/e-piano via soundfont
  midi_handler.py     — Transpose, note tracking
  preset_manager.py   — JSON preset persistence
  websocket_server.py — WebSocket + HTTP server
  config.py           — Defaults, paths

ui/
  index.html          — Main UI layout
  style.css           — Terminal aesthetic
  script.js           — Faders, presets, WebSocket client
```

## Config Paths

- Presets: `~/.config/stave-synth/presets/`
- State: `~/.config/stave-synth/current_state.json`
- Soundfonts: `~/.local/share/stave-synth/soundfonts/`

## Logs

```bash
journalctl --user -u stave-synth -f
```

## License

MIT
