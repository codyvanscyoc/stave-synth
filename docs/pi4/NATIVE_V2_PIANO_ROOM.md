# Native-v2 piano-room checkpoint — September 17, 2026

The downstream `PianoRoom` component now preserves the existing float Faust
Dattorro room plus the original double dry/wet mixing and smoothing. This is
another M2 component, **not a complete downstream graph or stage candidate**.
`StageSources` still exposes its original seven stems, including dry processed
piano; the room is not silently inserted into that API or the M1 demo.
No Pi contact, deployment, restart, audio/MIDI opening or state change occurred.

## Ownership and compatibility

One audio owner configures, renders and clears. Construction/destruction occurs
while stopped. Fixed48kHz, a256/512-frame maximum, preallocated512-frame storage;
valid calls accept1..maximum frames, but integration must retain whole source
blocks. In-place buffers work; overlapping output channels, null pointers and
invalid lengths are rejected without changing state or destinations. A future
driver must handle invalid-call silence itself.

Controls are validated transactionally: wet/size0..1, damp0..0.99. Nonfinite or
float-unrepresentable input, or nonfinite processed output, terminally faults
the component and zeros a valid output block. `clear()` cannot recover a fault;
reconstruction requires the owner to be stopped. This is deliberate invalid-
input safety, not claimed equivalence for malformed legacy inputs.

The preserved musical behavior includes:

- Wet smoothing uses the original30ms block-rate formula. DSP input/output
  remain float; dry mixing and the smoother remain double.
- Enabled with wet<=.001 bypasses processing and freezes the tank AND wet
  smoother. Re-enabling wet can resume stored ambience. This is not a newly
  implemented musical freeze feature.
- Enabled-to-disabled clears the tank on the next valid render, but retains
  the wet smoother. Re-enable therefore begins with an empty tank and previous
  wet-smoothed value. Explicit hard clear also retains controls/smoother.
- No transition crossfade, tail-draining bypass or intentional tonal change
  was introduced. These existing quirks need a separate reviewed improvement,
  not an undocumented change inside a fidelity port.

## Evidence

`tools/compare_native_room.py` extracts the original Faust wrapper and actual
room-mixing branch from pinned reference57bb94c. It does not import the app.
Both sides instantiate the same pinned generated DSP under matched compiler
settings. This proves native wrapper/control/mix fidelity on this Mac, not
cross-architecture parity or real-time performance.

Four comparisons pass: synthetic stereo impulse/chord/decay input and saved
real piano source stems, each processed at512 and256. Each comparison contains
327,680 frames (6.827seconds) with size/damping/wet changes, threshold crossings,
disable/re-enable, explicit clears and tails. **Every output sample and every
checked wet-smoother value matches exactly**; the predeclared audio tolerance
was1e-6. The synthetic release tail is nonzero before hard clear; all outputs
are exactly zero after the final clear with silent input.

The optional real-piano test uses the saved512-cadence piano stem as the same
input for both downstream cadences. It does NOT assert that the source graph
itself is cadence-invariant; its separate source comparison remains authoritative.

Additional C++ guards cover invalid construction/configuration/buffers, in-place
bypass, wet-threshold freeze, clear silence, terminal numeric faults, and2,000
control/render iterations per maximum block size. A calibrated C++ `new`/`new[]`
probe observes zero allocations in the exercised configure/render/clear loops.
This does not certify C malloc, library locks or deadlines.

UBSan passes on the C++ room wrapper and guards. Generated Faust C remains
uninstrumented; ASan remains unverified from earlier work. The reviewed legacy
suite passes549tests, zero skips, plus Node/UI and syntax checks;122Python
files parsed. No full-instrument listening or Pi4 timing test is claimed.

Reproduce using installed Faust/C/C++/NumPy/CFFI only:

```sh
python3 tools/compare_native_room.py
# Optional: reuse an explicitly supplied saved seven-channel source fixture.
python3 tools/compare_native_room.py --source-stems /absolute/path/sources-native-512.npy
```

Artifacts require a new directory. Final private evidence is at:

`Documents/stave-synth-pi4-backups/native-v2-m2-room-20260917-verified/report.json`

Report SHA256:
`d42a66797bb2007b6bf98f195014cc69479445c25ab7ecabb777d98f1e38b37b`.

The report records source/fixture/generated-code/binary hashes, commands and
compiler identity. No samples, settings or generated binaries are committed.

## Next work

1. Port the oscillator pad bus/main-filter scalar smoothing and dry/send/bypass
   routing; compare real stems, independent/shared filters, Haas and shimmer
   cloud tails. Preserve existing gating/reset behavior explicitly.
2. Compose those downstream components with `StageSources` and room under one
   owner. Add complete master/effects routing before any finished audio demo.
3. Continue remaining control/event coverage and worship graph from
   [the source checkpoint](NATIVE_V2_SOURCE_GRAPH.md). Only then plan isolated
   Pi4 timing work and the later browser/hardware qualification gates.

The working v1.2 snapshot and Pi5 branches are untouched. No test job remains
running at this checkpoint. Offline256 fidelity is NOT playable256 qualification.
