#pragma once
#include "stave/pad_bus.hpp"
#include "stave/piano_room.hpp"

namespace stave {
struct StageBusConfig {
    PadBusConfig pad{};
    PianoRoomConfig room{};
    bool valid() const noexcept { return pad.valid() && room.valid(); }
};
// Offline downstream composition, single owner, no driver/event adapter.
// Consumes StageSources' seven stems. Outputs nine PadBus stems followed by
// room-processed piano L/R (9/10). These are INTERNAL buses, not a master mix.
// Caller supplies block flags explicitly until source/control integration.
// Inputs must not alias owned outputs; invalid pointers/aliases are refused
// without changing state or outputs. The source owner retains input storage.
class StageBuses final {
public:
    explicit StageBuses(std::uint32_t frames = 512) : frames_(frames), pad_(frames), room_(frames) {}
    bool configure(const StageBusConfig& config) noexcept {
        if (!healthy() || !config.valid()) return false;
        return pad_.configure(config.pad) && room_.configure(config.room);
    }
    bool process_block(const std::array<const double*, 7>& input, PadBlockFlags flags) noexcept {
        for (const auto* p : input) if (!p) return false;
        for (const auto* p : input) {
            const auto a = reinterpret_cast<std::uintptr_t>(p);
            for (unsigned c = 0; c < 11; ++c) {
                const auto b = reinterpret_cast<std::uintptr_t>(stem(c));
                if ((a < b ? b - a : a - b) < frames_ * sizeof(double)) return false;
            }
        }
        if (!healthy()) { stop(); return false; }
        if (!pad_.process_block({input[0], input[1], input[2], input[3], input[4]}, flags) ||
            !room_.process_block(input[5], input[6], piano_[0].data(), piano_[1].data(), frames_)) {
            stop(); return false;
        }
        return true;
    }
    const double* stem(unsigned channel) const noexcept {
        if (channel < 9) return pad_.stem(channel);
        return channel < 11 ? piano_[channel - 9].data() : nullptr;
    }
    // Hard DSP clear retains scalar smoothing. It is not a release command.
    void clear() noexcept { pad_.clear(); room_.clear(); for (auto& c : piano_) c.fill(0); }
    void stop() noexcept { if (!stopped_) { stopped_ = true; clear(); } }
    bool healthy() const noexcept { return !stopped_ && pad_.healthy() && room_.healthy(); }
    std::array<double, 9> state() const noexcept {
        const auto p = pad_.state();
        return {p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], room_.current_wet()};
    }
private:
    std::uint32_t frames_;
    PadBus pad_;
    PianoRoom room_;
    bool stopped_{};
    std::array<std::array<double, 512>, 2> piano_{};
};
} // namespace stave
