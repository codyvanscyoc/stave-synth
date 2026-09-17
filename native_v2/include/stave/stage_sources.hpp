#pragma once
#include "stave/piano_chain.hpp"
#include "stave/stage_voices.hpp"
#include <memory>
#include <string>

namespace stave {
struct VoicePhases { double osc1{}, osc2{}, lfo1{}, lfo2{}; };
// Prepared fixture/asset owner supplies phase draws. No random-device I/O on
// the render owner. A production generator/seed policy is not supplied here.
class PhaseSource {
public:
    virtual ~PhaseSource() = default;
    virtual bool next(VoicePhases&) noexcept = 0;
};
struct PolyLfo { bool active{}; double rate{1}, depth{}; int shape{}; };
struct StagePatch {
    EnvelopeConfig env1{}, env2{};
    PianoChainConfig piano{};
    int transpose{}, piano_octave{}, minimum_velocity{10};
    int wave1{}, wave2{1}, octave1{}, octave2{};
    double blend1{.6}, blend2{.4}, detune{.07}, spread{.85}, pan1{}, pan2{};
    double piano_velocity_curve{1}, osc_bend_semitones{};
    bool shimmer{}, shimmer_high{};
    std::array<PolyLfo, 2> poly_lfo{};
    bool valid() const noexcept;
};
enum class StageAction { NoteOn, NoteOff, Sustain, Sostenuto, ReleaseAll };
struct StageCommand {
    StageAction action{StageAction::NoteOn};
    int note{}, value{}; // value: MIDI velocity, or pedal0/1
    LayerWeights weights{}; // externally computed split weights
};
struct StageSourceStats {
    std::uint64_t rejected_commands{}, fluid_errors{}, unmatched_piano_offs{},
                  full_scale_piano_samples{}, phase_errors{}, numeric_errors{};
};

// M2 OFFLINE boundary-cadence integration, deliberately not an Engine Backend.
// Single owner, no devices, threads, queue, browser or saved-state access.
// Owns actual Faust/Fluid plus stage key/voice/envelope/piano-chain components.
// Only48k and fixed256/512 are admitted. Every command must name the CURRENT
// block boundary: future, stale and sub-block commands are rejected, NOT rounded.
// Piano compressor and envelopes run once per complete block. A later sample-
// positioned adapter must explicitly resolve this contract, not call per slice.
// Output stems: OSC1 L/R, OSC2 L/R, shimmer mono, processed piano L/R.
// Fixed3-copy unison. NO master mixing/limiter, room, piano pitch bend,
// global modulation, drift or worship FX/bed. piano.velocity_target is owned
// by note activity here (the nested patch value is not a separate control).
class StageSources final {
public:
    StageSources(const std::string& soundfont, PhaseSource& phases,
                 std::uint32_t frames = 512, int piano_program = 0);
    ~StageSources();
    StageSources(const StageSources&) = delete;
    StageSources& operator=(const StageSources&) = delete;
    bool configure(const StagePatch&) noexcept;
    bool command(std::uint64_t boundary, const StageCommand&) noexcept;
    bool render_block() noexcept;
    // Owner-only terminal hard stop; cannot resume or be called concurrently.
    void stop() noexcept;
    bool healthy() const noexcept;
    std::uint64_t frame_position() const noexcept;
    std::uint32_t block_frames() const noexcept;
    const double* stem(unsigned channel) const noexcept; // nullptr outside0..6
    const short* raw_piano() const noexcept; // interleaved signed16 diagnostic
    const StageSourceStats& stats() const noexcept;
    unsigned active_voices() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
