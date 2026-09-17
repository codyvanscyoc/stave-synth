#pragma once
// Private float32 Faust ownership helper for shared effects only.
#include <faust/gui/CInterface.h>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace stave::detail {
struct StereoApi {
    void* (*create)(); void (*destroy)(void*); void (*init)(void*,int);
    void (*clear)(void*); void (*ui)(void*,UIGlue*);
    void (*compute)(void*,int,float**,float**);
};
class StereoUnit final {
public:
    explicit StereoUnit(StereoApi api): api_(api), dsp_(api.create()) {
        if(!dsp_) throw std::runtime_error("Faust effect allocation failed");
        api_.init(dsp_,48000);
        UIGlue ui{this,box,box,box,end,zone,zone,slider,slider,slider,bar,bar,soundfile,declare};
        api_.ui(dsp_,&ui);
        if(overflow_) { api_.destroy(dsp_); dsp_=nullptr; throw std::runtime_error("Effect zone capacity exceeded"); }
    }
    ~StereoUnit() { if(dsp_) api_.destroy(dsp_); }
    StereoUnit(const StereoUnit&)=delete;
    StereoUnit& operator=(const StereoUnit&)=delete;
    void require(const char* name) const { if(!find(name)) throw std::runtime_error("Missing effect control zone"); }
    void set(const char* name,double value) noexcept { if(auto* p=find(name)) *p=static_cast<float>(value); }
    double get(const char* name) const noexcept { const auto* p=find(name); return p?*p:std::numeric_limits<double>::quiet_NaN(); }
    void clear() noexcept { api_.clear(dsp_); }
    bool process(const std::array<const double*,2>& input,unsigned n) noexcept {
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) {
            if(!std::isfinite(input[c][i])||std::abs(input[c][i])>std::numeric_limits<float>::max()) return false;
            in_[c][i]=static_cast<float>(input[c][i]);
        }
        api_.compute(dsp_,static_cast<int>(n),ins_.data(),outs_.data());
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) {
            if(!std::isfinite(out_[c][i])) return false;
            result_[c][i]=out_[c][i];
        }
        return true;
    }
    const double* channel(unsigned c) const noexcept { return result_[c].data(); }
private:
    float* find(const char* name) const noexcept {
        if(!name) return nullptr;
        for(unsigned i=0;i<count_;++i) if(std::strcmp(names_[i],name)==0) return zones_[i];
        return nullptr;
    }
    static void box(void*,const char*) {} static void end(void*) {}
    static void zone(void* p,const char* n,float* v) {
        auto& s=*static_cast<StereoUnit*>(p);
        if(s.count_==s.names_.size()) { s.overflow_=true; return; }
        s.names_[s.count_]=n; s.zones_[s.count_++]=v;
    }
    static void slider(void* p,const char* n,float* v,float,float,float,float) { zone(p,n,v); }
    static void bar(void*,const char*,float*,float,float) {}
    static void soundfile(void*,const char*,const char*,Soundfile**) {}
    static void declare(void*,float*,const char*,const char*) {}
    StereoApi api_; void* dsp_;
    std::array<const char*,32> names_{};
    std::array<float*,32> zones_{};
    unsigned count_{}; bool overflow_{};
    std::array<std::array<float,512>,2> in_{},out_{};
    std::array<std::array<double,512>,2> result_{};
    std::array<float*,2> ins_{in_[0].data(),in_[1].data()},outs_{out_[0].data(),out_[1].data()};
};
inline bool range(double x,double lo,double hi) noexcept { return std::isfinite(x)&&x>=lo&&x<=hi; }
inline void frames_valid(unsigned n) { if(n!=256&&n!=512) throw std::invalid_argument("Effects require fixed256/512 at48k"); }
inline bool disjoint(const double* p,const double* q,unsigned n) noexcept {
    const auto a=reinterpret_cast<std::uintptr_t>(p),b=reinterpret_cast<std::uintptr_t>(q);
    return (a<b?b-a:a-b)>=n*sizeof(double);
}
} // namespace stave::detail

// Generate type-safe adapters for Faust's opaque C structs; no function-pointer casts.
#define STAVE_STEREO_API(Name) \
extern "C" { struct Name; Name* new##Name(); void delete##Name(Name*); \
void init##Name(Name*,int); void instanceClear##Name(Name*); \
void buildUserInterface##Name(Name*,UIGlue*); void compute##Name(Name*,int,float**,float**); } \
static stave::detail::StereoApi api##Name() { return { \
[]()->void* {return new##Name();}, [](void* p){delete##Name(static_cast<Name*>(p));}, \
[](void* p,int n){init##Name(static_cast<Name*>(p),n);}, \
[](void* p){instanceClear##Name(static_cast<Name*>(p));}, \
[](void* p,UIGlue* ui){buildUserInterface##Name(static_cast<Name*>(p),ui);}, \
[](void* p,int n,float** in,float** out){compute##Name(static_cast<Name*>(p),n,in,out);} }; }
