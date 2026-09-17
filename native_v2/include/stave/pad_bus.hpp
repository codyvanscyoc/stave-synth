#pragma once
#include <array>
#include <cstdint>
#include <memory>

namespace stave {
struct PadBusConfig {
    double cutoff{8000}, resonance{.707}, range_min{150}, range_max{20000};
    bool slope24{}, shared1{true}, shared2{true};
    double independent1{20000}, independent2{20000}, highpass{20};
    double send1{1}, send2{1};
    bool bypass1{}, bypass2{};
    unsigned haas_samples{960};
    bool shimmer{}, shimmer_high{};
    double shimmer_mix{.5}, shimmer_send{1};
    bool valid() const noexcept;
};
// Supplied by the source owner for THIS block, not inferred from amplitude:
// osc2_audible comes from smoothed blend; voices_present from rendered voices;
// haas_active is the effective pan separation > .5 (including hard-pan).
struct PadBlockFlags {
    bool osc2_audible{true}, voices_present{}, haas_active{};
    // Only the fixed3-unison all-source-muted path: native DSP pauses while
    // scalar filters advance. Inputs must be exactly silent in this mode.
    bool native_active{true};
};

// Single-owner offline routing slice. Fixed48k/fixed256 or512 whole blocks.
// No global LFO, filter drift/wobble, ping-pong, reverb or master. Those are
// explicit integration gaps, NOT disabled controls in a replacement app.
// Five input stems: OSC1 L/R, OSC2 L/R, shimmer mono. Owned output channels:
// 0/1 filtered dry (post bypass carve); 2/3 reverb input (including shimmer);
// 4/5 dry FX-bypass; 6 shimmer mono; 7/8 cloud L/R diagnostics.
class PadBus final {
public:
    explicit PadBus(std::uint32_t frames = 512, const PadBusConfig& config = {});
    ~PadBus();
    PadBus(const PadBus&) = delete;
    PadBus& operator=(const PadBus&) = delete;
    bool configure(const PadBusConfig&) noexcept;
    bool process_block(const std::array<const double*, 5>& input, PadBlockFlags) noexcept;
    const double* stem(unsigned channel) const noexcept;
    // Full DSP re-init flushes CLOUD table too; scalar smoothers survive,
    // as in v1. clear() does not recover a terminal numerical fault.
    void clear() noexcept;
    bool healthy() const noexcept;
    std::uint32_t block_frames() const noexcept;
    // Owner-only diagnostic: main/independent1/2/HP/shimmer smoothers,
    // last-set cutoff/resonance, bypass ratio.
    std::array<double, 8> state() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
