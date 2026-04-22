# Mac-port handoff

This document is the handoff prompt for the Mac-side Claude Code session
that picks up the `mac-port` branch on macOS and finishes the port.
Written from the Pi-side session that set up the refactor scaffold.

## How to use this document

Paste the contents (or link) into your Mac-side Claude Code session as
initial context. Work through the checklist top-to-bottom. Each checklist
item is independently commit-able and shippable.

## Branch state at handoff

Branch `mac-port` on origin is 7 commits ahead of `main`:

| Commit | Scope |
|---|---|
| `e91fa80` | `stave_synth/audio_io/` subpackage: `AudioIO` ABC + `LinuxJackIO` wrapping jack_bridge.so + `MacPortAudioIO` stub |
| `12186a0` | JackEngine cutover — all `self._bridge.bridge_X()` → `self._audio.X()` |
| `bc06c80` | Platform-gate `SCHED_FIFO` / `malloc_trim` / `/proc/self/status` behind `audio_io/platform.py` |
| `6081ef7` | `.dylib` suffix swap in Faust loaders + `main.py` DISPLAY gate treats Mac/Windows as always-GUI |
| `bfd1ffc` | `stave_synth/paths.py` — XDG on Linux, `~/Library/Application Support/` on macOS |
| `cb0b083` | `install-mac.sh` + `Brewfile` + `requirements-mac.txt` + `stave-synth-mac.sh` + `faust/build.sh` Darwin branch |
| *(this)* | This handoff document |

`main` is unchanged at `b2ed8eb` (Review #7 Tier 1/2/3 batch). The live
Linux synth keeps running from `main` while you work on `mac-port`.

## Architecture summary

**Linux path (unchanged, working):**
```
synth_engine → jack_engine → LinuxJackIO → jack_bridge.so → JACK/pw-jack → hardware
```

**Mac path (to be built):**
```
synth_engine → jack_engine → MacPortAudioIO → sounddevice.Stream → Core Audio → hardware
                                            ↘ python-rtmidi → Core MIDI → keyboard
```

The split is clean: everything above `audio_io/` is platform-agnostic
Python code (DSP, voice allocation, UI, Faust bindings). Everything in
`audio_io/` is the per-platform transport.

## What's already done (Pi-side verified)

- ✅ `stave_synth/audio_io/base.py` — `AudioIO` ABC with all 17 methods JackEngine needs.
- ✅ `stave_synth/audio_io/linux_jack.py` — full Linux impl, battle-tested via runtime verification on Pi.
- ✅ `stave_synth/audio_io/mac_portaudio.py` — **stub** that raises `NotImplementedError` for every method. This is what you fill in.
- ✅ `stave_synth/audio_io/platform.py` — `set_realtime_priority`, `try_malloc_trim`, `get_rss_kb`, `LIB_SUFFIX` — all platform-gated.
- ✅ `stave_synth/paths.py` — `config_dir()`, `data_dir()`, `soundfont_search_dirs()` for both platforms.
- ✅ All 10 Faust loaders use `f"libstave_X{LIB_SUFFIX}"` — will load `.dylib` on Mac.
- ✅ `faust/build.sh` detects Darwin and emits `.dylib` via `clang -dynamiclib`.
- ✅ `install-mac.sh`, `Brewfile`, `stave-synth-mac.sh`, `requirements-mac.txt` are drafted but unverified.

## What's left (Mac-side checklist)

### 1. Bootstrap a Mac environment

- [ ] Homebrew installed (`/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`)
- [ ] `git clone` the repo to a sensible path (e.g. `~/stave-synth`)
- [ ] `git checkout mac-port`
- [ ] `./install-mac.sh` — expect breakage on first run; debug and fix.

Known unknowns at install-mac.sh step:
- Homebrew prefix differs between Apple Silicon (`/opt/homebrew`) and Intel (`/usr/local`). Paths in `soundfont_search_dirs()` cover both but only one exists on any given Mac.
- FluidR3_GM may ship under a different relative path inside the Homebrew `fluid-synth` bottle than the candidates listed. Check `brew ls fluidsynth | grep -i .sf2` to locate it.
- pywebview on Mac needs `pyobjc-framework-WebKit`. `requirements-gui.txt` may need a Mac-specific marker or a separate `requirements-mac-gui.txt`.

### 2. Build the Faust .dylib files

- [ ] `cd faust && ./build.sh` — should produce 11 `.dylib` files.
- [ ] If compilation fails on any module, note which one and why. `faust` itself is cross-platform; errors are most likely from the `clang` vs `gcc` switch (e.g. missing `-Wno-...` flags Linux is lenient about).

### 3. Implement `MacPortAudioIO`

This is the biggest chunk. File: `stave_synth/audio_io/mac_portaudio.py`.

**Architecture — mirror the Linux ring-buffer model:**
- `sounddevice.OutputStream(callback=...)` drives audio from Core Audio's RT thread.
- JackEngine's render thread (already running) calls `write_stereo(l, r, n)` to push blocks.
- Between them: a thread-safe 24-slot ring buffer in Python.
  - Producer (render thread, via `write_stereo`): blocks if ring is full.
  - Consumer (sounddevice callback, RT thread): underruns to silence if ring is empty (log underrun count).

**Why not drive render from the sounddevice callback?** That would call into Python code from the RT thread → GIL contention → dropouts. The Pi architecture keeps Python render off the RT path; Mac should too.

**Method map:**

| AudioIO method | Mac implementation |
|---|---|
| `start()` | `sd.OutputStream(samplerate=48000, channels=2, callback=self._cb).start()` + open MIDI port |
| `stop()` | `stream.stop(); stream.close()` + close MIDI |
| `get_sample_rate()` | `self._stream.samplerate` |
| `get_buffer_size()` | `self._stream.blocksize` (or whatever you pass at open) |
| `write_stereo(l, r, n)` | Interleave to (n,2) float32, push to ring |
| `read_midi(buf)` | Drain `self._midi_queue`, write [status, d1, d2, 0] into `buf` |
| `get_midi_event_count()` | `self._midi_queue.qsize()` |
| `set_master_volume(amp)` | Store; callback multiplies output by this |
| `set_btl_mode(enabled)` | **No-op** — Mac has no BTL hardware hack. Log warning if called with 1. |
| `get_btl_mode()` | Always return 0 |
| `is_shutdown()` | True if stream is closed / errored (sounddevice doesn't have a "server died" concept — the OS doesn't go away) |
| `get_ring_fill()` | `len(ring_buffer)` |
| `get_peak_output()` | Track max `abs()` per callback, return + reset |
| `get_xrun_count()` | Count callback misses via `status.output_underflow` |
| `get_underrun_count()` | Same as xrun on Mac (PortAudio lumps them) |
| `get_callback_count()` | Increment per callback |

**Dependencies already listed in `requirements-mac.txt`:**
- `sounddevice>=0.4.6`
- `python-rtmidi>=1.5.0`

**MIDI integration sketch:**
```python
import rtmidi
midi_in = rtmidi.MidiIn()
ports = midi_in.get_ports()
# Auto-connect to first port matching user's keyboard; main.py can expose a selector later.
midi_in.open_port(0)
midi_in.set_callback(lambda msg, _: self._midi_queue.put(msg[0]))
```

### 4. End-to-end smoke test

- [ ] `./stave-synth-mac.sh` from repo root
- [ ] Open `http://localhost:8080` in a browser (or wait for pywebview window)
- [ ] Plug in a USB MIDI keyboard, press a key — expect audio
- [ ] Compare sound to Pi: reverb, piano, organ, effects should all be audible
- [ ] Watch for xruns in logs; tune `sounddevice.default.latency` if needed

### 5. Optional stretch — `.app` bundle via py2app

- [ ] `pip install py2app`
- [ ] Create `setup-mac.py` with a `py2app` target pointing at `stave_synth/main.py`
- [ ] Bundle the Faust `.dylib` files + `soundfonts/` directory into the `.app`
- [ ] Test double-click launch
- [ ] Code-sign (optional) for distribution

This deserves its own session — 2-4 hours to get a working `.app`.

## Key design decisions (rationale)

### Why not drive audio from the sounddevice callback?
The LinuxJackIO architecture keeps Python off the realtime path: C owns the
JACK callback, Python renders ahead into a ring buffer. On Mac we could
technically have sounddevice call a Python callback (the GIL-held kind),
but that would regress the careful SCHED_FIFO + GC-disabled render thread
work that's been tuned on Pi. Keep the same producer/consumer split.

### Why keep BTL_MODE in the AudioIO interface?
It's a hardware hack specific to Bridge-Tied-Load USB audio adapters
(which only exist on a handful of Linux-targeted dongles). No Mac audio
interface needs it. `MacPortAudioIO.set_btl_mode` is a documented no-op;
removing it from the interface would mean a base-class ABC change and
rippling edits. Not worth the churn.

### Why Python ring buffer instead of lock-free queue?
The ring buffer in jack_bridge.so is lock-free because the C callback
can't wait. On Mac, sounddevice's callback runs in a Core Audio thread
that's likewise RT, BUT — we have the GIL regardless, so a simple
`collections.deque` with a `threading.Lock` is already as good as it gets
from Python. If profiling shows lock contention, revisit.

### Why keep the Faust .dylib build separate from install-mac.sh?
Same split as Linux — `install.sh` calls `faust/build.sh` internally
rather than duplicating. Makes incremental rebuilds (`./build.sh -f`)
possible without re-running the full installer.

## Gotchas known in advance

- **Mach real-time scheduling**: `audio_io/platform.py::set_realtime_priority` is a
  no-op on Mac. Core Audio already gives its callback thread a realtime
  policy via `thread_policy_set(THREAD_TIME_CONSTRAINT_POLICY)`. Our
  Python render thread doesn't need it because it's not on the RT path.
  Don't add RT scheduling to the Python thread — the GIL makes it
  counterproductive.

- **sample rate mismatch**: Mac's default output device might be running at
  44100 or 96000, not 48000. The synth engine assumes 48000 (see
  `config.SAMPLE_RATE`). Options:
  - Force sounddevice to 48000 and let Core Audio resample.
  - Query the device rate and pass it through to the engine.
  Easiest is the former; watch for xruns on high-SR devices.

- **First-launch MIDI permissions**: macOS may prompt to authorize MIDI access
  the first time. Test this with a real keyboard plugged in before declaring done.

- **Bluetooth MIDI**: nice-to-have, not in scope for v1. Skip unless trivial.

- **MIDI reconnection**: Linux uses `a2jmidid` to auto-bridge. Mac doesn't need
  this (Core MIDI sees devices natively) but rtmidi's port-index can shift
  when devices are hot-plugged. Poll `get_ports()` periodically or listen for
  `MIDIClientDispatch` if serious.

- **pywebview on Mac**: uses WKWebView; usually works, but confirm fullscreen
  mode doesn't fight with macOS's fullscreen app concept. Our `create_window`
  passes `fullscreen=True` — may want to swap to windowed if Mac UX suffers.

- **faust .dylib code signing**: if you ever distribute a `.app`, every
  `.dylib` inside needs to be signed with the same identity as the bundle.
  Unsigned dylibs are blocked by Gatekeeper on first run.

## Files you'll likely touch

**Must edit:**
- `stave_synth/audio_io/mac_portaudio.py` — the real implementation

**Might need tweaks once you run it:**
- `install-mac.sh` — Homebrew path discovery, FluidR3_GM location
- `stave-synth-mac.sh` — env vars, launch flags
- `requirements-mac.txt` — sounddevice/rtmidi versions if current ones have Mac-specific bugs

**Shouldn't need changes (cross-platform from Pi session):**
- `stave_synth/audio_io/base.py` — interface is stable
- `stave_synth/audio_io/linux_jack.py` — Linux only, ignore
- `stave_synth/audio_io/platform.py` — platform branches already cover Mac
- `stave_synth/paths.py` — Mac paths already configured
- `stave_synth/jack_engine.py` — uses `self._audio`; agnostic
- All Faust loaders — use `LIB_SUFFIX`; agnostic

## Verification checklist (final)

When you think you're done:

- [ ] `./stave-synth-mac.sh` runs for 60 seconds without errors in logs.
- [ ] MIDI keyboard plays audio end-to-end.
- [ ] `http://localhost:8080` loads, UI is responsive, knobs affect sound.
- [ ] pywebview window opens on its own (or `--no-gui` skips it cleanly).
- [ ] All 7 piano voicings work (Salamander, Fluid, Rhodes, Suitcase each cycle cleanly).
- [ ] Reverb types switch without clicks.
- [ ] No xrun warnings during normal play.
- [ ] `pkill -f stave_synth.main` cleanly shuts down (no orphan threads).
- [ ] Re-launch works without port-conflict errors.
- [ ] Config / recordings / presets land under `~/Library/Application Support/stave-synth/`.

Good luck. The Pi side is solid; the bones are good; the interface is
already doing its job of keeping platform specifics contained. This is a
port, not a rewrite.
