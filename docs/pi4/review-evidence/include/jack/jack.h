#ifndef AUDIT_FAKE_JACK_H
#define AUDIT_FAKE_JACK_H
#include <stdint.h>
#include <stddef.h>
typedef uint32_t jack_nframes_t;
typedef int jack_status_t;
typedef struct {int dummy;} jack_client_t;
typedef struct {int id;} jack_port_t;
#define JackNoStartServer 1
#define JackPortIsOutput 2
#define JackPortIsInput 4
#define JackPortIsPhysical 8
#define JACK_DEFAULT_AUDIO_TYPE "audio"
#define JACK_DEFAULT_MIDI_TYPE "midi"
jack_client_t *jack_client_open(const char *, int, jack_status_t *);
jack_port_t *jack_port_register(jack_client_t *,const char *,const char *,unsigned long,unsigned long);
void *jack_port_get_buffer(jack_port_t *, jack_nframes_t);
jack_nframes_t jack_get_buffer_size(jack_client_t *);
jack_nframes_t jack_get_sample_rate(jack_client_t *);
int jack_set_process_callback(jack_client_t *,int (*)(jack_nframes_t,void *),void *);
int jack_set_xrun_callback(jack_client_t *,int (*)(void *),void *);
void jack_on_shutdown(jack_client_t *,void (*)(void *),void *);
int jack_activate(jack_client_t *);
int jack_deactivate(jack_client_t *);
int jack_client_close(jack_client_t *);
const char **jack_get_ports(jack_client_t *,const char *,const char *,unsigned long);
int jack_connect(jack_client_t *,const char *,const char *);
const char *jack_port_name(jack_port_t *);
void jack_free(void *);
#endif
