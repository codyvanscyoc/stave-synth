#pragma once
#include "stave/source_mix.hpp"
#include "stave/stage_sources.hpp"
#include "stave/stage_buses.hpp"

namespace stave {
// Stage-facing source fields are faders, not prepared DSP amplitudes. Each
// shared setting has one owner: shimmer settings live ONLY in buses.pad.
struct StageCoreConfig {
    EnvelopeConfig env1{}, env2{};
    PianoChainConfig piano{};
    int transpose{}, piano_octave{}, minimum_velocity{10};
    int wave1{}, wave2{1}, octave1{}, octave2{};
    double fader1{.6}, fader2{.4}, pan1{}, pan2{}, detune{.07}, spread{.85};
    double piano_velocity_curve{1}, osc_bend_semitones{};
    bool hard_pan{};
    std::array<PolyLfo,2> poly_lfo{};
    StageBusConfig buses{};
    SourceMixConfig mix_config() const noexcept {
        return {fader1,fader2,pan1,pan2,hard_pan,buses.pad.shimmer,buses.pad.shimmer_high,buses.pad.shimmer_mix};
    }
    StagePatch source_patch(const PreparedSourceMix& mix) const noexcept {
        StagePatch p;
        p.env1=env1; p.env2=env2; p.piano=piano;
        p.transpose=transpose; p.piano_octave=piano_octave; p.minimum_velocity=minimum_velocity;
        p.wave1=wave1; p.wave2=wave2; p.octave1=octave1; p.octave2=octave2;
        p.blend1=mix.amplitude1; p.blend2=mix.amplitude2; p.pan1=mix.pan1; p.pan2=mix.pan2;
        p.detune=detune; p.spread=spread; p.piano_velocity_curve=piano_velocity_curve;
        p.osc_bend_semitones=osc_bend_semitones; p.poly_lfo=poly_lfo;
        p.shimmer=mix.render_shimmer; p.shimmer_high=mix.shimmer_high;
        return p;
    }
    bool valid() const noexcept { return mix_config().valid() && source_patch({}).valid() && buses.valid(); }
};

// Single-owner OFFLINE source + filter/room composition. Fixed whole blocks,
// boundary-only commands. Eleven INTERNAL buses, still no finished master,
// shared FX/bed/driver/UI. No callback/thread-safe control API implied.
class StageCore final {
public:
    StageCore(const std::string& font, PhaseSource& phases, std::uint32_t frames=512, int program=0)
        : sources_(font,phases,frames,program), buses_(frames), mix_(frames) {
        if (!configure(config_)) throw std::runtime_error("Stage core initialization failed");
    }
    bool configure(const StageCoreConfig& config) noexcept {
        if (!healthy() || !config.valid()) return false;
        // All validation before mutation. Source configuration also applies
        // note routing/envelope edits BEFORE following boundary note events.
        if (!sources_.configure(config.source_patch(prepared_)) || !buses_.configure(config.buses) ||
            !mix_.configure(config.mix_config())) { stop(); return false; }
        config_=config; return true;
    }
    bool command(std::uint64_t frame, const StageCommand& command) noexcept {
        if (!healthy()) return false;
        const bool result=sources_.command(frame,command);
        if (!sources_.healthy()) stop();
        return result;
    }
    bool render_block() noexcept {
        if (!healthy()) { stop(); return false; }
        prepared_=mix_.advance();
        if (!sources_.configure(config_.source_patch(prepared_)) || !sources_.render_block(!prepared_.skip_voices)) {
            stop(); return false;
        }
        std::array<const double*,7> input{};
        for(unsigned c=0;c<7;++c) input[c]=sources_.stem(c);
        if (!buses_.process_block(input,{prepared_.render_osc2,sources_.active_voices()>0,
                                        prepared_.haas_active,!prepared_.skip_voices})) { stop(); return false; }
        return true;
    }
    void stop() noexcept { stopped_=true; sources_.stop(); buses_.stop(); }
    bool healthy() const noexcept { return !stopped_ && sources_.healthy() && buses_.healthy(); }
    const double* stem(unsigned c) const noexcept { return buses_.stem(c); }
    std::uint64_t frame_position() const noexcept { return sources_.frame_position(); }
    std::uint32_t block_frames() const noexcept { return sources_.block_frames(); }
    unsigned active_voices() const noexcept { return sources_.active_voices(); }
    const StageSourceStats& stats() const noexcept { return sources_.stats(); }
    const short* raw_piano() const noexcept { return sources_.raw_piano(); }
    const PreparedSourceMix& prepared_mix() const noexcept { return prepared_; }
private:
    StageSources sources_;
    StageBuses buses_;
    SourceMix mix_;
    StageCoreConfig config_;
    PreparedSourceMix prepared_;
    bool stopped_{};
};
} // namespace stave
