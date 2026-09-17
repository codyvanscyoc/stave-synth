#include "stave/stage_splits.hpp"
#include <cstdio>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool value) { if(!value) std::abort(); }
bool same(stave::LayerWeights a,stave::LayerWeights b) {
    return a.osc1==b.osc1&&a.osc2==b.osc2&&a.shimmer==b.shimmer&&a.piano==b.piano;
}
void guards() {
    counting=true; auto* p=::operator new(1); counting=false; ::operator delete(p);
    check(allocations==1); allocations=0; counting=true;
    for(bool enabled:{false,true}) {
        stave::StageSplits s; s.enabled=enabled;
        const stave::LayerWeights sentinel{.1,.2,.3,.4};
        for(int bad:{-1,128,std::numeric_limits<int>::min(),std::numeric_limits<int>::max()}) {
            auto out=sentinel; check(!s.weights(bad,out)&&same(out,sentinel));
            for(int field=0;field<3;++field) for(int zone=0;zone<4;++zone) {
                auto invalid=s;
                auto* range=zone==0?&invalid.osc1:zone==1?&invalid.osc2:zone==2?&invalid.shimmer:&invalid.piano;
                if(field==0) range->low=bad;
                else if(field==1) range->high=bad;
                else range->crossfade=bad;
                check(!invalid.valid()&&!invalid.weights(60,out)&&same(out,sentinel));
            }
        }
        s.osc1.crossfade=25; check(!s.valid());
        s={}; s.enabled=enabled; s.osc1={60,60,24}; s.osc2={127,0,24};
        for(unsigned pass=0;pass<1000;++pass) for(int note=0;note<128;++note) {
            stave::LayerWeights w; check(s.weights(note,w)&&w.valid());
            if(!enabled) check(same(w,{}));
        }
    }
    counting=false; check(allocations==0);
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }
int main() {
    guards(); std::cout << std::setprecision(17);
    int enabled;
    while(std::cin>>enabled) {
        stave::StageSplits s; s.enabled=enabled;
        for(auto* r:{&s.osc1,&s.osc2,&s.shimmer,&s.piano}) std::cin>>r->low>>r->high>>r->crossfade;
        check(bool(std::cin)&&(enabled==0||enabled==1)&&s.valid());
        for(int note=0;note<128;++note) {
            stave::LayerWeights w; check(s.weights(note,w));
            std::cout<<w.osc1<<' '<<w.osc2<<' '<<w.shimmer<<' '<<w.piano<<'\n';
        }
    }
}
