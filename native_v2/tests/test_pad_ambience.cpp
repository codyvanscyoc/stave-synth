#include "stave/pad_ambience.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool p) { if(!p) std::abort(); }
void silent(const stave::PadAmbience& bus,unsigned n) {
    for(unsigned c=0;c<6;++c) for(unsigned i=0;i<n;++i) check(bus.channel(c)[i]==0);
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
        stave::PadAmbience bus(n); silent(bus,n); check(!bus.channel(6));
        std::array<std::array<double,512>,5> data{};
        std::array<const double*,5> in{};
        for(unsigned c=0;c<5;++c) { in[c]=data[c].data(); data[c][0]=.1; }
        const std::array<const double*,2> send{data[0].data(),data[1].data()};
        stave::PadAmbienceConfig config; config.delay.enabled=true; config.delay.wet=.5;
        check(bus.configure(config)); check(bus.process(in,{true,true,true},&send,&send));
        counting=true;
        for(unsigned block=0;block<700;++block) {
            config.pad.send1=block%3?1:.2; config.pad.bypass2=block%5==0;
            config.wet=(block%101)/100.; config.delay.enabled=block%7!=0;
            if(block%70==0) check(bus.reverb_type(static_cast<stave::ReverbType>((block/70)%7)));
            if(block==10) check(bus.freeze(true));
            if(block==600) check(bus.freeze(false));
            check(bus.configure(config)); check(bus.process(in,{true,true,true},&send,&send));
            for(unsigned c=0;c<6;++c) for(unsigned i=0;i<n;++i) check(std::isfinite(bus.channel(c)[i]));
        }
        auto invalid=in; invalid[0]=bus.channel(0)+1;
        const double wet=bus.current_wet();
        check(!bus.process(invalid,{true,true,true})); check(bus.current_wet()==wet);
        config.wet=2; check(!bus.configure(config)); check(bus.current_wet()==wet);
        bus.stop(); silent(bus,n); check(!bus.configure({})&&!bus.freeze(false));
        check(!bus.process(in,{true,true,true})); counting=false; check(allocations==0);
        stave::PadAmbience failure(n);
        data[0][0]=std::numeric_limits<double>::quiet_NaN();
        check(!failure.process(in,{true,true,true})); silent(failure,n); check(!failure.healthy());
    }
    std::puts("PASS: composed ambience guards, 1400 transition blocks, alias/configuration refusal, terminal failure silence, zero C++ new/new[]");
}
