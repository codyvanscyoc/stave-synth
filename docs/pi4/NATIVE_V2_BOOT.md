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
