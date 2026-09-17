#include "stave/stage_output.hpp"
#include <cstdlib>
#include <cstdio>
#include <new>
namespace {
unsigned allocations{}; bool counting{};
void check(bool x) { if(!x) std::abort(); }
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void silent(const stave::StageOutput& o) { for(unsigned c=0;c<2;++c) for(unsigned i=0;i<o.block_frames();++i) check(o.channel(c)[i]==0&&o.recording_tap(c)[i]==0); }
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }
int main() {
    counting=true; auto* x=::operator new(1); counting=false; ::operator delete(x); check(allocations==1); allocations=0;
    for(unsigned n:{256u,512u}) {
        stave::StageOutput o(n); std::array<double,512> input{};
        for(unsigned i=0;i<n;++i) input[i]=2*std::sin(.1*i);
        check(!o.channel(2)&&!o.recording_tap(2)); silent(o); counting=true;
        for(unsigned b=0;b<1000;++b) {
            check(o.configure({(b%101)/100.,bool(b%2)})); check(o.process({input.data(),input.data()}));
            for(unsigned i=0;i<n;++i) {
                check(o.recording_tap(0)[i]==float(input[i]));
                check(std::abs(o.channel(0)[i])<=1&&std::abs(o.channel(1)[i])<=1);
                if(b%2) check(o.channel(1)[i]==-o.channel(0)[i]);
            }
        }
        const auto v=o.volume();
        check(!o.configure({1.1,false})&&!o.configure({std::numeric_limits<double>::quiet_NaN(),false}));
        check(!o.process({nullptr,input.data()})&&o.volume()==v);
        check(!o.process({reinterpret_cast<const double*>(o.channel(0)),input.data()})&&o.volume()==v);
        o.stop(); silent(o); check(!o.process({input.data(),input.data()})&&!o.configure({}));
        counting=false; check(allocations==0);
        for(double bad:{std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::max()}) {
            stave::StageOutput failure(n); input[n-1]=bad;
            check(!failure.process({input.data(),input.data()})&&!failure.healthy()); silent(failure);
        }
    }
    std::puts("PASS: 2000 output blocks, volume/BTL/recorder tap, finite/alias/config/fault/stop guards, zero C++ new/new[]");
}
