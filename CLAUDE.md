# Stave Synth

Live MIDI synthesizer for Raspberry Pi 5 — worship ambient pad with piano layer.

## Hardware

- Pi 5 (8GB), Pi OS Trixie (Debian 13)
- TTGK USB-C audio adapter (**BTL amplifier** — must invert R channel for mono, see below)
- Akai MPKmini2 USB MIDI keyboard
- 5" capacitive DSI touchscreen (800x480, not yet arrived)

## Architecture

- **JACK2** for audio (not PipeWire). jackd runs on `hw:2` (USB-C audio), 48kHz, 512 buffer, 3 periods
- **C bridge** (`stave_synth/jack_bridge.c` → `jack_bridge.so`): handles JACK process callback natively because python-jack-client CFFI is broken on aarch64/Pi 5. Ring buffer (8 slots) lets Python render ahead.
- **Python synth engine** (`synth_engine.py`): renders audio blocks, pushes to C bridge via ctypes
- **FluidSynth**: piano via soundfonts, connects to JACK independently
- **WebSocket + HTTP**: UI served to browser/pywebview

## Critical: BTL USB Audio Adapter

The TTGK USB-C adapter uses Bridge-Tied Load output. Headphones hear L - R (difference).
**Identical L/R = silence.** Fix: `out_r = -signal` in `jack_bridge.c`.

`BTL_MODE = True` in `config.py`. Set `False` for normal audio interfaces.

## Build the C bridge

```bash
cd stave_synth
gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread
```

## Run

```bash
# Start JACK first (if not already running)
jackd -R -d alsa -d hw:2 -r 48000 -p 512 -n 3 &

# Run the app
./venv/bin/python -m stave_synth.main --no-gui
```

## Current state (2026-04-13)

Working: synth pad (OSC1 sine + OSC2 saturated), MIDI input, FluidSynth piano, lowpass filter with smooth sweep, master volume, presets, WebSocket UI.

### Known issues / next steps
- OSC blend volume curve is linear — needs dB scaling (too loud at top, distorts)
- Filter is 12dB/oct — add 24dB/oct option (cascade second biquad)
- Reverb is basic feedback delay — needs proper diffusion for ambient wash
- Shimmer is a fixed sine modulation — needs octave-up pitch shift in reverb tail (BigSky-style)
- Piano EQ not verified working
- Fader UX: resonance via horizontal swipe on filter fader (not yet implemented)

## Design principles

- Piano with pad underneath is the core goal. Everything else is extra.
- Simple, lean, clean. Don't over-engineer.
- Low latency, touch-friendly, easy install on other Pis.
- Distribution: `git clone` + `./install.sh`
