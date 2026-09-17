#pragma once
#include "stave/shared_effects.hpp"
#include <cmath>
namespace stave_fixture {
inline bool delay_configuration(const double* v,unsigned n,stave::DelayConfig& c) {
    if(!v||n!=23) return false;
    for(unsigned i=0;i<n;++i) if(!std::isfinite(v[i])) return false;
    for(unsigned i:{0u,1u,2u}) if(v[i]!=0&&v[i]!=1) return false;
    for(unsigned i:{3u,4u}) if(v[i]<0||v[i]>8||std::floor(v[i])!=v[i]) return false;
    c.enabled=v[0]; c.oblivion=v[1]; c.aurora=v[2];
    c.division=static_cast<stave::DelayDivision>(int(v[3])); c.reverse_division=static_cast<stave::DelayDivision>(int(v[4]));
    c.bpm=v[5]; c.milliseconds=v[6]; c.offset_ms=v[7]; c.rate=v[8]; c.feedback=v[9];
    c.wet=v[10]; c.motion=v[11]; c.lowcut=v[12]; c.highcut=v[13]; c.drive=v[14];
    c.width=v[15]; c.mod_rate=v[16]; c.mod_depth=v[17]; c.reverse=v[18];
    c.reverse_ms=v[19]; c.reverse_feedback=v[20]; c.aurora_seconds=v[21];
    return v[22]==0&&c.valid();
}
}
