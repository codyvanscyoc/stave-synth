// Device-free throughput probe. No driver, MIDI input, threads or live state.
#include "stave/stage_instrument.hpp"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <vector>
#include <sys/resource.h>
namespace {
struct FixtureRandom final:stave::PhaseSource,stave::MotionRandom {
    unsigned state{917};
    double uniform() noexcept { state=1664525u*state+1013904223u; return double(state)/4294967296.; }
    bool next(stave::VoicePhases& p) noexcept override { p={uniform(),uniform(),uniform(),uniform()}; return true; }
    bool next(double& x) noexcept override { x=2*uniform()-1; return true; }
};
double thread_ms() {
    timespec t{};
    if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t)) throw std::runtime_error("Thread clock unavailable");
    return t.tv_sec*1000.+t.tv_nsec/1e6;
}
void require(bool ok) { if(!ok) throw std::runtime_error("Instrument operation rejected"); }
double percentile(const std::vector<double>& v,double q) { return v[std::min(v.size()-1,std::size_t(std::ceil(q*v.size())-1))]; }
void metrics(const char* name,std::vector<double> v,double budget) {
    std::sort(v.begin(),v.end()); double sum=0; unsigned late=0;
    for(double x:v) { sum+=x; late+=x>budget; }
    std::printf("\"%s\":{\"mean_ms\":%.9f,\"p50_ms\":%.9f,\"p95_ms\":%.9f,\"p99_ms\":%.9f,\"max_ms\":%.9f,\"over_block_budget\":%u}",
        name,sum/v.size(),percentile(v,.5),percentile(v,.95),percentile(v,.99),v.back(),late);
}
}
int main(int argc,char** argv) {
    try {
        if(argc!=5) throw std::runtime_error("Usage: benchmark FONT FRAMES SCENARIO BLOCKS");
        const unsigned frames=std::stoul(argv[2]),scenario=std::stoul(argv[3]),blocks=std::stoul(argv[4]);
        if((frames!=256&&frames!=512)||scenario>3||blocks<100||blocks>2000) throw std::runtime_error("Invalid probe bounds");
        FixtureRandom random;
        stave::StageInstrument g(argv[1],random,frames,0,&random);
        stave::StageInstrumentConfig p;
        p.owned_motion=true; p.filter_motion={2,.1};
        p.piano.volume=.5; p.output.volume=.7;
        p.fader1=scenario==0?0:.6; p.fader2=scenario==0?0:.4;
        p.wave1=2; p.wave2=1;
        if(scenario>=2) {
            p.buses.pad.shimmer=true; p.buses.pad.shimmer_mix=.5;
            p.delay.enabled=true; p.delay.wet=.3; p.delay.feedback=.45;
            p.piano_reverb_send=.2; p.piano_delay_send=.1;
            p.wet_filter=true; p.master.compression=true;
            p.motion.lfo[0].depth=.4; p.motion.lfo[0].target=stave::MotionTarget::Filter;
            p.motion.lfo[1].depth=.3; p.motion.lfo[1].target=stave::MotionTarget::Pan;
        }
        require(g.configure(p));
        require(g.reverb_type(stave::ReverbType::Hall));
        const unsigned notes=scenario>=2?12:6;
        const stave::LayerWeights weights=scenario==0?stave::LayerWeights{0,0,0,1}:stave::LayerWeights{};
        for(unsigned i=0;i<notes;++i) require(g.command(g.frame_position(),{stave::StageAction::NoteOn,int(48+i*2),100,weights}));
        require(g.command(g.frame_position(),{stave::StageAction::Sustain,0,1}));
        for(unsigned i=0;i<100;++i) require(g.render_block());
        std::vector<double> wall(blocks),cpu(blocks);
        double peak=0,energy=0; unsigned voice_peak=0;
        for(unsigned b=0;b<blocks;++b) {
            const double tc=thread_ms(); const auto start=std::chrono::steady_clock::now();
            if(scenario==3) {
                p.buses.pad.cutoff=300.+7700.*(.5+.5*std::sin(b*.05));
                p.pan1=.4*std::sin(b*.03); p.pan2=-p.pan1;
                p.motion.lfo[1].target=static_cast<stave::MotionTarget>((b/50)%4);
                require(g.configure(p));
                if(b%50==0) {
                    require(g.command(g.frame_position(),{stave::StageAction::Sustain,0,0}));
                    require(g.command(g.frame_position(),{stave::StageAction::ReleaseAll,0,0}));
                    for(unsigned i=0;i<notes;++i) require(g.command(g.frame_position(),{stave::StageAction::NoteOn,int(48+i*2+(b/50)%5),100}));
                    require(g.command(g.frame_position(),{stave::StageAction::Sustain,0,1}));
                    require(g.reverb_type(static_cast<stave::ReverbType>((b/50)%7)));
                }
            }
            require(g.render_block());
            wall[b]=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
            cpu[b]=thread_ms()-tc;
            voice_peak=std::max(voice_peak,g.active_voices());
            for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) {
                const double x=g.pcm(c)[i]; require(std::isfinite(x)&&std::abs(x)<=1);
                peak=std::max(peak,std::abs(x)); energy+=x*x;
            }
        }
        const auto stats=g.stats(); require(stats.fluid_errors==0&&stats.phase_errors==0&&stats.numeric_errors==0&&stats.rejected_commands==0);
        require(peak>1e-5);
        g.stop(); require(!g.render_block());
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames;++i) require(g.pcm(c)[i]==0);
        rusage usage{}; require(getrusage(RUSAGE_SELF,&usage)==0);
        std::printf("{\"scenario\":%u,\"block_frames\":%u,\"blocks\":%u,\"warmup_blocks\":100,\"audio_seconds\":%.6f,\"block_budget_ms\":%.9f,",scenario,frames,blocks,blocks*frames/48000.,frames/48.);
        metrics("wall",wall,frames/48.); std::printf(","); metrics("thread_cpu",cpu,frames/48.);
        std::printf(",\"peak\":%.9f,\"rms\":%.9f,\"peak_oscillator_voices\":%u,\"raw_piano_full_scale_samples\":%llu,\"maxrss_platform_units\":%ld,\"terminal_silence\":true}\n",peak,std::sqrt(energy/(blocks*frames*2)),voice_peak,static_cast<unsigned long long>(stats.full_scale_piano_samples),usage.ru_maxrss);
        return 0;
    } catch(const std::exception& e) { std::fprintf(stderr,"Offline benchmark failed: %s\n",e.what()); return 1; }
}
