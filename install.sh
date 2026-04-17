#!/bin/bash
# Stave Synth — Installation Script for Raspberry Pi
# Run: ./install.sh

set -e

GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'

echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  STAVE SYNTH — Installer${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOUNDFONT_DIR="$HOME/.local/share/stave-synth/soundfonts"
CONFIG_DIR="$HOME/.config/stave-synth"

# ── Step 1: System dependencies ──
echo -e "${ORANGE}[1/6]${NC} Installing system dependencies..."

# WebKit package differs by Debian release: Bookworm ships 4.0, Trixie ships 4.1.
WEBKIT_PKG="gir1.2-webkit2-4.1"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${VERSION_CODENAME:-}" in
        bookworm|bullseye) WEBKIT_PKG="gir1.2-webkit2-4.0" ;;
    esac
fi
echo -e "${GREEN}  Using WebKit package: $WEBKIT_PKG${NC}"

sudo apt-get update -qq
sudo apt-get install -y \
    jackd2 \
    libjack-jackd2-dev \
    python3-dev \
    python3-pip \
    python3-venv \
    fluidsynth \
    libfluidsynth-dev \
    alsa-utils \
    a2jmidid \
    libgirepository1.0-dev \
    "$WEBKIT_PKG" \
    python3-gi \
    python3-gi-cairo

# ── Step 2: Audio permissions ──
echo -e "${ORANGE}[2/6]${NC} Configuring audio permissions..."
# Configure real-time audio limits
if ! grep -q "audio.*rtprio" /etc/security/limits.d/audio.conf 2>/dev/null; then
    echo -e "${ORANGE}Setting up real-time audio permissions...${NC}"
    sudo tee /etc/security/limits.d/audio.conf > /dev/null << 'EOF'
@audio   -  rtprio     95
@audio   -  memlock    unlimited
EOF
    # Ensure user is in audio group
    sudo usermod -aG audio "$USER"
    echo -e "${GREEN}  Audio permissions configured (re-login may be needed)${NC}"
fi

# ── Step 3: System tuning (live-performance stability) ──
echo -e "${ORANGE}[3/6]${NC} Applying system tuning..."

# CPU governor → performance (prevents audio stutter from clock scaling)
if [ ! -f /etc/systemd/system/cpu-performance.service ]; then
    sudo tee /etc/systemd/system/cpu-performance.service > /dev/null << 'EOF'
[Unit]
Description=Set CPU governor to performance (audio stability)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $c; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable cpu-performance.service > /dev/null 2>&1
    sudo systemctl start cpu-performance.service > /dev/null 2>&1
    echo -e "${GREEN}  CPU governor locked to performance${NC}"
else
    echo -e "${GREEN}  CPU governor service already installed${NC}"
fi

# Disable screen blanking (DPMS + screensaver) via user autostart
BLANK_DESKTOP="$HOME/.config/autostart/disable-screen-blank.desktop"
if [ ! -f "$BLANK_DESKTOP" ]; then
    mkdir -p "$HOME/.config/autostart"
    cat > "$BLANK_DESKTOP" << 'EOF'
[Desktop Entry]
Type=Application
Name=Disable screen blanking
Comment=Keep display always on for live synth use
Exec=sh -c "xset s off; xset s noblank; xset -dpms"
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
    echo -e "${GREEN}  Screen blanking disabled on login${NC}"
else
    echo -e "${GREEN}  Screen blanking autostart already present${NC}"
fi

# Disable USB autosuspend (prevents MIDI/audio interface dropouts) via kernel cmdline
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ] && ! grep -q "usbcore.autosuspend=-1" "$CMDLINE"; then
    sudo cp "$CMDLINE" "${CMDLINE}.bak"
    # cmdline.txt must remain one line — append to end of existing line
    sudo sed -i 's/$/ usbcore.autosuspend=-1/' "$CMDLINE"
    echo -e "${GREEN}  USB autosuspend disabled (active after next reboot)${NC}"
else
    echo -e "${GREEN}  USB autosuspend already disabled${NC}"
fi
# Apply live as well for current session
for dev in /sys/bus/usb/devices/*/power/control; do
    [ -w "$dev" ] && echo on | sudo tee "$dev" > /dev/null 2>&1
done 2>/dev/null
echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend > /dev/null 2>&1 || true

# ── Step 4: Build C audio bridge ──
echo -e "${ORANGE}[4/6]${NC} Building JACK audio bridge..."
cd "$SCRIPT_DIR/stave_synth"
gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread
cd "$SCRIPT_DIR"
echo -e "${GREEN}  Built jack_bridge.so${NC}"

# ── Step 5: Python dependencies ──
echo -e "${ORANGE}[5/6]${NC} Installing Python dependencies..."
cd "$SCRIPT_DIR"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    python3 -m venv venv --system-site-packages
fi

source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# ── Step 6: Soundfonts & service ──
echo -e "${ORANGE}[6/6]${NC} Setting up soundfonts & service..."
mkdir -p "$SOUNDFONT_DIR"
mkdir -p "$CONFIG_DIR/presets"

# Try to find an existing soundfont first
FOUND_SF=""
for sf in /usr/share/sounds/sf2/*.sf2 /usr/share/soundfonts/*.sf2; do
    if [ -f "$sf" ]; then
        FOUND_SF="$sf"
        break
    fi
done

if [ -n "$FOUND_SF" ]; then
    echo -e "${GREEN}  Found system soundfont: $FOUND_SF${NC}"
    # Symlink it
    ln -sf "$FOUND_SF" "$SOUNDFONT_DIR/system.sf2" 2>/dev/null || true
fi

# Install TimGM6mb. Prefer the copy bundled in the repo (offline-proof);
# fall back to Debian package; fall back to direct download.
BUNDLED_SF="$SCRIPT_DIR/soundfonts/TimGM6mb.sf2"
if [ ! -f "$SOUNDFONT_DIR/TimGM6mb.sf2" ]; then
    if [ -f "$BUNDLED_SF" ]; then
        cp "$BUNDLED_SF" "$SOUNDFONT_DIR/TimGM6mb.sf2"
        echo -e "${GREEN}  Installed bundled TimGM6mb.sf2${NC}"
    elif apt-cache show timgm6mb-soundfont > /dev/null 2>&1; then
        sudo apt-get install -y timgm6mb-soundfont > /dev/null 2>&1 && \
            ln -sf /usr/share/sounds/sf2/TimGM6mb.sf2 "$SOUNDFONT_DIR/TimGM6mb.sf2" && \
            echo -e "${GREEN}  Installed TimGM6mb via apt${NC}"
    else
        echo "  Attempting to download TimGM6mb soundfont (~6MB)..."
        wget -q "https://sourceforge.net/projects/mscore/files/soundfont/TimGM6mb.sf2/download" \
            -O "$SOUNDFONT_DIR/TimGM6mb.sf2" 2>/dev/null && \
            echo -e "${GREEN}  Downloaded TimGM6mb.sf2${NC}" || \
            echo -e "${ORANGE}  Could not install a soundfont. Drop a .sf2 file in: $SOUNDFONT_DIR${NC}"
    fi
fi

# ── Install systemd service ──
echo "  Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"

# Generate service file with correct paths.
# Amixer PCM/Master probe is wrapped: some USB DACs only expose Master.
cat > "$HOME/.config/systemd/user/stave-synth.service" << EOF
[Unit]
Description=Stave Synth — Live MIDI Synthesizer
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 3
ExecStartPre=-/bin/bash -c 'for c in /proc/asound/card*/id; do n=\$\$(dirname \$\$c | grep -o "[0-9]*"); if grep -qi usb "\$\$c" 2>/dev/null; then if amixer -c \$\$n sget PCM >/dev/null 2>&1; then amixer -c \$\$n set PCM 100%% >/dev/null 2>&1; elif amixer -c \$\$n sget Master >/dev/null 2>&1; then amixer -c \$\$n set Master 100%% >/dev/null 2>&1; fi; fi; done; true'
ExecStart=/usr/bin/pw-jack ${SCRIPT_DIR}/venv/bin/python -m stave_synth.main
WorkingDirectory=${SCRIPT_DIR}
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable stave-synth.service

# Enable linger so user services start at boot without login
loginctl enable-linger "$USER" 2>/dev/null || true

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""

# ── Summary of environment detected at install time ──
check() { if [ "$1" = "ok" ]; then echo -e "${GREEN}✓${NC} $2"; else echo -e "${ORANGE}✗${NC} $2"; fi; }

echo "  Environment check:"

# Soundfont
SF_COUNT=$(ls -1 "$SOUNDFONT_DIR"/*.sf2 2>/dev/null | wc -l)
if [ "$SF_COUNT" -gt 0 ]; then
    check ok "Soundfont: $SF_COUNT file(s) in $SOUNDFONT_DIR"
else
    check no "No soundfont found — piano will be silent. Drop a .sf2 in $SOUNDFONT_DIR"
fi

# USB audio interfaces
USB_AUDIO=""
for c in /proc/asound/card*/id; do
    [ -f "$c" ] || continue
    if grep -qi usb "$c" 2>/dev/null; then
        USB_AUDIO="$USB_AUDIO $(cat "$c")"
    fi
done
if [ -n "$USB_AUDIO" ]; then
    check ok "USB audio detected:$USB_AUDIO"
else
    check no "No USB audio interface plugged in (can attach later — UI audio selector will pick it up)"
fi

# USB MIDI
if command -v aconnect >/dev/null 2>&1 && aconnect -i 2>/dev/null | grep -qi -E "midi|keyboard|mpk|akai"; then
    MIDI_NAME=$(aconnect -i 2>/dev/null | grep -iE "client.*[0-9]+:" | grep -vi "system\|through" | head -1 | sed 's/client [0-9]*: //; s/ \[.*$//')
    check ok "USB MIDI detected: $MIDI_NAME"
else
    check no "No USB MIDI input detected (plug in your keyboard — it'll auto-connect on synth start)"
fi

# Service enabled
if systemctl --user is-enabled stave-synth.service >/dev/null 2>&1; then
    check ok "Auto-start on boot: enabled"
else
    check no "Auto-start on boot: not enabled (run: systemctl --user enable stave-synth)"
fi

echo ""
echo "  To start now:    systemctl --user start stave-synth"
echo "  Open the UI:     http://localhost:8080  (or http://<pi-ip>:8080 from another device)"
echo "  Logs:            journalctl --user -u stave-synth -f"
echo "  Run manually:    pw-jack $SCRIPT_DIR/venv/bin/python -m stave_synth.main"
echo ""
echo "  On next boot, Stave Synth will start automatically."
echo ""
