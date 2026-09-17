#pragma once
#include "stave/sampled_bed.hpp"

namespace stave {
struct BedConfig {
    double level{1},mellow_hz{400};
    bool mellow{};
    bool valid() const noexcept {
        return std::isfinite(level)&&level>=0&&level<=1&&
            std::isfinite(mellow_hz)&&mellow_hz>=100&&mellow_hz<=8000;
    }
};
// Native owner of prepared samples, independent level/mellow and reversible
// fade. The bank is sealed before audio begins. No file/asset operations here.
// Level edits use20ms smoothing; fade uses a sample-clock cubic S curve, not
// the legacy background thread. These are explicit control-transition changes.
class BedBus final {
public:
    explicit BedBus(std::unique_ptr<SampledBed> bank,unsigned frames=512)
        :bank_(std::move(bank)),frames_(frames) {
        if(!bank_||(frames!=256&&frames!=512)||!bank_->healthy())
            throw std::invalid_argument("Bed bus requires healthy prepared bank and256/512 frames");
        bank_->seal(); for(auto& f:filter_) f.tune(400,.707);
    }
    bool configure(const BedConfig& config) noexcept {
        if(!healthy()||!config.valid()) return false;
        if(config.mellow&&!config_.mellow) for(auto& f:filter_) { f={}; f.tune(config.mellow_hz,.707); }
        if(config.mellow_hz!=config_.mellow_hz) for(auto& f:filter_) f.tune(config.mellow_hz,.707);
        config_=config; return true;
    }
    bool trigger(unsigned slot,double rise=0,double cutoff=3000) noexcept {
        return healthy()&&bank_->trigger(slot,rise,cutoff);
    }
    void release() noexcept { bank_->release_all(); }
    bool fade(bool out,double seconds=5) noexcept {
        if(!healthy()||!std::isfinite(seconds)||seconds<.02||seconds>30) return false;
        fade_start_=fade_current_; fade_target_=out?0:1; fade_position_=0;
        fade_length_=static_cast<unsigned>(seconds*48000); return true;
    }
    bool process() noexcept {
        for(auto& c:output_) c.fill(0);
        if(!healthy()||!bank_->process(output_[0].data(),output_[1].data(),frames_)) { stop(); return false; }
        const bool mellow=config_.mellow&&!bank_->rise_filter_active();
        const double alpha=1-std::exp(-1./(.02*48000));
        for(unsigned i=0;i<frames_;++i) {
            if(fade_position_<fade_length_) {
                const double t=double(++fade_position_)/fade_length_,s=t*t*(3-2*t);
                fade_current_=fade_start_+(fade_target_-fade_start_)*s;
            } else fade_current_=fade_target_;
            if(std::abs(level_-config_.level)<1e-12) level_=config_.level;
            else level_+=alpha*(config_.level-level_);
            for(unsigned c=0;c<2;++c) {
                const double sample=mellow?filter_[c].tick(output_[c][i]):output_[c][i];
                output_[c][i]=sample*level_*fade_current_;
                if(!std::isfinite(output_[c][i])) { stop(); return false; }
            }
        }
        return true;
    }
    bool healthy() const noexcept { return !stopped_&&bank_->healthy(); }
    unsigned active() const noexcept { return bank_->active(); }
    bool loaded(unsigned slot) const noexcept { return bank_->loaded(slot); }
    const double* channel(unsigned c) const noexcept { return c<2?output_[c].data():nullptr; }
    double fade_gain() const noexcept { return fade_current_; }
    bool faded_target() const noexcept { return fade_target_==0; }
    void stop() noexcept { stopped_=true; bank_->stop(); for(auto& c:output_) c.fill(0); }
private:
    std::unique_ptr<SampledBed> bank_;
    unsigned frames_;
    BedConfig config_{};
    std::array<StageLowpass,2> filter_{};
    std::array<std::array<double,512>,2> output_{};
    double level_{1},fade_current_{1},fade_start_{1},fade_target_{1};
    unsigned fade_position_{},fade_length_{};
    bool stopped_{};
};
} // namespace stave
