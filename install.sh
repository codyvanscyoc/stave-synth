#!/bin/bash
# Stave Synth — Installation Script for Raspberry Pi
# Run: ./install.sh               — full install with auto-start on boot
# Run: ./install.sh --no-autostart — casual use; no systemd service installed

set -e

AUTOSTART=1
INSTALL_SALAMANDER=1
for arg in "$@"; do
    case "$arg" in
        --no-autostart)  AUTOSTART=0 ;;
        --no-salamander) INSTALL_SALAMANDER=0 ;;
        -h|--help)
            echo "Usage: ./install.sh [--no-autostart] [--no-salamander]"
            echo "  --no-autostart    Skip the systemd user service install."
            echo "                    Launch on demand with ./stave-synth.sh instead."
            echo "  --no-salamander   Skip the Salamander Grand Piano download (~296MB"
            echo "                    download → 1.2GB on disk). Use if you're on a slow"
            echo "                    link or can't afford the space; the installer will"
            echo "                    fall back to FluidR3_GM via apt."
            exit 0
            ;;
    esac
done

GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'

echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  STAVE SYNTH — Installer${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""
if [ "$AUTOSTART" = "0" ]; then
    echo -e "${ORANGE}  Mode: casual (no auto-start — run ./stave-synth.sh to play)${NC}"
    echo ""
fi

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
    pipewire \
    pipewire-jack \
    wireplumber \
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
    python3-gi-cairo \
    faust \
    gcc \
    libc6-dev

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

# ── Step 4: Build C audio bridge + Faust DSP modules ──
echo -e "${ORANGE}[4/6]${NC} Building JACK audio bridge..."
cd "$SCRIPT_DIR/stave_synth"
gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread
cd "$SCRIPT_DIR"
echo -e "${GREEN}  Built jack_bridge.so${NC}"

echo -e "${ORANGE}[4/6]${NC} Building Faust DSP modules..."
cd "$SCRIPT_DIR/faust"
./build.sh > /dev/null
cd "$SCRIPT_DIR"
echo -e "${GREEN}  Built Faust modules (reverb, ping_pong, osc_bank, sympathetic, master_fx, bus_comp)${NC}"

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

# Optional native-window deps — only needed to open a fullscreen pywebview
# window directly on the Pi. The browser UI at http://<pi-ip>:8080 works
# without it, so a pywebview install hiccup is non-fatal.
if ! pip install -r requirements-gui.txt -q 2>/dev/null; then
    echo -e "${ORANGE}  (Optional pywebview skipped — browser UI at :8080 still works)${NC}"
fi

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

# Salamander Grand Piano — FreePats SF2 build, 16 velocity layers, Yamaha C5.
# CC-BY 3.0 (Alexander Holm; SF2 assembly by Roberto @ FreePats). 296MB download
# → 1.2GB on disk. The default piano; run with --no-salamander if you want to
# skip it (e.g. slow link or tight storage — fallback chain below handles it).
SAL_INSTALLED=0
if [ "$INSTALL_SALAMANDER" = "1" ]; then
    if [ ! -f "$SOUNDFONT_DIR/Salamander.sf2" ]; then
        echo "  Downloading Salamander Grand Piano (~296MB, extracts to 1.2GB)..."
        SAL_URL="https://freepats.zenvoid.org/Piano/SalamanderGrandPiano/SalamanderGrandPiano-SF2-V3+20200602.tar.xz"
        TMP_TAR="$(mktemp --suffix=.tar.xz)"
        if wget -q --show-progress "$SAL_URL" -O "$TMP_TAR"; then
            TMP_DIR="$(mktemp -d)"
            tar xf "$TMP_TAR" -C "$TMP_DIR"
            SAL_SF2="$(find "$TMP_DIR" -name '*.sf2' -type f | head -1)"
            SAL_LIC="$(find "$TMP_DIR" -name 'readme.txt' -type f | head -1)"
            if [ -n "$SAL_SF2" ]; then
                mv "$SAL_SF2" "$SOUNDFONT_DIR/Salamander.sf2"
                [ -n "$SAL_LIC" ] && cp "$SAL_LIC" "$SOUNDFONT_DIR/Salamander-LICENSE.txt"
                echo -e "${GREEN}  Installed Salamander Grand Piano${NC}"
                SAL_INSTALLED=1
            else
                echo -e "${ORANGE}  Salamander archive unpacked but no .sf2 found${NC}"
            fi
            rm -rf "$TMP_DIR" "$TMP_TAR"
        else
            echo -e "${ORANGE}  Salamander download failed — falling back to FluidR3_GM${NC}"
            rm -f "$TMP_TAR"
        fi
    else
        echo -e "${GREEN}  Salamander Grand Piano already installed${NC}"
        SAL_INSTALLED=1
    fi
fi

# Fallback: FluidR3_GM (via apt `fluid-soundfont-gm`). Always present on a
# Debian-based install after the apt step above, and gives us something piano-
# capable even when Salamander isn't available (slow link, --no-salamander,
# or download failure).
if [ "$SAL_INSTALLED" != "1" ] && [ ! -f "$SOUNDFONT_DIR/FluidR3_GM.sf2" ]; then
    if [ -f /usr/share/sounds/sf2/FluidR3_GM.sf2 ]; then
        ln -sf /usr/share/sounds/sf2/FluidR3_GM.sf2 "$SOUNDFONT_DIR/FluidR3_GM.sf2"
        echo -e "${GREEN}  Symlinked FluidR3_GM from apt package${NC}"
    elif apt-cache show fluid-soundfont-gm > /dev/null 2>&1; then
        sudo apt-get install -y fluid-soundfont-gm > /dev/null 2>&1 && \
            ln -sf /usr/share/sounds/sf2/FluidR3_GM.sf2 "$SOUNDFONT_DIR/FluidR3_GM.sf2" && \
            echo -e "${GREEN}  Installed FluidR3_GM via apt${NC}" || \
            echo -e "${ORANGE}  FluidR3_GM install failed — piano will be silent${NC}"
    fi
fi

# ── Install systemd service ──
# The tracked unit file (systemd/stave-synth.service) is the single source of
# truth — `git pull` always picks up edits. The drop-in (systemd/stave-synth.
# service.d/faust.conf) is what actually toggles the Faust DSP backends on;
# without it, the synth runs the slower Python fallback and quietly loses
# Faust-only features (organ width/tone-tilt, etc).
if [ "$AUTOSTART" = "1" ]; then
    echo "  Installing systemd service..."
    mkdir -p "$HOME/.config/systemd/user/stave-synth.service.d"

    # Copy unit. Tracked file uses %h/stave-synth as WorkingDirectory; if the
    # user cloned somewhere else, sed-substitute on the way in.
    if [ "$SCRIPT_DIR" = "$HOME/stave-synth" ]; then
        cp "$SCRIPT_DIR/systemd/stave-synth.service" "$HOME/.config/systemd/user/stave-synth.service"
    else
        sed -e "s|%h/stave-synth|${SCRIPT_DIR}|g" \
            "$SCRIPT_DIR/systemd/stave-synth.service" \
            > "$HOME/.config/systemd/user/stave-synth.service"
    fi

    # Faust env-var drop-in. Without this, STAVE_FAUST_* are unset and every
    # module falls back to the Python implementation.
    cp "$SCRIPT_DIR/systemd/stave-synth.service.d/faust.conf" \
       "$HOME/.config/systemd/user/stave-synth.service.d/faust.conf"

    systemctl --user daemon-reload
    systemctl --user enable stave-synth.service

    # Enable linger so user services start at boot without login
    loginctl enable-linger "$USER" 2>/dev/null || true
else
    echo -e "${GREEN}  Skipping systemd service (--no-autostart).${NC}"
    echo -e "${GREEN}  Launch on demand with: ./stave-synth.sh${NC}"
    echo -e "${GREEN}  (Faust DSP envs are exported by stave-synth.sh — no drop-in needed)${NC}"
fi

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
if [ "$AUTOSTART" = "1" ]; then
    if systemctl --user is-enabled stave-synth.service >/dev/null 2>&1; then
        check ok "Auto-start on boot: enabled"
    else
        check no "Auto-start on boot: not enabled (run: systemctl --user enable stave-synth)"
    fi
else
    check ok "Auto-start on boot: skipped (--no-autostart mode)"
fi

echo ""
if [ "$AUTOSTART" = "1" ]; then
    echo "  To start now:    systemctl --user start stave-synth"
    echo "  Open the UI:     http://localhost:8080  (or http://<pi-ip>:8080 from another device)"
    echo "  Logs:            journalctl --user -u stave-synth -f"
    echo "  Run manually:    pw-jack $SCRIPT_DIR/venv/bin/python -m stave_synth.main"
    echo ""
    echo "  On next boot, Stave Synth will start automatically."
else
    echo "  To play:         ./stave-synth.sh"
    echo "  Open the UI:     http://localhost:8080  (or http://<pi-ip>:8080 from another device)"
    echo "  Stop it:         Ctrl-C in the terminal, or pkill -f stave_synth.main"
fi
echo ""
