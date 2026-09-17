#pragma once
#include "stave/stage_motion.hpp"
namespace stave_fixture {
inline bool motion_configuration(const double* v,unsigned count,stave::MotionConfig& p) {
    if(!v||count!=35) return false;
    for(unsigned i=0;i<count;++i) if(!std::isfinite(v[i])) return false;
    p.mix=v[0]; p.bpm=v[1]; p.haas_ms=v[2];
    for(unsigned j=0;j<2;++j) {
        const auto* a=v+3+16*j; auto& l=p.lfo[j];
        for(unsigned i=6;i<16;++i) if(std::floor(a[i])!=a[i]) return false;
        if(a[6]<0||a[6]>8||a[7]<0||a[7]>6||a[8]<0||a[8]>3) return false;
        for(unsigned i=9;i<16;++i) if(a[i]!=0&&a[i]!=1) return false;
        l.rate=a[0]; l.multiplier=a[1]; l.depth=a[2]; l.spread=a[3]; l.offset_ms=a[4]; l.smooth=a[5];
        l.division=static_cast<stave::DelayDivision>(int(a[6])); l.shape=static_cast<stave::MotionShape>(int(a[7]));
        l.target=static_cast<stave::MotionTarget>(int(a[8])); l.active=a[9]; l.key_sync=a[10]; l.invert=a[11];
        l.haas_compensate=a[12]; l.poly=a[13]; l.received={bool(a[14]),bool(a[15])};
    }
    return p.valid();
}
}
