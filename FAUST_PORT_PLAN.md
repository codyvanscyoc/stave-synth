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

### Phase 1 implementation notes (shipped 2026-07-22, awaiting Pi 4 soak)
Files: `faust/pad_bus.dsp` (StavePadBus, 5-in/6-out), `stave_synth/faust_pad_bus.py`,
`tools/compare_pad_bus.py`, `config.USE_FAUST_PAD_BUS`, surgical branches in
`synth_engine._render_locked`. Flag OFF by default — Pi 5 behavior unchanged.

**Included:** OSC2 Haas delay (write-gated line, int samples, 4096 = 40 ms @
96 k headroom) · shared/indep filter routing incl. the render_osc2 dry gate ·
shared 12/24 dB lowpass (24 = staggered-Q cascade, stage-2 input gated by
slope so its state mirrors Python's frozen-when-12 instance) · filter comp
between stages · indep per-OSC lowpass + own comps · shared highpass
(input-gated) · per-OSC reverb-send weighted sum through the
`_rev_send_filter_*` mirror (input gated by `send_filter_active` so state
advances only on slow-path blocks, like Python).

**Excluded (stays Python):** LFO amp/pan application (mid-region, Phase 4) ·
FX-bypass dry carve (post-LFO, data-dependent magnitude ratio — bypass_l/r
outputs reserved, always 0) · reverb-send FAST path (engine still copies the
post-LFO dry bus when both sends == 1) · shimmer chain (Phase 2; inert
post-filter `shimmer_send_gain` hook is wired) · everything upstream
(voices/ADSR/blend) and downstream (delay/reverb/sympathetic).

**Plan corrections found while porting (follow-the-code):** per-OSC pan law
+ width are NOT in this region — pan is applied per-unison inside osc
generation (osc_bank.dsp owns it on the Faust path); only the Haas half of
WIDE lives here. And osc*_fx_bypass never reroutes the wet send to a bypass
bus — it zeroes that OSC's send weight and the dry carve happens post-LFO.
The I/O sketch's "piano stereo (for sends)" was also unnecessary: piano
reaches the reverb via external_reverb_send after this region.

**Precision:** built with `-double` + `-DFAUSTFLOAT=double` (only this
module) — pole-near-unity biquads can't hold 1e-6 in float32, and double
zones/IO let the wrapper pass the engine's float64 blocks zero-copy (no
f32 scratch). Biquads implemented as DF2-transposed, the exact scipy
lfilter recurrence, with BiquadLowpass/Highpass's clamps copied.

**Zone ownership:** Python pushes per block: `_filter_cutoff_last_set` +
`_filter_res_last_set` (so the >0.1 Hz set_params skip is mirrored, and
LFO/drift/wobble effective-cutoff math stays in Python), f_pos + per-OSC
f_pos zones, smoothed indep + highpass cutoffs, bypass-zeroed sends,
haas_on/samples, render_osc2. NO si.smoo anywhere in the module.

**Parity (tools/compare_pad_bus.py):** two full engines, identical
notes/params/seed, both on the Faust osc bank; taps at ping-pong entry
(dry), reverb entry (send), and render output. 1300 blocks covering sweep
down/up, 12→24, resonance, Haas via pans + hard-pan, slow-path sends,
bursts, highpass, both indep filters, chord change, osc2-blend-to-zero.
Result: peak |Δ| 2.9e-13 (dry), 1.7e-13 (send), 2.1e-13 (final) — PASS
at the 1e-6 bar with ~6.5 orders of margin.

**Known transition-only divergences (documented, sub-audible, steady-state
exact):** re-engaging a branch whose state Python froze while Faust's
decayed (24→12→24, HP off→on→off→on, Haas re-engage inside the window
where Python replays stale ring content vs our zeros, fast→slow send
transitions warm vs frozen send-filter state). First engagement of every
branch is exact by construction (input gating).

**Faust gotchas earned this port (added below):** `-(expr)` is partial
application of subtraction — a phantom-input block, NOT unary negation;
and with-local closures inside `~` get lambda-lifted (keep the recursion
tick top-level with coefficients as explicit args).

### Phase 2 — shimmer chain — DONE (2026-07-22, awaiting Pi 4 soak)
Shimmer HP split, CLOUD multi-tap pre-verb delay, shimmer delay lines,
+12/+24 handling glue. **Design decision: extended `faust/pad_bus.dsp`
instead of a separate module** — the chain's input is pad_bus's existing
input 5 (the osc bank's shimmer channel), its only Python entanglements
are per-block scalars, and its output lands at the reverb entry pad_bus
already computes toward. One merged compute, no new island boundary, and
**no `STAVE_FAUST_SHIMMER` flag — `STAVE_FAUST_PAD_BUS` governs it all.**

### Phase 2 implementation notes (shipped 2026-07-22, awaiting Pi 4 soak)
Files: `faust/pad_bus.dsp` (now 5-in/9-out), `stave_synth/faust_pad_bus.py`,
`tools/compare_pad_bus.py` (11 new shimmer segments), surgical branches in
`synth_engine._render_locked`. Same flag, still OFF by default — Pi 5
behavior unchanged (verified: flag-off render bit-identical to a
pre-change baseline, max |Δ| exactly 0.0 over a 500-block shimmer-heavy
scenario).

**Included:** shimmer HP split (BiquadHighpass mirror; 400 Hz +12 / 1200 Hz
+24, cutoff pushed as a zone, coefficients swap with state carried) ·
mix scaling from the Python-owned ~80 ms one-pole (`_shimmer_mix_cur`
advanced at the zone-push site under the exact `render_shimmer and
voice_idx > 0` gate, then consumed as a block-constant) · CLOUD multi-tap
pre-reverb delay (one 65536-slot rwtable, ring wrap at `int(0.6·SR)`,
4 taps per channel, gains 0.65/0.50/0.36/0.22) · new outputs 7/8/9
(shimmer mono, cloud L/R already ×shimmer_send) added by the engine to
reverb_in in the legacy add order, on fast-path AND slow-path blocks.

**Excluded (stays Python):** the `render_shimmer` / `voice_idx > 0` gate
logic and mix smoothing (pushed as zones) · the reverb_in adds themselves
(shimmer must join on fast-path blocks where send_L/R are ignored — this
is WHY shimmer got dedicated outputs instead of riding the send bus) ·
shimmer pitch generation (osc bank, was already Faust).

**Beyond the Phase-1 parity bar — frozen-state emulation:** Python
*freezes* shimmer state when the chain is inactive (skipped `.process()`
= frozen biquad zi; no ring write = frozen delay content AND index).
Phase 1 accepted decayed-vs-frozen re-engage transients; Phase 2 mirrors
the freezes exactly: a select2-held biquad (`biquad_tick_frozen`) and an
rwtable whose write index advances only while the cloud is active
(`cloud_widx`). Re-engagement after arbitrary pauses is sample-exact —
including Python's stale-content CLOUD replay. (The one intentional
divergence: while frozen, Faust rewrites one ring slot with 0.0 each
sample; that slot is overwritten by the first re-engage write before any
tap can read it, so it is unobservable.)

**Plan corrections found while porting (follow-the-code):** the inert
`shimmer_send_gain` hook from Phase 1 was the wrong shape and is REMOVED
— shimmer cannot join `send_L/R` because those outputs are ignored on
fast-path blocks while shimmer still reaches the reverb. Also
`shimmer_high` touches this region ONLY via the HP cutoff; the ×2/×4
pitch lives in the osc bank. And Python's deferred HP-mode bookkeeping
(`_shimmer_hp_high_last`) needs no mirror: pushing `1200 if high else
400` every block lands the same coefficients on every processed block.

**Precision:** same `-double + -DFAUSTFLOAT=double` build. Tap offsets and
ring length are init-time zones computed by the WRAPPER with Python's
exact expressions — `int(0.289·96000) == 27743`, an in-Faust recompute
can round differently. Cloud output grouping verified in the generated C:
`raw·(send·active)` with active ∈ {0,1} exactly, so the multiply matches
Python's `cloud * send` bit-for-bit.

**Panic:** `FaustPadBus.clear()` now does a FULL `initStavePadBus` + static
zone re-push, because Faust puts rwtable initialization in
`instanceConstants` — `instanceClear` alone would leave up to 0.6 s of
cloud material to replay after panic. Verified by smoke test: post-panic
output exactly 0.0, notes work after, static zones intact.

**Parity (tools/compare_pad_bus.py):** scenario extended 1300 → 2100
blocks: shimmer on over warm slow-path sends, mix sweep, +12↔+24 flips,
slow→fast send transition under shimmer, CLOUD freeze + re-engage,
chain off/on (frozen HP), mix-threshold gate off/on. Result: full-run
peak |Δ| 2.9e-13 (dry) / 1.7e-13 (send) / 2.1e-13 (final) — the peaks
still come from Phase 1's slope-24 segment; every shimmer segment sits
at ≤5e-14, including both re-engagements. PASS at 1e-6 with ~6.5 orders
of margin.

**Scenario gotcha (cost an hour):** the first shimmer segment restored
`osc2_blend` from 0 with hard-pan WIDE still engaged → Haas RE-engage →
the documented Phase-1 stale-ring transient polluted every following
segment (looked like a shimmer failure at 1e-1; the osc-bank tap proved
inputs bit-identical). Same trap again with fast→slow send re-entry.
Phase-1 known divergences constrain scenario ORDER for all later phases:
engage Haas/slow-sends once, or keep re-engages out of measured segments.

### Phase 3 — piano chain — DONE (2026-07-22, awaiting Pi 4 soak)
LA-2A comp (careful: its soft-knee is the CORRECT reference formula),
4-band piano EQ + voicing curves, velocity-brightness filter, tremolo.
FluidSynth stays (sample playback). Flag: `STAVE_FAUST_PIANO_CHAIN`.

### Phase 3 implementation notes (shipped 2026-07-22, awaiting Pi 4 soak)
Files: `faust/piano_chain.dsp` (StavePianoChain, 2-in/3-out),
`stave_synth/faust_piano_chain.py`, `tools/compare_piano_chain.py`,
`config.USE_FAUST_PIANO_CHAIN`, surgical branch in
`fluidsynth_player.render_block` (+ `_compress` split into a shared
`_comp_gain_ramp`). Flag OFF by default — Pi 5 behavior unchanged
(verified: flag-off render EXACTLY 0.0 vs a pre-change baseline over a
1200-block piano-heavy scenario with the identical stored int16 source).

**Included:** volume gain (Python owns the ~10 ms one-pole + dB curve +
≤0.001 hard-zero and pushes ONE block-constant zone; gain 0.0 mirrors
np.zeros_like — filters keep processing zeros, state decays identically) ·
24 dB low-cut (2× cascaded RBJ highpass, always running) · 24 dB high-cut
(2× cascaded RBJ lowpass, always running) · 4-band parametric EQ
(BiquadPeakingEQ mirror — NB its q clamp is 20, not the lowpass's 10;
per-band `enabled` uses the Phase-2 FROZEN-state biquad so a disabled
band's zi holds exactly like Python skipping .process(), and re-enable is
sample-exact even after retuning while frozen) · comp-sidechain mono tap
(output 3 = post-EQ (L+R)·0.5) · int16 conversion lands directly in the
wrapper's persistent input rows (np.multiply(int16_view, 2⁻¹⁵, out=row)
is bit-identical to astype-then-multiply).

**Excluded (stays Python — follow-the-code):** the LA-2A compressor
ENTIRELY — its envelope is block-rate (one RMS per block, attack/release
coefficients derived from block length) and the resulting gain ramp
applies to the SAME block the RMS was measured from; Faust can't see the
block boundary, and a sample-rate envelope would change the sonics. The
flag path feeds `_comp_gain_ramp` from the module's mono output (two
vector multiplies left in Python). · velocity brightness — per-note-event
parameterized (tracker updates in note_on), per-block work is two scalar
ops + one biquad pair, and it sits POST-comp (porting it would need a
second Faust island). · tremolo — post-comp, preset-gated (Suitcase
only), vectorized numpy sin. · piano-room reverb — already its own Faust
island. · silence skip / disabled early-outs — upstream of the branch;
skipped blocks never call compute(), freezing module state exactly like
Python's untouched filters.

**LA-2A divide-hazard resolution (CR#9 dumb-list item 6):** the legacy
wet path recovers the gain ramp as `compressed / safe_mono` — a
per-sample divide with an epsilon guard (|mono| ≤ 1e-10 → that sample's
wet path silently drops to dry, even when L ≈ −R with loud channels).
`_compress` was split so the ramp is returned directly; the flag path
applies `blend = (1−w) + ramp·w` with no divide. Equivalence PROVEN on
real piano material (harness records every Python-player (mono, ramp)
pair): max |divide-recovered − direct| = 4.4e-16 (~1 ulp) over 327,680
guarded sidechain samples; 10,679 guard trips occurred — ALL inside the
volume-zero segment where the signal itself is ≤ ~1e-9, and they are the
sole source of the full-run parity peak (legacy drops the wet path on
those samples; ramp-direct keeps it — strictly more correct). Python
path untouched: `_compress` still returns `samples * ramp` byte-identically.

**Precision:** same `-double + -DFAUSTFLOAT=double` build as pad_bus —
the 150–300 Hz EQ bells and 40 Hz low cut are pole-near-unity biquads
that can't hold 1e-6 in float32; double IO gives zero-copy float64
blocks. Same DF2-transposed recurrence as pad_bus (scipy lfilter /
bridge_biquad exact). Coefficients recompute per block from zones —
Python's setters keep attrs and filter coefficients in lockstep, so the
recompute always lands the same values (retuning a DISABLED band works
the same way: next enabled block sees the new coefficients on both paths).

**Panic:** Python's all_notes_off resets only comp state (+piano-room
clear) and NEVER resets the chain biquads — mirrored exactly: nothing
calls `FaustPianoChain.clear()` in normal operation. clear() itself does
a FULL init (the Phase-2 rwtable gotcha pattern; no init-time zones to
re-push here) and is smoke-tested: post-clear silence is exact 0.0,
signal processes after.

**Parity (tools/compare_piano_chain.py):** input identity by
construction — ONE real FluidSynth instance (FluidR3_GM grand, fixed
schedule, reverb/chorus off) pre-renders the int16 stream once; both
players replay the SAME array through a dummy `fs` stub (chosen over two
live FluidSynth engines: no 2× soundfont RAM, no reliance on assumed
cross-instance determinism). 1400 blocks covering comp on/wet-sweep/
off/on + drive, voicing switches, EQ band retune + freeze + retune-while-
frozen + re-engage, volume sweeps incl. hard-zero + recovery, velbright
sweeps, tremolo, piano-room off/on. Result: peak |Δ| 1.42e-9 (L) /
1.44e-9 (R) — PASS at 1e-6 with ~2.8 orders of margin. The peak lives
entirely in the volume-zero segment and is the documented epsilon-guard
divergence at epsilon-level signal; every other segment is ≤1.5e-13.

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
- `-(expr)` in Faust is PARTIAL APPLICATION of subtraction (`_ - expr`,
  a 1-input block), not unary negation — it silently adds a phantom input
  to whatever consumes it (biquad b1 coefficient bug, Phase 1). Write
  `0.0 - expr`.
- A `with`-local function that closes over outer args gets lambda-lifted
  inside `~` — define the recursion tick at top level and pass the
  captured values as explicit arguments.
- Faust initializes rwtable contents in `instanceConstants`, NOT in
  `instanceClear` — a panic clear() that only calls instanceClear leaves
  the table full. Call full `init` and re-push any init-time zones
  (Phase 2 cloud ring).
- Multiple `rwtable` reads DO share one table when (size, init, widx,
  wsig) are structurally identical — hash-consing merges them (verified
  in generated C: one `ftbl0`, 8 reads). Use this for multi-tap.
- Faust `%` on ints is C-semantics (can go negative) — add the modulus
  before taking it (`(w - off + len) % len`), unlike Python's `%`.
- `int(sec * SR)` tap offsets must be computed on the PYTHON side and
  pushed as zones — float truncation differs between formulations
  (`int(0.289·96000) == 27743`, not 27744).
- To mirror Python code that SKIPS processing (frozen zi / frozen ring
  index), don't input-gate (state decays) — freeze: select2-hold the
  biquad state registers, advance the rwtable write index by the gate.
  Makes re-engagement sample-exact instead of transition-divergent.
- Faust NORMALIZES arithmetic: `1-d + d*(1+r)*0.5` came out of codegen as
  `d*(0.5*(r+1)-1)+1` — algebraically equal, ~1-ulp different rounding.
  Fine for outputs (under the parity bar) but NEVER let canonical state
  flow through such an expression — keep state in its own recurrence
  (which Faust preserves verbatim) so ulps can't accumulate (Phase 4
  smoother: bit-exact; only the gate/pan output composition rounds).
- Bargraph zones make block-end STATE READBACK possible: hbargraph in
  scalar C writes the zone every sample, so after compute it holds the
  last sample's value. Record bargraph zones in the UI glue's _bar
  callback and read post-compute (Phase 4 smoother-state handoff;
  bus_comp's gr_db was the precedent). Keep the signal alive with attach.
- Faust has no block boundaries, but you can synthesize one: Python bumps
  a counter zone once per block; `zone != zone'` fires exactly on the
  first sample of the next compute (slider values are block-cached, the
  one-sample delay carries the previous block's value across the call).
  Drives ramp-index reset + zone-loaded state (Phase 4 i/(n-1) ramps).
- np.linspace forcibly sets its endpoint (y[-1] = stop exactly);
  `(n-1)·(1/(n-1))` in Faust can be 1 ulp off 1.0 — push n-1 as a zone
  and select2-snap the last sample's t to exactly 1.0.

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
- **2026-07-22** — Phase 1 implemented + parity-proven offline (peak |Δ|
  2.9e-13 across dry/send/final over a 1300-block torture scenario — see
  implementation notes above). Flag default OFF; not yet deployed
  anywhere. Next: Pi 4 deploy → render % measurement → robot ears →
  user ear check.
- **2026-07-22** — Phase 1 deployed to Pi 4 (commit 184b853, flag ON in its
  faust.conf drop-in). Real-patch chords: 75.2% → 73.5% render; robot ears
  clean (0 dropouts / 0 clicks). Modest delta is expected — Phase 1 scope
  excluded LFO application and the fast-path send copy; the structural win
  arrives with Phase 4's osc_bank→pad_bus merge (kills the inter-module
  buffer shuttling). Pi 5: flag still OFF, nothing changed.
- **2026-07-22** — Phase 2 (shimmer chain) implemented as a pad_bus
  extension (no new flag/module) + parity-proven offline: full-run peak
  |Δ| 2.9e-13 dry / 1.7e-13 send / 2.1e-13 final over a 2100-block
  scenario; shimmer segments ≤5e-14 with frozen-state emulation making
  on/off re-engagement sample-exact. Flag-off path verified bit-identical
  (Δ = 0.0). panic clear() upgraded to full re-init (rwtable gotcha).
  Not yet deployed anywhere. Next: Pi 4 rebuild (`faust/build.sh` picks
  up the .dsp timestamp automatically) → render % → robot ears → user
  ear check with shimmer + CLOUD engaged.
- **2026-07-22 (later)** — Phase 2 deployed to Pi 4 (commit 8369ea9).
  Chords + shimmer + CLOUD: 74.9% render, 0 dropouts. Robot-ears
  false-positive discovered: the |delta|>0.25 absolute click threshold
  trips on legitimate hot bright content (shimmer at 0.6 mix, peak 0.36)
  — Python-path A/B showed equivalent counts (153 vs 190, run variance),
  |delta|>0.5 both ~1-2. TODO: make the click detector relative to local
  signal statistics. Pi 5: still flag-off. Next: Phase 3 (piano chain) or
  Phase 4 (osc_bank→pad_bus merge — the big CPU win); user ear check on
  Phase 2 shimmer pending.
- **2026-07-22 (user ear-check, in office)** — "Sounds fantastic once I
  adjusted settings." Phases 1+2 APPROVED by ear. Full blast: ~80% with
  occasional CPU spikes (Phase 4's merge is the targeted fix), a few
  clips at hot settings (parked for the post-port tuning session:
  limiter/gain/comp re-dial). User verdict: "full Faust is absolutely
  the best call for CPU space."
- **2026-07-22** — Phase 3 (piano chain) implemented + parity-proven
  offline: peak |Δ| 1.42e-9 L / 1.44e-9 R over a 1400-block real-piano
  scenario (pre-rendered FluidSynth feed, identical to both players);
  outside the volume-zero segment every |Δ| ≤ 1.5e-13, and the peak is
  the documented divide-guard divergence at epsilon-level signal. LA-2A
  stays Python (block-rate envelope), fed by the module's mono tap; its
  wet-path divide replaced with direct ramp application (proven ≤4.4e-16
  from the divide form on 327k real sidechain samples). Flag-off render
  verified EXACTLY 0.0 vs a pre-change baseline. Flag default OFF; not
  yet deployed anywhere. Next: Pi 4 deploy (build.sh picks up the new
  .dsp; add STAVE_FAUST_PIANO_CHAIN=1 to the faust.conf drop-in) →
  render % with piano-heavy patch → robot ears → user ear check.
- **2026-07-22 (later)** — Phase 3 deployed to Pi 4 (commit 970c6c5,
  STAVE_FAUST_PIANO_CHAIN=1 in its drop-in). Chords with all 3 phases
  live: **70.5%** render (was 75.2 pre-port), robot ears clean (0
  dropouts). Pi 5: all port flags still OFF. REMAINING: Phase 4 (the
  osc_bank→pad_bus merge — targeted fix for the full-blast CPU spikes
  the user hears) + ring ratchet with user present + the post-port
  tuning session (voicings/comp re-dial/limiter/gain). User ear check
  on Phase 3 piano pending.
- **2026-07-22** — Phase 4 (Design A) implemented + parity-proven offline:
  C shim (`faust/merged_shim.c`, pointer-based — lite-bank-agnostic) fuses
  bank compute → f32→f64 → pad-bus compute into ONE cffi call; LFO amp/pan
  block ramps + one-pole smoothing + dip-only gate ported into pad_bus.dsp
  for fast-path (all-recv) blocks with Python-canonical smoother state
  (zone in / bargraph out — fallback blocks sample-exact). 3-run harness
  over 2950 blocks: merged-path peaks identical to the two-call path
  (2.85e-13), all 12 LFO segments ≤ 4.1e-14. Flag-off AND merged-off
  renders EXACTLY 0.0 vs pre-change baselines. Panic smoke green. Flag
  `STAVE_FAUST_MERGED` default OFF; NOT committed, NOT deployed anywhere
  — Pi 5 untouched. Next: commit → Pi 4 deploy (steps in the Phase 4
  notes) → render % + full-blast spike check → user ear check → then the
  live-session items (8-slot decision, ring ratchet) with the user.

### Phase 4 design brief (authored 2026-07-22, pre-implementation)
**Goal:** collapse the Python sandwich between osc_bank and pad_bus — the
per-block glue (f32→f64 conversions, buffer shuttling, LFO application in
numpy) that causes the full-blast CPU spikes the user hears.

**Design A — C shim (BUILD THIS FIRST):** one C function (new file
faust/merged_shim.c or extension of jack_bridge.c — builder's choice,
document it) that calls computeStaveOscBank → converts f32→double in C →
computes StavePadBus, buffers handed in-memory. ONE cffi call per block.
Each module keeps its own precision + existing parity proofs. Flag:
STAVE_FAUST_MERGED (requires OSC_BANK + PAD_BUS flags; default OFF).

**LFO ramp port (the delicate part):** Python currently applies LFO
amp/pan BETWEEN the modules as an exact linear per-block ramp (start→end,
anti-click by design — read the _osc_amp_pan region comments). Move the
application into pad_bus.dsp as per-bus linear block ramps: Python
computes each block's start/end gain per bus (osc1_l/r, osc2_l/r — study
the actual bus structure) and pushes BOTH as zones; the .dsp interpolates
(i/n ramp — NOT si.smoo). Must honor per-OSC LFO receive flags
(osc*_recv_lfo*), both LFO targets (amp gate formula + pan), spread,
poly-LFO interaction (poly amp is already inside osc_bank — verify no
double-application), and the target=="bus"/"filter" cases which do NOT
apply here (filter LFO already feeds the cutoff zones; bus target applies
post-mix in jack_engine — leave both alone).

**Parity:** extend tools/compare_pad_bus.py with LFO-active scenarios
(amp + pan targets, both LFOs, spread, recv-flag combinations, depth
ramps, poly on/off) and a merged-path run — bar unchanged (~1e-6, expect
much better). Flag-off delta must be exactly 0.0.

**Explicitly OUT of the builder's scope (needs the user present):**
8-slot bank decision (sustain-pedal ear test) and the ring ratchet
(16/8 → 12/6 → 10/5 → …, robot ears + user feel per step, stop at first
starvation; latency NEVER goes up). These happen in a live session after
the merge soaks.

**Design B (mega-module, single Faust graph) is fallback ONLY** if the
shim + LFO port leaves the spikes unresolved — it forces a single
precision and invalidates two parity harnesses; do not attempt first.

### Phase 4 implementation notes (shipped 2026-07-22, Design A, NOT yet committed/deployed)
Files: `faust/merged_shim.c` (NEW — plain C, not a Faust module; built by a
new plain-gcc step in `faust/build.sh`; NB `.gitignore` gained
`!faust/merged_shim.c` — the blanket `faust/*.c` rule for generated files
would have silently kept this hand-written source out of the commit and
broken the Pi 4 build), `stave_synth/faust_merged.py`
(NEW wrapper), `faust/pad_bus.dsp` (Phase 4 LFO section, still 5-in/9-out),
`stave_synth/faust_pad_bus.py` (`push_lfo_block` / `read_lfo_states`,
bargraph capture in the UI glue), `config.USE_FAUST_MERGED`, surgical
branches in `synth_engine._render_locked`, `tools/compare_pad_bus.py`
extended 2100 → 2950 blocks (12 LFO segments) and to a 3-run comparison.
Flag `STAVE_FAUST_MERGED` default OFF; requires OSC_BANK + PAD_BUS and
engages per block only when the Faust bank produced the block. Pi 5
behavior unchanged — VERIFIED: all-flags-off render EXACTLY 0.0 vs a
pre-change baseline over the full 2950-block scenario (dry/send/final all
0.0), and the two-call pad-bus path (PAD_BUS on, MERGED off) is ALSO
exactly 0.0 against its pre-change baseline even with the rebuilt .so
(the LFO section's ×1.0/+0.0 identity is IEEE-exact and gcc emitted the
existing math unchanged).

**Included — C shim:** `stave_merged_compute` runs computeStaveOscBank →
f32→f64 widen (exact, in C) → computeStavePadBus as ONE cffi call.
Builder's choice per brief: new file `faust/merged_shim.c`, NOT
jack_bridge.c (that file is audio-callback territory — routing-caution
rule). It links against nothing: compute-function pointers AND both dsp
pointers arrive from Python as void* (cast via uintptr_t across FFI
instances), so the 12-slot lite bank works unchanged — whichever .so
faust_osc_bank.py selected is what gets called (verified by construction:
lite exports identical symbols; only the full bank was runnable on this
Pi 5). Each module keeps its own precision (bank f32, pad bus double) and
its existing parity proofs. The wrapper owns all buffers (bank f32 rows,
the f64 conversion block — returned to the engine as osc_out for the
magnitude taps — and the 9-row output), realloc only on block-size change.

**Included — LFO ramp port (fast path):** pad_bus.dsp now applies, on the
post-highpass dry bus, the EXACT Python fast-path composition
`dry := (dry × amp_gate₁·amp_gate₂) × (1 + pan₁ + pan₂)`:
linear block ramps start→end as `i/(n-1)` with the last sample snapped to
exactly 1.0 (np.linspace endpoint semantics), NOT si.smoo · the optional
`lfo_smooth` one-pole (scipy-lfilter DF2T recurrence `y=(1-a)x+z, z=ay,
zi=a·state` — bit-exact, incl. `a=0.0` == Python's ≤0.001 passthrough) ·
the dip-only amp gate `1-d+d(1+r)/2` copied exactly (peaks at 1.0, no
makeup — limiter-safe by construction) · pan `±(r_a·d/2)` where only the
A channel drives pan and the spread/B channel is amp-R only (as in
Python) · per-OSC recv flags, poly, target and depth conditions are all
folded into the `en/is_amp` zones by Python, mirroring _osc_amp_pan's
branch logic — poly amp LFOs push en=0 (osc_bank applies them per voice:
no double application, exercised by the harness poly on/off segments).

**State ownership (the load-bearing design):** the module holds NO
cross-block LFO state. Python bumps a counter zone per merged block; the
.dsp detects block start via `ctr != ctr'`, resets the ramp index and
loads the smoother state from a ZONE; block-end states export through
bargraphs which the engine syncs back into `_lfo*_smooth_*_state` — for
exactly the LFOs whose ramps Faust computed that block (mirrors which
_smooth_one_pole calls Python would have made, incl. the bus-target and
split-path stepping patterns). Result: Python-fallback blocks and
Faust blocks interleave sample-exactly on the same state variables.

**Excluded (stays Python — per-block fallback, documented):** the per-OSC
recv SPLIT path (`not all_recv`) — its L/R split ratio is data-dependent
on THIS block's osc-bank output magnitudes, which don't exist until the
merged call has run; blocks on the split path get en=0 zones and the
legacy Python application (harness segments 2450–2590 prove the
transition both ways) · the bus-target LFO (post-reverb sidechain pump,
applies far downstream) · the filter-target LFO (already feeds the cutoff
zones) · _advance_lfo itself + all scalar work (rate/tempo/S&H/spread
phase math) · poly amp application (already inside osc_bank.dsp).

**Plan corrections found while porting (follow-the-code):** the brief's
"per-bus start/end gain per bus (osc1_l/r, osc2_l/r)" model is not how
the code works — the fast path applies ONE combined mod to the combined
post-filter dry bus, and per-OSC routing exists only in the split path
via the data-dependent magnitude ratio (hence the fallback, not a
per-OSC-bus port). Also what Python pushes are the raw bipolar RAMP
ENDPOINTS, not gains — the smoother filters the raw ramp BEFORE the
gate/pan formulas, so pushing "gains" would reorder the smoothing. The
brief also didn't mention `lfo_smooth`; it must move with the
application, solved via the zone-state/bargraph handoff above. One
non-issue discovered: dead-voice gate clears were feared to reorder
around the deferred bank compute, but the pre-compute unused-slot sweep
already zeroes them before process() on BOTH paths — no divergence.

**Precision:** merged-path output is not bit-identical to Python (unlike
the state path): Faust normalized the gate to `d*(0.5*(r+1)-1)+1` and
left-associated the multiply chain — ~1-ulp per sample, non-accumulating
because the smoother recurrence (the only carried quantity) IS bit-exact.
Measured: every LFO segment ≤ 4.1e-14 (indistinguishable from run B's
Phase-1 background deltas).

**Ordering equivalence (merged block):** bank zones + gate clears land
before the fused compute exactly as before; the pre-filter tap adds,
osc2_accum accumulation and the _osc2_pre snapshot are deferred to just
after the merged call (pure reads of osc_out — order-equivalent); RNG
call order is untouched (no RNG between the old and new compute sites).

**Panic:** nothing new to flush — the shim is stateless, FaustOscBank
.panic() and FaustPadBus.clear() (full re-init per the Phase-2 rwtable
gotcha) already cover the real state, the LFO zones reset to their
inert defaults (en=0) on init, and the smoother state lives in Python
(reset in _apply_panic as before). Smoke-tested on the merged path:
post-panic output exactly 0.0 for 10 blocks, notes sound after, state
readback still in sync. NB: the smoother state does NOT stay 0 after
panic — the LFO keeps ramping on silent blocks, identical to Python.

**Parity (tools/compare_pad_bus.py, 3 runs × 2950 blocks):**
run B (two-call) vs Python: peak |Δ| 2.62e-13/2.85e-13 dry, 1.57e-13/
1.70e-13 send, 2.09e-13/1.92e-13 final — unchanged from Phase 2/3 (peaks
still live in the slope-24 segment). run C (MERGED + in-Faust LFO) vs
Python: same peaks to the digit, and every Phase-4 LFO segment ≤ 4.1e-14
across all channels — including recv-split fallback, all-recv re-engage
(state handoff), poly on/off, bus-target coexistence, saw + smooth, and
depth-cap dual-gate stacking. PASS at 1e-6 with ~6.5 orders of margin.
Engagement positively proven (not silent fallback): shim buffers sized,
push_lfo_block ran every block, two-call process() never allocated, amp
swing 3.6× audible, states == bargraphs.

**Pi 5 bench (informational — the target is the Pi 4):** 8-note chord +
shimmer + both LFOs, 256-sample blocks: 2009 → 1945 µs/block (−64 µs,
−3.2% of render). The Pi 4 delta should be proportionally larger (slower
cores, same fixed Python overhead removed); the real success metric is
the full-blast CPU-spike behavior there.

**Exact Pi 4 enablement steps (after this is committed):**
1. Commit this work on the Pi 5 (NOT done by the builder — session rule)
   and `git pull` on the Pi 4.
2. `cd ~/stave-synth/faust && ./build.sh` — picks up pad_bus.dsp and the
   new merged_shim.c automatically (no --force needed); confirm
   `libstave_merged_shim.so` and a fresh `libstave_pad_bus.so` appear.
   The Pi 4's lite bank needs no special handling (pointer-based shim).
3. Add `Environment=STAVE_FAUST_MERGED=1` to
   `~/.config/systemd/user/stave-synth.service.d/faust.conf` (alongside
   the existing OSC_BANK / PAD_BUS / PIANO_CHAIN lines).
4. `systemctl --user daemon-reload && systemctl --user restart
   stave-synth` — NOT on a Saturday night.
5. Verify engagement in the journal log: "merged shim: single-call
   osc_bank→pad_bus path (STAVE_FAUST_MERGED=1)".
6. Measure render % (same-ssh-session PID capture), robot ears (use the
   |Δ|>0.5 threshold pending the relative-click-detector TODO), then the
   full-blast spike check with LFOs + shimmer + CLOUD engaged, then user
   ear check. Pi 5: flag stays OFF until the Pi 4 soak passes.
- **2026-07-22 (Phase 4 deployed)** — commit c78686f, STAVE_FAUST_MERGED=1
  on the Pi 4. Max-everything + BOTH LFOs (harder than any prior test):
  69.8-76.3% render, **0 dropouts** in the DAC capture, 1 starvation
  across the whole storm (was 79.8-85.8% / 1-4 dropouts without LFOs).
  The full-blast spike scenario is resolved. ALL FOUR PORT PHASES LIVE
  ON THE PI 4. Remaining (live sessions with the user): Phase 3+4 ear
  checks, 8-slot decision, ring ratchet (the latency prize), post-port
  tuning pass (voicings/comp/limiter/gain). Pi 5: all port flags still
  OFF, awaiting soak + the adoption checklist.
