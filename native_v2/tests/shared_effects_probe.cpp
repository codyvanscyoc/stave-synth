#include "stave/shared_effects.hpp"
#include <cmath>
#include "delay_fixture.hpp"

extern "C" {
void* effects_create(unsigned kind,unsigned frames) noexcept {
    try {
        if(kind==0) return new stave::StageDelay(frames);
        if(kind==1) return new stave::SharedReverb(frames);
    } catch(...) {}
    return nullptr;
}
void effects_delete(unsigned kind,void* p) {
    if(kind==0) delete static_cast<stave::StageDelay*>(p);
    if(kind==1) delete static_cast<stave::SharedReverb*>(p);
}
int delay_configure(void* p,const double* v,unsigned n) {
    stave::DelayConfig c;
    if(!p||!stave_fixture::delay_configuration(v,n,c)) return 0;
    return static_cast<stave::StageDelay*>(p)->configure(c);
}
int effects_process(unsigned kind,void* p,const double* l,const double* r,const double* el,const double* er) {
    if(!p) return 0;
    if(kind==0) {
        const std::array<const double*,2> ext{el,er};
        if(bool(el)!=bool(er)) return 0;
        return static_cast<stave::StageDelay*>(p)->process({l,r},el?&ext:nullptr);
    }
    return kind==1&&static_cast<stave::SharedReverb*>(p)->process({l,r});
}
const double* effects_channel(unsigned kind,void* p,unsigned c) {
    if(!p) return nullptr;
    if(kind==0) return static_cast<stave::StageDelay*>(p)->channel(c);
    return kind==1?static_cast<stave::SharedReverb*>(p)->channel(c):nullptr;
}
// 0..6 setters, 7 type, 8 freeze, 9 panic, 10 stop.
int reverb_command(void* p,unsigned action,double value) {
    if(!p||!std::isfinite(value)) return 0;
    auto& r=*static_cast<stave::SharedReverb*>(p);
    if(action<7) return r.set(static_cast<stave::ReverbControl>(action),value);
    if(action==7) return value>=0&&value<=6&&std::floor(value)==value&&r.set_type(static_cast<stave::ReverbType>(int(value)));
    if(action==8) return (value==0||value==1)&&r.freeze(value);
    if(action==9) { r.panic(); return 1; }
    if(action==10) { r.stop(); return 1; }
    return 0;
}
double reverb_zone(void* p,unsigned b,const char* n) { return static_cast<stave::SharedReverb*>(p)->zone(b,n); }
int reverb_state(void* p,unsigned field) {
    auto& r=*static_cast<stave::SharedReverb*>(p);
    if(field==0) return int(r.type());
    if(field==1) return r.frozen();
    return r.capture_remaining();
}
void effects_clear(unsigned kind,void* p) {
    if(kind==0) static_cast<stave::StageDelay*>(p)->clear();
    if(kind==1) static_cast<stave::SharedReverb*>(p)->panic();
}
void effects_stop(unsigned kind,void* p) {
    if(kind==0) static_cast<stave::StageDelay*>(p)->stop();
    if(kind==1) static_cast<stave::SharedReverb*>(p)->stop();
}
}
