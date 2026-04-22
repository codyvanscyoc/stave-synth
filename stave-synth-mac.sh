#!/bin/bash
# Stave Synth launcher (macOS).
# Mirrors stave-synth.sh (Linux) but drops pw-jack / amixer — Mac routes
# audio through Core Audio via sounddevice, no external audio server.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Faust DSP backends — mirror systemd/stave-synth.service.d/faust.conf
# (which the Mac doesn't use, but the flags are engine-level, not OS-level).
: "${STAVE_FAUST_REVERB:=1}"
: "${STAVE_FAUST_PING_PONG:=1}"
: "${STAVE_FAUST_OSC_BANK:=1}"
: "${STAVE_FAUST_SYMPATHETIC:=1}"
: "${STAVE_FAUST_MASTER_FX:=1}"
: "${STAVE_FAUST_BUS_COMP:=1}"
: "${STAVE_FAUST_ORGAN:=1}"
export STAVE_FAUST_REVERB STAVE_FAUST_PING_PONG STAVE_FAUST_OSC_BANK \
       STAVE_FAUST_SYMPATHETIC STAVE_FAUST_MASTER_FX STAVE_FAUST_BUS_COMP \
       STAVE_FAUST_ORGAN

# Kill any existing instance so we don't stack MIDI/audio clients.
pkill -f "stave_synth.main" 2>/dev/null || true
sleep 1

# GUI defaults on. Pass --no-gui as an argument to launch headless (UI still reachable at :8080).
exec ./venv/bin/python -m stave_synth.main "$@"
