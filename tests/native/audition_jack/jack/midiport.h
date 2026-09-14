#ifndef TEST_AUDITION_MIDIPORT_H
#define TEST_AUDITION_MIDIPORT_H
#include <stddef.h>
#include "jack.h"
typedef unsigned char jack_midi_data_t;
void jack_midi_clear_buffer(void *);
jack_midi_data_t *jack_midi_event_reserve(void *, jack_nframes_t, size_t);
#endif
