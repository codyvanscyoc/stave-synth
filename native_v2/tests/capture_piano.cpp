// Device-free v1.2-format source fixture. Fluid library only: no audio driver.
#include <fluidsynth.h>
#include <array>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>

int main(int argc, char** argv) {
    try {
        if (argc != 2) throw std::runtime_error("capture_piano requires explicit local SoundFont");
        std::unique_ptr<fluid_settings_t, decltype(&delete_fluid_settings)> settings(new_fluid_settings(), delete_fluid_settings);
        if (!settings) throw std::runtime_error("settings allocation failed");
        auto integer = [&](const char* name, int value) {
            if (fluid_settings_setint(settings.get(), name, value) != FLUID_OK) throw std::runtime_error(name);
        };
        if (fluid_settings_setnum(settings.get(), "synth.sample-rate", 48000) != FLUID_OK ||
            fluid_settings_setnum(settings.get(), "synth.gain", 1) != FLUID_OK) throw std::runtime_error("numeric setting failed");
        integer("synth.polyphony", 32); integer("synth.dynamic-sample-loading", 0);
        integer("synth.reverb.active", 0); integer("synth.chorus.active", 0);
        integer("synth.cpu-cores", 1); integer("synth.lock-memory", 0);
        std::unique_ptr<fluid_synth_t, decltype(&delete_fluid_synth)> piano(new_fluid_synth(settings.get()), delete_fluid_synth);
        if (!piano) throw std::runtime_error("synth allocation failed");
        const int sfid = fluid_synth_sfload(piano.get(), argv[1], 0);
        if (sfid < 0 || fluid_synth_program_select(piano.get(), 0, sfid, 0, 0) != FLUID_OK)
            throw std::runtime_error("explicit piano asset/program unavailable");
        constexpr unsigned blocks = 512, frames = 512;
        std::array<short, 2 * frames> samples{};
        static_assert(sizeof(short) == 2, "16-bit Fluid output required");
        unsigned noteoff_misses = 0;
        for (unsigned block = 0; block < blocks; ++block) {
            // Six fixed triads: varying register/velocity, non-overlapping key
            // ownership, long ringing tails. Pedal-owner parity is separate.
            for (unsigned chord = 0; chord < 6; ++chord) {
                const unsigned onset = 8 + chord * 64;
                const int root = std::array<int, 6>{48, 60, 53, 57, 36, 72}[chord];
                const int velocity = std::array<int, 6>{85, 30, 105, 70, 120, 45}[chord];
                for (int interval : {0, 4, 7}) {
                    if (block == onset && fluid_synth_noteon(piano.get(), 0, root + interval, velocity) != FLUID_OK)
                        throw std::runtime_error("fixture note-on failed");
                    if (block == onset + 48 && fluid_synth_noteoff(piano.get(), 0, root + interval) != FLUID_OK)
                        ++noteoff_misses;
                }
            }
            if (fluid_synth_write_s16(piano.get(), frames, samples.data(), 0, 2,
                                      samples.data(), 1, 2) != FLUID_OK) throw std::runtime_error("int16 render failed");
            // Explicit little-endian stream independent of host representation.
            for (short sample : samples) {
                const auto bits = static_cast<std::uint16_t>(sample);
                const char bytes[] = {static_cast<char>(bits), static_cast<char>(bits >> 8)};
                std::cout.write(bytes, 2);
            }
        }
        if (!std::cout) throw std::runtime_error("capture write failed");
        std::cerr << "FluidSynth " << fluid_version_str() << "; frames=" << blocks * frames
                  << "; noteoff_misses=" << noteoff_misses << "; no audio/MIDI driver\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
