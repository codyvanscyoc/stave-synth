// Offline differential-test adapter. stdin/stdout NEVER belong in audio code.
#include "stave/block_envelope.hpp"
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>

int main() {
    stave::BlockEnvelope envelope;
    std::string line;
    std::cout << std::setprecision(17);
    try {
        while (std::getline(std::cin, line)) {
            std::istringstream input(line);
            std::string command, extra;
            if (!(input >> command)) throw std::runtime_error("empty command");
            if (command == "reset") {
                unsigned rate;
                stave::EnvelopeConfig config;
                if (!(input >> rate >> config.attack_ms >> config.decay_ms >>
                      config.sustain_percent >> config.release_ms)) throw std::runtime_error("bad reset");
                envelope = stave::BlockEnvelope(rate, config);
            } else if (command == "config") {
                stave::EnvelopeConfig config;
                if (!(input >> config.attack_ms >> config.decay_ms >> config.sustain_percent >>
                      config.release_ms) || !envelope.configure(config)) throw std::runtime_error("bad config");
            } else if (command == "on") envelope.trigger();
            else if (command == "off") envelope.release();
            else if (command == "clear") envelope.clear();
            else if (command == "process") {
                unsigned frames;
                double value = -123;
                if (!(input >> frames) || !envelope.advance(frames, value)) throw std::runtime_error("bad frames");
                std::cout << value << ' ' << envelope.level() << ' '
                          << unsigned(envelope.stage()) << ' ' << envelope.active() << '\n';
            } else if (command == "guards") {
                const auto previous = envelope.level();
                const auto stage = envelope.stage();
                double value = -123;
                auto invalid = envelope.config();
                invalid.attack_ms = std::numeric_limits<double>::quiet_NaN();
                if (envelope.configure(invalid) || envelope.advance(0, value) ||
                    envelope.advance(4097, value) || value != -123 ||
                    envelope.level() != previous || envelope.stage() != stage)
                    throw std::runtime_error("validation changed state");
            } else throw std::runtime_error("unknown command");
            if (input >> extra) throw std::runtime_error("trailing command fields");
        }
        if (!std::cin.eof()) throw std::runtime_error("input failure");
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
