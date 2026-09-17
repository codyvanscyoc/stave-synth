// Offline integration fixture only. No audio/MIDI driver or application state.
#include "stave/engine.hpp"
#include "stave/sound_backend.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {
void u16(std::FILE* out, std::uint16_t value) {
    const char bytes[] = {static_cast<char>(value), static_cast<char>(value >> 8)};
    std::fwrite(bytes, 1, 2, out);
}
void u32(std::FILE* out, std::uint32_t value) {
    const char bytes[] = {static_cast<char>(value), static_cast<char>(value >> 8),
                          static_cast<char>(value >> 16), static_cast<char>(value >> 24)};
    std::fwrite(bytes, 1, 4, out);
}
void write_wav(const std::string& path, const std::vector<float>& audio) {
    // IEEE float WAV, stereo48k. File writes happen outside all process calls.
    // Exclusive creation also refuses unreadable existing files and symlinks.
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) throw std::runtime_error("cannot exclusively create new WAV");
    std::unique_ptr<std::FILE, decltype(&std::fclose)> stream(::fdopen(fd, "wb"), std::fclose);
    if (!stream) { ::close(fd); throw std::runtime_error("cannot open WAV stream"); }
    auto* out = stream.get();
    const auto bytes = static_cast<std::uint32_t>(audio.size() * sizeof(float));
    std::fwrite("RIFF", 1, 4, out); u32(out, bytes + 48); std::fwrite("WAVEfmt ", 1, 8, out);
    u32(out, 16); u16(out, 3); u16(out, 2); u32(out, 48000);
    u32(out, 48000 * 8); u16(out, 8); u16(out, 32);
    std::fwrite("fact", 1, 4, out); u32(out, 4); u32(out, static_cast<std::uint32_t>(audio.size() / 2));
    std::fwrite("data", 1, 4, out); u32(out, bytes);
    for (float sample : audio) {
        std::uint32_t bits = 0;
        static_assert(sizeof(bits) == sizeof(sample), "32bit float required");
        std::memcpy(&bits, &sample, sizeof(bits));
        u32(out, bits);
    }
    const bool write_failed = std::ferror(out) != 0;
    const int close_result = std::fclose(stream.release());
    if (write_failed || close_result != 0) throw std::runtime_error("WAV write failed");
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc < 3 || argc > 4) throw std::runtime_error("usage: render_demo NEW.wav 256|512 [explicit.sf2]");
        const std::string block_arg = argv[2];
        if (block_arg != "256" && block_arg != "512") throw std::runtime_error("block must be256 or512");
        const std::uint32_t block = block_arg == "256" ? 256 : 512;
        stave::SoundConfig config;
        config.max_frames = block;
        if (argc == 4) config.soundfont_path = argv[3];
        stave::SoundBackend sound(config);
        stave::Engine engine(sound, 48000, block);
        auto event = [&](std::uint64_t frame, stave::EventType type, std::uint8_t note = 0,
                         std::uint8_t velocity = 0, float value = 0) {
            if (!engine.enqueue({frame, type, 0, note, velocity, value}))
                throw std::runtime_error("fixture event rejected");
        };
        event(0, stave::EventType::Osc1Blend, 0, 0, .35F);
        event(0, stave::EventType::Osc2Blend, 0, 0, .20F);
        // Non-block-aligned attacks explicitly exercise event slicing.
        for (int chord = 0; chord < 3; ++chord) {
            const std::uint64_t onset = 12001 + static_cast<std::uint64_t>(chord) * 96000;
            const std::uint8_t root = static_cast<std::uint8_t>(chord == 0 ? 60 : chord == 1 ? 57 : 53);
            event(onset, stave::EventType::Sustain, 0, 0, 1);
            for (auto offset : {0, 7, 12})
                event(onset + 17, stave::EventType::NoteOn, static_cast<std::uint8_t>(root + offset), 84);
            for (auto offset : {0, 7, 12})
                event(onset + 36000, stave::EventType::NoteOff, static_cast<std::uint8_t>(root + offset));
            event(onset + 64000, stave::EventType::Sustain, 0, 0, 0);
        }
        event(7 * 48000, stave::EventType::Panic);
        constexpr std::uint32_t total_frames = 8 * 48000;
        std::array<float, 512> left{}, right{};
        std::vector<float> audio(static_cast<std::size_t>(total_frames) * 2);
        double square_sum = 0;
        float peak = 0;
        for (std::uint32_t position = 0; position < total_frames; position += block) {
            if (!engine.process(left.data(), right.data(), block)) throw std::runtime_error("engine fault");
            for (std::uint32_t i = 0; i < block; ++i) {
                for (unsigned ch = 0; ch < 2; ++ch) {
                    const float sample = ch == 0 ? left[i] : right[i];
                    if (!std::isfinite(sample)) throw std::runtime_error("nonfinite fixture audio");
                    audio[2 * (position + i) + ch] = sample;
                    peak = std::max(peak, std::abs(sample));
                    square_sum += static_cast<double>(sample) * sample;
                }
            }
        }
        const auto counters = engine.counters();
        const auto stats = sound.stats();
        if (!sound.healthy() || peak <= 1e-7F || counters.accepted_events != counters.dispatched_events ||
            counters.rejected_events || counters.late_events || stats.fluid_errors ||
            stats.nonfinite_samples || stats.clamped_samples || stats.rejected_events || stats.invalid_render_calls)
            throw std::runtime_error("fixture health/output check failed");
        write_wav(argv[1], audio);
        std::cout << "{\"prototype\":true,\"live_driver\":false,\"piano\":"
                  << (sound.piano_enabled() ? "true" : "false")
                  << ",\"block_frames\":" << block << ",\"frames\":" << total_frames
                  << ",\"events\":" << counters.dispatched_events << ",\"peak\":" << peak
                  << ",\"rms\":" << std::sqrt(square_sum / audio.size())
                  << ",\"fluid_noteoff_misses\":" << stats.fluid_noteoff_misses
                  << ",\"v1_parity\":false,\"pi4_timing_qualified\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
