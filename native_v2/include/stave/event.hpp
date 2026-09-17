#pragma once

#include <cstdint>

namespace stave {

enum class EventType : std::uint8_t {
    NoteOn,
    NoteOff,
    Sustain,
    Panic,
    Osc1Blend,
    Osc2Blend,
};

// Absolute audio-frame time, not wall-clock time. The single producer supplies
// nondecreasing timestamps; equal timestamps preserve submission order.
struct Event {
    std::uint64_t frame = 0;
    EventType type = EventType::Panic;
    std::uint8_t channel = 0;
    std::uint8_t note = 0;
    std::uint8_t velocity = 0;
    float value = 0.0F;
};

} // namespace stave
