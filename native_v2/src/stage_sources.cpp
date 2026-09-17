#include "stave/stage_sources.hpp"
#include <faust/gui/CInterface.h>
#include <fluidsynth.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

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
bool range(double x, double a, double b) noexcept { return std::isfinite(x) && x >= a && x <= b; }
struct Zones {
    std::array<std::pair<const char*, float*>, 256> entries{};
    unsigned count{}; bool overflow{};
    float* find(const char* name) const noexcept {
        for (unsigned i = 0; i < count; ++i) if (!std::strcmp(name, entries[i].first)) return entries[i].second;
        return nullptr;
    }
    float* require(const std::string& name) const {
        if (auto* value = find(name.c_str())) return value;
        throw std::runtime_error("Missing stage-source zone: " + name);
    }
};
void box(void*, const char*) {}
void end(void*) {}
void zone(void* context, const char* name, float* value) {
    auto& z = *static_cast<Zones*>(context);
    if (z.count == z.entries.size()) z.overflow = true;
    else z.entries[z.count++] = {name, value};
}
void slider(void* c, const char* n, float* v, float, float, float, float) { zone(c, n, v); }
void bar(void*, const char*, float*, float, float) {}
void file(void*, const char*, const char*, Soundfile**) {}
void declare(void*, float*, const char*, const char*) {}
}
bool StagePatch::valid() const noexcept {
    if (!env1.valid() || !env2.valid() || !piano.valid() || transpose < -24 || transpose > 24 ||
        piano_octave < -3 || piano_octave > 3 || minimum_velocity < 1 || minimum_velocity > 127 ||
        wave1 < 0 || wave1 > 4 || wave2 < 0 || wave2 > 4 || octave1 < -3 || octave1 > 3 ||
        octave2 < -3 || octave2 > 3 || !range(blend1, 0, 1) || !range(blend2, 0, 1) ||
        !range(detune, 0, 1) || !range(spread, 0, 1) || !range(pan1, -1, 1) || !range(pan2, -1, 1) ||
        !range(piano_velocity_curve, 1, 4) || !range(osc_bend_semitones, -2, 2)) return false;
    for (const auto& lfo : poly_lfo)
        if (!range(lfo.rate, .05, 20) || !range(lfo.depth, 0, .7) || lfo.shape < 0 || lfo.shape > 6) return false;
    return true;
}
struct StageSources::Impl final : StageNoteSink, StageVoiceSink {
    std::unique_ptr<StaveOscBank, decltype(&deleteStaveOscBank)> osc{nullptr, deleteStaveOscBank};
    std::unique_ptr<fluid_settings_t, decltype(&delete_fluid_settings)> settings{nullptr, delete_fluid_settings};
    std::unique_ptr<fluid_synth_t, decltype(&delete_fluid_synth)> piano{nullptr, delete_fluid_synth};
    PhaseSource& phases;
    StageVoices voices{*this};
    StageNotes notes{*this};
    PianoChain chain;
    StagePatch patch;
    StageSourceStats counters;
    std::array<std::array<float*, 9>, 12> slots{};
    std::array<float*, 12> globals{};
    std::array<std::array<float*, 4>, 2> lfos{};
    std::array<std::array<float, 512>, 5> osc_audio{};
    std::array<float*, 5> osc_ptrs{};
    std::array<std::array<double, 512>, 7> stems{};
    std::array<short, 1024> raw{};
    const std::uint32_t frames;
    std::uint64_t position{};
    double velocity_tracker{.7};
    bool fault{}, stopped{};

    Impl(const std::string& font, PhaseSource& source, std::uint32_t size, int program)
        : phases(source), chain(48000, size), frames(size) {
        static_assert(sizeof(short) == 2, "Fluid int16 acquisition requires16-bit short");
        if ((size != 256 && size != 512) || font.empty() || program < 0 || program > 127)
            throw std::invalid_argument("Stage source needs explicit SF2, fixed256/512, valid program");
        osc.reset(newStaveOscBank());
        if (!osc) throw std::runtime_error("Oscillator allocation failed");
        initStaveOscBank(osc.get(), 48000);
        if (getNumInputsStaveOscBank(osc.get()) != 0 || getNumOutputsStaveOscBank(osc.get()) != 5)
            throw std::runtime_error("Wrong oscillator topology");
        Zones zones;
        UIGlue ui{&zones, box, box, box, end, zone, zone, slider, slider, slider, bar, bar, file, declare};
        buildUserInterfaceStaveOscBank(osc.get(), &ui);
        if (zones.overflow || zones.find("freq_v12")) throw std::runtime_error("Expected12 oscillator slots");
        for (unsigned i = 0; i < 12; ++i) {
            unsigned n = 0;
            for (const char* prefix : {"freq_v", "gate_v", "gate_osc1_v", "gate_osc2_v", "shimmer_gate_v",
                                      "osc1_phase_v", "osc2_phase_v", "lfo1_phase_v", "lfo2_phase_v"})
                slots[i][n++] = zones.require(std::string(prefix) + std::to_string(i));
        }
        unsigned n = 0;
        for (const char* name : {"osc1_wf", "osc2_wf", "osc1_blend", "osc2_blend", "osc1_oct", "osc2_oct",
                                 "uni_detune", "uni_spread", "osc1_pan", "osc2_pan", "shimmer_enable", "shimmer_mult"})
            globals[n++] = zones.require(name);
        for (unsigned i = 0; i < 2; ++i) {
            n = 0;
            for (const char* suffix : {"active", "rate", "depth", "shape"})
                lfos[i][n++] = zones.require("lfo" + std::to_string(i + 1) + "_" + suffix);
        }
        for (unsigned i = 0; i < 5; ++i) osc_ptrs[i] = osc_audio[i].data();
        settings.reset(new_fluid_settings());
        if (!settings) throw std::runtime_error("Fluid settings allocation failed");
        auto integer = [&](const char* name, int value) {
            if (fluid_settings_setint(settings.get(), name, value) != FLUID_OK) throw std::runtime_error(name);
        };
        if (fluid_settings_setnum(settings.get(), "synth.sample-rate", 48000) != FLUID_OK ||
            fluid_settings_setnum(settings.get(), "synth.gain", 1) != FLUID_OK) throw std::runtime_error("Fluid settings failed");
        integer("synth.polyphony", 32); integer("synth.dynamic-sample-loading", 0);
        integer("synth.reverb.active", 0); integer("synth.chorus.active", 0);
        integer("synth.cpu-cores", 1); integer("synth.lock-memory", 0);
        piano.reset(new_fluid_synth(settings.get()));
        if (!piano) throw std::runtime_error("Fluid synth allocation failed");
        const int sfid = fluid_synth_sfload(piano.get(), font.c_str(), 0);
        if (sfid < 0 || fluid_synth_program_select(piano.get(), 0, sfid, 0, program) != FLUID_OK)
            throw std::runtime_error("Explicit piano asset/program unavailable");
        if (!configure(patch)) throw std::runtime_error("Initial stage patch failed");
    }
    void fluid_check(int status) noexcept { if (status != FLUID_OK) { ++counters.fluid_errors; fault = true; } }
    bool configure(const StagePatch& next) noexcept {
        if (fault || stopped || !next.valid()) return false;
        // All validation precedes writes; numeric controls only, no asset load.
        voices.configure(next.env1, next.env2);
        notes.configure(next.transpose, next.piano_octave, next.minimum_velocity);
        chain.configure(next.piano);
        patch = next;
        const std::array<double, 12> values{double(next.wave1), double(next.wave2), next.blend1, next.blend2,
            std::pow(2., next.octave1), std::pow(2., next.octave2), next.detune, next.spread, next.pan1, next.pan2,
            next.shimmer ? 1. : 0., next.shimmer_high ? 4. : 2.};
        for (unsigned i = 0; i < values.size(); ++i) *globals[i] = static_cast<float>(values[i]);
        for (unsigned i = 0; i < 2; ++i) {
            const auto& l = next.poly_lfo[i];
            *lfos[i][0] = l.active ? 1 : 0; *lfos[i][1] = static_cast<float>(l.rate);
            *lfos[i][2] = static_cast<float>(l.depth); *lfos[i][3] = static_cast<float>(l.shape);
        }
        return true;
    }
    void osc_on(std::uint8_t n, double v, LayerWeights w) noexcept override { if (!voices.note_on(n, v, w)) fault = true; }
    void osc_off(std::uint8_t n) noexcept override { if (!voices.note_off(n)) fault = true; }
    void piano_on(std::uint8_t n, double velocity) noexcept override {
        if (fault) return; // A failed oscillator phase handoff already stopped this command.
        const int value = std::clamp(static_cast<int>(std::pow(velocity, 1 / std::max(1., patch.piano_velocity_curve)) * 127), 1, 127);
        const int status = fluid_synth_noteon(piano.get(), 0, n, value);
        fluid_check(status);
        if (status == FLUID_OK) velocity_tracker = .6 * velocity_tracker + .4 * velocity;
    }
    void piano_off(std::uint8_t n) noexcept override {
        if (fluid_synth_noteoff(piano.get(), 0, n) != FLUID_OK) ++counters.unmatched_piano_offs;
    }
    void all_notes_off() noexcept override {
        if (!voices.release_all()) fault = true;
        for (int cc : {64, 66, 123}) fluid_check(fluid_synth_cc(piano.get(), 0, cc, 0));
        fluid_check(fluid_synth_pitch_bend(piano.get(), 0, 8192));
    }
    void key_trigger() noexcept override {} // global/key-sync LFOs not in this source slice
    void start_slot(unsigned slot) noexcept override {
        VoicePhases p;
        if (!phases.next(p) || !range(p.osc1, 0, 1) || !range(p.osc2, 0, 1) ||
            !range(p.lfo1, 0, 1) || !range(p.lfo2, 0, 1)) { ++counters.phase_errors; fault = true; return; }
        *slots[slot][5] = static_cast<float>(p.osc1); *slots[slot][6] = static_cast<float>(p.osc2);
        *slots[slot][7] = static_cast<float>(p.lfo1); *slots[slot][8] = static_cast<float>(p.lfo2);
    }
    void clear_slot(unsigned slot) noexcept override { for (unsigned i : {1u, 2u, 3u}) *slots[slot][i] = 0; }
    void gate(unsigned slot, std::uint8_t note, double a, double b, double shimmer) noexcept override {
        double hz = 440. * std::pow(2., (double(note) - 69) / 12);
        if (patch.osc_bend_semitones != 0) hz *= std::pow(2., patch.osc_bend_semitones / 12);
        *slots[slot][0] = static_cast<float>(hz);
        a = std::clamp(a, 0., 1.); b = std::clamp(b, 0., 1.);
        *slots[slot][1] = static_cast<float>(std::max(a, b));
        *slots[slot][2] = static_cast<float>(a); *slots[slot][3] = static_cast<float>(b);
        *slots[slot][4] = static_cast<float>(std::clamp(shimmer, 0., 1.));
    }
    void stop() noexcept {
        if (!stopped) {
            stopped = true;
            fluid_check(fluid_synth_all_sounds_off(piano.get(), 0));
            instanceClearStaveOscBank(osc.get());
            for (unsigned i = 0; i < 12; ++i) clear_slot(i);
            chain.clear();
        }
        for (auto& s : stems) s.fill(0);
        raw.fill(0);
    }
    bool command(std::uint64_t boundary, const StageCommand& e) noexcept {
        bool valid = !fault && !stopped && boundary == position && e.note >= 0 && e.note < 128 && e.weights.valid();
        switch (e.action) {
        case StageAction::NoteOn: valid = valid && e.value >= 0 && e.value <= 127; break;
        case StageAction::Sustain: case StageAction::Sostenuto: valid = valid && (e.value == 0 || e.value == 1); break;
        case StageAction::NoteOff: case StageAction::ReleaseAll: valid = valid && e.value == 0; break;
        default: valid = false;
        }
        if (!valid) { ++counters.rejected_commands; return false; }
        switch (e.action) {
        case StageAction::NoteOn: notes.note_on(e.note, e.value, e.weights); break;
        case StageAction::NoteOff: notes.note_off(e.note); break;
        case StageAction::Sustain: notes.sustain(e.value != 0); break;
        case StageAction::Sostenuto: notes.sostenuto(e.value != 0); break;
        case StageAction::ReleaseAll: notes.all_notes_off(); break;
        }
        if (fault) stop();
        return !fault;
    }
    bool render(bool render_oscillators) noexcept {
        if (fault || stopped) { stop(); return false; }
        if (frames > std::numeric_limits<std::uint64_t>::max() - position) { fault = true; stop(); return false; }
        if (!voices.begin_block(frames, !render_oscillators)) { fault = true; stop(); return false; }
        if (render_oscillators) {
            // Original active-slot cleanup precedes compute (separate from
            // retiring/recycling inactive voices after compute).
            std::array<bool, 12> active{};
            for (unsigned i = 0; i < voices.size(); ++i) {
                const auto* v = voices.voice_at(i);
                if (v->env1.active() || v->env2.active()) active[voices.slot_at(i)] = true;
            }
            for (unsigned i = 0; i < active.size(); ++i) if (!active[i]) clear_slot(i);
            computeStaveOscBank(osc.get(), frames, nullptr, osc_ptrs.data());
        } else for (auto& channel : osc_audio) channel.fill(0);
        if (!voices.end_block()) fault = true;
        fluid_check(fluid_synth_write_s16(piano.get(), frames, raw.data(), 0, 2, raw.data(), 1, 2));
        for (unsigned i = 0; i < frames; ++i) {
            for (unsigned ch = 0; ch < 5; ++ch) stems[ch][i] = osc_audio[ch][i];
            for (unsigned ch = 0; ch < 2; ++ch) {
                const short value = raw[2 * i + ch];
                counters.full_scale_piano_samples += value == -32768 || value == 32767;
                stems[5 + ch][i] = value / 32768.;
            }
        }
        auto p = patch.piano; p.velocity_target = velocity_tracker;
        if (!chain.configure(p) || !chain.process_block(stems[5].data(), stems[6].data(),
                stems[5].data(), stems[6].data(), frames)) fault = true;
        for (auto& s : stems) for (unsigned i = 0; i < frames; ++i)
            if (!std::isfinite(s[i])) { ++counters.numeric_errors; fault = true; }
        if (fault) { stop(); return false; }
        position += frames;
        return true;
    }
};
StageSources::StageSources(const std::string& font, PhaseSource& phases, std::uint32_t frames, int program)
    : impl_(std::make_unique<Impl>(font, phases, frames, program)) {}
StageSources::~StageSources() = default;
bool StageSources::configure(const StagePatch& p) noexcept { return impl_->configure(p); }
bool StageSources::command(std::uint64_t b, const StageCommand& e) noexcept { return impl_->command(b, e); }
bool StageSources::render_block(bool render_oscillators) noexcept { return impl_->render(render_oscillators); }
void StageSources::stop() noexcept { impl_->stop(); }
bool StageSources::healthy() const noexcept { return !impl_->fault && !impl_->stopped; }
std::uint64_t StageSources::frame_position() const noexcept { return impl_->position; }
std::uint32_t StageSources::block_frames() const noexcept { return impl_->frames; }
const double* StageSources::stem(unsigned c) const noexcept { return c < 7 ? impl_->stems[c].data() : nullptr; }
const short* StageSources::raw_piano() const noexcept { return impl_->raw.data(); }
const StageSourceStats& StageSources::stats() const noexcept { return impl_->counters; }
unsigned StageSources::active_voices() const noexcept { return impl_->stopped ? 0 : impl_->voices.size(); }
} // namespace stave
