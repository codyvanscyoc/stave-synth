#include "stave/engine.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
#include <thread>

namespace {
std::atomic<bool> count_allocations{false};
std::atomic<std::uint64_t> allocation_count{0};

void* allocate(std::size_t bytes) {
    if (count_allocations.load(std::memory_order_relaxed)) {
        allocation_count.fetch_add(1, std::memory_order_relaxed);
    }
    if (void* pointer = std::malloc(bytes == 0 ? 1 : bytes)) {
        return pointer;
    }
    throw std::bad_alloc();
}

void require(bool condition, const char* expression, int line) {
    if (!condition) {
        std::fprintf(stderr, "FAIL line %d: %s\n", line, expression);
        std::abort();
    }
}
#define CHECK(expression) require((expression), #expression, __LINE__)

using stave::Engine;
using stave::Event;
using stave::EventType;
using stave::Fault;

struct Seen {
    Event event{};
    std::uint64_t observed_frame = 0;
};

class Probe final : public stave::Backend {
public:
    std::array<Seen, 1024> seen{};
    std::size_t count = 0;
    std::uint64_t cursor = 0;
    std::uint64_t renders = 0;
    std::uint64_t panics = 0;
    float level = 0.0F;
    Engine* inject_engine = nullptr;
    bool injected = false;
    bool overflow_during_render = false;
    bool stop_during_render = false;

    void render(float* left, float* right, std::uint32_t frames) noexcept override {
        ++renders;
        if (inject_engine != nullptr && !injected) {
            injected = true;
            if (stop_during_render) {
                inject_engine->request_stop();
            } else if (overflow_during_render) {
                for (std::size_t i = 0; i != Engine::queue_capacity; ++i) {
                    CHECK(inject_engine->enqueue({100000, EventType::NoteOff}));
                }
                CHECK(!inject_engine->enqueue({100000, EventType::NoteOff}));
            } else {
                CHECK(inject_engine->enqueue({cursor, EventType::NoteOn, 0, 60, 7}));
            }
        }
        for (std::uint32_t i = 0; i != frames; ++i) {
            left[i] = level;
            right[i] = -level;
        }
        cursor += frames;
    }

    void handle(const Event& event) noexcept override {
        CHECK(count < seen.size());
        seen[count++] = {event, cursor};
        switch (event.type) {
        case EventType::NoteOn: level = static_cast<float>(event.velocity); break;
        case EventType::NoteOff: level = 0.0F; break;
        case EventType::Osc1Blend: level += event.value; break;
        case EventType::Osc2Blend: level -= event.value; break;
        case EventType::Sustain: level += event.value * 2.0F; break;
        case EventType::Panic: CHECK(false); break; // Engine dispatches panic().
        }
    }

    void panic() noexcept override {
        ++panics;
        level = 0.0F;
    }
};

void sample_placement_and_boundaries() {
    Probe probe;
    Engine engine(probe);
    std::array<float, 512> left{}, right{};
    CHECK(engine.enqueue({0, EventType::NoteOn, 0, 60, 9}));
    CHECK(engine.enqueue({3, EventType::NoteOff, 0, 60}));
    CHECK(engine.enqueue({3, EventType::NoteOn, 0, 61, 4}));
    CHECK(engine.enqueue({256, EventType::NoteOff, 0, 61}));
    CHECK(engine.enqueue({511, EventType::NoteOn, 0, 62, 2}));
    CHECK(engine.enqueue({512, EventType::Panic}));
    CHECK(engine.process(left.data(), right.data(), 256));
    CHECK(probe.count == 3);
    CHECK(probe.seen[0].observed_frame == 0);
    CHECK(probe.seen[1].observed_frame == 3);
    CHECK(probe.seen[2].observed_frame == 3);
    CHECK(probe.seen[1].event.type == EventType::NoteOff);
    for (std::size_t i = 0; i != 256; ++i) {
        CHECK(left[i] == (i < 3 ? 9.0F : 4.0F));
        CHECK(right[i] == -left[i]);
    }
    CHECK(engine.process(left.data(), right.data(), 256));
    CHECK(probe.count == 5);
    CHECK(probe.seen[3].observed_frame == 256);
    CHECK(probe.seen[4].observed_frame == 511);
    for (std::size_t i = 0; i != 256; ++i) {
        CHECK(left[i] == (i == 255 ? 2.0F : 0.0F));
    }
    CHECK(probe.panics == 0);
    CHECK(engine.process(left.data(), right.data(), 1));
    CHECK(left[0] == 0.0F);
    CHECK(probe.panics == 1);
    CHECK(engine.frame_position() == 513);
    CHECK(engine.counters().dispatched_events == 6);
    CHECK(engine.counters().late_events == 0);
}

void block_size_equivalence() {
    Probe small_probe, large_probe;
    Engine small(small_probe, 48000, 256), large(large_probe, 48000, 512);
    constexpr std::array<Event, 9> events{{
        {1, EventType::NoteOn, 0, 60, 6},
        {255, EventType::Osc1Blend, 0, 0, 0, 0.5F},
        {256, EventType::Sustain, 0, 0, 0, 1.0F},
        {511, EventType::NoteOff, 0, 60},
        {512, EventType::NoteOn, 1, 64, 3},
        {777, EventType::Osc2Blend, 0, 0, 0, 0.25F},
        {1024, EventType::Panic},
        {1500, EventType::NoteOn, 0, 60, 12},
        {1900, EventType::NoteOff, 0, 60},
    }};
    for (const auto& event : events) {
        CHECK(small.enqueue(event));
        CHECK(large.enqueue(event));
    }
    std::array<float, 2048> small_left{}, small_right{}, large_left{}, large_right{};
    for (std::size_t offset = 0; offset != small_left.size(); offset += 256) {
        CHECK(small.process(small_left.data() + offset, small_right.data() + offset, 256));
    }
    for (std::size_t offset = 0; offset != large_left.size(); offset += 512) {
        CHECK(large.process(large_left.data() + offset, large_right.data() + offset, 512));
    }
    CHECK(small_left == large_left);
    CHECK(small_right == large_right);
    CHECK(small_probe.count == large_probe.count);
    for (std::size_t i = 0; i != small_probe.count; ++i) {
        CHECK(small_probe.seen[i].observed_frame == large_probe.seen[i].observed_frame);
    }
    CHECK(small.counters().dispatched_events == events.size());
    CHECK(small.counters().late_events == 0);
    CHECK(large.counters().late_events == 0);
}

void late_future_and_validation() {
    Probe probe;
    Engine engine(probe);
    std::array<float, 512> left{}, right{};
    CHECK(engine.process(left.data(), right.data(), 16));
    CHECK(engine.enqueue({4, EventType::NoteOn, 0, 60, 8}));
    CHECK(!engine.enqueue({3, EventType::NoteOff, 0, 60}));
    CHECK(engine.enqueue({900, EventType::NoteOff, 0, 60}));
    CHECK(engine.process(left.data(), right.data(), 16));
    CHECK(probe.count == 1);
    CHECK(probe.seen[0].event.frame == 4);
    CHECK(probe.seen[0].observed_frame == 16);
    CHECK(engine.counters().late_events == 1);
    CHECK(!engine.enqueue({1000, static_cast<EventType>(255)}));
    CHECK(!engine.enqueue({1000, EventType::NoteOn, 16, 60, 1}));
    CHECK(!engine.enqueue({1000, EventType::NoteOn, 0, 128, 1}));
    CHECK(!engine.enqueue({1000, EventType::NoteOn, 0, 60, 128}));
    CHECK(!engine.enqueue({1000, EventType::Osc1Blend, 0, 0, 0, -0.1F}));
    CHECK(!engine.enqueue({1000, EventType::Osc2Blend, 0, 0, 0, 1.1F}));
    CHECK(!engine.enqueue({1000, EventType::Sustain, 0, 0, 0,
                           std::numeric_limits<float>::quiet_NaN()}));
    CHECK(!engine.enqueue({1000, EventType::NoteOff, 0, 60, 0,
                           std::numeric_limits<float>::infinity()}));
    CHECK(engine.counters().rejected_events == 9);
    CHECK(engine.fault() == Fault::None);
    CHECK(!engine.process(nullptr, right.data(), 16));
    CHECK(!engine.process(left.data(), nullptr, 16));
    CHECK(!engine.process(left.data(), left.data(), 16));
    CHECK(!engine.process(left.data(), left.data() + 1, 16));
    CHECK(!engine.process(left.data(), right.data(), 0));
    CHECK(!engine.process(left.data(), right.data(), 513));
    CHECK(engine.counters().invalid_blocks == 6);
    CHECK(engine.frame_position() == 32);
    CHECK(engine.process(left.data(), right.data(), 512));
    CHECK(probe.count == 1); // Future event remains queued across blocks.
    CHECK(engine.process(left.data(), right.data(), 512));
    CHECK(probe.count == 2);
    CHECK(probe.seen[1].observed_frame == 900);

    Probe invalid_probe;
    Engine invalid_rate(invalid_probe, 0);
    CHECK(invalid_rate.fault() == Fault::InvalidConfiguration);
    CHECK(!invalid_rate.enqueue({}));
    CHECK(!invalid_rate.process(left.data(), right.data(), 16));
    CHECK(invalid_probe.panics == 1);
    Engine invalid_block(invalid_probe, 48000, 513);
    CHECK(invalid_block.fault() == Fault::InvalidConfiguration);
    Engine zero_block(invalid_probe, 48000, 0);
    CHECK(zero_block.fault() == Fault::InvalidConfiguration);
}

void overflow_fault_and_reconstruction() {
    Probe probe;
    Engine engine(probe);
    std::array<float, 512> left{}, right{};
    probe.level = 11.0F;
    for (std::size_t i = 0; i != Engine::queue_capacity; ++i) {
        CHECK(engine.enqueue({0, EventType::NoteOn, 0, 60, 1}));
    }
    CHECK(!engine.enqueue({0, EventType::NoteOff, 0, 60}));
    CHECK(engine.fault() == Fault::QueueOverflow);
    CHECK(probe.panics == 0); // Producer does not touch backend state.
    CHECK(!engine.process(left.data(), right.data(), 512));
    CHECK(probe.panics == 1);
    CHECK(probe.count == 0);
    CHECK(probe.level == 0.0F);
    CHECK(!engine.enqueue({1, EventType::NoteOn, 0, 60, 1}));
    CHECK(!engine.process(left.data(), right.data(), 512));
    CHECK(probe.panics == 1);
    for (std::size_t i = 0; i != left.size(); ++i) {
        CHECK(left[i] == 0.0F && right[i] == 0.0F);
    }
    CHECK(engine.frame_position() == 0);
    CHECK(engine.counters().queue_overflows == 1);
    CHECK(engine.counters().panic_calls == 1);
    // New, stopped-owner construction cannot expose the faulted queue's notes.
    Engine replacement(probe);
    CHECK(replacement.process(left.data(), right.data(), 512));
    CHECK(probe.count == 0);
}

void frame_overflow_is_terminal() {
    Probe probe;
    constexpr auto initial = std::numeric_limits<std::uint64_t>::max() - 8;
    Engine engine(probe, 48000, 512, initial);
    probe.cursor = initial;
    std::array<float, 512> left{}, right{};
    CHECK(engine.process(left.data(), right.data(), 8));
    CHECK(engine.frame_position() == std::numeric_limits<std::uint64_t>::max());
    CHECK(!engine.process(left.data(), right.data(), 1));
    CHECK(engine.fault() == Fault::FrameOverflow);
    CHECK(probe.panics == 1);
    CHECK(!engine.process(left.data(), right.data(), 1));
    CHECK(probe.panics == 1);
}

void snapshot_work_and_mid_render_fault() {
    Probe probe;
    Engine engine(probe);
    probe.inject_engine = &engine;
    std::array<float, 512> left{}, right{};
    CHECK(engine.process(left.data(), right.data(), 512));
    CHECK(probe.count == 0); // Published during render: next block, never a loop.
    CHECK(engine.process(left.data(), right.data(), 512));
    CHECK(probe.count == 1);
    CHECK(probe.seen[0].observed_frame == 512);
    CHECK(engine.counters().late_events == 1);

    Probe overflow_probe;
    Engine overflow_engine(overflow_probe);
    overflow_probe.inject_engine = &overflow_engine;
    overflow_probe.overflow_during_render = true;
    overflow_probe.level = 10.0F;
    CHECK(!overflow_engine.process(left.data(), right.data(), 512));
    CHECK(overflow_engine.fault() == Fault::QueueOverflow);
    CHECK(overflow_probe.panics == 1);
    CHECK(overflow_engine.frame_position() == 0);
    for (std::size_t i = 0; i != left.size(); ++i) {
        CHECK(left[i] == 0.0F && right[i] == 0.0F);
    }
}

void no_engine_heap_allocation() {
    Probe probe;
    Engine engine(probe);
    std::array<float, 512> left{}, right{};
    // Calibrate interception so a disabled/broken hook cannot silently pass.
    allocation_count.store(0, std::memory_order_relaxed);
    count_allocations.store(true, std::memory_order_relaxed);
    void* calibration = ::operator new(64);
    count_allocations.store(false, std::memory_order_relaxed);
    CHECK(allocation_count.load(std::memory_order_relaxed) == 1);
    ::operator delete(calibration);
    allocation_count.store(0, std::memory_order_relaxed);
    count_allocations.store(true, std::memory_order_relaxed);
    for (std::uint64_t frame = 0; frame != 128; ++frame) {
        CHECK(engine.enqueue({frame * 2, EventType::NoteOn, 0, 60, 1}));
    }
    CHECK(engine.process(left.data(), right.data(), 256));
    CHECK(engine.process(left.data(), right.data(), 512));
    CHECK(!engine.enqueue({0, EventType::Sustain, 0, 0, 0, 2.0F}));
    for (std::size_t i = 0; i != Engine::queue_capacity; ++i) {
        CHECK(engine.enqueue({1000, EventType::NoteOff, 0, 60}));
    }
    CHECK(!engine.enqueue({1000, EventType::NoteOff, 0, 60}));
    CHECK(!engine.process(left.data(), right.data(), 512));
    count_allocations.store(false, std::memory_order_relaxed);
    CHECK(allocation_count.load(std::memory_order_relaxed) == 0);
}

void priority_stop_ignores_future_queue() {
    Probe probe;
    Engine engine(probe);
    std::array<float, 512> left{}, right{};
    probe.level = 5;
    CHECK(engine.enqueue({100000, EventType::NoteOn, 0, 60, 100}));
    engine.request_stop();
    CHECK(engine.fault() == Fault::StopRequested);
    CHECK(probe.panics == 0);
    CHECK(!engine.process(left.data(), right.data(), 512));
    CHECK(probe.panics == 1 && probe.count == 0);
    CHECK(!engine.enqueue({100001, EventType::NoteOn, 0, 64, 100}));
    engine.request_stop();
    CHECK(!engine.process(left.data(), right.data(), 512));
    CHECK(probe.panics == 1);
    for (unsigned i = 0; i < 512; ++i) CHECK(left[i] == 0 && right[i] == 0);

    Probe during;
    Engine active(during);
    during.inject_engine = &active;
    during.stop_during_render = true;
    CHECK(active.enqueue({5, EventType::NoteOn, 0, 60, 100}));
    CHECK(!active.process(left.data(), right.data(), 512));
    CHECK(during.count == 0); // Stop during pre-event render prevents note-on.
    CHECK(during.panics == 1);
    CHECK(active.frame_position() == 0);
    for (unsigned i = 0; i < 512; ++i) CHECK(left[i] == 0 && right[i] == 0);
}

class ConcurrentProbe final : public stave::Backend {
public:
    std::atomic<std::uint64_t> received{0};
    std::uint64_t panics = 0;
    void render(float* left, float* right, std::uint32_t frames) noexcept override {
        for (std::uint32_t i = 0; i != frames; ++i) {
            left[i] = right[i] = 0.0F;
        }
    }
    void handle(const Event& event) noexcept override {
        const auto expected = received.load(std::memory_order_relaxed);
        CHECK(event.value == static_cast<float>(expected));
        CHECK(event.note == expected % 128);
        received.store(expected + 1, std::memory_order_release);
    }
    void panic() noexcept override { ++panics; }
};

void concurrent_spsc_ordering() {
    constexpr std::uint64_t total = 20000;
    ConcurrentProbe probe;
    Engine engine(probe);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    std::thread producer([&] {
        for (std::uint64_t i = 0; i != total; ++i) {
            // Test-only producer pacing: substantial slack below queue capacity.
            // No wait or retry is present inside Engine::enqueue/process.
            while (i - probe.received.load(std::memory_order_acquire) >= 128) {
                CHECK(std::chrono::steady_clock::now() < deadline);
                std::this_thread::yield();
            }
            CHECK(engine.enqueue({0, EventType::NoteOn, 0,
                                   static_cast<std::uint8_t>(i % 128), 1,
                                   static_cast<float>(i)}));
        }
    });
    std::array<float, 256> left{}, right{};
    while (probe.received.load(std::memory_order_acquire) != total) {
        CHECK(std::chrono::steady_clock::now() < deadline);
        CHECK(engine.process(left.data(), right.data(), 256));
        std::this_thread::yield();
    }
    producer.join();
    CHECK(engine.counters().accepted_events == total);
    CHECK(engine.counters().dispatched_events == total);
    CHECK(engine.counters().queue_overflows == 0);
    CHECK(probe.panics == 0);
}
} // namespace

// The fixed probe makes these allocation checks specifically about the engine.
// They do not certify the future FluidSynth/Faust backend or interception of C
// malloc calls. Construction/thread startup are outside the guarded region.
void* operator new(std::size_t bytes) { return allocate(bytes); }
void* operator new[](std::size_t bytes) { return allocate(bytes); }
void operator delete(void* pointer) noexcept { std::free(pointer); }
void operator delete[](void* pointer) noexcept { std::free(pointer); }
void operator delete(void* pointer, std::size_t) noexcept { std::free(pointer); }
void operator delete[](void* pointer, std::size_t) noexcept { std::free(pointer); }

int main() {
    std::fputs("test: sample placement\n", stderr);
    sample_placement_and_boundaries();
    std::fputs("test: block equivalence\n", stderr);
    block_size_equivalence();
    std::fputs("test: validation\n", stderr);
    late_future_and_validation();
    std::fputs("test: queue overflow\n", stderr);
    overflow_fault_and_reconstruction();
    std::fputs("test: frame overflow\n", stderr);
    frame_overflow_is_terminal();
    std::fputs("test: snapshot bounds\n", stderr);
    snapshot_work_and_mid_render_fault();
    std::fputs("test: priority stop\n", stderr);
    priority_stop_ignores_future_queue();
    std::fputs("test: heap allocation\n", stderr);
    no_engine_heap_allocation();
    std::fputs("test: concurrent SPSC\n", stderr);
    concurrent_spsc_ordering();
    std::puts("PASS: native engine scheduling, bounds, fault safety, allocation, SPSC");
}
