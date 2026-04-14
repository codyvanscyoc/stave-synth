/*
 * jack_bridge.c — C bridge for JACK audio I/O on aarch64.
 *
 * Ring buffer design: Python renders blocks ahead into a lock-free ring buffer.
 * C drains one block per JACK callback. This absorbs Python's timing jitter.
 *
 * Build:
 *   gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread
 */

#include <jack/jack.h>
#include <jack/midiport.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>

/* ── Audio ring buffer ──
 * Fixed number of slots, each holding one JACK block.
 * Writer (Python) advances write_pos, reader (JACK callback) advances read_pos.
 * Lock-free: single producer, single consumer.
 */

#define MAX_BLOCK   2048   /* max samples per JACK block */
#define RING_SLOTS  8      /* number of blocks buffered ahead */

static float ring_l[RING_SLOTS][MAX_BLOCK];
static float ring_r[RING_SLOTS][MAX_BLOCK];
static volatile uint32_t ring_read  = 0;
static volatile uint32_t ring_write = 0;
static volatile uint32_t ring_block_size = 512;

/* ── MIDI ring buffer ── */
#define MIDI_RING_SIZE 512

typedef struct {
    uint8_t  data[4];
    uint32_t size;
} midi_event_t;

static midi_event_t midi_ring[MIDI_RING_SIZE];
static volatile uint32_t midi_read  = 0;
static volatile uint32_t midi_write = 0;

/* ── JACK state ── */
static jack_client_t *client     = NULL;
static jack_port_t   *port_out_l = NULL;
static jack_port_t   *port_out_r = NULL;
static jack_port_t   *port_midi  = NULL;

static volatile float master_volume = 0.85f;
static volatile int   btl_mode      = 1;     /* 1 = invert R for BTL adapter */

/* ── Stats ── */
static volatile uint32_t stat_callbacks   = 0;
static volatile uint32_t stat_underruns   = 0;
static volatile uint32_t stat_xruns       = 0;
static volatile uint32_t stat_midi_events = 0;
static volatile float    stat_peak        = 0.0f;

/* ── Helpers ── */

static inline uint32_t ring_readable(void) {
    uint32_t w = ring_write, r = ring_read;
    return (w >= r) ? (w - r) : (RING_SLOTS - r + w);
}

static inline uint32_t ring_writable(void) {
    return RING_SLOTS - 1 - ring_readable();
}

/* ── JACK process callback ── */

static int process_callback(jack_nframes_t nframes, void *arg) {
    (void)arg;

    float *out_l = (float *)jack_port_get_buffer(port_out_l, nframes);
    float *out_r = (float *)jack_port_get_buffer(port_out_r, nframes);

    if (ring_readable() > 0 && nframes <= MAX_BLOCK) {
        uint32_t slot = ring_read % RING_SLOTS;
        const float *src_l = ring_l[slot];
        const float *src_r = ring_r[slot];
        float vol = master_volume;
        float peak = 0.0f;

        for (jack_nframes_t i = 0; i < nframes; i++) {
            float l = src_l[i] * vol;
            float r = src_r[i] * vol;
            if (l >  1.0f) l =  1.0f;
            if (l < -1.0f) l = -1.0f;
            if (r >  1.0f) r =  1.0f;
            if (r < -1.0f) r = -1.0f;

            if (btl_mode) {
                /* BTL adapter: sum to mono, invert R for headphone correction */
                float mono = (l + r) * 0.5f;
                out_l[i] =  mono;
                out_r[i] = -mono;
            } else {
                /* Normal stereo output */
                out_l[i] = l;
                out_r[i] = r;
            }

            float a = l < 0.0f ? -l : l;
            if (a > peak) peak = a;
            a = r < 0.0f ? -r : r;
            if (a > peak) peak = a;
        }
        stat_peak = peak;

        __sync_synchronize();
        ring_read = (ring_read + 1) % RING_SLOTS;
    } else {
        /* Underrun — output silence */
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        stat_underruns++;
    }

    /* ── MIDI input ── */
    void *midi_buf = jack_port_get_buffer(port_midi, nframes);
    uint32_t nevents = jack_midi_get_event_count(midi_buf);

    for (uint32_t i = 0; i < nevents; i++) {
        jack_midi_event_t ev;
        if (jack_midi_event_get(&ev, midi_buf, i) != 0) break;
        if (ev.size > 4) continue;

        uint32_t next = (midi_write + 1) % MIDI_RING_SIZE;
        if (next == midi_read) continue;  /* full, drop */

        memcpy((void *)midi_ring[midi_write].data, ev.buffer, ev.size);
        midi_ring[midi_write].size = ev.size;
        __sync_synchronize();
        midi_write = next;
        stat_midi_events++;
    }

    stat_callbacks++;
    return 0;
}

static int xrun_callback(void *arg) {
    (void)arg;
    stat_xruns++;
    return 0;
}

/* ── Public API ── */

int bridge_start(void) {
    jack_status_t status;
    client = jack_client_open("StaveSynth", JackNoStartServer, &status);
    if (!client) return -1;

    port_out_l = jack_port_register(client, "out_L",  JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    port_out_r = jack_port_register(client, "out_R",  JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    port_midi  = jack_port_register(client, "midi_in", JACK_DEFAULT_MIDI_TYPE,  JackPortIsInput, 0);
    if (!port_out_l || !port_out_r || !port_midi) return -2;

    ring_block_size = jack_get_buffer_size(client);

    /* Zero the ring buffers */
    memset(ring_l, 0, sizeof(ring_l));
    memset(ring_r, 0, sizeof(ring_r));
    ring_read = ring_write = 0;

    jack_set_process_callback(client, process_callback, NULL);
    jack_set_xrun_callback(client, xrun_callback, NULL);

    if (jack_activate(client) != 0) return -3;

    /* Auto-connect audio */
    const char **playback = jack_get_ports(client, NULL, JACK_DEFAULT_AUDIO_TYPE,
                                           JackPortIsPhysical | JackPortIsInput);
    if (playback) {
        if (playback[0]) jack_connect(client, jack_port_name(port_out_l), playback[0]);
        if (playback[1]) jack_connect(client, jack_port_name(port_out_r), playback[1]);
        jack_free(playback);
    }

    return 0;
}

void bridge_stop(void) {
    if (client) {
        jack_deactivate(client);
        jack_client_close(client);
        client = NULL;
    }
}

/* Push one mono block into the ring buffer (duplicated to both channels).
 * Returns 1 on success, 0 if ring is full (caller should wait). */
int bridge_write_audio(const float *samples, int nframes) {
    if (ring_writable() == 0) return 0;  /* full */
    if (nframes > MAX_BLOCK) nframes = MAX_BLOCK;

    uint32_t slot = ring_write % RING_SLOTS;
    memcpy(ring_l[slot], samples, nframes * sizeof(float));
    memcpy(ring_r[slot], samples, nframes * sizeof(float));
    __sync_synchronize();
    ring_write = (ring_write + 1) % RING_SLOTS;
    return 1;
}

/* Push one stereo block (separate L/R arrays) into the ring buffer.
 * Returns 1 on success, 0 if ring is full (caller should wait). */
int bridge_write_stereo(const float *left, const float *right, int nframes) {
    if (ring_writable() == 0) return 0;  /* full */
    if (nframes > MAX_BLOCK) nframes = MAX_BLOCK;

    uint32_t slot = ring_write % RING_SLOTS;
    memcpy(ring_l[slot], left, nframes * sizeof(float));
    memcpy(ring_r[slot], right, nframes * sizeof(float));
    __sync_synchronize();
    ring_write = (ring_write + 1) % RING_SLOTS;
    return 1;
}

void bridge_set_master_volume(float vol) {
    master_volume = vol;
}

void bridge_set_btl_mode(int enabled) {
    btl_mode = enabled;
}

/* Read one MIDI event. Returns byte count (0 = empty). */
int bridge_read_midi(uint8_t *out) {
    if (midi_read == midi_write) return 0;
    uint32_t sz = midi_ring[midi_read].size;
    memcpy(out, (void *)midi_ring[midi_read].data, sz);
    __sync_synchronize();
    midi_read = (midi_read + 1) % MIDI_RING_SIZE;
    return (int)sz;
}

/* Queries */
int   bridge_get_sample_rate(void)    { return client ? (int)jack_get_sample_rate(client) : 0; }
int   bridge_get_buffer_size(void)    { return client ? (int)jack_get_buffer_size(client) : 0; }
int   bridge_get_callback_count(void) { return (int)stat_callbacks; }
float bridge_get_peak_output(void)    { return stat_peak; }
int   bridge_get_xrun_count(void)     { return (int)stat_xruns; }
int   bridge_get_underrun_count(void) { return (int)stat_underruns; }
int   bridge_get_midi_event_count(void) { return (int)stat_midi_events; }
int   bridge_get_ring_fill(void)      { return (int)ring_readable(); }
