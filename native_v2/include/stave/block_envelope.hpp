#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace stave {

struct EnvelopeConfig {
    double attack_ms{200};
    double decay_ms{1500};
    double sustain_percent{80};
    double release_ms{500};

    bool valid() const noexcept {
        return std::isfinite(attack_ms) && attack_ms >= 0 && attack_ms <= 10000 &&
               std::isfinite(decay_ms) && decay_ms >= 0 && decay_ms <= 20000 &&
               std::isfinite(sustain_percent) && sustain_percent >= 0 && sustain_percent <= 100 &&
               std::isfinite(release_ms) && release_ms >= 0 && release_ms <= 30000;
    }
};

// Compatibility component, NOT a new sample-rate ADSR design. Reproduces the
// final sample, state and level of v1.2 ADSREnvelope.process(n) without creating
// a vector. That final sample is what the existing Faust gate consumes.
// Block-size-dependent attack/threshold behavior is intentionally retained.
// See the pinned Python differential oracle before changing these semantics.
class BlockEnvelope final {
public:
    enum class Stage : std::uint8_t { Attack, Decay, Sustain, Release, Off };

    explicit BlockEnvelope(std::uint32_t sample_rate = 48000,
                           EnvelopeConfig config = {})
        : sample_rate_(sample_rate), config_(config) {
        if (sample_rate < 8000 || sample_rate > 192000 || !config.valid())
            throw std::invalid_argument("Invalid envelope configuration");
    }

    bool configure(EnvelopeConfig config) noexcept {
        if (!config.valid()) return false;
        config_ = config; // Existing voices see live edits without retrigger.
        return true;
    }
    const EnvelopeConfig& config() const noexcept { return config_; }
    void trigger() noexcept { stage_ = Stage::Attack; } // Do not reset level.
    void release() noexcept { if (active()) stage_ = Stage::Release; }
    void clear() noexcept { stage_ = Stage::Off; level_ = 0; }
    bool active() const noexcept { return stage_ != Stage::Off; }
    Stage stage() const noexcept { return stage_; }
    double level() const noexcept { return level_; }

    // Invalid length leaves both state and caller's result untouched.
    bool advance(std::uint32_t frames, double& last_sample) noexcept {
        if (frames == 0 || frames > 4096) return false;
        switch (stage_) {
        case Stage::Off: last_sample = 0; return true;
        case Stage::Sustain:
            last_sample = level_ = config_.sustain_percent / 100;
            return true;
        case Stage::Attack: {
            const double rate = 1 / std::max(config_.attack_ms * sample_rate_ / 1000, 1.0);
            const double start = level_;
            const double end = start + rate * frames;
            if (end >= 1) {
                const auto attack_frames = std::min(
                    static_cast<std::uint32_t>((1 - start) / rate) + 1, frames);
                level_ = 1;
                stage_ = Stage::Decay;
                // numpy.linspace(start, 1, 1) returns start, not 1.
                last_sample = attack_frames == 1 ? start : 1;
                if (attack_frames < frames) decay(frames - attack_frames, last_sample);
            } else {
                level_ = end;
                last_sample = frames == 1 ? start : end;
            }
            return true;
        }
        case Stage::Decay: decay(frames, last_sample); return true;
        case Stage::Release: {
            const double rate = 1 / std::max(config_.release_ms * sample_rate_ / 1000, 1.0);
            last_sample = level_ *= std::pow(1 - rate, frames);
            if (level_ < .001) { level_ = 0; stage_ = Stage::Off; }
            return true;
        }
        }
        return false;
    }

private:
    void decay(std::uint32_t frames, double& last_sample) noexcept {
        const double sustain = config_.sustain_percent / 100;
        const double rate = 1 / std::max(config_.decay_ms * sample_rate_ / 1000, 1.0);
        last_sample = level_ = sustain + (level_ - sustain) * std::pow(1 - rate, frames);
        if (std::abs(level_ - sustain) < .001) {
            level_ = sustain;
            stage_ = Stage::Sustain;
        }
    }
    std::uint32_t sample_rate_;
    EnvelopeConfig config_;
    Stage stage_{Stage::Off};
    double level_{};
};

} // namespace stave
