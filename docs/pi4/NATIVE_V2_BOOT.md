# Native Pi4 boot and recovery

September17: player authorized replacing the default boot application after
accepting the native512 sound and response. This does not close the remaining
recording/feature-parity or physical qualification gates. DSP/binary,48k/512,
saved tone and explicit device selection stay unchanged.

## Ownership and safe startup

`systemd/stave-native.service` is a separate user unit, with linger already
enabled. Install after backing up the old units/state. Disable (do not delete)
`stave-synth.service`; stop the transient candidate and its legacy-restore hook
before starting the new unit. Conflicts prevent two Stave owners. Stable source
root is `~/stave-native/current`; separate persistent native state is
`~/.local/share/stave-native`. Copy only accepted native_controls/native_routes
JSON, never notes/master/freeze/actions. Saved tone is restored MUTED.

The controller sends systemd READY when its supervision loop is available;
this does not mean the audio hardware is ready. `/status` reports that separately.
The30s watchdog supervises controller progress; SIGKILL gives bounded recovery
even for a stopped controller, and core dumps are disabled to protect storage.
Restart=always retries at5s
without exhausting a short boot-time limit. A manual service stop stays stopped.
Stale native progress, fault, exit or lost route tears down the owned child and
restarts muted. Missing selected ports wait; no arbitrary replacement is chosen.
Normal stop/reboot uses SIGTERM, bounded child termination, and control-group
cleanup. No audio process depends on a browser, SSH connection or Mac session.

## Networking

### Preparation without devices (September 17 update)

Keyboard and audio-device absence no longer prevent editing or saving tone.
The controller exposes `runtime.preparation` when it has no audio child and is
not processing a requested restart. Its authoritative in-memory prepared sound
starts from the saved native snapshot. Validated non-performance edits update
this state, and Save sound persists it with the existing atomic writer.
Browser refreshes and multiple browser clients see the same prepared values.

When the selected devices return, the child loads the prepared tone, including
unsaved edits made in the same controller session, and waits for acknowledgment
before enabling live controls. Device loss retains acknowledged tone but drops
pending/unacknowledged requests. Epoch changes cancel old browser gestures.
Failed startup cannot replace the prepared tone with child defaults. A full
controller/Pi restart still restores the last explicitly saved snapshot.

Master/output gain, freeze, key triggers and release/fade actions require live
audio and are never queued during preparation. Empty-bed shaping can be edited
and saved before the recordings exist; it does not create or trigger a sample.
Device readiness and preparation availability are distinct UI facts. The native
host still requires its selected MIDI and audio routes for rendering; this
change enables setup with neither device, not headphone audition without MIDI.
The DSP executable and 48k/512 profile are unchanged.

`--listen auto --network-interface wlan0 --network-interface eth0` inventories
RFC1918 IPv4 addresses every2s and binds explicit addresses plus loopback, never
a wildcard/public listener. Hostname remains `stavepi4.local:8082`; numeric IP
access follows the current address. Per-listener exact Host/Origin checks remain;
all listeners share four HTTP worker slots. Port/bind/network failure retries
independently without stopping healthy audio. UI can be absent while MIDI plays.

Existing NetworkManager, Avahi and `stave-wifi-fallback.timer` are preserved.
The inspected fallback checks at75s after boot and every120s, bringing up the
existing `stave-hotspot` connection only if wlan0 has no active connection.
It does not automatically switch back mid-set. Do not change Wi-Fi credentials
or profile/security policy as part of this promotion. No Internet exposure.

## Rollback and power

To return to the preserved old version in a muted maintenance window:

```
systemctl --user disable --now stave-native.service
systemctl --user enable --now stave-synth.service
```

Verify old8080 health before playing. Both source/state trees remain intact.
To restart native audio without reboot use System's restart action; it returns
muted. For clean device power-off use `sudo systemctl poweroff`, wait for shutdown,
then remove power. `sudo systemctl reboot` exercises orderly stop/boot. Sudden
power removal is not equivalent to clean shutdown: atomic settings replacement
reduces partial-file risk but cannot guarantee SD-card/filesystem survival.

## Evidence scope

597 reviewed Mac tests pass, zero skips, including6 added recovery/network/unit
contracts. No DSP/native source changes in this batch; target binary hash must
match the player-accepted build. Target installation/recovery/reboot results are
recorded in RESUME_HERE.md after execution, not inferred from unit tests.
Physical unplug/replug, hard power cycles, no-network hotspot access, iOS
sleep/wake and actual key-to-analogue latency still require their own evidence.

### Installed and exercised

Native is now enabled at boot; old service disabled, source/state retained.
15 target controller tests and systemd unit verification pass. Real muted
recovery: child exit4.535s, child stall15.830s, final controller watchdog34.107s.
One orderly reboot automatically brought up native with Yamaha routes and saved
tone; controller active33.814s after boot, healthy audio observed by58.68s uptime.
Zero postboot xruns/over-budget/full-scale in the recorded interval. This is not
a physical cold-power or worst-load musical test. Rollback to old8080 and back
to native8082 passed with both saved-state hashes unchanged. Isolated absent
device selections left the status UI alive and launched no audio child.
Final state remains native enabled, old disabled, native output muted.
