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
if [[ -e $unit || -L $unit ]]; then
    [[ -f "$base/.managed" && ! -L $unit ]] || fail "Existing $unit belongs to another installation."
    if ! cmp -s "$unit" "$base/current/service.unit" && ! cmp -s "$unit" "$unit_src"; then
        fail "Existing $unit belongs to another installation."
    fi
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
    install -m 644 "$prebuilt.build.json" "$stage/bin/audition-jack.build.json"
fi
/usr/bin/python3 - "$stage/bin/audition-jack" <<'PYTHON'
import hashlib, json, sys
from pathlib import Path
engine = Path(sys.argv[1])
record = json.loads(engine.with_name(engine.name + ".build.json").read_bytes())
if record.get("format") != 1 or record.get("binary_sha256") != hashlib.sha256(engine.read_bytes()).hexdigest():
    raise SystemExit("Stave binary does not match its build receipt.")
PYTHON
linkage=$(ldd "$stage/bin/audition-jack") || fail "Native binary is incompatible. Try --build."
[[ $linkage != *"not found"* ]] || fail "Missing native libraries: $linkage"
# Exercise the actual import graph before switching any installed files.
/usr/bin/python3 -B "$stage/tools/native_v2_stage.py" --help >/dev/null

install -m 644 "$unit_src" "$stage/service.unit"
expected_files=(service.unit bin/audition-jack bin/audition-jack.build.json "${files[@]}")
digest_runtime() {
    local rel
    [[ -x "$1/bin/audition-jack" ]] || return 1
    for rel in "${expected_files[@]}"; do
        [[ -f "$1/$rel" && ! -L "$1/$rel" ]] || return 1
    done
    (cd "$1" && sha256sum "${expected_files[@]}" | sha256sum | cut -d' ' -f1)
}
release_id=$(digest_runtime "$stage")
release="$base/releases/$release_id"
old_release=$(readlink "$base/current" || true)
# A damaged release may still be running. Stage its repair in a fresh
# directory so the current process retains its files until the atomic switch.
if [[ -n $old_release && $old_release == "$base/releases/"* && -d $old_release ]] &&
        [[ $(digest_runtime "$old_release" 2>/dev/null || true) == "$release_id" ]]; then
    release=$old_release
elif [[ ! -d $release ]]; then
    mv -- "$stage" "$release"
elif [[ $(digest_runtime "$release" 2>/dev/null || true) != "$release_id" ]]; then
    release=$(mktemp -d "$base/releases/$release_id.repair.XXXXXXXX")
    mv -T -- "$stage" "$release"
fi
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
        if curl -fs --noproxy '*' --max-time 2 http://127.0.0.1:8082/status > "$temp/status.json" &&
            /usr/bin/python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("instance")=="native-v2-stage" and "127.0.0.1" in s["runtime"]["network"]["addresses"]' < "$temp/status.json" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}
if ! activate; then
    fail "Startup check failed. See journalctl --user -u stave-native.service -n 40."
fi
/usr/bin/python3 - "$temp/status.json" "$(hostname -s)" <<'PYTHON'
import ipaddress, json, sys
with open(sys.argv[1]) as stream:
    network = json.load(stream)["runtime"]["network"]
lan = [address for address in network["addresses"] if not ipaddress.ip_address(address).is_loopback]
if lan:
    for address in lan:
        print(f"Open http://{address}:8082 on the same LAN.")
    print(f"Bonjour address: http://{sys.argv[2]}.local:8082")
else:
    print("Stave is running on http://127.0.0.1:8082 on the Pi.")
    print("For another device, connect wlan0 or eth0 to a network with a private IPv4 address.")
if network.get("discovery_error"):
    print("Network discovery:", network["discovery_error"])
for address, error in network.get("errors", {}).items():
    print(f"Listener {address}: {error}")
PYTHON
echo "In System, select one MIDI input and the left and right audio outputs."
if "$change_group"; then
    echo "Reboot the Pi once to give the instrument user access to headless audio devices."
fi
