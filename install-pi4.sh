#!/usr/bin/env bash
# First install and updates for a dedicated Pi4 instrument user.
set -euo pipefail

usage() {
    echo "Usage: bash install-pi4.sh [--build]"
    echo "Install the native synth for the current user on 64-bit Raspberry Pi OS Trixie."
    echo "Uses the bundled ARM64 engine if present. --build compiles from source."
    echo "Installs distro packages, sets this user's PipeWire clock to 48 kHz / 512,"
    echo "restarts this user's audio services when needed, and enables Stave at boot."
}
fail() { echo "Stave install: $*" >&2; exit 1; }
build=false
case "${1-}" in
    --build) build=true ;;
    --help|-h) usage; exit 0 ;;
    "") ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }
[[ $(uname -s) == Linux ]] || fail "Run this installer on the target Raspberry Pi."
[[ $EUID != 0 ]] || fail "Log in as the instrument user and run without sudo."
. /etc/os-release
[[ ${VERSION_CODENAME:-} == trixie && $(dpkg --print-architecture) == arm64 ]] ||
    fail "This release targets 64-bit Raspberry Pi OS Trixie (Debian 13)."
systemctl --user show-environment >/dev/null || fail "A systemd user login is required."

cd -- "$(dirname -- "$0")"
root=$(pwd -P)
base="$HOME/.local/opt/stave-pi4"
unit_dir="$HOME/.config/systemd/user"
unit="$unit_dir/stave-native.service"
unit_src="$root/systemd/stave-pi4-share.service"
clock_dir="$HOME/.config/pipewire/pipewire.conf.d"
clock_conf="$clock_dir/52-stave-pi4-clock.conf"
clock_src="$root/config/pi4/52-stave-pi4-clock.conf"
state="$HOME/.local/share/stave-native"
[[ -f "$unit_src" && -f "$clock_src" ]] || fail "Incomplete Stave distribution."

# Guard against conflicting installs, including the original gigging instrument.
# A lock covers package setup through activation for concurrent installer calls.
mkdir -p "$base"
exec 9>"$base/.install-lock"
flock -n 9 || fail "Another installer is running."
fragment=$(systemctl --user show stave-native.service -p FragmentPath --value || true)
if [[ -n $fragment && $fragment != "$unit" ]]; then
    fail "A different stave-native.service is already installed at $fragment."
fi
if [[ -L $unit ]] || { [[ -e $unit ]] && ! cmp -s "$unit" "$base/current/service.unit"; }; then
    fail "Existing $unit belongs to another installation."
fi
drop_ins=$(systemctl --user show stave-native.service -p DropInPaths --value || true)
if [[ -n $drop_ins || -d "$unit.d" ]]; then
    fail "Existing user service overrides for stave-native.service were found; preserved."
fi
for owner in stave-synth.service stave-native-v2-candidate.service; do
    if systemctl --user is-active --quiet "$owner" || systemctl --user is-enabled --quiet "$owner"; then
        fail "$owner is active or enabled. Use a fresh instrument user."
    fi
done
if [[ ! -e "$base/.managed" && ( -e "$state" || -e "$base/current" || -L "$base/current" ) ]]; then
    fail "Existing instrument files were found. Use a fresh instrument user."
fi
if [[ ! -e "$base/.managed" ]]; then
    /usr/bin/python3 -c 'import socket; port = socket.socket(); port.bind(("0.0.0.0", 8082)); port.close()' ||
        fail "Port 8082 is in use. The existing listener was preserved."
fi
if [[ -e $clock_conf || -L $clock_conf ]]; then
    cmp -s "$clock_src" "$clock_conf" || fail "A different PipeWire profile already exists at $clock_conf."
fi

prebuilt="$root/bin/audition-jack"
if [[ ! -f $prebuilt ]]; then build=true; fi
packages=(pipewire pipewire-jack wireplumber jack-example-tools libfluidsynth3
          fluid-soundfont-gm python3-numpy python3-scipy alsa-utils iproute2
          avahi-daemon curl libspa-0.2-modules)
if "$build"; then
    packages+=(build-essential pkg-config faust libfluidsynth-dev libjack-jackd2-dev)
fi
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
[[ -r /usr/share/sounds/sf2/FluidR3_GM.sf2 ]] || fail "FluidR3 GM soundfont is missing."
/usr/bin/python3 -c 'import numpy, scipy.signal'

mkdir -p "$base/releases" "$unit_dir" "$clock_dir"
temp=$(mktemp -d "$base/.install.XXXXXXXX")
trap 'rm -rf -- "$temp"' EXIT
stage="$temp/runtime"
mkdir -p "$stage/bin"
files=(tools/native_v2_stage.py tools/native_v2_audition.py tools/native_v2_lifecycle.py
       native_v2/audition.html native_v2/bed_assets.py native_v2/control_store.py
       stave_synth/state_store.py ui/logo.png LICENSE THIRD_PARTY_NOTICES.txt)
for rel in "${files[@]}"; do
    install -D -m 644 "$root/$rel" "$stage/$rel"
done
if "$build"; then
    /usr/bin/python3 "$root/tools/build_pi4_native.py" "$stage/bin/audition-jack"
else
    install -m 755 "$prebuilt" "$stage/bin/audition-jack"
fi
linkage=$(ldd "$stage/bin/audition-jack") || fail "Native binary is incompatible. Try --build."
[[ $linkage != *"not found"* ]] || fail "Missing native libraries: $linkage"
# Exercise the actual import graph before switching any installed files.
/usr/bin/python3 -B "$stage/tools/native_v2_stage.py" --help >/dev/null

install -m 644 "$unit_src" "$stage/service.unit"
release_id=$(cd "$stage" && sha256sum service.unit bin/audition-jack "${files[@]}" | sha256sum | cut -d' ' -f1)
release="$base/releases/$release_id"
if [[ ! -d $release ]]; then mv -- "$stage" "$release"; fi
old_release=$(readlink "$base/current" || true)
# Headless boots need both a lingering user manager and device access.
change_group=false
if ! id -nG | tr ' ' '\n' | grep -Fxq audio; then
    sudo usermod -aG audio "$(id -un)"
    change_group=true
fi
sudo loginctl enable-linger "$(id -un)"
sudo systemctl enable --now avahi-daemon.service
was_active=false
was_enabled=false
if systemctl --user is-active --quiet stave-native.service; then was_active=true; fi
if systemctl --user is-enabled --quiet stave-native.service; then was_enabled=true; fi
if [[ -f $unit ]]; then cp "$unit" "$temp/previous.unit"; fi
created_clock=false
if [[ ! -e $clock_conf ]]; then created_clock=true; fi

# On any failure after switching files, restore the previous runtime and
# service state. The fresh install keeps its log and user data for a retry.
rollback() {
    status=$?
    trap - EXIT
    set +e
    if [[ $status != 0 ]]; then
        systemctl --user stop stave-native.service >/dev/null 2>&1
        if ! "$was_enabled"; then systemctl --user disable stave-native.service >/dev/null 2>&1; fi
        if [[ -f $temp/previous.unit ]]; then
            install -m 644 "$temp/previous.unit" "$unit"
        else
            rm -f -- "$unit"
        fi
        if [[ -n $old_release ]]; then
            ln -s "$old_release" "$temp/previous"
            mv -Tf -- "$temp/previous" "$base/current"
        else
            rm -f -- "$base/current"
        fi
        systemctl --user daemon-reload
        if "$created_clock"; then
            rm -f -- "$clock_conf" "$base/.clock-loaded"
            systemctl --user restart pipewire.service wireplumber.service
        fi
        if "$was_enabled"; then systemctl --user enable stave-native.service; fi
        if "$was_active"; then systemctl --user start stave-native.service; fi
        echo "Installation failed. Restored prior Stave activation files. See journalctl --user -u stave-native.service." >&2
    fi
    rm -rf -- "$temp"
    exit "$status"
}
trap rollback EXIT
touch "$base/.managed"
ln -s "$release" "$temp/current"
mv -Tf -- "$temp/current" "$base/current"
if ! cmp -s "$unit_src" "$unit"; then
    install -m 644 "$unit_src" "$unit.tmp"
    mv -f -- "$unit.tmp" "$unit"
fi
if "$created_clock"; then install -m 644 "$clock_src" "$clock_conf"; fi
systemctl --user daemon-reload
systemctl --user enable --now pipewire.socket wireplumber.service
if [[ ! -f "$base/.clock-loaded" ]]; then
    systemctl --user restart pipewire.service wireplumber.service
    touch "$base/.clock-loaded"
fi
systemctl --user enable stave-native.service
activate() {
    if [[ "$old_release" != "$release" ]]; then
        systemctl --user restart stave-native.service || return 1
    else
        systemctl --user start stave-native.service || return 1
    fi
    for attempt in {1..20}; do
        if curl -fs --noproxy '*' --max-time 2 http://127.0.0.1:8082/status |
            /usr/bin/python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("instance")=="native-v2-stage" and "127.0.0.1" in s["runtime"]["network"]["addresses"]' 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}
if ! activate; then
    fail "Startup check failed. See journalctl --user -u stave-native.service -n 40."
fi
printf 'Stave is running at http://%s.local:8082\n' "$(hostname -s)"
echo "In System, select one MIDI input and the left and right audio outputs."
if "$change_group"; then
    echo "Reboot the Pi once to give the instrument user access to headless audio devices."
fi
