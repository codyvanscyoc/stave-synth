#!/bin/bash
# Start JACK2 targeting the USB-C Audio device
# This script finds the USB audio card dynamically (card number can change)

# Find the USB-C Audio card number
CARD=$(aplay -l 2>/dev/null | grep "USB.*Audio" | head -1 | sed 's/card \([0-9]*\):.*/\1/')

if [ -z "$CARD" ]; then
    echo "ERROR: No USB audio device found. Falling back to default."
    CARD=0
fi

echo "Starting JACK on hw:${CARD} (USB Audio)"

# Kill any existing JACK server
killall jackd 2>/dev/null
sleep 0.5

# Start JACK2 with low-latency settings
# -d alsa: use ALSA driver
# -d hw:N: target USB audio card
# -r 48000: 48kHz sample rate
# -p 256: 256 samples per period (~5.3ms latency)
# -n 2: 2 periods (total ~10.6ms round-trip)
# -S: 16-bit (matches USB adapter capability)
exec jackd -R -d alsa -d "hw:${CARD}" -r 48000 -p 256 -n 2 -S
