#include "stave/stage_master.hpp"
#include <cmath>
extern "C" {
void* master_create(unsigned n,int reference) noexcept {
    try { if(reference!=0&&reference!=1) return nullptr;
        return new stave::StageMaster(n,reference?stave::LimiterPolicy::Reference:stave::LimiterPolicy::CeilingSafe);
    } catch(...) { return nullptr; }
}
void master_delete(void* p) { delete static_cast<stave::StageMaster*>(p); }
int master_configure(void* p,const double* v,unsigned n) {
    if(!p||!v||n!=28) return 0;
    for(unsigned i=0;i<n;++i) if(!std::isfinite(v[i])) return 0;
    for(unsigned i:{9u,14u,15u,16u,17u,18u}) if(v[i]!=0&&v[i]!=1) return 0;
    if((v[11]!=6&&v[11]!=12&&v[11]!=24)||v[19]<0||v[19]>3||std::floor(v[19])!=v[19]) return 0;
    stave::MasterConfig c;
    for(unsigned i=0;i<3;++i) c.eq[i]={v[3*i],v[3*i+1],v[3*i+2]};
    c.highpass=v[9]; c.cutoff=v[10]; c.slope=int(v[11]); c.space=v[12]; c.pre_gain=v[13]; c.saturation=v[14];
    c.compression=v[15]; c.fx_bypass=v[16]; c.native_self=v[17]; c.release_auto=v[18]; c.sidechain=static_cast<stave::SidechainSource>(int(v[19]));
    c.threshold=v[20]; c.ratio=v[21]; c.attack=v[22]; c.release=v[23]; c.knee=v[24]; c.makeup=v[25]; c.mix=v[26]; c.sc_highpass=v[27];
    return static_cast<stave::StageMaster*>(p)->configure(c);
}
int master_process(void* p,const double* pad,const double* piano,unsigned n,double bpm,double depth,double value) {
    if(!p||!pad||static_cast<stave::StageMaster*>(p)->block_frames()!=n) return 0;
    std::array<const double*,6> in{}; for(unsigned i=0;i<6;++i) in[i]=pad+i*n;
    const std::array<const double*,2> keys{piano,piano?piano+n:nullptr};
    return static_cast<stave::StageMaster*>(p)->process(in,piano?&keys:nullptr,{bpm,depth,value});
}
const double* master_channel(void* p,unsigned c) { return static_cast<stave::StageMaster*>(p)->channel(c); }
void master_state(void* p,double* v) { const auto s=static_cast<stave::StageMaster*>(p)->state(); std::copy(s.begin(),s.end(),v); }
int master_command(void* p,unsigned op) {
    auto& m=*static_cast<stave::StageMaster*>(p);
    if(op==0) return m.retrigger_bpm();
    if(op==1) { m.reset_limiter(); return 1; }
    if(op==2) { m.stop(); return 1; }
    return 0;
}
void* limiter_create(unsigned n,int reference) noexcept {
    try { if(reference!=0&&reference!=1) return nullptr;
        return new stave::LookaheadLimiter(n,reference?stave::LimiterPolicy::Reference:stave::LimiterPolicy::CeilingSafe);
    } catch(...) { return nullptr; }
}
void limiter_delete(void* p) { delete static_cast<stave::LookaheadLimiter*>(p); }
int limiter_process(void* p,const double* l,const double* r) { return static_cast<stave::LookaheadLimiter*>(p)->process({l,r}); }
const double* limiter_channel(void* p,unsigned c) { return static_cast<stave::LookaheadLimiter*>(p)->channel(c); }
void limiter_reset(void* p) { static_cast<stave::LookaheadLimiter*>(p)->reset(); }
}
