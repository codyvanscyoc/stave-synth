#ifndef TEST_AUDITION_JACK_H
#define TEST_AUDITION_JACK_H
#include <stdint.h>
typedef uint32_t jack_nframes_t;
typedef uint32_t jack_port_id_t;
typedef uint32_t jack_status_t;
typedef struct jack_client jack_client_t;
typedef struct jack_port jack_port_t;
#define JackNoStartServer 1u
#define JackUseExactName 2u
#define JackNameNotUnique 4u
#define JackPortIsInput 1u
#define JackPortIsOutput 2u
#define JACK_DEFAULT_AUDIO_TYPE "32 bit float mono audio"
#define JACK_DEFAULT_MIDI_TYPE "8 bit raw midi"
jack_client_t *jack_client_open(const char *, unsigned, jack_status_t *);
int jack_client_close(jack_client_t *);
const char *jack_get_client_name(jack_client_t *);
jack_nframes_t jack_get_sample_rate(jack_client_t *);
jack_nframes_t jack_get_buffer_size(jack_client_t *);
jack_port_t *jack_port_register(jack_client_t *, const char *, const char *, unsigned long, unsigned long);
jack_port_t *jack_port_by_name(jack_client_t *, const char *);
const char *jack_port_name(const jack_port_t *);
const char *jack_port_type(const jack_port_t *);
unsigned long jack_port_flags(const jack_port_t *);
int jack_port_connected(const jack_port_t *);
int jack_port_connected_to(const jack_port_t *, const char *);
const char **jack_port_get_all_connections(const jack_client_t *, const jack_port_t *);
const char **jack_get_ports(jack_client_t *, const char *, const char *, unsigned long);
void jack_free(void *);
void *jack_port_get_buffer(jack_port_t *, jack_nframes_t);
int jack_set_process_callback(jack_client_t *, int (*)(jack_nframes_t, void *), void *);
int jack_set_xrun_callback(jack_client_t *, int (*)(void *), void *);
int jack_set_buffer_size_callback(jack_client_t *, int (*)(jack_nframes_t, void *), void *);
int jack_set_sample_rate_callback(jack_client_t *, int (*)(jack_nframes_t, void *), void *);
int jack_set_port_connect_callback(jack_client_t *, void (*)(jack_port_id_t, jack_port_id_t, int, void *), void *);
int jack_activate(jack_client_t *);
int jack_deactivate(jack_client_t *);
int jack_connect(jack_client_t *, const char *, const char *);
#endif
