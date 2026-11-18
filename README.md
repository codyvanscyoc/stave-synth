# Stave Synth for Raspberry Pi 4

Play a layered piano and two oscillators from a USB MIDI keyboard. A phone, tablet, or laptop on the same Wi-Fi controls the mix, filters, effects, recorded pads, five sound presets and MIDI Learn. The Pi runs the audio engine, so the instrument keeps playing when the browser closes.

## Install the Pi 4 beta

Use a Raspberry Pi 4 with **64-bit Raspberry Pi OS Trixie (Debian 13)**, internet access for the first install, and a regular login with sudo access. Use a dedicated instrument login. The installer sets that user’s PipeWire audio to 48 kHz and 512 frames, restarts its audio session on first setup, and enables the synth at boot.

On the Pi, run these commands in a new directory. They fetch the ARM64 bundle and verify its download before installation. You can also download the same two files under **Assets** on the [Pi 4 beta 3 release page](https://github.com/codyvanscyoc/stave-synth/releases/tag/pi4-native-2026.11.18-beta.3).

```sh
mkdir -p ~/Downloads/stave-pi4-beta
cd ~/Downloads/stave-pi4-beta
release=https://github.com/codyvanscyoc/stave-synth/releases/download/pi4-native-2026.11.18-beta.3
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
3. On first use, **Soundcheck** opens. Choose your keyboard and stereo output, then tap **Apply connections**. For separate left and right ports, expand **Individual output ports**. Press a key and look for its note, velocity and channel.
4. Master starts at zero. Raise it slowly with the speakers turned down, then use **Test left** and **Test right** to verify the wiring. The meters show the output after Master. For the Pi’s analog jack, check **System → Pi headphone output**.
5. Tap **Open Stage**, bring up **Piano**, and play. **Save startup** remembers the power-on sound, including the Master level. It returns when the selected audio route is ready.

Anyone who can reach the private-LAN UI can control the instrument. Use trusted Wi-Fi or Ethernet. The default service listens on loopback and the private IPv4 addresses of `wlan0` and `eth0`.

## Sounds, recordings and maintenance

**Stage.** In tablet landscape, the transpose and layer controls share a row above the faders. The twelve pad keys and Stop are on the same screen. Soundcheck shows the names of any disconnected devices. On phones, **Layers** puts both oscillators, Piano and Master together. **Filter / FX** opens the other mix controls. Master and Mute stay within reach. Mute silences the output while keeping the mix level. Panic releases notes, pedals, freeze and pads and mutes the output. Check Master before unmuting.

**Presets.** Tap a named slot to recall it. To store the sound you are playing, use **Store preset…**, choose a destination, enter a name and confirm. Selecting a destination keeps the current sound live. The Stage readout shows edits to the active preset and whether the sound differs from startup.

**Sound and MIDI.** Shape the piano, oscillators and effects under **Sound**. In **Delay**, use **Tap tempo** or enter BPM manually. In **Soundcheck**, set a receive channel, minimum note velocity and hardware pickup. With pickup enabled, moving a mapped fader past its current value hands control to the hardware. To assign a control, open **MIDI MAP**, choose the target, move one knob or fader, then tap **Done**. Clear old assignments in **System → MIDI assignments**.

**Pads.** Open **PAD RECORD / SHAPE**, record up to 40 seconds, stop, choose a key and use **Save pad**. The display shows remaining bank time. Replacing a pad frees its old space. An accepted assignment restarts audio briefly, so save between songs. An oversized assignment leaves the current bank playing. If an existing bank cannot load, piano and oscillators remain available.

Settings, presets, MIDI assignments, recordings and pad samples live under `~/.local/share/stave-native/`. Back up that directory to keep your sounds. The runtime lives under `~/.local/opt/stave-pi4/`. Run the new bundle’s installer to update an installation made by this installer. If activation fails during the update, the installer restores the previous runtime and service configuration.

```sh
systemctl --user status stave-native.service
journalctl --user -u stave-native.service -n 50
pw-jack jack_lsp -t -p
```

The last command lists the ports available to the instrument. If only “MIDI Through” appears, check the keyboard’s USB connection and MIDI mode. If the service starts but the browser cannot connect, check the hostname, port, network and Wi-Fi client isolation. To stop automatic startup and silence the service, run `systemctl --user disable --now stave-native.service`. To enable it again, run `systemctl --user enable --now stave-native.service`.

The [Pi 4 review](docs/pi4/SHARE_REVIEW.md) records what was tested and outstanding hardware checks. The full source repository also contains [historical documentation for the earlier engine](https://github.com/codyvanscyoc/stave-synth/blob/pi4-shareable-native/README-legacy.md).
