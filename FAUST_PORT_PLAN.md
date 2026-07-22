# Faust Port Plan — Render Skeleton

**Goal:** Move the remaining Python/numpy per-block DSP ("the skeleton") into
Faust modules, phase by phase, with proven sonic parity at every step.

**Why:** Profiled on the Pi 4 (2026-07-21, quantum 512, real patch, 8-note
chords): render thread ~75%, of which ~45% is Python glue *between* the
existing Faust islands — buffer shuttling, filter routing, pan/Haas, sends,
bus sums. Every phase ported = CPU freed = **buffers get tighter** (the
prize is Pi 5-class feel on the Pi 4; latency NEVER goes up — user law,
2026-07-21). The Pi 5 adopts each phase later via the same env flags after
it has soaked on the Pi 4.

**Method (proven across 10 existing modules):**
1. Write `faust/<name>.dsp` mirroring the Python math exactly (watch the
   gotchas list below)
2. `faust/build.sh` target (+ lite variant if it has per-voice state)
3. cffi wrapper `stave_synth/faust_<name>.py` (copy an existing wrapper's
   zone-discovery pattern; persistent f64 out buffers — zero-alloc rule)
4. Env flag `STAVE_FAUST_<NAME>` (default OFF until proven; then default ON
   for small-Pi; then Pi 5 after soak). Python path stays as Mac fallback.
5. **Parity proof before enabling:** offline A/B — render the same input
   through both paths (see `tools/render_compare.py` pattern), assert
   max delta at or below ~1e-6 (float32 zone boundaries make exact parity
   impossible; -120 dB is the bar). Then on-device "robot ears": inject
   chords via `aplaymidi` → `pw-record` the sink monitor → scan for
   zero-gaps ≥24 samples inside loud material + |delta|>0.2 clicks.
6. Deploy Pi 4 → measure render % (same-ssh-session PID capture, never
   across a restart) → user ear check → update this file's journal.

## Phases

### Phase 0 — DONE (historical, sessions 04-18 → 07-21)
reverb (FDN) / plate / drone / piano_room / osc_bank (+12-slot lite) /
sympathetic (+lite) / ping_pong / master_fx / bus_comp / organ.
Plus C kernels in jack_bridge.c: `bridge_limiter_env`, `bridge_biquad`,
`bridge_onepole` (2026-07-21).

### Phase 1 — pad_bus.dsp  ← NEXT
The fat middle of `synth_engine._render_locked` (~lines 2800–3300):
- Shared filter path: 12/24 dB lowpass (24 = staggered-Q Butterworth
  cascade), smooth log-space cutoff (Python keeps the log-smoothing and
  writes the smoothed cutoff per block — Faust just consumes the zone),
  filter comp `10^(-15·f_pos^1.3/20)`
- Independent per-OSC filter paths + their own comp (added CR#9)
- Shared highpass (low cut) path
- Per-OSC pan (|p|^0.7 + equal-power), hard-pan WIDE + Haas delay, width
- Dry/wet split + per-OSC reverb sends (incl. rev_send_filter split-path)
  + FX-bypass routing sums
- **Stays in Python for now:** LFO shape gen + per-block ramp application
  (sonic character risk — the block-ramp smoothing is deliberate; port in
  Phase 4 only with A/B), voice alloc/ADSR, pad sample player, sympathetic
  bookkeeping.
- I/O sketch: inputs = osc bank's 5 channels (osc1 L/R, osc2 L/R,
  shimmer) + piano stereo (for sends only if needed later); outputs =
  dry L/R, wet-send L/R, bypass L/R (6 ch). Zones for every param above.
- Flag: `STAVE_FAUST_PAD_BUS`.

### Phase 2 — shimmer chain
Shimmer HP split, CLOUD multi-tap pre-verb delay, shimmer delay lines,
+12/+24 handling glue. Flag: `STAVE_FAUST_SHIMMER`.

### Phase 3 — piano chain
LA-2A comp (careful: its soft-knee is the CORRECT reference formula),
4-band piano EQ + voicing curves, velocity-brightness filter, tremolo.
FluidSynth stays (sample playback). Flag: `STAVE_FAUST_PIANO_CHAIN`.

### Phase 4 — consolidation
- Merge adjacent modules where Python shuttling between them is the cost
  (candidate: osc_bank → pad_bus as one compute when both flags on)
- Revisit LFO application in-Faust (A/B the smoothing character)
- 8-slot lite variant decision (user ear test on voice stealing first)
- Then: ring ratchet 16/8 → 12/6 → 10/5 … with user feel-testing each step.

## Gotchas (learned the hard way — check EVERY port against these)
- Drive makeup is `tanh(g)/g`, never `/tanh(g)` (organ bug, CR#9)
- Reverse-delay offset advances at 2× rate; `~` recursion needs named
  functions, not lambdas
- Soft-knee: `slope·(over+knee/2)²/(2·knee)` — the /2 matters (bus comp bug)
- NEVER clear filter state (zi / si.smoo internals) on param changes —
  only on panic/freeze
- Faust `si.smoo` ≈ 21 ms smoothing — if Python already ramps a value
  per-block, DON'T double-smooth (osc blend does this today; acceptable,
  documented)
- Float32 zone boundary: parity bar is ~1e-6, not bit-exact
- `%` in Faust strings needs escaping in build logs (`%h` incident)
- ctypes: cache pointer objects per instance — per-call `data_as`
  marshalling can cost as much as what you're replacing (C-IIR lesson)
- After synthetic MIDI tests ALWAYS send panic (killed aplaymidi = hung
  notes = polluted measurements)
- Snapshot/restore `current_state.json` around any WS-driven test
  (autosave pollutes within 30 s)

## Pi 5 adoption checklist (per phase, AFTER Pi 4 soak)
1. `git pull` on the Pi 5 (build.sh now carries `-mcpu=native` — fine)
2. `cd faust && ./build.sh --force`
3. Add the phase's `STAVE_FAUST_*=1` to
   `~/.config/systemd/user/stave-synth.service.d/faust.conf` (and the
   repo's `systemd/stave-synth.service.d/faust.conf` + `stave-synth.sh`
   exports once it's default-on)
4. Restart NOT on a Saturday night. A/B by ear against a recent recording.
5. Note: Pi 5 runs the FULL profile — never the lite .so variants.

## Journal
- **2026-07-21** — Plan written. Profile basis: Pi 4 real-patch chords
  75.2% render (osc_bank 22%, piano 12%, reverb 6%, skeleton glue ~45%).
  Phase 1 scoped. Max-everything torture: 1 dropout/36 s remaining.
