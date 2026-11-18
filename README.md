# Stave Synth for Raspberry Pi 4

Play a layered piano and two oscillators from a USB MIDI keyboard. A phone, tablet, or laptop on the same Wi-Fi controls the mix, filters, effects, recorded pads, five sound presets and MIDI Learn. The Pi runs the audio engine, so the instrument keeps playing when the browser closes.

## Install the Pi 4 beta

Use a Raspberry Pi 4 with **64-bit Raspberry Pi OS Trixie (Debian 13)**, internet access for the first install, and a regular login with sudo access. Use a dedicated instrument login. The installer sets that user’s PipeWire audio to 48 kHz and 512 frames, restarts its audio session on first setup, and enables the synth at boot.

On the Pi, run these commands in a new directory. They fetch the ARM64 bundle and verify its download before installation.

```sh
mkdir -p ~/Downloads/stave-pi4-beta
cd ~/Downloads/stave-pi4-beta
release=https://github.com/codyvanscyoc/stave-synth/releases/download/pi4-native-2026.11.18-beta.1
curl -fLO "$release/stave-synth-pi4-arm64.tar.gz"
curl -fLO "$release/SHA256SUMS"
sha256sum -c SHA256SUMS &&
  tar -xzf stave-synth-pi4-arm64.tar.gz &&
  bash stave-synth-pi4/install-pi4.sh
```

Run the installer as your normal user. It uses sudo for system packages, audio device access and boot setup. If it asks for a reboot after adding audio permissions, reboot once. For a source build, use `bash install-pi4.sh --build` from inside the extracted folder. The Debian package manager supplies the FluidR3 GM piano sound bank.

## Play your first note

1. Connect a USB MIDI keyboard and a stereo audio interface or the Pi’s enabled onboard audio. Set your speaker or headphone volume low.
2. Open `http://<your-pi-hostname>.local:8082` on the same trusted network. For a Pi named `raspberrypi`, use `http://raspberrypi.local:8082`. If the name does not resolve, run `hostname -I` on the Pi and open its LAN IPv4 address with port `8082`.
3. Open **System → Connections**. Choose the keyboard’s MIDI capture port and distinct left/right playback ports for the output you want. Click **Apply connections**. With the hardware unplugged, you can still edit sounds in the browser.
4. In **Stage**, bring up **Piano** and slowly raise **Master** while playing. A fresh installation starts at zero master gain. **Save sound** stores the startup mix, including Master, which restores after the selected ports reconnect. For the Pi’s analog jack, also check **System → Pi headphone output**.

Anyone who can reach the private-LAN UI can control the instrument. Use trusted Wi-Fi or Ethernet. The default service listens on loopback and the private IPv4 addresses of `wlan0` and `eth0`.

## Sounds, recordings and maintenance

**Stage** holds the live mix. **Edit** adjusts the oscillators, piano, filters and effects. **MIDI MAP** assigns controls to a keyboard’s CC messages. Recorded-pad slots start empty. Use **PAD RECORD / SHAPE** to record a take, choose a key and save it. Each take holds up to 40 seconds. Saving a pad restarts the audio engine to load it, so do this between songs. Use **System** to select new hardware when you change keyboards or interfaces.

Settings, presets, MIDI assignments, recordings and pad samples live under `~/.local/share/stave-native/`. Back up that directory to keep your sounds. The runtime lives under `~/.local/opt/stave-pi4/`. Run the new bundle’s installer to update an installation made by this installer.

```sh
systemctl --user status stave-native.service
journalctl --user -u stave-native.service -n 50
pw-jack jack_lsp -t -p
```

The last command lists the ports available to the instrument. If only “MIDI Through” appears, check the keyboard’s USB connection and MIDI mode. If the service starts but the browser cannot connect, check the hostname, port, network and Wi-Fi client isolation. To stop automatic startup and silence the service, run `systemctl --user disable --now stave-native.service`. To enable it again, run `systemctl --user enable --now stave-native.service`.

The [Pi 4 review](docs/pi4/SHARE_REVIEW.md) records what was tested and outstanding hardware checks. The full source repository also contains [historical documentation for the earlier engine](https://github.com/codyvanscyoc/stave-synth/blob/pi4-shareable-native/README-legacy.md).
