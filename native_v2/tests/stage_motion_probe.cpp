#include "stave/stage_motion.hpp"
#include <memory>
#include <vector>
struct MotionProbe final:stave::MotionRandom {
    std::vector<double> tape;
    unsigned cursor{};
    stave::StageMotion motion;
    stave::FilterMotion filter;
    MotionProbe(unsigned frames,const double* data,unsigned count):tape(data,data+count),motion(frames,this),filter(this) {}
    bool next(double& x) noexcept override { if(cursor==tape.size()) return false; x=tape[cursor++]; return true; }
};
extern "C" {
int filter_motion_config(void* h,double drift,double wobble) {
    return h&&static_cast<MotionProbe*>(h)->filter.configure({drift,wobble});
}
int filter_motion_process(void* h,double cutoff,double modulation,double resonance,double* result) {
    if(!h||!result) return 0;
    auto& owner=*static_cast<MotionProbe*>(h);
    if(!owner.filter.process(cutoff,modulation,resonance)) return 0;
    const auto state=owner.filter.state();
    for(unsigned i=0;i<3;++i) result[i]=state[i];
    result[3]=owner.cursor; return 1;
}
void* motion_create(unsigned frames,const double* tape,unsigned count) noexcept {
    if(!tape||!count||count>1000000) return nullptr;
    try { return new MotionProbe(frames,tape,count); } catch(...) { return nullptr; }
}
void motion_delete(void* h) { delete static_cast<MotionProbe*>(h); }
int motion_config(void* h,const double* v,unsigned count) {
    if(!h||!v||count!=35) return 0;
    for(unsigned i=0;i<count;++i) if(!std::isfinite(v[i])) return 0;
    stave::MotionConfig p; p.mix=v[0]; p.bpm=v[1]; p.haas_ms=v[2];
    for(unsigned j=0;j<2;++j) {
        const auto* a=v+3+16*j; auto& l=p.lfo[j];
        for(unsigned i=6;i<16;++i) if(std::floor(a[i])!=a[i]) return 0;
        if(a[6]<0||a[6]>8||a[7]<0||a[7]>6||a[8]<0||a[8]>3) return 0;
        for(unsigned i=9;i<16;++i) if(a[i]!=0&&a[i]!=1) return 0;
        l.rate=a[0]; l.multiplier=a[1]; l.depth=a[2]; l.spread=a[3]; l.offset_ms=a[4]; l.smooth=a[5];
        l.division=static_cast<stave::DelayDivision>(int(a[6])); l.shape=static_cast<stave::MotionShape>(int(a[7]));
        l.target=static_cast<stave::MotionTarget>(int(a[8])); l.active=a[9]; l.key_sync=a[10]; l.invert=a[11];
        l.haas_compensate=a[12]; l.poly=a[13]; l.received={bool(a[14]),bool(a[15])};
    }
    return static_cast<MotionProbe*>(h)->motion.configure(p);
}
int motion_process(void* h,int faust) { return h&&(faust==0||faust==1)&&static_cast<MotionProbe*>(h)->motion.process(faust); }
int motion_key(void* h) { return h&&static_cast<MotionProbe*>(h)->motion.key_trigger(); }
int motion_retune(void* h) { return h&&static_cast<MotionProbe*>(h)->motion.take_filter_retune(); }
void motion_stop(void* h) { if(h) static_cast<MotionProbe*>(h)->motion.stop(); }
const double* motion_channel(void* h,unsigned c) {
    if(!h) return nullptr;
    auto& m=static_cast<MotionProbe*>(h)->motion;
    return c<8?m.oscillator(c/4,c%4):c<10?m.bus(c-8):nullptr;
}
int motion_state(void* h,double* v) {
    if(!h||!v) return 0;
    auto& owner=*static_cast<MotionProbe*>(h); auto& m=owner.motion;
    for(unsigned j=0;j<2;++j) {
        const auto s=m.state(j);
        const std::array<double,7> a{s.phase,s.held_a,s.held_b,s.last_a,s.last_b,s.smooth_a,s.smooth_b};
        for(unsigned i=0;i<7;++i) v[j*7+i]=a[i];
    }
    v[14]=m.depth(0); v[15]=m.depth(1); v[16]=m.filter_modulation(); v[17]=m.split();
    v[18]=owner.cursor; return 1;
}
}
