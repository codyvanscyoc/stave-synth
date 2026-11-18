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
void piano_voice_test(const char* font) {
    using A=stave::StageAction;
    constexpr unsigned frames=512;
    Phases phases;
    stave::StageSources rhodes(font,phases,frames),suitcase(font,phases,frames),untrimmed(font,phases,frames,4);
    stave::StagePatch p; p.piano_voice=1; check(rhodes.configure(p));
    p.piano_voice=2; check(suitcase.configure(p));
    for(int voice=0;voice<4;++voice) check(rhodes.piano_voice_available(voice));
    check(!rhodes.piano_voice_available(4)); p.piano_voice=4; check(!rhodes.configure(p));
    const auto send=[](stave::StageSources& g,A action,int note,int value) {
        check(g.command(g.frame_position(),{action,note,value,{0,0,0,1}}));
    };
    for(auto* g:{&rhodes,&suitcase,&untrimmed}) {
        send(*g,A::NoteOn,60,100); send(*g,A::Sustain,0,1); send(*g,A::NoteOff,60,0);
    }
    constexpr double pi=3.14159265358979323846;
    int peak=0;
    for(unsigned b=0;b<220;++b) {
        // Selecting a new piano with the pedal held changes only future attacks.
        if(b==3) { p.piano_voice=0; check(suitcase.configure(p)); p.piano_voice=3; check(rhodes.configure(p)); }
        if(b==30) for(auto* g:{&rhodes,&suitcase,&untrimmed}) send(*g,A::Sustain,0,0);
        for(auto* g:{&rhodes,&suitcase,&untrimmed}) check(g->render_block(false));
        for(unsigned i=0;i<frames;++i) for(unsigned ch=0;ch<2;++ch) {
            const auto sample=rhodes.raw_piano()[2*i+ch];
            peak=std::max(peak,std::abs(int(sample)));
            // Raw Rhodes is the actual GM EP1 signal attenuated before quantizing.
            check(std::abs(sample-.7079457843841379*untrimmed.raw_piano()[2*i+ch])<=2);
            check(std::abs(sample-suitcase.raw_piano()[2*i+ch])<=2);
            if(b<30) {
                const double time=(b*frames+i)*5.5/48000.;
                const double gain=.75+.25*std::sin(2*pi*(time+ch*.5));
                check(std::abs(suitcase.stem(5+ch)[i]-rhodes.stem(5+ch)[i]*gain)<1e-9);
            }
            if(b==219) check(std::abs(sample)<=2);
        }
    }
    check(peak>2000);
    // Same-key cross-program restrike must retire the previous channel's note.
    // Compare against an explicitly released reference in the exact same state.
    rhodes.panic(); suitcase.panic();
    p.piano_voice=3;
    for(auto* g:{&rhodes,&suitcase}) {
        check(g->configure(p)); send(*g,A::NoteOn,64,95); check(g->render_block(false));
    }
    send(suitcase,A::NoteOff,64,0);
    p.piano_voice=1;
    for(auto* g:{&rhodes,&suitcase}) { check(g->configure(p)); send(*g,A::NoteOn,64,110); }
    for(unsigned b=0;b<80;++b) {
        if(b==20) for(auto* g:{&rhodes,&suitcase}) send(*g,A::NoteOff,64,0);
        check(rhodes.render_block(false)&&suitcase.render_block(false));
        for(unsigned i=0;i<2*frames;++i) check(std::abs(rhodes.raw_piano()[i]-suitcase.raw_piano()[i])<=2);
    }
    // Raw 60 and 48 can both sound C4 across an octave edit. The held pair
    // must equal two isolated reference stems through either release order.
    for(auto* g:{&rhodes,&suitcase,&untrimmed}) g->panic();
    p.piano_voice=3; p.piano_octave=0;
    for(auto* g:{&rhodes,&suitcase}) { check(g->configure(p)); send(*g,A::NoteOn,60,90); }
    p.piano_voice=1; p.piano_octave=1;
    for(auto* g:{&rhodes,&untrimmed}) { check(g->configure(p)); send(*g,A::NoteOn,48,84); }
    for(unsigned b=0;b<120;++b) {
        if(b==12) for(auto* g:{&rhodes,&suitcase}) send(*g,A::NoteOff,60,0);
        if(b==64) for(auto* g:{&rhodes,&untrimmed}) send(*g,A::NoteOff,48,0);
        for(auto* g:{&rhodes,&suitcase,&untrimmed}) check(g->render_block(false));
        for(unsigned i=0;i<2*frames;++i)
            check(std::abs(rhodes.raw_piano()[i]-suitcase.raw_piano()[i]-untrimmed.raw_piano()[i])<=4);
    }
}
void bend_test(const char* font) {
    Phases phases;
    stave::StageSources graph(font,phases,512);
    stave::StagePatch patch; patch.detune=0; patch.blend2=0; check(graph.configure(patch));
    check(graph.command(0,{stave::StageAction::NoteOn,69,110,{1,0,0,0}}));
    check(!graph.pitch_bend(16384));
    for(auto bend:{8192u,16383u,0u,8192u}) {
        check(graph.pitch_bend(bend));
        unsigned crossings=0;
        double previous=0;
        for(unsigned block=0;block<50;++block) {
            check(graph.render_block());
            for(unsigned i=0;i<512;++i) {
                const double value=graph.stem(0)[i];
                if(block>=5&&value>0&&previous<=0) ++crossings;
                previous=value;
            }
        }
        const double hz=crossings*48000./(45*512);
        const int semitones=bend==0?-2:bend==16383?2:0;
        check(std::abs(hz-440*std::exp2(semitones/12.))<=2.1);
    }
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
    piano_voice_test(argv[1]); bend_test(argv[1]);
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
            patch.piano_voice=cycle%4; check(graph.configure(patch));
            check(graph.pitch_bend(cycle%2?16383:8192));
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
