#include <stave/sound_backend.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
void require(bool result, const char* message) {
    if (!result) throw std::runtime_error(message);
}

stave::Event event(stave::EventType type, std::uint8_t note = 60,
                   std::uint8_t velocity = 100, float value = 0,
                   std::uint8_t channel = 0, std::uint64_t frame = 0) {
    return {frame, type, channel, note, velocity, value};
}

float peak(const std::vector<float>& samples) {
    float result = 0;
    for (float value : samples) {
        require(std::isfinite(value), "nonfinite exported sample");
        require(std::abs(value) <= 0.98f, "unbounded exported sample");
        result = std::max(result, std::abs(value));
    }
    return result;
}

void expect_bad_config(stave::SoundConfig config) {
    bool rejected = false;
    try { stave::SoundBackend backend(config); }
    catch (const std::exception&) { rejected = true; }
    require(rejected, "invalid sound configuration unexpectedly accepted");
}

void test_configuration() {
    stave::SoundConfig config;
    config.max_frames = 0;
    expect_bad_config(config);
    config.max_frames = 4097;
    expect_bad_config(config);
    config.max_frames = 512;
    config.osc_mix_gain = std::numeric_limits<float>::quiet_NaN();
    expect_bad_config(config);
    config.osc_mix_gain = .025f;
    config.soundfont_path = "/stave-native-v2-test-no-such-directory/missing.sf2";
    expect_bad_config(config);
}

void test_oscillator_notes_and_pedals() {
    stave::SoundBackend backend({});
    std::vector<float> left(512), right(512);
    backend.render(left.data(), right.data(), 512);
    require(peak(left) == 0 && peak(right) == 0, "startup is not silent");
    require(!backend.piano_enabled(), "unspecified piano was silently enabled");
    backend.handle(event(stave::EventType::NoteOn));
    backend.render(left.data(), right.data(), 512);
    require(peak(left) > .0001f, "reused Faust oscillator did not sound");
    backend.handle(event(stave::EventType::Sustain, 0, 0, 1));
    backend.handle(event(stave::EventType::NoteOff));
    for (int i = 0; i < 4; ++i) backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 1, "pedal did not hold note");
    require(peak(left) > .0001f, "pedal-held oscillator was silent");
    // Another channel's pedal cannot release channel zero's held note.
    backend.handle(event(stave::EventType::Sustain, 0, 0, 0, 1));
    backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 1, "cross-channel sustain release");
    backend.handle(event(stave::EventType::Sustain, 0, 0, 0));
    for (int i = 0; i < 4; ++i) backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 0, "pedal release leaked a slot");
    require(peak(left) < .000001f, "released oscillator did not decay");

    backend.handle(event(stave::EventType::NoteOn));
    backend.render(left.data(), right.data(), 31);
    backend.panic();
    backend.render(left.data(), right.data(), 512);
    require(peak(left) == 0 && peak(right) == 0, "oscillator STOP leaked audio");
    require(backend.active_oscillator_slots() == 0, "STOP leaked oscillator slots");
    backend.handle(event(stave::EventType::NoteOn));
    backend.handle(event(stave::EventType::NoteOn, 60, 0));
    backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 0, "velocity-zero note-on did not release");
    require(backend.healthy(), "normal oscillator fixture faulted");
}

void test_stealing_and_repeated_key_ownership() {
    stave::SoundBackend backend({});
    std::vector<float> left(512), right(512);
    for (int note = 60; note <= 72; ++note)
        backend.handle(event(stave::EventType::NoteOn, static_cast<std::uint8_t>(note)));
    require(backend.active_oscillator_slots() == 12, "voice capacity is not bounded");
    require(backend.stats().voice_steals == 1, "capacity crossing not reported");
    backend.handle(event(stave::EventType::NoteOn, 60));
    require(backend.stats().voice_steals == 2, "repeated-key allocation not bounded");
    // The first 60 was stolen. Its delayed release must not release the newer60.
    backend.handle(event(stave::EventType::NoteOff, 60));
    backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 12, "stolen-key noteoff killed new press");
    backend.handle(event(stave::EventType::NoteOff, 60));
    backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 11, "second press did not release");
    backend.handle(event(stave::EventType::NoteOff, 60));
    backend.render(left.data(), right.data(), 512);
    require(backend.active_oscillator_slots() == 11, "duplicate release changed ownership");
    require(backend.stats().clamped_samples == 0, "default maximum oscillator chord clipped");
    require(backend.healthy(), "voice fixture faulted");
}

void test_rejection_and_guard() {
    stave::SoundBackend backend({});
    backend.handle(event(stave::EventType::Osc1Blend, 0, 0,
                         std::numeric_limits<float>::quiet_NaN()));
    backend.handle(event(stave::EventType::Osc2Blend, 0, 0, -1));
    backend.handle(event(stave::EventType::Sustain, 0, 0, 2));
    backend.handle(event(stave::EventType::NoteOn, 255));
    backend.handle(event(stave::EventType::NoteOn, 60, 100, 0, 16));
    require(backend.stats().rejected_events == 5, "malformed input not rejected");
    require(backend.healthy(), "rejected input should not poison valid engine");
    std::array<float, 513> left{}, right{};
    left.fill(0.125f);
    right.fill(0.25f);
    backend.render(left.data(), right.data(), 513);
    require(!backend.healthy() && backend.stats().invalid_render_calls == 1,
            "invalid render contract did not latch fault");
    require(left.back() == .125f && right.back() == .25f, "invalid call wrote output");
    backend.render(left.data(), right.data(), 512);
    require(left.front() == 0 && right.front() == 0, "faulted backend was not silent");

    stave::SoundConfig loud;
    loud.osc_mix_gain = 1;
    stave::SoundBackend guarded(loud);
    for (int note = 60; note < 72; ++note)
        guarded.handle(event(stave::EventType::NoteOn, static_cast<std::uint8_t>(note), 127));
    // Existing Faust blend controls ramp from zero via si.smoo. A single
    // startup block may not reach overload even with an intentionally unsafe
    // mix gain; exercise the sustained chord after that real ramp settles.
    for (int block = 0; block < 32; ++block)
        guarded.render(left.data(), right.data(), 512);
    require(guarded.stats().clamped_samples > 0, "export clamp hid unreported overload");
}

std::vector<float> render_schedule(const stave::SoundConfig& config, std::uint32_t block) {
    stave::SoundBackend backend(config);
    stave::Engine engine(backend, config.sample_rate, block);
    const std::array<stave::Event, 11> events = {{
        event(stave::EventType::NoteOn, 60, 94, 0, 0, 37),
        event(stave::EventType::NoteOn, 64, 85, 0, 0, 137),
        event(stave::EventType::NoteOn, 67, 101, 0, 0, 511),
        event(stave::EventType::Sustain, 0, 0, 1, 0, 1000),
        event(stave::EventType::NoteOff, 60, 0, 0, 0, 1703),
        event(stave::EventType::NoteOff, 64, 0, 0, 0, 1703),
        event(stave::EventType::NoteOff, 67, 0, 0, 0, 1703),
        event(stave::EventType::Sustain, 0, 0, 0, 0, 3201),
        event(stave::EventType::Osc1Blend, 0, 0, .2f, 0, 4093),
        event(stave::EventType::NoteOn, 72, 100, 0, 1, 5000),
        event(stave::EventType::Panic, 0, 0, 0, 0, 13003),
    }};
    for (const auto& item : events) require(engine.enqueue(item), "schedule enqueue failed");
    std::vector<float> left(16384), right(16384), interleaved(32768);
    for (std::uint32_t frame = 0; frame < left.size(); frame += block) {
        require(engine.process(left.data() + frame, right.data() + frame,
                               std::min(block, static_cast<std::uint32_t>(left.size()) - frame)),
                "schedule render failed");
    }
    require(peak(left) > .0001f && peak(right) > .0001f, "scheduled output silent");
    for (std::uint32_t frame = 0; frame < 37; ++frame)
        require(left[frame] == 0 && right[frame] == 0, "audio before scheduled first note");
    for (std::uint32_t frame = 13003; frame < left.size(); ++frame)
        require(left[frame] == 0 && right[frame] == 0, "STOP leaked precomputed audio");
    require(backend.healthy() && backend.stats().fluid_errors == 0 &&
            backend.stats().clamped_samples == 0 && backend.stats().nonfinite_samples == 0,
            "scheduled backend reported sound faults");
    require(engine.counters().dispatched_events == events.size(), "schedule dropped events");
    for (std::size_t i = 0; i < left.size(); ++i) {
        interleaved[i * 2] = left[i];
        interleaved[i * 2 + 1] = right[i];
    }
    return interleaved;
}

void test_block_equivalence(const stave::SoundConfig& config, const char* label) {
    const auto a = render_schedule(config, 512);
    const auto b = render_schedule(config, 256);
    float delta = 0;
    for (std::size_t i = 0; i < a.size(); ++i) delta = std::max(delta, std::abs(a[i] - b[i]));
    require(delta <= 1e-6f, "outer block size changed scheduled audio");
    std::cout << label << " 512/256 schedule max_delta=" << delta << '\n';
}

void test_piano_stolen_noteoff(const std::string& soundfont) {
    stave::SoundConfig config;
    config.soundfont_path = soundfont;
    config.osc_mix_gain = config.piano_mix_gain = 0;
    stave::SoundBackend backend(config);
    std::array<float, 64> left{}, right{};
    // More distinct keys than the32-voice native piano profile can retain.
    // Releasing old/stolen keys is legitimate and must not panic the engine.
    for (std::uint8_t note = 21; note < 85; ++note) {
        backend.handle(event(stave::EventType::NoteOn, note));
        backend.render(left.data(), right.data(), 64);
    }
    for (std::uint8_t note = 21; note < 85; ++note)
        backend.handle(event(stave::EventType::NoteOff, note));
    require(backend.stats().fluid_noteoff_misses > 0, "piano stealing fixture did not exercise missing voices");
    require(backend.healthy() && backend.stats().fluid_errors == 0,
            "normal stolen-note release incorrectly faulted piano");
    backend.panic();
    backend.render(left.data(), right.data(), 64);
    require(backend.healthy(), "piano STOP failed after native stealing");
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc > 2) throw std::invalid_argument("usage: test_sound_backend [explicit.sf2]");
        test_configuration();
        test_oscillator_notes_and_pedals();
        test_stealing_and_repeated_key_ownership();
        test_rejection_and_guard();
        test_block_equivalence({}, "Existing Faust oscillator-only");
        if (argc == 2) {
            stave::SoundConfig config;
            config.soundfont_path = argv[1];
            test_block_equivalence(config, "Existing Faust plus native float piano");
            config.osc_mix_gain = 0;
            test_block_equivalence(config, "Native float piano-only");
            test_piano_stolen_noteoff(config.soundfont_path);
        } else {
            std::cout << "Piano NOT exercised: supply an explicit local SoundFont.\n";
        }
        std::cout << "Sound backend tests PASS (offline prototype; not V1 parity or Pi timing).\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << "Sound backend tests FAIL: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
