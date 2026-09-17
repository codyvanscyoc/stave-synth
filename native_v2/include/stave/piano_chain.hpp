#pragma once
#include <array>
#include <cstdint>
#include <memory>

namespace stave {

struct PianoEqBand {
    double frequency{1000}, gain_db{}, q{1};
    bool enabled{true};
};

struct PianoChainConfig {
    double volume{.5}, lowcut_hz{20}, highcut_hz{20000};
    // Opt-in performance sweep; zero preserves legacy instantaneous retuning.
    double highcut_smoothing_ms{};
    std::array<PianoEqBand, 4> eq{{{150, 2, .8, true}, {300, -2.5, 1, true},
                                  {2800, -3, 1.5, true}, {10000, -1.5, .7, true}}};
    bool comp_enabled{false};
    double comp_threshold_db{-20}, comp_ratio{3}, comp_attack_ms{10}, comp_release_ms{80};
    double comp_makeup_db{}, comp_knee_db{18}, comp_drive_db{}, comp_wet{1};
    bool brightness_enabled{false};
    double brightness_amount{.5}, velocity_target{.7};
    double tremolo_hz{}, tremolo_depth{};
    bool valid() const noexcept;
};

// Component port: volume -> existing double Faust HP/LP/EQ -> block compressor
// -> velocity brightness -> tremolo. No room/shared effects/Fluid acquisition.
// One audio owner. Construct/destruct while stopped, configure between calls.
// process_block must receive a complete reference cadence, not each MIDI event
// slice: compressor RMS intentionally applies to the SAME complete block.
// No runtime/browser, drivers, queues, filesystem access or callback adapters.
class PianoChain final {
public:
    PianoChain(std::uint32_t sample_rate = 48000, std::uint32_t maximum_frames = 512,
               const PianoChainConfig& config = {});
    ~PianoChain();
    PianoChain(const PianoChain&) = delete;
    PianoChain& operator=(const PianoChain&) = delete;
    bool configure(const PianoChainConfig&) noexcept;
    bool process_block(const double* in_l, const double* in_r,
                       double* out_l, double* out_r, std::uint32_t frames) noexcept;
    // Explicit hard clear, not tail-preserving all-notes-off.
    void clear() noexcept;
    bool healthy() const noexcept;
    // Owner-only diagnostic state, for differential tests; no concurrent UI.
    std::array<double, 6> state() const noexcept;
    double current_highcut_hz() const noexcept; // audio-owner diagnostic only
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
