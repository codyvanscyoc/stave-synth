# Stave Synth Pi4 development

This checkout is for the Pi4 stage edition. Use the `pi4-stage-pro` branch and the requirements in `docs/pi4/PRODUCT_VISION.md`. The Pi5 and Mac lines are separate; do not push, merge, or deploy these changes to `main` or `mac-port` unless the user explicitly expands the task.

The preserved Pi4 baseline is tag `pi4-stage-baseline-2026-09-14` at `d0142f0b811baa4ba4fa216d55c6d5e4b0ac7dbd`. See `docs/pi4/BASELINE.md` for backup scope. The baseline has been used on stage, but it has not passed the new release gates.

The product centers on expressive piano plus OSC1/OSC2 blended live with filter and effects. Sampled beds, held/frozen ambience, and tonic-and-fifth drones are also part of the workflow. Presets/setlists are secondary. Preserve the loved sound, familiar five-fader layout, and saved-data compatibility while improving reliability and measured performance.

Read `docs/pi4/ENGINEERING_VALIDATION.md` before testing. A separate checkout does not isolate runtime: the current application shares home-directory state, fixed network ports, and JACK names. Use explicit isolated instance paths/ports/identity for tests. Existing parameter-flood, connection-chaos, and boundary-sweep scripts must not target the production service by accident.

Keep reliability fixes separate from intentional tonal changes. Verify which native/Faust path is active before attributing a defect to fallback Python code. Match baseline sound levels when comparing audio, and report observed results separately from static risks and proposed targets. Buffer capacity is not measured end-to-end latency.

Publish scoped Pi4 commits with their verification. Rebuild native components on the target architecture before deployment; copying Python source alone does not update compiled DSP. Keep private runtime/settings archives and credentials out of the repository. Use the release plan to determine when live deployment, hardware interruption, and whole-device recovery testing are appropriate.
