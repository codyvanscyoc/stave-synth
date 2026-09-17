# Native512 stage panel candidate — September17

This is a reversible candidate, not the completed M5/M6 migration or a claim of
universal interface support. The original v1.2 working service remains preserved.
Player deferred recording the pad slots; no invented/demo assets go into that
library. Recording, disk finalization and live asset replacement remain pending.

## Delivered source

- Stage: piano/OSC1/OSC2 balance, independent piano brightness, synth filter,
  reverb mix, master, freeze, key/pedal release and mute. OSC2 is always visible.
- Edit: waveform/envelope/ambience controls, separate from the live mixer.
- System: exact discovered MIDI/stereo selections, restart, sound snapshot,
  truthful callback counters and explicit feature limitations.
- Native sound snapshots use a separate `native_controls.json`, the reviewed
  atomic writer, bounded validated reads and no legacy-state import/overwrite.
  Master, notes, freeze, bed triggers and fade/release actions are never saved.
- Explicit `.local` allowlisting supports hostname and numeric-IP access while
  retaining exact same-origin JSON checks. Trusted private LAN control is open,
  as requested; no public exposure/pairing system.
- `tools/native_v2_stage.py` supervises an explicitly selected native512 owner.
  Unlike the bounded audition, candidate mode has no one-hour expiry. Port
  absence waits without choosing an arbitrary replacement. A failed/lost route
  ends the native child; recovery starts a new MUTED owner, restores acknowledged
  tone controls, and invalidates old session actions. It does not install or
  stop any service. HTTP clients are bounded to four workers with socket timeout.
- Binding is explicit private IPv4; allowed mDNS names do not solve DHCP address
  changes or prove cold-boot readiness. Browser disconnect does not own audio.

## Candidate operation

Keep512. Use the verified ARM64 binary and explicit existing soundfont, separate
state directory, `--hostname stavepi4.local`, MIDI source and distinct stereo
output. Ports8082–8090 only. Requires `--allow-stage-candidate`; native stage
mode refuses256. The old audio owner must be paused in an authorized window;
startup refuses an existing Stave audio client. Never run beside v1.2.

Save sound only after desired controls have been acknowledged. Changes remain
live and unsaved until that button. Restart/device selection interrupts audio
and discards unsaved knobs. Raise Master deliberately after recovery. A browser
timeout never retries a save, restart, route change or performance gesture.

Only loaded recorded keys are enabled. Empty slots are visibly unavailable.
The existing organ, recorder, legacy presets/maps, pitch bend, macros/scenes and
program-selection workflows are not all migrated; see the on-screen ledger.
An active candidate is not automatic permission to replace boot service policy.

## Verification

Current evidence and actual running owner/URL are recorded in RESUME_HERE.md.
Device-free tests cover validation, durable-state failure preservation, missing
assets, restore acknowledgments, startup timeout, session invalidation, route
types/directions, same-origin/hostname policy and bounded HTTP workers. Panel
JavaScript runs under a mock DOM for acknowledged controls, tabs, route/save
actions and disconnected/recovering states. Native fake-JACK/UBSan guards check
both the unchanged bounded audition and explicitly opted-in native512 mode.
Browser visual/Safari/device hotplug and physical latency qualification are
separate; passing those unit tests does not complete them.

### Verified target handoff

Native3795210, HTML9658fba. Mac591 reviewed tests (zero skips), Node UI, native
C++/UBSan/fake-JACK and independent MIDI-fixture checks pass. Safari desktop,
390x844 phone-width and768x1024 tablet-width previews checked against a local
simulated controller; no claim of physical iOS recovery coverage.

Pi root `/home/codyvanscyoc/stave-v2-panel-20260917.HSb57u`: native build/guards,
9 new controller tests and4,000 measured offline blocks pass without deadline
misses. Muted actual-driver94.552090s test:360 notes,8,883 callback delta, zero
xruns/over-budget/raw-piano-full-scale/unsupported MIDI. Save/restart/restore,
hostname origin policy, stale epochs and route selection passed. One deliberate
software MIDI-route disconnect recovered in4.698791s, muted. This is not a
physical USB hotplug or arbitrary-interface qualification.

Transient `stave-native-v2-candidate.service` is running at8082, conflicts with
old `stave-synth.service` (currently stopped), with no hour expiry. It is NOT
enabled on boot. Stop it to automatically restore old8080; this exact rollback
policy was rehearsed successfully and legacy current-state bytes stayed equal.
Reboot still starts the preserved old app. See RESUME_HERE.md for current owner,
accepted tone settings, state paths and outstanding gates.

Private source/build/settings/evidence archive is on both Pi and Mac:
`stave-v2-panel-evidence-20260917.tar.gz`, SHA256
`e9d5bd69c20d2f2763f454b66682a6b7ae21b725b679760dbed69acbe57103ee`.
Mac root: `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups`.

- Pi build report SHA256: `8b9904e5302c8b67a38f541b335c8f728f588d3b97ad0fe08c2fdf0019353a32`.
- Muted live report: `a9e411edf6850dbafda7aba360999a444a0a84198abbc4daa4ce894c5dad9546`.
- Rollback/final handoff report: `bbcb417067f3128be8c943fb533b702a6b7efff7364037428905486d50ae4480`.
- Final HTML: `0705478fdbc8ef3a6a92d16a5e509a442dcd7088d7821d4ce54dd01fb5f2af05`.
- Mac native guard report: `3d18fd26f1261b1f4292f33950f4a098b990dc8bc837f2f2ad6a7d00aafa9f01`.

This archive includes the one-off guarded build/test/handoff scripts. It is not
a whole-SD image. Saved player tone is separate from disposable test state.
