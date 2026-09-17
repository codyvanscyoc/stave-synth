#include "stave/pad_ambience.hpp"
#include "bus_fixture.hpp"
#include "delay_fixture.hpp"
extern "C" {
void* ambience_create(unsigned frames) noexcept {
    try { return new stave::PadAmbience(frames); } catch(...) { return nullptr; }
}
void ambience_delete(void* p) { delete static_cast<stave::PadAmbience*>(p); }
int ambience_configure(void* p,const double* bus,const double* delay,double wet,double gain) {
    if(!p) return 0;
    stave::StageBusConfig b; stave::PadAmbienceConfig c;
    if(!stave_fixture::bus_configuration(bus,23,b)||!stave_fixture::delay_configuration(delay,23,c.delay)) return 0;
    c.pad=b.pad; c.wet=wet; c.wet_gain=gain;
    return static_cast<stave::PadAmbience*>(p)->configure(c);
}
int ambience_command(void* p,unsigned action,double value) {
    if(!p||!std::isfinite(value)) return 0;
    auto& a=*static_cast<stave::PadAmbience*>(p);
    if(action<7) return a.reverb_control(static_cast<stave::ReverbControl>(action),value);
    if(action==7) return value>=0&&value<=6&&std::floor(value)==value&&a.reverb_type(static_cast<stave::ReverbType>(int(value)));
    if(action==8) return (value==0||value==1)&&a.freeze(value);
    return 0;
}
int ambience_process(void* p,const double* source,unsigned n,unsigned flags,const double* delay,const double* reverb) {
    if(!p||!source||(n!=256&&n!=512)||flags>15||static_cast<stave::PadAmbience*>(p)->block_frames()!=n) return 0;
    std::array<const double*,5> in{}; for(unsigned c=0;c<5;++c) in[c]=source+c*n;
    const std::array<const double*,2> d{delay,delay?delay+n:nullptr},r{reverb,reverb?reverb+n:nullptr};
    return static_cast<stave::PadAmbience*>(p)->process(in,{bool(flags&1),bool(flags&2),bool(flags&4),!(flags&8)},delay?&d:nullptr,reverb?&r:nullptr);
}
const double* ambience_channel(void* p,unsigned c) { return static_cast<stave::PadAmbience*>(p)->channel(c); }
double ambience_wet(void* p) { return static_cast<stave::PadAmbience*>(p)->current_wet(); }
void ambience_stop(void* p) { static_cast<stave::PadAmbience*>(p)->stop(); }
}
