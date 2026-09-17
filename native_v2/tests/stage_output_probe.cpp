#include "stave/stage_output.hpp"
extern "C" {
void* output_create(unsigned n) noexcept { try { return new stave::StageOutput(n); } catch(...) { return nullptr; } }
void output_delete(void* p) { delete static_cast<stave::StageOutput*>(p); }
int output_configure(void* p,double volume,int btl) {
    return p&&(btl==0||btl==1)&&static_cast<stave::StageOutput*>(p)->configure({volume,bool(btl)});
}
int output_process(void* p,const double* l,const double* r) { return p&&static_cast<stave::StageOutput*>(p)->process({l,r}); }
const float* output_channel(void* p,unsigned c) { return static_cast<stave::StageOutput*>(p)->channel(c); }
const float* output_tap(void* p,unsigned c) { return static_cast<stave::StageOutput*>(p)->recording_tap(c); }
float output_volume(void* p) { return static_cast<stave::StageOutput*>(p)->volume(); }
float output_peak(void* p) { return static_cast<stave::StageOutput*>(p)->peak(); }
void output_stop(void* p) { static_cast<stave::StageOutput*>(p)->stop(); }
}
