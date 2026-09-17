#include "stave/piano_chain.hpp"
#define FAUSTFLOAT double
#include <faust/gui/CInterface.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

extern "C" {
struct StavePianoChain;
StavePianoChain* newStavePianoChain();
void deleteStavePianoChain(StavePianoChain*);
void initStavePianoChain(StavePianoChain*, int);
void buildUserInterfaceStavePianoChain(StavePianoChain*, UIGlue*);
void computeStavePianoChain(StavePianoChain*, int, double**, double**);
}

namespace stave {
namespace {
constexpr double pi = 3.14159265358979323846;
bool range(double value, double low, double high) noexcept {
    return std::isfinite(value) && value >= low && value <= high;
}
struct Zones {
    struct Entry { const char* name{}; double* value{}; };
    std::array<Entry, 32> entries{};
    std::size_t count{};
    bool overflow{};
    double* require(const char* name) const {
        for (std::size_t i = 0; i < count; ++i)
            if (std::strcmp(name, entries[i].name) == 0) return entries[i].value;
        throw std::runtime_error(std::string("Missing piano zone: ") + name);
    }
};
void box(void*, const char*) {}
void end(void*) {}
void zone(void* opaque, const char* name, double* value) {
    auto& zones = *static_cast<Zones*>(opaque);
    if (zones.count == zones.entries.size()) { zones.overflow = true; return; }
    zones.entries[zones.count++] = {name, value};
}
void slider(void* o, const char* n, double* v, double, double, double, double) { zone(o, n, v); }
void bar(void*, const char*, double*, double, double) {}
void soundfile(void*, const char*, const char*, Soundfile**) {}
void declare(void*, double*, const char*, const char*) {}

struct Lowpass {
    double b0{}, b1{}, b2{}, a1{}, a2{}, s1{}, s2{};
    void tune(double cutoff, double sample_rate) noexcept {
        cutoff = std::clamp(cutoff, 20.0, sample_rate * .45);
        const double omega = 2 * pi * cutoff / sample_rate;
        const double cosine = std::cos(omega), alpha = std::sin(omega) / (2 * .707);
        const double a0 = 1 + alpha;
        b0 = ((1 - cosine) / 2) / a0;
        b1 = (1 - cosine) / a0;
        b2 = b0;
        a1 = (-2 * cosine) / a0;
        a2 = (1 - alpha) / a0;
    }
    double tick(double x) noexcept {
        const double y = b0 * x + s1;
        s1 = b1 * x - a1 * y + s2;
        s2 = b2 * x - a2 * y;
        return y;
    }
};
} // namespace

bool PianoChainConfig::valid() const noexcept {
    if (!range(volume, 0, 1) || !range(lowcut_hz, 20, 2000) || !range(highcut_hz, 200, 20000) ||
        !range(comp_threshold_db, -40, 0) || !range(comp_ratio, 1, 20) ||
        !range(comp_attack_ms, .5, 200) || !range(comp_release_ms, 5, 2000) ||
        !range(comp_makeup_db, 0, 24) || !range(comp_knee_db, 0, 24) ||
        !range(comp_drive_db, -12, 12) || !range(comp_wet, 0, 1) ||
        !range(brightness_amount, 0, 1) || !range(velocity_target, 0, 1) ||
        !range(tremolo_hz, 0, 20) || !range(tremolo_depth, 0, 1)) return false;
    for (const auto& band : eq)
        if (!range(band.frequency, 20, 20000) || !range(band.gain_db, -18, 18) ||
            !range(band.q, .1, 10)) return false;
    return true;
}

struct PianoChain::Impl {
    std::uint32_t sample_rate, maximum_frames;
    PianoChainConfig config;
    std::unique_ptr<StavePianoChain, decltype(&deleteStavePianoChain)> dsp{nullptr, deleteStavePianoChain};
    std::array<std::vector<double>, 2> input;
    std::array<std::vector<double>, 3> output;
    std::array<double*, 2> inputs{};
    std::array<double*, 3> outputs{};
    double *gain_zone{}, *lowcut_zone{}, *highcut_zone{};
    std::array<std::array<double*, 4>, 4> eq_zones{};
    double volume_cur{}, comp_envelope{}, previous_gain{}, velocity_cur{.7}, last_cutoff{18000}, phase{};
    bool previous_gain_valid{}, fault{};
    Lowpass brightness_l, brightness_r;

    Impl(std::uint32_t rate, std::uint32_t frames, const PianoChainConfig& initial)
        : sample_rate(rate), maximum_frames(frames), config(initial), volume_cur(initial.volume) {
        if (rate < 8000 || rate > 192000 || frames == 0 || frames > 512 || !initial.valid())
            throw std::invalid_argument("Invalid piano chain configuration");
        for (unsigned i = 0; i < input.size(); ++i) { input[i].resize(frames); inputs[i] = input[i].data(); }
        for (unsigned i = 0; i < output.size(); ++i) { output[i].resize(frames); outputs[i] = output[i].data(); }
        dsp.reset(newStavePianoChain());
        if (!dsp) throw std::runtime_error("Piano DSP allocation failed");
        initStavePianoChain(dsp.get(), rate);
        Zones zones;
        UIGlue ui{&zones, box, box, box, end, zone, zone, slider, slider, slider,
                  bar, bar, soundfile, declare};
        buildUserInterfaceStavePianoChain(dsp.get(), &ui);
        if (zones.overflow) throw std::runtime_error("Piano zone inventory overflow");
        gain_zone = zones.require("piano_gain");
        lowcut_zone = zones.require("lowcut_hz"); highcut_zone = zones.require("highcut_hz");
        for (unsigned i = 0; i < 4; ++i) {
            unsigned index = 0;
            for (const char* suffix : {"freq", "gain", "q", "on"}) {
                const std::string name = "eq" + std::to_string(i) + "_" + suffix;
                eq_zones[i][index++] = zones.require(name.c_str());
            }
        }
        brightness_l.tune(18000, rate); brightness_r.tune(18000, rate);
    }

    bool process(const double* l, const double* r, double* dest_l, double* dest_r,
                 std::uint32_t frames) noexcept {
        if (!l || !r || !dest_l || !dest_r || frames == 0 || frames > maximum_frames) return false;
        const auto a = reinterpret_cast<std::uintptr_t>(dest_l), b = reinterpret_cast<std::uintptr_t>(dest_r);
        if ((a < b ? b - a : a - b) < frames * sizeof(double)) return false;
        for (unsigned i = 0; i < frames; ++i) {
            if (!std::isfinite(l[i]) || !std::isfinite(r[i])) fault = true;
            input[0][i] = l[i]; input[1][i] = r[i];
        }
        if (fault) { std::fill_n(dest_l, frames, 0); std::fill_n(dest_r, frames, 0); return false; }
        const double alpha = 1 - std::exp(-static_cast<double>(frames) / (.01 * sample_rate));
        volume_cur += alpha * (config.volume - volume_cur);
        *gain_zone = volume_cur <= .001 ? 0 : std::pow(10.0, (volume_cur - 1) * 40 / 20);
        *lowcut_zone = config.lowcut_hz; *highcut_zone = config.highcut_hz;
        for (unsigned i = 0; i < 4; ++i) {
            *eq_zones[i][0] = config.eq[i].frequency; *eq_zones[i][1] = config.eq[i].gain_db;
            *eq_zones[i][2] = config.eq[i].q; *eq_zones[i][3] = config.eq[i].enabled ? 1 : 0;
        }
        computeStavePianoChain(dsp.get(), frames, inputs.data(), outputs.data());
        if (config.comp_enabled && config.comp_wet > .001) {
            const double drive = std::pow(10.0, config.comp_drive_db / 20);
            const double makeup = std::pow(10.0, config.comp_makeup_db / 20);
            double sum = 0;
            for (unsigned i = 0; i < frames; ++i) { const double x = output[2][i] * drive; sum += x * x; }
            const double rms = std::sqrt(sum / frames);
            const double milliseconds = rms > comp_envelope ? std::max(1.0, config.comp_attack_ms)
                                                            : std::max(1.0, config.comp_release_ms);
            comp_envelope += (1 - std::exp(-static_cast<double>(frames) / (milliseconds * .001 * sample_rate))) * (rms - comp_envelope);
            const double delta = 20 * std::log10(std::max(comp_envelope, 1e-10)) - config.comp_threshold_db;
            const double knee = std::max(.1, config.comp_knee_db), half = knee * .5;
            const double slope = 1 - 1 / std::max(1.0, config.comp_ratio);
            double reduction = 0;
            if (delta > half) reduction = delta * slope;
            else if (delta > -half) reduction = slope * ((delta + half) * (delta + half)) / (2 * knee);
            const double gain = drive * std::pow(10.0, -reduction / 20) * makeup;
            const double before = previous_gain_valid ? previous_gain : gain;
            const double step = frames > 1 ? (gain - before) / (frames - 1) : 0;
            for (unsigned i = 0; i < frames; ++i) {
                const double ramp = frames > 1 && i == frames - 1 ? gain : before + i * step;
                const double mix = (1 - config.comp_wet) + ramp * config.comp_wet;
                output[0][i] *= mix; output[1][i] *= mix;
            }
            previous_gain = gain; previous_gain_valid = true;
        }
        if (config.brightness_enabled && config.brightness_amount > .001) {
            velocity_cur += (1 - std::exp(-static_cast<double>(frames) / (.05 * sample_rate))) * (config.velocity_target - velocity_cur);
            const double floor = 18000 - config.brightness_amount * 16500;
            const double cutoff = floor + (18000 - floor) * std::clamp(velocity_cur, 0.0, 1.0);
            if (std::abs(cutoff - last_cutoff) > 10) {
                brightness_l.tune(cutoff, sample_rate); brightness_r.tune(cutoff, sample_rate); last_cutoff = cutoff;
            }
            for (unsigned i = 0; i < frames; ++i) {
                output[0][i] = brightness_l.tick(output[0][i]); output[1][i] = brightness_r.tick(output[1][i]);
            }
        }
        if (config.tremolo_depth > 1e-4 && config.tremolo_hz > 0) {
            const double step = config.tremolo_hz / sample_rate, depth = config.tremolo_depth;
            for (unsigned i = 0; i < frames; ++i) {
                const double time = phase + i * step;
                output[0][i] *= (1 - depth) + depth * (.5 + .5 * std::sin(2 * pi * time));
                output[1][i] *= (1 - depth) + depth * (.5 + .5 * std::sin(2 * pi * (time + .5)));
            }
            phase = std::fmod(phase + (frames - 1) * step + step, 1.0);
        }
        for (unsigned i = 0; i < frames; ++i) {
            if (!std::isfinite(output[0][i]) || !std::isfinite(output[1][i])) fault = true;
        }
        if (fault) { std::fill_n(dest_l, frames, 0); std::fill_n(dest_r, frames, 0); return false; }
        std::copy_n(output[0].data(), frames, dest_l); std::copy_n(output[1].data(), frames, dest_r);
        return true;
    }
};

PianoChain::PianoChain(std::uint32_t rate, std::uint32_t frames, const PianoChainConfig& config)
    : impl_(std::make_unique<Impl>(rate, frames, config)) {}
PianoChain::~PianoChain() = default;
bool PianoChain::configure(const PianoChainConfig& config) noexcept {
    if (!config.valid() || impl_->fault) return false;
    impl_->config = config; return true;
}
bool PianoChain::process_block(const double* l, const double* r, double* out_l, double* out_r, std::uint32_t frames) noexcept {
    return impl_->process(l, r, out_l, out_r, frames);
}
void PianoChain::clear() noexcept {
    initStavePianoChain(impl_->dsp.get(), impl_->sample_rate);
    impl_->volume_cur = impl_->config.volume; impl_->comp_envelope = 0; impl_->previous_gain = 0;
    impl_->previous_gain_valid = false; impl_->velocity_cur = .7; impl_->phase = 0; impl_->last_cutoff = 18000;
    impl_->brightness_l = {}; impl_->brightness_r = {};
    impl_->brightness_l.tune(18000, impl_->sample_rate); impl_->brightness_r.tune(18000, impl_->sample_rate);
    // Numeric faults remain terminal; clear is not permission to hide one.
}
bool PianoChain::healthy() const noexcept { return !impl_->fault; }
std::array<double, 6> PianoChain::state() const noexcept {
    return {impl_->volume_cur, impl_->comp_envelope, impl_->previous_gain, impl_->velocity_cur,
            impl_->last_cutoff, impl_->phase};
}
} // namespace stave
