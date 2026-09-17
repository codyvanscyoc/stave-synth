#include "stave/stage_master.hpp"
#include "../src/master_filters.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool p) { if(!p) std::abort(); }
void silent(const stave::StageMaster& m,unsigned n) { for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) check(m.channel(c)[i]==0); }
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }
int main() {
    for(double ratio:{1.,4.,20.,1000.}) {
        check(stave::detail::master_compression_target(0,0,ratio)==0);
        check(stave::detail::master_compression_target(-1e-10,0,ratio)==0);
        check(stave::detail::master_compression_target(1e-10,0,ratio)==1e-10*(1-1/ratio));
    }
    counting=true; auto* calibration=::operator new(1); counting=false; ::operator delete(calibration);
    check(allocations==1); allocations=0;
    for(unsigned n:{256u,512u}) {
        stave::StageMaster master(n); stave::MasterConfig p;
        std::array<std::array<double,512>,6> signal{}; std::array<const double*,6> in{};
        for(unsigned c=0;c<6;++c) { in[c]=signal[c].data(); for(unsigned i=0;i<n;++i) signal[c][i]=2*std::sin(.1*i+c); }
        const std::array<const double*,2> piano{in[0],in[1]};
        check(master.process(in,&piano)); counting=true;
        for(unsigned block=0;block<1000;++block) {
            p.compression=block%3!=0; p.sidechain=static_cast<stave::SidechainSource>(block%4);
            p.fx_bypass=block%5==0; p.native_self=block%8<4; p.release_auto=block%2;
            p.space=(block%101)/100.; p.saturation=block%7==0; p.highpass=block%11!=0;
            p.slope=block%3==0?6:block%3==1?12:24; p.eq[0].gain=block%9-4.0;
            p.mix=block%3==0?0:block%3==1?.5:1; p.knee=block%3;
            check(master.configure(p)); if(block%13==0) check(master.retrigger_bpm());
            check(master.process(in,block%17?&piano:nullptr,{97,.6,.4}));
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) check(std::isfinite(master.channel(c)[i])&&std::abs(master.channel(c)[i])<=.98);
        }
        auto bad=in; bad[0]=master.channel(0)+1; const auto before=master.state();
        check(!master.process(bad)); check(master.state()==before);
        p.ratio=std::numeric_limits<double>::quiet_NaN(); check(!master.configure(p));
        check(!master.process(in,nullptr,{0,0,0})); check(master.state()==before);
        master.reset_limiter(); silent(master,n); master.stop();
        check(!master.process(in)&&!master.configure({})&&!master.retrigger_bpm());
        master.reset_limiter(); check(!master.healthy()); silent(master,n);
        counting=false; check(allocations==0);
        stave::StageMaster fail(n); signal[5][0]=std::numeric_limits<double>::infinity();
        check(!fail.process(in)); check(!fail.healthy()); silent(fail,n);
        std::array<double,512> constant; constant.fill(2);
        stave::LookaheadLimiter safe(n),reference(n,stave::LimiterPolicy::Reference);
        check(safe.process({constant.data(),constant.data()})); check(reference.process({constant.data(),constant.data()}));
        bool reproduced=false;
        for(unsigned i=0;i<n;++i) { check(std::abs(safe.channel(0)[i])<=.98); reproduced|=reference.channel(0)[i]>.98; }
        check(reproduced);
        for(unsigned i=0;i<72;++i) check(safe.channel(0)[i]==0);
        const auto gain=safe.gain();
        check(!safe.process({nullptr,constant.data()})); check(safe.gain()==gain);
        check(!safe.process({safe.channel(0)+1,constant.data()})); check(safe.gain()==gain);
        constant[0]=std::numeric_limits<double>::quiet_NaN();
        check(!safe.process({constant.data(),constant.data()})); check(!safe.healthy());
        safe.reset(); check(!safe.healthy());
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) check(safe.channel(c)[i]==0);
    }
    for(unsigned n:{0u,1u,128u,1024u}) {
        bool refused=false;
        try { stave::LookaheadLimiter invalid(n); } catch(const std::invalid_argument&) { refused=true; }
        check(refused);
    }
    std::puts("PASS: 2000 master transition blocks, all sidechains, finite/alias/config/stop guards, limiter ceiling and hard-knee corrections, zero C++ new/new[]");
}
