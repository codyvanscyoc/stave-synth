#!/usr/bin/env bash
# Compile Faust .dsp files → C → shared library.
# Build dependency: `faust` + `gcc`. Both installed by install.sh.

set -euo pipefail
cd "$(dirname "$0")"

CFLAGS="-shared -fPIC -O3 -ffast-math -include $(pwd)/faust_cprelude.h"

build_module() {
    local name=$1       # dsp file stem (no extension)
    local cname=$2      # C class name passed to faust -cn
    local libname=$3    # output library stem (libNAME.so)

    echo "─── $name → lib${libname}.so ───"
    faust -lang c -cn "$cname" -o "${name}.c" "${name}.dsp"
    gcc $CFLAGS -o "lib${libname}.so" "${name}.c"
    ls -la "lib${libname}.so"
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

echo
echo "Faust modules built. Restart the synth to pick up changes."
