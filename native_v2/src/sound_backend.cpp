#include <stave/sound_backend.hpp>

#include <faust/gui/CInterface.h>
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
#include <fluidsynth.h>
#endif

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <vector>

// The existing osc_bank.dsp, built with its existing 12-slot lite transform.
// It remains a separate C translation unit, compiled without altering DSP.
extern "C" {
struct StaveOscBank;
StaveOscBank* newStaveOscBank();
void deleteStaveOscBank(StaveOscBank*);
void initStaveOscBank(StaveOscBank*, int);
void instanceClearStaveOscBank(StaveOscBank*);
int getNumInputsStaveOscBank(StaveOscBank*);
int getNumOutputsStaveOscBank(StaveOscBank*);
void buildUserInterfaceStaveOscBank(StaveOscBank*, UIGlue*);
void computeStaveOscBank(StaveOscBank*, int, float**, float**);
}

namespace stave {
namespace {
struct Zone {
    const char* name{};
    float* value{};
};

struct ZoneCollector {
    std::array<Zone, 256> zones{};
    std::size_t size{};
    bool overflow{};

    float* find(const char* name) const noexcept {
        for (std::size_t i = 0; i < size; ++i) {
            if (std::strcmp(name, zones[i].name) == 0) return zones[i].value;
        }
        return nullptr;
    }
    float* require(const char* name) const {
        if (auto* value = find(name)) return value;
        throw std::runtime_error(std::string("Faust zone missing: ") + name);
    }
};

void open_box(void*, const char*) {}
void close_box(void*) {}
void add_zone(void* opaque, const char* label, float* value) {
    auto& collector = *static_cast<ZoneCollector*>(opaque);
    if (collector.size == collector.zones.size()) {
        collector.overflow = true;
        return;
    }
    collector.zones[collector.size++] = {label, value};
}
void add_slider(void* opaque, const char* label, float* value,
                float, float, float, float) { add_zone(opaque, label, value); }
void add_bar(void*, const char*, float*, float, float) {}
void add_soundfile(void*, const char*, const char*, Soundfile**) {}
void declare_zone(void*, float*, const char*, const char*) {}

bool unit(float value) noexcept {
    return std::isfinite(value) && value >= 0.0f && value <= 1.0f;
}
} // namespace

struct SoundBackend::Impl {
    struct Voice {
        float* freq{};
        float* gate{};
        float* gate1{};
        float* gate2{};
        std::uint64_t age{};
        std::uint64_t key_generation{};
        std::uint32_t release_remaining{};
        std::uint8_t channel{};
        std::uint8_t note{};
        bool occupied{};
        bool down{};
    };

    SoundConfig config;
    SoundStats counters;
    std::unique_ptr<StaveOscBank, decltype(&deleteStaveOscBank)> osc{
        nullptr, deleteStaveOscBank};
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
    std::unique_ptr<fluid_settings_t, decltype(&delete_fluid_settings)> settings{
        nullptr, delete_fluid_settings};
    std::unique_ptr<fluid_synth_t, decltype(&delete_fluid_synth)> piano{
        nullptr, delete_fluid_synth};
#endif
    std::array<Voice, oscillator_slots> voices{};
    std::array<bool, 16> sustain{};
    // Independent of oscillator stealing, so releasing an old/stolen key
    // cannot inadvertently release a newer occurrence of that key on piano.
    std::array<std::array<std::uint16_t, 128>, 16> held{};
    std::array<std::array<std::uint64_t, 128>, 16> pressed_generation{};
    std::array<std::array<std::uint64_t, 128>, 16> released_generation{};
    std::array<float, 128> frequencies{};
    std::array<std::vector<float>, 5> osc_buffers;
    std::array<float*, 5> osc_outputs{};
    std::vector<float> piano_left;
    std::vector<float> piano_right;
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
    std::vector<float> piano_drain_left;
    std::vector<float> piano_drain_right;
#endif
    float* blend1{};
    float* blend2{};
    std::uint64_t serial{};
    bool fault{};

    explicit Impl(const SoundConfig& requested) : config(requested) {
        if (config.sample_rate < 8000 || config.sample_rate > 192000 ||
            config.max_frames == 0 || config.max_frames > 4096 ||
            !unit(config.osc_mix_gain) || !unit(config.piano_mix_gain) ||
            config.piano_bank < 0 || config.piano_bank > 16383 ||
            config.piano_program < 0 || config.piano_program > 127) {
            throw std::invalid_argument("Invalid offline sound configuration");
        }
        for (std::size_t i = 0; i < osc_buffers.size(); ++i) {
            osc_buffers[i].resize(config.max_frames);
            osc_outputs[i] = osc_buffers[i].data();
        }
        piano_left.resize(config.max_frames);
        piano_right.resize(config.max_frames);
        for (std::size_t i = 0; i < frequencies.size(); ++i) {
            frequencies[i] = 440.0f * std::exp2((static_cast<float>(i) - 69.0f) / 12.0f);
        }
        osc.reset(newStaveOscBank());
        if (!osc) throw std::runtime_error("Faust allocation failed");
        initStaveOscBank(osc.get(), static_cast<int>(config.sample_rate));
        if (getNumInputsStaveOscBank(osc.get()) != 0 ||
            getNumOutputsStaveOscBank(osc.get()) != 5) {
            throw std::runtime_error("Expected zero-input/five-output osc_bank");
        }
        ZoneCollector zones;
        UIGlue ui{&zones, open_box, open_box, open_box, close_box,
                  add_zone, add_zone, add_slider, add_slider, add_slider,
                  add_bar, add_bar, add_soundfile, declare_zone};
        buildUserInterfaceStaveOscBank(osc.get(), &ui);
        if (zones.overflow || zones.find("freq_v12")) {
            throw std::runtime_error("Expected existing 12-slot lite oscillator build");
        }
        for (std::size_t i = 0; i < voices.size(); ++i) {
            char name[40];
            auto bind = [&](const char* prefix) {
                std::snprintf(name, sizeof(name), "%s%zu", prefix, i);
                return zones.require(name);
            };
            auto& voice = voices[i];
            voice.freq = bind("freq_v");
            voice.gate = bind("gate_v");
            voice.gate1 = bind("gate_osc1_v");
            voice.gate2 = bind("gate_osc2_v");
            // Fixed deterministic decorrelation for comparable offline runs.
            // This is deliberately not V1's random-on-every-note behavior.
            *bind("osc1_phase_v") = std::fmod(0.137f + 0.173f * i, 1.0f);
            *bind("osc2_phase_v") = std::fmod(0.532f + 0.317f * i, 1.0f);
        }
        blend1 = zones.require("osc1_blend");
        blend2 = zones.require("osc2_blend");
        // Keep DSP defaults: sine/square, 0.6/0.4, unity octave, unison3,
        // detune .07, spread .85. Shimmer/poly-LFO are off in this slice.
        *zones.require("shimmer_enable") = 0.0f;
        *zones.require("lfo1_active") = 0.0f;
        *zones.require("lfo2_active") = 0.0f;
        prepare_piano();
    }

    void prepare_piano() {
        if (config.soundfont_path.empty()) return;
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
        settings.reset(new_fluid_settings());
        if (!settings) throw std::runtime_error("FluidSynth settings allocation failed");
        auto set_int = [&](const char* key, int value) {
            if (fluid_settings_setint(settings.get(), key, value) != FLUID_OK)
                throw std::runtime_error(std::string("FluidSynth rejected setting: ") + key);
        };
        auto set_num = [&](const char* key, double value) {
            if (fluid_settings_setnum(settings.get(), key, value) != FLUID_OK)
                throw std::runtime_error(std::string("FluidSynth rejected setting: ") + key);
        };
        set_num("synth.sample-rate", config.sample_rate);
        set_num("synth.gain", 0.2);
        set_int("synth.polyphony", piano_polyphony);
        set_int("synth.cpu-cores", 1);
        set_int("synth.threadsafe-api", 0); // Sole native owner, never UI calls.
        set_int("synth.dynamic-sample-loading", 0);
        set_int("synth.reverb.active", 0);
        set_int("synth.chorus.active", 0);
        // Offline preparation must not try locking large personal SF2 memory.
        set_int("synth.lock-memory", 0);
        piano.reset(new_fluid_synth(settings.get()));
        if (!piano) throw std::runtime_error("FluidSynth allocation failed");
        const int internal_frames = fluid_synth_get_internal_bufsize(piano.get());
        if (internal_frames <= 0 || internal_frames > 4096)
            throw std::runtime_error("Unsupported FluidSynth internal block size");
        piano_drain_left.resize(internal_frames);
        piano_drain_right.resize(internal_frames);
        const int sfid = fluid_synth_sfload(piano.get(), config.soundfont_path.c_str(), 0);
        if (sfid < 0) throw std::runtime_error("Requested SoundFont failed to load");
        for (int ch = 0; ch < 16; ++ch) {
            if (fluid_synth_program_select(piano.get(), ch, sfid,
                                          config.piano_bank, config.piano_program) != FLUID_OK)
                throw std::runtime_error("Requested SoundFont piano program unavailable");
        }
#else
        throw std::runtime_error("Piano requested but built without FluidSynth support");
#endif
    }

    void release(Voice& voice) noexcept {
        *voice.gate = *voice.gate1 = *voice.gate2 = 0.0f;
        voice.down = false;
        // Let the existing 1ms Faust gate smoother drain before reusing an
        // otherwise-free slot. Stealing is still a prototype limitation.
        voice.release_remaining = config.sample_rate / 100;
    }

    void note_off(std::uint8_t channel, std::uint8_t note) noexcept {
        auto& count = held[channel][note];
        if (count == 0) return; // Duplicate/spurious release is harmless.
        --count;
        const auto generation = ++released_generation[channel][note];
        Voice* first = nullptr;
        for (auto& voice : voices) {
            if (voice.occupied && voice.down && voice.channel == channel &&
                voice.note == note && voice.key_generation == generation) first = &voice;
        }
        if (first) {
            first->down = false;
            if (!sustain[channel]) release(*first);
        }
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
        // FluidSynth noteoff releases the key, not a separately addressed
        // repetition; aggregate until all outstanding presses are released.
        // The documented failure status also means no voice matched (normal
        // after native polyphony stealing/decay). Validated channel/key/owner
        // make this distinct from a failed note-on/control/render operation.
        if (piano && count == 0 &&
            fluid_synth_noteoff(piano.get(), channel, note) != FLUID_OK) ++counters.fluid_noteoff_misses;
#endif
    }

    void note_on(const Event& event) noexcept {
        if (event.velocity == 0) { note_off(event.channel, event.note); return; }
        auto& count = held[event.channel][event.note];
        auto& generation = pressed_generation[event.channel][event.note];
        if (count == std::numeric_limits<std::uint16_t>::max() ||
            generation == std::numeric_limits<std::uint64_t>::max() ||
            serial == std::numeric_limits<std::uint64_t>::max()) {
            ++counters.rejected_events;
            fault = true;
            panic();
            return;
        }
        ++count;
        Voice* chosen = nullptr;
        for (auto& voice : voices) {
            if (!voice.occupied) { chosen = &voice; break; }
        }
        if (!chosen) {
            // Prefer a released tail, otherwise the oldest occupied slot.
            for (auto& voice : voices) {
                if (!chosen || (voice.release_remaining && !chosen->release_remaining) ||
                    ((bool(voice.release_remaining) == bool(chosen->release_remaining)) &&
                     voice.age < chosen->age)) chosen = &voice;
            }
            ++counters.voice_steals;
        }
        chosen->occupied = true;
        chosen->down = true;
        chosen->release_remaining = 0;
        chosen->channel = event.channel;
        chosen->note = event.note;
        chosen->age = ++serial;
        chosen->key_generation = ++generation;
        *chosen->freq = frequencies[event.note];
        *chosen->gate = *chosen->gate1 = *chosen->gate2 = event.velocity / 127.0f;
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
        if (piano && fluid_synth_noteon(piano.get(), event.channel,
                                      event.note, event.velocity) != FLUID_OK) {
            ++counters.fluid_errors;
            fault = true;
            panic();
        }
#endif
    }

    void panic() noexcept {
        for (auto& voice : voices) {
            *voice.gate = *voice.gate1 = *voice.gate2 = 0.0f;
            voice.occupied = voice.down = false;
            voice.release_remaining = 0;
        }
        sustain.fill(false);
        for (auto& channel : held) channel.fill(0);
        for (auto& channel : pressed_generation) channel.fill(0);
        for (auto& channel : released_generation) channel.fill(0);
        serial = 0;
        instanceClearStaveOscBank(osc.get());
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
        if (piano) {
            for (int ch = 0; ch < 16; ++ch) {
                if (fluid_synth_cc(piano.get(), ch, 64, 0) != FLUID_OK) {
                    ++counters.fluid_errors; fault = true;
                }
                if (fluid_synth_all_sounds_off(piano.get(), ch) != FLUID_OK) {
                    ++counters.fluid_errors; fault = true;
                }
            }
            // write_float may retain a partly consumed internal block from
            // before STOP. Discard at most one known/preallocated block.
            if (fluid_synth_write_float(piano.get(), static_cast<int>(piano_drain_left.size()),
                                       piano_drain_left.data(), 0, 1,
                                       piano_drain_right.data(), 0, 1) != FLUID_OK) {
                ++counters.fluid_errors;
                fault = true;
            }
        }
#endif
    }

    void handle(const Event& event) noexcept {
        if (fault) return;
        if (event.channel > 15 || event.note > 127 || event.velocity > 127 ||
            !std::isfinite(event.value)) { ++counters.rejected_events; return; }
        switch (event.type) {
        case EventType::NoteOn: note_on(event); break;
        case EventType::NoteOff: note_off(event.channel, event.note); break;
        case EventType::Sustain:
            if (!unit(event.value)) { ++counters.rejected_events; return; }
            sustain[event.channel] = event.value >= 0.5f;
            if (!sustain[event.channel]) for (auto& voice : voices) {
                if (voice.occupied && !voice.down && !voice.release_remaining &&
                    voice.channel == event.channel) release(voice);
            }
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
            if (piano && fluid_synth_cc(piano.get(), event.channel, 64,
                                        sustain[event.channel] ? 127 : 0) != FLUID_OK) {
                ++counters.fluid_errors; fault = true; panic();
            }
#endif
            break;
        case EventType::Panic: panic(); break;
        case EventType::Osc1Blend:
        case EventType::Osc2Blend:
            if (!unit(event.value)) { ++counters.rejected_events; return; }
            *(event.type == EventType::Osc1Blend ? blend1 : blend2) = event.value;
            break;
        default: ++counters.rejected_events; break;
        }
    }

    void render(float* left, float* right, std::uint32_t frames) noexcept {
        if (!left || !right || left == right || frames > config.max_frames) {
            ++counters.invalid_render_calls;
            fault = true;
            // The engine validates capacity before calling us. An invalid
            // direct caller must not cause scratch/output overrun.
            return;
        }
        std::fill_n(left, frames, 0.0f);
        std::fill_n(right, frames, 0.0f);
        if (fault || frames == 0) return;
        computeStaveOscBank(osc.get(), static_cast<int>(frames), nullptr, osc_outputs.data());
        bool use_piano = false;
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
        if (piano) {
            if (fluid_synth_write_float(piano.get(), static_cast<int>(frames),
                                       piano_left.data(), 0, 1, piano_right.data(), 0, 1) != FLUID_OK) {
                ++counters.fluid_errors; fault = true; panic(); return;
            }
            use_piano = true;
        }
#endif
        for (std::uint32_t i = 0; i < frames; ++i) {
            float l = (osc_buffers[0][i] + osc_buffers[2][i]) * config.osc_mix_gain;
            float r = (osc_buffers[1][i] + osc_buffers[3][i]) * config.osc_mix_gain;
            if (use_piano) {
                l += piano_left[i] * config.piano_mix_gain;
                r += piano_right[i] * config.piano_mix_gain;
            }
            if (!std::isfinite(l) || !std::isfinite(r)) {
                ++counters.nonfinite_samples; fault = true;
                continue;
            }
            // Last-resort offline export guard, NOT a musical master limiter.
            // Count every affected stereo frame so overload cannot pass hidden.
            if (std::abs(l) > 0.98f || std::abs(r) > 0.98f) ++counters.clamped_samples;
            left[i] = std::clamp(l, -0.98f, 0.98f);
            right[i] = std::clamp(r, -0.98f, 0.98f);
        }
        if (fault) {
            std::fill_n(left, frames, 0.0f);
            std::fill_n(right, frames, 0.0f);
            panic();
        }
        for (auto& voice : voices) {
            if (voice.release_remaining) {
                voice.release_remaining -= std::min(frames, voice.release_remaining);
                if (!voice.release_remaining) voice.occupied = false;
            }
        }
    }
};

SoundBackend::SoundBackend(const SoundConfig& config) : impl_(std::make_unique<Impl>(config)) {}
SoundBackend::~SoundBackend() = default;
void SoundBackend::render(float* left, float* right, std::uint32_t frames) noexcept {
    impl_->render(left, right, frames);
}
void SoundBackend::handle(const Event& event) noexcept { impl_->handle(event); }
void SoundBackend::panic() noexcept { impl_->panic(); }
const SoundStats& SoundBackend::stats() const noexcept { return impl_->counters; }
bool SoundBackend::healthy() const noexcept { return !impl_->fault; }
bool SoundBackend::piano_enabled() const noexcept {
#if defined(STAVE_V2_WITH_FLUIDSYNTH)
    return bool(impl_->piano);
#else
    return false;
#endif
}
std::uint32_t SoundBackend::active_oscillator_slots() const noexcept {
    std::uint32_t count = 0;
    for (const auto& voice : impl_->voices) if (voice.occupied) ++count;
    return count;
}
} // namespace stave
