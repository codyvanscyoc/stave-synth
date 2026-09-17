#pragma once
#include "stave/shared_effects.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>

namespace stave {
enum class MotionShape { Sine, Triangle, Square, Saw, Ramp, Peak, SampleHold };
enum class MotionTarget { Filter, Amp, Pan, Bus };
struct MotionLfo {
    double rate{1}, multiplier{1}, depth{}, spread{}, offset_ms{}, smooth{};
    DelayDivision division{DelayDivision::Free};
    MotionShape shape{MotionShape::Sine};
    MotionTarget target{MotionTarget::Filter};
    bool active{true}, key_sync{}, invert{}, haas_compensate{}, poly{};
    std::array<bool,2> received{true,true}; // OSC1, OSC2
    bool valid() const noexcept {
        const auto range=[](double x,double a,double b) { return std::isfinite(x)&&x>=a&&x<=b; };
        return range(rate,.05,20)&&range(multiplier,.1,10)&&range(depth,0,1)&&range(spread,0,1)&&
            range(offset_ms,-500,500)&&range(smooth,0,1)&&unsigned(division)<=8&&unsigned(shape)<=6&&unsigned(target)<=3;
    }
};
struct MotionConfig {
    std::array<MotionLfo,2> lfo{};
    double mix{1},bpm{120},haas_ms{20};
    MotionConfig() { lfo[1].target=MotionTarget::Pan; }
    bool valid() const noexcept {
        return lfo[0].valid()&&lfo[1].valid()&&std::isfinite(mix)&&mix>=0&&mix<=1&&
            std::isfinite(bpm)&&bpm>=40&&bpm<=240&&std::isfinite(haas_ms)&&haas_ms>=5&&haas_ms<=40;
    }
};
// Prepared/offline randomness. Caller supplies bounded no-I/O draws in [-1,1].
// No implicit random-device access or production seed policy in this component.
class MotionRandom {
public:
    virtual ~MotionRandom()=default;
    virtual bool next(double& value) noexcept=0;
};
struct MotionState {
    double phase{},held_a{},held_b{},last_a{},last_b{},smooth_a{},smooth_b{};
};
// Offline, single-owner scalar/ramp component. Fixed48k,256/512. Mirrors the
// original non-merged modulation path, including twice-stepped smoothers when
// selective OSC routing consumes the same LFO twice. It does not apply audio,
// drift/wobble, Faust poly LFOs, filter retunes or dispatch note events itself.
class StageMotion final {
public:
    explicit StageMotion(unsigned frames=512,MotionRandom* random=nullptr):frames_(frames),random_(random) {
        if(frames!=256&&frames!=512) throw std::invalid_argument("Motion requires256/512 frames");
        const double step=1./(frames-1);
        for(unsigned i=0;i<frames;++i) axis_[i]=i*step;
        axis_[frames-1]=1.; identity();
    }
    bool configure(const MotionConfig& p) noexcept {
        if(!healthy()||!p.valid()) return false;
        for(unsigned j=0;j<2;++j) if(p.lfo[j].target!=config_.lfo[j].target) {
            auto& s=state_[j]; s.last_a=s.last_b=s.smooth_a=s.smooth_b=0;
            retune_=true;
        }
        config_=p; return true;
    }
    bool key_trigger() noexcept {
        if(!healthy()) return false;
        for(unsigned j=0;j<2;++j) if(config_.lfo[j].key_sync)
            state_[j].phase=state_[j].held_a=state_[j].held_b=0;
        return true; // last/smoother endpoints deliberately retained
    }
    bool process(bool use_faust=true) noexcept {
        if(!healthy()) { silence(); return false; }
        identity(); filter_=0;
        for(unsigned j=0;j<2;++j) {
            if(!advance(j)) { stop(); return false; }
            const auto& p=config_.lfo[j]; const auto& s=state_[j];
            depth_[j]=p.active?std::min(p.depth*config_.mix,.7):0;
            if((p.received[0]||p.received[1])&&p.target==MotionTarget::Filter&&depth_[j]>.001)
                filter_+=(end_[j][0]+s.last_a)*.5*depth_[j];
        }
        const bool all=config_.lfo[0].received[0]&&config_.lfo[0].received[1]&&
                       config_.lfo[1].received[0]&&config_.lfo[1].received[1];
        const auto used=[this,use_faust](unsigned j) {
            const auto& p=config_.lfo[j];
            return depth_[j]>.001&&(p.target==MotionTarget::Pan||(p.target==MotionTarget::Amp&&!(use_faust&&p.poly)));
        };
        split_=!all&&((used(0)&&(config_.lfo[0].received[0]||config_.lfo[0].received[1]))||
                     (used(1)&&(config_.lfo[1].received[0]||config_.lfo[1].received[1])));
        if(all||split_) for(unsigned group=0;group<(split_?2u:1u);++group)
            for(unsigned j=0;j<2;++j) if(used(j)&&(all||config_.lfo[j].received[group])) {
                ramps(j);
                for(unsigned i=0;i<frames_;++i) {
                    if(config_.lfo[j].target==MotionTarget::Amp) {
                        gain_[group][0][i]*=gate(ramp_[0][i],depth_[j]);
                        gain_[group][1][i]*=gate(ramp_[1][i],depth_[j]);
                    } else {
                        gain_[group][2][i]+=ramp_[0][i]*depth_[j]*.5;
                        gain_[group][3][i]+=-ramp_[0][i]*depth_[j]*.5; // v1 uses A on both sides
                    }
                }
            }
        for(unsigned j=0;j<2;++j) if(config_.lfo[j].target==MotionTarget::Bus&&depth_[j]>.001) {
            ramps(j);
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames_;++i) bus_[c][i]*=gate(ramp_[c][i],depth_[j]);
        }
        for(unsigned j=0;j<2;++j) { state_[j].last_a=end_[j][0]; state_[j].last_b=end_[j][1]; }
        return true;
    }
    // Two OSC groups when split()==true, otherwise combined bus uses group0.
    // channels0/1 are AMP multipliers;2/3 are PAN additions (multiply by1+pan).
    // Keep those separate to preserve multiply order around magnitude splits.
    const double* oscillator(unsigned group,unsigned c) const noexcept { return group<2&&c<4?gain_[group][c].data():nullptr; }
    const double* bus(unsigned c) const noexcept { return c<2?bus_[c].data():nullptr; }
    double filter_modulation() const noexcept { return filter_; } // octave exponent is2*value, gated atabs>.001
    double depth(unsigned j) const noexcept { return j<2?depth_[j]:0; }
    MotionState state(unsigned j) const noexcept { return j<2?state_[j]:MotionState{}; }
    bool split() const noexcept { return split_; }
    bool take_filter_retune() noexcept { const bool v=retune_; retune_=false; return v; }
    unsigned block_frames() const noexcept { return frames_; }
    bool healthy() const noexcept { return !stopped_; }
    void stop() noexcept { stopped_=true; silence(); }
private:
    static double wrap(double x) noexcept { return x-std::floor(x); }
    static double gate(double x,double d) noexcept { return 1.-d+d*(1.+x)*.5; }
    double evaluate(MotionShape shape,double phase,double held) const noexcept {
        switch(shape) {
        case MotionShape::Triangle:return 4.*std::abs(phase-.5)-1.;
        case MotionShape::Square:return phase<.5?1.:-1.;
        case MotionShape::Saw:return 2.*phase-1.;
        case MotionShape::Ramp:return 1.-2.*phase;
        case MotionShape::Peak:return phase<.2?(phase/.2)*2.-1.:(1.-(phase-.2)/.8)*2.-1.;
        case MotionShape::SampleHold:return held;
        default:return std::sin(phase*(2.*3.14159265358979323846));
        }
    }
    bool draw(double& value) noexcept { return random_&&random_->next(value)&&std::isfinite(value)&&value>=-1&&value<=1; }
    bool advance(unsigned j) noexcept {
        const auto& p=config_.lfo[j]; auto& s=state_[j];
        if(p.depth*config_.mix<.001) { s.last_a=s.last_b=0; end_[j]={0,0}; return true; }
        constexpr std::array<double,9> beats{0,2,1.5,1,2./3,.75,.5,1./3,.25};
        double rate=p.rate;
        if(p.division!=DelayDivision::Free) {
            double cycle=std::max(.05,(60./std::max(40.,config_.bpm))*beats[unsigned(p.division)]);
            cycle/=std::max(.1,p.multiplier); rate=1./cycle;
        }
        const double prior=s.phase; s.phase=wrap(prior+rate*(double(frames_)/48000));
        if(p.shape==MotionShape::SampleHold&&s.phase<prior)
            if(!draw(s.held_a)||!draw(s.held_b)) return false;
        double offset=p.offset_ms; if(p.haas_compensate) offset+=config_.haas_ms;
        const double phase_offset=(offset/1000.)*rate;
        end_[j]={evaluate(p.shape,wrap(s.phase+phase_offset),s.held_a),
                 evaluate(p.shape,wrap(s.phase+phase_offset+.5*p.spread),s.held_b)};
        if(p.invert) for(auto& x:end_[j]) x=-x;
        return true;
    }
    void ramps(unsigned j) noexcept {
        auto& s=state_[j]; const auto& p=config_.lfo[j];
        const std::array<double,2> start{s.last_a,s.last_b};
        std::array<double,2> current{s.smooth_a,s.smooth_b};
        const double a=p.smooth<=.001?0:std::exp(-1./std::max((.0005+.0995*p.smooth)*48000,1.));
        for(unsigned c=0;c<2;++c) {
            for(unsigned i=0;i<frames_;++i) {
                const double x=axis_[i]*(end_[j][c]-start[c])+start[c];
                current[c]=a==0?x:(1.-a)*x+a*current[c];
                ramp_[c][i]=current[c];
            }
        }
        s.smooth_a=current[0]; s.smooth_b=current[1];
    }
    void identity() noexcept {
        for(auto& group:gain_) for(unsigned c=0;c<4;++c) group[c].fill(c<2?1.:0.);
        for(auto& c:bus_) c.fill(1);
    }
    void silence() noexcept {
        for(auto& group:gain_) for(auto& c:group) c.fill(0);
        for(auto& c:bus_) c.fill(0);
        depth_={}; filter_=0; split_=false;
    }
    unsigned frames_; MotionRandom* random_{}; MotionConfig config_{};
    std::array<MotionState,2> state_{};
    std::array<std::array<double,2>,2> end_{};
    std::array<double,2> depth_{};
    std::array<double,512> axis_{};
    std::array<std::array<std::array<double,512>,4>,2> gain_{};
    std::array<std::array<double,512>,2> bus_{},ramp_{};
    double filter_{}; bool split_{},retune_{},stopped_{};
};

struct FilterMotionConfig {
    double drift_cents{2},wobble{};
    bool valid() const noexcept {
        return std::isfinite(drift_cents)&&drift_cents>=0&&drift_cents<=40&&
            std::isfinite(wobble)&&wobble>=0&&wobble<=1;
    }
};
// Runs AFTER cutoff smoothing and StageMotion, before coefficient-update
// gating. This owns only the two random walks; not PadBus's cutoff smoother
// or biquads. Disabled walks retain their old values, exactly as before.
class FilterMotion final {
public:
    explicit FilterMotion(MotionRandom* random=nullptr) noexcept:random_(random) {}
    bool configure(FilterMotionConfig p) noexcept {
        if(!healthy()||!p.valid()) return false;
        config_=p; return true;
    }
    bool process(double smoothed_cutoff,double modulation,double resonance) noexcept {
        if(!healthy()) return false;
        // exp(log(bound)) may sit a few ULP outside nominal control bounds.
        if(!std::isfinite(smoothed_cutoff)||smoothed_cutoff<20-1e-10||smoothed_cutoff>20000+1e-8||
           !std::isfinite(modulation)||std::abs(modulation)>1.400000000001||
           !std::isfinite(resonance)||resonance<.1||resonance>10) return false;
        double cutoff=smoothed_cutoff;
        if(std::abs(modulation)>.001) cutoff*=std::pow(2.,modulation*2.);
        if(config_.drift_cents>.01) {
            double value;
            if(!draw(value)) { stop(); return false; }
            drift_+=value*.06; drift_=std::clamp(drift_*.9985,-1.,1.);
            cutoff*=std::pow(2.,config_.drift_cents*drift_/1200.);
        }
        if(config_.wobble>.001&&resonance>.5) {
            double value;
            if(!draw(value)) { stop(); return false; }
            wobble_+=value*.12; wobble_=std::clamp(wobble_*.995,-1.,1.);
            const double scale=(resonance-.5)*2.;
            cutoff*=1.+config_.wobble*scale*wobble_*.03;
        }
        cutoff_=std::clamp(cutoff,20.,20000.); return true;
    }
    std::array<double,3> state() const noexcept { return {cutoff_,drift_,wobble_}; }
    bool healthy() const noexcept { return !stopped_; }
    void stop() noexcept { stopped_=true; cutoff_=20; }
private:
    bool draw(double& value) noexcept { return random_&&random_->next(value)&&std::isfinite(value)&&value>=-1&&value<=1; }
    MotionRandom* random_{}; FilterMotionConfig config_{};
    double cutoff_{8000},drift_{},wobble_{}; bool stopped_{};
};
} // namespace stave
