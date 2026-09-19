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
std::unique_ptr<stave::SampledBed> test_bank() {
    auto bank=std::make_unique<stave::SampledBed>();
    std::array<double,97> l{},r{};
    for(unsigned i=0;i<97;++i) { l[i]=(int(i)-48)/512.; r[i]=-l[i]; }
    for(unsigned slot:{0u,7u}) {
        std::unique_ptr<const stave::PreparedBed> sample=std::make_unique<stave::PreparedBed>(l.data(),r.data(),l.size());
        check(bank->install(slot,sample));
    }
    return bank;
}
}
int main(int argc,char** argv) {
    check(argc==2);
    for(unsigned frames:{256u,512u}) {
        {
            // New UI controls must be the same existing graph operations, not
            // new DSP. No bed bank: unrelated controls must never require one.
            Random ra,rb;
            stave::StageInstrument a(argv[1],ra,frames,0,&ra),b(argv[1],rb,frames,0,&rb);
            stave::AuditionSession owner(a);
            stave::StageInstrumentConfig p; p.owned_motion=true; p.fader1=p.fader2=0; p.output.volume=0;
            p.piano.highcut_smoothing_ms=80; check(b.configure(p));
            std::array<float,512> left{},right{};
            std::uint64_t id=0;
            auto send=[&](C c,double v){check(owner.enqueue({++id,c,v}));};
            send(C::Master,.6); p.output.volume=.6;
            send(C::Osc1,.5); p.fader1=.5; send(C::Osc2,.4); p.fader2=.4;
            for(unsigned block=0;block<180;++block) {
                if(block==5) { send(C::Attack1,120); p.env1.attack_ms=120; }
                if(block==10) { send(C::Attack2,770); p.env2.attack_ms=770; }
                if(block==15) { send(C::Decay1,900); p.env1.decay_ms=900; }
                if(block==20) { send(C::Sustain2,35); p.env2.sustain_percent=35; }
                if(block==25) { send(C::Release1,1234); p.env1.release_ms=1234; }
                if(block==30) { send(C::EnvelopeLink,1); } // enabling must not copy unequal envelopes
                if(block==35) { send(C::Decay2,2300); p.env1.decay_ms=p.env2.decay_ms=2300; }
                if(block==40) { send(C::Attack2,420); p.env1.attack_ms=p.env2.attack_ms=420; }
                if(block==45) { send(C::Sustain1,63); p.env1.sustain_percent=p.env2.sustain_percent=63; }
                if(block==50) { send(C::Release2,890); p.env1.release_ms=p.env2.release_ms=890; }
                if(block==55) { send(C::EnvelopeLink,0); send(C::Decay2,200); p.env2.decay_ms=200; }
                if(block==60) { send(C::Attack,90); p.env1.attack_ms=p.env2.attack_ms=90; }
                if(block==65) { send(C::Release,1500); p.env1.release_ms=p.env2.release_ms=1500; }
                if(block==70) { send(C::MasterLow,-2); p.master.eq[0].gain=-2; }
                if(block==75) { send(C::MasterMid,1.4); p.master.eq[1].gain=1.4; }
                if(block==80) { send(C::MasterHigh,-3.1); p.master.eq[2].gain=-3.1; }
                if(block==85) { send(C::MasterLowcutHz,43); p.master.cutoff=43; }
                if(block==90) { send(C::MasterLowcut,1); p.master.highpass=true; }
                if(block==91) { send(C::PianoLowcut,48); p.piano.lowcut_hz=48; }
                if(block==92) { send(C::PianoVelocity,1.7); p.piano_velocity_curve=1.7; }
                if(block==93) { send(C::Osc1Reverb,.74); p.buses.pad.send1=.74; }
                if(block==94) { send(C::Osc2Reverb,.39); p.buses.pad.send2=.39; }
                if(block==115) { send(C::PianoRoomSize,.72); p.buses.room.size=.72; }
                if(block==120) { send(C::PianoRoomDamp,.81); p.buses.room.damp=.81; }
                if(block==125) { send(C::DelayTime,417); p.delay.division=stave::DelayDivision::Free; p.delay.milliseconds=417; }
                if(block==130) { send(C::DelayLowcut,95); p.delay.lowcut=95; }
                if(block==135) { send(C::DelayHighcut,9200); p.delay.highcut=9200; }
                if(block==140) { send(C::ReverbDecay,7.4); check(b.reverb_control(stave::ReverbControl::Decay,7.4)); }
                if(block==145) { send(C::ReverbPredelay,33); check(b.reverb_control(stave::ReverbControl::Predelay,33)); }
                if(block==150) { send(C::ReverbLowcut,120); check(b.reverb_control(stave::ReverbControl::LowCut,120)); }
                if(block==155) { send(C::ReverbHighcut,6500); check(b.reverb_control(stave::ReverbControl::HighCut,6500)); }
                if(block==160) { send(C::ReverbDamp,.64); check(b.reverb_control(stave::ReverbControl::Damp,.64)); }
                if(block==165) { send(C::PianoDelay,.31); p.piano_delay_send=.31; }
                if(block==166) { send(C::FilterSlope,1); p.buses.pad.slope24=true; }
                if(block==167) { send(C::PianoFilter,1); p.piano_filter=true; }
                if(block==168) { send(C::Bpm,93); p.motion.bpm=p.modulation.bpm=93; }
                if(block==169) { send(C::DelayDivision,5); p.delay.division=stave::DelayDivision::DottedEighth; }
                check(b.configure(p));
                stave::AuditionMidi e{0,3,{0x90,60,100}}; unsigned count=0;
                if(block==0||block==95) { count=1; check(b.key_command(b.frame_position(),stave::StageAction::NoteOn,60,100)); }
                if(block==100) { e.bytes={0xb0,64,127}; count=1; check(b.key_command(b.frame_position(),stave::StageAction::Sustain,0,1)); }
                if(block==105) { e.bytes={0x80,60,0}; count=1; check(b.key_command(b.frame_position(),stave::StageAction::NoteOff,60,0)); }
                if(block==110) { e.bytes={0xb0,64,0}; count=1; check(b.key_command(b.frame_position(),stave::StageAction::Sustain,0,0)); }
                check(owner.process(left.data(),right.data(),frames,count?&e:nullptr,count));
                check(b.render_block());
                for(unsigned i=0;i<frames;++i) check(left[i]==b.pcm(0)[i]&&right[i]==b.pcm(1)[i]);
            }
            check(owner.applied()==id);
            check(!owner.enqueue({id+1,C::Sustain1,101}));
            check(!owner.enqueue({id+1,C::MasterLow,6.1}));
            check(!owner.enqueue({id+1,C::EnvelopeLink,.5}));
            check(!owner.enqueue({id+1,C::DelayTime,0}));
            check(!owner.enqueue({id+1,C::DelayHighcut,499}));
            check(!owner.enqueue({id+1,C::ReverbPredelay,151}));
            check(!owner.enqueue({id+1,C::PianoLowcut,501}));
            check(!owner.enqueue({id+1,C::PianoVelocity,.99}));
            check(!owner.enqueue({id+1,C::Bpm,39}));
            check(!owner.enqueue({id+1,C::DelayDivision,2.5}));
            check(!owner.enqueue({id+1,C::RecordStart,1}));
        }
        {
            Random random;
            stave::StageInstrument graph(argv[1],random,frames,0,&random,test_bank());
            stave::AuditionSession session(graph);
            std::array<float,512> l{},r{};
            check(session.bed_mask()==129&&session.bed_key()==-1&&session.active_beds()==0);
            check(!session.enqueue({1,C::BedKey,1})); // missing key does not enter queue or stop graph
            check(!session.enqueue({1,C::BedKey,12}));
            check(!session.enqueue({1,C::BedKey,.5}));
            check(session.enqueue({1,C::BedKey,0}));
            check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(session.bed_key()==0&&session.active_beds()==1&&session.applied()==1);
            check(session.enqueue({2,C::BedRise,2}));
            check(session.enqueue({3,C::BedRiseCutoff,4800}));
            check(session.enqueue({4,C::BedKey,7}));
            check(session.enqueue({5,C::ReleaseAll,1}));
            check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(session.bed_key()==7&&session.active_beds()>=1&&session.applied()==5);
            // Recorded key remains independent of keyboard note/pedal release.
            check(session.enqueue({6,C::BedRelease,1}));
            check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(session.bed_key()==-1&&session.fault()==F::None);
            session.request_stop(); check(!session.process(l.data(),r.data(),frames,nullptr,0));
            check(session.active_beds()==0);
        }
        {
            // Pad capture never recursively records an already-playing pad.
            Random random;
            stave::StageInstrument graph(argv[1],random,frames,0,&random,test_bank());
            stave::RecordingCapture<> capture(frames);
            stave::AuditionSession session(graph,&capture);
            std::array<float,512> l{},r{}; stave::RecordingCapture<>::Block block;
            check(session.enqueue({1,C::BedKey,0}));
            bool existing_bed_heard=false;
            for(unsigned n=0;n<4;++n) {
                check(session.process(l.data(),r.data(),frames,nullptr,0)); check(capture.pop(block));
                for(unsigned i=0;i<frames;++i) {
                    check(block.channel[0][i]==graph.pad_recording_tap(0)[i]);
                    check(block.channel[1][i]==graph.pad_recording_tap(1)[i]);
                    existing_bed_heard|=std::abs(graph.recording_tap(0)[i]-graph.pad_recording_tap(0)[i])>1e-6;
                }
            }
            check(existing_bed_heard);
        }
        {
            // Actual graph tap stays pre-master: muted physical PCM does not
            // mute a take. Disk overflow ends only capture, not the synth.
            Random random;
            stave::StageInstrument graph(argv[1],random,frames,0,&random);
            auto capture=std::make_unique<stave::RecordingCapture<>>(frames);
            stave::AuditionSession session(graph,capture.get());
            stave::RecordingCapture<>::Block block;
            std::array<float,512> l{},r{};
            stave::AuditionMidi note{0,3,{0x90,60,100}};
            bool captured_sound=false;
            for(unsigned n=0;n<64;++n) {
                check(session.process(l.data(),r.data(),frames,n?nullptr:&note,n?0:1));
                check(capture->pop(block));
                for(unsigned i=0;i<frames;++i) {
                    check(l[i]==0&&r[i]==0);
                    check(block.channel[0][i]==graph.pad_recording_tap(0)[i]&&block.channel[1][i]==graph.pad_recording_tap(1)[i]);
                    captured_sound|=std::abs(block.channel[0][i])>1e-5;
                }
            }
            check(captured_sound);
            for(unsigned n=0;n<130;++n) check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(capture->end()==stave::CaptureEnd::Overflow&&session.fault()==F::None&&graph.healthy());
            session.request_stop(); check(!session.process(l.data(),r.data(),frames,nullptr,0));
            check(capture->end()==stave::CaptureEnd::Overflow);
            unsigned count=0; while(capture->pop(block)) ++count;
            check(count==128&&capture->drained());
        }
        {
            // One-shot recording transport is armed and stopped only by
            // acknowledged audio-boundary commands; invalid order is refused.
            Random random; stave::StageInstrument graph(argv[1],random,frames,0,&random);
            stave::RecordingCapture<> capture(frames,frames*4,false);
            stave::AuditionSession session(graph,&capture); std::array<float,512> l{},r{};
            check(!session.enqueue({1,C::RecordStop,1}));
            check(session.enqueue({1,C::RecordStart,1})); check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(capture.end()==stave::CaptureEnd::Open&&capture.frames_written()==frames);
            check(!session.enqueue({2,C::RecordStart,1}));
            check(session.enqueue({2,C::RecordStop,1})); check(session.process(l.data(),r.data(),frames,nullptr,0));
            check(capture.end()==stave::CaptureEnd::Complete&&capture.frames_written()==frames&&session.applied()==2);
        }
        Random a,b; stave::StageInstrument actual(argv[1],a,frames,0,&a),expected(argv[1],b,frames,0,&b);
        stave::AuditionSession s(actual);
        stave::StageInstrumentConfig p; p.owned_motion=true; p.fader1=p.fader2=0; p.output.volume=0;
        p.piano.highcut_smoothing_ms=80;
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
            if(block==80) {
                check(s.enqueue({5,C::PianoTone,.5})); p.piano.highcut_hz=2000; check(expected.configure(p));
            }
            if(block==120) {
                check(s.enqueue({6,C::PianoTone,1})); p.piano.highcut_hz=20000; check(expected.configure(p));
            }
            check(s.process(l.data(),r.data(),frames,block==0?events.data():nullptr,block==0?events.size():0));
            check(expected.render_block());
            for(unsigned i=0;i<frames;++i) {
                check(l[i]==expected.pcm(0)[i]&&r[i]==expected.pcm(1)[i]); heard|=std::abs(l[i])>1e-5;
            }
        }
        check(heard&&s.notes()==3&&s.quantized_midi()==4&&s.applied()==6);
        // Bounded queue/full rejection, bounded drain, monotonic acknowledgments.
        for(unsigned i=0;i<s.capacity;++i) check(s.enqueue({10+i,C::Master,.5}));
        check(!s.enqueue({1000,C::Master,.5}));
        check(s.process(l.data(),r.data(),frames,nullptr,0)); check(s.applied()==41);
        s.request_stop(); check(!s.process(l.data(),r.data(),frames,nullptr,0));
        check(s.fault()==F::Stop&&!s.enqueue({1001,C::Master,1}));
        for(unsigned i=0;i<frames;++i) check(l[i]==0&&r[i]==0);
        check(s.applied()==41); // queued controls never replay after STOP
        // Every exposed control's endpoints must actually configure/render.
        Random c; stave::StageInstrument g(argv[1],c,frames,0,&c,test_bank()); stave::AuditionSession controls(g);
        std::uint64_t id=1;
        for(unsigned control=0;control<unsigned(C::Count);++control) {
            const auto kind=static_cast<C>(control);
            for(double v:{0.,.5,1.,4.,6.,7.,10.,20.,60.,100.,200.,8000.,20000.,30000.}) if(stave::audition_value_valid(kind,v)) {
                if(kind==C::RecordStart||kind==C::RecordStop) { check(!controls.enqueue({id,kind,v})); continue; }
                if((kind==C::ReverbLowcut&&v>=7000)||(kind==C::ReverbHighcut&&v<=80)) continue;
                if((kind==C::DelayLowcut&&v>=18000)||(kind==C::DelayHighcut&&v<=1000)) continue;
                if(kind==C::BedKey&&v!=0&&v!=7) { check(!controls.enqueue({id,kind,v})); continue; }
                check(controls.enqueue({id,kind,v}));
                const bool rendered=controls.process(l.data(),r.data(),frames,nullptr,0);
                if(!rendered) std::fprintf(stderr,"Control endpoint failed: %s=%g\n",stave::audition_names[control],v);
                check(rendered); check(controls.applied()==id++);
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
        // Learned MIDI stays inside the audio owner: one CC is coalesced per
        // control/block, safety pedals remain reserved, and telemetry is
        // versioned so a later browser edit is not overwritten by stale data.
        {
            Random d; stave::StageInstrument g2(argv[1],d,frames,0,&d); stave::AuditionSession learned(g2);
            check(!learned.map_cc(64,C::Master)&&!learned.map_cc(120,C::Master));
            check(!learned.map_cc(21,C::ReleaseAll)&&learned.map_cc(21,C::Master));
            stave::AuditionMidi e{0,3,{0xb0,21,127}};
            check(learned.process(l.data(),r.data(),frames,&e,1));
            check(learned.midi_cc()==21&&learned.midi_cc_value()==127&&learned.midi_cc_serial()==1);
            check(learned.midi_apply_control()==int(C::Master)&&learned.midi_apply_value()==127&&learned.midi_apply_serial()==1);
            check(learned.midi_mapped_raw(unsigned(C::Master))==127&&learned.midi_mapped_serial(unsigned(C::Master))==1);
            check(learned.unsupported_midi()==0);
            check(learned.map_cc(21,C::Cutoff));
            check(learned.midi_mapped_raw(unsigned(C::Master))==-1&&learned.midi_mapped_serial(unsigned(C::Master))==0);
            e.bytes={0xb0,21,0}; check(learned.process(l.data(),r.data(),frames,&e,1));
            check(learned.midi_mapped_raw(unsigned(C::Cutoff))==0&&learned.midi_mapped_serial(unsigned(C::Cutoff))==2);
            check(stave::audition_midi_value(C::Cutoff,0)==20&&stave::audition_midi_value(C::Cutoff,127)==20000);
        }
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
