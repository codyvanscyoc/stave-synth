# Organ Leslie STOP correction — Pi4 candidate

Source fix `da0681e`; native checker `8ef73e7`. Candidate-only evidence,
not normal-stage promotion or release qualification.

The UI/schema accepted `leslie_speed: "stop"`, but both Python and native
organ wrappers only accepted slow/fast and mapped other values to slow.
Both now accept stop and target 0 Hz through the existing rotor ramps, retaining
depth, phases, voices and volume. Slow 0.8 Hz and fast 6.5 Hz are unchanged.
The existing Faust zone's declared minimum changes 0.1→0.0; no zone name,
ABI, voice count, synthesis formula, precision or ramp constant changes.

## Actual target-native verification

On the Pi4, the new library was built in a private staging directory using
the organ flags from `faust/build.sh`: Faust `-lang c -cn StaveOrgan`, GCC
`-shared -fPIC -O3 -ffast-math -include faust_cprelude.h -mcpu=native`.
Before/after generated-C comparison showed exactly one changed line: the
declared UI slider minimum. Generated compute/state/ramp code is identical.

The bounded `tools/check_organ_stop.py` check **passed** on the Pi in 5.572 s:

- Separate baseline/candidate DSP instances at 48 kHz, deterministic held triad.
- Slow 48,000 frames and fast 48,000 frames: **sample/bit exact**, max difference 0.
- STOP: declared/accepted target 0 Hz; 384,000 frames/eight simulated seconds,
  entirely finite and nonmuted; held gate/depth/volume readbacks unchanged.
- No instance-clear, gate release or bypass was invoked at mode changes.

There is no native rotor-speed/phase readback. This does not establish an
exact stopping time, subjective sound, end-to-end latency, whole-app behavior
or real-time stage load. Seven wrapper/regression tests and ten checker tests
also passed; those use mock native calls and are distinct from this target run.

## Native manifest delta

Only `faust/libstave_organ.so` changes from the historical
[TARGET_BUILD.md](TARGET_BUILD.md) manifest:

```text
before: 330bf3f9b3f19af7051e2c644e3d5f640e6a428c410c2d463270c43e8ab65c82
after:  d7b1898096c0c9a0ec5d5e53e39bf85187ac2253fb95802e65bec7ff9b1d7455
```

The other 15 native artifacts were reverified unchanged. The new organ was
copied only into the stopped private audition worktree. The normal stage
checkout remains `ce15cfb` with its original libraries and settings.

Private raw JSON, both libraries and generated C:

- Pi: `/home/codyvanscyoc/stave-synth-pi4-backups/organ-stop-20260914.Kt7pb9`.
- Mac: `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/organ-stop-20260914.dRGE5U`.

Do not use the old 16-file manifest unmodified to check the new candidate:
expect this organ delta and require the other 15 exact matches. Preserve both
versions for rollback; no native artifacts are committed to Git.
