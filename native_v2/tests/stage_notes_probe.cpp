// Test adapter; I/O and containers are confined here, never StageNotes.
#include "stave/stage_notes.hpp"
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

struct Sink final : stave::StageNoteSink {
    struct Event { int layer, on, note; double velocity; };
    std::vector<Event> events;
    void osc_on(std::uint8_t n, double v, stave::LayerWeights) noexcept override { events.push_back({0, 1, n, v}); }
    void osc_off(std::uint8_t n) noexcept override { events.push_back({0, 0, n, 0}); }
    void piano_on(std::uint8_t n, double v) noexcept override { events.push_back({1, 1, n, v}); }
    void piano_off(std::uint8_t n) noexcept override { events.push_back({1, 0, n, 0}); }
    void all_notes_off() noexcept override { events.push_back({2, 0, 0, 0}); }
};

int main() {
    Sink sink;
    sink.events.reserve(512); // Maximum128 simultaneous releases *2, plus slack.
    stave::StageNotes notes(sink);
    std::string line;
    std::cout << std::setprecision(17);
    try {
        while (std::getline(std::cin, line)) {
            std::istringstream input(line);
            std::string command, extra;
            int a = 0, b = 0, c = 0;
            if (!(input >> command)) throw std::runtime_error("empty command");
            sink.events.clear();
            if (command == "config") {
                if (!(input >> a >> b >> c) || !notes.configure(a, b, c)) throw std::runtime_error("invalid configuration");
            } else if (command == "on") {
                if (!(input >> a >> b) || !notes.note_on(a, b)) throw std::runtime_error("invalid note-on");
            } else if (command == "off") {
                if (!(input >> a) || !notes.note_off(a)) throw std::runtime_error("invalid note-off");
            } else if (command == "sustain" || command == "sostenuto") {
                if (!(input >> a) || (a != 0 && a != 1)) throw std::runtime_error("invalid pedal");
                if (command == "sustain") notes.sustain(a != 0); else notes.sostenuto(a != 0);
            } else if (command == "reset") {
                notes.all_notes_off(); notes.configure(0, 0, 10); sink.events.clear();
            } else throw std::runtime_error("unknown command");
            if (input >> extra) throw std::runtime_error("extra command field");
            std::cout << "E";
            for (const auto& event : sink.events)
                std::cout << ' ' << event.layer << ',' << event.on << ',' << event.note << ',' << event.velocity;
            std::cout << " K";
            for (unsigned i = 0; i < 128; ++i) {
                const auto& key = notes.keys()[i];
                if (key.mapped || key.physical || key.sustained || key.captured)
                    std::cout << ' ' << i << ',' << key.mapped << ',' << key.physical << ',' << key.sustained
                              << ',' << key.captured << ',' << (key.mapped ? int(key.osc_note) : -1)
                              << ',' << (key.mapped ? int(key.piano_note) : -1);
            }
            std::cout << " P " << notes.sustain_down() << ',' << notes.sostenuto_down() << '\n';
        }
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
