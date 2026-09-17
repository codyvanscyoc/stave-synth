#define STAVE_OFFLINE_TRACE
#include "stave/stage_instrument.hpp"
using ProbeGraph=stave::StageInstrument;
using ProbeConfig=stave::StageInstrumentConfig;
#include "source_graph_probe.hpp"
#include "delay_fixture.hpp"
#include "master_fixture.hpp"
extern "C" {
int instrument_splits(void* h,const int* v,unsigned n) {
    if(!h||!v||n!=13||(v[0]!=0&&v[0]!=1)) return 0;
    auto& owner=*static_cast<CoreOwner*>(h); auto p=owner.config;
    p.splits={bool(v[0]),{v[1],v[2],v[3]},{v[4],v[5],v[6]},
              {v[7],v[8],v[9]},{v[10],v[11],v[12]}};
    if(!owner.graph->configure(p)) return 0;
    owner.config=p; return 1;
}
int instrument_key(void* h,std::uint64_t frame,int kind,int note,int value) {
    if(!h||kind<0||kind>4) return 0;
    return static_cast<CoreOwner*>(h)->graph->key_command(frame,static_cast<stave::StageAction>(kind),note,value);
}
int instrument_output(void* h,double volume,int btl) {
    if(!h||(btl!=0&&btl!=1)) return 0;
    auto& owner=*static_cast<CoreOwner*>(h); auto p=owner.config;
    p.output={volume,bool(btl)};
    if(!owner.graph->configure(p)) return 0;
    owner.config=p; return 1;
}
const float* instrument_pcm(void* h,unsigned c) { return h?static_cast<CoreOwner*>(h)->graph->pcm(c):nullptr; }
const float* instrument_tap(void* h,unsigned c) { return h?static_cast<CoreOwner*>(h)->graph->recording_tap(c):nullptr; }
const double* instrument_trace(void* h,unsigned c) {
    return h?static_cast<CoreOwner*>(h)->graph->trace(c):nullptr;
}
int instrument_effects(void* h,const double* d,const double* m,const double* routing,unsigned n) {
    if(!h||!routing||n!=9) return 0;
    for(unsigned i=0;i<n;++i) if(!std::isfinite(routing[i])) return 0;
    for(unsigned i:{4u,5u}) if(routing[i]!=0&&routing[i]!=1) return 0;
    auto& owner=*static_cast<CoreOwner*>(h); auto p=owner.config;
    if(!stave_fixture::delay_configuration(d,23,p.delay)||!stave_fixture::master_configuration(m,28,p.master)) return 0;
    p.wet=routing[0]; p.wet_gain=routing[1]; p.piano_reverb_send=routing[2]; p.piano_delay_send=routing[3];
    p.wet_filter=routing[4]; p.piano_filter=routing[5]; p.modulation={routing[6],routing[7],routing[8]};
    if(!owner.graph->configure(p)) return 0;
    owner.config=p; return 1;
}
int instrument_reverb(void* h,unsigned action,double value) {
    if(!h||!std::isfinite(value)) return 0;
    auto& graph=*static_cast<CoreOwner*>(h)->graph;
    if(action<7) return graph.reverb_control(static_cast<stave::ReverbControl>(action),value);
    if(action==7) return value>=0&&value<=6&&std::floor(value)==value&&graph.reverb_type(static_cast<stave::ReverbType>(int(value)));
    if(action==8) return (value==0||value==1)&&graph.freeze(value);
    if(action==10) return graph.retrigger_bpm();
    return 0;
}
}
