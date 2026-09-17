#pragma once
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace stave {
// Browser/stage-side values, unlike StagePatch's prepared linear amplitudes.
struct SourceMixConfig {
    double fader1{.6}, fader2{.4}, pan1{}, pan2{};
    bool hard_pan{}, shimmer{}, shimmer_high{};
    double shimmer_mix{.5};
    bool valid() const noexcept {
        const auto in = [](double x, double lo, double hi) { return std::isfinite(x) && x >= lo && x <= hi; };
        return in(fader1,0,1) && in(fader2,0,1) && in(pan1,-1,1) && in(pan2,-1,1) && in(shimmer_mix,0,1);
    }
};
struct PreparedSourceMix {
    double amplitude1{}, amplitude2{}, pan1{}, pan2{};
    bool render_osc1{}, render_osc2{}, render_shimmer{}, skip_voices{}, haas_active{}, shimmer_high{};
};
// Exact v1 scalar preprocessing, independent of DSP/event ownership. Call once
// per complete block, not per MIDI slice. This exposes the skip decision but
// does NOT implement the old all-muted Faust/fallback transition itself.
class SourceMix final {
public:
    explicit SourceMix(std::uint32_t frames = 512) : frames_(frames) {
        if (frames != 256 && frames != 512) throw std::invalid_argument("Source mix needs fixed256/512 at48k");
    }
    bool configure(const SourceMixConfig& config) noexcept {
        if (!config.valid()) return false;
        config_ = config; return true;
    }
    PreparedSourceMix advance() noexcept {
        const double alpha = 1 - std::exp(-static_cast<double>(frames_) / (.005 * 48000));
        current_[0] += alpha * (config_.fader1 - current_[0]);
        current_[1] += alpha * (config_.fader2 - current_[1]);
        if (config_.fader1 <= 0 && current_[0] < .005) current_[0] = 0;
        if (config_.fader2 <= 0 && current_[1] < .005) current_[1] = 0;
        PreparedSourceMix result;
        result.amplitude1 = amplitude(current_[0]); result.amplitude2 = amplitude(current_[1]);
        result.pan1 = config_.hard_pan ? -1 : config_.pan1;
        result.pan2 = config_.hard_pan ? 1 : config_.pan2;
        result.render_osc1 = result.amplitude1 > 0; result.render_osc2 = result.amplitude2 > 0;
        result.render_shimmer = config_.shimmer && config_.shimmer_mix > .001;
        result.skip_voices = !result.render_osc1 && !result.render_osc2 && !result.render_shimmer;
        result.haas_active = std::abs(result.pan1 - result.pan2) > .5;
        result.shimmer_high = config_.shimmer_high;
        return result;
    }
    std::array<double,2> state() const noexcept { return current_; }
private:
    static double amplitude(double fader) noexcept {
        return fader <= 0 ? 0 : std::pow(10., (fader - 1) * 24 / 20);
    }
    std::uint32_t frames_;
    SourceMixConfig config_;
    std::array<double,2> current_{.6,.4};
};
} // namespace stave
