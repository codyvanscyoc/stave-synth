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
    gir1.2-webkit2-4.1 \
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

# Download TimGM6mb (small, reliable fallback)
if [ ! -f "$SOUNDFONT_DIR/TimGM6mb.sf2" ]; then
    echo "  Downloading TimGM6mb soundfont (~6MB)..."
    # Try multiple sources
    wget -q "https://sourceforge.net/projects/mscore/files/soundfont/TimGM6mb.sf2/download" \
        -O "$SOUNDFONT_DIR/TimGM6mb.sf2" 2>/dev/null || \
    echo -e "${ORANGE}  Could not auto-download soundfont. Place a .sf2 file in: $SOUNDFONT_DIR${NC}"
fi

# ── Install systemd service ──
echo "  Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"

# Generate service file with correct paths
cat > "$HOME/.config/systemd/user/stave-synth.service" << EOF
[Unit]
Description=Stave Synth — Live MIDI Synthesizer
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 3
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
echo "  To start now:   systemctl --user start stave-synth"
echo "  To run manually: cd $SCRIPT_DIR && source venv/bin/activate && python -m stave_synth.main"
echo "  Logs:           journalctl --user -u stave-synth -f"
echo ""
echo "  On next boot, Stave Synth will start automatically."
echo ""

if [ ! -f "$SOUNDFONT_DIR/TimGM6mb.sf2" ] && [ ! -f "$SOUNDFONT_DIR/Arachno.sf2" ]; then
    echo -e "${ORANGE}  NOTE: No soundfont found. Piano will be disabled.${NC}"
    echo -e "${ORANGE}  Place a .sf2 file in: $SOUNDFONT_DIR${NC}"
    echo ""
fi
