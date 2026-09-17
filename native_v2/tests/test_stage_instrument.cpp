#include "stave/stage_instrument.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void check(bool p) { if(!p) std::abort(); }
struct Phases final:stave::PhaseSource {
    bool available{true};
    bool next(stave::VoicePhases& p) noexcept override { p={.13,.54,.24,.77}; return available; }
};
void silence(const stave::StageInstrument& g) {
    for(unsigned c=0;c<10;++c) for(unsigned i=0;i<g.block_frames();++i) check(g.stem(c)[i]==0);
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }
int main(int argc,char** argv) {
    check(argc==2);
    counting=true; auto* calibration=::operator new(1); counting=false; ::operator delete(calibration);
    check(allocations==1); allocations=0;
    for(unsigned n:{256u,512u}) {
        Phases phases; stave::StageInstrument graph(argv[1],phases,n);
        stave::StageInstrumentConfig p;
        check(!graph.stem(10)&&!graph.channel(2)); silence(graph);
        check(!graph.command(1,{stave::StageAction::NoteOn,60,100}));
        for(int note:{60,64,67}) check(graph.command(0,{stave::StageAction::NoteOn,note,110}));
        check(graph.render_block()); counting=true;
        unsigned muted=0; bool heard=false;
        for(unsigned b=0;b<700;++b) {
            p.fader1=b>=20&&b<140?0:.6; p.fader2=b>=20&&b<140?0:.4;
            p.piano_reverb_send=b%2?.6:0; p.piano_delay_send=b%3?.4:0;
            p.piano_filter=b%7<3; p.wet_filter=b%11<6;
            p.buses.pad.cutoff=b%2?8000:400; p.buses.pad.slope24=b%3;
            p.delay.enabled=true; p.delay.wet=.35;
            p.master.compression=b%5; p.master.fx_bypass=b%2;
            p.master.sidechain=static_cast<stave::SidechainSource>(b%4);
            check(graph.configure(p));
            if(b%100==0) check(graph.reverb_type(static_cast<stave::ReverbType>((b/100)%7)));
            if(b%30==0) check(graph.freeze((b/30)%2));
            if(b%70==0) check(graph.retrigger_bpm());
            if(b==180) check(graph.command(graph.frame_position(),{stave::StageAction::ReleaseAll,0,0}));
            if(b==240) check(graph.command(graph.frame_position(),{stave::StageAction::NoteOn,64,100}));
            check(graph.render_block()); muted+=graph.prepared_mix().skip_voices;
            for(unsigned c=0;c<10;++c) for(unsigned i=0;i<n;++i) check(std::isfinite(graph.stem(c)[i]));
            for(unsigned i=0;i<n;++i) { check(std::abs(graph.channel(0)[i])<=.98); heard|=std::abs(graph.channel(0)[i])>1e-4; }
        }
        check(muted>0&&heard);
        const auto frame=graph.frame_position(); p.wet=std::numeric_limits<double>::quiet_NaN();
        check(!graph.configure(p)&&graph.healthy()&&graph.frame_position()==frame);
        graph.stop(); silence(graph);
        check(!graph.configure({})&&!graph.render_block()&&!graph.retrigger_bpm()&&!graph.freeze(true));
        check(!graph.command(frame,{stave::StageAction::NoteOn,60,100}));
        counting=false; check(allocations==0);
        Phases unavailable; stave::StageInstrument failed(argv[1],unavailable,n);
        unavailable.available=false; check(!failed.command(0,{stave::StageAction::NoteOn,60,100}));
        check(!failed.healthy()); silence(failed);
    }
    std::puts("PASS: 1400 owned instrument blocks, mute/re-entry, sends/filter/freeze/type/master transitions, zero C++ new/new[], configuration/phase fault and terminal silence guards");
}
