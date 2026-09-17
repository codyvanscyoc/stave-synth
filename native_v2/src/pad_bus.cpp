#include "stave/pad_bus.hpp"
#include "stave/stage_motion.hpp"
#define FAUSTFLOAT double
#include <faust/gui/CInterface.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <string>

extern "C" {
struct StavePadBus;
StavePadBus* newStavePadBus();
void deleteStavePadBus(StavePadBus*);
void initStavePadBus(StavePadBus*, int);
void buildUserInterfaceStavePadBus(StavePadBus*, UIGlue*);
void computeStavePadBus(StavePadBus*, int, double**, double**);
}
namespace stave {
namespace {
bool range(double v, double low, double high) noexcept { return std::isfinite(v) && v >= low && v <= high; }
struct Zones {
    struct Entry { const char* name{}; double* value{}; };
    std::array<Entry, 80> entries{};
    unsigned count{};
    bool overflow{};
    double* require(const char* name) const {
        for (unsigned i = 0; i < count; ++i)
            if (std::strcmp(name, entries[i].name) == 0) return entries[i].value;
        throw std::runtime_error(std::string("Missing pad bus zone: ") + name);
    }
};
void box(void*, const char*) {}
void end(void*) {}
void zone(void* p, const char* n, double* v) {
    auto& z = *static_cast<Zones*>(p);
    if (z.count == z.entries.size()) { z.overflow = true; return; }
    z.entries[z.count++] = {n, v};
}
void slider(void* p, const char* n, double* v, double, double, double, double) { zone(p, n, v); }
void bar(void*, const char*, double*, double, double) {}
void soundfile(void*, const char*, const char*, Soundfile**) {}
void declare(void*, double*, const char*, const char*) {}
double smooth_log(double current, double target, double alpha) noexcept {
    double value = std::log(std::max(current, 20.0));
    value += alpha * (std::log(std::max(target, 20.0)) - value);
    return std::exp(value);
}
enum Zone { Cutoff, Resonance, Slope24, Position, Shared1, Shared2, Indep1, Indep2,
            Pos1, Pos2, HpOn, HpCutoff, Send1, Send2, SendActive, HaasOn, HaasSamples,
            Osc2Audible, ShimmerActive, ShimmerHp, ShimmerMix, ShimmerSend, ZoneCount };
constexpr std::array<const char*, ZoneCount> names{
    "cutoff_hz", "filter_resonance", "filter_slope24", "f_pos", "osc1_filter_enabled", "osc2_filter_enabled",
    "osc1_indep_cutoff", "osc2_indep_cutoff", "osc1_f_pos", "osc2_f_pos", "filter_highpass_on", "filter_highpass_hz",
    "osc1_reverb_send", "osc2_reverb_send", "send_filter_active", "haas_on", "haas_delay_samps", "osc2_audible",
    "shimmer_active", "shimmer_hp_hz", "shimmer_mix_cur", "shimmer_send"};
constexpr std::array<const char*, 9> static_names{"shim_ring_len", "shim_tap_l1", "shim_tap_l2", "shim_tap_l3",
    "shim_tap_l4", "shim_tap_r1", "shim_tap_r2", "shim_tap_r3", "shim_tap_r4"};
// int(seconds*48000) from the existing Python wrapper. .289 truncates to13871.
constexpr std::array<double, 9> static_values{28800, 6240, 11856, 17424, 23088, 8304, 13871, 19440, 25104};
}

bool PadBusConfig::valid() const noexcept {
    return range(cutoff, 20, 20000) && range(resonance, .1, 10) &&
        range(range_min, 20, 20000) && range(range_max, 20, 20000) &&
        range(independent1, 20, 20000) && range(independent2, 20, 20000) &&
        range(highpass, 20, 5000) && range(send1, 0, 2) && range(send2, 0, 2) &&
        haas_samples <= 4095 && range(shimmer_mix, 0, 1) && range(shimmer_send, 0, 2);
}
struct PadBus::Impl {
    std::uint32_t frames;
    PadBusConfig config;
    bool fault{}, retuned{};
    std::unique_ptr<StavePadBus, decltype(&deleteStavePadBus)> dsp{nullptr, deleteStavePadBus};
    std::array<double*, ZoneCount> zones{};
    std::array<double*, 9> statics{};
    std::array<std::array<double, 512>, 5> input{};
    std::array<std::array<double, 512>, 9> output{};
    std::array<double*, 5> inputs{};
    std::array<double*, 9> outputs{};
    double cutoff_cur{8000}, indep1_cur{20000}, indep2_cur{20000}, hp_cur{20}, shimmer_cur{.5};
    double cutoff_set{-1}, resonance_set{-1}, bypass_ratio{};
    Impl(std::uint32_t n, const PadBusConfig& initial) : frames(n), config(initial) {
        if ((n != 256 && n != 512) || !initial.valid()) throw std::invalid_argument("Invalid pad bus configuration");
        dsp.reset(newStavePadBus());
        if (!dsp) throw std::runtime_error("Pad bus allocation failed");
        initStavePadBus(dsp.get(), 48000);
        Zones collected;
        UIGlue ui{&collected, box, box, box, end, zone, zone, slider, slider, slider, bar, bar, soundfile, declare};
        buildUserInterfaceStavePadBus(dsp.get(), &ui);
        if (collected.overflow) throw std::runtime_error("Pad zone inventory overflow");
        for (unsigned i = 0; i < zones.size(); ++i) zones[i] = collected.require(names[i]);
        for (unsigned i = 0; i < statics.size(); ++i) statics[i] = collected.require(static_names[i]);
        for (unsigned i = 0; i < inputs.size(); ++i) inputs[i] = input[i].data();
        for (unsigned i = 0; i < outputs.size(); ++i) outputs[i] = output[i].data();
        push_static();
    }
    void push_static() noexcept { for (unsigned i = 0; i < statics.size(); ++i) *statics[i] = static_values[i]; }
    void silence() noexcept { for (auto& channel : output) channel.fill(0); }
    void clear() noexcept { initStavePadBus(dsp.get(), 48000); push_static(); silence(); }
    bool process(const std::array<const double*, 5>& source, PadBlockFlags flags, StageMotion* motion, FilterMotion* filter) noexcept {
        for (const auto* p : source) if (!p) return false; // invalid call: leave state/output intact
        if(motion&&(!filter||motion->block_frames()!=frames)) return false;
        if (fault) { silence(); return false; }
        for (unsigned c = 0; c < input.size(); ++c)
            for (unsigned i = 0; i < frames; ++i) {
                input[c][i] = source[c][i];
                if (!std::isfinite(input[c][i])) fault = true;
                if (!flags.native_active && input[c][i] != 0) fault = true;
            }
        if (fault) { silence(); return false; }
        const auto& p = config;
        const double alpha = 1 - std::exp(-static_cast<double>(frames) / (.08 * 48000));
        cutoff_cur = smooth_log(cutoff_cur, p.cutoff, alpha);
        // One clock advance, then filter walks, preserving random draw order.
        if(motion&&!motion->process(flags.native_active)) { fault=true; silence(); return false; }
        double effective = std::clamp(cutoff_cur, 20.0, 20000.0);
        if(filter) {
            if(!filter->process(cutoff_cur,motion?motion->filter_modulation():0,p.resonance)) { fault=true; silence(); return false; }
            effective=filter->state()[0];
        }
        const bool invalidated=motion&&motion->take_filter_retune();
        retuned=invalidated||std::abs(effective-cutoff_set)>.1||p.resonance!=resonance_set;
        if (retuned) {
            cutoff_set = effective; resonance_set = p.resonance;
        }
        if (!p.shared1) indep1_cur = smooth_log(indep1_cur, p.independent1, alpha);
        if (!p.shared2) indep2_cur = smooth_log(indep2_cur, p.independent2, alpha);
        const double low = std::max(p.range_min, 20.0), high = std::max(p.range_max, low + 1);
        const auto position = [low, high](double x) noexcept {
            return std::clamp(std::log(std::max(x, low) / low) / std::log(high / low), 0.0, 1.0);
        };
        bool hp_on = false;
        if (p.highpass > 25 || hp_cur > 25) {
            hp_cur = smooth_log(hp_cur, p.highpass, alpha); hp_on = hp_cur > 25;
        }
        const double send1 = p.bypass1 ? 0 : p.send1, send2 = p.bypass2 ? 0 : p.send2;
        const bool slow = !(std::abs(send1 - 1) < 1e-6 && std::abs(send2 - 1) < 1e-6);
        const bool shimmer_on = p.shimmer && p.shimmer_mix > .001 && flags.voices_present;
        if (!flags.native_active && (flags.osc2_audible || (p.shimmer && p.shimmer_mix > .001))) {
            fault = true; silence(); return false;
        }
        if (!flags.native_active) {
            // Fixed3-unison: legacy fallback filters have only ever received
            // zero input, so they output exact zero; native state stays frozen.
            bypass_ratio = 0; silence(); return true;
        }
        if (shimmer_on) shimmer_cur += alpha * (p.shimmer_mix - shimmer_cur);
        const std::array<double, ZoneCount> values{
            cutoff_set, resonance_set, p.slope24 ? 1.0 : 0.0, position(cutoff_cur),
            p.shared1 ? 1.0 : 0.0, p.shared2 ? 1.0 : 0.0, indep1_cur, indep2_cur,
            p.shared1 ? 0 : position(indep1_cur), p.shared2 ? 0 : position(indep2_cur), hp_on ? 1.0 : 0.0, hp_cur,
            send1, send2, slow ? 1.0 : 0.0, flags.haas_active && flags.osc2_audible ? 1.0 : 0.0,
            static_cast<double>(p.haas_samples), flags.osc2_audible ? 1.0 : 0.0, shimmer_on ? 1.0 : 0.0,
            p.shimmer_high ? 1200.0 : 400.0, shimmer_cur, p.shimmer_send};
        for (unsigned i = 0; i < zones.size(); ++i) *zones[i] = values[i];
        computeStavePadBus(dsp.get(), frames, inputs.data(), outputs.data());
        // Original merged-path approximation: magnitudes of PRE-Haas sources,
        // applied to the complete filtered bus, not exact independent stems.
        bypass_ratio = 0;
        std::array<double, 4> magnitudes{};
        double m1=0,m2=0,total=0;
        if (p.bypass1 || p.bypass2 || (motion&&motion->split())) {
            for (unsigned c = 0; c < 4; ++c)
                for (unsigned i = 0; i < frames; ++i) magnitudes[c] += std::abs(input[c][i]);
            m1 = magnitudes[0] + magnitudes[1]; m2 = magnitudes[2] + magnitudes[3]; total = m1 + m2;
            if (!std::isfinite(total)) fault = true;
            if (total > 1e-6) bypass_ratio = ((p.bypass1 ? m1 : 0) + (p.bypass2 ? m2 : 0)) / total;
        }
        for (unsigned i = 0; i < frames; ++i) {
            if(motion) for(unsigned c=0;c<2;++c) {
                const double x=output[c][i];
                if(motion->split()) {
                    double a=x*(total>1e-6?m1/total:.5),b=x*(total>1e-6?m2/total:.5);
                    a*=motion->oscillator(0,c)[i]; b*=motion->oscillator(1,c)[i];
                    a*=1+motion->oscillator(0,c+2)[i]; b*=1+motion->oscillator(1,c+2)[i];
                    output[c][i]=a+b;
                } else {
                    output[c][i]*=motion->oscillator(0,c)[i];
                    output[c][i]*=1+motion->oscillator(0,c+2)[i];
                }
            }
            if (bypass_ratio > 1e-6) {
                output[4][i] = output[0][i] * bypass_ratio; output[5][i] = output[1][i] * bypass_ratio;
                output[0][i] *= 1 - bypass_ratio; output[1][i] *= 1 - bypass_ratio;
            }
            // Ping-pong is explicitly absent in this slice. Fast-path send
            // will need to move after that processor when it is composed.
            if (!slow) { output[2][i] = output[0][i]; output[3][i] = output[1][i]; }
            if (shimmer_on) {
                output[2][i] += output[6][i]; output[3][i] += output[6][i];
                if (p.shimmer_send > .001) { output[2][i] += output[7][i]; output[3][i] += output[8][i]; }
            }
            for (const auto& channel : output) if (!std::isfinite(channel[i])) fault = true;
        }
        if (fault) { silence(); return false; }
        return true;
    }
};
PadBus::PadBus(std::uint32_t frames, const PadBusConfig& config) : impl_(std::make_unique<Impl>(frames, config)) {}
PadBus::~PadBus() = default;
bool PadBus::configure(const PadBusConfig& config) noexcept {
    if (impl_->fault || !config.valid()) return false;
    impl_->config = config; return true;
}
bool PadBus::process_block(const std::array<const double*, 5>& source, PadBlockFlags flags, StageMotion* motion, FilterMotion* filter) noexcept { return impl_->process(source, flags, motion, filter); }
bool PadBus::filter_retuned() const noexcept { return impl_->retuned; }
const double* PadBus::stem(unsigned c) const noexcept { return c < 9 ? impl_->output[c].data() : nullptr; }
void PadBus::clear() noexcept { impl_->clear(); }
bool PadBus::healthy() const noexcept { return !impl_->fault; }
std::uint32_t PadBus::block_frames() const noexcept { return impl_->frames; }
std::array<double, 8> PadBus::state() const noexcept {
    return {impl_->cutoff_cur, impl_->indep1_cur, impl_->indep2_cur, impl_->hp_cur, impl_->shimmer_cur,
            impl_->cutoff_set, impl_->resonance_set, impl_->bypass_ratio};
}
} // namespace stave
