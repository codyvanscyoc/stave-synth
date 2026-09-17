// Opt-in isolated listening host. No service/config/preset writes or automatic
// sink selection. Run only with explicit maintenance authorization and ports.
#include "stave/audition_session.hpp"
#include <jack/jack.h>
#include <jack/midiport.h>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <iostream>
#include <poll.h>
#include <sstream>
#include <unistd.h>

namespace {
volatile std::sig_atomic_t interrupted=0;
void signal_handler(int) { interrupted=1; }
struct AuditionRandom final:stave::PhaseSource,stave::MotionRandom {
    // Explicit reproducible audition seed. Not a production RNG/tone decision.
    std::uint32_t state{917};
    double draw() noexcept { state=1664525u*state+1013904223u; return double(state)/4294967296.; }
    bool next(stave::VoicePhases& p) noexcept override { p={draw(),draw(),draw(),draw()}; return true; }
    bool next(double& x) noexcept override { x=2*draw()-1; return true; }
};
struct Client {
    jack_client_t* value{}; bool active{};
    ~Client() { if(value) { if(active) jack_deactivate(value); jack_client_close(value); } }
};
struct Host {
    stave::AuditionSession& session;
    unsigned frames;
    jack_port_t *left{},*right{},*midi{};
    std::atomic<bool> armed{false};
    std::atomic<std::uint64_t> xruns{0},callbacks{0},over_budget{0},max_ns{0};
    std::array<stave::AuditionMidi,stave::AuditionSession::midi_limit> events{};
    static int process(jack_nframes_t n,void* context) noexcept {
        auto& h=*static_cast<Host*>(context);
        auto* l=static_cast<float*>(jack_port_get_buffer(h.left,n));
        auto* r=static_cast<float*>(jack_port_get_buffer(h.right,n));
        if(!l||!r) { h.session.request_stop(stave::AuditionFault::GraphContract); return 0; }
        std::fill_n(l,n,0); std::fill_n(r,n,0);
        if(n!=h.frames) { h.session.request_stop(stave::AuditionFault::GraphContract); return 0; }
        if(!h.armed.load(std::memory_order_acquire)) return 0;
        const auto begin=std::chrono::steady_clock::now();
        auto* input=jack_port_get_buffer(h.midi,n);
        if(!input) { h.session.request_stop(stave::AuditionFault::InvalidMidi); return 0; }
        const unsigned count=jack_midi_get_event_count(input);
        if(count>h.events.size()) h.session.request_stop(stave::AuditionFault::MidiOverflow);
        else for(unsigned i=0;i<count;++i) {
            jack_midi_event_t e{};
            if(jack_midi_event_get(&e,input,i)||!e.buffer||!e.size) {
                h.session.request_stop(stave::AuditionFault::InvalidMidi); break;
            }
            auto& dest=h.events[i]; dest={}; dest.offset=e.time;
            // System/SysEx messages are ignored in session; don't copy or scan
            // arbitrary payloads on the audio thread. Preserve channel length.
            dest.size=static_cast<unsigned>(std::min<std::size_t>(e.size,4));
            std::copy_n(e.buffer,std::min<std::size_t>(e.size,3),dest.bytes.begin());
        }
        h.session.process(l,r,n,h.events.data(),count);
        const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count();
        const auto ns=static_cast<std::uint64_t>(elapsed);
        // One callback writer: no CAS retry loop on the audio thread.
        if(ns>h.max_ns.load(std::memory_order_relaxed)) h.max_ns.store(ns,std::memory_order_relaxed);
        if(ns>std::uint64_t(n)*1000000000/48000) h.over_budget.fetch_add(1,std::memory_order_relaxed);
        h.callbacks.fetch_add(1,std::memory_order_relaxed); return 0;
    }
    static int xrun(void* p) noexcept { static_cast<Host*>(p)->xruns.fetch_add(1,std::memory_order_relaxed); return 0; }
    static int size(jack_nframes_t n,void* p) noexcept {
        auto& h=*static_cast<Host*>(p); if(n!=h.frames) h.session.request_stop(stave::AuditionFault::GraphContract); return 0;
    }
    static int rate(jack_nframes_t n,void* p) noexcept {
        if(n!=48000) static_cast<Host*>(p)->session.request_stop(stave::AuditionFault::GraphContract);
        return 0;
    }
    static void shutdown(void* p) noexcept { static_cast<Host*>(p)->session.request_stop(stave::AuditionFault::GraphContract); }
    void status(bool routed) const {
        std::cout<<"{\"type\":\"status\",\"instance\":\"native-v2-audition\",\"frames\":"<<frames
            <<",\"routed\":"<<(routed?"true":"false")<<",\"fault\":"<<unsigned(session.fault())
            <<",\"applied\":"<<session.applied()<<",\"blocks\":"<<session.blocks()<<",\"notes\":"<<session.notes()
            <<",\"unsupported_midi\":"<<session.unsupported_midi()<<",\"quantized_midi\":"<<session.quantized_midi()
            <<",\"piano_full_scale\":"<<session.piano_full_scale()<<",\"xruns\":"<<xruns.load()
            <<",\"over_budget\":"<<over_budget.load()<<",\"max_callback_ms\":"<<max_ns.load()/1e6<<"}"<<std::endl;
    }
};
void require(bool ok,const char* message) { if(!ok) throw std::runtime_error(message); }
bool unsigned_text(const std::string& text,std::uint64_t& result) {
    if(text.empty()||text.size()>16||text.find_first_not_of("0123456789")!=std::string::npos) return false;
    result=std::stoull(text); return result<=9007199254740991ULL;
}
}
int main(int argc,char** argv) {
    try {
        require(argc==9,"Usage: audition FONT FRAMES CLIENT MIDI_SOURCE AUDIO_LEFT AUDIO_RIGHT SECONDS --allow-live-audition");
        require(std::string(argv[8])=="--allow-live-audition","Live audition opt-in required");
        std::uint64_t frame_arg{},second_arg{};
        require(unsigned_text(argv[2],frame_arg)&&unsigned_text(argv[7],second_arg)&&
                (frame_arg==512||frame_arg==256)&&second_arg>=10&&second_arg<=3600,"Invalid fixed cadence or bounded duration");
        const unsigned frames=static_cast<unsigned>(frame_arg),seconds=static_cast<unsigned>(second_arg);
        const std::string name=argv[3];
        require(name.rfind("stave-v2-audition-",0)==0&&name.size()<48,"Isolated exact client prefix required");
        require(std::string(argv[5])!=argv[6],"Distinct explicit stereo output ports required");
        AuditionRandom random; stave::StageInstrument graph(argv[1],random,frames,0,&random);
        stave::AuditionSession session(graph); Host host{session,frames}; Client client;
        jack_status_t status{};
        client.value=jack_client_open(name.c_str(),static_cast<jack_options_t>(JackNoStartServer|JackUseExactName),&status);
        require(client.value,"JACK unavailable or audition client identity already in use");
        require(name==jack_get_client_name(client.value),"JACK renamed isolated client");
        const char** stage_ports=jack_get_ports(client.value,"^StaveSynth:",nullptr,0);
        const bool stage_present=stage_ports&&stage_ports[0];
        if(stage_ports) jack_free(stage_ports);
        require(!stage_present,"Working Stave client still present; pause it in an authorized window first");
        require(jack_get_sample_rate(client.value)==48000&&jack_get_buffer_size(client.value)==frames,"Actual graph does not match requested48k/cadence; not changing it");
        auto port=[&](const char* pname,const char* type,unsigned long flags) {
            auto* p=jack_port_by_name(client.value,pname);
            require(p&&std::string(jack_port_type(p))==type&&(jack_port_flags(p)&flags),"Explicit device port missing/wrong type/direction"); return p;
        };
        port(argv[4],JACK_DEFAULT_MIDI_TYPE,JackPortIsOutput);
        port(argv[5],JACK_DEFAULT_AUDIO_TYPE,JackPortIsInput); port(argv[6],JACK_DEFAULT_AUDIO_TYPE,JackPortIsInput);
        host.left=jack_port_register(client.value,"out_l",JACK_DEFAULT_AUDIO_TYPE,JackPortIsOutput,0);
        host.right=jack_port_register(client.value,"out_r",JACK_DEFAULT_AUDIO_TYPE,JackPortIsOutput,0);
        host.midi=jack_port_register(client.value,"midi_in",JACK_DEFAULT_MIDI_TYPE,JackPortIsInput,0);
        require(host.left&&host.right&&host.midi,"JACK port registration failed");
        require(!jack_set_process_callback(client.value,Host::process,&host)&&!jack_set_xrun_callback(client.value,Host::xrun,&host)&&
                !jack_set_buffer_size_callback(client.value,Host::size,&host)&&!jack_set_sample_rate_callback(client.value,Host::rate,&host),"Callback registration failed");
        jack_on_shutdown(client.value,Host::shutdown,&host);
        require(!jack_activate(client.value),"JACK activation failed"); client.active=true;
        require(!jack_connect(client.value,argv[4],jack_port_name(host.midi))&&
                !jack_connect(client.value,jack_port_name(host.left),argv[5])&&
                !jack_connect(client.value,jack_port_name(host.right),argv[6]),"Exact route connection failed");
        host.armed.store(true,std::memory_order_release);
        std::signal(SIGINT,signal_handler); std::signal(SIGTERM,signal_handler); std::signal(SIGHUP,signal_handler);
        const auto start=std::chrono::steady_clock::now(); auto next_status=start;
        auto last_progress=start; std::uint64_t previous_blocks=0;
        std::string line; line.reserve(256);
        bool routed=true,quit=false;
        while(!interrupted&&!quit&&session.fault()==stave::AuditionFault::None&&std::chrono::steady_clock::now()-start<std::chrono::seconds(seconds)) {
            const auto now=std::chrono::steady_clock::now();
            const auto blocks=session.blocks();
            if(blocks!=previous_blocks) { previous_blocks=blocks; last_progress=now; }
            if(now-last_progress>std::chrono::seconds(3)) { session.request_stop(stave::AuditionFault::GraphContract); break; }
            routed=jack_port_connected_to(host.left,argv[5])&&jack_port_connected_to(host.right,argv[6])&&jack_port_connected_to(host.midi,argv[4]);
            if(!routed) { session.request_stop(stave::AuditionFault::GraphContract); break; }
            if(now>=next_status) { host.status(routed); next_status=now+std::chrono::seconds(1); }
            pollfd input{STDIN_FILENO,POLLIN,0}; const int ready=poll(&input,1,100);
            if(ready<0) { if(interrupted) break; throw std::runtime_error("Control poll failed"); }
            if(input.revents&POLLNVAL) throw std::runtime_error("Control descriptor unavailable");
            if(ready>0&&(input.revents&(POLLIN|POLLHUP|POLLERR))) {
                char buffer[256]; const auto n=read(STDIN_FILENO,buffer,sizeof(buffer));
                if(n<=0) break; // control-process loss terminates isolated audition
                for(ssize_t i=0;i<n;++i) {
                    if(buffer[i]!='\n') { if(line.size()>=255) throw std::runtime_error("Control line exceeds bound"); line+=buffer[i]; continue; }
                    if(line=="stop") { session.request_stop(); quit=true; line.clear(); break; }
                    std::istringstream parser(line); std::uint64_t id{}; std::string id_text,key,extra; double value{};
                    bool accepted=false;
                    if(parser>>id_text>>key>>value&&!(parser>>extra)&&unsigned_text(id_text,id)) {
                        for(unsigned c=0;c<stave::audition_names.size();++c) if(key==stave::audition_names[c])
                            accepted=session.enqueue({id,static_cast<stave::AuditionControl>(c),value});
                    }
                    std::cout<<"{\"type\":\"accepted\",\"id\":"<<id<<",\"ok\":"<<(accepted?"true":"false")<<"}"<<std::endl;
                    line.clear();
                }
            }
        }
        session.request_stop(); host.armed.store(false,std::memory_order_release);
        require(!jack_deactivate(client.value),"JACK deactivation failed"); client.active=false;
        graph.stop(); host.status(routed);
        return session.fault()==stave::AuditionFault::Stop?0:1;
    } catch(const std::exception& e) { std::cerr<<"Audition refused/stopped: "<<e.what()<<std::endl; return 1; }
}
