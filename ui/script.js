/* Stave Synth — UI Logic & WebSocket Client */

(function () {
    "use strict";

    const WS_URL = "ws://" + window.location.hostname + ":8765";
    const RECONNECT_DELAY = 2000;

    // ═══ State ═══
    let ws = null;
    let state = null;
    let altModes = [false, false, false, false];
    let fader1AltState = 0;  // 0=Volume, 1=Tone, 2=Compressor (cycles on alt click)
    let fader2AltState = 0;  // 0=Filter, 1=Reverb Mix, 2=Shimmer Vol (cycles on alt click)
    let faderValues = [0.6, 0.5, 1.0, 0.85]; // osc1, piano, filter, master
    let altFaderValues = [0.4, 1.0, 0.65, 0];  // osc2, piano tone, reverb mix, -
    let fader1CompValue = 0.5;  // compressor amount (maps to threshold)
    let fader2ShimmerValue = 0.5;  // shimmer volume
    let transposeValue = 0;
    let shimmerEnabled = false;
    let freezeEnabled = false;
    let osc1Octave = 0;  // -3 to +3
    let osc2Octave = 0;  // -3 to +3
    let pianoOctave = 0; // -3 to +3

    // Mute state for OSC1/OSC2/Piano toggles
    let osc1Enabled = true;
    let osc2Enabled = true;
    let pianoEnabled = true;
    let osc1PreMute = 0.6;  // saved fader value before mute
    let osc2PreMute = 0.4;

    const FADER_LABELS = [
        ["OSC 1", "OSC 2"],
        ["PIANO", "TONE", "COMP"],
        ["FILTER", "REVERB MIX", "SHIMMER"],
        ["MASTER", "MASTER"],
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
    const freezeBtn = document.getElementById("freeze-btn");
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
    const clipIndicator = document.getElementById("clip-indicator");
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
        } else if (msg.type === "freeze_ack") {
            freezeEnabled = msg.enabled;
            updateFreezeDisplay();
            updateShimmerDisplay();
        } else if (msg.type === "preset_saved") {
            markPresetSaved(msg.slot);
        } else if (msg.type === "preset_loaded") {
            markPresetLoaded(msg.slot);
        } else if (msg.type === "midi_activity") {
            flashMidiIndicator();
        } else if (msg.type === "peak_level") {
            if (msg.peak > 1.0) {
                clipIndicator.classList.add("clipping");
                if (clipTimeout) clearTimeout(clipTimeout);
                clipTimeout = setTimeout(function () {
                    clipIndicator.classList.remove("clipping");
                }, 500);
            }
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
                statusIndicator.textContent = msg.name.length > 12
                    ? msg.name.substring(0, 12) : msg.name;
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
            } else if (id === 2) {
                if (alt === 0) faderValues[2] = val;
                else if (alt === 1) altFaderValues[2] = val;
                else fader2ShimmerValue = val;
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
            faderValues[0] = s.synth_pad.osc1_blend ?? 0.6;
            altFaderValues[0] = s.synth_pad.osc2_blend ?? 0.4;
            // Map filter cutoff back to 0-1 using configured range
            var fMin = s.synth_pad.filter_range_min ?? 150;
            var fMax = s.synth_pad.filter_range_max ?? 20000;
            var freq = s.synth_pad.filter_cutoff_hz ?? s.synth_pad.filter_highpass_hz ?? 8000;
            faderValues[2] = Math.log(freq / fMin) / Math.log(fMax / fMin);
            faderValues[2] = Math.max(0, Math.min(1, faderValues[2]));
            altFaderValues[2] = s.synth_pad.reverb_dry_wet ?? 0.65;
            fader2ShimmerValue = s.synth_pad.shimmer_mix ?? 0.5;
            shimmerEnabled = s.synth_pad.shimmer_enabled ?? false;
            freezeEnabled = s.synth_pad.freeze_enabled ?? false;
            updateFreezeDisplay();

            // Sync mute state with actual blend values
            osc1Enabled = faderValues[0] > 0;
            osc2Enabled = altFaderValues[0] > 0;
            if (osc1Enabled) osc1PreMute = faderValues[0];
            if (osc2Enabled) osc2PreMute = altFaderValues[0];
            updateOscButtons();
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
            faderValues[3] = s.master.volume ?? 0.85;
            transposeValue = s.master.transpose_semitones ?? 0;
        }
        if (s.ui) {
            syncPresetSlots(s.ui.preset_saved);
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
        if (id === 2) {
            if (fader2AltState === 0) return faderValues[2];
            if (fader2AltState === 1) return altFaderValues[2];
            return fader2ShimmerValue;
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
            label.textContent = FADER_LABELS[1][fader1AltState];
        } else if (id === 2) {
            label.textContent = FADER_LABELS[2][fader2AltState];
        } else {
            label.textContent = FADER_LABELS[id][altModes[id] ? 1 : 0];
        }

        if ((id === 2 && fader2AltState === 0) || (id === 1 && fader1AltState === 1)) {
            // Frequency display: filter cutoff or piano tone
            var minF, maxF;
            if (id === 1) {
                minF = (state && state.piano && state.piano.tone_range_min) ?? 200;
                maxF = (state && state.piano && state.piano.tone_range_max) ?? 20000;
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
        for (let i = 0; i < 4; i++) {
            updateFader(i);
        }
    }

    function setFaderValue(id, normalizedValue) {
        normalizedValue = Math.max(0, Math.min(1, normalizedValue));

        var altVal;
        if (id === 1) {
            if (fader1AltState === 0) faderValues[1] = normalizedValue;
            else if (fader1AltState === 1) altFaderValues[1] = normalizedValue;
            else fader1CompValue = normalizedValue;
            altVal = fader1AltState;
        } else if (id === 2) {
            if (fader2AltState === 0) faderValues[2] = normalizedValue;
            else if (fader2AltState === 1) altFaderValues[2] = normalizedValue;
            else fader2ShimmerValue = normalizedValue;
            altVal = fader2AltState;
        } else if (altModes[id]) {
            altFaderValues[id] = normalizedValue;
            altVal = 1;
        } else {
            faderValues[id] = normalizedValue;
            altVal = 0;
        }

        updateFader(id);

        send({
            type: "fader",
            id: id,
            value: normalizedValue,
            alt: altVal,
        });
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
                // Fader 1 cycles: Volume(0) → Tone(1) → Comp(2) → Volume(0)
                fader1AltState = (fader1AltState + 1) % 3;
                altModes[id] = fader1AltState > 0;
                btn.classList.toggle("active", altModes[id]);
                faderColumns[id].classList.toggle("alt-mode", altModes[id]);
                updateFader(id);
            } else if (id === 2) {
                // Fader 2 cycles: Filter(0) → Reverb Mix(1) → Shimmer(2) → Filter(0)
                fader2AltState = (fader2AltState + 1) % 3;
                altModes[id] = fader2AltState > 0;
                btn.classList.toggle("active", altModes[id]);
                faderColumns[id].classList.toggle("alt-mode", altModes[id]);
                updateFader(id);
            } else {
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
        pianoEnabled = !pianoEnabled;
        pianoBtn.classList.toggle("active", pianoEnabled);
        pianoBtn.classList.toggle("off", !pianoEnabled);
        send({ type: "setting", section: "piano", param: "enabled", value: pianoEnabled });
    });

    // ═══ Presets — tap empty to save, tap filled to load ═══
    presetBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            var slot = parseInt(btn.dataset.slot);
            if (btn.classList.contains("empty")) {
                // Save current state to this slot
                send({ type: "preset_save", slot: slot });
            } else {
                // Load this preset
                send({ type: "preset_load", slot: slot });
            }
        });
    });

    function markPresetSaved(slot) {
        var btn = presetBtns[slot];
        if (!btn) return;
        btn.classList.remove("empty");
        btn.classList.add("filled", "just-saved");
        // Clear loaded highlight from others
        presetBtns.forEach(function (b) { b.classList.remove("loaded"); });
        btn.classList.add("loaded");
        loadedPreset = slot;
        setTimeout(function () { btn.classList.remove("just-saved"); }, 600);
    }

    function markPresetLoaded(slot) {
        presetBtns.forEach(function (b) { b.classList.remove("loaded"); });
        var btn = presetBtns[slot];
        if (btn) btn.classList.add("loaded");
        loadedPreset = slot;
    }

    function syncPresetSlots(presetSaved) {
        if (!presetSaved) return;
        presetBtns.forEach(function (btn, i) {
            if (presetSaved[i]) {
                btn.classList.remove("empty");
                btn.classList.add("filled");
            } else {
                btn.classList.remove("filled", "loaded");
                btn.classList.add("empty");
            }
        });
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
        transposeDisplay.textContent = "T: " + prefix + transposeValue;
    }

    // ═══ Panic / Stop ═══
    document.getElementById("panic-btn").addEventListener("click", function () {
        send({ type: "panic" });
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

    function updateShimmerDisplay() {
        shimmerBtn.textContent = shimmerEnabled ? "SHIM" : "SHIM";
        shimmerBtn.classList.toggle("active", shimmerEnabled);
        // Show/hide freeze button based on shimmer state
        freezeBtn.classList.toggle("hidden", !shimmerEnabled);
    }

    function updateFreezeDisplay() {
        freezeBtn.textContent = "FRZ";
        freezeBtn.classList.toggle("active", freezeEnabled);
    }

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

    // Settings sliders
    document.querySelectorAll(".setting-slider").forEach(function (slider) {
        slider.addEventListener("input", function () {
            const section = slider.dataset.section;
            const param = slider.dataset.param;
            let value = parseFloat(slider.value);
            const valueEl = slider.parentElement.querySelector(".setting-value");

            let displayValue;
            let sendValue;

            if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                param === "osc1_blend" || param === "osc2_blend") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(2);
            } else if (param === "reverb_wet_gain") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(1) + "x";
            } else if (param === "unison_detune") {
                sendValue = value / 1000;
                displayValue = sendValue.toFixed(3);
            } else if (param === "reverb_decay_seconds") {
                sendValue = value / 10;
                displayValue = sendValue.toFixed(1);
            } else if (param === "filter_highcut_hz" || param === "filter_lowcut_hz" ||
                       param === "filter_range_min" ||
                       param === "filter_range_max" || param === "tone_range_min" ||
                       param === "tone_range_max" || param === "reverb_low_cut" ||
                       param === "reverb_high_cut" || param === "osc1_indep_cutoff" ||
                       param === "osc2_indep_cutoff") {
                sendValue = value;
                if (value >= 1000) {
                    displayValue = (value / 1000).toFixed(1) + "kHz";
                } else {
                    displayValue = Math.round(value) + "Hz";
                }
            } else {
                sendValue = value;
                displayValue = Math.round(value).toString();
            }

            if (valueEl) {
                valueEl.textContent = displayValue;
            }

            // Sync OSC blend sliders with faders
            if (param === "osc1_blend") {
                faderValues[0] = sendValue;
                if (sendValue > 0) { osc1Enabled = true; osc1PreMute = sendValue; }
                else { osc1Enabled = false; }
                updateOscButtons();
                if (!altModes[0]) updateFader(0);
                // Send as fader message for consistency
                send({ type: "fader", id: 0, value: sendValue, alt: false });
                return;
            }
            if (param === "osc2_blend") {
                altFaderValues[0] = sendValue;
                if (sendValue > 0) { osc2Enabled = true; osc2PreMute = sendValue; }
                else { osc2Enabled = false; }
                updateOscButtons();
                if (altModes[0]) updateFader(0);
                // Send as fader message for consistency
                send({ type: "fader", id: 0, value: sendValue, alt: true });
                return;
            }

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
            if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                param === "osc1_blend" || param === "osc2_blend") {
                slider.value = value * 100;
            } else if (param === "reverb_wet_gain") {
                slider.value = value * 100;
            } else if (param === "unison_detune") {
                slider.value = value * 1000;
            } else if (param === "reverb_decay_seconds") {
                slider.value = value * 10;
            } else {
                slider.value = value;
            }

            const valueEl = slider.parentElement.querySelector(".setting-value");
            if (valueEl) {
                if (param === "reverb_dry_wet" || param === "shimmer_mix" ||
                    param === "osc1_blend" || param === "osc2_blend") {
                    valueEl.textContent = value.toFixed(2);
                } else if (param === "reverb_wet_gain") {
                    valueEl.textContent = value.toFixed(1) + "x";
                } else if (param === "unison_detune") {
                    valueEl.textContent = value.toFixed(3);
                } else if (param === "reverb_decay_seconds") {
                    valueEl.textContent = value.toFixed(1);
                } else if (param === "filter_highcut_hz" || param === "filter_lowcut_hz" ||
                           param === "filter_range_min" ||
                           param === "filter_range_max" || param === "tone_range_min" ||
                           param === "tone_range_max" || param === "reverb_low_cut" ||
                           param === "reverb_high_cut" || param === "osc1_indep_cutoff" ||
                           param === "osc2_indep_cutoff") {
                    if (value >= 1000) {
                        valueEl.textContent = (value / 1000).toFixed(1) + "kHz";
                    } else {
                        valueEl.textContent = Math.round(value) + "Hz";
                    }
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
    }

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
        if (id === 2) return fader2AltState;
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
