#pragma once
#include "stave/stage_master.hpp"
#include <cmath>
namespace stave_fixture {
inline bool master_configuration(const double* v,unsigned n,stave::MasterConfig& c) {
    if(!v||n!=28) return 0;
    for(unsigned i=0;i<n;++i) if(!std::isfinite(v[i])) return 0;
    for(unsigned i:{9u,14u,15u,16u,17u,18u}) if(v[i]!=0&&v[i]!=1) return 0;
    if((v[11]!=6&&v[11]!=12&&v[11]!=24)||v[19]<0||v[19]>3||std::floor(v[19])!=v[19]) return 0;
    for(unsigned i=0;i<3;++i) c.eq[i]={v[3*i],v[3*i+1],v[3*i+2]};
    c.highpass=v[9]; c.cutoff=v[10]; c.slope=int(v[11]); c.space=v[12]; c.pre_gain=v[13]; c.saturation=v[14];
    c.compression=v[15]; c.fx_bypass=v[16]; c.native_self=v[17]; c.release_auto=v[18]; c.sidechain=static_cast<stave::SidechainSource>(int(v[19]));
    c.threshold=v[20]; c.ratio=v[21]; c.attack=v[22]; c.release=v[23]; c.knee=v[24]; c.makeup=v[25]; c.mix=v[26]; c.sc_highpass=v[27];
    return c.valid();
}
}
