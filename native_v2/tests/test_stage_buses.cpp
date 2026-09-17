#include "stave/stage_buses.hpp"
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{};
unsigned allocations{};
void check(bool p) { if (!p) std::abort(); }
void* allocate(std::size_t n) {
    if (counting) ++allocations;
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void silent(const stave::StageBuses& bus, unsigned n) {
    for (unsigned c=0; c<11; ++c) for (unsigned i=0; i<n; ++i) check(bus.stem(c)[i]==0);
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }
int main() {
    for (unsigned n : {0u, 1u, 255u, 513u}) {
        bool rejected=false;
        try { stave::StageBuses bus(n); } catch (...) { rejected=true; }
        check(rejected);
    }
    counting=true; void* calibration=::operator new(1); counting=false;
    ::operator delete(calibration); check(allocations==1); allocations=0;
    for (unsigned frames : {256u,512u}) {
        stave::StageBuses bus(frames);
        std::array<std::array<double,512>,7> input{};
        std::array<const double*,7> ptrs{};
        for (unsigned c=0; c<7; ++c) {
            ptrs[c]=input[c].data();
            for (unsigned i=0;i<frames;++i) input[c][i]=.05*std::sin(i*(c+1)*.03);
        }
        stave::StageBusConfig config;
        check(!bus.stem(11)); silent(bus,frames);
        auto invalid=ptrs; invalid[5]=nullptr;
        auto before=bus.state();
        check(!bus.process_block(invalid,{true,true,true})); check(bus.state()==before);
        invalid=ptrs; invalid[0]=bus.stem(10);
        check(!bus.process_block(invalid,{true,true,true})); check(bus.state()==before);
        config.pad.cutoff=30; config.room.wet=std::numeric_limits<double>::quiet_NaN();
        check(!bus.configure(config)); check(bus.state()==before); config={};
        for (double value : {-1.,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}) {
            config.pad.cutoff=value; check(!bus.configure(config)); config={};
            config.pad.send1=value; check(!bus.configure(config)); config={};
            config.pad.shimmer_mix=value; check(!bus.configure(config)); config={};
        }
        config.pad.haas_samples=4096; check(!bus.configure(config)); config={};
        check(bus.process_block(ptrs,{true,true,false}));
        counting=true;
        for (unsigned block=0;block<1000;++block) {
            auto& p=config.pad;
            p.cutoff=20+(block%200)*90; p.resonance=.1+(block%100)*.099;
            p.slope24=block%3; p.shared1=block%7; p.shared2=block%11;
            p.independent1=500; p.independent2=1500; p.highpass=block%2?20:5000;
            p.range_min=block%19?150:20000; p.range_max=block%19?20000:20;
            p.bypass1=block%23==0; p.bypass2=block%13==0;
            p.send1=block%5?1:0; p.send2=block%7?1:2;
            p.shimmer=block%5; p.shimmer_mix=block%7?.8:0; p.shimmer_high=block%2;
            p.shimmer_send=block%3?1:0; p.haas_samples=block%2?0:4095;
            config.room.enabled=block%29; config.room.wet=(block%100)/100.;
            check(bus.configure(config)); check(bus.process_block(ptrs,{block%3!=0,block%5!=0,block%7!=0}));
            for(unsigned c=0;c<11;++c) for(unsigned i=0;i<frames;++i) check(std::isfinite(bus.stem(c)[i]));
            if(block%71==0) bus.clear();
        }
        bus.clear(); counting=false; check(allocations==0); silent(bus,frames);
        // Full re-init must really flush the CLOUD rwtable, not just filter
        // registers. Re-engage all processing into silence for longer than it.
        for(auto& c:input) c.fill(0);
        config={}; config.pad.shimmer=true; config.pad.shimmer_mix=1;
        check(bus.configure(config));
        for(unsigned i=0;i<150;++i) { check(bus.process_block(ptrs,{true,true,true})); silent(bus,frames); }
        input[0][0]=.25;
        check(bus.process_block(ptrs,{true,true,true})); bus.stop(); silent(bus,frames);
        check(!bus.healthy() && !bus.configure(config) && !bus.process_block(ptrs,{true,true,true}));
        bus.clear(); bus.stop(); check(!bus.healthy()); silent(bus,frames);
        // A piano failure must silence oscillator output too, and vice versa.
        for(unsigned bad_channel : {0u,5u}) {
            stave::StageBuses fail(frames);
            input[bad_channel][5]=std::numeric_limits<double>::quiet_NaN();
            check(!fail.process_block(ptrs,{true,true,true})); check(!fail.healthy()); silent(fail,frames);
            input[bad_channel][5]=0; fail.clear(); check(!fail.healthy());
        }
    }
    std::puts("PASS: downstream configuration/bounds/aliasing, 2000 control blocks, CLOUD clear, coupled fail-silence, terminal stop, calibrated C++ allocation probe");
}
