#define main probe_main
#include "audition_midi_probe.cpp"
#undef main
#include <algorithm>
#include <cstdlib>
namespace {
unsigned ons{},offs{},ccs{},writes{}; bool fail_write{};
std::array<unsigned char,3> last{};
void check(bool x) { if(!x) std::abort(); }
}
extern "C" {
jack_client_t* jack_client_open(const char*,jack_options_t,jack_status_t*,...) { return nullptr; }
int jack_client_close(jack_client_t*) { return 0; }
int jack_deactivate(jack_client_t*) { return 0; }
jack_port_t* jack_port_by_name(jack_client_t*,const char*) { return nullptr; }
const char* jack_port_type(const jack_port_t*) { return JACK_DEFAULT_MIDI_TYPE; }
int jack_port_flags(const jack_port_t*) { return JackPortIsInput; }
jack_nframes_t jack_get_sample_rate(jack_client_t*) { return 48000; }
jack_nframes_t jack_get_buffer_size(jack_client_t*) { return 512; }
jack_port_t* jack_port_register(jack_client_t*,const char*,const char*,unsigned long,unsigned long) { return nullptr; }
int jack_set_process_callback(jack_client_t*,JackProcessCallback,void*) { return 0; }
int jack_activate(jack_client_t*) { return 0; }
int jack_connect(jack_client_t*,const char*,const char*) { return 0; }
const char* jack_port_name(const jack_port_t*) { return "fake:test"; }
void* jack_port_get_buffer(jack_port_t*,jack_nframes_t) { return &writes; }
void jack_midi_clear_buffer(void*) { writes=0; }
int jack_midi_event_write(void*,jack_nframes_t time,const jack_midi_data_t* bytes,size_t size) {
    check(time==0&&size==3&&bytes[1]<128&&bytes[2]<128);
    ++writes; check(writes<=27); std::copy_n(bytes,3,last.begin());
    ons+=bytes[0]==0x90; offs+=bytes[0]==0x80; ccs+=bytes[0]==0xb0;
    return fail_write?1:0;
}
}
int main() {
    Probe p;
    check(!p.armed); Probe::process(512,&p); check(!p.position&&!p.sent);
    p.armed=true;
    for(unsigned i=0;i<8720;++i) Probe::process(512,&p);
    check(p.complete&&p.chords==45&&p.sent==857&&!p.errors);
    check(ons==360&&offs==360&&ccs==137&&last[0]==0xb0&&last[1]==123&&last[2]==0);
    Probe invalid; invalid.armed=true; Probe::process(256,&invalid); check(invalid.errors==1&&!invalid.sent);
    Probe failed; failed.armed=true; failed.position=48000; fail_write=true; Probe::process(512,&failed);
    check(failed.errors==15&&!failed.sent);
    std::cout<<"PASS: offline live-MIDI fixture45 chords/360 notes/857 packets, bounded27 writes, terminal pedal/note release, invalid cadence/write failures; no devices\n";
}
