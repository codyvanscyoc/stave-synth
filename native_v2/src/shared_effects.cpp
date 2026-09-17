#include "stave/shared_effects.hpp"
#include "stereo_faust_unit.hpp"
#include <algorithm>

STAVE_STEREO_API(StavePingPong)
STAVE_STEREO_API(StaveReverb)
STAVE_STEREO_API(StavePlate)
STAVE_STEREO_API(StaveDrone)

namespace stave {
using detail::range;
namespace {
constexpr std::array<double,9> divisions{0,2,1.5,1,2.0/3,.75,.5,1.0/3,.25};
bool division_valid(DelayDivision d) { return static_cast<unsigned>(d)<divisions.size(); }
double time_ms(DelayDivision d,double free,double bpm) noexcept {
    return d==DelayDivision::Free?free:60.0/std::max(40.0,bpm)*divisions[static_cast<unsigned>(d)]*1000;
}
struct Preset { unsigned backend; double decay,predelay,low,high,damp,er,shimmer,noise; };
constexpr std::array<Preset,7> presets{{
    {0,6,25,80,7000,.50,.4,0,0}, {0,9,45,120,8500,.35,.6,0,0},
    {0,1.5,8,200,10000,.70,.8,0,0}, {1,3,5,150,11000,.30,.4,0,0},
    {0,7,30,150,7000,.55,.3,.35,0}, {2,10,15,50,4000,.30,.4,0,0},
    {0,8,20,100,6500,.50,.3,0,.70}}};
double plate_decay(double seconds) noexcept { return std::clamp(.35+seconds*.08,0.,.97); }
double drone_decay(double seconds) noexcept { return seconds<=.05?0:std::min(std::pow(10.,-3/(seconds*110)),.999); }
}
bool DelayConfig::valid() const noexcept {
    return division_valid(division)&&division_valid(reverse_division)&&range(bpm,20,300)&&
        range(milliseconds,1,1000)&&range(offset_ms,-200,200)&&range(rate,-10,10)&&rate!=0&&
        range(feedback,0,.99)&&range(wet,0,1)&&range(motion,0,1)&&range(lowcut,20,1000)&&
        range(highcut,500,20000)&&range(drive,0,1)&&range(width,0,1)&&range(mod_rate,.05,8)&&
        range(mod_depth,0,15)&&range(reverse,0,1)&&range(reverse_ms,50,3000)&&
        range(reverse_feedback,0,.7)&&range(aurora_seconds,3,15);
}
struct StageDelay::Impl {
    unsigned frames; DelayConfig config; bool stopped{},fault{};
    detail::StereoUnit dsp{apiStavePingPong()};
    std::array<std::array<double,512>,2> input{},output{};
    explicit Impl(unsigned n):frames(n) {
        detail::frames_valid(n);
        for(const char* s: {"delay_l_samps","delay_r_samps","feedback","wet","low_cut_hz","high_cut_hz",
            "drive","width","mod_rate_hz","mod_depth_ms","polarity","reverse_amount","reverse_window_ms","reverse_feedback"}) dsp.require(s);
    }
    void silence() noexcept { for(auto& c:output) c.fill(0); }
    bool process(const std::array<const double*,2>& dry,const std::array<const double*,2>* ext) noexcept {
        for(unsigned c=0;c<2;++c) {
            if(!dry[c]||(ext&&!(*ext)[c])) return false;
            for(const auto& out:output)
                if(!detail::disjoint(dry[c],out.data(),frames)||(ext&&!detail::disjoint((*ext)[c],out.data(),frames))) return false;
        }
        if(stopped||fault) { silence(); return false; }
        const auto& p=config;
        const double wet=p.wet*p.motion,rev=p.reverse*p.motion;
        const bool active=p.enabled&&!(wet<.001&&rev<.001);
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
            if(!std::isfinite(dry[c][i])||(ext&&!std::isfinite((*ext)[c][i]))) fault=true;
            input[c][i]=dry[c][i]+(active&&ext?(*ext)[c][i]:0);
            output[c][i]=dry[c][i];
        }
        if(fault) { silence(); return false; }
        if(!active) return true; // Pauses DSP and zones; no tail draining, matching v1.
        const double mult=std::max(.1,std::abs(p.rate));
        const int samples=static_cast<int>(std::clamp(time_ms(p.division,p.milliseconds,p.bpm),1.,1000.)*.001*48000);
        const int delay=std::max(1,static_cast<int>(samples/mult));
        const int offset=static_cast<int>(p.offset_ms*.001*48000/mult);
        const double reverse=p.aurora?std::clamp(p.aurora_seconds*1000,50.,15000.):
            std::clamp(time_ms(p.reverse_division,p.reverse_ms,p.bpm),50.,3000.);
        dsp.set("delay_l_samps",std::clamp(delay,1,65535)); dsp.set("delay_r_samps",std::clamp(delay+offset,1,65535));
        dsp.set("feedback",p.oblivion?1:p.feedback); dsp.set("wet",wet);
        dsp.set("low_cut_hz",p.lowcut); dsp.set("high_cut_hz",p.highcut);
        dsp.set("drive",p.drive); dsp.set("width",p.width); dsp.set("mod_rate_hz",p.mod_rate);
        dsp.set("mod_depth_ms",p.mod_depth); dsp.set("polarity",p.rate<0?-1:1);
        dsp.set("reverse_amount",rev); dsp.set("reverse_window_ms",reverse); dsp.set("reverse_feedback",p.reverse_feedback);
        if(!dsp.process({input[0].data(),input[1].data()},frames)) fault=true;
        if(!fault) for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
            output[c][i]=dsp.channel(c)[i]-(ext?(*ext)[c][i]:0);
            if(!std::isfinite(output[c][i])) fault=true;
        }
        if(fault) silence();
        return !fault;
    }
};
StageDelay::StageDelay(std::uint32_t n):impl_(std::make_unique<Impl>(n)) {}
StageDelay::~StageDelay()=default;
bool StageDelay::configure(const DelayConfig& p) noexcept { if(!healthy()||!p.valid()) return false; impl_->config=p; return true; }
bool StageDelay::process(const std::array<const double*,2>& p,const std::array<const double*,2>* e) noexcept { return impl_->process(p,e); }
const double* StageDelay::channel(unsigned c) const noexcept { return c<2?impl_->output[c].data():nullptr; }
void StageDelay::clear() noexcept { impl_->dsp.clear(); impl_->silence(); }
void StageDelay::stop() noexcept { impl_->stopped=true; impl_->silence(); }
bool StageDelay::healthy() const noexcept { return !impl_->stopped&&!impl_->fault; }

struct SharedReverb::Impl {
    unsigned frames;
    detail::StereoUnit fdn{apiStaveReverb()},plate{apiStavePlate()},drone{apiStaveDrone()};
    std::array<detail::StereoUnit*,3> units{&fdn,&plate,&drone};
    ReverbType type{ReverbType::Wash}; bool frozen{},stopped{},fault{};
    int remaining{};
    double seconds{6},normal_damp{.5},feedback_target{.9},damp_target{.5};
    std::array<std::array<double,512>,2> output{};
    explicit Impl(unsigned n):frames(n) {
        detail::frames_valid(n);
        for(auto* u:units) for(const char* s:{"feedback","predelay_ms","low_cut_hz","high_cut_hz","freeze_input"}) u->require(s);
        for(const char* s:{"damp","er_scale","shimmer_fb","noise_mod"}) fdn.require(s);
        plate.require("damp"); drone.require("drone_key");
        decay(6); mirror_all("low_cut_hz",80); mirror_all("high_cut_hz",7000); mirror_all("predelay_ms",25);
    }
    void silence() noexcept { for(auto& c:output) c.fill(0); }
    void mirror(const char* n,double v) noexcept { plate.set(n,v); drone.set(n,v); }
    void mirror_all(const char* n,double v) noexcept { fdn.set(n,v); mirror(n,v); }
    void decay(double value) noexcept {
        seconds=value;
        double feedback=0;
        if(value>0) {
            // Preserve original summation and operation order, including old
            // type's cap while set_type is applying its new preset.
            double sum=0; for(double x:{63.7,79.3,95.3,111.7,131.9,153.1,177.7,200.9}) sum+=x;
            const double avg=sum*48000/1000/8,loops=48000/avg;
            feedback=std::min(std::pow(10.,-3/(value*loops)),type==ReverbType::Drone?.9985:.985);
        }
        if(frozen) return;
        feedback_target=feedback; fdn.set("feedback",feedback); drone.set("feedback",drone_decay(value));
        if(value>0) plate.set("feedback",plate_decay(value)); // Existing zero-decay quirk preserved.
    }
    void damp(double value) noexcept {
        normal_damp=value;
        if(!frozen) { damp_target=value; mirror_all("damp",value); }
    }
    void restore() noexcept {
        frozen=false; remaining=0; decay(seconds); damp(normal_damp);
        mirror_all("freeze_input",1); fdn.set("er_scale",presets[static_cast<unsigned>(type)].er);
    }
    bool set_type(ReverbType requested) noexcept {
        const unsigned index=static_cast<unsigned>(requested);
        if(index>=presets.size()||stopped||fault) return false;
        if(requested==type) return true;
        const auto& p=presets[index];
        mirror_all("predelay_ms",p.predelay); mirror_all("low_cut_hz",p.low); mirror_all("high_cut_hz",p.high);
        damp(p.damp); fdn.set("er_scale",frozen?0:p.er); fdn.set("shimmer_fb",p.shimmer); fdn.set("noise_mod",p.noise);
        if(p.backend==2) drone.set("drone_key",0);
        decay(p.decay);
        if(!frozen) plate.set("feedback",plate_decay(p.decay));
        if(p.backend!=presets[static_cast<unsigned>(type)].backend) units[p.backend]->clear();
        type=requested;
        if(frozen) { mirror("feedback",.999); mirror("damp",.05); mirror("freeze_input",remaining>0?1:0); }
        return true;
    }
    bool set(ReverbControl control,double value) noexcept {
        if(stopped||fault||!std::isfinite(value)) return false;
        switch(control) {
        case ReverbControl::Decay: if(!range(value,0,30)) return false; decay(value); break;
        case ReverbControl::LowCut: if(!range(value,20,20000)) return false; mirror_all("low_cut_hz",value); break;
        case ReverbControl::HighCut: if(!range(value,20,20000)) return false; mirror_all("high_cut_hz",value); break;
        case ReverbControl::Damp: if(!range(value,0,.99)) return false; damp(value); break;
        case ReverbControl::Shimmer: if(!range(value,0,1)) return false; fdn.set("shimmer_fb",value); break;
        case ReverbControl::Noise: if(!range(value,0,1)) return false; fdn.set("noise_mod",value); break;
        case ReverbControl::Predelay: if(!range(value,0,150)) return false; mirror_all("predelay_ms",value); break;
        default: return false;
        }
        return true;
    }
    bool freeze(bool enabled) noexcept {
        if(stopped||fault) return false;
        if(enabled&&!frozen) {
            normal_damp=damp_target; feedback_target=.999; damp_target=.05;
            remaining=96000; frozen=true;
            mirror_all("feedback",.999); mirror_all("damp",.05); fdn.set("er_scale",0);
        } else if(!enabled&&frozen) restore();
        return true;
    }
    bool process(const std::array<const double*,2>& in) noexcept {
        for(const auto* p:in) {
            if(!p) return false;
            for(const auto& out:output) if(!detail::disjoint(p,out.data(),frames)) return false;
        }
        if(stopped||fault) { silence(); return false; }
        if(frozen&&remaining>0) { remaining-=frames; if(remaining<=0) mirror_all("freeze_input",0); }
        auto* active=units[presets[static_cast<unsigned>(type)].backend];
        if(!active->process(in,frames)) { fault=true; silence(); return false; }
        for(unsigned c=0;c<2;++c) std::copy_n(active->channel(c),frames,output[c].data());
        return true;
    }
};
SharedReverb::SharedReverb(std::uint32_t n):impl_(std::make_unique<Impl>(n)) {}
SharedReverb::~SharedReverb()=default;
bool SharedReverb::set_type(ReverbType t) noexcept { return impl_->set_type(t); }
bool SharedReverb::set(ReverbControl c,double v) noexcept { return impl_->set(c,v); }
bool SharedReverb::freeze(bool b) noexcept { return impl_->freeze(b); }
bool SharedReverb::process(const std::array<const double*,2>& in) noexcept { return impl_->process(in); }
const double* SharedReverb::channel(unsigned c) const noexcept { return c<2?impl_->output[c].data():nullptr; }
void SharedReverb::panic() noexcept { impl_->restore(); for(auto* u:impl_->units) u->clear(); impl_->silence(); }
void SharedReverb::stop() noexcept { impl_->stopped=true; impl_->silence(); }
bool SharedReverb::healthy() const noexcept { return !impl_->stopped&&!impl_->fault; }
ReverbType SharedReverb::type() const noexcept { return impl_->type; }
bool SharedReverb::frozen() const noexcept { return impl_->frozen; }
int SharedReverb::capture_remaining() const noexcept { return impl_->remaining; }
double SharedReverb::zone(unsigned b,const char* n) const noexcept {
    return b<3?impl_->units[b]->get(n):std::numeric_limits<double>::quiet_NaN();
}
} // namespace stave
