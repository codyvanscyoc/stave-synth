#pragma once
#include "stave/lookahead_limiter.hpp"
#include <memory>

namespace stave {
enum class SidechainSource { Self, Piano, Lfo, Bpm };
struct MasterBand { double frequency{1000},gain{},q{1.5}; };
struct MasterConfig {
    std::array<MasterBand,3> eq{{{200,0,1.5},{1000,0,1.5},{5000,0,1.5}}};
    bool highpass{}; double cutoff{80}; int slope{12};
    double space{},pre_gain{1.5}; bool saturation{};
    bool compression{},fx_bypass{},native_self{true},release_auto{true};
    SidechainSource sidechain{SidechainSource::Self};
    double threshold{-10},ratio{4},attack{3},release{300},knee{2},makeup{},mix{1},sc_highpass{100};
    bool valid() const noexcept;
};
struct MasterModulation {
    double bpm{120},lfo_depth{},lfo_value{};
    bool valid() const noexcept;
};
// Fixed48k whole-block owner. Six pad channels use PadAmbience order:
// mixed L/R, dry L/R, FX L/R. Optional piano is already-prepared dry piano/organ.
// Stereo output is post-limiter, PRE bridge float32/master fader/device gain.
// No runtime, driver, warming noise, reset from other threads or I/O.
class StageMaster final {
public:
    explicit StageMaster(unsigned frames=512,LimiterPolicy policy=LimiterPolicy::CeilingSafe);
    ~StageMaster();
    StageMaster(const StageMaster&)=delete;
    StageMaster& operator=(const StageMaster&)=delete;
    bool configure(const MasterConfig&) noexcept;
    bool process(const std::array<const double*,6>& pad,const std::array<const double*,2>* piano=nullptr,
                 MasterModulation modulation={}) noexcept;
    // Caller has already qualified the triggering note and retrigger setting.
    bool retrigger_bpm() noexcept;
    void reset_limiter() noexcept; // Matches the old master's local panic slice.
    void stop() noexcept;
    bool healthy() const noexcept;
    const double* channel(unsigned) const noexcept;
    unsigned block_frames() const noexcept;
    // Owner-only: limiter gain, fallback GR, BPM phase/remainder, space cache.
    std::array<double,5> state() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
