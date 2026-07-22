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

# Tune for the machine we're building ON — .so files are built per-device by
# install.sh (never shipped), so -mcpu=native is safe and buys real NEON
# autovectorization on DSP loops (measured win on Pi 4's A72). Guarded so
# exotic toolchains without the flag still build.
if echo 'int main(){return 0;}' | gcc -mcpu=native -x c - -o /dev/null 2>/dev/null; then
    CFLAGS="$CFLAGS -mcpu=native"
    echo "── native CPU tuning enabled (-mcpu=native)"
fi

build_module() {
    local name=$1       # dsp file stem (no extension)
    local cname=$2      # C class name passed to faust -cn
    local libname=$3    # output library stem (libNAME.so)
    local faust_extra="${4:-}"   # optional extra faust flags (e.g. -double)
    local cc_extra="${5:-}"      # optional extra gcc flags (e.g. -DFAUSTFLOAT=double)

    local out="lib${libname}.so"
    if [ "$FORCE" -eq 0 ] && [ -f "$out" ] && [ "$out" -nt "${name}.dsp" ] && [ "$out" -nt "faust_cprelude.h" ]; then
        echo "─── $name → $out  (up-to-date, skip)"
        return
    fi
    echo "─── $name → $out ───"
    # NOTE: tried `faust -vec` for SIMD vectorization but gcc's optimizer
    # OOMed the Pi 5 trying to compile the unrolled 16-voice osc_bank code
    # (5+ min, 1.3GB RAM, didn't finish). Sticking with scalar.
    faust -lang c $faust_extra -cn "$cname" -o "${name}.c" "${name}.dsp"
    gcc $CFLAGS $cc_extra -o "$out" "${name}.c"
    ls -la "$out"
}

# Lite variant: rewrite the compile-time slot constant (24 → 12) in a temp
# copy of the .dsp and build lib<libname>_lite.so with the SAME class name
# (Python cdefs / symbol names stay identical; only the voice count shrinks).
# Loaded by the wrappers when config.LOW_RAM_MODE is set (Pi 4 / 2GB).
# The normal 24-slot builds above are untouched.
build_lite_module() {
    local name=$1       # dsp file stem (no extension)
    local cname=$2      # C class name passed to faust -cn (same as full build)
    local libname=$3    # full-build library stem; lite output is lib${libname}_lite.so
    local pattern=$4    # exact constant line to rewrite, e.g. 'NVOICES = 24;'
    local replace=$5    # replacement line, e.g. 'NVOICES = 12;'

    local out="lib${libname}_lite.so"
    if [ "$FORCE" -eq 0 ] && [ -f "$out" ] && [ "$out" -nt "${name}.dsp" ] && [ "$out" -nt "faust_cprelude.h" ]; then
        echo "─── $name (lite) → $out  (up-to-date, skip)"
        return
    fi
    echo "─── $name (lite) → $out ───"

    # Safety: the pattern must match exactly one line, or the sed rewrite
    # would silently build a wrong-sized (or unchanged) bank.
    local matches
    matches=$(grep -cF "$pattern" "${name}.dsp")
    if [ "$matches" -ne 1 ]; then
        echo "ERROR: expected exactly 1 line matching '$pattern' in ${name}.dsp, found $matches" >&2
        exit 1
    fi

    local tmp="${name}_lite"
    sed "s/^${pattern}$/${replace}/" "${name}.dsp" > "${tmp}.dsp"
    if ! grep -qF "$replace" "${tmp}.dsp"; then
        echo "ERROR: sed rewrite '$pattern' → '$replace' did not apply in ${tmp}.dsp" >&2
        rm -f "${tmp}.dsp"
        exit 1
    fi
    faust -lang c -cn "$cname" -o "${tmp}.c" "${tmp}.dsp"
    gcc $CFLAGS -o "$out" "${tmp}.c"
    rm -f "${tmp}.dsp" "${tmp}.c"
    ls -la "$out"
}

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
# pad_bus builds in DOUBLE precision (-double + FAUSTFLOAT=double): its
# pole-near-unity biquads at low cutoffs can't hold the ~1e-6 parity bar
# in float32, and double zones/IO let faust_pad_bus.py pass the engine's
# float64 blocks zero-copy. No per-voice state → no lite variant.
build_module pad_bus      StavePadBus        stave_pad_bus  "-double"  "-DFAUSTFLOAT=double"
# piano_chain (Phase 3) also builds in DOUBLE precision — same rationale:
# the piano EQ's low-frequency bells (150-300 Hz) and 40 Hz low cut are
# pole-near-unity biquads that can't hold ~1e-6 parity in float32, and
# double IO lets faust_piano_chain.py pass float64 blocks zero-copy.
# No per-voice state → no lite variant.
build_module piano_chain  StavePianoChain    stave_piano_chain  "-double"  "-DFAUSTFLOAT=double"

# 12-slot lite variants for low-RAM boxes (config.LOW_RAM_MODE)
build_lite_module osc_bank     StaveOscBank      stave_osc_bank     'NVOICES = 24;' 'NVOICES = 12;'
build_lite_module sympathetic  StaveSympathetic  stave_sympathetic  'N_SLOTS = 24;' 'N_SLOTS = 12;'

echo
echo "Faust modules built. Restart the synth to pick up changes."
