// Real JACK headers, fake API: exercises lifecycle/callbacks with real graph,
// never contacts a server or opens an audio/MIDI device.
#define main audition_main
#include "../src/audition_jack.cpp"
#undef main
struct _jack_client {};
struct _jack_port { int index{}; };
namespace {
_jack_client fake_client;
std::array<_jack_port,6> fake_ports{{{0},{1},{2},{3},{4},{5}}};
std::array<std::array<float,2048>,2> output{};
std::array<jack_midi_event_t,257> packets{};
unsigned packet_count{},open_count{},close_count{},activate_count{},deactivate_count{},connect_count{},registrations{};
unsigned fail_mode{};
std::string client_name;
int (*callback)(jack_nframes_t,void*){}; void* callback_context{};
void check(bool ok) { if(!ok) std::abort(); }
}
extern "C" {
jack_client_t* jack_client_open(const char* name,jack_options_t options,jack_status_t*,...) {
    check(options==(JackNoStartServer|JackUseExactName)); ++open_count; client_name=name;
    return fail_mode==1?nullptr:&fake_client;
}
int jack_client_close(jack_client_t*) { ++close_count; return 0; }
char* jack_get_client_name(jack_client_t*) { return client_name.data(); }
const char** jack_get_ports(jack_client_t*,const char* pattern,const char*,unsigned long) {
    check(std::string(pattern)=="^(StaveSynth:|stave-v2-(audition|stage)-.*:)");
    static const char* found[]={"StaveSynth:out_l",nullptr}; return fail_mode==10?found:nullptr;
}
void jack_free(void*) {}
jack_nframes_t jack_get_sample_rate(jack_client_t*) { return fail_mode==2?44100:48000; }
jack_nframes_t jack_get_buffer_size(jack_client_t*) { return 512; }
jack_port_t* jack_port_by_name(jack_client_t*,const char* name) { return fail_mode==3?nullptr:&fake_ports[std::string(name)=="keyboard:midi"?3:std::string(name)=="out:left"?4:5]; }
const char* jack_port_type(const jack_port_t* p) { return p->index==3?JACK_DEFAULT_MIDI_TYPE:JACK_DEFAULT_AUDIO_TYPE; }
int jack_port_flags(const jack_port_t* p) { return p->index==3?JackPortIsOutput:JackPortIsInput; }
jack_port_t* jack_port_register(jack_client_t*,const char*,const char*,unsigned long,unsigned long) { return fail_mode==4?nullptr:&fake_ports[(registrations++)%3]; }
const char* jack_port_name(const jack_port_t*) { return "fake:port"; }
int jack_port_connected_to(const jack_port_t*,const char*) { return fail_mode==8?0:1; }
void* jack_port_get_buffer(jack_port_t* p,jack_nframes_t) { return p->index<2?static_cast<void*>(output[p->index].data()):static_cast<void*>(packets.data()); }
int jack_set_process_callback(jack_client_t*,JackProcessCallback f,void* p) { callback=f; callback_context=p; return fail_mode==5?1:0; }
int jack_set_xrun_callback(jack_client_t*,JackXRunCallback,void*) { return 0; }
int jack_set_buffer_size_callback(jack_client_t*,JackBufferSizeCallback,void*) { return 0; }
int jack_set_sample_rate_callback(jack_client_t*,JackSampleRateCallback,void*) { return 0; }
void jack_on_shutdown(jack_client_t*,JackShutdownCallback,void*) {}
int jack_activate(jack_client_t*) { ++activate_count; if(fail_mode==6) return 1; if(callback) callback(512,callback_context); return 0; }
int jack_deactivate(jack_client_t*) { ++deactivate_count; return 0; }
int jack_connect(jack_client_t*,const char*,const char*) { ++connect_count; return fail_mode==7?1:0; }
uint32_t jack_midi_get_event_count(void*) { return packet_count; }
int jack_midi_event_get(jack_midi_event_t* e,void*,uint32_t index) { *e=packets[index]; return 0; }
}
int main(int argc,char** argv) {
    check(argc==2);
    for(unsigned frames:{256u,512u}) for(unsigned mode=0;mode<5;++mode) {
        AuditionRandom random; stave::StageInstrument graph(argv[1],random,frames,0,&random);
        stave::AuditionSession session(graph); Host h{session,frames};
        h.left=&fake_ports[0]; h.right=&fake_ports[1]; h.midi=&fake_ports[2];
        packet_count=0;
        for(auto& c:output) c.fill(.5f);
        check(Host::process(frames,&h)==0&&session.blocks()==0); // inactive startup
        for(const auto& c:output) for(unsigned i=0;i<frames;++i) check(c[i]==0);
        h.armed.store(true);
        unsigned char note[3]={0x90,60,100};
        packets[0]={frames-1,3,note}; packet_count=1;
        if(mode==0) { check(session.enqueue({1,stave::AuditionControl::Master,.3})); }
        if(mode==1) Host::rate(44100,&h);
        if(mode==2) Host::size(frames==512?256:512,&h);
        if(mode==3) packet_count=257;
        if(mode==4) packets[0].buffer=nullptr;
        check(Host::process(frames,&h)==0);
        if(mode==0) {
            check(session.blocks()==1&&session.notes()==1&&session.quantized_midi()==1);
            Host::xrun(&h); check(h.xruns==1);
            Host::shutdown(&h); check(session.fault()==stave::AuditionFault::GraphContract);
            check(Host::process(frames,&h)==0);
        } else check(session.blocks()==0&&session.fault()!=stave::AuditionFault::None);
        for(const auto& c:output) for(unsigned i=0;i<frames;++i) check(c[i]==0);
    }
    std::uint64_t n{};
    for(const char* value:{"-1","+1","512x","18446744073709551616","9007199254740992"}) check(!unsigned_text(value,n));
    check(unsigned_text("512",n)&&n==512);
    // CLI refusal/cleanup before activation. No implicit device fallback.
    std::array<std::string,9> args{"audition",argv[1],"512","stave-v2-audition-fake","keyboard:midi","out:left","out:right","10","--allow-live-audition"};
    std::array<char*,9> pointers{}; for(unsigned i=0;i<9;++i) pointers[i]=args[i].data();
    const int saved_stdin=dup(STDIN_FILENO); int pipe_fds[2]{};
    check(saved_stdin>=0&&pipe(pipe_fds)==0&&dup2(pipe_fds[0],STDIN_FILENO)>=0);
    close(pipe_fds[0]); close(pipe_fds[1]); // true control-pipe EOF
    for(fail_mode=1;fail_mode<=10;++fail_mode) {
        open_count=close_count=activate_count=deactivate_count=connect_count=registrations=0;
        check(audition_main(9,pointers.data())==(fail_mode==9?0:1));
        check(open_count==1&&close_count==(fail_mode==1?0u:1u));
        check(activate_count==(fail_mode>=6&&fail_mode<=9?1u:0u));
        check(deactivate_count==(fail_mode>=7&&fail_mode<=9?1u:0u));
        check(connect_count==(fail_mode>=8&&fail_mode<=9?3u:fail_mode==7?1u:0u));
    }
    // Real private-bank CLI loading/refusal happens before any JACK open.
    char bank_path[]="/tmp/stave-native-bank-host-XXXXXX";
    const int bank_fd=mkstemp(bank_path); check(bank_fd>=0);
    std::array<unsigned char,96> bank_bytes{};
    std::memcpy(bank_bytes.data(),"STVBANK1",8);
    bank_bytes[8]=0x80; bank_bytes[9]=0xbb; bank_bytes[12]=1; //48k, one entry
    bank_bytes[16]=7; bank_bytes[24]=4; //G, four zero stereo frames
    check(write(bank_fd,bank_bytes.data(),bank_bytes.size())==ssize_t(bank_bytes.size())); close(bank_fd);
    std::array<std::string,11> bank_args{};
    for(unsigned i=0;i<9;++i) bank_args[i]=args[i];
    bank_args[9]="--bed-bank"; bank_args[10]=bank_path;
    std::array<char*,11> bank_pointers{};
    for(unsigned i=0;i<11;++i) bank_pointers[i]=bank_args[i].data();
    fail_mode=9; open_count=close_count=activate_count=deactivate_count=connect_count=registrations=0;
    check(audition_main(11,bank_pointers.data())==0&&open_count==1&&close_count==1);
    check(unlink(bank_path)==0); open_count=0;
    check(audition_main(11,bank_pointers.data())==1&&open_count==0);
    args[3]="stave-v2-stage-fake"; args[7]="0"; args[8]="--allow-stage-candidate";
    for(unsigned i=0;i<9;++i) pointers[i]=args[i].data();
    open_count=close_count=activate_count=deactivate_count=connect_count=registrations=0;
    check(audition_main(9,pointers.data())==0&&open_count==1&&close_count==1);
    args[2]="256"; pointers[2]=args[2].data(); open_count=0;
    check(audition_main(9,pointers.data())==1&&open_count==0); //candidate stays512
    check(dup2(saved_stdin,STDIN_FILENO)>=0); close(saved_stdin);
    std::puts("PASS: fake JACK real graph callback512/256, silent unarmed/fault output, MIDI bound, graph changes, shutdown/xruns, signed CLI refusal,10 startup/route/disconnect/EOF/production-client cleanup paths; no devices opened");
}
