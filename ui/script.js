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
    let fader3AltState = 0;  // 0=Reverb, 1=Shimmer, 2=Motion-bus (cycles on alt click)
    let faderValues = [0.6, 0.5, 1.0, 0.65, 0.85]; // osc1, piano, filter, fx(reverb), master
    let altFaderValues = [0.4, 1.0, 0, 0.5, 0];  // osc2, piano tone, -, fx(shimmer), -
    let fader3MotionValue = 1.0;  // motion bus mix (fader3 alt state 2)
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
        ["FX", "SHIMMER", "MOTION"],
        ["MASTER", "DRIVE"],
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
    const droneBtn = document.getElementById("drone-btn");  // may be null — moved to pad player
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
    const bpmVal = document.getElementById("bpm-val");
    const tapBtn = null;  // removed — BPM number itself is now the tap target
    const panicBtn = document.getElementById("panic-btn");
    let bpm = 120;
    let tapTimes = [];
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
            // Reset ephemeral UI: FX fader alt-state back to reverb (state 0),
            // and clear BPM tap history so the old taps don't skew the next tap-average.
            fader3AltState = 0;
            altModes[3] = false;
            var altBtn3 = document.querySelector('.alt-btn[data-id="3"]');
            if (altBtn3) {
                altBtn3.classList.remove("active");
                faderColumns[3].classList.remove("alt-mode");
                faderColumns[3].classList.remove("motion-mode");
            }
            tapTimes = [];
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
            // Per-fader level meters: tanh-compress so brief high peaks don't
            // slam the bar to 100% and stay there. Fader 0 (OSC) + 1 (piano)
            // show their source; 2/3/4 show master peak as a rough activity cue.
            var padN = Math.tanh((msg.pad || 0) * 1.0);
            var pianoN = Math.tanh((msg.piano || 0) * 1.0);
            var masterN = Math.tanh((msg.peak || 0) * 0.7);
            var tracks = document.querySelectorAll(".fader-column .fader-track");
            if (tracks[0]) tracks[0].style.setProperty("--level", padN.toFixed(3));
            if (tracks[1]) tracks[1].style.setProperty("--level", pianoN.toFixed(3));
            if (tracks[2]) tracks[2].style.setProperty("--level", masterN.toFixed(3));
            if (tracks[3]) tracks[3].style.setProperty("--level", masterN.toFixed(3));
            if (tracks[4]) tracks[4].style.setProperty("--level", masterN.toFixed(3));
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
            // CPU ring on STOP button — only update when cpu_percent is in the message
            if (panicBtn && typeof msg.cpu_percent === "number") {
                panicBtn.classList.remove("cpu-warn", "cpu-hot", "cpu-crit");
                var cpu = msg.cpu_percent;
                if (cpu > 90) panicBtn.classList.add("cpu-crit");
                else if (cpu > 75) panicBtn.classList.add("cpu-hot");
                else if (cpu > 50) panicBtn.classList.add("cpu-warn");
            }
            // Numeric readout on Global tab
            var sysCpu = document.getElementById("sys-cpu");
            var sysRam = document.getElementById("sys-ram");
            if (sysCpu && typeof msg.cpu_percent === "number") {
                sysCpu.textContent = msg.cpu_percent.toFixed(1);
                sysCpu.classList.remove("warn", "crit");
                if (msg.cpu_percent > 90) sysCpu.classList.add("crit");
                else if (msg.cpu_percent > 70) sysCpu.classList.add("warn");
            }
            if (sysRam && typeof msg.ram_mb === "number") {
                sysRam.textContent = msg.ram_mb.toFixed(0);
            }
        } else if (msg.type === "bus_comp_gr") {
            // Beat-rate GR push (20 Hz) for the LED flash
            if (typeof msg.gr_db === "number") updateBusCompGr(msg.gr_db);
        } else if (msg.type === "bus_comp_preset_ack") {
            // Refresh all bus_comp sliders/knobs from state after preset load
            if (state) {
                for (var k in msg.values) {
                    state.master[k] = msg.values[k];
                }
            }
            updateSettingsSliders();
            // Belt-and-suspenders: directly set the enable checkbox + source dropdown
            // from the preset payload so there's no chance updateSettingsSliders misses them.
            if (msg.values) {
                if (typeof msg.values.bus_comp_enabled === "boolean") {
                    var enCb = document.querySelector('.setting-checkbox[data-param="bus_comp_enabled"]');
                    if (enCb) enCb.checked = msg.values.bus_comp_enabled;
                }
                if (typeof msg.values.bus_comp_source === "string") {
                    var srcSel = document.querySelector('.setting-select[data-param="bus_comp_source"]');
                    if (srcSel) srcSel.value = msg.values.bus_comp_source;
                }
                if (typeof msg.values.bus_comp_fx_bypass === "boolean") {
                    var bypCb = document.querySelector('.setting-checkbox[data-param="bus_comp_fx_bypass"]');
                    if (bypCb) bypCb.checked = msg.values.bus_comp_fx_bypass;
                }
                if (typeof msg.values.bus_comp_release_auto === "boolean") {
                    var arCb = document.querySelector('.setting-checkbox[data-param="bus_comp_release_auto"]');
                    if (arCb) arCb.checked = msg.values.bus_comp_release_auto;
                }
            }
            // Pin the preset dropdown to the active preset name
            if (busCompPresetSelect) busCompPresetSelect.value = msg.name;
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
        } else if (msg.type === "record_ack") {
            setRecordingState(!!msg.recording);
            // Refresh takes list after a recording finishes so it appears
            if (!msg.recording) send({ type: "list_recordings" });
        } else if (msg.type === "recordings_list") {
            renderTakes(msg.takes || []);
        } else if (msg.type === "recording_deleted") {
            renderTakes(msg.takes || []);
        } else if (msg.type === "pad_slots") {
            renderPadSlots(msg.slots || []);
        } else if (msg.type === "pad_slot_saved" || msg.type === "pad_slot_cleared") {
            renderPadSlots(msg.slots || []);
        } else if (msg.type === "recall_params_ack") {
            // Backend broadcasts a state message too — that'll re-sync sliders.
        } else if (msg.type === "drone_key_ack") {
            padActiveNote = (msg.enabled && typeof msg.note === "number") ? msg.note : null;
            if (typeof updatePadKeyVisuals === "function") updatePadKeyVisuals();
        } else if (msg.type === "drone_fade_ack") {
            if (padFadeBtn) padFadeBtn.classList.toggle("fading-out", !!msg.faded_out);
        } else if (msg.type === "macro_assign_ack") {
            if (state && state.macros && state.macros[msg.idx]) {
                state.macros[msg.idx].assignments = msg.assignments || [];
            }
            applyMacroVisuals();
        } else if (msg.type === "macro_value_ack") {
            if (state && state.macros && state.macros[msg.idx]) {
                state.macros[msg.idx].value = msg.value;
            }
            // Refresh sliders because assigned params just changed on the backend
            updateSettingsSliders();
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
            // FX fader: reverb mix (default) / shimmer vol (alt 1) / motion bus (alt 2)
            faderValues[3] = s.synth_pad.reverb_dry_wet ?? 0.65;
            altFaderValues[3] = s.synth_pad.shimmer_mix ?? 0.5;
            fader3MotionValue = s.synth_pad.motion_mix ?? 1.0;
            shimmerEnabled = s.synth_pad.shimmer_enabled ?? false;
            shimmerHigh = s.synth_pad.shimmer_high ?? false;
            freezeEnabled = s.synth_pad.freeze_enabled ?? false;
            droneEnabled = s.synth_pad.drone_enabled ?? false;
            // Pad player: reflect saved drone_key + volume
            padActiveNote = droneEnabled ? (s.synth_pad.drone_key ?? null) : null;
            if (typeof updatePadKeyVisuals === "function") updatePadKeyVisuals();
            if (padVolSlider) {
                var dl = s.synth_pad.drone_level;
                if (typeof dl === "number") padVolSlider.value = Math.round(dl * 100);
            }
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
            updateMirrorIndicators();
            // Sync OSC octave displays — state persists but UI var was init-only
            osc1Octave = s.synth_pad.osc1_octave ?? 0;
            osc2Octave = s.synth_pad.osc2_octave ?? 0;
            if (typeof updatePadOctaveDisplay === "function") updatePadOctaveDisplay();
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
            // Sync fader1 COMP-mode value from piano.comp_wet so reload / state
            // broadcast (e.g. after PERFECT preset) updates the fader visual.
            fader1CompValue = s.piano.comp_wet ?? 1.0;
            pianoEnabled = s.piano.enabled !== false;
            pianoBtn.classList.toggle("active", pianoEnabled);
            pianoBtn.classList.toggle("off", !pianoEnabled);
        }
        if (s.master) {
            faderValues[4] = s.master.volume ?? 0.85;
            // Master ALT → DRIVE: map pre_limiter_trim (0.5-3.0) back to 0-1
            var trim = s.master.pre_limiter_trim ?? 2.0;
            altFaderValues[4] = Math.max(0, Math.min(1, (trim - 0.5) / 2.5));
            // BPM
            bpm = Math.round(s.master.bpm ?? 120);
            if (bpmVal) bpmVal.textContent = bpm;
            transposeValue = s.master.transpose_semitones ?? 0;
            // Sync piano octave display — state persists but UI var was init-only
            pianoOctave = s.master.piano_octave ?? 0;
            var pianoOctDisp = document.querySelector('.oct-display[data-inst="piano"]');
            if (pianoOctDisp) pianoOctDisp.textContent = pianoOctave;
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
        if (typeof applyMacroVisuals === "function") applyMacroVisuals();
    }

    // ═══ Fader Logic ═══
    function getMultiAltValue(id) {
        if (id === 1) {
            if (fader1AltState === 0) return faderValues[1];
            if (fader1AltState === 1) return altFaderValues[1];
            return fader1CompValue;
        }
        if (id === 3) {
            if (fader3AltState === 0) return faderValues[3];
            if (fader3AltState === 1) return altFaderValues[3];
            return fader3MotionValue;
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
        } else if (id === 3) {
            label.textContent = FADER_LABELS[3][fader3AltState];
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
        } else if (id === 4 && altModes[4]) {
            // Master ALT: DRIVE (pre-limiter trim), fader 0..1 → 0.5..3.0x
            var trim = 0.5 + value * 2.5;
            valueEl.textContent = trim.toFixed(2) + "x";
        } else {
            valueEl.textContent = Math.round(value * 100) + "%";
        }

        // Warmth glow on master fader when DRIVE (ALT) is engaged
        if (id === 4) {
            if (altModes[4]) {
                applyWarmthToMasterFader(0.5 + value * 2.5);
            } else {
                clearMasterFaderWarmth();
            }
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
        } else if (id === 3) {
            if (fader3AltState === 0) faderValues[3] = normalizedValue;
            else if (fader3AltState === 1) altFaderValues[3] = normalizedValue;
            else fader3MotionValue = normalizedValue;
            altVal = fader3AltState;
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

        // Mirror master-ALT (DRIVE) back into the menu knob + slider
        if (id === 4 && altModes[4]) {
            var trim = 0.5 + normalizedValue * 2.5;
            var driveSlider = document.querySelector('[data-param="pre_limiter_trim"]');
            if (driveSlider) {
                driveSlider.value = trim * 100;  // slider range is 50..300
                var valEl = driveSlider.parentElement.querySelector(".setting-value");
                if (valEl) valEl.textContent = trim.toFixed(2) + "x";
                var knob = driveSlider.closest(".knob");
                if (knob) {
                    updateKnobRotation(knob);
                    applyWarmthToKnob(knob, trim);
                }
            }
        }

        // Mirror piano ALT=COMP (MIX) back into the menu knob + slider so
        // the piano settings panel stays in sync when user rides the fader.
        if (id === 1 && fader1AltState === 2) {
            var compSlider = document.querySelector('[data-param="comp_wet"]');
            if (compSlider) {
                compSlider.value = normalizedValue * 100;
                var cValEl = compSlider.parentElement.querySelector(".setting-value");
                if (cValEl) cValEl.textContent = Math.round(normalizedValue * 100) + "%";
                var cKnob = compSlider.closest(".knob");
                if (cKnob) updateKnobRotation(cKnob);
            }
        }

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
            } else if (id === 3) {
                // FX fader cycles: Reverb(0) → Shimmer(1) → Motion(2) → Reverb(0)
                fader3AltState = (fader3AltState + 1) % 3;
                altModes[id] = fader3AltState > 0;
                btn.classList.toggle("active", altModes[id]);
                faderColumns[id].classList.toggle("alt-mode", altModes[id]);
                faderColumns[id].classList.toggle("motion-mode", fader3AltState === 2);
                updateFader(id);
            } else {
                // Simple toggle (OSC1/2, master)
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

    // Presets: 2 layers of 5 slots each (L1=0-4, L2=5-9). Macros live in their
    // own always-visible row below (not a layer).
    var currentLayer = 0;  // 0=L1, 1=L2
    var allPresetSaved = [];
    for (var _i = 0; _i < 10; _i++) allPresetSaved.push(false);
    presetLabels = [];
    for (var _j = 0; _j < 10; _j++) presetLabels.push("");

    var macroRow = document.getElementById("macro-row");
    var macroSlots = macroRow ? macroRow.querySelectorAll(".macro-slot") : [];
    var macroLayerBtn = document.getElementById("macro-layer-btn");
    var macroLayer = 0;  // 0 = M1-M4, 1 = M5-M8

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
        currentLayer = (currentLayer + 1) % 2;  // L1 ↔ L2
        applyLayer();
    });

    // ═══ Macros ═══
    // State.macros is kept server-side; we mirror it for learn-mode + UI counts.
    var macroLearnIdx = -1;  // which macro, if any, is currently in "learn" mode

    function getMacros() {
        return (state && state.macros) ? state.macros : [];
    }

    function applyMacroVisuals() {
        var macros = getMacros();
        // Map visible slot -> absolute macro index (0-3 or 4-7)
        macroSlots.forEach(function (slot, pos) {
            var idx = pos + macroLayer * 4;
            slot.dataset.macro = idx;
            var m = macros[idx];
            if (!m) return;
            slot.style.setProperty("--macro-value", (m.value || 0).toFixed(3));
            var cnt = slot.querySelector(".macro-count");
            if (cnt) cnt.textContent = (m.assignments || []).length;
            var lbl = slot.querySelector(".macro-label");
            if (lbl) lbl.textContent = "MACRO " + (idx + 1);
            slot.classList.toggle("learn", idx === macroLearnIdx);
        });
        document.body.classList.toggle("macro-learning", macroLearnIdx >= 0);
        if (macroLayerBtn) {
            macroLayerBtn.textContent = macroLayer === 0 ? "A" : "B";
            macroLayerBtn.classList.toggle("active", macroLayer > 0);
        }
    }

    if (macroLayerBtn) {
        macroLayerBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            macroLayer = (macroLayer + 1) % 2;
            applyMacroVisuals();
        });
    }

    // Macro EDIT button — toggles macros-editing mode where tapping the count
    // badge on a macro clears that macro's assignments.
    var macroEditBtn = document.getElementById("macro-edit-btn");
    var macroEditMode = false;
    if (macroEditBtn) {
        macroEditBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            macroEditMode = !macroEditMode;
            macroEditBtn.classList.toggle("active", macroEditMode);
            document.body.classList.toggle("macros-editing", macroEditMode);
            // Exit learn mode if entering edit mode (avoid conflicting state)
            if (macroEditMode && macroLearnIdx >= 0) setMacroLearn(macroLearnIdx);
        });
    }
    // Click on a count badge in editing mode → clear that macro
    macroSlots.forEach(function (slot) {
        var cnt = slot.querySelector(".macro-count");
        if (!cnt) return;
        cnt.addEventListener("click", function (e) {
            if (!macroEditMode) return;
            e.stopPropagation();
            var idx = parseInt(slot.dataset.macro || "0", 10);
            send({ type: "macro_assign", idx: idx, action: "clear" });
        });
    });

    function setMacroLearn(idx) {
        macroLearnIdx = (macroLearnIdx === idx) ? -1 : idx;
        applyMacroVisuals();
    }

    function sendMacroValue(idx, value) {
        send({ type: "macro_value", idx: idx, value: value });
    }

    // Pointer drag on macro slot → value 0..1 based on horizontal position.
    // idx is read from dataset.macro at EVENT TIME so it tracks macro-layer changes.
    macroSlots.forEach(function (slot) {
        var dragging = false;
        var pressStart = 0;
        var moved = false;

        function currentIdx() {
            return parseInt(slot.dataset.macro || "0", 10);
        }
        function setValueFromPointer(e) {
            var rect = slot.getBoundingClientRect();
            var x = (e.clientX !== undefined ? e.clientX : (e.touches ? e.touches[0].clientX : 0)) - rect.left;
            var v = Math.max(0, Math.min(1, x / rect.width));
            slot.style.setProperty("--macro-value", v.toFixed(3));
            sendMacroValue(currentIdx(), v);
        }

        slot.addEventListener("pointerdown", function (e) {
            dragging = true;
            moved = false;
            pressStart = Date.now();
            slot.setPointerCapture(e.pointerId);
            e.preventDefault();
        });
        slot.addEventListener("pointermove", function (e) {
            if (!dragging) return;
            var rect = slot.getBoundingClientRect();
            var x = e.clientX - rect.left;
            if (!moved) {
                var startX = parseFloat(slot.style.getPropertyValue("--macro-value") || "0") * rect.width;
                if (Math.abs(x - startX) > 4) moved = true;
            }
            if (moved) setValueFromPointer(e);
        });
        slot.addEventListener("pointerup", function (e) {
            if (!dragging) return;
            dragging = false;
            slot.releasePointerCapture(e.pointerId);
            var held = Date.now() - pressStart;
            if (!moved && held < 300) setMacroLearn(currentIdx());
        });
        slot.addEventListener("pointercancel", function () { dragging = false; });
    });

    // Click-to-assign: while a macro is in learn mode, the CSS disables
    // pointer-events on the inputs (so no accidental value change) and the
    // click lands on .setting-row. This handler reads the child input's
    // section/param + CURRENT VALUE (captured as the macro's max target).
    document.addEventListener("click", function (e) {
        if (macroLearnIdx < 0) return;
        var row = e.target.closest(".setting-row, .setting-row-checkbox");
        if (!row) return;
        // Find the first assignable control in this row.
        var ctl = row.querySelector(
            "[data-section][data-param].setting-slider, " +
            "[data-section][data-param].setting-checkbox, " +
            "[data-section][data-param].setting-select"
        );
        if (!ctl) return;
        e.preventDefault();
        e.stopPropagation();
        var section = ctl.dataset.section;
        var param = ctl.dataset.param;
        var is_bool = ctl.type === "checkbox";
        var mn = 0.0, mx = 1.0;
        if (is_bool) {
            // Boolean: macro >= 0.5 maps to true, < 0.5 to false
            mn = 0.0; mx = 1.0;
        } else {
            // Relative mapping: macro 0 → slider.min; macro 1 → CURRENT value
            // at capture time. Moving macro can only reduce param from its
            // current setting, not push it beyond where you had it.
            mn = parseFloat(ctl.min || "0");
            mx = parseFloat(ctl.value);
            if (!isFinite(mx)) mx = parseFloat(ctl.max || "1");
        }
        send({
            type: "macro_assign",
            idx: macroLearnIdx,
            action: "toggle",
            section: section,
            param: param,
            min: mn, max: mx,
            is_bool: is_bool,
        });
    }, true);

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

    var LABEL_MAX = 16;
    var LABEL_DICTIONARY = [
        "Song 1", "Song 2", "Song 3", "Song 4", "Song 5",
        "Intro", "Verse", "Chorus", "Bridge", "Outro", "Prayer", "Sermon",
        "Warm", "Bright", "Dark", "Airy", "Wide", "Deep", "Soft", "Big", "Tight",
        "Pad", "Piano", "Keys", "Lead", "Bass", "Strings", "Organ", "Bells",
        "Choir", "Shimmer"
    ];
    var labelInputBuf = "";
    var labelInputTextEl = document.getElementById("label-input-text");
    var labelSuggestEl = document.getElementById("label-suggestions");

    function showLabelPicker(slot) {
        labelPickerSlot = slot;
        labelInputBuf = presetLabels[slot] || "";
        renderLabelInput();
        labelPickerModal.classList.remove("hidden");
    }

    function hideLabelPicker() {
        labelPickerModal.classList.add("hidden");
        labelPickerSlot = -1;
    }

    function renderLabelInput() {
        labelInputTextEl.textContent = labelInputBuf;
        var q = labelInputBuf.trim().toLowerCase();
        var matches = [];
        if (q.length === 0) {
            matches = LABEL_DICTIONARY.slice(0, 10);
        } else {
            for (var i = 0; i < LABEL_DICTIONARY.length; i++) {
                if (LABEL_DICTIONARY[i].toLowerCase().indexOf(q) === 0) matches.push(LABEL_DICTIONARY[i]);
            }
            for (var j = 0; j < LABEL_DICTIONARY.length && matches.length < 10; j++) {
                var w = LABEL_DICTIONARY[j];
                if (matches.indexOf(w) === -1 && w.toLowerCase().indexOf(q) > 0) matches.push(w);
            }
        }
        labelSuggestEl.innerHTML = "";
        matches.forEach(function (word) {
            var b = document.createElement("button");
            b.className = "label-suggest";
            b.textContent = word;
            b.addEventListener("click", function (e) {
                e.stopPropagation();
                labelInputBuf = word.slice(0, LABEL_MAX);
                renderLabelInput();
            });
            labelSuggestEl.appendChild(b);
        });
    }

    function labelKeyPress(ch) {
        if (labelInputBuf.length >= LABEL_MAX) return;
        // First letter of a word → uppercase; mid-word → lowercase (except digits/space)
        if (/[A-Z]/.test(ch)) {
            var atWordStart = labelInputBuf.length === 0 ||
                              labelInputBuf.charAt(labelInputBuf.length - 1) === " ";
            ch = atWordStart ? ch : ch.toLowerCase();
        }
        labelInputBuf += ch;
        renderLabelInput();
    }

    document.getElementById("label-picker-close").addEventListener("click", function (e) {
        e.stopPropagation();
        hideLabelPicker();
    });
    labelPickerModal.addEventListener("click", function (e) {
        if (e.target === labelPickerModal) hideLabelPicker();
    });
    document.querySelectorAll("#label-keyboard .kb-key").forEach(function (key) {
        key.addEventListener("click", function (e) {
            e.stopPropagation();
            var action = key.dataset.action;
            if (action === "backspace") {
                labelInputBuf = labelInputBuf.slice(0, -1);
                renderLabelInput();
            } else if (action === "clear") {
                labelInputBuf = "";
                renderLabelInput();
            } else if (action === "done") {
                var slot = labelPickerSlot;
                var label = labelInputBuf.trim();
                hideLabelPicker();
                if (slot >= 0) send({ type: "preset_label", slot: slot, label: label });
            } else if (key.dataset.key) {
                labelKeyPress(key.dataset.key);
            }
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
    // ═══ Bus compressor preset + GR LED ═══
    var busCompPresetSelect = document.getElementById("bus-comp-preset-select");
    var busCompGrLed = document.getElementById("bus-comp-gr-led");
    var busCompGrLabel = document.getElementById("bus-comp-gr-label");
    var grLedPeakDb = 0.0;  // peak-hold value (fast attack, slow decay)
    if (busCompPresetSelect) {
        busCompPresetSelect.addEventListener("change", function () {
            var name = busCompPresetSelect.value;
            if (name && name !== "custom") {
                send({ type: "bus_comp_preset", name: name });
            }
        });
    }
    // Flip preset dropdown to "custom" when the user tweaks any bus_comp control
    function markBusCompCustom() {
        if (busCompPresetSelect && busCompPresetSelect.value !== "custom") {
            busCompPresetSelect.value = "custom";
        }
    }
    // LED color stops by dB of reduction: dim green → bright green → amber → orange → red
    function grLedColor(db) {
        var stops = [
            { t: 0.0,  c: [0, 48, 24],    g: 0  },   // dim (no GR)
            { t: 1.0,  c: [0, 191, 99],   g: 4  },   // green just lighting up
            { t: 4.0,  c: [160, 224, 96], g: 8  },   // green/yellow — glue territory
            { t: 8.0,  c: [232, 180, 80], g: 12 },   // amber — working
            { t: 14.0, c: [232, 144, 64], g: 16 },   // orange — heavy
            { t: 20.0, c: [216, 80, 48],  g: 22 },   // red — pumping hard
        ];
        if (db <= stops[0].t) return { color: "rgb(" + stops[0].c.join(",") + ")", glow: 0 };
        for (var i = 1; i < stops.length; i++) {
            if (db <= stops[i].t) {
                var a = stops[i - 1], b = stops[i];
                var f = (db - a.t) / (b.t - a.t);
                var r = Math.round(a.c[0] + (b.c[0] - a.c[0]) * f);
                var g = Math.round(a.c[1] + (b.c[1] - a.c[1]) * f);
                var bch = Math.round(a.c[2] + (b.c[2] - a.c[2]) * f);
                var glow = a.g + (b.g - a.g) * f;
                return { color: "rgb(" + r + "," + g + "," + bch + ")", glow: glow };
            }
        }
        var last = stops[stops.length - 1];
        return { color: "rgb(" + last.c.join(",") + ")", glow: last.g };
    }

    // Separate peak-hold for the numeric readout so digits don't flicker.
    // LED uses fast-attack/slow-decay; number uses ~1s peak hold before bleed-off.
    var grLabelPeakDb = 0.0;
    var grLabelHoldTicks = 0;
    function updateBusCompGr(grDb) {
        if (!busCompGrLed) return;
        // LED: fast-attack / gentle decay for beat-pulse readability
        if (grDb > grLedPeakDb) grLedPeakDb = grDb;
        else grLedPeakDb = Math.max(grDb, grLedPeakDb - 0.6);
        var w = grLedColor(grLedPeakDb);
        busCompGrLed.style.background = w.color;
        busCompGrLed.style.boxShadow = w.glow > 0 ? "0 0 " + w.glow + "px " + w.color : "none";
        busCompGrLed.style.borderColor = w.color;
        // Label peak-hold: only grows on a new peak, hold ~1s, then decay
        if (grDb > grLabelPeakDb) {
            grLabelPeakDb = grDb;
            grLabelHoldTicks = 20;  // 20 ticks × 50ms = 1s hold
        } else if (grLabelHoldTicks > 0) {
            grLabelHoldTicks--;
        } else {
            grLabelPeakDb = Math.max(grDb, grLabelPeakDb - 0.3);
        }
        if (busCompGrLabel) busCompGrLabel.textContent = grLabelPeakDb >= 0.05
            ? "-" + grLabelPeakDb.toFixed(1)
            : "0.0";
    }

    // ═══ Tempo (BPM + TAP) ═══
    function setBpm(newBpm, sendUpdate) {
        bpm = Math.max(40, Math.min(300, Math.round(newBpm)));
        if (bpmVal) bpmVal.textContent = bpm;
        if (sendUpdate !== false) {
            send({ type: "setting", section: "master", param: "bpm", value: bpm });
        }
    }

    function registerTap() {
        var now = performance.now();
        if (tapTimes.length && (now - tapTimes[tapTimes.length - 1]) > 2000) {
            tapTimes = [];
        }
        tapTimes.push(now);
        if (tapTimes.length > 6) tapTimes.shift();
        if (tapTimes.length >= 2) {
            var avgInterval = (tapTimes[tapTimes.length - 1] - tapTimes[0]) / (tapTimes.length - 1);
            setBpm(60000 / avgInterval);
        }
    }

    // BPM number is both tap target (short press, no drag) AND drag-to-adjust.
    if (bpmVal) {
        var bpmDragStartY = 0, bpmDragStartVal = 120, bpmDragging = false;
        var bpmPressTime = 0, bpmMoved = false;
        bpmVal.addEventListener("pointerdown", function (e) {
            e.preventDefault();
            bpmDragging = true;
            bpmMoved = false;
            bpmPressTime = performance.now();
            bpmDragStartY = e.clientY;
            bpmDragStartVal = bpm;
            try { bpmVal.setPointerCapture(e.pointerId); } catch (err) {}
        });
        bpmVal.addEventListener("pointermove", function (e) {
            if (!bpmDragging) return;
            var dy = bpmDragStartY - e.clientY;
            if (Math.abs(dy) > 3) bpmMoved = true;
            if (bpmMoved) setBpm(bpmDragStartVal + Math.round(dy / 3));
        });
        function bpmEndDrag(e) {
            if (!bpmDragging) return;
            bpmDragging = false;
            try { bpmVal.releasePointerCapture(e.pointerId); } catch (err) {}
            var held = performance.now() - bpmPressTime;
            if (!bpmMoved && held < 350) {
                // Treated as a tap — contributes to BPM average
                registerTap();
            }
        }
        bpmVal.addEventListener("pointerup", bpmEndDrag);
        bpmVal.addEventListener("pointercancel", bpmEndDrag);
    }

    if (droneBtn) {
        droneBtn.addEventListener("click", function () {
            droneEnabled = !droneEnabled;
            updateDroneDisplay();
            send({ type: "drone_toggle", enabled: droneEnabled });
        });
    }

    function updateDroneDisplay() {
        if (droneBtn) droneBtn.classList.toggle("active", droneEnabled);
    }

    // ═══ Pad Player (12-key drone launcher) ═══
    var padKeys = document.querySelectorAll(".pad-key");
    var padVolSlider = document.getElementById("pad-vol");
    var padFadeBtn = document.getElementById("pad-fade-btn");
    var padActiveNote = null;  // currently highlighted key

    function updatePadKeyVisuals() {
        padKeys.forEach(function (btn) {
            var note = parseInt(btn.dataset.note, 10);
            btn.classList.toggle("active", note === padActiveNote);
        });
    }

    padKeys.forEach(function (btn) {
        btn.addEventListener("click", function () {
            var note = parseInt(btn.dataset.note, 10);
            // Optimistic UI — backend will echo the true state in drone_key_ack
            if (padActiveNote === note) {
                padActiveNote = null;
            } else {
                padActiveNote = note;
            }
            updatePadKeyVisuals();
            send({ type: "drone_key", note: note });
        });
    });

    if (padVolSlider) {
        padVolSlider.addEventListener("input", function () {
            var v = parseFloat(padVolSlider.value) / 100.0;
            send({ type: "setting", section: "synth_pad", param: "drone_level", value: v });
        });
    }

    if (padFadeBtn) {
        padFadeBtn.addEventListener("click", function () {
            padFadeBtn.classList.toggle("fading-out");
            send({ type: "drone_fade" });
        });
    }

    // ═══ Recorder (record button + takes list) ═══
    var recordBtn = document.getElementById("record-btn");
    var recTimeEl = document.getElementById("rec-time");
    var takesListEl = document.getElementById("takes-list");
    var recording = false;
    var recStartMs = 0;
    var recTimer = null;

    function fmtDur(sec) {
        sec = Math.max(0, Math.floor(sec));
        var m = Math.floor(sec / 60);
        var s = sec % 60;
        return m + ":" + (s < 10 ? "0" + s : s);
    }

    function setRecordingState(on) {
        recording = !!on;
        var recBox = document.getElementById("record-box");
        if (recBox) recBox.classList.toggle("recording", recording);
        if (recording) {
            recStartMs = performance.now();
            if (recTimer) clearInterval(recTimer);
            recTimer = setInterval(function () {
                if (!recording) return;
                var elapsed = (performance.now() - recStartMs) / 1000;
                if (recTimeEl) recTimeEl.textContent = fmtDur(elapsed);
            }, 500);
        } else {
            if (recTimer) clearInterval(recTimer);
            recTimer = null;
            if (recTimeEl) recTimeEl.textContent = "";
        }
    }

    if (recordBtn) {
        recordBtn.addEventListener("click", function () {
            send({ type: "record_toggle" });
        });
    }

    var PAD_NOTES = [
        { note: 60, label: "C" }, { note: 61, label: "C#" }, { note: 62, label: "D" },
        { note: 63, label: "D#" }, { note: 64, label: "E" }, { note: 65, label: "F" },
        { note: 66, label: "F#" }, { note: 67, label: "G" }, { note: 68, label: "G#" },
        { note: 69, label: "A" }, { note: 70, label: "A#" }, { note: 71, label: "B" },
    ];

    function renderTakes(takes) {
        if (!takesListEl) return;
        takesListEl.innerHTML = "";
        if (!takes || takes.length === 0) {
            var empty = document.createElement("div");
            empty.className = "takes-empty";
            empty.textContent = "No recordings yet. Tap ● on the top bar to start one.";
            takesListEl.appendChild(empty);
            return;
        }
        takes.forEach(function (t) {
            var row = document.createElement("div");
            row.className = "take-row";

            var name = document.createElement("span");
            name.className = "take-name";
            name.textContent = t.filename.replace(/\.wav$/, "");
            row.appendChild(name);

            var meta = document.createElement("span");
            meta.className = "take-meta";
            meta.textContent = fmtDur(t.duration_seconds);
            row.appendChild(meta);

            var audio = document.createElement("audio");
            audio.controls = true;
            audio.preload = "none";
            audio.src = t.url;
            row.appendChild(audio);

            // Pad-target dropdown + SEND button
            var sel = document.createElement("select");
            sel.className = "pad-target";
            sel.title = "Pad slot to copy into";
            PAD_NOTES.forEach(function (n) {
                var opt = document.createElement("option");
                opt.value = n.note;
                opt.textContent = n.label;
                sel.appendChild(opt);
            });
            row.appendChild(sel);

            var send2pad = document.createElement("button");
            send2pad.className = "send-pad";
            send2pad.textContent = "→ PAD";
            send2pad.title = "Copy this recording into the selected pad slot";
            send2pad.addEventListener("click", function () {
                var note = parseInt(sel.value, 10);
                send({ type: "save_to_pad_slot", source: t.filename, note: note });
            });
            row.appendChild(send2pad);

            var recall = document.createElement("button");
            recall.className = "recall";
            recall.textContent = "⟲";
            recall.title = "Recall parameters from record-start";
            recall.disabled = !t.has_state;
            recall.addEventListener("click", function () {
                send({ type: "recall_recording_params", filename: t.filename });
            });
            row.appendChild(recall);

            var del = document.createElement("button");
            del.className = "delete";
            del.textContent = "✕";
            del.title = "Delete this take";
            del.addEventListener("click", function () {
                if (!confirm("Delete " + t.filename + "?")) return;
                send({ type: "delete_recording", filename: t.filename });
            });
            row.appendChild(del);

            takesListEl.appendChild(row);
        });
    }

    function renderPadSlots(slots) {
        var el = document.getElementById("pad-slots-list");
        if (!el) return;
        el.innerHTML = "";
        slots.forEach(function (s) {
            var slot = document.createElement("div");
            slot.className = "pad-slot" + (s.loaded ? " loaded" : "");
            var key = document.createElement("span");
            key.className = "slot-key";
            key.textContent = s.label;
            slot.appendChild(key);
            var dur = document.createElement("span");
            dur.className = "slot-dur";
            dur.textContent = s.loaded ? fmtDur(s.duration_seconds) : "—";
            slot.appendChild(dur);
            var clr = document.createElement("button");
            clr.className = "clear";
            clr.textContent = "✕";
            clr.title = "Remove this slot (reverts to live synth for this key)";
            clr.addEventListener("click", function () {
                if (!confirm("Clear pad slot " + s.label + "?")) return;
                send({ type: "clear_pad_slot", note: s.note });
            });
            slot.appendChild(clr);
            el.appendChild(slot);
        });
    }

    // Refresh takes + pad slots whenever the Record tab is opened.
    var recordTabBtn = document.querySelector('.settings-tab[data-tab="record"]');
    function refreshTakes() {
        send({ type: "list_recordings" });
        send({ type: "list_pad_slots" });
    }
    if (recordTabBtn) recordTabBtn.addEventListener("click", refreshTakes);
    // Initial populate once on load
    setTimeout(refreshTakes, 500);

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
        "bus_comp_sc_hpf_hz": true,
        "drone_cutoff_hz": true,
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
                param === "drive" || param === "drone_wash_mix" ||
                param === "drone_air_mix" || param === "drone_double_mix" ||
                param === "width" || param === "tone_tilt" ||
                param === "reverb_damp" || param === "reverb_shimmer_fb" ||
                param === "reverb_noise_mod" || param === "piano_reverb_send" ||
                param === "comp_wet" || param === "osc1_reverb_send" ||
                param === "osc2_reverb_send") {
                sendValue = value / 100;
                displayValue = Math.round(sendValue * 100) + "%";
                // MIX knob ↔ fader1-ALT-COMP live sync. Any time the menu
                // knob moves, reflect the new value into the front-panel
                // fader state so flipping to COMP mode shows the right bar.
                if (param === "comp_wet") {
                    fader1CompValue = sendValue;
                    if (fader1AltState === 2) updateFader(1);
                }
            } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms" ||
                param === "attack_ms" || param === "release_ms") {
                sendValue = value;
                displayValue = Math.round(value) + "ms";
            } else if (param === "comp_threshold_db" || param === "comp_makeup_db" ||
                       param === "comp_drive_db" || param === "comp_knee_db") {
                sendValue = value;
                var sign = value > 0 ? "+" : "";
                displayValue = sign + Math.round(value) + "dB";
            } else if (param === "comp_ratio") {
                sendValue = value;
                displayValue = value.toFixed(1) + ":1";
            } else if (param === "bus_comp_threshold_db" || param === "bus_comp_makeup_db") {
                sendValue = value;
                var bsgn = value > 0 ? "+" : "";
                displayValue = bsgn + value.toFixed(1) + "dB";
            } else if (param === "bus_comp_ratio") {
                // At the top of the slider (>= 25), lock to infinity (brick-wall limiter)
                if (value >= 25) {
                    sendValue = 1000;
                    displayValue = "∞";
                } else if (value >= 20) {
                    sendValue = value;
                    displayValue = Math.round(value) + ":1";
                } else {
                    sendValue = value;
                    displayValue = value.toFixed(1) + ":1";
                }
            } else if (param === "bus_comp_attack_ms") {
                sendValue = value;
                displayValue = value.toFixed(1) + "ms";
            } else if (param === "bus_comp_release_ms") {
                sendValue = value;
                displayValue = Math.round(value) + "ms";
            } else if (param === "bus_comp_mix") {
                sendValue = value / 100;
                displayValue = Math.round(value) + "%";
            } else if (param === "reverb_wet_gain") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(1) + "x";
            } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                sendValue = value / 100;
                displayValue = sendValue.toFixed(2) + "x";
            } else if (param === "lfo_rate_hz") {
                // slider 5..2000 → 0.05..20 Hz (log-ish via linear /100)
                sendValue = value / 100;
                displayValue = sendValue.toFixed(2) + "Hz";
            } else if (param === "lfo_depth" || param === "lfo_spread" ||
                param === "delay_wet" || param === "delay_feedback") {
                sendValue = value / 100;
                displayValue = Math.round(value) + "%";
            } else if (param === "delay_time_ms") {
                sendValue = value;
                displayValue = Math.round(value) + "ms";
            } else if (param === "delay_offset_ms") {
                sendValue = value;
                var s2 = value > 0 ? "+" : "";
                displayValue = s2 + Math.round(value) + "ms";
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

            // MIRROR: when OSC level-mirror is on, also mirror per-OSC reverb sends
            if (linkOscLevels && (param === "osc1_reverb_send" || param === "osc2_reverb_send")) {
                var twin = param === "osc1_reverb_send" ? "osc2_reverb_send" : "osc1_reverb_send";
                var twinSlider = document.querySelector('.setting-slider[data-param="' + twin + '"]');
                if (twinSlider && twinSlider.value !== slider.value) {
                    twinSlider.value = slider.value;
                    var twinValEl = twinSlider.parentElement.querySelector(".setting-value");
                    if (twinValEl) twinValEl.textContent = displayValue;
                    send({ type: "setting", section: "synth_pad", param: twin, value: sendValue });
                }
            }

            if (param && param.indexOf && param.indexOf("adsr.") === 0) {
                updateAdsrCurve();
            }
            if (param && param.indexOf && param.indexOf("bus_comp_") === 0) {
                markBusCompCustom();
            }
            if (param === "pre_limiter_trim") {
                updateWarmthClass(slider, sendValue);
                // Mirror to master-ALT fader so both controls stay in sync
                altFaderValues[4] = Math.max(0, Math.min(1, (sendValue - 0.5) / 2.5));
                updateFader(4);
            }
        });
    });

    // Continuous color interpolation for drive/warmth.
    // Input: trim value 0.5..3.0. Output: {color: "rgb(r,g,b)", glow: px}.
    // Stops: green until 1.0, then blend through amber → orange → red.
    function warmFromTrim(trim) {
        var stops = [
            { t: 1.0, c: [0, 191, 99],  g: 0 },
            { t: 1.4, c: [224, 192, 96], g: 8 },
            { t: 1.8, c: [232, 144, 64], g: 14 },
            { t: 2.4, c: [216, 80, 48],  g: 18 },
            { t: 3.0, c: [216, 64, 48],  g: 22 },
        ];
        if (trim <= stops[0].t) return { color: "rgb(" + stops[0].c.join(",") + ")", glow: 0 };
        for (var i = 1; i < stops.length; i++) {
            if (trim <= stops[i].t) {
                var a = stops[i - 1], b = stops[i];
                var f = (trim - a.t) / (b.t - a.t);
                var r = Math.round(a.c[0] + (b.c[0] - a.c[0]) * f);
                var g = Math.round(a.c[1] + (b.c[1] - a.c[1]) * f);
                var bch = Math.round(a.c[2] + (b.c[2] - a.c[2]) * f);
                var glow = a.g + (b.g - a.g) * f;
                return { color: "rgb(" + r + "," + g + "," + bch + ")", glow: glow };
            }
        }
        var last = stops[stops.length - 1];
        return { color: "rgb(" + last.c.join(",") + ")", glow: last.g };
    }

    function applyWarmthToKnob(knob, trim) {
        if (!knob) return;
        var w = warmFromTrim(trim);
        var dial = knob.querySelector(".knob-dial");
        var indicatorBar = knob.querySelector(".knob-indicator");
        if (dial) {
            dial.style.borderColor = w.color;
            dial.style.boxShadow = w.glow > 0 ? "0 0 " + w.glow + "px " + w.color : "none";
        }
        if (indicatorBar) {
            // The indicator's ::before is the visible bar; tint via CSS custom prop
            indicatorBar.style.setProperty("--warm-color", w.color);
        }
    }

    function applyWarmthToMasterFader(trim) {
        var col = faderColumns[4];
        if (!col) return;
        var fill = col.querySelector(".fader-fill");
        var track = col.querySelector(".fader-track");
        var label = col.querySelector(".fader-label");
        var w = warmFromTrim(trim);
        if (fill) {
            fill.style.background = w.color;
            fill.style.boxShadow = w.glow > 0 ? "0 0 " + w.glow + "px " + w.color : "none";
        }
        if (track) {
            track.style.borderColor = w.color;
        }
        if (label) {
            label.style.color = w.color;
        }
    }

    function clearMasterFaderWarmth() {
        var col = faderColumns[4];
        if (!col) return;
        var fill = col.querySelector(".fader-fill");
        var track = col.querySelector(".fader-track");
        var label = col.querySelector(".fader-label");
        if (fill) { fill.style.background = ""; fill.style.boxShadow = ""; }
        if (track) { track.style.borderColor = ""; }
        if (label) { label.style.color = ""; }
    }

    function updateWarmthClass(slider, value) {
        if (!slider) return;
        var knob = slider.closest(".knob");
        if (knob) applyWarmthToKnob(knob, value);
    }

    // MIRROR indicator pill on OSC 1 / OSC 2 tabs — reflects linkOscLevels
    function updateMirrorIndicators() {
        document.querySelectorAll(".mirror-pill").forEach(function (pill) {
            pill.classList.toggle("active", linkOscLevels);
            var txt = pill.querySelector(".mirror-text");
            if (txt) txt.textContent = linkOscLevels ? "MIRROR ON — linked to other OSC" : "MIRROR OFF";
        });
    }

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
            var p = checkbox.dataset.param;
            send({
                type: "setting",
                section: checkbox.dataset.section,
                param: p,
                value: checkbox.checked,
            });

            // MIRROR: when OSC level-mirror is on, also mirror per-OSC FX bypass
            if (p === "osc_levels_linked") {
                linkOscLevels = checkbox.checked;
                updateMirrorIndicators();
            }
            if (linkOscLevels && (p === "osc1_fx_bypass" || p === "osc2_fx_bypass")) {
                var twin = p === "osc1_fx_bypass" ? "osc2_fx_bypass" : "osc1_fx_bypass";
                var twinCb = document.querySelector('.setting-checkbox[data-param="' + twin + '"]');
                if (twinCb && twinCb.checked !== checkbox.checked) {
                    twinCb.checked = checkbox.checked;
                    send({ type: "setting", section: "synth_pad", param: twin, value: checkbox.checked });
                }
            }

            updateIndepFilterVisibility();
            if (p && p.indexOf("bus_comp_") === 0) markBusCompCustom();
        });
    });

    // Settings selects
    document.querySelectorAll(".setting-select").forEach(function (select) {
        select.addEventListener("change", function () {
            var p = select.dataset.param;
            send({
                type: "setting",
                section: select.dataset.section,
                param: p,
                value: select.value,
            });
            if (p && p.indexOf("bus_comp_") === 0) markBusCompCustom();
        });
    });

    // One-line description of each reverb type, shown to the right of the
    // reset button. Kept tight enough to fit on a single row at typical UI
    // widths; updated whenever the dropdown changes or state loads.
    var REVERB_TYPE_DESC = {
        wash:  "Long ambient wash — the default pad texture",
        hall:  "Wide diffuse concert hall — piano + pad glue",
        room:  "Tight bright room — intimate close-mic feel",
        plate: "Metallic 70s plate — cuts through on vocals",
        bloom: "Self-rising perfect fifths — ethereal worship swell",
        drone: "Chord-tone bass pedal — strongest at A2–A3",
        ghost: "Breathing modulated tail — subtle motion under chords",
    };
    function updateReverbTypeDesc() {
        var desc = document.getElementById("reverb-type-desc");
        var sel = document.querySelector('[data-param="reverb_type"]');
        if (!desc || !sel) return;
        desc.textContent = REVERB_TYPE_DESC[sel.value] || "";
    }

    // Reverb type ↻ reset — re-sends the currently selected type so the
    // backend re-applies the preset dict (damp/shimmer_fb/noise_mod back to
    // defaults). Browsers don't fire `change` when you re-click the same
    // option, so this button covers "reset my tweaks" intent.
    var reverbResetBtn = document.getElementById("reverb-type-reset");
    if (reverbResetBtn) {
        reverbResetBtn.addEventListener("click", function () {
            var sel = document.querySelector('[data-param="reverb_type"]');
            if (!sel) return;
            send({
                type: "setting",
                section: "synth_pad",
                param: "reverb_type",
                value: sel.value,
            });
        });
    }

    // Piano compressor "PERFECT" preset button — restores the LA-2A-style
    // musical defaults (threshold/ratio/attack/release/knee/makeup/wet all
    // in one action). Server broadcasts new state so every knob visually
    // snaps to the preset values.
    var pianoCompPerfect = document.getElementById("piano-comp-perfect");
    if (pianoCompPerfect) {
        pianoCompPerfect.addEventListener("click", function () {
            send({ type: "piano_comp_preset", preset: "perfect" });
        });
    }
    var reverbTypeSel = document.querySelector('[data-param="reverb_type"]');
    if (reverbTypeSel) {
        reverbTypeSel.addEventListener("change", updateReverbTypeDesc);
    }

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
                param === "drive" || param === "drone_wash_mix" ||
                param === "drone_air_mix" || param === "drone_double_mix" ||
                param === "width" || param === "tone_tilt" ||
                param === "reverb_damp" || param === "reverb_shimmer_fb" ||
                param === "reverb_noise_mod" || param === "piano_reverb_send" ||
                param === "comp_wet" || param === "osc1_reverb_send" ||
                param === "osc2_reverb_send") {
                slider.value = value * 100;
            } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms" ||
                param === "attack_ms" || param === "release_ms" ||
                param === "comp_threshold_db" || param === "comp_makeup_db" ||
                param === "comp_ratio" || param === "comp_drive_db" ||
                param === "comp_knee_db") {
                slider.value = value;
            } else if (param === "reverb_wet_gain") {
                slider.value = value * 100;
            } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                slider.value = value * 100;
            } else if (param === "lfo_rate_hz" || param === "lfo_depth" ||
                param === "lfo_spread" || param === "delay_wet" ||
                param === "delay_feedback") {
                slider.value = value * 100;
            } else if (param === "delay_time_ms" || param === "delay_offset_ms") {
                slider.value = value;
            } else if (param === "bus_comp_mix") {
                slider.value = value * 100;
            } else if (param === "bus_comp_threshold_db" || param === "bus_comp_makeup_db" ||
                param === "bus_comp_ratio" || param === "bus_comp_attack_ms" ||
                param === "bus_comp_release_ms") {
                slider.value = value;
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
                param === "drive" || param === "width" || param === "tone_tilt" ||
                param === "reverb_damp" || param === "reverb_shimmer_fb" ||
                param === "reverb_noise_mod" || param === "piano_reverb_send" ||
                param === "comp_wet" || param === "osc1_reverb_send" ||
                param === "osc2_reverb_send") {
                    valueEl.textContent = Math.round(value * 100) + "%";
                } else if (param === "reverb_predelay_ms" || param === "haas_delay_ms" ||
                    param === "attack_ms" || param === "release_ms") {
                    valueEl.textContent = Math.round(value) + "ms";
                } else if (param === "comp_threshold_db" || param === "comp_makeup_db" ||
                           param === "comp_drive_db" || param === "comp_knee_db") {
                    var sign2 = value > 0 ? "+" : "";
                    valueEl.textContent = sign2 + Math.round(value) + "dB";
                } else if (param === "comp_ratio") {
                    valueEl.textContent = value.toFixed(1) + ":1";
                } else if (param === "bus_comp_threshold_db" || param === "bus_comp_makeup_db") {
                    var s4 = value > 0 ? "+" : "";
                    valueEl.textContent = s4 + value.toFixed(1) + "dB";
                } else if (param === "bus_comp_ratio") {
                    if (value >= 100) valueEl.textContent = "∞";
                    else if (value >= 20) valueEl.textContent = Math.round(value) + ":1";
                    else valueEl.textContent = value.toFixed(1) + ":1";
                } else if (param === "bus_comp_attack_ms") {
                    valueEl.textContent = value.toFixed(1) + "ms";
                } else if (param === "bus_comp_release_ms") {
                    valueEl.textContent = Math.round(value) + "ms";
                } else if (param === "bus_comp_mix") {
                    valueEl.textContent = Math.round(value * 100) + "%";
                } else if (param === "reverb_wet_gain") {
                    valueEl.textContent = value.toFixed(1) + "x";
                } else if (param === "pre_limiter_trim" || param === "shimmer_send") {
                    valueEl.textContent = value.toFixed(2) + "x";
                } else if (param === "lfo_rate_hz") {
                    valueEl.textContent = value.toFixed(2) + "Hz";
                } else if (param === "lfo_depth" || param === "lfo_spread" ||
                    param === "delay_wet" || param === "delay_feedback") {
                    valueEl.textContent = Math.round(value * 100) + "%";
                } else if (param === "delay_time_ms") {
                    valueEl.textContent = Math.round(value) + "ms";
                } else if (param === "delay_offset_ms") {
                    var s3 = value > 0 ? "+" : "";
                    valueEl.textContent = s3 + Math.round(value) + "ms";
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

        // Sync the reverb type description after the select value updates.
        if (typeof updateReverbTypeDesc === "function") updateReverbTypeDesc();

        // Refresh pre-limiter warmth class from current value
        var warmSlider = document.querySelector('[data-param="pre_limiter_trim"]');
        if (warmSlider) {
            updateWarmthClass(warmSlider, parseFloat(warmSlider.value) / 100);
        }

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
        updateAdsrCurve();
    }

    // ═══ ADSR envelope curve ═══
    function getAdsrSliderVal(param) {
        var el = document.querySelector('[data-param="adsr.' + param + '"]');
        return el ? parseFloat(el.value) : 0;
    }

    function updateAdsrCurve() {
        var poly = document.getElementById("adsr-path");
        if (!poly) return;
        var a = Math.min(1, getAdsrSliderVal("attack_ms") / 1000);
        var d = Math.min(1, getAdsrSliderVal("decay_ms") / 2000);
        var s = Math.max(0, Math.min(1, getAdsrSliderVal("sustain_percent") / 100));
        var r = Math.min(1, getAdsrSliderVal("release_ms") / 3000);
        // attack starts at x=0 (no hardcoded offset) so attack_ms=0 draws a
        // true vertical rise; any offset misleads at the bottom of the knob.
        var ax = a * 45;
        var dx = ax + 10 + d * 35;
        var sx = 140;
        var rx = sx + 15 + r * 40;
        var sustainY = 5 + (1 - s) * 50;
        poly.setAttribute(
            "points",
            "0,58 " + ax + ",5 " + dx + "," + sustainY + " " + sx + "," + sustainY + " " + rx + ",58"
        );
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
