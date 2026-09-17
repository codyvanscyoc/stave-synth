#include "stave/stage_instrument.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>
namespace {
bool counting{}; unsigned allocations{};
void* allocate(std::size_t n) { if(counting) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
void checked(bool p,int line) { if(!p) { std::fprintf(stderr,"Instrument guard failed at line %d\n",line); std::abort(); } }
#define check(p) checked(bool(p),__LINE__)
struct Phases final:stave::PhaseSource {
    bool available{true};
    bool next(stave::VoicePhases& p) noexcept override { p={.13,.54,.24,.77}; return available; }
};
struct Random final:stave::MotionRandom {
    bool available{true}; unsigned draws{};
    bool next(double& x) noexcept override { x=(++draws%17)/8.-1.; return available; }
};
void silence(const stave::StageInstrument& g) {
    for(unsigned c=0;c<10;++c) for(unsigned i=0;i<g.block_frames();++i) check(g.stem(c)[i]==0);
    for(unsigned c=0;c<2;++c) for(unsigned i=0;i<g.block_frames();++i) check(g.pcm(c)[i]==0&&g.recording_tap(c)[i]==0);
}
std::unique_ptr<stave::SampledBed> bed_bank() {
    std::array<double,48037> left{},right{};
    for(unsigned i=0;i<left.size();++i) { left[i]=.2*std::sin(i*.031); right[i]=.17*std::cos(i*.019); }
    auto bank=std::make_unique<stave::SampledBed>();
    std::unique_ptr<const stave::PreparedBed> a=std::make_unique<stave::PreparedBed>(left.data(),right.data(),left.size());
    check(bank->install(0,a));
    a=std::make_unique<stave::PreparedBed>(right.data(),left.data(),left.size());
    check(bank->install(7,a)); return bank;
}
void integrated_bed(const char* font,unsigned n) {
    Phases a,b;
    stave::StageInstrument actual(font,a,n,0,nullptr,bed_bank()),dry(font,b,n);
    stave::BedBus bed(bed_bank(),n);
    stave::StageMaster master(n); stave::StageOutput output(n);
    stave::StageInstrumentConfig p; p.output.volume=.5;
    std::array<std::array<double,512>,6> pad{};
    check(actual.bed_loaded(0)&&actual.bed_loaded(7)&&!actual.bed_loaded(1));
    check(!actual.trigger_bed(1)&&actual.healthy()&&!dry.trigger_bed(0));
    check(actual.trigger_bed(0)&&bed.trigger(0));
    counting=true;
    bool heard=false;
    for(unsigned block=0;block<300;++block) {
        p.master.compression=block>=100; p.master.fx_bypass=block>=150;
        p.bed.level=block>=30?.55:1;
        p.bed.mellow=block>=90&&block<180; p.bed.mellow_hz=650;
        check(actual.configure(p)&&dry.configure(p)&&bed.configure(p.bed)&&master.configure(p.master)&&output.configure(p.output));
        if(block==0||block==130) {
            check(actual.key_command(actual.frame_position(),stave::StageAction::NoteOn,60,100));
            check(dry.key_command(dry.frame_position(),stave::StageAction::NoteOn,60,100));
        }
        if(block==50) check(actual.fade_bed(true,.2)&&bed.fade(true,.2));
        if(block==55) check(actual.fade_bed(false,.3)&&bed.fade(false,.3));
        if(block==70) {
            check(actual.key_command(actual.frame_position(),stave::StageAction::ReleaseAll,0,0));
            check(dry.key_command(dry.frame_position(),stave::StageAction::ReleaseAll,0,0));
            check(actual.active_beds()==1); // keyboard pedals/release do not stop bed
        }
        if(block==100) check(actual.trigger_bed(7,.4,4800)&&bed.trigger(7,.4,4800));
        if(block==190) { actual.release_bed(); bed.release(); }
        check(actual.render_block()&&dry.render_block()&&bed.process());
        check(actual.active_voices()==dry.active_voices()); // bed never steals keys
        for(unsigned c=0;c<6;++c) for(unsigned i=0;i<n;++i) {
            pad[c][i]=dry.stem(c+2)[i]+(c<4?bed.channel(c%2)[i]*.85:0);
            check(pad[c][i]==actual.stem(c+2)[i]);
        }
        const std::array<const double*,6> bus{pad[0].data(),pad[1].data(),pad[2].data(),pad[3].data(),pad[4].data(),pad[5].data()};
        const std::array<const double*,2> piano{dry.stem(8),dry.stem(9)};
        check(master.process(bus,&piano)&&output.process({master.channel(0),master.channel(1)}));
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) {
            check(actual.pcm(c)[i]==output.channel(c)[i]);
            check(std::abs(actual.channel(c)[i])<=.98);
            heard|=std::abs(actual.pcm(c)[i]-dry.pcm(c)[i])>1e-5;
        }
    }
    check(heard&&bed.fade_gain()==1&&!bed.faded_target());
    auto bad=p; bad.bed.level=1.1; check(!actual.configure(bad)&&actual.healthy());
    check(!actual.fade_bed(true,0)&&!actual.fade_bed(true,std::numeric_limits<double>::quiet_NaN()));
    actual.stop(); silence(actual); check(actual.active_beds()==0&&!actual.trigger_bed(0));
    counting=false; check(allocations==0);
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
    for(unsigned n:{256u,512u}) integrated_bed(argv[1],n);
    for(unsigned n:{256u,512u}) {
        Phases phases; Random random; stave::StageInstrument graph(argv[1],phases,n,0,&random);
        stave::StageInstrumentConfig p;
        check(!graph.stem(10)&&!graph.channel(2)); silence(graph);
        p.splits={true,{0,0,0},{127,127,0},{0,0,0},{127,127,0}};
        check(graph.configure(p));
        check(!graph.key_command(1,stave::StageAction::NoteOn,60,100));
        check(!graph.key_command(0,stave::StageAction::NoteOn,-1,100));
        check(!graph.key_command(0,stave::StageAction::NoteOn,128,100));
        check(graph.key_command(0,stave::StageAction::NoteOn,60,100));
        check(graph.active_voices()==0); // excluded key consumes no oscillator slot
        check(graph.key_command(0,stave::StageAction::NoteOff,60,0));
        auto invalid=p; invalid.splits.piano.crossfade=25;
        check(!graph.configure(invalid)&&graph.healthy());
        check(graph.key_command(0,stave::StageAction::NoteOn,60,100)&&graph.active_voices()==0);
        check(graph.key_command(0,stave::StageAction::ReleaseAll,0,0));
        p.transpose=12; p.piano_octave=1; p.splits.osc1={60,60,0};
        check(graph.configure(p));
        check(graph.key_command(0,stave::StageAction::NoteOn,60,100)&&graph.active_voices()==1);
        check(graph.key_command(0,stave::StageAction::NoteOn,72,100)&&graph.active_voices()==1);
        // Musical release is not terminal STOP: the slot retires during render.
        check(graph.key_command(0,stave::StageAction::ReleaseAll,0,0)&&graph.active_voices()==1);
        p={}; check(graph.configure(p));
        check(!graph.command(1,{stave::StageAction::NoteOn,60,100}));
        for(int note:{60,64,67}) check(graph.command(0,{stave::StageAction::NoteOn,note,110}));
        check(graph.render_block()); counting=true;
        unsigned muted=0; bool heard=false;
        for(unsigned b=0;b<700;++b) {
            p.owned_motion=true;
            p.motion.bpm=b%2?40:240; p.motion.mix=b%37?1:0;
            p.filter_motion={b%3?2.:0.,b%7?.2:0.};
            for(unsigned j=0;j<2;++j) {
                auto& l=p.motion.lfo[j]; l.depth=.6; l.key_sync=true; l.poly=b%2;
                l.target=static_cast<stave::MotionTarget>((b/13+j)%4);
                l.shape=static_cast<stave::MotionShape>((b/17+j)%7);
                l.division=static_cast<stave::DelayDivision>((b/19+j)%9);
                l.multiplier=b%2?.1:10; l.received={bool(b%3),true}; l.smooth=.25;
            }
            p.fader1=b>=20&&b<140?0:.6; p.fader2=b>=20&&b<140?0:.4;
            p.piano_reverb_send=b%2?.6:0; p.piano_delay_send=b%3?.4:0;
            p.piano_filter=b%7<3; p.wet_filter=b%11<6;
            p.buses.pad.cutoff=b%2?8000:400; p.buses.pad.slope24=b%3;
            p.delay.enabled=true; p.delay.wet=.35;
            p.master.compression=b%5; p.master.fx_bypass=b%2;
            p.master.sidechain=static_cast<stave::SidechainSource>(b%4);
            p.output={(b%101)/100.,bool(b%2)};
            p.splits.enabled=b%2;
            p.splits.osc1={48,72,12}; p.splits.osc2={60,84,6};
            check(graph.configure(p));
            if(b%100==0) check(graph.reverb_type(static_cast<stave::ReverbType>((b/100)%7)));
            if(b%30==0) check(graph.freeze((b/30)%2));
            if(b%70==0) check(graph.retrigger_bpm());
            if(b==180) check(graph.key_command(graph.frame_position(),stave::StageAction::ReleaseAll,0,0));
            if(b==240) check(graph.key_command(graph.frame_position(),stave::StageAction::NoteOn,64,100));
            if(b==241) check(graph.key_command(graph.frame_position(),stave::StageAction::Sustain,0,1));
            if(b==242) check(graph.key_command(graph.frame_position(),stave::StageAction::NoteOn,64,0));
            if(b==260) check(graph.key_command(graph.frame_position(),stave::StageAction::Sustain,0,0));
            check(graph.render_block()); muted+=graph.prepared_mix().skip_voices;
            for(unsigned c=0;c<10;++c) for(unsigned i=0;i<n;++i) check(std::isfinite(graph.stem(c)[i]));
            for(unsigned i=0;i<n;++i) { check(std::abs(graph.channel(0)[i])<=.98); heard|=std::abs(graph.channel(0)[i])>1e-4; }
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<n;++i) {
                check(std::isfinite(graph.pcm(c)[i])&&std::abs(graph.pcm(c)[i])<=1);
                check(graph.recording_tap(c)[i]==float(graph.channel(c)[i]));
                if(p.output.btl) check(graph.pcm(0)[i]==-graph.pcm(1)[i]);
            }
        }
        check(muted>0&&heard);
        const auto frame=graph.frame_position(); p.wet=std::numeric_limits<double>::quiet_NaN();
        check(!graph.configure(p)&&graph.healthy()&&graph.frame_position()==frame);
        graph.stop(); silence(graph);
        check(!graph.configure({})&&!graph.render_block()&&!graph.retrigger_bpm()&&!graph.freeze(true));
        check(!graph.command(frame,{stave::StageAction::NoteOn,60,100}));
        check(!graph.key_command(frame,stave::StageAction::NoteOn,60,100));
        counting=false; check(allocations==0);
        Phases unavailable; stave::StageInstrument failed(argv[1],unavailable,n);
        unavailable.available=false; check(!failed.command(0,{stave::StageAction::NoteOn,60,100}));
        check(!failed.healthy()); silence(failed);
        Phases keys; Random r; stave::StageInstrument sync(argv[1],keys,n,0,&r);
        stave::StageInstrumentConfig m; m.owned_motion=true; m.filter_motion={0,0};
        m.motion.lfo[0].depth=.6; m.motion.lfo[0].key_sync=true;
        auto bad=m; bad.motion.lfo[1].depth=std::numeric_limits<double>::quiet_NaN();
        check(!sync.configure(bad)&&sync.healthy());
        bad=m; bad.filter_motion.wobble=1.1; check(!sync.configure(bad)&&sync.healthy());
        bad=m; bad.motion.lfo[0].division=static_cast<stave::DelayDivision>(9);
        check(!sync.configure(bad)&&sync.healthy());
        auto tempo=m; tempo.motion.lfo[0].division=stave::DelayDivision::Sixteenth;
        tempo.motion.bpm=240; tempo.motion.lfo[0].multiplier=10;
        check(tempo.motion.lfo[0].effective_rate(240)==160&&tempo.source_patch({}).poly_lfo[0].rate==20);
        tempo.motion.bpm=40; tempo.motion.lfo[0].division=stave::DelayDivision::Half; tempo.motion.lfo[0].multiplier=.1;
        check(tempo.motion.lfo[0].effective_rate(40)==1./30&&tempo.source_patch({}).poly_lfo[0].rate==.05);
        tempo.motion.haas_ms=17.137;
        check(tempo.ambience_config().pad.haas_samples==822&&tempo.motion.haas_ms==17.137);
        check(sync.configure(m)&&sync.render_block());
        const double phase=sync.motion_state(0).phase; check(phase>0);
        m.splits={true,{0,0,0},{0,0,0},{0,0,0},{0,127,0}};
        check(sync.configure(m));
        check(sync.key_command(n,stave::StageAction::NoteOn,60,100));
        check(sync.motion_state(0).phase==phase); // piano-only split must NOT reset
        m.splits.enabled=false; check(sync.configure(m));
        check(sync.key_command(n,stave::StageAction::NoteOn,64,1));
        check(sync.motion_state(0).phase==phase); // rejected low velocity
        check(sync.key_command(n,stave::StageAction::NoteOn,64,100));
        check(sync.motion_state(0).phase==0);
        check(sync.render_block());
        m.filter_motion.drift_cents=2; check(sync.configure(m)); r.available=false;
        check(!sync.render_block()&&!sync.healthy()); silence(sync);
    }
    std::puts("PASS: 600 integrated bed blocks (exact mixed/dry/FX/master routing, independent keys, fade/mellow/rise) plus1400 owned instrument blocks, motion/poly/filter walks/key-sync, raw-key splits/pedal release, mute/re-entry, sends/filter/freeze/type/master transitions, zero C++ new/new[], configuration/phase/random fault and terminal silence guards");
}
