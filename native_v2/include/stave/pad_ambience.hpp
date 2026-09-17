#pragma once
#include "stave/pad_bus.hpp"
#include "stave/shared_effects.hpp"
#include "stave/stage_lowpass.hpp"
#include "stave/stage_motion.hpp"
#include <algorithm>
#include <cmath>

namespace stave {
struct PadAmbienceConfig {
    PadBusConfig pad{};
    DelayConfig delay{};
    double wet{.75}, wet_gain{1};
    bool wet_filter{};
    bool valid() const noexcept {
        return pad.valid()&&delay.valid()&&std::isfinite(wet)&&wet>=0&&wet<=1&&
            std::isfinite(wet_gain)&&wet_gain>=.5&&wet_gain<=3;
    }
};
// Composed native pad/filter/delay/reverb return path. NOT a master output:
// piano/organ dry, independent sample bed, sympathetic and master processing
// are external. Optional motion gates FX before dry-bypass addback.
// Only this audio owner calls methods, including reverb control operations.
class PadAmbience final {
public:
    explicit PadAmbience(std::uint32_t frames=512):frames_(frames),pad_(frames),delay_(frames),reverb_(frames) {}
    bool configure(const PadAmbienceConfig& config) noexcept {
        if(!healthy()||!config.valid()) return false;
        if(!pad_.configure(config.pad)||!delay_.configure(config.delay)) { stop(); return false; }
        config_=config; return true;
    }
    bool reverb_control(ReverbControl c,double v) noexcept { return healthy()&&reverb_.set(c,v); }
    bool reverb_type(ReverbType t) noexcept { return healthy()&&reverb_.set_type(t); }
    bool freeze(bool v) noexcept { return healthy()&&reverb_.freeze(v); }
    bool process(const std::array<const double*,5>& source,PadBlockFlags flags,
                 const std::array<const double*,2>* delay_send=nullptr,
                 const std::array<const double*,2>* reverb_send=nullptr,
                 StageMotion* motion=nullptr,FilterMotion* filter=nullptr) noexcept {
        // Refuse missing/aliased external buffers before changing any DSP.
        for(const auto* p:source) if(!acceptable(p)) return false;
        for(const auto* pair:{delay_send,reverb_send}) if(pair)
            for(const auto* p:*pair) if(!acceptable(p)) return false;
        if(!healthy()) { stop(); return false; }
        if(!pad_.process_block(source,flags,motion,filter)||!delay_.process({pad_.stem(0),pad_.stem(1)},delay_send)) { stop(); return false; }
        const auto& p=config_.pad;
        const auto state=pad_.state();
        // Preserve v1's coefficient-update gate, including paused filters.
        // Enabling/changing slope alone does not force a retune in v1.
        if(config_.wet_filter&&pad_.filter_retuned()) {
            for(auto& f:wet_filters_) {
                f[0].tune(state[5],p.resonance*(p.slope24?.5412/.707:1));
                if(p.slope24) f[1].tune(state[5],p.resonance*(1.3066/.707));
            }
        }
        const double s1=p.bypass1?0:p.send1,s2=p.bypass2?0:p.send2;
        const bool fast=std::abs(s1-1)<1e-6&&std::abs(s2-1)<1e-6;
        const bool shimmer=p.shimmer&&p.shimmer_mix>.001&&flags.voices_present;
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames_;++i) {
            // Rebuild fast send AFTER delay, then shimmer/cloud in original
            // order. Slow send already includes those in PadBus and does not
            // include delay taps. Do not add/subtract a rounded delay delta.
            double x=fast?delay_.channel(c)[i]:pad_.stem(2+c)[i];
            if(fast&&shimmer) { x+=pad_.stem(6)[i]; if(p.shimmer_send>.001) x+=pad_.stem(7+c)[i]; }
            if(reverb_send) {
                if(!std::isfinite((*reverb_send)[c][i])) { stop(); return false; }
                x+=(*reverb_send)[c][i];
            }
            if(!std::isfinite(x)) { stop(); return false; }
            send_[c][i]=std::tanh(x*.6);
        }
        if(!reverb_.process({send_[0].data(),send_[1].data()})) { stop(); return false; }
        const double alpha=1-std::exp(-double(frames_)/(.08*48000));
        wet_cur_+=alpha*(config_.wet-wet_cur_);
        const double angle=wet_cur_*(3.14159265358979323846/2),dry_gain=std::cos(angle),wet_gain=std::sin(angle)*config_.wet_gain;
        const double ratio=pad_.state()[7],effective=ratio+(1-ratio)*dry_gain;
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames_;++i) {
            double wet=reverb_.channel(c)[i];
            if(config_.wet_filter) {
                wet=wet_filters_[c][0].tick(wet);
                if(p.slope24) wet=wet_filters_[c][1].tick(wet);
            }
            double mixed=delay_.channel(c)[i]*dry_gain+wet*wet_gain;
            if(motion) mixed*=motion->bus(c)[i];
            if(ratio>1e-6) mixed+=pad_.stem(4+c)[i];
            // Reconstruct pre-carve dry only for split routing; original dry
            // snapshot differs by at most rounding in this reconstruction.
            const double dry=(pad_.stem(c)[i]+pad_.stem(4+c)[i])*effective;
            output_[c][i]=mixed*.85;
            output_[2+c][i]=dry*.85;
            output_[4+c][i]=(mixed-dry)*.85;
            if(!std::isfinite(output_[c][i])||!std::isfinite(output_[2+c][i])||!std::isfinite(output_[4+c][i])) { stop(); return false; }
        }
        return true;
    }
    // mixed pad L/R, separated dry L/R, separated FX L/R (do not sum all six).
    const double* channel(unsigned c) const noexcept { return c<6?output_[c].data():nullptr; }
#ifdef STAVE_OFFLINE_TRACE
    // Read-only test instrumentation, never part of live control/telemetry.
    const double* trace(unsigned c) const noexcept {
        if(c<2) return delay_.channel(c);
        if(c<4) return send_[c-2].data();
        return c<6?reverb_.channel(c-4):nullptr;
    }
#endif
    bool healthy() const noexcept { return !stopped_&&pad_.healthy()&&delay_.healthy()&&reverb_.healthy(); }
    double current_wet() const noexcept { return wet_cur_; }
    double current_cutoff() const noexcept { return pad_.state()[0]; }
    unsigned block_frames() const noexcept { return frames_; }
    void stop() noexcept { stopped_=true; delay_.stop(); reverb_.stop(); for(auto& c:output_) c.fill(0); }
private:
    bool acceptable(const double* p) const noexcept {
        if(!p) return false;
        const auto a=reinterpret_cast<std::uintptr_t>(p);
        for(const auto& out:output_) {
            const auto b=reinterpret_cast<std::uintptr_t>(out.data());
            if((a<b?b-a:a-b)<frames_*sizeof(double)) return false;
        }
        return true;
    }
    unsigned frames_; PadBus pad_; StageDelay delay_; SharedReverb reverb_;
    PadAmbienceConfig config_{}; double wet_cur_{.75}; bool stopped_{};
    std::array<std::array<double,512>,2> send_{};
    std::array<std::array<double,512>,6> output_{};
    std::array<std::array<StageLowpass,2>,2> wet_filters_{};
};
} // namespace stave
