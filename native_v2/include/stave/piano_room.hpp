#pragma once
#include <cstdint>
#include <memory>

namespace stave {
struct PianoRoomConfig {
    bool enabled{true};
    double wet{.4}, size{.5}, damp{.6};
    bool valid() const noexcept;
};

// Offline downstream component, not a device/queue adapter. One owner calls
// configure/process/clear. Construct and destroy while that owner is stopped.
// Preserves v1: wet <= .001 freezes the tank; disable clears it on the next
// valid render. Neither action resets the wet smoother. This is NOT a click
// fix or a tail-preserving bypass. Use only once per complete source block.
class PianoRoom final {
public:
    explicit PianoRoom(std::uint32_t maximum_frames = 512,
                       const PianoRoomConfig& config = {});
    ~PianoRoom();
    PianoRoom(const PianoRoom&) = delete;
    PianoRoom& operator=(const PianoRoom&) = delete;
    bool configure(const PianoRoomConfig&) noexcept;
    bool process_block(const double* in_l, const double* in_r,
                       double* out_l, double* out_r, std::uint32_t frames) noexcept;
    // Hard tank clear; keeps controls/wet smoother and cannot clear a fault.
    void clear() noexcept;
    bool healthy() const noexcept;
    double current_wet() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
