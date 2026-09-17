# Native finishing checkpoint — September17,2026

User finished playing/muted and authorized maintenance. Keep the accepted512
profile. No persistent replacement service, Pi5 change, saved-patch migration
or live256 change was performed. This checkpoint is NOT stage-release approval.

## Verified this batch

Piano brightness: target sourcee080d34, isolated Pi directory
`/home/codyvanscyoc/stave-v2-finish-20260917.6Hhv6M`. Initial object-reuse attempt
correctly refused a report that did not contain original compile evidence;
service restored. Retry used hash-verified originalgpQ10J DSP artifacts and
rebuilt owners. `evidence-retry/report.json` passes. The failed initial report
was retained, not overwritten. Existing accepted listening executable unchanged.

Muted real-JACK tone sweep:94.298724s,45 chords/360 notes,341 acknowledged
controls,8,849 callback-block delta,0 xruns/over-budget/raw-piano-full-scale/
unsupported-channel messages. Worst callback5.17846ms; temperatures69.627–72.549°C.
Master/startup gate stayed0. Actual independent piano brightness and synth
cutoff moved while both oscillators/effects ran. This is real callback evidence,
not an audible brightness audition or measured keyboard-to-analogue latency.

Sampled-bed graph integration sourcecc38d7f: isolated Pi directory
`/home/codyvanscyoc/stave-v2-bed-20260917.NCfVqk`. `BedBus` adds independent level,
mellow and reversible sample-clock fade. The actual StageInstrument mixes bed
at the original0.85 factor into mixed/dry, not FX stems, before master/limiter.
Keyboard release/pedals do not stop the independent bed. Terminal STOP does.
20ms level smoothing and sample-clock cubic fades are intentional transition
improvements, not a claim of bit-exact legacy background-thread fade timing.

600 integrated bed guard blocks at512/256 compare exact explicit routing plus
the independent bed bus;1,400 existing full-instrument guards also pass. No
scoped C++new/new[] during render. Standalone original SamplePlayer oracle:
12 fixtures/10,200 blocks, predeclared max absolute1e-10, passes on Mac and Pi.

Pi ten offline benchmark cases,400 measured blocks each plus100 warm-up each:

| Dense case | Mean callback-equivalent work | Worst | Block budget | Misses |
| --- | --- | --- | --- | --- |
|512, piano/OSC/effects/transitions |4.038343ms |5.413375ms |10.666667ms |0 |
|512, above plus sampled beds |4.100814ms |5.558649ms |10.666667ms |0 |
|256, above plus sampled beds |2.063967ms |3.391188ms |5.333333ms |0 |

All4,000 measured blocks within budget. Bed case:12 oscillator voices, five
overlapping active beds observed,12 synthetic test slots/9,223,104 PCM bytes.
These are unpaced offline throughput tests, not a live256 recommendation,
whole-system CPU measurement, exhaustive twelve-active-bed load or physical
latency. Benchmark temperatures73.036–74.984°C, firmware0x0. Dense raw piano
int16 full-scale counts remain4/8 at512/256; do not silently close that warning.

## Recorder transport (source8fb7cb3)

`RecordingCapture` is a one-take fixed SPSC queue. Default128 slots occupy
512KiB of PCM storage; maximum duration30minutes. Construct/destroy off audio,
with owners stopped. Callback pushes float32 stereo from the original
pre-volume/BTL tap; one worker consumes copies. No callback file access,
locks, waits, notifications, allocations or asset retirement. Overflow,
invalid audio, duration limit and writer error end capture independently of
instrument health; queued valid blocks remain drainable. No reset/reuse race.
An empty queue is not completion. Producer Complete also is NOT proof that a
WAV was saved: writer failure can occur afterward while draining/finalizing.

Actual AuditionSession optional-tap tests prove exact tap samples while physical
output is muted, and continued healthy rendering after intentional queue-full.
Standalone UBSan tests cover8,000 concurrent blocks, wrap/order/exact payload,
invalid PCM, stop/failure/limit/drain and scoped zero-new/new[]. Mac reviewed
regression569 tests, zero skips, plus Node checks pass (14.692s); full native
C++/UBSan audition and fake-JACK guards pass. Mac ThreadSanitizer attempt exits139
without diagnostics, also on a minimal unrelated std::thread smoke program
outside the sandbox. Race-sanitizer qualification remains open, not waived.

The same standalone recorder guard passes on the Pi using GCC14.2/UBSan in
`/home/codyvanscyoc/stave-v2-capture-20260917.PqAL9z`; source8fb7cb3. StagePID722136
unchanged across this small device-free check, temperature72.549→73.523°C,
firmware0x0. Binary SHA256
`3f670d04c9d69fefa158a1cacb6620152f6f35460af76850f85e924b6711cf50`.
This confirms queue ownership tests on ARM64, not live recorder timing.

This does NOT provide a disk worker, valid WAV, UI recording action, saved take
or record-to-pad workflow yet. The live audition host does not attach a take.
Do not claim recording works merely because transport/component tests pass.

## Restoration and saved evidence

Working service restoredPID722136,NRestarts0, same cleanf600c7e rehearsal
checkout; verified HTTP8080, WebSocket8765, native/audio healthy48k/512, no
candidate/probe JACK client. Current saved state byte-identical to stopped
backup, SHA256`2f366d3c5dfad79e5a4e343e2c6800f7d598a1cd8c213b28ab16ef62dad93872`.
Restoration temperature72.549°C, firmware0x0. Independent restore timers were
canceled only after service start succeeded. No test audition is left on8082.

Private Mac backup root `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups`:

- `stave-v2-finish-bed-evidence-20260917.tar.gz`: both isolated source/build/log/
  settings directories; SHA256
  `1b47aa218fbfc5e5b83c9bf030c80e0d26dc69165b57030163ba02b0bef9c879`,
  matched Pi and Mac before extraction.
- `stave-v2-bed-20260917.NCfVqk/evidence/report.json`: SHA256
  `a0633e82bc2ed32ca30f9d2e039c3483116befa5aa57fdd3521b69b8463c2298`.
- `stave-v2-finish-20260917.6Hhv6M/tone-live-evidence/report.json`: SHA256
  `d2f5ede7e5ae2b01daf7cb18e87a82f894fc0abf8fe59fb8a5678ecee835e8b1`.
- `native-v2-capture-connected-20260917/report.json`: SHA256
  `6f1f155dadebb3cd1236bc43da8346b002cd0a5d0376993e48f77ae9d4025e75`.

Runtime/settings archives stay private, not in Git. The accepted listening tag
and original Pi4 fallback remain intact. Recheck service ownership on resume.

## Next implementation gates, in order

1. Off-audio WAV decode/preparation and immutable pad-bank handoff; preserve
   existing key recordings, failed-load rollback and aggregate memory bounds.
   Connect bed level/key/rise/mellow/fade to acknowledged controls. The current
   live host does not preload the bank despite the integrated graph being ready.
2. Disk worker/finalization using the capture queue: exclusive take creation,
   bounded storage, durable WAV/metadata, honest dropped/incomplete/stopping
   state, no new take until old owners retire. Test full/slow/error storage and
   shutdown. Connect explicit saved-take-to-key workflow transactionally.
3. Stage/Edit/System UI, saved state/preset compatibility and explicit feature
   ledger for organ/programs/pitch bend/MIDI maps/macros/scenes. Restore normal
   hostname via explicit Host/Origin policy; do not broadly weaken it.
4. Reversible512 candidate with combined feature load, startup, device-late/
   disconnect/reconnect, and independent UI recovery. Then player rehearsal.
   No eight-hour soak was requested. Keep v1.2 recoverable throughout.

High-gain independent numerical parity and dense raw piano headroom remain
open alongside these integration gates. Successful player feedback is valuable
evidence, not proof every sound, device or recovery path is qualified.
