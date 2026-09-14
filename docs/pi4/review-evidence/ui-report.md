# Stave Synth complete UI / WebSocket / preset audit

Audit target: `/Users/codyvanscyoc/Documents/stave-synth-pi4-stage`, commit `4c64f3305a611462c01faa3ad4233f40042f3dcc`.

Scope was read-only. No Pi was contacted and no production service or data was exercised.

## Exact owned-file coverage

Each owned source was read in full, in line-numbered chunks:

- `ui/index.html` (1-1714), SHA-256 `fb1bfccc7bb637956bc7031fc98a30c64c5abc0b111bec4f981634a3042515a0`
- `ui/script.js` (1-4693), SHA-256 `239699705910d32a3c2376334f989d7f5c104d4366f436304bb08dfc3a3f5900`
- `ui/style.css` (1-3370), SHA-256 `3edea222e66c2e32b12712abbb547e274bb137adc0e0bcd2a04b258066eeeff2`
- `ui/manifest.json` (1-13), SHA-256 `c9c95cb94bf98f54deb69f4cb7d92a791f70c2a6d5e3a3056157b5a32487927e`
- `ui/logo.png`, SHA-256 `2f6bf7fcf9188a5f2bb1baf5b771f0b3c0f4e0c706debed64c1348e684a9561f`; inspected at original resolution (510x510 RGBA). Artwork is coherent and readable.
- `stave_synth/websocket_server.py` (1-183), SHA-256 `1cc25b73a580c0a64ea7a15857bacaeab3b254e8dcabbc18fd98ba9b454fb120`
- `stave_synth/preset_manager.py` (1-102), SHA-256 `341d896feb6c41dd9ed78c6363528081c182f0543aecbc32b0eb6d9ed31c5495`

I also read `AGENTS.md`, the Pi 4 product/baseline/validation documents, and the relevant message handlers and lifecycle paths in `stave_synth/main.py` to trace the owned code's contracts.

## Prioritized findings

No P0 issue (immediate data loss or guaranteed total audio failure) was found in this owned surface.

### P1 — A hardware MIDI macro is browser-dependent and fan-outs duplicate engine commands

**Refs:** `stave_synth/main.py:1693-1709`; `ui/script.js:578-581`; `ui/script.js:1422-1437`; `ui/script.js:1454-1494`.

**Trigger:** Move a controller CC mapped to a macro with zero, one, or multiple browser clients connected.

**Mechanism/consequence:** The backend broadcasts `macro_cc_value` and returns without applying the macro to engine state. With zero browsers, the live control does nothing. Every connected browser receives the broadcast and independently sends `macro_value` plus one fader/setting message per assignment, so two browsers apply every target twice and increase handler/broadcast load. This conflicts directly with headless stage operation.

**Confidence:** High; explicit comments and control flow on both ends confirm it.

### P1 — The reconnect implementation permits stale WebSocket callbacks to corrupt the current connection

**Refs:** `ui/script.js:103-151`; `ui/script.js:154-185`; global socket at `ui/script.js:13-16`.

**Trigger:** Manual reconnect while an older socket is connecting/open, or an old socket emits delayed `open`, `error`, or `close` after a replacement has been assigned to global `ws`.

**Mechanism/consequence:** Every callback closes/sends through global `ws`, not the socket instance that owns the callback. `reconnectNow()` creates a replacement without closing or invalidating the old socket. An old `onerror` can therefore close the new socket; an old `onopen` can mark the UI connected and send hydration requests through the new socket; an old `onclose` can schedule another connection. This can yield alternating sockets, misleading `CONN`, duplicate hydration, and an apparent connection loop while backend audio remains healthy.

**Confidence:** High for the race; medium that it explains the user's historical incident because no contemporaneous logs were available.

### P1 — HTTP/WS bind failure is detached from service readiness

**Refs:** `stave_synth/websocket_server.py:116-124`, `:126-170`; `stave_synth/main.py:1958-1960`, `:2020-2030`.

**Trigger:** Port 8080 or 8765 is already occupied, address binding fails, or either daemon thread exits during startup.

**Mechanism/consequence:** Both listeners start in daemon threads; exceptions remain in those threads and `start()` returns immediately. Main later sends unconditional `READY=1`, while its watchdog observes the audio callback only. Audio can therefore keep running and systemd can report healthy even though the browser UI is unreachable. `stop()` also closes only WS, not the HTTP server (`websocket_server.py:172-183`).

**Confidence:** High for failure behavior; medium as attribution to the historical no-connect event.

### P1 — The unauthenticated control plane is exposed on every network interface

**Refs:** `stave_synth/config.py:48-51`; `stave_synth/websocket_server.py:49-88`, `:118-122`, `:133-155`; dispatch surface `stave_synth/main.py:129-186`.

**Trigger:** Any device able to reach the Pi's stage/Wi-Fi network opens port 8765 or 8080. Browser Origin is not checked and there is no token/session authorization.

**Mechanism/consequence:** The peer can read state and invoke performance, preset, recorder deletion, pad, audio-output, and settings operations. Recordings are also downloadable over unauthenticated HTTP. This is especially risky on venue/shared Wi-Fi. Binding to all interfaces may be an intentional appliance choice, but unauthenticated mutation is not a pro-stage boundary.

**Confidence:** High.

### P1 — Response-name-based broadcasting leaves multiple screens with different control truth

**Refs:** broadcast rule `stave_synth/websocket_server.py:79-88`; client handlers `ui/script.js:397-415`, `:562-577`; instrument mutation `stave_synth/main.py:1063-1070`; preset save/delete `stave_synth/main.py:607-644`; macro assignment `stave_synth/main.py:967-1020`; pad mutation `stave_synth/main.py:830-872`.

**Trigger:** Use phone and iPad together, then change instrument mode, save/delete/load a preset, assign a macro, or save/clear a pad slot on one screen.

**Mechanism/consequence:** The server broadcasts only responses ending `_ack`. Several state-changing responses (`instrument_mode`, `preset_saved`, `preset_deleted`, `preset_loaded`, `pad_slot_saved`, `pad_slot_cleared`) do not match. `macro_assign_ack` is explicitly excluded under the comment that state is separately broadcast, but this handler does not broadcast state. Secondary screens can show the wrong instrument/fader-1 meaning, stale slot occupancy, or stale macro assignments. A preset load does broadcast general state, but no canonical loaded-slot field exists, so the active highlight remains local (`ui/script.js:1947-1981`).

**Confidence:** High.

### P1 — Presets serialize global libraries and can recursively amplify setlist data

**Refs:** whole-state save `stave_synth/main.py:607-613`; unfiltered serialization `stave_synth/preset_manager.py:34-50`; setlist embeds the contents of ten preset files into state `stave_synth/main.py:399-419`; preset load replaces runtime state with defaults plus the saved whole state `stave_synth/main.py:453-465`.

**Trigger:** Save presets after setlists exist, then save further setlists/presets or load an older sound preset.

**Mechanism/consequence:** A sound preset contains `setlists`, and each setlist contains copies of presets that may themselves contain earlier `setlists`. Repeated cycles can produce rapidly growing JSON, slower serialized WS work, more SD writes, and larger recorder state sidecars. Loading an old sound preset also rolls global UI/macro/setlist state backward rather than changing only the sound/scene. That violates the likely separation between performance sound and the user's global library.

**Confidence:** High for inclusion/replacement; medium for real-world growth magnitude because no production data was inspected.

### P2 — Recording status is not reconnect-hydrated

**Refs:** client-only flag and timer `ui/script.js:2382-2415`; only `record_ack` updates it `ui/script.js:554-557`; backend `get_state` omits recorder status `stave_synth/main.py:187-217`; toggle behavior `stave_synth/main.py:743-756`.

**Trigger:** Reload/reconnect a browser while recording is active, or begin recording on another screen while this screen is disconnected.

**Mechanism/consequence:** The reconnected UI displays idle even while recording. Its next tap stops the take, contrary to the apparent action; elapsed time is also reset/client-estimated. The control is not truthfully hydrated.

**Confidence:** High.

### P2 — Slow clients can accumulate broadcast work on the single WS event loop

**Refs:** serial per-client awaits `stave_synth/websocket_server.py:95-103`; every producer call schedules and discards a future `:105-114`; high-rate audio heartbeat handling on client `ui/script.js:504-509`.

**Trigger:** A connected browser becomes slow/suspended or its Wi-Fi path stops draining while engine telemetry continues.

**Mechanism/consequence:** Broadcasts await clients sequentially and `broadcast_sync()` has no coalescing, queue bound, timeout, or retained future/error handling. Telemetry can accumulate event-loop tasks behind a slow send, delay connection/message handling, and make healthy clients appear unable to connect while audio continues. Exact pressure depends on the installed `websockets` version and transport limits.

**Confidence:** Medium-high.

### P2 — An OPEN-but-stale WS never attempts recovery

**Refs:** heartbeat receipt `ui/script.js:504-509`; liveness watchdog `ui/script.js:4239-4251`; reconnect only on socket close/error `ui/script.js:145-176`.

**Trigger:** TCP/WebSocket stays `OPEN`, but backend messages stop because of a half-open link or event-loop/backpressure stall.

**Mechanism/consequence:** Audio status eventually turns amber/red, but the client never probes, closes, or reconnects. The user must notice and tap the separate `CONN` indicator manually. This is a truthful warning but not self-healing.

**Confidence:** High for client behavior.

### P2 — Phone portrait is not an implemented layout

**Refs:** `ui/manifest.json:4-6`; global fixed viewport `ui/style.css:41-50`; the only media query is input-capability based `ui/style.css:18-28`; fixed five-column main surface `ui/style.css:635-680`.

**Trigger:** Install/open as a PWA on a phone in portrait or use a narrow browser viewport.

**Mechanism/consequence:** The manifest requests landscape, `html/body` cannot scroll, and there is no width/orientation breakpoint. Many controls use viewport-height targets, while only selected compact buttons get a 36px target plus pseudo hit area. The five-fader iPad landscape workflow is intentionally preserved and coherent; full phone portrait editing is currently a candidate, not an existing capability.

**Confidence:** High from static CSS/manifest; actual OS orientation enforcement varies.

### P2 — Pad-slot empty behavior is contradicted in the same UI contract

**Refs:** Record help says missing slots are silent `ui/index.html:1409-1411`; clear tooltip says the key reverts to live synth `ui/script.js:2542-2549`; backend comments also promise live fallback `stave_synth/main.py:858-860`, `:1962-1965`.

**Trigger:** Clear a pad slot and trigger that note.

**Mechanism/consequence:** A performer cannot know whether silence or a live synth voice is expected. This is safety-relevant wording during a service. The actual sample-player signal behavior must decide the intended contract; this report does not infer it from contradictory comments.

**Confidence:** High for contradiction; actual audible outcome is covered by the sampler/audio audit.

### P2 — Loaded preset indication is not a truthful scene-dirty indicator

**Refs:** local loaded slot `ui/script.js:65`, `:1947-1981`; arbitrary setting sends update only local state `ui/script.js:262-281`.

**Trigger:** Load or save a preset, then move a fader/setting/macro, or reconnect/open a second screen.

**Mechanism/consequence:** The preset remains highlighted as “loaded” after the live sound has diverged, and a new/reconnected client cannot hydrate which preset was loaded. Treat the highlight as “last selected on this screen,” not “current sound equals scene.” For professional set workflow, the label needs an explicit meaning or dirty state.

**Confidence:** High.

### P3 — Valid JSON that is not an object can disconnect its sender instead of returning an error

**Refs:** `stave_synth/websocket_server.py:55-78`.

**Trigger:** Send `[]`, `null`, a string, or a number as a WS frame.

**Mechanism/consequence:** JSON parsing succeeds; the handler fails on `.get`; the exception-reporting path itself calls `msg.get`, so it throws and unwinds the connection. This is input-hardening debt, although it affects the offending client rather than audio.

**Confidence:** High.

### P3 — Preset durability and validation stop short of an appliance-grade contract

**Refs:** `stave_synth/preset_manager.py:31-79`.

**Trigger:** Corrupt but valid JSON, wrong top-level JSON type, power loss immediately after rename, or concurrent same-slot writers outside the currently serialized WS handler.

**Mechanism/consequence:** Load has no schema/top-level-type validation; save fsyncs the file but not the containing directory; each slot uses one fixed `.tmp` name. Current single-worker WS dispatch makes same-slot races unlikely in normal browser use, so the fixed temp path is a latent constraint rather than a confirmed current race.

**Confidence:** High for implementation, medium-low for likelihood.

### P3 — PWA icon metadata does not match the image

**Refs:** `ui/manifest.json:9-11`; `ui/logo.png` is 510x510.

**Trigger/consequence:** The same 510px asset is declared as both exact 192x192 and 512x512. Installers may reject a declared size, resample unexpectedly, or choose no optimal icon. Visual artwork itself is fine.

**Confidence:** High.

### P3 — Low-latency help makes fixed numerical promises the UI cannot verify

**Refs:** `ui/index.html:1677-1682`; mode default/application `stave_synth/main.py:1945-1948`.

**Trigger/consequence:** It promises “~27 ms faster” and “~20 ms” mute without deriving either from active JACK/PipeWire periods or sample rate. On a different runtime quantum those numbers are false. Keep the control, but treat the numbers as configuration-specific, not a current status readout.

**Confidence:** High.

## Confirmed existing workflow / design choices (not defects)

- The five familiar performance faders, ALT cycles, source toggles, split/LAYER panel, two banks of five presets, and secondary settings tabs are deliberately retained. Nothing in this review recommends changing the sound or that learned surface.
- Record-to-pad is implemented as: top-bar record toggle (`ui/index.html:39-45`, `ui/script.js:2412-2415`), take list/playback and parameter recall (`ui/script.js:2425-2501`), choose one destination C..B (`ui/script.js:2418-2423`, `:2455-2474`), then copy that WAV byte-for-byte to that one slot (`stave_synth/main.py:830-856`). There is no source-root field, pitch shifting, transposition, or “fill all keys” operation. Copying the same take to multiple keys creates same-pitch copies; this matches the current resampling-to-pad design and is not silently treated as a defect.
- Offline queuing intentionally accepts only absolute fader/setting/macro values and drops one-shot actions (`ui/script.js:187-239`). That avoids delayed panic/preset/pad actions and is a sound stage-safety choice.
- Fader/settings state hydration is substantial, and incoming dispatch is exception-guarded (`ui/script.js:128-142`, `:702-874`). The problem is the few important ephemeral/cross-client states outside that canonical payload, not absence of hydration overall.

## Checks run

Follow-up cross-review: the full eight-scenario `core_probes.py` was independently run successfully. Piano-octave recall is confirmed: set piano octave +1, save preset A, change octave to0, then load A; state/UI return to+1 but JACK retains0 because the final preset snap uses the no-op `master.piano_octave` setting handler. Recording recall uses the same handler and restores neither its state field nor JACK octave. See `main.py:453-465`, `:573-600`, `:769-795`, `:1135-1148`, `:1406-1585`.

Growth qualification: ordinary repeated saves of an unchanged preset do **not** cause growth. The structurally unbounded feedback requires saving a bank into a setlist, resaving presets that now contain that parent setlist state, then embedding those parent-bearing presets into another setlist snapshot. No growth benchmark was completed; no measured factor or inevitable growth rate is claimed.

- `node --check ui/script.js` — passed.
- Python syntax compilation of `stave_synth/websocket_server.py` and `stave_synth/preset_manager.py` — passed.
- Verified commit, owned-file hashes, dimensions/type of `logo.png`, sole CSS media query, and a clean Git worktree.
- Static message tracing from every UI send/receive family into `main.py`; no live sockets were opened.

## Outstanding test gaps

The repository has only four system-oriented tests (`test_1_atomic_save.py` through `test_4_boundary_sweep.py`); none target the browser state machine or these owned classes directly. The following should become deterministic offline/unit or isolated-device acceptance tests before a pro-stage claim:

1. Fake-WebSocket race matrix: old/new socket open/error/close orderings, repeated manual reconnect, timer cancellation, exactly one authoritative socket.
2. Half-open test: transport remains OPEN but telemetry stops; recovery and honest status.
3. Zero/one/two-client MIDI macro test: identical engine result and bounded command count in every case.
4. Slow-client telemetry test: suspend one sender, verify bounded/coalesced queue and prompt connection/control for another client.
5. Multi-client mutation table covering instrument, preset save/load/delete/swap/label, macro assign/value, recorder, pad slot, setlist, fade/panic, and audio output.
6. Reload while recording: exact recording flag/start time; stop action semantics.
7. Preset/setlist scope and growth test over repeated save/setlist cycles, malformed/old-schema inputs, power-cut durability, and explicit failure propagation.
8. Browser acceptance at representative iPad landscape and phone portrait sizes, including 44px critical targets, scroll reachability, keyboard popup, LAYER drags, and installed-PWA orientation.
9. Security boundary tests for Origin/authentication, LAN peer mutation, recording download/delete, traversal normalization, frame size/rate limits, and malformed non-object JSON.
10. Recorder-to-pad contract test: one-slot copy, all-bank reload behavior, missing-slot audible behavior, and on-screen wording consistency.
