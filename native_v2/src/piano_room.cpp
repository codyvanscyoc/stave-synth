#include "stave/piano_room.hpp"
#include <faust/gui/CInterface.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>

extern "C" {
struct StavePianoRoom;
StavePianoRoom* newStavePianoRoom();
void deleteStavePianoRoom(StavePianoRoom*);
void initStavePianoRoom(StavePianoRoom*, int);
void instanceClearStavePianoRoom(StavePianoRoom*);
void buildUserInterfaceStavePianoRoom(StavePianoRoom*, UIGlue*);
void computeStavePianoRoom(StavePianoRoom*, int, float**, float**);
}

namespace stave {
namespace {
bool range(double x, double lo, double hi) noexcept {
    return std::isfinite(x) && x >= lo && x <= hi;
}
struct Zones { float* size{}; float* damp{}; };
void box(void*, const char*) {}
void end(void*) {}
void zone(void* p, const char* name, float* v) {
    auto& z = *static_cast<Zones*>(p);
    if (std::strcmp(name, "size") == 0) z.size = v;
    if (std::strcmp(name, "damp") == 0) z.damp = v;
}
void slider(void* p, const char* n, float* v, float, float, float, float) { zone(p, n, v); }
void bar(void*, const char*, float*, float, float) {}
void soundfile(void*, const char*, const char*, Soundfile**) {}
void declare(void*, float*, const char*, const char*) {}
} // namespace

bool PianoRoomConfig::valid() const noexcept {
    return range(wet, 0, 1) && range(size, 0, 1) && range(damp, 0, .99);
}

struct PianoRoom::Impl {
    std::uint32_t maximum_frames;
    PianoRoomConfig config;
    double wet_cur;
    // v1 begins with was_enabled true, even when saved settings disable it.
    bool was_enabled{true}, fault{};
    std::unique_ptr<StavePianoRoom, decltype(&deleteStavePianoRoom)> dsp{nullptr, deleteStavePianoRoom};
    Zones zones;
    std::array<std::array<double, 512>, 2> dry{};
    std::array<std::array<float, 512>, 2> input{}, wet{};
    std::array<float*, 2> inputs{input[0].data(), input[1].data()};
    std::array<float*, 2> outputs{wet[0].data(), wet[1].data()};

    Impl(std::uint32_t frames, const PianoRoomConfig& initial)
        : maximum_frames(frames), config(initial), wet_cur(initial.wet) {
        if ((frames != 256 && frames != 512) || !initial.valid())
            throw std::invalid_argument("Piano room requires 48 kHz, 256/512 maximum frames and valid controls");
        dsp.reset(newStavePianoRoom());
        if (!dsp) throw std::runtime_error("Piano room allocation failed");
        initStavePianoRoom(dsp.get(), 48000);
        UIGlue ui{&zones, box, box, box, end, zone, zone, slider, slider, slider,
                  bar, bar, soundfile, declare};
        buildUserInterfaceStavePianoRoom(dsp.get(), &ui);
        if (!zones.size || !zones.damp) throw std::runtime_error("Missing piano room controls");
        controls();
    }
    void controls() noexcept {
        *zones.size = static_cast<float>(config.size);
        *zones.damp = static_cast<float>(config.damp);
    }
    bool process(const double* l, const double* r, double* dl, double* dr, std::uint32_t n) noexcept {
        if (!l || !r || !dl || !dr || !n || n > maximum_frames) return false;
        const auto a = reinterpret_cast<std::uintptr_t>(dl), b = reinterpret_cast<std::uintptr_t>(dr);
        if ((a < b ? b - a : a - b) < n * sizeof(double)) return false;
        // Copy both channels before writing any destination (in-place safe).
        for (unsigned i = 0; i < n; ++i) {
            dry[0][i] = l[i]; dry[1][i] = r[i];
            // Prevent nonfinite/overflowed float inputs before invoking Faust.
            for (unsigned c = 0; c < 2; ++c)
                if (!std::isfinite(dry[c][i]) || std::abs(dry[c][i]) > std::numeric_limits<float>::max()) fault = true;
        }
        if (!fault) {
            if (!config.enabled && was_enabled) instanceClearStavePianoRoom(dsp.get());
            was_enabled = config.enabled;
            if (config.enabled && config.wet > .001) {
                const double alpha = 1 - std::exp(-static_cast<double>(n) / (.03 * 48000));
                wet_cur += alpha * (config.wet - wet_cur);
                for (unsigned c = 0; c < 2; ++c)
                    for (unsigned i = 0; i < n; ++i) input[c][i] = static_cast<float>(dry[c][i]);
                computeStavePianoRoom(dsp.get(), n, inputs.data(), outputs.data());
                for (unsigned c = 0; c < 2; ++c)
                    for (unsigned i = 0; i < n; ++i) {
                        dry[c][i] = dry[c][i] * (1 - wet_cur) + static_cast<double>(wet[c][i]) * wet_cur;
                        if (!std::isfinite(dry[c][i])) fault = true;
                    }
            }
        }
        if (fault) { std::fill_n(dl, n, 0); std::fill_n(dr, n, 0); return false; }
        std::copy_n(dry[0].data(), n, dl); std::copy_n(dry[1].data(), n, dr);
        return true;
    }
};
PianoRoom::PianoRoom(std::uint32_t frames, const PianoRoomConfig& config)
    : impl_(std::make_unique<Impl>(frames, config)) {}
PianoRoom::~PianoRoom() = default;
bool PianoRoom::configure(const PianoRoomConfig& config) noexcept {
    if (impl_->fault || !config.valid()) return false;
    impl_->config = config; impl_->controls(); return true;
}
bool PianoRoom::process_block(const double* l, const double* r, double* dl, double* dr, std::uint32_t n) noexcept {
    return impl_->process(l, r, dl, dr, n);
}
void PianoRoom::clear() noexcept { instanceClearStavePianoRoom(impl_->dsp.get()); }
bool PianoRoom::healthy() const noexcept { return !impl_->fault; }
double PianoRoom::current_wet() const noexcept { return impl_->wet_cur; }
} // namespace stave
