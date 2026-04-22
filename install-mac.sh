#!/bin/bash
# Stave Synth — macOS installer (Homebrew-based).
# Run: ./install-mac.sh [--no-salamander]
#
# Differs from install.sh (Linux) in several important ways:
#   - No systemd. Launch via ./stave-synth-mac.sh or a future .app bundle.
#   - No pipewire / jackd / a2jmidid. Mac uses sounddevice (PortAudio) +
#     python-rtmidi (Core MIDI) directly. No audio server to manage.
#   - No CPU governor / USB autosuspend tweaks. Core Audio and macOS
#     power management handle this correctly out of the box.
#   - No C bridge build. jack_bridge.so is Linux-only; MacPortAudioIO
#     in stave_synth/audio_io/mac_portaudio.py takes its place.
#   - Paths follow Apple HIG: ~/Library/Application Support/stave-synth/.

set -e

INSTALL_SALAMANDER=1
for arg in "$@"; do
    case "$arg" in
        --no-salamander) INSTALL_SALAMANDER=0 ;;
        -h|--help)
            echo "Usage: ./install-mac.sh [--no-salamander]"
            echo "  --no-salamander   Skip Salamander Grand Piano download (~296MB)."
            exit 0
            ;;
    esac
done

GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'

echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  STAVE SYNTH — macOS Installer${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/stave-synth"
SOUNDFONT_DIR="$APP_DIR/soundfonts"
PRESETS_DIR="$APP_DIR/presets"

# ── Step 1: Homebrew ──
echo -e "${ORANGE}[1/5]${NC} Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Install from https://brew.sh first, then re-run."
    exit 1
fi
echo -e "${GREEN}  Homebrew present.${NC}"

# ── Step 2: System dependencies via Brewfile ──
echo -e "${ORANGE}[2/5]${NC} Installing system dependencies (brew bundle)..."
cd "$SCRIPT_DIR"
brew bundle --file=Brewfile
echo -e "${GREEN}  System deps installed.${NC}"

# ── Step 3: Python venv + deps ──
echo -e "${ORANGE}[3/5]${NC} Setting up Python venv..."
if [ ! -d "venv" ]; then
    # Homebrew Python is the source of truth on Mac. python3 resolves to
    # /opt/homebrew/bin/python3 on Apple Silicon, /usr/local/... on Intel.
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install -r requirements-mac.txt -q
# pywebview on Mac uses WKWebView via pyobjc-framework-WebKit. Include it.
pip install -r requirements-gui.txt -q || \
    echo -e "${ORANGE}  (pywebview install hiccup — browser UI at :8080 still works)${NC}"
echo -e "${GREEN}  Python deps installed.${NC}"

# ── Step 4: Build Faust DSP modules (→ .dylib) ──
echo -e "${ORANGE}[4/5]${NC} Building Faust DSP modules..."
cd "$SCRIPT_DIR/faust"
./build.sh > /dev/null
cd "$SCRIPT_DIR"
echo -e "${GREEN}  Built Faust modules.${NC}"

# ── Step 5: App Support dirs + soundfont ──
echo -e "${ORANGE}[5/5]${NC} Setting up Application Support directory..."
mkdir -p "$SOUNDFONT_DIR" "$PRESETS_DIR"
echo -e "${GREEN}  $APP_DIR${NC}"

# Salamander Grand Piano — same source as the Linux installer.
SAL_INSTALLED=0
if [ "$INSTALL_SALAMANDER" = "1" ]; then
    if [ ! -f "$SOUNDFONT_DIR/Salamander.sf2" ]; then
        echo "  Downloading Salamander Grand Piano (~296MB → 1.2GB on disk)..."
        SAL_URL="https://freepats.zenvoid.org/Piano/SalamanderGrandPiano/SalamanderGrandPiano-SF2-V3+20200602.tar.xz"
        TMP_TAR="$(mktemp -t salamander).tar.xz"
        if curl -fsSL "$SAL_URL" -o "$TMP_TAR"; then
            TMP_DIR="$(mktemp -d)"
            tar xf "$TMP_TAR" -C "$TMP_DIR"
            SAL_SF2="$(find "$TMP_DIR" -name '*.sf2' -type f | head -1)"
            SAL_LIC="$(find "$TMP_DIR" -name 'readme.txt' -type f | head -1)"
            if [ -n "$SAL_SF2" ]; then
                mv "$SAL_SF2" "$SOUNDFONT_DIR/Salamander.sf2"
                [ -n "$SAL_LIC" ] && cp "$SAL_LIC" "$SOUNDFONT_DIR/Salamander-LICENSE.txt"
                echo -e "${GREEN}  Installed Salamander Grand Piano.${NC}"
                SAL_INSTALLED=1
            fi
            rm -rf "$TMP_DIR" "$TMP_TAR"
        else
            echo -e "${ORANGE}  Salamander download failed.${NC}"
            rm -f "$TMP_TAR"
        fi
    else
        echo -e "${GREEN}  Salamander already installed.${NC}"
        SAL_INSTALLED=1
    fi
fi

# Fallback soundfont: Homebrew ships FluidR3_GM with fluidsynth.
if [ "$SAL_INSTALLED" != "1" ] && [ ! -f "$SOUNDFONT_DIR/FluidR3_GM.sf2" ]; then
    for cand in /opt/homebrew/share/sounds/sf2/FluidR3_GM.sf2 \
                /usr/local/share/sounds/sf2/FluidR3_GM.sf2 \
                /opt/homebrew/share/fluid-soundfont/FluidR3_GM.sf2 \
                /usr/local/share/fluid-soundfont/FluidR3_GM.sf2; do
        if [ -f "$cand" ]; then
            ln -sf "$cand" "$SOUNDFONT_DIR/FluidR3_GM.sf2"
            echo -e "${GREEN}  Symlinked FluidR3_GM from Homebrew: $cand${NC}"
            break
        fi
    done
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""
echo "  To start:        ./stave-synth-mac.sh"
echo "  Open the UI:     http://localhost:8080"
echo "  Stop it:         Ctrl-C in the terminal, or pkill -f stave_synth.main"
echo ""
echo "  Future .app bundle (py2app) will launch on double-click; see MAC_PORT.md."
echo ""
