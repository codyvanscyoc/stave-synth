#include "jack.h"
typedef struct {jack_nframes_t time; size_t size; unsigned char *buffer;} jack_midi_event_t;
uint32_t jack_midi_get_event_count(void *);
int jack_midi_event_get(jack_midi_event_t *,void *,uint32_t);
