# Raspberry Pi 4 native release review

Reviewed November 18, 2026 for the first downloadable Pi 4 native beta. The release targets a Raspberry Pi 4 running 64-bit Raspberry Pi OS Trixie. The test instrument was a Pi 4 Model B Rev 1.5 with about 2 GB RAM and a Debian 13.6 ARM64 userland.

## Matching the stage instrument

SSH host-key verification, systemd, the active launcher, listening sockets and the served HTML identified the native service on port 8082. Of 61 deployed source files checked against native branch commit `5965e5b`, three differed. All three supplied the Pi-only analog headphone control, including its mixer access, HTTP routes and browser controls. The release incorporates those changes and their separately recovered hardware-mocked tests. The release was built in a separate directory. The stage service continued running with its original sound, routes and files.

## Findings and release changes

| Area | Finding | Release behavior |
| --- | --- | --- |
| First install | The stage unit referenced a separate checkout’s virtual environment, Yamaha-specific ports and a fixed hostname. The top-level installer targeted an older application. | A dedicated installer uses the system Python, installs the required packages, sets up the native user service and uses the recipient’s hostname. First run opens the device selector. |
| Routes and storage | Loading a damaged sound snapshot could prevent loading a valid saved route. | Device selection is restored independently. Missing hardware leaves tone editing available. Master starts at zero on a fresh installation. |
| Preset recall | Linked controls, old filter limits and a full control queue could alter or partially apply a recalled preset. | The controller restores defaults for sparse presets, applies independent values in order and admits a preset only when the queue has room for the whole change. |
| Headphones | The GitHub native branch lacked the controls present on the Pi. A failed mixer write could leave the slider unusable until reopening System. | The web controls operate the Pi Headphones mixer and show its actual state after a failed write. A disk-save failure is reported separately. |
| Recording | A filename collision could delete a partial take owned by another writer. A healthy recording at the duration limit was discarded. | Exclusive file ownership is preserved. A take that reaches the 40-second limit is finalized and can be assigned to a pad. |
| Distribution | The working sound bank, user sessions and stage settings lived outside the repository. | The bundle includes the compiled ARM64 engine, corresponding build inputs, controller, UI, installer and license notices. APT supplies FluidR3 GM. Personal recordings, presets, routes and network settings stay on the original Pi. |

## What was exercised

- **Native sound and MIDI.** Device-free tests covered note and pedal handling, oscillators, piano, stage routing, recording, bounded control delivery and graph callbacks. The 256- and 512-frame offline renders using FluidR3 produced matching peak and RMS levels with zero missed note-offs. Four full-graph guard programs passed using a fake JACK driver and the native C++ code. The release service uses 48 kHz and 512 frames.
- **Performance evidence.** Two Mac throughput runs with five beds and 400 blocks per cadence completed with zero overruns. The raw piano reached full scale on ten samples in the demanding 512-frame fixture. This fixture measures an intermediate piano signal on the build host. Physical output headroom on a second Pi requires its own playing test.
- **Controls and data.** The regression suite exercises browser control validation and synchronization, device selection, tone and preset persistence, MIDI mapping, bed assignment, recorder failures and recovery. It also covers older software retained in the full source repository. JavaScript interaction tests exercise the native interface with and without attached devices.

- **ARM64 build and startup.** The serial Faust/C++ build completed on the Pi using the release build script. The resulting dynamically linked engine has SHA-256 `c4b4282d1640e04737197dbdbbc7b9ef9753d955bdae56fe0ce7ddfa82d09865`. Systemd verified the unit. On a separate loopback port and a fresh state directory, the controller served its UI, reported zero master gain and no selected routes, handled the headphone status request, and restored a saved mix and preset after restart.
- **Installer.** A harness exercised seven Pi-side cases, covering fresh activation failure, first install, idempotent repeat, failed update rollback, successful update, preservation of an existing foreign service, and selection of the source-build path. It used the actual target libraries and Python imports. The harness simulated package management, systemd changes, HTTP readiness and the compiler step. The real build and controller checks were separate.
- **Virtual stereo playback on the Pi.** A separately named JACK dummy server presented a virtual MIDI input and stereo sink to the release binary. A middle C note produced finite peaks of 0.05264 left and 0.05576 right across 189 blocks. The engine reported one note, zero faults, zero xruns and zero over-budget callbacks in that short check.
- **Browser.** Chromium exercised the native controls at 1366 × 1024 and 390 × 844. The checks covered touch release, knob response, state updates and horizontal overflow.

## Remaining hardware checks

The original Pi’s saved Yamaha MIDI and stereo ports were absent during this review. Its controller was healthy and in preparation mode. Physical key-to-speaker latency, unplug/replug during a performance, sustained worst-case playing, and a clean-card install and boot on a second Pi still need testing. The native instrument accepts note, sustain and mapped CC messages at block boundaries. Pitch bend, channel aftertouch and program change are outside this release’s implemented MIDI controls.

An earlier high-gain reference comparison measured a peak difference of 0.000002795 at 512 frames against a strict threshold of 0.000001. Ordinary fixtures passed. That comparison remains open and is separate from runtime reliability. [Native instrument comparison](https://github.com/codyvanscyoc/stave-synth/blob/5965e5ba3563cb7d2c2a7871866854f9bfbb126b/docs/pi4/NATIVE_V2_INSTRUMENT.md)

The sound bank and JACK client tools come from the operating system. [FluidR3 GM](https://packages.debian.org/trixie/fluid-soundfont-gm) · [JACK client tools](https://packages.debian.org/trixie/jack-example-tools) · [PipeWire JACK](https://packages.debian.org/trixie/pipewire-jack)
