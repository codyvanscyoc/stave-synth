#include "stave/stage_motion.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool v) { if(!v) std::abort(); }
struct Random final:stave::MotionRandom {
    unsigned count{}; bool bad{},unavailable{};
    bool next(double& v) noexcept override { ++count; v=bad?std::numeric_limits<double>::quiet_NaN():double(count%101)/50.-1.; return !unavailable; }
};
void silent(const stave::StageMotion& m) {
    for(unsigned i=0;i<m.block_frames();++i) {
        for(unsigned g=0;g<2;++g) for(unsigned c=0;c<4;++c) check(m.oscillator(g,c)[i]==0);
        check(m.bus(0)[i]==0&&m.bus(1)[i]==0);
    }
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
    for(unsigned size:{256u,512u}) {
        Random random; stave::StageMotion m(size,&random); stave::MotionConfig p;
        check(!m.oscillator(2,0)&&!m.oscillator(0,4)&&!m.bus(2)&&!m.take_filter_retune());
        counting=true;
        // Every floating configuration field rejects nonfinite/out-of-range
        // input before changing any target/phase/smoother state.
        for(unsigned field=0;field<15;++field) {
            stave::MotionConfig invalid;
            std::array<double*,15> fields{&invalid.mix,&invalid.bpm,&invalid.haas_ms,
                &invalid.lfo[0].rate,&invalid.lfo[0].multiplier,&invalid.lfo[0].depth,&invalid.lfo[0].spread,&invalid.lfo[0].offset_ms,&invalid.lfo[0].smooth,
                &invalid.lfo[1].rate,&invalid.lfo[1].multiplier,&invalid.lfo[1].depth,&invalid.lfo[1].spread,&invalid.lfo[1].offset_ms,&invalid.lfo[1].smooth};
            const std::array<double,15> lows{0,40,5,.05,.1,0,0,-500,0,.05,.1,0,0,-500,0};
            const std::array<double,15> highs{1,240,40,20,10,1,1,500,1,20,10,1,1,500,1};
            for(double bad:{lows[field]-1,highs[field]+1,std::numeric_limits<double>::infinity(),
                            -std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}) {
                *fields[field]=bad; check(!m.configure(invalid)&&!m.take_filter_retune());
            }
            check(m.state(0).phase==0&&m.state(1).smooth_b==0);
        }
        for(unsigned b=0;b<3000;++b) {
            p.mix=b%13?1:0; p.bpm=40+b%201;
            for(unsigned j=0;j<2;++j) {
                auto& l=p.lfo[j]; l.depth=.8; l.rate=20; l.spread=.8; l.smooth=b%11?.5:0;
                l.shape=static_cast<stave::MotionShape>((b/30+j)%7);
                l.target=static_cast<stave::MotionTarget>((b/70+j)%4);
                l.key_sync=b%3; l.active=b%5; l.received={bool(b%2),bool(b%7)};
            }
            check(m.configure(p)); if(b%97==0) check(m.key_trigger());
            check(m.process(b%2));
            check(std::isfinite(m.filter_modulation())&&std::abs(m.filter_modulation())<=1.4);
            for(unsigned i=0;i<size;++i) for(unsigned g=0;g<2;++g) {
                for(unsigned c=0;c<2;++c) {
                    const double amp=m.oscillator(g,c)[i],pan=m.oscillator(g,c+2)[i];
                    check(std::isfinite(amp)&&amp>=0&&amp<=1.000000000001);
                    check(std::isfinite(pan)&&std::abs(pan)<=.700000000001);
                    check(std::isfinite(m.bus(c)[i])&&m.bus(c)[i]>=0&&m.bus(c)[i]<=1.000000000001);
                }
            }
            m.take_filter_retune();
            const auto before=m.state(0);
            auto bad=p; bad.lfo[0].depth=std::numeric_limits<double>::quiet_NaN();
            check(!m.configure(bad)); bad=p; bad.lfo[0].target=static_cast<stave::MotionTarget>(4);
            check(!m.configure(bad)); bad=p; bad.lfo[1].division=static_cast<stave::DelayDivision>(-1);
            check(!m.configure(bad)); bad=p; bad.lfo[1].shape=static_cast<stave::MotionShape>(7);
            check(!m.configure(bad)&&!m.take_filter_retune()&&m.state(0).last_a==before.last_a&&m.state(0).phase==before.phase);
        }
        check(random.count>0); m.stop(); silent(m);
        check(!m.process()&&!m.configure(p)&&!m.key_trigger()); silent(m);
        counting=false; check(allocations==0);
        for(int failure=0;failure<3;++failure) {
            Random bad; bad.bad=failure==1; bad.unavailable=failure==2;
            stave::StageMotion f(size,failure==0?nullptr:&bad); p={};
            p.lfo[0].shape=stave::MotionShape::SampleHold; p.lfo[0].depth=1; p.lfo[0].rate=20;
            check(f.configure(p));
            for(unsigned b=0;b<20&&f.healthy();++b) f.process();
            check(!f.healthy()); silent(f); check(!f.process());
        }
        Random draws; stave::FilterMotion filter(&draws);
        counting=true;
        for(unsigned b=0;b<3000;++b) {
            check(filter.configure({b%3?40.:0.,b%5?1.:0.}));
            check(filter.process(b%2?8000:20,b%2?1.4:-1.4,b%7?10:.5));
            const auto s=filter.state(); check(s[0]>=20&&s[0]<=20000&&std::abs(s[1])<=1&&std::abs(s[2])<=1);
            check(!filter.process(0,0,1)&&filter.state()==s);
            check(!filter.process(1000,2,1)&&filter.state()==s);
            check(!filter.process(1000,0,std::numeric_limits<double>::quiet_NaN())&&filter.state()==s);
            check(!filter.configure({41,0})&&!filter.configure({0,-1})&&filter.state()==s);
        }
        filter.stop(); check(!filter.process(8000,0,.707)&&!filter.configure({}));
        counting=false; check(allocations==0);
        stave::FilterMotion absent; check(!absent.process(8000,0,.707)&&!absent.healthy());
        Random bad; bad.bad=true; stave::FilterMotion faulty(&bad);
        check(!faulty.process(8000,0,.707)&&!faulty.healthy());
    }
    for(unsigned bad:{0u,1u,255u,513u}) {
        bool refused=false; try { stave::StageMotion m(bad); } catch(...) { refused=true; }
        check(refused);
    }
    std::puts("PASS:6000 motion +6000 filter blocks, zero C++ new/new[], bounds/config/random-fault/terminal-stop guards");
}
