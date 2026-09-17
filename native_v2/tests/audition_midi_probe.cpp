// Explicit live-test peer: sends bounded musical MIDI ONLY to the isolated
// audition input. Never connects to a keyboard output or the production synth.
#include <jack/jack.h>
#include <jack/midiport.h>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <iostream>
#include <thread>

namespace {
volatile std::sig_atomic_t interrupted{};
void interrupt(int) { interrupted=1; }
struct Probe {
    jack_port_t* midi{};
    std::uint64_t position{},next_change{48000};
    std::atomic<unsigned> sent{0},chords{0},errors{0};
    std::atomic<bool> complete{false},armed{false};
    bool released{};
    static int process(jack_nframes_t n,void* data) noexcept {
        auto& p=*static_cast<Probe*>(data);
        auto* buffer=jack_port_get_buffer(p.midi,n);
        if(!buffer) { p.errors.fetch_add(1); return 0; }
        jack_midi_clear_buffer(buffer);
        if(!p.armed.load(std::memory_order_acquire)) return 0;
        if(n!=512) { p.errors.fetch_add(1); return 0; }
        const auto emit=[&](unsigned char a,unsigned char b,unsigned char c) {
            const std::array<unsigned char,3> bytes{a,b,c};
            if(jack_midi_event_write(buffer,0,bytes.data(),bytes.size())) p.errors.fetch_add(1);
            else p.sent.fetch_add(1);
        };
        if(p.position>=90*48000ULL) {
            if(!p.released) { emit(0xb0,64,0); emit(0xb0,123,0); p.released=true; }
            if(p.position>=93*48000ULL) p.complete.store(true,std::memory_order_release);
        } else if(p.position>=p.next_change) {
            emit(0xb0,64,0); emit(0xb0,123,0);
            constexpr std::array<unsigned char,12> notes{48,55,60,64,67,72,76,79,84,88,91,96};
            const unsigned count=p.position>=60*48000ULL?12:6;
            const unsigned transpose=p.chords.load()%4;
            for(unsigned i=0;i<count;++i) emit(0x90,static_cast<unsigned char>(notes[i]+transpose),static_cast<unsigned char>(80+5*(i%8)));
            emit(0xb0,64,127);
            // Key-up under sustain exercises pedal ownership, not just gates.
            for(unsigned i=0;i<count;++i) emit(0x80,static_cast<unsigned char>(notes[i]+transpose),0);
            p.chords.fetch_add(1); p.next_change+=2*48000;
        }
        p.position+=n; return 0;
    }
};
}
int main(int argc,char** argv) {
    if(argc!=2||std::strcmp(argv[1],"--allow-isolated-live-midi")) {
        std::cerr<<"Explicit --allow-isolated-live-midi required\n"; return 2;
    }
    Probe probe; jack_status_t status{};
    auto* client=jack_client_open("stave-v2-midi-probe",static_cast<jack_options_t>(JackNoStartServer|JackUseExactName),&status);
    if(!client) return 1;
    bool active=false;
    const auto finish=[&](int code) { if(active) jack_deactivate(client); jack_client_close(client); return code; };
    const char* destination="stave-v2-audition-listen:midi_in";
    auto* target=jack_port_by_name(client,destination);
    if(!target||std::strcmp(jack_port_type(target),JACK_DEFAULT_MIDI_TYPE)||!(jack_port_flags(target)&JackPortIsInput)||
       jack_get_sample_rate(client)!=48000||jack_get_buffer_size(client)!=512) return finish(1);
    probe.midi=jack_port_register(client,"musical_fixture",JACK_DEFAULT_MIDI_TYPE,JackPortIsOutput,0);
    if(!probe.midi||jack_set_process_callback(client,Probe::process,&probe)||jack_activate(client)) return finish(1);
    active=true;
    if(jack_connect(client,jack_port_name(probe.midi),destination)) return finish(1);
    probe.armed.store(true,std::memory_order_release);
    std::signal(SIGTERM,interrupt); std::signal(SIGINT,interrupt);
    const auto start=std::chrono::steady_clock::now();
    while(!interrupted&&!probe.complete.load(std::memory_order_acquire)&&!probe.errors.load()&&
          std::chrono::steady_clock::now()-start<std::chrono::seconds(100)) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    jack_deactivate(client); active=false;
    const bool complete=probe.complete.load()&&!probe.errors.load();
    std::cout<<"{\"complete\":"<<(complete?"true":"false")<<",\"midi_events\":"<<probe.sent.load()
             <<",\"chords\":"<<probe.chords.load()<<",\"errors\":"<<probe.errors.load()<<"}\n";
    return finish(complete?0:1);
}
