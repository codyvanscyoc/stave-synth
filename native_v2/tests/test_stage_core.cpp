#include "stave/stage_core.hpp"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{};
unsigned allocations{};
void* allocate(std::size_t n) {
    if(counting) ++allocations;
    if(void* p=std::malloc(n?n:1)) return p;
    throw std::bad_alloc();
}
void check(bool p) { if(!p) std::abort(); }
struct Phases final:stave::PhaseSource {
    bool available{true};
    bool next(stave::VoicePhases& p) noexcept override { p={.13,.54,.24,.77}; return available; }
};
void silent(const stave::StageCore& core) {
    for(unsigned c=0;c<11;++c) for(unsigned i=0;i<core.block_frames();++i) check(core.stem(c)[i]==0);
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
    counting=true; void* calibration=::operator new(1); counting=false;
    ::operator delete(calibration); check(allocations==1); allocations=0;
    for(unsigned size:{256u,512u}) {
        Phases phases; stave::StageCore core(argv[1],phases,size);
        stave::StageCoreConfig config;
        check(core.healthy() && !core.stem(11)); silent(core);
        config.fader1=std::numeric_limits<double>::quiet_NaN(); check(!core.configure(config)); config={};
        config.buses.room.wet=2; check(!core.configure(config)); config={};
        check(!core.command(1,{stave::StageAction::NoteOn,60,100}));
        check(core.command(0,{stave::StageAction::NoteOn,60,100}));
        check(core.render_block());
        counting=true;
        bool piano_heard=false;
        for(unsigned block=0;block<500;++block) {
            config.fader1=block>=20&&block<140?0:.6;
            config.fader2=block>=20&&block<140?0:.4;
            config.buses.pad.shimmer=block>=65&&block<85;
            config.buses.pad.cutoff=block%2?8000:400;
            config.hard_pan=block%4==0;
            check(core.configure(config));
            if(block==100) check(core.command(core.frame_position(),{stave::StageAction::ReleaseAll,0,0}));
            if(block==140) check(core.command(core.frame_position(),{stave::StageAction::NoteOn,64,100}));
            check(core.render_block());
            for(unsigned c=0;c<11;++c) for(unsigned i=0;i<size;++i) check(std::isfinite(core.stem(c)[i]));
            if(core.prepared_mix().skip_voices)
                for(unsigned c=0;c<9;++c) for(unsigned i=0;i<size;++i) check(core.stem(c)[i]==0);
            if(block>30&&block<60) for(unsigned i=0;i<size;++i) piano_heard|=std::abs(core.stem(9)[i])>1e-5;
        }
        check(piano_heard); core.stop(); silent(core);
        check(!core.render_block()&&!core.configure(config)&&!core.command(core.frame_position(),{stave::StageAction::NoteOn,60,100}));
        core.stop(); silent(core);
        counting=false; check(allocations==0);
        Phases unavailable; stave::StageCore failure(argv[1],unavailable,size);
        unavailable.available=false;
        check(!failure.command(0,{stave::StageAction::NoteOn,60,100}));
        check(!failure.healthy()); silent(failure);
    }
    std::puts("PASS: stage core configuration, boundaries, mute/shimmer/re-enable, piano continuity, coupled phase fault and stop");
}
