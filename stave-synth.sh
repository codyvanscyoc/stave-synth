#!/bin/bash
# Stave Synth launcher — sets USB audio to max, starts synth.
# Portable: locates the repo relative to this script; auto-detects USB card.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kill any existing instance
pkill -f "stave_synth.main" 2>/dev/null || true
sleep 1

# Set PCM to max on whichever USB audio card is present (card number varies)
for c in /proc/asound/card*/id; do
    n=$(dirname "$c" | grep -o "[0-9]*")
    if grep -qi usb "$c" 2>/dev/null; then
        amixer -c "$n" set PCM 100% 2>/dev/null || true
    fi
done

exec pw-jack ./venv/bin/python -m stave_synth.main --no-gui
