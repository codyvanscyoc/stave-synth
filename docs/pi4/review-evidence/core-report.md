# Core control, MIDI, bridge, persistence and recorder review

Reviewed 2026-09-14, `pi4-stage-pro` at `4c64f3305a611462c01faa3ad4233f40042f3dcc`. Full-file review: `main.py` (2336 lines), `config.py` (543), `jack_engine.py` (1552), `jack_bridge.c` (438), `recorder.py` (286), `midi_handler.py` (31), `__init__.py` (2), plus cross-references to the other reviewers' files. No production process was started, stopped, reconfigured, or contacted during this pass.

Severity: P1 = resolve before a pro-stage release in the affected supported workflow; P2 = important correctness/hardening or configuration-dependent risk; P3 = lower-impact cleanup. A source finding does not establish the cause of any past stage incident. Reproductions below run exact extracted Python methods or unchanged C source with mocked dependencies; they are not target-hardware audio tests.

## Findings

### C01 — P1: simultaneous state saves defeat the atomic replacement guarantee

`config.py:530-543` uses one `.tmp` filename and unlinks it before every save. Autosave (`main.py:1756`) and UI/MIDI-triggered saves have no shared serialization. One writer can unlink another writer's file, then the first can rename the second writer's partially written file into the real state path. An isolated barrier-controlled test reproduced partial JSON visible at the final path and the second writer's `FileNotFoundError`. This is stronger than merely noting a theoretical rename race. A process kill in that window can leave corrupt state; a single-writer SIGKILL test does not cover it. Use serialized validated snapshots and unique temporary files, plus defined recovery/durability semantics. **High confidence; reproduced.**

### C02 — P1: settings can poison persistent state before rejection

`main.py:1309-1585` validates some controls but stores many raw values before engine conversion (`:1333`, `:1393`, master compressor branch). Piano synthetic EQ indices (`:1380-1389`) have no upper bound: an isolated request for `eq_band1000_gain` creates 1001 persistent bands; larger values scale memory and work with the supplied index. `config.py:419-528` merges without a complete type/range/finite schema; top-level valid JSON such as a list reaches `.get` and raises outside the JSON/OSError handler. Invalid numeric values can persist across restart. Validate whole commands/snapshots before mutating state or DSP, enforce finite bounded values and fixed collection sizes, and reject unknown keys. **High confidence; bounded EQ growth reproduced, other paths statically traced.**

### C03 — P1: panic is not a complete, authoritative stop

`main.py:1111-1125` does not cancel preset/drone ramps, and `jack_engine.py:1509-1529` clears sustain but leaves `_sostenuto_on` and `_sostenuto_held`. The exact method reproduced stale sostenuto after UI panic. Audio review independently confirms sampled beds are not stopped by `SynthEngine._apply_panic`. Pending control work/MIDI can also restart sound after the reset; the bridge only flushes audio, not queued MIDI. Resetting limiter/native state from the control thread is not a render-owner transaction. Define panic across every sound source, pedal, queue and ramp, with acknowledgement after the render owner has applied it. **High confidence; pedal portion reproduced; sampler and interleaving portions statically traced.**

### C04 — P1: piano note ownership fails across octave-only sustained retriggers

`jack_engine.py:1320-1327` releases an old mapped pitch only if the pad transpose changed, not if the piano octave changed. Play a note, sustain it, release the key, change piano octave, then play/release the same raw key: the map is overwritten and the original piano pitch never receives note-off. Reproduced with the exact MIDI loop and event spies. Track each sounding instance and compare both destinations when replacing an entry. **High confidence; reproduced.**

### C05 — P2: sustain and sostenuto are not combined correctly

The two pedal-release branches (`jack_engine.py:1378-1412`) release their captured notes without honoring the other still-held pedal. Reproduced sustain release while sostenuto still holds the note: piano receives premature note-off. Repeated high CC66 values also recapture the held set instead of acting only on the down edge. Important for the requested sustained bass/voicing workflow; less relevant to controllers with only one sustain pedal. **High confidence; overlap reproduced.**

### C06 — P1: JACK callback size and producer block size have no safe change contract

The producer snapshots block size (`jack_engine.py:674` onward), while `jack_bridge.c:119-181` consumes current callback `nframes` and advances an entire slot. No size-change callback reconciles the two. Compiling the unchanged bridge against mock JACK reproduced (a) larger callbacks reading a stale slot tail, and (b) smaller callbacks discarding the block remainder. A sample-rate mismatch is logged rather than rebuilt/refused. Even an initially larger quantum can fail the compressor-enabled saturation fallback: `_sat_scratch` is fixed at 512 (`jack_engine.py:314`, `:1052`). Qualify a fixed size/rate and fail safely, or implement coordinated rebuilding/resampling semantics. Do not lower buffers experimentally on production. **High confidence; size mismatch reproduced, rate/scratch paths statically traced.**

### C07 — P1: audio-ring reset uses delay as a synchronization substitute

`jack_bridge.c:310-357` raises a flag and sleeps 20 ms before resetting shared arrays/indices. A producer or callback that passed the flag check and is then descheduled has not acknowledged leaving its critical region. It may resume across the reset. Two control callers can overlap clear/depth transitions as well. The acquire/release ring publication is a sound improvement for steady-state single-producer/single-consumer operation, but it does not prove the transition protocol. Replace timing assumptions with an explicit quiescence/ownership protocol. **High confidence in missing synchronization; no deterministic stalled-thread reproduction in this pass.**

### C08 — P1: MIDI overload can silently discard note-offs

`jack_bridge.c:188-202` has 511 usable events and drops every further event when full, with no overflow counter or recovery. The same Python dispatch thread runs potentially expensive program/preset/control callbacks. The unchanged C bridge reproduced 511 queued controls followed by a lost note-off. This is an overload scenario, not a claim ordinary keyboard traffic currently saturates it. Preserve critical release semantics, bound control work and expose drops; test clock/aftertouch/CC bursts plus slow program loads. **High confidence; reproduced.**

### C09 — P1: recording/record-to-pad has data-loss and false-success paths

`recorder.py:108-114` opens a second-resolution filename with `wb`: two starts in the same second overwrite the earlier WAV and sidecar. Exact Recorder code reproduced replacement of a 64-frame take by a 32-frame take. `feed` silently drops full-queue blocks (`:177-195`) without the promised gap log/counter; write errors (`:215-225`) log but leave recording active. `delete_take` (`:253-277`) does not protect an active take. `main.py:830-856` copies directly over a pad slot before validation; reload failure does not undo replacement. Audio review covers all-bank reload interruption and unbounded decoded sample RAM. Use unique take identities, truthful error/drop status, active-take protection and validated atomic pad replacement. **High confidence; timestamp overwrite reproduced, remaining paths statically traced.**

### C10 — P1: shutdown does not establish native/recorder worker completion

`jack_engine.py:1546-1552` sets running false, sleeps 100 ms, and stops JACK without joining render/MIDI/GC workers or stopping/flushing Recorder. `main.py:2261` onward subsequently shuts down native piano/UI; delayed workers may still hold native references. Separately, Recorder `_stop_locked` (`recorder.py:148-175`) joins for at most three seconds then clears shared writer/file references regardless of whether the thread finished; a new take reuses the same queue/event. Stalled disk/native work can outlive declared stop, lose queued audio, or interfere with a new writer. Use bounded but truthful shutdown states and per-take ownership; do not free resources still in use. **High confidence for missing lifecycle guarantees; timing failures not injected in this pass.**

### C11 — P1: component/device readiness is not truthful

`main.py:2181-2201` reports MIDI connection success without checking `jack_connect` return status. The HTTP/WS bind threads are not part of READY/watchdog success (UI report). Audio rendering to Dummy Output is not physical-output readiness. Existing previous-session device evidence showed USB enumeration errors and no physical USB MIDI/audio device; that establishes an OS/device-layer failure at that time, not which physical component caused it. Require separate engine, UI, MIDI-port, intended audio-route and hardware statuses. **High confidence in code behavior; historical cause remains unproven.**

### C12 — P2: preset/recording recall can acknowledge settings it never applies

The final preset snap loop (`main.py:573-599`) sends `master.piano_octave` through `_handle_setting`, which has no case for it. The loaded state/UI can show the target octave while `jack.piano_octave` remains old. An isolated exact-handler test reproduces the no-op success; other reviewer independently traces the complete preset path. Recording recall (`:769-800`) also uses this generic handler for fields that have separate APIs: master volume, transpose and instrument mode are not handled there. Define one validated complete scene-application API and report unsupported fields as failures. **High confidence; handler reproduction plus static end-to-end trace.**

### C13 — P2: multi-file preset operations can report success after partial failure

Setlist load (`main.py:421-442`) writes/deletes ten files, and preset swap (`:647-679`) changes two files while ignoring `PresetManager` failure booleans. Labels/state and acknowledgements still advance. ENOSPC/permissions/interruption can leave a mixed bank or lose a slot during swap. Preset crossfades independently update runtime parameters on a thread, can overwrite a performer's simultaneous gesture, and are not canceled by panic. Define transaction boundaries, failure propagation and control ownership. Whole-state preset/setlist growth is separately covered in the UI report. **High confidence in control flow; disk-failure injection not run.**

### C14 — P1: garbage collection's idle detector does not establish silence

`jack_engine.py:1115-1215` tests only synth voice count and active piano-note bookkeeping, not sampled beds, organ release, piano/reverb tails, freeze or effect energy. Thus even 90 seconds with no held keys does not prove silence. Full/gen0 Python collections hold the interpreter and can starve render work. Previous live inspection correlated a 32.4 ms collection with an underrun increment, but Dummy Output meant no audible dropout was demonstrated. Inspect cyclic allocation and real queue/scheduler reserve; no indefinite-GC-disable recommendation. **High confidence in detector gap; previous observation is correlation, not a controlled test.**

### C15 — P2: diagnostics are not entirely passive or correctly labeled

The `debug` handler (`main.py:218-239`) calls `self.piano.fs.get_samples(64)` and discards those live synth samples, outside the render lock: repeated diagnostic queries advance the piano stream independently. Do not poll it during performance or call it a read-only health check. `jack_engine.py:1531` describes a post-limiter/output meter, but the stored peak is measured pre-limiter at `:1021`; neither it nor rendered callbacks proves physical output. Starvation recovery uses fill >=4 in a low-latency mode whose producer refills to3, suppressing subsequent log episodes; render timing currently excludes substantial master/piano processing. **High confidence; static tracing.**

### C16 — P2: bridge master gain has no non-finite safety boundary

`jack_bridge.c:299-301` accepts any float. NaN poisons `master_volume_smooth` (`:145-147`) and survives a later valid gain command; comparisons do not sanitize NaN. The unchanged native source reproduced NaN output after restoring gain1. This is a native robustness failure; it does not by itself establish that the ordinary clamped fader sends NaN. Reject non-finite values upstream and enforce a final safe native boundary/recovery. **High confidence; reproduced at bridge API.**

### C17 — P2: keyboard expression and multi-channel policy are implicit

`jack_engine.py:231` fixes minimum velocity at10, so velocities1-9 are dropped (`:1299`), relevant to delicate worship piano. `:1294` strips the MIDI channel and note maps are raw-note keyed; identical notes across channels/controllers collide, and pedals are global. MIDI event timestamps are not retained by the C queue, while tempo estimation uses Python dispatch time (`:1264`), so queued clock bursts lose original timing. The velocity filter may have been an intentional ghost-note workaround, and channel merging may be intentional omni behavior: make both explicit supported policies and audition before changing feel. **High confidence in behavior; not all are unconditional defects.**

## Verification performed

- `core_probes.py`: eight baseline defect scenarios reproduced on this Mac using AST-extracted methods, NumPy and disposable temporary data. No application imports/network/native production modules.
- `native_probe.c`: four scenarios reproduced by including the unchanged bridge in a mock JACK translation unit. Compiled with local clang GNU11; one warning about a label before a declaration (C23 extension), no compile/run failure. Mock tests do not establish ARM memory ordering, real-time performance or actual JACK compatibility.
- Complete source reading and cross-review reconciliation; no performance assertions derived from ring depth.
- These assertions expect the existing bug. They must be inverted/replaced as real regressions when fixes are implemented; their successful exit means reproduction succeeded, not that Stave passed qualification.

## Remaining core tests

Validated schema/restart recovery; concurrent/failing saves; full panic source/pedal/queue matrix; sostenuto+sustain/retrigger/split/channel cases; recorded-take lifecycle and memory bounds; stalled producer/callback ring transitions; variable/unsupported graph contract; exact native shutdown ownership; separate listener/MIDI/sink readiness; slow clients; real stage-input-to-analogue latency; representative musical load, tail/bed GC, long soak, reboot and hotplug matrix. These require isolated fixtures or a separately authorized target-hardware qualification session.
