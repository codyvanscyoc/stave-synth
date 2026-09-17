# Native-v2 source mix checkpoint — September17,2026

Follow-up to [downstream buses](NATIVE_V2_BUSES.md): `SourceMix` now ports the
original source-fader5ms smoothing, exact-zero snap, -24dB amplitude mapping,
hard-pan override, shimmer threshold and rendering/Haas decisions.
Its `SourceMixConfig` contains fader positions; its output contains prepared
linear amplitudes suitable for `StagePatch`. This distinction prevents the
over-gain that direct browser-fader-to-Faust wiring would cause.

It is still a separate scalar component, NOT a completed stage-control owner,
mute-transition solution or live application. No source DSP or stage runtime
was changed. No Pi commands, device opening, deployment or restart occurred.

## Tests and limits

`tools/compare_source_mix.py` extracts the actual smoothing/gating/pan blocks
and `blend_to_amplitude` function from pinned57bb94c. No app imports. Tests
compare5,504blocks across512/256, including sustained fader steps, mute and
re-enable, shimmer at/above.001, pan separation at/above.5, hard-pan override,
and seeded controller changes. Largest numeric difference1.111e-16 under
predeclared2e-12 tolerance. Every boolean decision matches;122skip-voice
blocks have exactly zero prepared oscillator amplitudes.

UBSan probe passes. Invalid construction/control/nonfinite values are refused
without changing smoother state. The component has fixed scalar storage and
no dynamic render/configuration containers; no separate allocation counter or
execution deadline measurement is claimed for this probe. Earlier bus/room
allocation measurements retain their own scope.

The reviewed legacy suite passes549tests, zero skips, Node/UI and syntax
checks;124Python files parsed. No native whole-instrument listening, hardware
latency or Pi4 deadline test was run.
The first final regression rerun was sandbox-blocked on temporary loopback
port binds (four errors, one skip); the authorized local-only rerun passed
all549with zero skips. No test was removed or weakened to obtain the pass.

```sh
python3 tools/compare_source_mix.py
```

Requires installed C++ and NumPy; report requires a new directory. Evidence:

`Documents/stave-synth-pi4-backups/native-v2-m2-source-mix-20260917/report.json`

SHA256 `e7778b2ac5eefc749f8a27d48c146e7ed186c52c189a6d86ed3b13adf5690505`.

## Next integration boundary

Compose SourceMix, StageSources and StageBuses with one explicit control
snapshot and owned stop. SourceMix must advance once per WHOLE block before
bank parameters/gates; it must not advance once per event slice. Route its
OSC2 render flag and effective pan separation to the bus; use actual rendered
voice presence for shimmer. Keep source shimmer enable/high consistent with
bus shimmer configuration. Do not infer source eligibility from measured RMS.

The remaining all-muted path is not solved by returning `skip_voices`:
the legacy engine skips Faust bank AND pad bus and temporarily uses its Python
fallback filters when all oscillator/shimmer sources are muted. This preserves
some native state but advances other state. A complete owner needs explicit
reference tests for mute/re-enable and tails before choosing exact compatibility
or documenting/testing an intentional transition repair. Never claim whole-
instrument parity from the separate source/bus comparisons.

Global modulation, delay/shared reverb/master, independent bed/drone/freeze,
organ/recorder/program/scene coverage, event/control/browser integration and
Pi4 timing/field qualification remain open. The existing512-frame stage app
is untouched and remains the previously played build; native-v2 is not ready
to replace it for morning testing. No background test remains running here.
