#include "stave/stage_voices.hpp"
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>

struct Sink final : stave::StageVoiceSink {
    void key_trigger() noexcept override { std::cout << " key"; }
    void start_slot(unsigned slot) noexcept override { std::cout << " new," << slot; }
    void clear_slot(unsigned slot) noexcept override { std::cout << " clear," << slot; }
    void gate(unsigned slot, std::uint8_t note, double a, double b, double shimmer) noexcept override {
        std::cout << " gate," << slot << ',' << unsigned(note) << ',' << a << ',' << b << ',' << shimmer;
    }
};
// Test-only text I/O sink. Never used as an audio-thread adapter.
int main() {
    Sink sink;
    std::unique_ptr<stave::StageVoices> owner;
    std::cout << std::setprecision(17);
    std::string line;
    try {
      while (std::getline(std::cin, line)) {
        std::istringstream input(line);
        std::string op, extra;
        if (!(input >> op)) return 2;
        std::cout << 'E';
        bool ok = true;
        if (op == "reset") {
            unsigned rate{}, limit{};
            if (!(input >> rate >> limit)) return 4;
            owner = std::make_unique<stave::StageVoices>(sink, rate, limit);
        } else if (!owner) return 2;
        else if (op == "config") {
            stave::EnvelopeConfig a, b;
            if (!(input >> a.attack_ms >> a.decay_ms >> a.sustain_percent >> a.release_ms
                  >> b.attack_ms >> b.decay_ms >> b.sustain_percent >> b.release_ms)) return 4;
            ok = owner->configure(a, b);
        } else if (op == "on") {
            int note{}; double velocity{}; stave::LayerWeights weights;
            if (!(input >> note >> velocity >> weights.osc1 >> weights.osc2 >> weights.shimmer)) return 4;
            ok = owner->note_on(note, velocity, weights);
        } else if (op == "off") {
            int note{}; if (!(input >> note)) return 4; ok = owner->note_off(note);
        } else if (op == "all") ok = owner->release_all();
        else if (op == "begin") {
            unsigned frames{}; int skip{};
            if (!(input >> frames >> skip) || (skip != 0 && skip != 1)) return 4;
            ok = owner->begin_block(frames, skip != 0);
        } else if (op == "end") ok = owner->end_block();
        else return 3;
        if (!input || !ok) return 4;
        if (input >> extra) return 4;
        std::cout << " V";
        for (unsigned i = 0; i < owner->size(); ++i) {
            const auto& v = *owner->voice_at(i);
            std::cout << ' ' << owner->slot_at(i) << ',' << unsigned(v.note) << ',' << v.velocity << ','
                << v.age << ',' << int(v.env1.stage()) << ',' << v.env1.level() << ','
                << int(v.env2.stage()) << ',' << v.env2.level() << ',' << v.weights.osc1 << ','
                << v.weights.osc2 << ',' << v.weights.shimmer;
        }
        std::cout << '\n';
      }
      if (!std::cin.eof()) return 4;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; return 1;
    }
}
