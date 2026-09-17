# Stave Synth Pi4 development

Start resumed work with `docs/pi4/RESUME_HERE.md`; it records the latest saved
operational state, evidence locations, user decisions and unresolved findings.

This checkout is the user-authorized Pi4 native-engine successor experiment.
Use `pi4-native-engine-v2` here; preserve `pi4-stage-pro` and the annotated
`pi4-v1.2-stage-snapshot-20260916` tag as the working version. The v1.2 label is
a preservation label, not a claim of full release qualification. The user
authorized organizing and implementing this successor on September 16 after
successful light-load worship use. Follow `docs/pi4/PRODUCT_VISION.md`.
Do not push, merge, or deploy onto Pi5 `main`, `mac-port`, or the running stage
checkout. Native-v2 starts as an offline prototype: no physical output, MIDI
connections, service installation, production config writes or runtime imports.
See `docs/pi4/NATIVE_V2_PLAN.md` for milestone and acceptance boundaries.

The preserved Pi4 baseline is tag `pi4-stage-baseline-2026-09-14` at `d0142f0b811baa4ba4fa216d55c6d5e4b0ac7dbd`. See `docs/pi4/BASELINE.md` for backup scope. The baseline has been used on stage, but it has not passed the new release gates.

The product centers on expressive piano plus OSC1/OSC2 blended live with filter and effects. Sampled beds, held/frozen ambience, and tonic-and-fifth drones are also part of the workflow. Presets/setlists are secondary. Preserve the loved sound, familiar five-fader layout, and saved-data compatibility while improving reliability and measured performance.

Read `docs/pi4/ENGINEERING_VALIDATION.md` before testing. A separate checkout does not isolate runtime: the current application shares home-directory state, fixed network ports, and JACK names. Use explicit isolated instance paths/ports/identity for tests. Existing parameter-flood, connection-chaos, and boundary-sweep scripts must not target the production service by accident.

Keep reliability fixes separate from intentional tonal changes. Verify which native/Faust path is active before attributing a defect to fallback Python code. Match baseline sound levels when comparing audio, and report observed results separately from static risks and proposed targets. Buffer capacity is not measured end-to-end latency.

Publish scoped Pi4 commits with their verification. Rebuild native components on the target architecture before deployment; copying Python source alone does not update compiled DSP. Keep private runtime/settings archives and credentials out of the repository. Use the release plan to determine when live deployment, hardware interruption, and whole-device recovery testing are appropriate.

Operational checkpoint, 2026-09-15: the Pi's normal `stave-synth.service` now
uses `/home/codyvanscyoc/stave-synth-pi4-rehearsal` through the removable
`95-pi4-continuity-candidate.conf` user-service drop-in. The existing 90 override
and clean `ce15cfb` stage checkout remain the tested immediate rollback;
the original checkout is also preserved. See `docs/pi4/REHEARSAL_CHECKPOINT.md`.
The player declined an eight-hour soak for this release effort. Do not run it
or claim long-duration qualification. Do not assume a checkout is idle:
verify actual service ownership before modifying/syncing runtime files there,
and use an authorized maintenance window or a separate isolated candidate.
See `docs/pi4/STARTUP_CHECKPOINT.md` and `docs/pi4/RELEASE_CHECKLIST.md` for
current evidence and the still-required physical playing/rehearsal gates.
