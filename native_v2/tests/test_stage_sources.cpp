#include "stave/stage_sources.hpp"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>

namespace {
bool count_new{};
unsigned allocations{};
void* allocate(std::size_t n) {
    if (count_new) ++allocations;
    if (auto* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void check_impl(bool value, int line) {
    if (!value) { std::fprintf(stderr,"check failed at line%d\n",line); std::abort(); }
}
#define check(value) check_impl((value), __LINE__)
struct Phases final : stave::PhaseSource {
    unsigned calls{};
    bool available{true}, corrupt{};
    bool next(stave::VoicePhases& p) noexcept override {
        ++calls;
        p = {.13,.54,.24,.77};
        if (corrupt) p.osc1 = std::numeric_limits<double>::quiet_NaN();
        return available;
    }
};
bool silent(const stave::StageSources& graph, unsigned start, unsigned end) {
    for (unsigned c = start; c < end; ++c)
        for (unsigned i = 0; i < graph.block_frames(); ++i)
            if (graph.stem(c)[i] != 0) return false;
    return true;
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main(int argc, char** argv) {
    check(argc == 2);
    Phases phases;
    for (unsigned invalid : {0u,128u,513u}) {
        bool threw = false;
        try { stave::StageSources graph(argv[1],phases,invalid); } catch (...) { threw=true; }
        check(threw);
    }
    for (unsigned size : {256u,512u}) {
        stave::StageSources graph(argv[1],phases,size);
        check(graph.healthy() && graph.frame_position()==0 && graph.stem(7)==nullptr);
        stave::StagePatch patch;
        check(graph.configure(patch));
        auto invalid = patch; invalid.blend1 = std::numeric_limits<double>::infinity();
        check(!graph.configure(invalid));
        invalid=patch; invalid.transpose=25; check(!graph.configure(invalid));
        invalid=patch; invalid.poly_lfo[0].depth=.8; check(!graph.configure(invalid));
        check(!graph.command(1,{stave::StageAction::NoteOn,60,100}));
        check(!graph.command(0,{stave::StageAction::Sustain,0,2}));
        check(!graph.command(0,{static_cast<stave::StageAction>(55),0,0}));
        check(!graph.command(0,{stave::StageAction::NoteOn,60,100,{1,1,1,-.1}}));
        check(graph.active_voices()==0 && graph.frame_position()==0);
        check(graph.stats().rejected_commands==4);
        // Zero layer weights suppress the whole corresponding source.
        check(graph.command(0,{stave::StageAction::NoteOn,60,100,{0,0,0,1}}));
        bool piano_heard=false;
        for (unsigned i=0;i<20;++i) {
            check(graph.render_block()); check(silent(graph,0,5));
            piano_heard |= !silent(graph,5,7);
        }
        check(piano_heard && graph.active_voices()==0);
        count_new=true;
        void* calibration=::operator new(1);
        count_new=false;
        ::operator delete(calibration); check(allocations==1); allocations=0;
        count_new=true;
        for (int cycle=0;cycle<20;++cycle) {
            for (int n=48;n<63;++n) check(graph.command(graph.frame_position(),{stave::StageAction::NoteOn,n,80}));
            check(graph.command(graph.frame_position(),{stave::StageAction::Sostenuto,0,1}));
            check(graph.command(graph.frame_position(),{stave::StageAction::Sustain,0,1}));
            for (int n=48;n<63;++n) check(graph.command(graph.frame_position(),{stave::StageAction::NoteOff,n,0}));
            for (unsigned b=0;b<12;++b) check(graph.render_block());
            check(graph.command(graph.frame_position(),{stave::StageAction::Sostenuto,0,0}));
            check(graph.command(graph.frame_position(),{stave::StageAction::Sustain,0,0}));
            patch.piano.comp_enabled=patch.piano.brightness_enabled=true;
            patch.piano.tremolo_hz=5; patch.piano.tremolo_depth=.4;
            check(graph.configure(patch));
            for (unsigned b=0;b<12;++b) check(graph.render_block());
            check(graph.command(graph.frame_position(),{stave::StageAction::ReleaseAll,0,0}));
        }
        graph.stop(); graph.stop();
        count_new=false;
        check(allocations==0);
        check(!graph.healthy() && graph.active_voices()==0 && silent(graph,0,7));
        check(!graph.render_block() && !graph.configure(patch));
        check(!graph.command(graph.frame_position(),{stave::StageAction::NoteOn,60,100}));
        check(graph.stats().fluid_errors==0 && graph.stats().numeric_errors==0 && graph.stats().phase_errors==0);
    }
    for (bool corrupt : {false,true}) {
        Phases bad; bad.available=corrupt; bad.corrupt=corrupt;
        stave::StageSources graph(argv[1],bad);
        check(!graph.command(0,{stave::StageAction::NoteOn,60,100}));
        check(!graph.healthy() && !graph.render_block() && silent(graph,0,7));
        check(graph.active_voices()==0 && graph.stats().phase_errors==1);
    }
    Phases isolated;
    stave::StageSources osc_only(argv[1],isolated);
    stave::StageSources idle_piano_reference(argv[1],isolated);
    check(osc_only.command(0,{stave::StageAction::NoteOn,60,100,{1,1,1,0}}));
    bool osc_heard=false;
    for (unsigned i=0;i<20;++i) {
        check(osc_only.render_block()); check(idle_piano_reference.render_block());
        // Fluid's int16 acquisition dithers even idle output. Suppression must
        // match an idle piano, not impose a new numerical-zero noise gate.
        for (unsigned c=5;c<7;++c) for (unsigned frame=0;frame<512;++frame)
            check(osc_only.stem(c)[frame]==idle_piano_reference.stem(c)[frame]);
        for (unsigned frame=0;frame<1024;++frame)
            check(osc_only.raw_piano()[frame]==idle_piano_reference.raw_piano()[frame]);
        osc_heard |= !silent(osc_only,0,4);
    }
    check(osc_heard);
    std::puts("PASS: source graph validation, cadence, layer isolation, pedals/stealing, phase failure, terminal silence; no C++ new/new[] in tested processing");
}
