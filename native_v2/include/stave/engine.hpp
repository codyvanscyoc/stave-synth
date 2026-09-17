#pragma once

#include "stave/event.hpp"

#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace stave {

// Backend state belongs exclusively to the audio/process owner after startup.
// Implementations must initialize resources before playing; noexcept alone does
// not establish allocation-free, lock-free, or bounded backend behavior.
class Backend {
public:
    virtual ~Backend() = default;
    virtual void render(float* left, float* right, std::uint32_t frames) noexcept = 0;
    virtual void handle(const Event& event) noexcept = 0;
    virtual void panic() noexcept = 0;
};

enum class Fault : std::uint8_t {
    None,
    InvalidConfiguration,
    QueueOverflow,
    FrameOverflow,
};

struct Counters {
    std::uint64_t accepted_events = 0;
    std::uint64_t dispatched_events = 0;
    std::uint64_t rejected_events = 0;
    std::uint64_t late_events = 0;
    std::uint64_t queue_overflows = 0;
    std::uint64_t invalid_blocks = 0;
    std::uint64_t completed_blocks = 0;
    std::uint64_t panic_calls = 0;
};

// Offline prototype scheduler: one enqueue owner, one process owner. A third
// thread may read atomic telemetry, but snapshots are not transactionally
// coherent. Not a JACK/ALSA driver; no physical audio, files, network, or clocks.
//
// Overflow is deliberately TERMINAL, not an attempted live queue reset. At the
// next process boundary it panics exactly once and returns silent output; a
// stopped owner must destroy/reconstruct the engine before reuse. Already
// queued events remain inaccessible. This avoids racing resets or losing a
// note-off and then silently resuming with stale notes.
class Engine final {
public:
    static constexpr std::size_t queue_capacity = 256;
    static constexpr std::uint32_t maximum_block = 512;

    // initialFrame seeds an absolute audio timeline, not a seek/reset API.
    // At any signed/external boundary, validate before converting to unsigned;
    // this typed API cannot distinguish a wrapped negative from a large frame.
    explicit Engine(Backend& backend, std::uint32_t sampleRate = 48000,
                    std::uint32_t maxBlock = 512,
                    std::uint64_t initialFrame = 0) noexcept
        : backend_(backend), sample_rate_(sampleRate), max_block_(maxBlock),
          frame_position_(initialFrame) {
        if (sampleRate == 0 || maxBlock == 0 || maxBlock > maximum_block) {
            fault_.store(Fault::InvalidConfiguration, std::memory_order_relaxed);
        }
    }

    Engine(const Engine&) = delete;
    Engine& operator=(const Engine&) = delete;

    // Rejected invalid/out-of-order input has no effect on queue/timeline.
    // A full queue latches a fault even if the consumer frees a slot just after
    // the acquire load. The caller must inspect failure, never retry blindly.
    bool enqueue(Event event) noexcept {
        if (fault() != Fault::None || !valid(event) ||
            (has_previous_event_ && event.frame < previous_event_frame_)) {
            rejected_events_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        const auto head = head_.load(std::memory_order_relaxed);
        const auto tail = tail_.load(std::memory_order_acquire);
        if (head - tail >= queue_capacity) {
            queue_overflows_.fetch_add(1, std::memory_order_relaxed);
            rejected_events_.fetch_add(1, std::memory_order_relaxed);
            latch(Fault::QueueOverflow);
            return false;
        }
        events_[head % queue_capacity] = event;
        previous_event_frame_ = event.frame;
        has_previous_event_ = true;
        accepted_events_.fetch_add(1, std::memory_order_relaxed);
        head_.store(head + 1, std::memory_order_release);
        return true;
    }

    // Caller supplies distinct, non-overlapping arrays with at least frames
    // samples each. Invalid arguments return false without touching buffers or
    // advancing time; an adapter must supply its own invalid-call silence.
    // Valid faulted calls write silence and return false, without time advance.
    // Enqueues published after the initial head snapshot wait until the next
    // block. This bounds event work to <=256 dispatches per process call.
    bool process(float* left, float* right, std::uint32_t frames) noexcept {
        if (left == nullptr || right == nullptr || left == right || frames == 0 ||
            frames > max_block_ || frames > maximum_block) {
            invalid_blocks_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        const auto left_address = reinterpret_cast<std::uintptr_t>(left);
        const auto right_address = reinterpret_cast<std::uintptr_t>(right);
        const auto separation = left_address < right_address
                                    ? right_address - left_address
                                    : left_address - right_address;
        if (separation < frames * sizeof(float)) {
            invalid_blocks_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        if (fault() != Fault::None) {
            return silence_fault(left, right, frames);
        }
        const auto start = frame_position_.load(std::memory_order_relaxed);
        if (frames > std::numeric_limits<std::uint64_t>::max() - start) {
            latch(Fault::FrameOverflow);
            return silence_fault(left, right, frames);
        }
        const auto end = start + frames;
        const auto head = head_.load(std::memory_order_acquire);
        auto tail = tail_.load(std::memory_order_relaxed);
        auto cursor = start;
        while (tail != head) {
            // Copy before releasing tail: producer may immediately reuse slot.
            const Event event = events_[tail % queue_capacity];
            if (event.frame >= end) {
                break; // An event exactly at end belongs to the next block.
            }
            if (fault() != Fault::None) {
                return silence_fault(left, right, frames);
            }
            const auto target = event.frame < cursor ? cursor : event.frame;
            if (event.frame < start) {
                late_events_.fetch_add(1, std::memory_order_relaxed);
            }
            if (target != cursor) {
                const auto offset = static_cast<std::uint32_t>(cursor - start);
                backend_.render(left + offset, right + offset,
                                static_cast<std::uint32_t>(target - cursor));
                cursor = target;
            }
            if (event.type == EventType::Panic) {
                backend_.panic();
                panic_calls_.fetch_add(1, std::memory_order_relaxed);
            } else {
                backend_.handle(event);
            }
            ++tail;
            tail_.store(tail, std::memory_order_release);
            dispatched_events_.fetch_add(1, std::memory_order_relaxed);
        }
        if (cursor != end) {
            const auto offset = static_cast<std::uint32_t>(cursor - start);
            backend_.render(left + offset, right + offset,
                            static_cast<std::uint32_t>(end - cursor));
        }
        // A concurrent overflow during rendering invalidates this entire block.
        // An overflow after this check is handled at the next process boundary.
        if (fault() != Fault::None) {
            return silence_fault(left, right, frames);
        }
        frame_position_.store(end, std::memory_order_release);
        completed_blocks_.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    std::uint64_t frame_position() const noexcept {
        return frame_position_.load(std::memory_order_acquire);
    }
    std::uint32_t sample_rate() const noexcept { return sample_rate_; }
    std::uint32_t max_block() const noexcept { return max_block_; }
    Fault fault() const noexcept { return fault_.load(std::memory_order_acquire); }

    Counters counters() const noexcept {
        return {accepted_events_.load(std::memory_order_relaxed),
                dispatched_events_.load(std::memory_order_relaxed),
                rejected_events_.load(std::memory_order_relaxed),
                late_events_.load(std::memory_order_relaxed),
                queue_overflows_.load(std::memory_order_relaxed),
                invalid_blocks_.load(std::memory_order_relaxed),
                completed_blocks_.load(std::memory_order_relaxed),
                panic_calls_.load(std::memory_order_relaxed)};
    }

private:
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free,
                  "This prototype requires lock-free 64-bit atomics");
    static_assert(std::atomic<Fault>::is_always_lock_free,
                  "This prototype requires lock-free fault atomics");

    static bool valid(const Event& event) noexcept {
        if (event.channel > 15 || event.note > 127 || event.velocity > 127 ||
            !std::isfinite(event.value)) {
            return false;
        }
        switch (event.type) {
        case EventType::NoteOn:
        case EventType::NoteOff:
        case EventType::Panic:
            return true;
        case EventType::Sustain:
        case EventType::Osc1Blend:
        case EventType::Osc2Blend:
            return event.value >= 0.0F && event.value <= 1.0F;
        }
        return false;
    }

    void latch(Fault fault) noexcept {
        auto expected = Fault::None;
        fault_.compare_exchange_strong(expected, fault, std::memory_order_release,
                                       std::memory_order_relaxed);
    }

    bool silence_fault(float* left, float* right, std::uint32_t frames) noexcept {
        if (!fault_panicked_) {
            backend_.panic();
            panic_calls_.fetch_add(1, std::memory_order_relaxed);
            fault_panicked_ = true;
        }
        for (std::uint32_t i = 0; i != frames; ++i) {
            left[i] = 0.0F;
            right[i] = 0.0F;
        }
        return false;
    }

    Backend& backend_;
    const std::uint32_t sample_rate_;
    const std::uint32_t max_block_;
    std::array<Event, queue_capacity> events_{};
    alignas(64) std::atomic<std::uint64_t> head_{0};
    alignas(64) std::atomic<std::uint64_t> tail_{0};
    std::uint64_t previous_event_frame_ = 0; // enqueue-owner only
    bool has_previous_event_ = false;       // enqueue-owner only
    bool fault_panicked_ = false;           // process-owner only
    std::atomic<Fault> fault_{Fault::None};
    std::atomic<std::uint64_t> frame_position_{0};
    std::atomic<std::uint64_t> accepted_events_{0};
    std::atomic<std::uint64_t> dispatched_events_{0};
    std::atomic<std::uint64_t> rejected_events_{0};
    std::atomic<std::uint64_t> late_events_{0};
    std::atomic<std::uint64_t> queue_overflows_{0};
    std::atomic<std::uint64_t> invalid_blocks_{0};
    std::atomic<std::uint64_t> completed_blocks_{0};
    std::atomic<std::uint64_t> panic_calls_{0};
};

} // namespace stave
