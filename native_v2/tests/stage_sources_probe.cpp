// Test-only C ABI. Construction copies a finite phase fixture while stopped.
#include "stave/stage_sources.hpp"
#include <cmath>
#include <vector>

struct Owner final : stave::PhaseSource {
    std::vector<stave::VoicePhases> tape;
    std::size_t cursor{};
    std::unique_ptr<stave::StageSources> graph;
    Owner(const char* font, unsigned frames, const double* values, unsigned count) {
        if (!font || !values || count == 0 || count > 4096) throw std::invalid_argument("Bad phase tape");
        tape.reserve(count);
        for (unsigned i = 0; i < count; ++i) {
            for (unsigned j = 0; j < 4; ++j)
                if (!std::isfinite(values[i*4+j]) || values[i*4+j] < 0 || values[i*4+j] > 1)
                    throw std::invalid_argument("Invalid prepared phase");
            tape.push_back({values[4*i], values[4*i+1], values[4*i+2], values[4*i+3]});
        }
        graph = std::make_unique<stave::StageSources>(font, *this, frames);
    }
    bool next(stave::VoicePhases& p) noexcept override {
        if (cursor == tape.size()) return false;
        p = tape[cursor++]; return true;
    }
};
extern "C" {
void* sources_create(const char* font, unsigned frames, const double* phases, unsigned count) noexcept {
    try { return new Owner(font, frames, phases, count); } catch (...) { return nullptr; }
}
void sources_delete(void* h) noexcept { delete static_cast<Owner*>(h); }
int sources_patch(void* h, const double* v, unsigned count) noexcept {
    if (!h || !v || count != 47) return 0;
    for (unsigned i = 0; i < count; ++i) if (!std::isfinite(v[i]) || std::abs(v[i]) > 30000) return 0;
    for (unsigned i : {8u,9u,10u,11u,12u,13u,14u,22u,23u,24u,27u,28u,31u,35u,41u})
        if (std::floor(v[i]) != v[i]) return 0;
    for (unsigned i : {22u,23u,24u,28u,35u,41u}) if (v[i] != 0 && v[i] != 1) return 0;
    stave::StagePatch p;
    p.env1 = {v[0],v[1],v[2],v[3]}; p.env2 = {v[4],v[5],v[6],v[7]};
    p.transpose = int(v[8]); p.piano_octave = int(v[9]); p.minimum_velocity = int(v[10]);
    p.wave1 = int(v[11]); p.wave2 = int(v[12]); p.octave1 = int(v[13]); p.octave2 = int(v[14]);
    p.blend1 = v[15]; p.blend2 = v[16]; p.detune = v[17]; p.spread = v[18];
    p.pan1 = v[19]; p.pan2 = v[20]; p.osc_bend_semitones = v[21];
    p.shimmer = v[22]; p.shimmer_high = v[23];
    p.poly_lfo[0] = {v[24] != 0, v[25], v[26], int(v[27])};
    p.poly_lfo[1] = {v[28] != 0, v[29], v[30], int(v[31])};
    p.piano.volume = v[32]; p.piano.lowcut_hz = v[33]; p.piano.highcut_hz = v[34];
    p.piano.comp_enabled = v[35]; p.piano.comp_threshold_db = v[36]; p.piano.comp_ratio = v[37];
    p.piano.comp_makeup_db = v[38]; p.piano.comp_drive_db = v[39]; p.piano.comp_wet = v[40];
    p.piano.brightness_enabled = v[41]; p.piano.brightness_amount = v[42];
    p.piano.tremolo_hz = v[43]; p.piano.tremolo_depth = v[44]; p.piano_velocity_curve = v[45];
    // Last field is reserved to catch accidental ABI expansion/misalignment.
    if (v[46] != 0) return 0;
    return static_cast<Owner*>(h)->graph->configure(p);
}
int sources_command(void* h, std::uint64_t frame, int type, int note, int value, const double* weights) noexcept {
    if (!h || !weights || type < 0 || type > 4) return 0;
    return static_cast<Owner*>(h)->graph->command(frame, {static_cast<stave::StageAction>(type), note, value,
                                                       {weights[0],weights[1],weights[2],weights[3]}});
}
int sources_render(void* h) noexcept { return h && static_cast<Owner*>(h)->graph->render_block(); }
const double* sources_stem(void* h, unsigned c) noexcept { return h ? static_cast<Owner*>(h)->graph->stem(c) : nullptr; }
const short* sources_raw(void* h) noexcept { return h ? static_cast<Owner*>(h)->graph->raw_piano() : nullptr; }
void sources_stop(void* h) noexcept { if (h) static_cast<Owner*>(h)->graph->stop(); }
unsigned sources_voices(void* h) noexcept { return h ? static_cast<Owner*>(h)->graph->active_voices() : 0; }
std::uint64_t sources_frame(void* h) noexcept { return h ? static_cast<Owner*>(h)->graph->frame_position() : 0; }
unsigned sources_phases(void* h) noexcept { return h ? unsigned(static_cast<Owner*>(h)->cursor) : 0; }
int sources_stats(void* h, std::uint64_t* values) noexcept {
    if (!h || !values) return 0;
    const auto& s = static_cast<Owner*>(h)->graph->stats();
    values[0]=s.rejected_commands; values[1]=s.fluid_errors; values[2]=s.unmatched_piano_offs;
    values[3]=s.full_scale_piano_samples; values[4]=s.phase_errors; values[5]=s.numeric_errors;
    return 1;
}
}
