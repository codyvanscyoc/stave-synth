#!/usr/bin/env bash
# Compile Faust .dsp files → C → shared library.
# Build dependency: `faust` + `gcc`. Both installed by install.sh.
#
# Modules whose .so is newer than the .dsp are skipped. Pass --force to
# rebuild everything (e.g. after changing CFLAGS or the prelude header).

set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -f|--force) FORCE=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

CFLAGS="-shared -fPIC -O3 -ffast-math -include $(pwd)/faust_cprelude.h"

build_module() {
    local name=$1       # dsp file stem (no extension)
    local cname=$2      # C class name passed to faust -cn
    local libname=$3    # output library stem (libNAME.so)

    local out="lib${libname}.so"
    if [ "$FORCE" -eq 0 ] && [ -f "$out" ] && [ "$out" -nt "${name}.dsp" ] && [ "$out" -nt "faust_cprelude.h" ]; then
        echo "─── $name → $out  (up-to-date, skip)"
        return
    fi
    echo "─── $name → $out ───"
    # NOTE: tried `faust -vec` for SIMD vectorization but gcc's optimizer
    # OOMed the Pi 5 trying to compile the unrolled 16-voice osc_bank code
    # (5+ min, 1.3GB RAM, didn't finish). Sticking with scalar.
    faust -lang c -cn "$cname" -o "${name}.c" "${name}.dsp"
    gcc $CFLAGS -o "$out" "${name}.c"
    ls -la "$out"
}

build_module gain         StaveGain         stave_gain
build_module reverb       StaveReverb       stave_reverb
build_module ping_pong    StavePingPong     stave_ping_pong
build_module osc_bank     StaveOscBank      stave_osc_bank
build_module sympathetic  StaveSympathetic  stave_sympathetic
build_module master_fx    StaveMasterFX     stave_master_fx
build_module bus_comp     StaveBusComp      stave_bus_comp
build_module organ        StaveOrgan         stave_organ
build_module plate        StavePlate         stave_plate
build_module drone        StaveDrone         stave_drone
build_module piano_room   StavePianoRoom     stave_piano_room

echo
echo "Faust modules built. Restart the synth to pick up changes."
