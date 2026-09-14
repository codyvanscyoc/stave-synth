#!/bin/bash
# Stave Synth launcher. Never kills another instance or changes a global mixer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--stage" ]]; then
    export STAVE_INSTANCE=stage
    shift
elif [[ "${STAVE_INSTANCE:-stage}" == "stage" ]]; then
    echo "Choose --stage explicitly, or configure a non-stage STAVE_INSTANCE for isolated testing." >&2
    exit 2
fi

STAVE_PYTHON="${STAVE_PYTHON:-./venv/bin/python}"
# Validate identity and paths before any application or device activity.
"$STAVE_PYTHON" -m stave_synth.runtime --describe

# Faust DSP backends. Mirrors systemd/stave-synth.service.d/faust.conf so the
# casual launcher and the systemd path enable the same modules. Pre-set in
# the environment? We respect that — only export defaults for unset vars.
: "${STAVE_FAUST_REVERB:=1}"
: "${STAVE_FAUST_PING_PONG:=1}"
: "${STAVE_FAUST_OSC_BANK:=1}"
: "${STAVE_FAUST_SYMPATHETIC:=1}"
: "${STAVE_FAUST_MASTER_FX:=1}"
: "${STAVE_FAUST_BUS_COMP:=1}"
: "${STAVE_FAUST_ORGAN:=1}"
: "${STAVE_FAUST_PAD_BUS:=1}"
: "${STAVE_FAUST_PIANO_CHAIN:=1}"
: "${STAVE_FAUST_MERGED:=1}"
export STAVE_FAUST_REVERB STAVE_FAUST_PING_PONG STAVE_FAUST_OSC_BANK \
       STAVE_FAUST_SYMPATHETIC STAVE_FAUST_MASTER_FX STAVE_FAUST_BUS_COMP \
       STAVE_FAUST_ORGAN STAVE_FAUST_PAD_BUS STAVE_FAUST_PIANO_CHAIN STAVE_FAUST_MERGED

exec pw-jack "$STAVE_PYTHON" -m stave_synth.main --no-gui "$@"
