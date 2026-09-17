# Recorded-pad startup and controls — September17,2026

Sourceeee5b25 adds a real WAV-library → immutable native-bank → acknowledged
bed-control path. It does not finish recording, live asset replacement, or the
full product migration. Keep the accepted512 profile and original stage fallback.

## What is connected

The temporary launcher accepts an explicit `--pad-library DIRECTORY`; there is
no home-library default. It prepares a private bank before starting the native
child. Native accepts `--bed-bank FILE`, validates it independently, constructs
loop overlap before audio, and hands sole ownership to StageInstrument. No file
read, decode, resample, asset allocation or retirement occurs in the callback.
Temporary bank files are removed after the child exits. The source WAVs are
read-only; this does not alter saved state or the running v1.2 library.

Canonical names remain `pad_C.wav`, `pad_Cs.wav` through `pad_B.wav`, slots0–11
corresponding to the original MIDI60–71 recordings. Keys are concert pitch,
independent of played MIDI notes, pedals and keyboard transpose. Missing files
remain missing: no synthesized replacement, transposition or fabricated sample.
A malformed PRESENT recording aborts the new startup bank; it is not silently
discarded. An incomplete preparation retains a non-ready magic header and the
native loader refuses it. Existing destination bundles cannot be overwritten.

Supported source formats follow SamplePlayer: mono/stereo little-endian PCM
8/16/24/32 and float32/64, including extensible PCM/float,8k–192k subject to the
existing resampling-ratio bound. Real polyphase resampling targets48k. No gain
normalization is applied. Files and bundles must be regular/non-symlink; decode
checks source identity/size/timestamps and finite samples. RIFF chunk bounds,
native entry count/index/duplicates/truncation/trailing data and byte budgets
are validated before publishing the bank.

The native representation remains double stereo:32MiB per slot,128MiB total;
the slot limit corresponds to about43.69s at48k. Some long legacy mono/float32
files therefore need a future storage-policy review instead of silent truncation.
Decoder scratch retains the128MiB preparation estimate and exports small chunks;
native preload additionally needs one slot's scratch while the new bank is built.
These are component bounds, NOT measured whole-application peak memory.

The temporary UI now shows12 key buttons and independent bed level, rise/peak
filter, mellow/cutoff,5s fade-out/in and4s release. Loaded-key mask, selected key
and sounding/releasing count come from native telemetry. Missing keys and all
bed controls on an empty bank are disabled. Missing-key commands are rejected
without consuming a sequence or faulting the instrument. Key triggers retain
the existing fade target; Fade in restores a deliberately faded bed. Keyboard
ReleaseAll leaves the independent bed alone; dedicated bed release ends it.

This is immutable STARTUP loading, not hot replacement. A live record-to-pad
feature still requires off-audio prepared-asset installation/retirement with
failure rollback and no interruption of a sounding bed. Do not offer a live
Load/Record button before that ownership path is implemented and tested.

## Local evidence

Reviewed offline suite:581 tests/zero skips plus Node checks,16.132s on Mac
(includes the added panel execution test in source19aca73).
New asset tests compare all six PCM/float formats, mono/stereo, native48k and
real44.1→48k resampling against the original SamplePlayer (24 combinations,
exact arrays). Actual Python-prepared bundle → native loader → native loop
playback matches the original player within the predeclared1e-10 limit.
Additional guards cover corruption, nonfinite audio, symlinks, aggregate/slot
bounds, empty libraries, source preservation, exclusive destination and failed
preparation. Launcher mock tests prove preparation precedes child launch,
failure prevents launch, and private files are cleaned after child exit.

Full C++/UBSan graph/audition/fake-JACK checks pass at512/256, including actual
loaded-key controls and CLI bank loading/refusal before JACK open. Private Mac
`native-v2-bed-controls-final-20260917/report.json` SHA256
`e5b0240af0f6a7f4120f2e2edcede112054290c23b2861352f0a7f1cf15cf195`.
Additional Node execution of the actual panel script verifies missing-key
disable, native-selected-key display, bed actions, stale disable and no replay
on reconnect. This is not actual Safari/touch/layout qualification.

## Library inventory and target work

Read-only Pi check found the active pad-sample directory empty. The saved
September14 full runtime archive also contains an empty pad_samples directory;
no loose canonical pad WAVs were found in the Mac backup tree. This does not
establish whether recordings exist on the Pi5 or elsewhere. No files were
removed, imported or fabricated in the user's library.

The isolated target source/build is
`/home/codyvanscyoc/stave-v2-library-20260917.HHKSwP`, sourceeee5b25. Target
build/UBSan/full-graph/audition/fake-JACK guards and all seven WAV/bank tests
pass. Ten offline benchmarks/4,000 measured blocks have no deadline misses.
Dense combined-bed512 mean4.489510/max5.860667ms against10.666667ms budget;
256 mean2.230700/max3.522603ms against5.333333ms. These unpaced runs do not
measure end-to-end latency or authorize a live256 switch. Dense raw-piano
full-scale counts remain4/8, as in the earlier stress cases.

Muted real-JACK/PipeWire512 test PASSES:94.311159s,360 notes,353 acknowledged
controls,8,856 callback-block delta;0 xruns/over-budget/piano-full-scale/
unsupported-channel MIDI. Maximum callback5.30067ms. Two active beds observed;
native ended with selected key-1 and zero active beds after dedicated release.
Both piano/synth filters moved while key/rise/mellow/fade/release and effects
were exercised. Startup/master gate remained0; no audible/analogue-latency
qualification is implied. Synthetic C/G fixture WAVs belong only to this
isolated test directory, not the user's library or a proposed performance sound.

Working stage restoredPID739029,NRestarts0, same cleanf600c7e rehearsal checkout.
HTTP/WS/native/audio health verified48k/512; no test/probe JACK client remains.
Saved current-state bytes unchanged, SHA256
`2f366d3c5dfad79e5a4e343e2c6800f7d598a1cd8c213b28ab16ef62dad93872`.
User pad library remains empty/unchanged. Restore temperature73.523°C,
firmware0x0. Both build and live test had independent fallback timers, canceled
only after restoration; native test service also had a bounded lifetime and
ExecStopPost restoration. Normal8080 remains the old working application;
temporary8082 audition is not left active.

Private source/build/fixture/log/settings evidence archived on Pi and copied to
Mac `/Users/codyvanscyoc/Documents/stave-synth-pi4-backups/`:

- `stave-v2-library-evidence-20260917.tar.gz`, matching Pi/Mac SHA256
  `47561dac2c37f826ccc6136f25662494bd9ec9f5667ab383694c38b4378357f9`.
- Extracted `stave-v2-library-20260917.HHKSwP/evidence/report.json`, SHA256
  `bb315a4c7ed2d64a5b7d6aa53d551d86aae6619ffc0b8bd9f6ff2e1dcfb5ed52`.
- `stave-v2-library-20260917.HHKSwP/library-live-evidence/report.json`, SHA256
  `b755cf17d71c4e07f3e0320cab4f44242e2a2a225aacdc60a431f4bfe5f09fed`.

Saved-state archives remain private, not in Git. Recheck actual runtime
ownership before any later operation; PID/status above is this checkpoint.

## Next work

Finish recorder disk worker/durable WAV metadata and the live saved-take-to-pad
transaction. Then product UI/persistence/feature parity and reversible512
startup/device recovery qualification before a combined player rehearsal.
No playing test is requested from the user during this implementation batch.
