#pragma once

#include <stave/engine.hpp>

#include <cstdint>
#include <memory>
#include <string>

namespace stave {

// OFFLINE proof of one native render owner, not the complete V1 instrument.
// No drivers, MIDI ports, threads, services, or saved-state paths are opened.
struct SoundConfig {
    std::uint32_t sample_rate{48000};
    std::uint32_t max_frames{512};
    // Empty explicitly selects oscillator-only. A requested but unavailable
    // piano is a startup error, never replaced with a synthetic substitute.
    std::string soundfont_path;
    int piano_bank{0};
    int piano_program{0};
    float osc_mix_gain{0.025f};
    float piano_mix_gain{0.5f};
};

struct SoundStats {
    std::uint64_t voice_steals{};
    std::uint64_t rejected_events{};
    std::uint64_t invalid_render_calls{};
    std::uint64_t fluid_errors{};
    // FluidSynth's noteoff failure can simply mean the voice was already
    // stolen or decayed. Report separately; it is not a terminal synth fault.
    std::uint64_t fluid_noteoff_misses{};
    std::uint64_t nonfinite_samples{};
    std::uint64_t clamped_samples{};
};

// Construct/destruct only while stopped. All other calls, including stats(),
// are confined to one owner; this class does not supply a concurrency API.
// Render uses fixed-capacity scratch; C++ backend code allocates only at
// construction. This does not certify third-party library allocation behavior.
class SoundBackend final : public Backend {
public:
    static constexpr std::uint32_t oscillator_slots = 12;
    static constexpr std::uint32_t piano_polyphony = 32;

    explicit SoundBackend(const SoundConfig& config);
    ~SoundBackend() override;
    SoundBackend(const SoundBackend&) = delete;
    SoundBackend& operator=(const SoundBackend&) = delete;
    SoundBackend(SoundBackend&&) = delete;
    SoundBackend& operator=(SoundBackend&&) = delete;

    void render(float* left, float* right, std::uint32_t frames) noexcept override;
    void handle(const Event& event) noexcept override;
    void panic() noexcept override;
    const SoundStats& stats() const noexcept;
    bool piano_enabled() const noexcept;
    bool healthy() const noexcept;
    std::uint32_t active_oscillator_slots() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace stave
