# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 — worship ambient pad with piano layer.

## Hardware

- Pi 5 (8GB), Pi OS Trixie (Debian 13)
- TTGK USB-C audio adapter (**BTL amplifier** — must invert R channel for mono, see below)
- Akai MPKmini2 USB MIDI keyboard
- 5" capacitive DSI touchscreen (800x480, not yet arrived)
- USB audio interface on order — will replace BTL adapter

## Architecture

- **PipeWire-JACK** for audio. App must run via `pw-jack` prefix — real jackd doesn't output audio on this Pi 5 Trixie setup.
- **C bridge** (`stave_synth/jack_bridge.c` → `jack_bridge.so`): handles JACK process callback natively because python-jack-client CFFI is broken on aarch64/Pi 5. Ring buffer (8 slots) lets Python render ahead.
- **Python synth engine** (`synth_engine.py`): renders audio blocks, pushes to C bridge via ctypes
- **FluidSynth rendered in Python pipeline** (not JACK driver): enables our own DSP (EQ, compressor) on piano audio
- **WebSocket + HTTP**: UI served to browser/pywebview

## Critical: BTL USB Audio Adapter

The TTGK USB-C adapter uses Bridge-Tied Load output. Headphones hear L - R (difference).
**Identical L/R = silence.** Fix: BTL_MODE sums to mono and inverts R in `jack_bridge.c`.

`BTL_MODE = True` in `config.py`. Set `False` for normal audio interfaces / mixer.

**Any mono signal (piano, click track) will be SILENT on BTL headphones with BTL_MODE=False.**

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

## Current state (2026-04-14)

Working: synth pad (OSC1 + OSC2 with 5 waveforms), MIDI input, FluidSynth piano, lowpass filter (12/24dB selectable) with smooth log-space sweep, per-oscillator independent filters, per-oscillator octave shift, master volume (single gain stage in C bridge, dB curve), voice stealing (16 voices), unison with detune, piano EQ (6dB/oct one-pole hi/lo cut), piano compressor, FDN reverb (8 lines, Hadamard, stereo, modulated, hi/lo cut on feedback), shimmer (synthesized octave-up sines into reverb), sustain pedal (CC64), true stereo pipeline, presets (5 slots), MIDI auto-connect, MIDI CC learn, audio output selector, WebSocket UI with settings modal.

## Design principles

- Piano with pad underneath is the core goal. Everything else is extra.
- Simple, lean, clean. Don't over-engineer.
- Low latency, touch-friendly, easy install on other Pis.
- Master volume: single gain stage in C bridge only. Don't add another in the synth engine.
- FluidSynth must route through Python pipeline — never go back to JACK driver output.
- Distribution: `git clone` + `./install.sh`
