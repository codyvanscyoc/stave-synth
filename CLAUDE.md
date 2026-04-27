# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 — worship ambient pad with piano layer.

## Hardware

- Pi 5 (8GB), Pi OS Trixie (Debian 13)
- ART USB DI (primary audio out, USB-bus-powered, no galvanic isolation — picks up USB bus noise from Pi; a USB isolator dongle would fix it)
- TTGK USB-C audio adapter (backup — NOT a BTL amplifier, confirmed proper stereo)
- Akai MPKmini2 USB MIDI keyboard
- 5" capacitive DSI touchscreen (800x480, not yet arrived)

## Architecture

- **PipeWire-JACK** for audio. App must run via `pw-jack` prefix — real jackd doesn't output audio on this Pi 5 Trixie setup.
- **C bridge** (`stave_synth/jack_bridge.c` → `jack_bridge.so`): handles JACK process callback natively because python-jack-client CFFI is broken on aarch64/Pi 5. Ring buffer (8 slots) lets Python render ahead.
- **Python synth engine** (`synth_engine.py`): renders audio blocks, pushes to C bridge via ctypes
- **FluidSynth rendered in Python pipeline** (not JACK driver): enables our own DSP (EQ, compressor) on piano audio
- **WebSocket + HTTP**: UI served to browser/pywebview

## BTL Mode (legacy)

`BTL_MODE = False` in `config.py` — correct for all common USB audio interfaces (ART USB DI, TTGK USB-C, etc — all confirmed proper stereo, not BTL). Only set `True` if using a genuine Bridge-Tied Load adapter where headphones hear L - R.

## Build the C bridge

```bash
cd stave_synth
gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread
```

## Run

```bash
# Run with PipeWire-JACK (required on this Pi 5)
pw-jack ./venv/bin/python -m stave_synth.main --no-gui
```

## Current state

For the live changelog and per-session details, see `~/.claude/projects/-home-codyvanscyoc/memory/` — `MEMORY.md` is the index. `project_code_review_8.md` (2026-04-26) is the latest comprehensive snapshot.

High-level architecture: 16-voice Faust polysynth + piano (FluidSynth + 4 soundfont presets + 7 voicings) + B3 organ (Faust) + 7 reverb engines + 2 LFOs (poly + tempo-sync + shape lib) + ping-pong delay + sympathetic resonance + bus comp (SSL G) + per-OSC ADSR/filter/pan + macros + setlist + recorder + WebSocket UI. Tested live in worship every week. ~50% CPU at full load on Pi 5.

## Design principles

- Piano with pad underneath is the core goal. Everything else is extra.
- Simple, lean, clean. Don't over-engineer.
- Low latency, touch-friendly, easy install on other Pis.
- Master volume: single gain stage in C bridge only. Don't add another in the synth engine.
- FluidSynth must route through Python pipeline — never go back to JACK driver output.
- Distribution: `git clone` + `./install.sh`
