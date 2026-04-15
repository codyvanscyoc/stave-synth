#!/bin/bash
# Stave Synth launcher — sets USB audio to max, starts synth
# Kill any existing instance
pkill -f "stave_synth.main" 2>/dev/null
sleep 1

# Set USB audio volume to max (TTGK adapter resets on reboot)
amixer -c 3 set PCM 100% 2>/dev/null

# Start synth
cd /home/codyvanscyoc/stave-synth
exec pw-jack ./venv/bin/python -m stave_synth.main --no-gui
