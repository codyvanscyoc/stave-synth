// Test-only ABI: stage values are supplied as existing source/bus fixtures.
#include "stave/stage_core.hpp"
#include <cmath>
#include <vector>
#include "bus_fixture.hpp"
struct CoreOwner final : stave::PhaseSource {
    std::vector<stave::VoicePhases> tape;
    unsigned cursor{};
    stave::StageCoreConfig config;
    std::unique_ptr<stave::StageCore> graph;
    CoreOwner(const char* font,unsigned frames,const double* data,unsigned count) {
        if(!font||!data||!count||count>4096) throw std::invalid_argument("Invalid core phase fixture");
        for(unsigned i=0;i<count;++i) {
            for(unsigned j=0;j<4;++j) if(!std::isfinite(data[4*i+j])||data[4*i+j]<0||data[4*i+j]>1)
                throw std::invalid_argument("Invalid phase");
            tape.push_back({data[4*i],data[4*i+1],data[4*i+2],data[4*i+3]});
        }
        graph=std::make_unique<stave::StageCore>(font,*this,frames);
    }
    bool next(stave::VoicePhases& p) noexcept override {
        if(cursor==tape.size()) return false;
        p=tape[cursor++]; return true;
    }
};
extern "C" {
void* sources_create(const char* font,unsigned frames,const double* tape,unsigned n) noexcept {
    try { return new CoreOwner(font,frames,tape,n); } catch(...) { return nullptr; }
}
void sources_delete(void* p) { delete static_cast<CoreOwner*>(p); }
int sources_patch(void* h,const double* v,unsigned n) {
    if(!h||!v||n!=47) return 0;
    for(unsigned i=0;i<n;++i) if(!std::isfinite(v[i])||std::abs(v[i])>30000) return 0;
    for(unsigned i:{8u,9u,10u,11u,12u,13u,14u,22u,23u,24u,27u,28u,31u,35u,41u}) if(std::floor(v[i])!=v[i]) return 0;
    for(unsigned i:{22u,23u,24u,28u,35u,41u}) if(v[i]!=0&&v[i]!=1) return 0;
    if(v[46]!=0) return 0;
    auto& owner=*static_cast<CoreOwner*>(h); auto p=owner.config;
    p.env1={v[0],v[1],v[2],v[3]}; p.env2={v[4],v[5],v[6],v[7]};
    p.transpose=int(v[8]); p.piano_octave=int(v[9]); p.minimum_velocity=int(v[10]);
    p.wave1=int(v[11]); p.wave2=int(v[12]); p.octave1=int(v[13]); p.octave2=int(v[14]);
    p.fader1=v[15]; p.fader2=v[16]; p.detune=v[17]; p.spread=v[18]; p.pan1=v[19]; p.pan2=v[20];
    p.osc_bend_semitones=v[21]; p.buses.pad.shimmer=v[22]; p.buses.pad.shimmer_high=v[23];
    p.poly_lfo[0]={v[24]!=0,v[25],v[26],int(v[27])}; p.poly_lfo[1]={v[28]!=0,v[29],v[30],int(v[31])};
    p.piano.volume=v[32]; p.piano.lowcut_hz=v[33]; p.piano.highcut_hz=v[34];
    p.piano.comp_enabled=v[35]; p.piano.comp_threshold_db=v[36]; p.piano.comp_ratio=v[37];
    p.piano.comp_makeup_db=v[38]; p.piano.comp_drive_db=v[39]; p.piano.comp_wet=v[40];
    p.piano.brightness_enabled=v[41]; p.piano.brightness_amount=v[42];
    p.piano.tremolo_hz=v[43]; p.piano.tremolo_depth=v[44]; p.piano_velocity_curve=v[45];
    if(!owner.graph->configure(p)) return 0;
    owner.config=p; return 1;
}
int core_buses(void* h,const double* v,unsigned n,int hard_pan) {
    if(!h||(hard_pan!=0&&hard_pan!=1)) return 0;
    auto& owner=*static_cast<CoreOwner*>(h); auto p=owner.config;
    if(!stave_fixture::bus_configuration(v,n,p.buses)) return 0;
    p.hard_pan=hard_pan;
    if(!owner.graph->configure(p)) return 0;
    owner.config=p; return 1;
}
int sources_command(void* h,std::uint64_t frame,int kind,int note,int value,const double* weights) {
    if(!h||!weights||kind<0||kind>4) return 0;
    return static_cast<CoreOwner*>(h)->graph->command(frame,{static_cast<stave::StageAction>(kind),note,value,
        {weights[0],weights[1],weights[2],weights[3]}});
}
int sources_render(void* h) { return h&&static_cast<CoreOwner*>(h)->graph->render_block(); }
const double* sources_stem(void* h,unsigned c) { return h?static_cast<CoreOwner*>(h)->graph->stem(c):nullptr; }
const short* sources_raw(void* h) { return h?static_cast<CoreOwner*>(h)->graph->raw_piano():nullptr; }
void sources_stop(void* h) { if(h) static_cast<CoreOwner*>(h)->graph->stop(); }
unsigned sources_voices(void* h) { return h?static_cast<CoreOwner*>(h)->graph->active_voices():0; }
std::uint64_t sources_frame(void* h) { return h?static_cast<CoreOwner*>(h)->graph->frame_position():0; }
unsigned sources_phases(void* h) { return h?static_cast<CoreOwner*>(h)->cursor:0; }
int sources_stats(void* h,std::uint64_t* out) {
    if(!h||!out) return 0;
    const auto& s=static_cast<CoreOwner*>(h)->graph->stats();
    out[0]=s.rejected_commands; out[1]=s.fluid_errors; out[2]=s.unmatched_piano_offs;
    out[3]=s.full_scale_piano_samples; out[4]=s.phase_errors; out[5]=s.numeric_errors; return 1;
}
}
