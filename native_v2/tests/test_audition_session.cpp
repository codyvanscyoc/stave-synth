#include "stave/audition_session.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <thread>
namespace {
void checked(bool x,int line) { if(!x) { std::fprintf(stderr,"Audition guard failed at line %d\n",line); std::abort(); } }
#define check(x) checked(bool(x),__LINE__)
struct Random final:stave::PhaseSource,stave::MotionRandom {
    unsigned state{917};
    double draw() noexcept { state=1664525u*state+1013904223u; return double(state)/4294967296.; }
    bool next(stave::VoicePhases& p) noexcept override { p={draw(),draw(),draw(),draw()}; return true; }
    bool next(double& x) noexcept override { x=2*draw()-1; return true; }
};
using C=stave::AuditionControl;
using F=stave::AuditionFault;
}
int main(int argc,char** argv) {
    check(argc==2);
    for(unsigned frames:{256u,512u}) {
        Random a,b; stave::StageInstrument actual(argv[1],a,frames,0,&a),expected(argv[1],b,frames,0,&b);
        stave::AuditionSession s(actual);
        stave::StageInstrumentConfig p; p.owned_motion=true; p.fader1=p.fader2=0; p.output.volume=0;
        check(expected.configure(p)); std::array<float,512> l{},r{};
        check(s.process(l.data(),r.data(),frames,nullptr,0)); check(expected.render_block());
        for(unsigned i=0;i<frames;++i) check(l[i]==0&&r[i]==0);
        check(!s.enqueue({0,C::Master,.5}));
        check(!s.enqueue({1,C::Master,std::numeric_limits<double>::quiet_NaN()}));
        check(!s.enqueue({1,static_cast<C>(999),.5}));
        check(!s.enqueue({1,C::Wave1,1.5}));
        check(s.enqueue({1,C::Master,.6})); check(!s.enqueue({1,C::Master,.7}));
        check(s.applied()==0); check(s.enqueue({2,C::Osc1,.6})); check(s.enqueue({3,C::Osc2,.4}));
        p.output.volume=.6; p.fader1=.6; p.fader2=.4; check(expected.configure(p));
        const std::array<stave::AuditionMidi,5> events{{{0,3,{0x90,60,100}},{1,3,{0x91,64,90}},
            {frames-1,3,{0x92,67,110}},{frames-1,3,{0xb0,64,127}},{frames-1,3,{0x80,60,50}}}};
        for(int note:{60,64,67}) check(expected.key_command(expected.frame_position(),stave::StageAction::NoteOn,note,note==60?100:note==64?90:110));
        check(expected.key_command(expected.frame_position(),stave::StageAction::Sustain,0,1));
        check(expected.key_command(expected.frame_position(),stave::StageAction::NoteOff,60,0));
        bool heard=false;
        for(unsigned block=0;block<180;++block) {
            if(block==50) {
                check(s.enqueue({4,C::Cutoff,2300})); p.buses.pad.cutoff=2300; check(expected.configure(p));
            }
            check(s.process(l.data(),r.data(),frames,block==0?events.data():nullptr,block==0?events.size():0));
            check(expected.render_block());
            for(unsigned i=0;i<frames;++i) {
                check(l[i]==expected.pcm(0)[i]&&r[i]==expected.pcm(1)[i]); heard|=std::abs(l[i])>1e-5;
            }
        }
        check(heard&&s.notes()==3&&s.quantized_midi()==4&&s.applied()==4);
        // Bounded queue/full rejection, bounded drain, monotonic acknowledgments.
        for(unsigned i=0;i<s.capacity;++i) check(s.enqueue({10+i,C::Master,.5}));
        check(!s.enqueue({1000,C::Master,.5}));
        check(s.process(l.data(),r.data(),frames,nullptr,0)); check(s.applied()==41);
        s.request_stop(); check(!s.process(l.data(),r.data(),frames,nullptr,0));
        check(s.fault()==F::Stop&&!s.enqueue({1001,C::Master,1}));
        for(unsigned i=0;i<frames;++i) check(l[i]==0&&r[i]==0);
        check(s.applied()==41); // queued controls never replay after STOP
        // Every exposed control's endpoints must actually configure/render.
        Random c; stave::StageInstrument g(argv[1],c,frames,0,&c); stave::AuditionSession controls(g);
        std::uint64_t id=1;
        for(unsigned control=0;control<unsigned(C::Count);++control) {
            const auto kind=static_cast<C>(control);
            for(double v:{0.,.5,1.,4.,6.,10.,20.,8000.,20000.,30000.}) if(stave::audition_value_valid(kind,v)) {
                check(controls.enqueue({id,kind,v}));
                check(controls.process(l.data(),r.data(),frames,nullptr,0)); check(controls.applied()==id++);
            }
        }
        // Concurrent single producer/audio owner, no shared graph access.
        std::atomic<bool> done{false}; const auto first=id;
        std::thread producer([&] {
            for(std::uint64_t n=first;n<first+2000;++n) {
                while(!controls.enqueue({n,C::Master,(n%10)/10.})) std::this_thread::yield();
            }
            done.store(true,std::memory_order_release);
        });
        while(!done.load(std::memory_order_acquire)||controls.applied()!=first+1999)
            check(controls.process(l.data(),r.data(),frames,nullptr,0));
        producer.join(); controls.request_stop(); check(!controls.process(l.data(),r.data(),frames,nullptr,0));
        // MIDI edge/fault paths on fresh instances.
        for(unsigned mode=0;mode<6;++mode) {
            Random d; stave::StageInstrument g2(argv[1],d,frames,0,&d); stave::AuditionSession midi(g2);
            stave::AuditionMidi e{0,3,{0x90,60,100}};
            if(mode==0) { e.size=2; check(!midi.process(l.data(),r.data(),frames,&e,1)); check(midi.fault()==F::InvalidMidi); }
            if(mode==1) { e.offset=frames; check(!midi.process(l.data(),r.data(),frames,&e,1)); check(midi.fault()==F::InvalidMidi); }
            if(mode==2) { check(!midi.process(l.data(),r.data(),frames,nullptr,257)); check(midi.fault()==F::MidiOverflow); }
            if(mode==3) { e.bytes={0xb0,120,0}; check(!midi.process(l.data(),r.data(),frames,&e,1)); check(midi.fault()==F::Stop); }
            if(mode==4) { e.bytes={0xe0,0,64}; check(midi.process(l.data(),r.data(),frames,&e,1)); check(midi.unsupported_midi()==1); }
            if(mode==5) { e.size=1; e.bytes={0xf8,0,0}; check(midi.process(l.data(),r.data(),frames,&e,1)); check(midi.unsupported_midi()==0); }
            for(unsigned i=0;i<frames;++i) check(l[i]==0&&r[i]==0);
        }
    }
    std::puts("PASS: audition whole-block PCM exact at512/256, boundary MIDI/pedals, silent startup, all control endpoints, bounded queue/ack/STOP,4000 concurrent controls, malformed/overflow MIDI guards");
}
