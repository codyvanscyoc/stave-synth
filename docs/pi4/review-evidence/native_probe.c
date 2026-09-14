/* Compile ORIGINAL bridge in a mock JACK environment; no sockets/audio opened.
 * Assertions demonstrate defects in baseline, not correctness after repairs. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "../../../stave_synth/jack_bridge.c"
static jack_client_t fake_client;
static jack_port_t fake_ports[3]={{0},{1},{2}};
static float outputs[3][MAX_BLOCK];
static uint32_t injected_count;
static unsigned char injected[512][3];
jack_client_t *jack_client_open(const char *n,int f,jack_status_t *s){return &fake_client;}
jack_port_t *jack_port_register(jack_client_t *c,const char *n,const char *t,unsigned long f,unsigned long b){static int p; return &fake_ports[p++%3];}
void *jack_port_get_buffer(jack_port_t *p,jack_nframes_t n){return outputs[p->id];}
jack_nframes_t jack_get_buffer_size(jack_client_t *c){return 512;}
jack_nframes_t jack_get_sample_rate(jack_client_t *c){return 48000;}
int jack_set_process_callback(jack_client_t *c,int (*f)(jack_nframes_t,void *),void *a){return 0;}
int jack_set_xrun_callback(jack_client_t *c,int (*f)(void *),void *a){return 0;}
void jack_on_shutdown(jack_client_t *c,void (*f)(void *),void *a){}
int jack_activate(jack_client_t *c){return 0;}
int jack_deactivate(jack_client_t *c){return 0;}
int jack_client_close(jack_client_t *c){return 0;}
const char **jack_get_ports(jack_client_t *c,const char *p,const char *t,unsigned long f){return NULL;}
int jack_connect(jack_client_t *c,const char *a,const char *b){return 0;}
const char *jack_port_name(jack_port_t *p){return "mock";}
void jack_free(void *p){}
uint32_t jack_midi_get_event_count(void *p){return injected_count;}
int jack_midi_event_get(jack_midi_event_t *e,void *p,uint32_t i){e->size=3; e->time=i; e->buffer=injected[i]; return 0;}
static void reset(void){
    ring_read=ring_write=midi_read=midi_write=0;
    master_volume=master_volume_smooth=1.0f;
    injected_count=0;
    memset(ring_l,0,sizeof ring_l); memset(ring_r,0,sizeof ring_r);
}
int main(void){
    assert(bridge_start()==0);
    float samples[4]={.1f,.2f,.3f,.4f};
    reset(); ring_l[0][2]=.7f; ring_l[0][3]=.8f;
    assert(bridge_write_stereo(samples,samples,2)==1);
    process_callback(4,NULL);
    assert(outputs[0][2]==.7f && outputs[0][3]==.8f);
    puts("REPRODUCED: callback growth outputs stale ring-slot tail");
    reset(); assert(bridge_write_stereo(samples,samples,4)==1);
    process_callback(2,NULL);
    assert(bridge_get_ring_fill()==0 && outputs[0][1]==.2f);
    process_callback(2,NULL);
    assert(outputs[0][0]==0 && outputs[0][1]==0);
    puts("REPRODUCED: callback shrink discards unwritten-to-output block remainder");
    reset(); bridge_set_master_volume(NAN);
    bridge_write_stereo(samples,samples,4); process_callback(4,NULL);
    bridge_set_master_volume(1);
    bridge_write_stereo(samples,samples,4); process_callback(4,NULL);
    assert(isnan(outputs[0][0]) && isnan(master_volume_smooth));
    puts("REPRODUCED: nonfinite master target permanently poisons smoothing state");
    reset(); injected_count=512;
    for(int i=0;i<511;i++){injected[i][0]=0xB0; injected[i][1]=1; injected[i][2]=i%128;}
    injected[511][0]=0x80; injected[511][1]=60; injected[511][2]=0;
    process_callback(4,NULL);
    uint8_t out[4]; int read_count=0; int note_offs=0;
    while(bridge_read_midi(out)){read_count++; if(out[0]==0x80)note_offs++;}
    assert(read_count==511 && note_offs==0);
    puts("REPRODUCED: full MIDI ring silently drops note-off");
    bridge_stop();
    puts("4 baseline native defect scenarios reproduced with mock JACK.");
    return 0;
}
