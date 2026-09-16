# Yamaha MX output latency profile

Player-approved response, September 16, 2026. This is device-specific output
tuning, not a lower Stave graph quantum or a measured MIDI-to-analogue target.

## Scope

Source: `config/pi4/51-stave-yamaha-mx-latency.conf`.
Installed only for the Pi's user at:
`~/.config/wireplumber/wireplumber.conf.d/51-stave-yamaha-mx-latency.conf`.
Requires WirePlumber 0.5 configuration syntax; target uses 0.5.8/PipeWire1.4.2.
The generic installer is unchanged: do not apply this to every device or Pi5.

The rule matches all three observed properties: exact Yamaha MX stereo output
node name, Audio/Sink class, and USB0499:1711 ALSA component ID. It changes only
api.alsa.period-size to128. No USB input, other interface, volume, sample rate,
batch-mode flag, effect, voice count, or Stave queue policy is overridden.
Other Yamaha models/profiles are not covered and require their own testing.

On this device, batch handling results in actual ALSA period64/headroom64,
instead of the original automatic256/256. Driver rate is44100Hz/24-bit stereo;
Stave/PipeWire graph remains48000Hz/512 frames, low-latency six slots/refill3.
The corresponding temporary trial changed sampled driver queue mean from
22.97ms to18.86ms; this is NOT a calibrated end-to-end latency measurement or
a matched-load A/B. Player reports: "It feels just right now."

## Verification

- Three device-free regression guards check exact match scope and that period
  size is the only changed property. Full Mac suite:549 tests, zero skips,
  plus Node UI/syntax checks.
- Actual WirePlumber restart recreated Yamaha output (node79 ->51), reapplied
  the saved rule and reopened hardware with period64. Stave restarted normally,
  with zero automatic restarts. Output volume1.00 and saved state unchanged.
- Temporary trial's120-second mixed idle/playing check: zero new underruns,
  xruns, piano misses or MIDI drops;15 late renders remained. This does not
  establish full-load/long-duration qualification. See REHEARSAL_CHECKPOINT.md
  for subsequent persistent-profile health evidence.
- Subsequent60-second idle check: zero new underruns/xruns/late renders/piano
  misses/MIDI drops, callbacks+5628/renders+5627, healthy native/audio/UI.
  No MIDI notes occurred; this is not a loaded playing qualification.
- Full power-cycle/physical USB-replug verification remains a player check;
  the persistent rule loads at device creation, including future boots. Do
  not claim a remotely unperformed power cycle as tested.

## Install/rollback safety

Use an authorized muted/off-stage window. Inspect current service/device
ownership first; numeric ALSA/PipeWire IDs change. Back up settings and existing
configuration, and refuse overwriting any unexpected file at this exact path.
Stop Stave before restarting WirePlumber, then start Stave and verify routes,
native/audio/UI health, volume and actual ALSA hardware parameters. Do not
restart the graph during a performance.

To undo: verify this file's SHA256 is
`3f8bfdfc54bf70b28800e8084e6471252036271d5bcadcffc490c99565ffaaf3`,
move only this owned rule into a private backup location (not delete the whole
configuration directory), restart WirePlumber with Stave stopped, then start
Stave and verify original automatic period256/headroom256 and correct route.
Existing Stave95/90 service overrides and required FluidSynth library stay.

Private stopped-runtime archive before install is retained on Mac and Pi,
SHA256 `98b2e5b4bbc24c9fe253d984974aa462c1218c68b0a203784a8cb2e3e936df61`.
See private latency-20260916 INDEX for exact backup roots and observations.

Reference: [WirePlumber ALSA rules and buffering](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/alsa.html).
