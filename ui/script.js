/* Stave Synth — UI Logic & WebSocket Client */

(function () {
    "use strict";

    const WS_URL = "ws://" + window.location.hostname + ":8765";
    const RECONNECT_DELAY = 2000;

    // ═══ State ═══
    let ws = null;
    let state = null;
    let altModes = [false, false, false, false, false];
    let fader1AltState = 0;  // 0=Volume, 1=Tone, 2=Compressor (cycles on alt click)
    let faderValues = [0.6, 0.5, 1.0, 0.65, 0.85]; // osc1, piano, filter, fx(reverb), master
    let altFaderValues = [0.4, 1.0, 0, 0.5, 0];  // osc2, piano tone, -, fx(shimmer), -
    let fader1CompValue = 0.5;  // compressor amount (maps to threshold)
    let transposeValue = 0;
    let shimmerEnabled = false;
    let shimmerHigh = false;
    let fadedOut = false;  // master FADE button state
    let freezeEnabled = false;
    let droneEnabled = false;
    let instrumentMode = "piano"; // "piano", "organ", "off"
    let osc1Octave = 0;  // -3 to +3
    let osc2Octave = 0;  // -3 to +3
    let pianoOctave = 0; // -3 to +3

    // Mute state for OSC1/OSC2/Piano toggles
    let osc1Enabled = true;
    let osc2Enabled = true;
    let pianoEnabled = true;
    let osc1PreMute = 0.6;  // saved fader value before mute
    let osc2PreMute = 0.4;

    const FADER_LABELS_PIANO = ["PIANO", "TONE", "COMP"];
    const FADER_LABELS_ORGAN = ["ORGAN", "TONE", "LESLIE"];
    const FADER_LABELS = [
        ["OSC 1", "OSC 2"],
        FADER_LABELS_PIANO,  // updated dynamically
        ["FILTER", "LOW CUT"],
        ["FX", "SHIMMER"],
        ["MASTER"],
    ];

    // ═══ DOM References ═══
    const faderColumns = document.querySelectorAll(".fader-column");
    const faderTracks = document.querySelectorAll(".fader-track");
    const faderFills = document.querySelectorAll(".fader-fill");
    const faderValueEls = document.querySelectorAll(".fader-value");
    const faderLabels = document.querySelectorAll(".fader-label");
    const altBtns = document.querySelectorAll(".alt-btn");
    const presetBtns = document.querySelectorAll(".preset-btn");
    let loadedPreset = -1; // which slot is currently loaded (-1 = none)
    const transposeDown = document.getElementById("transpose-down");
    const transposeUp = document.getElementById("transpose-up");
    const transposeDisplay = document.getElementById("transpose-display");
    const shimmerBtn = document.getElementById("shimmer-btn");
    const shimOctBtn = document.getElementById("shim-oct-btn");
    const fadeBtn = document.getElementById("fade-btn");
    const freezeBtn = document.getElementById("freeze-btn");
    const droneBtn = document.getElementById("drone-btn");
    const menuBtn = document.getElementById("menu-btn");
    const settingsModal = document.getElementById("settings-modal");
    const settingsClose = document.getElementById("settings-close");
    const settingsTabs = document.querySelectorAll(".settings-tab");
    const settingsPanels = document.querySelectorAll(".settings-panel");
    const statusIndicator = document.getElementById("status-indicator");
    const midiIndicator = document.getElementById("midi-indicator");
    const osc1Btn = document.getElementById("osc1-btn");
    const osc2Btn = document.getElementById("osc2-btn");
    const pianoBtn = document.getElementById("piano-btn");
    let midiFlashTimeout = null;
    let clipTimeout = null;
    let linkOscLevels = false;
    const clipIndicator = document.getElementById("clip-indicator");
    const levelDots = document.querySelectorAll(".level-dot");
    let midiLearnActive = false;
    let midiLearnWaiting = false;  // true after fader selected, waiting for CC
    let ccMap = {};  // { "cc_number": { id, alt } }

    // ═══ WebSocket ═══
    function connectWS() {
        ws = new WebSocket(WS_URL);

        ws.onopen = function () {
            statusIndicator.textContent = "CONN";
            statusIndicator.className = "connected";
            ws.send(JSON.stringify({ type: "get_state" }));
        };

        ws.onmessage = function (event) {
            let msg;
            try {
                msg = JSON.parse(event.data);
            } catch (e) {
                return;
            }
            handleServerMessage(msg);
        };

        ws.onclose = function () {
            statusIndicator.textContent = "DISC";
            statusIndicator.className = "disconnected";
            setTimeout(connectWS, RECONNECT_DELAY);
        };

        ws.onerror = function () {
            ws.close();
        };
    }

    function send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(msg));
        }
    }

    function handleServerMessage(msg) {
        if (msg.type === "state" && msg.state) {
            applyState(msg.state);
        } else if (msg.type === "transpose_ack") {
            transposeValue = msg.semitones;
            updateTransposeDisplay();
        } else if (msg.type === "shimmer_ack") {
            shimmerEnabled = msg.enabled;
            updateShimmerDisplay();
        } else if (msg.type === "shimmer_high_ack") {
            shimmerHigh = msg.enabled;
            updateShimmerDisplay();
        } else if (msg.type === "freeze_ack") {
            freezeEnabled = msg.enabled;
            updateFreezeDisplay();
            updateShimmerDisplay();
        } else if (msg.type === "drone_ack") {
            droneEnabled = msg.enabled;
            updateDroneDisplay();
        } else if (msg.type === "fade_ack") {
            fadedOut = !!msg.faded_out;
            updateFadeDisplay();
        } else if (msg.type === "panic_ack") {
            if (msg.fade_reset) {
                fadedOut = false;
                updateFadeDisplay();
            }
        } else if (msg.type === "preset_saved") {
            markPresetSaved(msg.slot);
        } else if (msg.type === "preset_loaded") {
            markPresetLoaded(msg.slot);
        } else if (msg.type === "preset_deleted") {
            markPresetDeleted(msg.slot);
            setPresetLabel(msg.slot, "");
        } else if (msg.type === "preset_labeled") {
            setPresetLabel(msg.slot, msg.label);
        } else if (msg.type === "instrument_mode") {
            instrumentMode = msg.mode;
            updateInstrumentButton();
            fader1AltState = 0;
            altModes[1] = false;
            var altBtn1 = document.querySelector('.alt-btn[data-id="1"]');
            if (altBtn1) {
                altBtn1.classList.remove("active");
                faderColumns[1].classList.remove("alt-mode");
            }
            updateFader(1);
        } else if (msg.type === "midi_activity") {
            flashMidiIndicator();
        } else if (msg.type === "peak_level") {
            updateLevelDots(msg.peak);
            clipIndicator.classList.remove("limiting", "limiting-hard", "limiting-crush");
            if (msg.peak > 2.5) {
                clipIndicator.classList.add("limiting-crush");
                if (clipTimeout) clearTimeout(clipTimeout);
                clipTimeout = setTimeout(function () {
                    clipIndicator.classList.remove("limiting-crush");
                }, 500);
            } else if (msg.peak > 1.5) {
                clipIndicator.classList.add("limiting-hard");
                if (clipTimeout) clearTimeout(clipTimeout);
                clipTimeout = setTimeout(function () {
                    clipIndicator.classList.remove("limiting-hard");
                }, 500);
            } else if (msg.peak > 1.0) {
                clipIndicator.classList.add("limiting");
                if (clipTimeout) clearTimeout(clipTimeout);
                clipTimeout = setTimeout(function () {
                    clipIndicator.classList.remove("limiting");
                }, 400);
            }
        } else if (msg.type === "system_stats") {
            var cpuEl = document.getElementById("cpu-val");
            var ramEl = document.getElementById("ram-val");
            var cpu = msg.cpu_percent;
            var ram = msg.ram_mb;
            cpuEl.textContent = cpu.toFixed(1) + "%";
            ramEl.textContent = Math.round(ram) + "M";
            cpuEl.className = cpu > 80 ? "crit" : cpu > 60 ? "warn" : "";
            ramEl.className = ram > 500 ? "crit" : ram > 400 ? "warn" : "";
        } else if (msg.type === "midi_learn_active") {
            midiLearnActive = msg.active;
            midiLearnWaiting = false;
            updateMidiLearnUI();
        } else if (msg.type === "midi_learn_waiting") {
            midiLearnWaiting = true;
            updateMidiLearnUI();
        } else if (msg.type === "midi_learn_mapped") {
            ccMap = msg.map || {};
            midiLearnActive = false;
            midiLearnWaiting = false;
            updateMidiLearnUI();
            updateCCIndicators();
        } else if (msg.type === "audio_outputs") {
            showOutputMenu(msg.outputs);
        } else if (msg.type === "audio_output_set") {
            if (msg.success) {
                statusIndicator.textContent = shortOutputName(msg.name);
                document.getElementById("output-menu").classList.add("hidden");
            }
        } else if (msg.type === "cc_map") {
            ccMap = msg.map || {};
            updateCCIndicators();
        } else if (msg.type === "fader_ack" && msg.from_cc) {
            // CC-driven fader update — sync local state and visual
            var id = msg.id;
            var val = msg.value;
            var alt = msg.alt;
            if (id === 1) {
                if (alt === 0) faderValues[1] = val;
                else if (alt === 1) altFaderValues[1] = val;
                else fader1CompValue = val;
            } else if (alt) {
                altFaderValues[id] = val;
            } else {
                faderValues[id] = val;
            }
            updateFader(id);
        }
    }

    function applyState(s) {
        state = s;

        if (s.synth_pad) {
            // Fader shows position within trim range (blend / max)
            var osc1Max = s.synth_pad.osc1_max ?? 1.0;
            var osc2Max = s.synth_pad.osc2_max ?? 1.0;
            faderValues[0] = osc1Max > 0 ? (s.synth_pad.osc1_blend ?? 0.6) / osc1Max : 0;
            altFaderValues[0] = osc2Max > 0 ? (s.synth_pad.osc2_blend ?? 0.4) / osc2Max : 0;
            faderValues[0] = Math.min(1, faderValues[0]);
            altFaderValues[0] = Math.min(1, altFaderValues[0]);
            // Map filter cutoff back to 0-1 using configured range
            var fMin = s.synth_pad.filter_range_min ?? 150;
            var fMax = s.synth_pad.filter_range_max ?? 20000;
            var freq = s.synth_pad.filter_cutoff_hz ?? s.synth_pad.filter_highpass_hz ?? 8000;
            faderValues[2] = Math.log(freq / fMin) / Math.log(fMax / fMin);
            faderValues[2] = Math.max(0, Math.min(1, faderValues[2]));
            // FX fader: reverb mix (default) / shimmer vol (alt)
            faderValues[3] = s.synth_pad.reverb_dry_wet ?? 0.65;
            altFaderValues[3] = s.synth_pad.shimmer_mix ?? 0.5;
            shimmerEnabled = s.synth_pad.shimmer_enabled ?? false;
            shimmerHigh = s.synth_pad.shimmer_high ?? false;
            freezeEnabled = s.synth_pad.freeze_enabled ?? false;
            droneEnabled = s.synth_pad.drone_enabled ?? false;
            // Sync filter lowcut (ALT value)
            if (s.synth_pad.filter_highpass_hz) {
                altFaderValues[2] = Math.log(s.synth_pad.filter_highpass_hz / 20) / Math.log(2000 / 20);
                altFaderValues[2] = Math.max(0, Math.min(1, altFaderValues[2]));
            }
            // Sync resonance button
            resEnabled = s.synth_pad.sympathetic_enabled ?? false;
            if (resBtn) resBtn.classList.toggle("active", resEnabled);
            // Sync OSC level link state
            linkOscLevels = s.synth_pad.osc_levels_linked ?? false;
            updateFreezeDisplay();
            updateDroneDisplay();

            // Sync mute state with actual blend values
            osc1Enabled = faderValues[0] > 0;
            osc2Enabled = altFaderValues[0] > 0;
            if (osc1Enabled) osc1PreMute = faderValues[0];
            if (osc2Enabled) osc2PreMute = altFaderValues[0];
            updateOscButtons();
        }
        if (s.master) {
            // Sync saturation button
            satEnabled = s.master.saturation_enabled ?? false;
            if (satBtn) satBtn.classList.toggle("active", satEnabled);
        }
        if (s.piano) {
            faderValues[1] = s.piano.volume ?? 0.5;
            // Map piano highcut freq back to 0-1 using configured range
            var tMin = (s.piano.tone_range_min ?? 200);
            var tMax = (s.piano.tone_range_max ?? 20000);
            var hcFreq = s.piano.filter_highcut_hz ?? 20000;
            altFaderValues[1] = Math.log(hcFreq / tMin) / Math.log(tMax / tMin);
            altFaderValues[1] = Math.max(0, Math.min(1, altFaderValues[1]));
            pianoEnabled = s.piano.enabled !== false;
            pianoBtn.classList.toggle("active", pianoEnabled);
            pianoBtn.classList.toggle("off", !pianoEnabled);
        }
        if (s.master) {
            faderValues[4] = s.master.volume ?? 0.85;
            transposeValue = s.master.transpose_semitones ?? 0;
            instrumentMode = s.master.instrument_mode ?? "piano";
            updateInstrumentButton();
            // Flatten eq_bands into master section for slider sync
            var bands = s.master.eq_bands;
            if (bands) {
                if (bands[0]) { s.master.eq_low_freq = bands[0].freq_hz; s.master.eq_low_gain = bands[0].gain_db; }
                if (bands[1]) { s.master.eq_mid_freq = bands[1].freq_hz; s.master.eq_mid_gain = bands[1].gain_db; }
                if (bands[2]) { s.master.eq_high_freq = bands[2].freq_hz; s.master.eq_high_gain = bands[2].gain_db; }
            }
        }
        if (s.ui) {
            syncPresetSlots(s.ui.preset_saved);
            syncPresetLabels(s.ui.preset_labels);
        }

        updateAllFaders();
        updateTransposeDisplay();
        updateShimmerDisplay();
        updateSettingsSliders();
    }

    // ═══ Fader Logic ═══
    function getMultiAltValue(id) {
        if (id === 1) {
            if (fader1AltState === 0) return faderValues[1];
            if (fader1AltState === 1) return altFaderValues[1];
            return fader1CompValue;
        }
        return altModes[id] ? altFaderValues[id] : faderValues[id];
    }

    function updateFader(id) {
        var value = getMultiAltValue(id);
        const fill = faderFills[id];
        const valueEl = faderValueEls[id];
        const label = faderLabels[id];

        fill.style.height = (value * 100) + "%";

        if (id === 1) {
            var labels1 = instrumentMode === "organ" ? FADER_LABELS_ORGAN : FADER_LABELS_PIANO;
            label.textContent = labels1[fader1AltState];
        } else {
            label.textContent = FADER_LABELS[id][altModes[id] ? 1 : 0];
        }

        if (id === 2 || (id === 1 && fader1AltState === 1)) {
            // Frequency display: filter cutoff, lowcut, or piano tone
            var minF, maxF;
            if (id === 1) {
                minF = (state && state.piano && state.piano.tone_range_min) ?? 200;
                maxF = (state && state.piano && state.piano.tone_range_max) ?? 20000;
            } else if (altModes[2]) {
                minF = 20; maxF = 2000;
            } else {
                minF = (state && state.synth_pad && state.synth_pad.filter_range_min) ?? 150;
                maxF = (state && state.synth_pad && state.synth_pad.filter_range_max) ?? 20000;
            }
            var freq = minF * Math.pow(maxF / minF, value);
            if (freq >= 1000) {
                valueEl.textContent = (freq / 1000).toFixed(1) + "kHz";
            } else {
                valueEl.textContent = Math.round(freq) + "Hz";
            }
        } else {
            valueEl.textContent = Math.round(value * 100) + "%";
        }
    }

    function updateAllFaders() {
        for (let i = 0; i < 5; i++) {
            updateFader(i);
        }
    }

    // Fader send throttle: max ~33 messages/sec per fader to avoid zipper noise
    var faderLastSendTime = [0, 0, 0, 0, 0];
    var faderPendingSend = [null, null, null, null, null];
    var FADER_THROTTLE_MS = 30;

    function setFaderValue(id, normalizedValue) {
        normalizedValue = Math.max(0, Math.min(1, normalizedValue));

        var altVal;
        if (id === 1) {
            if (fader1AltState === 0) faderValues[1] = normalizedValue;
            else if (fader1AltState === 1) altFaderValues[1] = normalizedValue;
            else fader1CompValue = normalizedValue;
            altVal = fader1AltState;
        } else if (altModes[id]) {
            altFaderValues[id] = normalizedValue;
            altVal = 1;
        } else {
            faderValues[id] = normalizedValue;
            altVal = 0;
        }

        // Mirror OSC1/OSC2 fader values when LINK OSC levels is active
        if (id === 0 && linkOscLevels) {
            faderValues[0] = normalizedValue;
            altFaderValues[0] = normalizedValue;
        }

        // Keep OSC1/OSC2 button state in sync with current fader values —
        // if user drags fader above 0 while muted, button should flip to "on"
        // (and vice versa). Also refresh the "pre-mute" memory to current level.
        if (id === 0) {
            var newOsc1 = faderValues[0] > 0;
            var newOsc2 = altFaderValues[0] > 0;
            if (newOsc1 !== osc1Enabled || newOsc2 !== osc2Enabled) {
                osc1Enabled = newOsc1;
                osc2Enabled = newOsc2;
                updateOscButtons();
            }
            if (faderValues[0] > 0) osc1PreMute = faderValues[0];
            if (altFaderValues[0] > 0) osc2PreMute = altFaderValues[0];
        }

        updateFader(id);

        var msg = { type: "fader", id: id, value: normalizedValue, alt: altVal };
        var now = performance.now();
        var elapsed = now - faderLastSendTime[id];

        if (elapsed >= FADER_THROTTLE_MS) {
            send(msg);
            faderLastSendTime[id] = now;
            if (faderPendingSend[id] !== null) {
                clearTimeout(faderPendingSend[id]);
                faderPendingSend[id] = null;
            }
        } else {
            // Schedule a trailing send so final position always arrives
            if (faderPendingSend[id] !== null) clearTimeout(faderPendingSend[id]);
            faderPendingSend[id] = setTimeout(function () {
                send(msg);
                faderLastSendTime[id] = performance.now();
                faderPendingSend[id] = null;
            }, FADER_THROTTLE_MS - elapsed);
        }
    }

    // ═══ Touch / Mouse Fader Interaction ═══
    let activeFader = -1;

    function getFaderValue(track, clientY) {
        const rect = track.getBoundingClientRect();
        const y = clientY - rect.top;
        return 1 - (y / rect.height);
    }

    function onFaderStart(e, id) {
        if (midiLearnActive) return;  // suppress fader drag in learn mode
        e.preventDefault();
        activeFader = id;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        setFaderValue(id, getFaderValue(faderTracks[id], clientY));
    }

    function onFaderMove(e) {
        if (activeFader < 0) return;
        e.preventDefault();
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        setFaderValue(activeFader, getFaderValue(faderTracks[activeFader], clientY));
    }

    function onFaderEnd() {
        activeFader = -1;
    }

    faderTracks.forEach(function (track, i) {
        track.addEventListener("mousedown", function (e) { onFaderStart(e, i); });
        track.addEventListener("touchstart", function (e) { onFaderStart(e, i); }, { passive: false });
    });

    document.addEventListener("mousemove", onFaderMove);
    document.addEventListener("touchmove", onFaderMove, { passive: false });
    document.addEventListener("mouseup", onFaderEnd);
    document.addEventListener("touchend", onFaderEnd);

    // ═══ Alt Buttons (visual switch only — fader routing handles the rest) ═══
    altBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            const id = parseInt(btn.dataset.id);
            if (id === 1) {
                // Piano fader cycles: Volume(0) → Tone(1) → Comp(2) → Volume(0)
                fader1AltState = (fader1AltState + 1) % 3;
                altModes[id] = fader1AltState > 0;
                btn.classList.toggle("active", altModes[id]);
                faderColumns[id].classList.toggle("alt-mode", altModes[id]);
                updateFader(id);
            } else {
                // Simple toggle (OSC1/2, FX reverb/shimmer)
                altModes[id] = !altModes[id];
                btn.classList.toggle("active", altModes[id]);
                faderColumns[id].classList.toggle("alt-mode", altModes[id]);
                updateFader(id);
                if (id === 0) updatePadOctaveDisplay();
            }
        });
    });

    // ═══ OSC / Piano Mute Toggles ═══
    function updateOscButtons() {
        osc1Btn.classList.toggle("active", osc1Enabled);
        osc1Btn.classList.toggle("off", !osc1Enabled);
        osc2Btn.classList.toggle("active", osc2Enabled);
        osc2Btn.classList.toggle("off", !osc2Enabled);
    }

    osc1Btn.addEventListener("click", function () {
        if (osc1Enabled) {
            // Mute: save current value, set to 0
            osc1PreMute = faderValues[0] > 0 ? faderValues[0] : osc1PreMute;
            osc1Enabled = false;
            faderValues[0] = 0;
            send({ type: "fader", id: 0, value: 0, alt: false });
        } else {
            // Unmute: restore saved value
            osc1Enabled = true;
            faderValues[0] = osc1PreMute;
            send({ type: "fader", id: 0, value: osc1PreMute, alt: false });
        }
        updateOscButtons();
        if (!altModes[0]) updateFader(0);
    });

    osc2Btn.addEventListener("click", function () {
        if (osc2Enabled) {
            osc2PreMute = altFaderValues[0] > 0 ? altFaderValues[0] : osc2PreMute;
            osc2Enabled = false;
            altFaderValues[0] = 0;
            send({ type: "fader", id: 0, value: 0, alt: true });
        } else {
            osc2Enabled = true;
            altFaderValues[0] = osc2PreMute;
            send({ type: "fader", id: 0, value: osc2PreMute, alt: true });
        }
        updateOscButtons();
        if (altModes[0]) updateFader(0);
    });

    pianoBtn.addEventListener("click", function () {
        send({ type: "instrument_cycle" });
    });

    function updateInstrumentButton() {
        if (instrumentMode === "piano") {
            pianoBtn.textContent = "PIANO";
            pianoBtn.classList.add("active");
            pianoBtn.classList.remove("off");
            pianoEnabled = true;
        } else if (instrumentMode === "organ") {
            pianoBtn.textContent = "ORGAN";
            pianoBtn.classList.add("active");
            pianoBtn.classList.remove("off");
            pianoEnabled = true;
        } else {
            pianoBtn.textContent = "KEYS";
            pianoBtn.classList.remove("active");
            pianoBtn.classList.add("off");
            pianoEnabled = false;
        }
    }

    // ═══ Presets — tap empty=save, tap filled=load, long-press filled=overwrite, double-tap filled=delete confirm ═══
    // In EDIT mode: tapping a preset opens an action popup (RENAME / DELETE / CANCEL).
    var presetLastTap = {};
    var presetLongTimer = {};
    var presetDeleteSlot = -1; // which slot is showing delete X
    var presetEditMode = false;
    var presetLabels = ["", "", "", "", ""];
    var actionPopupSlot = -1;
    var labelPickerSlot = -1;

    var presetEditBtn = document.getElementById("preset-edit-btn");
    var presetLayerBtn = document.getElementById("preset-layer-btn");
    var actionPopup = document.getElementById("preset-action-popup");
    var labelPickerModal = document.getElementById("label-picker-modal");
    var rearrangeSourceSlot = -1;  // slot number picked up for swap, -1 when idle

    // Presets now have 2 layers of 5 slots each (slots 0-4 = L1, slots 5-9 = L2).
    // The 5 on-screen buttons dynamically remap data-slot when the layer toggles.
    var currentLayer = 0;  // 0 = L1, 1 = L2
    var allPresetSaved = [];
    for (var _i = 0; _i < 10; _i++) allPresetSaved.push(false);
    presetLabels = [];
    for (var _j = 0; _j < 10; _j++) presetLabels.push("");

    function applyLayer() {
        presetBtns.forEach(function (btn, pos) {
            var slot = pos + currentLayer * 5;
            btn.dataset.slot = slot;
            var isSaved = !!allPresetSaved[slot];
            btn.classList.remove("loaded");
            btn.classList.toggle("filled", isSaved);
            btn.classList.toggle("empty", !isSaved);
            btn.classList.toggle("picked", slot === rearrangeSourceSlot);
            var span = btn.querySelector(".preset-label");
            if (span) span.textContent = presetLabels[slot] || "";
        });
        presetLayerBtn.textContent = "L" + (currentLayer + 1);
        presetLayerBtn.classList.toggle("active", currentLayer > 0);
        hideActionPopup();
    }

    presetLayerBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        currentLayer = (currentLayer + 1) % 2;
        applyLayer();
    });

    function setEditMode(on) {
        presetEditMode = on;
        presetEditBtn.classList.toggle("active", on);
        document.getElementById("app").classList.toggle("preset-editing", on);
        if (!on) {
            hideActionPopup();
            hideLabelPicker();
            cancelRearrange();
        }
    }

    function cancelRearrange() {
        if (rearrangeSourceSlot >= 0) {
            var btn = btnForSlot(rearrangeSourceSlot);
            if (btn) btn.classList.remove("picked");
        }
        rearrangeSourceSlot = -1;
    }

    function pickupForRearrange(slot) {
        cancelRearrange();
        rearrangeSourceSlot = slot;
        var btn = btnForSlot(slot);
        if (btn) btn.classList.add("picked");
    }

    presetEditBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        setEditMode(!presetEditMode);
    });

    function showActionPopup(slot, anchorEl) {
        actionPopupSlot = slot;
        var rect = anchorEl.getBoundingClientRect();
        actionPopup.classList.remove("hidden");
        // Position above the preset button, clamped to viewport
        var popRect = actionPopup.getBoundingClientRect();
        var left = rect.left + rect.width / 2 - popRect.width / 2;
        left = Math.max(4, Math.min(window.innerWidth - popRect.width - 4, left));
        var top = rect.top - popRect.height - 6;
        if (top < 4) top = rect.bottom + 6;
        actionPopup.style.left = left + "px";
        actionPopup.style.top = top + "px";
    }

    function hideActionPopup() {
        actionPopup.classList.add("hidden");
        actionPopupSlot = -1;
    }

    actionPopup.querySelectorAll(".preset-action").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            var action = btn.dataset.action;
            var slot = actionPopupSlot;
            hideActionPopup();
            if (slot < 0) return;
            if (action === "rename") {
                showLabelPicker(slot);
            } else if (action === "delete") {
                send({ type: "preset_delete", slot: slot });
            } else if (action === "rearrange") {
                pickupForRearrange(slot);
            }
        });
    });

    function showLabelPicker(slot) {
        labelPickerSlot = slot;
        labelPickerModal.classList.remove("hidden");
    }

    function hideLabelPicker() {
        labelPickerModal.classList.add("hidden");
        labelPickerSlot = -1;
    }

    document.getElementById("label-picker-close").addEventListener("click", function (e) {
        e.stopPropagation();
        hideLabelPicker();
    });
    labelPickerModal.addEventListener("click", function (e) {
        // Click outside the inner box closes the picker
        if (e.target === labelPickerModal) hideLabelPicker();
    });
    labelPickerModal.querySelectorAll(".label-pill").forEach(function (pill) {
        pill.addEventListener("click", function (e) {
            e.stopPropagation();
            var label = pill.dataset.label;
            var slot = labelPickerSlot;
            hideLabelPicker();
            if (slot < 0) return;
            send({ type: "preset_label", slot: slot, label: label });
        });
    });

    function setPresetLabel(slot, label) {
        presetLabels[slot] = label || "";
        // If this slot is currently visible (matches the active layer), update its span
        var pos = slot - currentLayer * 5;
        if (pos >= 0 && pos < 5) {
            var btn = presetBtns[pos];
            if (btn) {
                var span = btn.querySelector(".preset-label");
                if (span) span.textContent = presetLabels[slot];
            }
        }
    }

    function syncPresetLabels(labels) {
        if (!Array.isArray(labels)) return;
        for (var i = 0; i < 10; i++) {
            presetLabels[i] = labels[i] || "";
        }
        applyLayer();  // re-render to refresh label spans
    }

    function cancelDeleteConfirm() {
        if (presetDeleteSlot >= 0) {
            var old = btnForSlot(presetDeleteSlot);
            if (old) {
                var x = old.querySelector(".preset-delete-x");
                if (x) x.remove();
                old.classList.remove("delete-confirm");
            }
        }
        presetDeleteSlot = -1;
    }

    // Read the CURRENT slot number from the button's data-slot attribute
    // (not a cached closure value) so layer switching works correctly.
    function slotOf(btn) { return parseInt(btn.dataset.slot); }

    presetBtns.forEach(function (btn) {
        btn.addEventListener("contextmenu", function (e) { e.preventDefault(); });

        btn.addEventListener("pointerdown", function (e) {
            e.preventDefault();  // prevent touch selection/highlight
            if (presetEditMode) return;
            var slot = slotOf(btn);
            presetLongTimer[slot] = "armed";
            if (btn.classList.contains("filled")) {
                presetLongTimer[slot] = setTimeout(function () {
                    presetLongTimer[slot] = null;
                    cancelDeleteConfirm();
                    send({ type: "preset_save", slot: slot });
                }, 600);
            }
        });

        btn.addEventListener("pointerup", function (e) {
            var slot = slotOf(btn);
            if (presetEditMode) {
                // If a preset is "picked up" for rearrange, this tap is the
                // swap target. Tapping the source again cancels the pickup.
                if (rearrangeSourceSlot >= 0) {
                    if (slot === rearrangeSourceSlot) {
                        cancelRearrange();
                    } else {
                        send({ type: "preset_swap",
                               source: rearrangeSourceSlot,
                               target: slot });
                        cancelRearrange();
                    }
                    return;
                }
                // Otherwise open the action popup on filled slots only
                if (btn.classList.contains("filled")) {
                    showActionPopup(slot, btn);
                }
                return;
            }

            var wasLong = (presetLongTimer[slot] === null);
            if (typeof presetLongTimer[slot] === "number") {
                clearTimeout(presetLongTimer[slot]);
            }
            if (wasLong) {
                presetLongTimer[slot] = undefined;
                return;
            }
            presetLongTimer[slot] = undefined;

            if (e.target.classList && e.target.classList.contains("preset-delete-x")) {
                send({ type: "preset_delete", slot: slot });
                cancelDeleteConfirm();
                return;
            }

            if (presetDeleteSlot >= 0 && presetDeleteSlot !== slot) {
                cancelDeleteConfirm();
            }

            if (btn.classList.contains("empty")) {
                send({ type: "preset_save", slot: slot });
                return;
            }

            var now = Date.now();
            var last = presetLastTap[slot] || 0;
            presetLastTap[slot] = now;

            if (now - last < 400) {
                presetLastTap[slot] = 0;
                cancelDeleteConfirm();
                presetDeleteSlot = slot;
                btn.classList.add("delete-confirm");
                var x = document.createElement("span");
                x.className = "preset-delete-x";
                x.textContent = "X";
                btn.appendChild(x);
                return;
            }

            if (presetDeleteSlot === slot) {
                cancelDeleteConfirm();
                return;
            }

            send({ type: "preset_load", slot: slot });
        });

        btn.addEventListener("pointerleave", function () {
            var slot = slotOf(btn);
            if (typeof presetLongTimer[slot] === "number") {
                clearTimeout(presetLongTimer[slot]);
            }
            presetLongTimer[slot] = undefined;
        });
    });

    // Cancel delete confirm when tapping elsewhere
    document.addEventListener("click", function (e) {
        if (presetDeleteSlot >= 0) {
            var btn = btnForSlot(presetDeleteSlot);
            if (btn && !btn.contains(e.target)) {
                cancelDeleteConfirm();
            }
        }
        // Close action popup if clicking outside it (and not a preset button)
        if (actionPopupSlot >= 0 && !actionPopup.contains(e.target)) {
            var isPreset = false;
            for (var i = 0; i < presetBtns.length; i++) {
                if (presetBtns[i].contains(e.target)) { isPreset = true; break; }
            }
            if (!isPreset) hideActionPopup();
        }
    });

    // Return the on-screen button element for a given slot (0-9), or null if
    // that slot is not in the currently visible layer.
    function btnForSlot(slot) {
        var pos = slot - currentLayer * 5;
        if (pos < 0 || pos >= 5) return null;
        return presetBtns[pos];
    }

    function markPresetSaved(slot) {
        allPresetSaved[slot] = true;
        var btn = btnForSlot(slot);
        if (!btn) return;
        btn.classList.remove("empty");
        btn.classList.add("filled", "just-saved");
        presetBtns.forEach(function (b) { b.classList.remove("loaded"); });
        btn.classList.add("loaded");
        loadedPreset = slot;
        setTimeout(function () { btn.classList.remove("just-saved"); }, 600);
    }

    function markPresetLoaded(slot) {
        presetBtns.forEach(function (b) { b.classList.remove("loaded"); });
        var btn = btnForSlot(slot);
        if (btn) btn.classList.add("loaded");
        loadedPreset = slot;
    }

    function markPresetDeleted(slot) {
        allPresetSaved[slot] = false;
        var btn = btnForSlot(slot);
        if (!btn) return;
        btn.classList.remove("filled", "loaded", "delete-confirm");
        btn.classList.add("empty");
        if (loadedPreset === slot) loadedPreset = -1;
    }

    function syncPresetSlots(presetSaved) {
        if (!presetSaved) return;
        for (var i = 0; i < 10; i++) {
            allPresetSaved[i] = !!presetSaved[i];
        }
        applyLayer();
    }

    function updateLevelDots(peak) {
        // 8 dots, analog console style: bottom = quiet, top = clip
        var thresholds = [0.05, 0.12, 0.22, 0.35, 0.50, 0.68, 0.85, 0.97];
        for (var i = 0; i < levelDots.length; i++) {
            var dotIdx = parseInt(levelDots[i].dataset.dot);
            if (peak >= thresholds[dotIdx]) {
                levelDots[i].classList.add("lit");
            } else {
                levelDots[i].classList.remove("lit");
            }
        }
    }

    function flashMidiIndicator() {
        midiIndicator.classList.add("flash");
        if (midiFlashTimeout) clearTimeout(midiFlashTimeout);
        midiFlashTimeout = setTimeout(function () {
            midiIndicator.classList.remove("flash");
        }, 80);
    }


    // ═══ Transpose ═══
    transposeDown.addEventListener("click", function () {
        if (transposeValue > -12) {
            transposeValue--;
            updateTransposeDisplay();
            send({ type: "transpose", semitones: transposeValue });
        }
    });

    transposeUp.addEventListener("click", function () {
        if (transposeValue < 12) {
            transposeValue++;
            updateTransposeDisplay();
            send({ type: "transpose", semitones: transposeValue });
        }
    });

    function updateTransposeDisplay() {
        const prefix = transposeValue > 0 ? "+" : "";
        transposeDisplay.textContent = "T" + prefix + transposeValue;
        transposeUp.classList.toggle("shifted", transposeValue > 0);
        transposeDown.classList.toggle("shifted", transposeValue < 0);
    }

    // ═══ Panic / Stop ═══
    document.getElementById("panic-btn").addEventListener("click", function () {
        send({ type: "panic" });
    });

    // ═══ Hidden retro mode — click logo to toggle (dots + grain + amber retint all at once) ═══
    document.getElementById("logo-btn").addEventListener("click", function () {
        var on = !document.body.classList.contains("retro-mode");
        document.body.classList.toggle("retro-mode", on);
        document.body.classList.toggle("retro-amber", on);
    });

    // ═══ Shimmer & Freeze ═══
    shimmerBtn.addEventListener("click", function () {
        shimmerEnabled = !shimmerEnabled;
        if (!shimmerEnabled) {
            freezeEnabled = false;
            send({ type: "freeze_toggle", enabled: false });
            updateFreezeDisplay();
        }
        updateShimmerDisplay();
        send({ type: "shimmer_toggle", enabled: shimmerEnabled });
    });

    freezeBtn.addEventListener("click", function () {
        freezeEnabled = !freezeEnabled;
        updateFreezeDisplay();
        send({ type: "freeze_toggle", enabled: freezeEnabled });
    });

    shimOctBtn.addEventListener("click", function () {
        shimmerHigh = !shimmerHigh;
        updateShimmerDisplay();
        send({ type: "shimmer_high_toggle", enabled: shimmerHigh });
    });

    // ═══ Master Fade (musical fade out / fade back in) ═══
    fadeBtn.addEventListener("click", function () {
        fadedOut = !fadedOut;
        updateFadeDisplay();
        send({ type: "fade_toggle", faded_out: fadedOut });
    });

    function updateFadeDisplay() {
        fadeBtn.classList.toggle("active", fadedOut);
        fadeBtn.textContent = fadedOut ? "UP" : "FADE";
    }

    function updateShimmerDisplay() {
        shimmerBtn.textContent = shimmerEnabled ? "SHIM" : "SHIM";
        shimmerBtn.classList.toggle("active", shimmerEnabled);
        // Show/hide freeze + +12 buttons based on shimmer state
        freezeBtn.classList.toggle("hidden", !shimmerEnabled);
        shimOctBtn.classList.toggle("hidden", !shimmerEnabled);
        shimOctBtn.classList.toggle("active", shimmerHigh);
    }

    function updateFreezeDisplay() {
        freezeBtn.textContent = "FRZ";
        freezeBtn.classList.toggle("active", freezeEnabled);
    }

    // ═══ Drone ═══
    droneBtn.addEventListener("click", function () {
        droneEnabled = !droneEnabled;
        updateDroneDisplay();
        send({ type: "drone_toggle", enabled: droneEnabled });
    });

    function updateDroneDisplay() {
        droneBtn.classList.toggle("active", droneEnabled);
    }

    // ═══ Sympathetic Resonance (RES button) ═══
    var resBtn = document.getElementById("res-btn");
    var resEnabled = false;

    resBtn.addEventListener("click", function () {
        resEnabled = !resEnabled;
        resBtn.classList.toggle("active", resEnabled);
        send({ type: "setting", section: "synth_pad", param: "sympathetic_enabled", value: resEnabled });
    });

    // ═══ Saturation (SAT button) ═══
    var satBtn = document.getElementById("sat-btn");
    var satEnabled = false;

    satBtn.addEventListener("click", function () {
        satEnabled = !satEnabled;
        satBtn.classList.toggle("active", satEnabled);
        send({ type: "setting", section: "master", param: "saturation_enabled", value: satEnabled });
    });

    // ═══ Octave Controls ═══
    // Pad octave buttons: ALT-off = OSC1, ALT-on = OSC2
    function updatePadOctaveDisplay() {
        const display = document.querySelector('.oct-display[data-inst="pad"]');
        display.textContent = altModes[0] ? osc2Octave : osc1Octave;
    }
    document.querySelectorAll(".oct-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const inst = btn.dataset.inst;
            const dir = btn.classList.contains("oct-up") ? 1 : -1;
            if (inst === "pad") {
                if (altModes[0]) {
                    osc2Octave = Math.max(-3, Math.min(3, osc2Octave + dir));
                    send({ type: "octave", instrument: "osc2", octave: osc2Octave });
                } else {
                    osc1Octave = Math.max(-3, Math.min(3, osc1Octave + dir));
                    send({ type: "octave", instrument: "osc1", octave: osc1Octave });
                }
                updatePadOctaveDisplay();
            } else {
                pianoOctave = Math.max(-3, Math.min(3, pianoOctave + dir));
                document.querySelector('.oct-display[data-inst="piano"]').textContent = pianoOctave;
                send({ type: "octave", instrument: "piano", octave: pianoOctave });
            }
        });
    });

    // ═══ Settings Modal ═══
    menuBtn.addEventListener("click", function () {
        settingsModal.classList.remove("hidden");
    });

    settingsClose.addEventListener("click", function () {
        settingsModal.classList.add("hidden");
    });

    settingsModal.addEventListener("click", function (e) {
        if (e.target === settingsModal) {
            settingsModal.classList.add("hidden");
        }
    });

    settingsTabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
            const target = tab.dataset.tab;
            settingsTabs.forEach(function (t) { t.classList.remove("active"); });
            settingsPanels.forEach(function (p) { p.classList.remove("active"); });
            tab.classList.add("active");
            document.querySelector('.settings-panel[data-panel="' + target + '"]').classList.add("active");
        });
    });

    // Frequency params that need log-scale sliders
    var FREQ_PARAMS = {
        "filter_highcut_hz": true, "filter_lowcut_hz": true,
        "filter_range_min": true, "filter_range_max": true,
        "tone_range_min": true, "tone_range_max": true,
        "reverb_low_cut": true, "reverb_high_cut": true,
        "osc1_indep_cutoff": true, "osc2_indep_cutoff": true,
        "eq_low_freq": true, "eq_mid_freq": true, "eq_high_freq": true,
        "eq_lowcut_hz": true,
    };

    var EQ_GAIN_PARAMS = {
        "eq_low_gain": true, "eq_mid_gain": true, "eq_high_gain": true,
    };

    function sliderToHz(slider01, minHz, maxHz) {
        return minHz * Math.pow(maxHz / minHz, slider01);
    }

    function hzToSlider(hz, minHz, maxHz) {
        return Math.log(hz / minHz) / Math.log(maxHz / minHz);
    }

    function formatHz(value) {
        if (value >= 1000) return (value / 1000).toFixed(1) + "kHz";
        return Math.round(value) + "Hz";
    }

    // Settings sliders
    document.querySelectorAll(".setting-slider").forEach(function (slider) {
        slider.addEventListener("input", function () {
            const section = slider.dataset.section;
            const param = slider.dataset.param;
            let value = parseFloat(slider.value);
            const valueEl = slider.parentElement.querySelector(".setting-value");

            let displayValue;
            let sendValue;

            if (param === "osc1_pan" || param === "osc2_pan") {
                sendValue = value / 100;
                if (sendValue < -0.05) displayValue = "L" + Math.round(Math.abs(sendValue) * 100);
                else if (sendValue > 0.05) displayValue = "R" + Math.round(sendValue * 100);
                else displayValue = "C";
            } else if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                param === "reverb_space" || param === "unison_spread" ||
                param === "osc1_max" || param === "osc2_max" ||
                param === "leslie_depth" || param === "click_level" ||
                param === "drive") {
                sendValue = value / 100;
                displayValue = Math.round(sendValue * 100) + "%";
            } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms") {
                sendValue = value;
                displayValue = Math.round(value) + "ms";
            } else if (param === "reverb_wet_gain") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(1) + "x";
            } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(2) + "x";
            } else if (param === "sympathetic_level") {
                var t = value / 1000;
                sendValue = t * t * t * 0.15;
                displayValue = (sendValue * 100).toFixed(3) + "%";
            } else if (param === "unison_detune") {
                sendValue = value / 1000;
                displayValue = sendValue.toFixed(3);
            } else if (param === "reverb_decay_seconds") {
                sendValue = value / 10;
                displayValue = sendValue.toFixed(1);
            } else if (EQ_GAIN_PARAMS[param]) {
                sendValue = value / 10;
                var sign = sendValue > 0.05 ? "+" : "";
                displayValue = sign + sendValue.toFixed(1) + "dB";
            } else if (FREQ_PARAMS[param]) {
                // Log-scale: slider 0-1000 maps to minHz..maxHz exponentially
                var minHz = parseFloat(slider.dataset.hzMin || slider.min);
                var maxHz = parseFloat(slider.dataset.hzMax || slider.max);
                var t = value / 1000;
                sendValue = Math.round(sliderToHz(t, minHz, maxHz));
                displayValue = formatHz(sendValue);
            } else {
                sendValue = value;
                displayValue = Math.round(value).toString();
            }

            if (valueEl) {
                valueEl.textContent = displayValue;
            }

            // Trim sliders don't drive fader position — they set the ceiling
            // The fader's full throw maps to 0..trim value

            send({
                type: "setting",
                section: section,
                param: param,
                value: sendValue,
            });
        });
    });

    // Settings checkboxes
    function updateIndepFilterVisibility() {
        var osc1cb = document.querySelector('[data-param="osc1_filter_enabled"]');
        var osc2cb = document.querySelector('[data-param="osc2_filter_enabled"]');
        var osc1row = document.getElementById("osc1-indep-filter");
        var osc2row = document.getElementById("osc2-indep-filter");
        if (osc1cb && osc1row) osc1row.style.display = osc1cb.checked ? "none" : "";
        if (osc2cb && osc2row) osc2row.style.display = osc2cb.checked ? "none" : "";
        // Organ: hide independent filter sliders when shared filter is on
        var organCb = document.querySelector('[data-param="shared_filter_enabled"]');
        var organLow = document.getElementById("organ-lowcut-row");
        var organHigh = document.getElementById("organ-highcut-row");
        if (organCb && organLow) organLow.style.display = organCb.checked ? "none" : "";
        if (organCb && organHigh) organHigh.style.display = organCb.checked ? "none" : "";
    }
    document.querySelectorAll(".setting-checkbox").forEach(function (checkbox) {
        checkbox.addEventListener("change", function () {
            send({
                type: "setting",
                section: checkbox.dataset.section,
                param: checkbox.dataset.param,
                value: checkbox.checked,
            });
            updateIndepFilterVisibility();
        });
    });

    // Settings selects
    document.querySelectorAll(".setting-select").forEach(function (select) {
        select.addEventListener("change", function () {
            send({
                type: "setting",
                section: select.dataset.section,
                param: select.dataset.param,
                value: select.value,
            });
        });
    });

    function updateSettingsSliders() {
        if (!state) return;

        document.querySelectorAll(".setting-slider").forEach(function (slider) {
            const section = slider.dataset.section;
            const param = slider.dataset.param;
            let sectionData = state[section];
            if (!sectionData) return;

            let value;
            if (param.startsWith("adsr.")) {
                const adsr = sectionData.adsr;
                if (adsr) {
                    value = adsr[param.split(".")[1]];
                }
            } else {
                value = sectionData[param];
            }

            if (value === undefined) return;

            // Reverse the normalization
            if (param === "osc1_pan" || param === "osc2_pan") {
                slider.value = value * 100;
            } else if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                param === "reverb_space" || param === "unison_spread" ||
                param === "osc1_max" || param === "osc2_max" ||
                param === "leslie_depth" || param === "click_level" ||
                param === "drive") {
                slider.value = value * 100;
            } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms") {
                slider.value = value;
            } else if (param === "reverb_wet_gain") {
                slider.value = value * 100;
            } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                slider.value = value * 100;
            } else if (param === "sympathetic_level") {
                slider.value = Math.round(Math.cbrt(Math.max(0, value) / 0.15) * 1000);
            } else if (param === "unison_detune") {
                slider.value = value * 1000;
            } else if (param === "reverb_decay_seconds") {
                slider.value = value * 10;
            } else if (EQ_GAIN_PARAMS[param]) {
                slider.value = value * 10;
            } else if (FREQ_PARAMS[param]) {
                // Log-scale: Hz value → 0-1000 slider position
                var minHz = parseFloat(slider.dataset.hzMin || slider.min);
                var maxHz = parseFloat(slider.dataset.hzMax || slider.max);
                slider.value = Math.round(hzToSlider(value, minHz, maxHz) * 1000);
            } else {
                slider.value = value;
            }

            const valueEl = slider.parentElement.querySelector(".setting-value");
            if (valueEl) {
                if (param === "osc1_pan" || param === "osc2_pan") {
                    if (value < -0.05) valueEl.textContent = "L" + Math.round(Math.abs(value) * 100);
                    else if (value > 0.05) valueEl.textContent = "R" + Math.round(value * 100);
                    else valueEl.textContent = "C";
                } else if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                    param === "reverb_space" || param === "unison_spread" ||
                    param === "osc1_max" || param === "osc2_max" ||
                    param === "leslie_depth" || param === "click_level" ||
                param === "drive") {
                    valueEl.textContent = Math.round(value * 100) + "%";
                } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms") {
                    valueEl.textContent = Math.round(value) + "ms";
                } else if (param === "reverb_wet_gain") {
                    valueEl.textContent = value.toFixed(1) + "x";
                } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                    valueEl.textContent = value.toFixed(2) + "x";
                } else if (param === "sympathetic_level") {
                    valueEl.textContent = (value * 100).toFixed(3) + "%";
                } else if (param === "unison_detune") {
                    valueEl.textContent = value.toFixed(3);
                } else if (param === "reverb_decay_seconds") {
                    valueEl.textContent = value.toFixed(1);
                } else if (EQ_GAIN_PARAMS[param]) {
                    var sign = value > 0.05 ? "+" : "";
                    valueEl.textContent = sign + value.toFixed(1) + "dB";
                } else if (FREQ_PARAMS[param]) {
                    valueEl.textContent = formatHz(value);
                } else {
                    valueEl.textContent = Math.round(value).toString();
                }
            }
        });

        // Update checkboxes
        document.querySelectorAll(".setting-checkbox").forEach(function (checkbox) {
            const section = checkbox.dataset.section;
            const param = checkbox.dataset.param;
            let sectionData = state[section];
            if (sectionData && sectionData[param] !== undefined) {
                checkbox.checked = sectionData[param];
            }
        });
        updateIndepFilterVisibility();

        // Update selects
        document.querySelectorAll(".setting-select").forEach(function (select) {
            const section = select.dataset.section;
            const param = select.dataset.param;
            let sectionData = state[section];
            if (sectionData && sectionData[param]) {
                select.value = sectionData[param];
            }
        });

        syncKnobRotations();
    }

    // ═══ Knobs ═══
    function updateKnobRotation(knob) {
        var slider = knob.querySelector(".knob-slider");
        if (!slider) return;
        var min = parseFloat(slider.min);
        var max = parseFloat(slider.max);
        var value = parseFloat(slider.value);
        var norm = (value - min) / (max - min);
        norm = Math.max(0, Math.min(1, norm));
        var angle = -135 + norm * 270;
        var indicator = knob.querySelector(".knob-indicator");
        if (indicator) {
            indicator.style.transform = "rotate(" + angle + "deg)";
        }
    }

    function syncKnobRotations() {
        document.querySelectorAll(".knob").forEach(updateKnobRotation);
    }

    document.querySelectorAll(".knob").forEach(function (knob) {
        var slider = knob.querySelector(".knob-slider");
        if (!slider) return;
        updateKnobRotation(knob);

        var startY = 0, startValue = 0, dragging = false;

        knob.addEventListener("pointerdown", function (e) {
            e.preventDefault();
            dragging = true;
            startY = e.clientY;
            startValue = parseFloat(slider.value);
            knob.setPointerCapture(e.pointerId);
        });

        knob.addEventListener("pointermove", function (e) {
            if (!dragging) return;
            var dy = startY - e.clientY;
            var range = parseFloat(slider.max) - parseFloat(slider.min);
            var sensitivity = range / 200;
            var newValue = startValue + dy * sensitivity;
            newValue = Math.max(parseFloat(slider.min), Math.min(parseFloat(slider.max), newValue));
            if (newValue !== parseFloat(slider.value)) {
                slider.value = newValue;
                slider.dispatchEvent(new Event("input"));
                updateKnobRotation(knob);
            }
        });

        function endDrag(e) {
            if (!dragging) return;
            dragging = false;
            try { knob.releasePointerCapture(e.pointerId); } catch (err) {}
        }
        knob.addEventListener("pointerup", endDrag);
        knob.addEventListener("pointercancel", endDrag);
    });

    // ═══ MIDI Learn ═══
    function updateMidiLearnUI() {
        midiIndicator.classList.remove("learn-mode", "learn-waiting");
        if (midiLearnActive) {
            document.getElementById("app").classList.add("midi-learn-active");
            if (midiLearnWaiting) {
                midiIndicator.textContent = "MAP";
                midiIndicator.classList.add("learn-waiting");
            } else {
                midiIndicator.textContent = "MAP";
                midiIndicator.classList.add("learn-mode");
            }
        } else {
            document.getElementById("app").classList.remove("midi-learn-active");
            midiIndicator.textContent = "MIDI";
        }
        updateCCIndicators();
    }

    function updateCCIndicators() {
        document.querySelectorAll(".cc-indicator").forEach(function (el) { el.remove(); });
        if (!midiLearnActive) return;  // only show in learn mode
        for (var cc in ccMap) {
            var target = ccMap[cc];
            var col = faderColumns[target.id];
            if (col) {
                var dot = document.createElement("div");
                dot.className = "cc-indicator";
                var span = document.createElement("span");
                span.textContent = "CC" + cc;
                dot.appendChild(span);
                var xBtn = document.createElement("button");
                xBtn.className = "cc-delete";
                xBtn.textContent = "X";
                xBtn.dataset.cc = cc;
                function deleteCC(e) {
                    e.stopPropagation();
                    e.preventDefault();
                    var ccNum = e.currentTarget.dataset.cc;
                    send({ type: "midi_learn_clear", cc: ccNum });
                }
                xBtn.addEventListener("click", deleteCC);
                xBtn.addEventListener("touchend", deleteCC);
                dot.appendChild(xBtn);
                var label = col.querySelector(".fader-label");
                if (label) {
                    label.parentNode.insertBefore(dot, label.nextSibling);
                }
            }
        }
    }

    // Tap MIDI indicator to enter/exit learn mode
    midiIndicator.addEventListener("click", function () {
        if (midiLearnActive) {
            send({ type: "midi_learn_cancel" });
            midiLearnActive = false;
            midiLearnWaiting = false;
            updateMidiLearnUI();
        } else {
            send({ type: "midi_learn_start" });
            midiLearnActive = true;
            midiLearnWaiting = false;
            updateMidiLearnUI();
        }
    });

    // In learn mode, tapping a fader selects it for new CC mapping
    // (use the X button on a CC label to delete a mapping)
    function getFaderAlt(id) {
        if (id === 1) return fader1AltState;
        return altModes[id] ? 1 : 0;
    }

    faderTracks.forEach(function (track, i) {
        track.addEventListener("click", function () {
            if (!midiLearnActive || midiLearnWaiting) return;
            var alt = getFaderAlt(i);
            send({ type: "midi_learn_select", id: i, alt: alt });
            midiLearnWaiting = true;
            updateMidiLearnUI();
        });
    });

    // ═══ Initialize ═══
    updateAllFaders();
    updateTransposeDisplay();
    updateShimmerDisplay();
    connectWS();
    // Request CC map on load
    setTimeout(function () { send({ type: "get_cc_map" }); }, 1000);

    // ═══ Audio Output Selector ═══
    var outputMenu = document.getElementById("output-menu");

    function shortOutputName(name) {
        if (!name) return "OUT";
        var lower = name.toLowerCase();
        if (lower.indexOf("bluez") >= 0 || lower.indexOf("bluetooth") >= 0) return "BT";
        if (lower.indexOf("airpod") >= 0) return "PODS";
        if (lower.indexOf("hdmi") >= 0) return "HDMI";
        if (lower.indexOf("usb") >= 0) {
            if (lower.indexOf("art") >= 0) return "ART";
            if (lower.indexOf("burr") >= 0) return "USB";
            return "USB";
        }
        if (lower.indexOf("headphone") >= 0) return "HPH";
        // Fallback: strip prefix and take first 5 chars
        var cleaned = name.replace(/^alsa_output\./, "").replace(/^bluez_output\./, "");
        return cleaned.substring(0, 5).toUpperCase();
    }

    statusIndicator.addEventListener("click", function (e) {
        e.stopPropagation();
        if (statusIndicator.className !== "connected") return;
        if (!outputMenu.classList.contains("hidden")) {
            outputMenu.classList.add("hidden");
            return;
        }
        send({ type: "get_audio_outputs" });
    });

    document.addEventListener("click", function () {
        outputMenu.classList.add("hidden");
    });

    function showOutputMenu(outputs) {
        outputMenu.innerHTML = "";
        if (!outputs.length) {
            outputMenu.innerHTML = '<div class="output-option">No outputs found</div>';
            outputMenu.classList.remove("hidden");
            return;
        }
        outputs.forEach(function (out) {
            var div = document.createElement("div");
            div.className = "output-option" + (out.active ? " active" : "");
            div.textContent = out.name;
            div.addEventListener("click", function (e) {
                e.stopPropagation();
                send({ type: "set_audio_output", name: out.name });
            });
            outputMenu.appendChild(div);
        });
        outputMenu.classList.remove("hidden");
    }
})();
