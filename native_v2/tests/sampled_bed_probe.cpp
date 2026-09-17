#include "stave/sampled_bed.hpp"
#include "stave/bed_bus.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>

namespace {
bool tracking=false;
unsigned allocations=0;
void check(bool ok) { if(!ok) { std::fputs("Sampled-bed guard failed\n",stderr); std::abort(); } }
void* allocate(std::size_t n) {
    if(tracking) ++allocations;
    if(auto* p=std::malloc(n?n:1)) return p;
    throw std::bad_alloc();
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }

int main(int argc,char** argv) {
    check(argc==4);
    const unsigned frames=unsigned(std::atoi(argv[1])),length=unsigned(std::atoi(argv[2]));
    const bool rise=std::atoi(argv[3])!=0;
    check((frames==256||frames==512)&&length>=4&&length<=100000);
    std::vector<double> left(length),right(length);
    for(unsigned i=0;i<length;++i) {
        left[i]=(int(i%97)-48)/512.; right[i]=(int(i%71)-35)/512.;
    }
    bool rejected=false;
    try { stave::PreparedBed invalid(left.data(),right.data(),3); } catch(const std::invalid_argument&) { rejected=true; }
    check(rejected); rejected=false;
    try { stave::PreparedBed invalid(left.data(),right.data(),stave::PreparedBed::max_bytes/16+1); }
    catch(const std::invalid_argument&) { rejected=true; }
    check(rejected);
    {
        auto prepared=std::make_unique<stave::SampledBed>();
        std::unique_ptr<const stave::PreparedBed> asset=std::make_unique<stave::PreparedBed>(left.data(),right.data(),length);
        check(prepared->install(0,asset));
        stave::BedBus bus(std::move(prepared),frames);
        check(bus.trigger(0)&&bus.fade(true,.02));
        tracking=true;
        check(bus.process());
        const double t=double(frames)/960,expected=1-t*t*(3-2*t);
        check(std::abs(bus.fade_gain()-expected)<1e-14&&bus.faded_target());
        const double before=bus.fade_gain();
        check(bus.fade(false,.02)&&bus.fade_gain()==before&&!bus.faded_target());
        for(unsigned b=0;b<4;++b) check(bus.process());
        check(bus.fade_gain()==1);
        stave::BedConfig config; config.level=0;
        check(bus.configure(config));
        for(unsigned b=0;b<150;++b) check(bus.process());
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) check(bus.channel(c)[i]==0);
        config.level=std::numeric_limits<double>::quiet_NaN(); check(!bus.configure(config)&&bus.healthy());
        check(!bus.fade(true,0)&&!bus.fade(true,31));
        bus.stop(); check(!bus.process()&&!bus.trigger(0)&&bus.active()==0);
        tracking=false; check(allocations==0);
    }
    rejected=false;
    try { stave::PreparedBed invalid(left.data(),right.data(),4,44100); }
    catch(const std::invalid_argument&) { rejected=true; }
    check(rejected);
    {
        // Scaled-down budget tests exercise exact ownership/accounting without
        // allocating128MiB merely to test the cap.
        stave::SampledBed bounded(128);
        std::unique_ptr<const stave::PreparedBed> asset=std::make_unique<stave::PreparedBed>(left.data(),right.data(),4);
        check(bounded.install(0,asset)&&!asset&&bounded.bytes()==64);
        asset=std::make_unique<stave::PreparedBed>(left.data(),right.data(),4);
        check(bounded.install(1,asset)&&!asset&&bounded.bytes()==128);
        asset=std::make_unique<stave::PreparedBed>(left.data(),right.data(),4);
        const auto* original=asset.get();
        check(!bounded.install(2,asset)&&asset.get()==original&&bounded.bytes()==128);
        check(bounded.install(0,asset)&&asset&&asset.get()!=original&&bounded.bytes()==128);
        asset.reset(); check(bounded.install(1,asset)&&asset&&bounded.bytes()==64);
        std::array<double,4> bad{}; bad[1]=std::numeric_limits<double>::quiet_NaN();
        rejected=false;
        try { stave::PreparedBed invalid(bad.data(),bad.data(),4); }
        catch(const std::invalid_argument&) { rejected=true; }
        check(rejected);
    }
    stave::SampledBed bank;
    std::unique_ptr<const stave::PreparedBed> a=std::make_unique<stave::PreparedBed>(left.data(),right.data(),length);
    check(!bank.trigger(0)&&!bank.install(12,a)&&bool(a));
    check(bank.install(0,a)&&!a);
    a=std::make_unique<stave::PreparedBed>(right.data(),left.data(),length);
    check(bank.install(1,a)&&!a); check(bank.bytes()==length*32);
    bank.seal(); check(!bank.install(0,a));
    std::array<double,513> l{},r{}; l[512]=17; r[512]=18;
    check(!bank.process(l.data(),r.data(),513));
    check(!bank.process(l.data(),l.data()+1,frames));
    check(!bank.process(nullptr,r.data(),frames));
    check(!bank.trigger(0,-1)&&!bank.trigger(0,61)&&!bank.trigger(0,1,100));
    tracking=true; auto* calibration=::operator new(1); tracking=false;
    ::operator delete(calibration); check(allocations==1); allocations=0;
    for(unsigned block=0;block<850;++block) {
        tracking=true;
        if(block==0) check(bank.trigger(0,rise?1.2:0));
        if(block==15) check(!bank.trigger(11)); // missing key never steals active bed
        if(block==160) check(bank.trigger(1,rise?2:0,4800));
        if(block==280||block==550) bank.release_all();
        if(block==350) check(bank.trigger(0,rise?.8:0));
        if(block==450) check(bank.trigger(0));
        if(block==650) { bank.stop(); check(bank.active()==0); }
        if(block==675) check(bank.trigger(1));
        std::fill_n(l.data(),frames,0); std::fill_n(r.data(),frames,0);
        check(bank.process(l.data(),r.data(),frames));
        check(bank.active()<=2&&bank.healthy());
        tracking=false;
        for(unsigned i=0;i<frames;++i) {
            const std::array<double,2> sample{l[i],r[i]};
            check(std::fwrite(sample.data(),sizeof(double),2,stdout)==2);
        }
    }
    check(allocations==0&&l[512]==17&&r[512]==18);
    bank.stop(); l[0]=std::numeric_limits<double>::infinity();
    check(!bank.process(l.data(),r.data(),frames)&&!bank.healthy()&&!bank.trigger(0));
    for(unsigned i=0;i<frames;++i) check(l[i]==0&&r[i]==0);
    std::fputs("PASS: sampled bed guards and allocation-free850-block fixture\n",stderr);
}
