#include "stave/shared_effects.hpp"
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool p) { if(!p) std::abort(); }
template<class T> void silence(const T& effect,unsigned n) {
    for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) check(effect.channel(c)[i]==0);
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }
int main() {
    counting=true; auto* calibration=::operator new(1); counting=false; ::operator delete(calibration);
    check(allocations==1); allocations=0;
    for(unsigned n:{256u,512u}) {
        stave::StageDelay delay(n); stave::SharedReverb reverb(n);
        std::array<double,512> l{},r{}; l[0]=.4; r[0]=-.3;
        stave::DelayConfig p; p.enabled=true; p.wet=.4;
        check(delay.configure(p)); check(delay.process({l.data(),r.data()})); check(reverb.process({l.data(),r.data()}));
        counting=true;
        for(unsigned block=0;block<800;++block) {
            if(block%80==0) check(reverb.set_type(static_cast<stave::ReverbType>((block/80)%7)));
            if(block==10) check(reverb.freeze(true));
            if(block==600) check(reverb.freeze(false));
            if(block==650) reverb.panic();
            p.enabled=block%5!=0; p.reverse=.3; p.rate=block%2?-.1:2;
            check(delay.configure(p)); check(delay.process({l.data(),r.data()}));
            check(reverb.process({delay.channel(0),delay.channel(1)}));
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) check(std::isfinite(reverb.channel(c)[i]));
        }
        p.wet=std::numeric_limits<double>::quiet_NaN(); check(!delay.configure(p));
        check(!reverb.set(stave::ReverbControl::Decay,p.wet));
        check(!reverb.set_type(static_cast<stave::ReverbType>(99)));
        check(!reverb.set(static_cast<stave::ReverbControl>(99),0));
        check(!delay.process({nullptr,r.data()}));
        check(!reverb.process({reverb.channel(0),r.data()}));
        check(!delay.process({delay.channel(0)+1,r.data()}));
        delay.stop(); reverb.stop(); silence(delay,n); silence(reverb,n);
        check(!delay.process({l.data(),r.data()})); check(!reverb.process({l.data(),r.data()}));
        delay.clear(); reverb.panic(); check(!delay.healthy()&&!reverb.healthy());
        counting=false; check(allocations==0);
        stave::StageDelay bad_delay(n); stave::SharedReverb bad_reverb(n);
        l[0]=std::numeric_limits<double>::infinity();
        check(!bad_delay.process({l.data(),r.data()})); check(!bad_reverb.process({l.data(),r.data()}));
        silence(bad_delay,n); silence(bad_reverb,n);
        l[0]=0; bad_delay.clear(); bad_reverb.panic();
        check(!bad_delay.process({l.data(),r.data()})); check(!bad_reverb.process({l.data(),r.data()}));
    }
    std::puts("PASS: shared effect finite/configuration/alias/freeze/type/stop guards; zero post-warmup C++ new/new[]");
}
