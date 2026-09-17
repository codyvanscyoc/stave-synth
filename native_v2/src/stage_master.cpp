#include "stave/stage_master.hpp"
#include "stereo_faust_unit.hpp"
#include "master_filters.hpp"

STAVE_STEREO_API(StaveMasterFX)
STAVE_STEREO_API(StaveBusComp)

namespace stave {
using detail::range;
bool MasterConfig::valid() const noexcept {
    for(const auto& b:eq) if(!range(b.frequency,20,20000)||!range(b.gain,-18,18)||!range(b.q,.1,10)) return false;
    return range(cutoff,20,2000)&&(slope==6||slope==12||slope==24)&&range(space,0,1)&&range(pre_gain,.5,3)&&
        static_cast<unsigned>(sidechain)<=3&&range(threshold,-40,0)&&range(ratio,1,1000)&&range(attack,.1,30)&&
        range(release,50,1200)&&range(knee,0,12)&&range(makeup,0,20)&&range(mix,0,1)&&range(sc_highpass,20,500);
}
bool MasterModulation::valid() const noexcept { return range(bpm,40,300)&&range(lfo_depth,0,1)&&range(lfo_value,-1,1); }
struct StageMaster::Impl {
    unsigned frames; MasterConfig config; bool stopped{},fault{},eq_active{};
    detail::StereoUnit master{apiStaveMasterFX()},compressor{apiStaveBusComp()};
    LookaheadLimiter limiter;
    std::array<std::array<detail::MasterBiquad,3>,2> eq{};
    std::array<detail::MasterOnePole,2> hp6{},dc{};
    std::array<detail::MasterBiquad,2> hp12{};
    std::array<std::array<detail::MasterBiquad,2>,2> hp24{};
    detail::MasterBiquad shelf,sc_hp;
    double space_last{-1},rms_state{},env_gr{},bpm_phase{}; int pulse_remaining{};
    std::array<std::array<double,512>,2> audio{},fx{};
    std::array<double,512> sc{},level{};
    Impl(unsigned n,LimiterPolicy policy):frames(n),limiter(n,policy) {
        detail::frames_valid(n);
        for(const char* s:{"eq1_freq","eq1_gain","eq1_q","eq2_freq","eq2_gain","eq2_q","eq3_freq","eq3_gain","eq3_q",
                           "hp_enable","hp_freq","hp_slope","pre_gain","sat_enable"}) master.require(s);
        for(const char* s:{"enabled","threshold_db","ratio","attack_ms","release_ms","knee_db","makeup_db","mix","sc_hpf_hz"}) compressor.require(s);
        shelf.shelf(0); for(auto& c:dc) c.highpass(15); tune(config);
    }
    void tune(const MasterConfig& p) noexcept {
        eq_active=false;
        for(unsigned i=0;i<3;++i) {
            eq_active|=std::abs(p.eq[i].gain)>.01;
            for(unsigned c=0;c<2;++c) eq[c][i].peak(p.eq[i].frequency,p.eq[i].gain,p.eq[i].q);
        }
        for(unsigned c=0;c<2;++c) {
            hp6[c].highpass(p.cutoff); hp12[c].highpass(p.cutoff);
            for(auto& f:hp24[c]) f.highpass(p.cutoff);
        }
        sc_hp.highpass(p.sc_highpass); config=p;
    }
    void bpm(double rate) noexcept {
        const double beat=(60.0/rate)*48000,step=1/beat,decay=4.0/2400;
        const int previous=pulse_remaining; int last=-1;
        std::fill_n(sc.data(),frames,0);
        if(previous>0) for(unsigned i=0;i<std::min(unsigned(previous),frames);++i) sc[i]=std::exp(-double(2400-previous+i)*decay);
        double prior=std::floor(bpm_phase),phase=bpm_phase;
        for(unsigned i=0;i<frames;++i) {
            phase=bpm_phase+(i+1)*step;
            const double integer=std::floor(phase);
            if(integer>prior) {
                for(unsigned j=i;j<frames&&j<i+2400;++j) sc[j]=std::exp(-double(j-i)*decay);
                last=static_cast<int>(i);
            }
            prior=integer;
        }
        bpm_phase=phase-std::floor(phase);
        pulse_remaining=last>=0?std::max(0,2400-(int(frames)-last)):std::max(0,previous-int(frames));
    }
    void block_compress(const std::array<const double*,2>* piano,const MasterModulation& m) noexcept {
        const auto& p=config;
        const bool use_piano=p.sidechain==SidechainSource::Piano&&piano;
        const bool use_lfo=p.sidechain==SidechainSource::Lfo&&m.lfo_depth>.001;
        const bool use_bpm=p.sidechain==SidechainSource::Bpm;
        if(use_bpm) bpm(m.bpm);
        const double a=1-std::exp(-1.0/(.005*48000));
        double sum=0;
        for(unsigned i=0;i<frames;++i) {
            double x=use_bpm?sc[i]:use_piano?((*piano)[0][i]+(*piano)[1][i])*.5:
                use_lfo?std::abs(m.lfo_value)*.7:(audio[0][i]+audio[1][i])*.5;
            if(!use_bpm) x=sc_hp.tick(x);
            const double rms=a*(x*x)+rms_state;
            rms_state=(1-a)*rms;
            level[i]=10*std::log10(std::max(rms,1e-12)); sum+=level[i];
        }
        const double target=detail::master_compression_target(sum/frames-p.threshold,p.knee,p.ratio);
        const double delta=target-env_gr;
        const double tau=delta>0?std::max(p.attack*.001,.0001):
            p.release_auto?.1+.5*std::min(1.,env_gr/8):std::max(p.release*.001,.0001);
        const double next=env_gr+(1-std::exp(-(double(frames)/48000)/tau))*delta;
        const double increment=(next-env_gr)/double(frames-1);
        for(unsigned i=0;i<frames;++i) {
            const double ramp=i+1==frames?next:env_gr+i*increment;
            const double gain=std::pow(10.,(-ramp+p.makeup)/20);
            for(unsigned c=0;c<2;++c) {
                if(p.mix>=.999) audio[c][i]*=gain;
                else if(p.mix>=.001) audio[c][i]=audio[c][i]*(1-p.mix)+(audio[c][i]*gain)*p.mix;
            }
        }
        env_gr=next;
    }
    bool process(const std::array<const double*,6>& pad,const std::array<const double*,2>* piano,MasterModulation m) noexcept {
        if(!m.valid()) return false;
        for(const auto* p:pad) if(!acceptable(p)) return false;
        if(piano) for(const auto* p:*piano) if(!acceptable(p)) return false;
        if(stopped||fault||!limiter.healthy()) { stop(); return false; }
        for(const auto* p:pad) for(unsigned i=0;i<frames;++i) if(!std::isfinite(p[i])) { fault=true; stop(); return false; }
        if(piano) for(const auto* p:*piano) for(unsigned i=0;i<frames;++i) if(!std::isfinite(p[i])) { fault=true; stop(); return false; }
        const auto& p=config; const bool split=p.compression&&p.fx_bypass;
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
            audio[c][i]=pad[split?2+c:c][i]+(piano?(*piano)[c][i]:0);
            fx[c][i]=split?pad[4+c][i]:0;
        }
        if(p.space>.001) {
            if(std::abs(p.space-space_last)>.005) { shelf.shelf(10*p.space); space_last=p.space; }
            for(unsigned i=0;i<frames;++i) {
                const double mid=(audio[0][i]+audio[1][i])*.5,side=shelf.tick((audio[0][i]-audio[1][i])*.5);
                audio[0][i]=mid+side; audio[1][i]=mid-side;
            }
        }
        if(!p.compression) {
            constexpr const char* names[3][3]={{"eq1_freq","eq1_gain","eq1_q"},{"eq2_freq","eq2_gain","eq2_q"},{"eq3_freq","eq3_gain","eq3_q"}};
            for(unsigned i=0;i<3;++i) { master.set(names[i][0],p.eq[i].frequency); master.set(names[i][1],p.eq[i].gain); master.set(names[i][2],p.eq[i].q); }
            master.set("hp_enable",p.highpass?1:0); master.set("hp_freq",p.cutoff); master.set("hp_slope",p.slope);
            master.set("pre_gain",p.pre_gain); master.set("sat_enable",p.saturation?1:0);
            if(!master.process({audio[0].data(),audio[1].data()},frames)) { fault=true; stop(); return false; }
            for(unsigned c=0;c<2;++c) std::copy_n(master.channel(c),frames,audio[c].data());
        } else {
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
                double x=audio[c][i];
                if(eq_active) for(auto& f:eq[c]) x=f.tick(x);
                if(p.highpass) {
                    if(p.slope==6) x=hp6[c].tick(x);
                    else if(p.slope==24) for(auto& f:hp24[c]) x=f.tick(x);
                    else x=hp12[c].tick(x);
                }
                audio[c][i]=x*p.pre_gain; fx[c][i]*=p.pre_gain;
            }
            if(p.native_self&&p.sidechain==SidechainSource::Self) {
                compressor.set("enabled",1); compressor.set("threshold_db",p.threshold); compressor.set("ratio",std::min(p.ratio,20.));
                compressor.set("attack_ms",p.attack); compressor.set("release_ms",p.release); compressor.set("knee_db",p.knee);
                compressor.set("makeup_db",p.makeup); compressor.set("mix",p.mix); compressor.set("sc_hpf_hz",p.sc_highpass);
                if(!compressor.process({audio[0].data(),audio[1].data()},frames)) { fault=true; stop(); return false; }
                for(unsigned c=0;c<2;++c) std::copy_n(compressor.channel(c),frames,audio[c].data());
            } else {
                block_compress(piano,m);
                if(!std::isfinite(env_gr)||!std::isfinite(rms_state)) { fault=true; stop(); return false; }
            }
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
                if(split) audio[c][i]+=fx[c][i];
                if(p.saturation) {
                    const double scratch=std::abs(audio[c][i])*.09;
                    audio[c][i]*=1.01; audio[c][i]+=scratch; audio[c][i]=dc[c].tick(audio[c][i]);
                }
            }
        }
        if(!limiter.process({audio[0].data(),audio[1].data()})) { fault=true; stop(); return false; }
        return true;
    }
    bool acceptable(const double* p) const noexcept {
        return p&&detail::disjoint(p,limiter.channel(0),frames)&&detail::disjoint(p,limiter.channel(1),frames);
    }
    void stop() noexcept { stopped=true; limiter.stop(); }
};
StageMaster::StageMaster(unsigned n,LimiterPolicy policy):impl_(std::make_unique<Impl>(n,policy)) {}
StageMaster::~StageMaster()=default;
bool StageMaster::configure(const MasterConfig& p) noexcept { if(!healthy()||!p.valid()) return false; impl_->tune(p); return true; }
bool StageMaster::process(const std::array<const double*,6>& p,const std::array<const double*,2>* piano,MasterModulation m) noexcept { return impl_->process(p,piano,m); }
bool StageMaster::retrigger_bpm() noexcept { if(!healthy()) return false; impl_->bpm_phase=0; impl_->pulse_remaining=2400; return true; }
void StageMaster::reset_limiter() noexcept { impl_->limiter.reset(); }
void StageMaster::stop() noexcept { impl_->stop(); }
bool StageMaster::healthy() const noexcept { return !impl_->stopped&&!impl_->fault&&impl_->limiter.healthy(); }
const double* StageMaster::channel(unsigned c) const noexcept { return impl_->limiter.channel(c); }
unsigned StageMaster::block_frames() const noexcept { return impl_->frames; }
std::array<double,5> StageMaster::state() const noexcept { return {impl_->limiter.gain(),impl_->env_gr,impl_->bpm_phase,double(impl_->pulse_remaining),impl_->space_last}; }
} // namespace stave
