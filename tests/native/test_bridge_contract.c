/* Fixed-graph, overflow-recovery, and quiescent-transition tests.
 * Includes the actual bridge against a deterministic fake JACK boundary. */
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "../../stave_synth/jack_bridge.c"

static jack_client_t fake_client;
static jack_port_t fake_ports[3];
static float outputs[2][MAX_BLOCK];
static jack_nframes_t fake_block_size = 4;
static jack_nframes_t fake_sample_rate = 48000;
static int (*saved_buffer_callback)(jack_nframes_t, void *);
static int (*saved_rate_callback)(jack_nframes_t, void *);
static uint32_t injected_count;
static unsigned char injected[1024][3];

jack_client_t *jack_client_open(const char *name, int flags, jack_status_t *status) {
    (void)name; (void)flags; (void)status; return &fake_client;
}
jack_port_t *jack_port_register(jack_client_t *c, const char *name, const char *type,
                                unsigned long flags, unsigned long size) {
    static int next;
    (void)c; (void)name; (void)type; (void)flags; (void)size;
    fake_ports[next % 3].id = next % 3;
    return &fake_ports[next++ % 3];
}
void *jack_port_get_buffer(jack_port_t *port, jack_nframes_t frames) {
    (void)frames;
    return port->id < 2 ? (void *)outputs[port->id] : (void *)injected;
}
jack_nframes_t jack_get_buffer_size(jack_client_t *c) { (void)c; return fake_block_size; }
jack_nframes_t jack_get_sample_rate(jack_client_t *c) { (void)c; return fake_sample_rate; }
int jack_set_process_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
int jack_set_buffer_size_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)arg; saved_buffer_callback = cb; return 0;
}
int jack_set_sample_rate_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)arg; saved_rate_callback = cb; return 0;
}
int jack_set_xrun_callback(jack_client_t *c, int (*cb)(void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
void jack_on_shutdown(jack_client_t *c, void (*cb)(void *), void *arg) {
    (void)c; (void)cb; (void)arg;
}
int jack_activate(jack_client_t *c) { (void)c; return 0; }
int jack_deactivate(jack_client_t *c) { (void)c; return 0; }
int jack_client_close(jack_client_t *c) { (void)c; return 0; }
const char **jack_get_ports(jack_client_t *c, const char *pattern, const char *type,
                            unsigned long flags) {
    (void)c; (void)pattern; (void)type; (void)flags; return NULL;
}
int jack_connect(jack_client_t *c, const char *source, const char *destination) {
    (void)c; (void)source; (void)destination; return 0;
}
const char *jack_port_name(jack_port_t *port) { (void)port; return "mock"; }
void jack_free(void *value) { (void)value; }
uint32_t jack_midi_get_event_count(void *buffer) { (void)buffer; return injected_count; }
int jack_midi_event_get(jack_midi_event_t *event, void *buffer, uint32_t index) {
    (void)buffer;
    event->time = 0;
    event->size = 3;
    event->buffer = injected[index];
    return 0;
}

static void reset_io(void) {
    memset(outputs, 0x7f, sizeof outputs);
    injected_count = 0;
    ring_read = ring_write = 0;
    midi_read = midi_write = 0;
    __atomic_store_n(&midi_recovery_required, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&graph_error_flags, 0, __ATOMIC_RELEASE);
}

static void *clear_thread(void *arg) {
    int *result = arg;
    *result = bridge_clear_ring();
    return NULL;
}

static void wait_for_transition(void) {
    struct timespec pause = {0, 1000000L};
    for (int i = 0; i < 1000; i++) {
        if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) return;
        nanosleep(&pause, NULL);
    }
    assert(!"transition did not start");
}

int main(void) {
    assert(bridge_start_named("StaveSynth_contract", 0) == 0);
    assert(saved_buffer_callback && saved_rate_callback);

    float samples[4] = {.1f, .2f, .3f, .4f};
    /* Non-finite gain/DSP values cannot poison future blocks or the DAC. */
    reset_io();
    float broken[4] = {NAN, INFINITY, -INFINITY, .25f};
    bridge_set_master_volume(NAN);
    master_volume_smooth = NAN;
    assert(bridge_write_stereo(broken, broken, 4) == 1);
    process_callback(4, NULL);
    for (int i = 0; i < 4; i++) assert(isfinite(outputs[0][i]) && isfinite(outputs[1][i]));
    bridge_set_master_volume(.85f);
    assert(bridge_write_stereo(samples, samples, 4) == 1);
    process_callback(4, NULL);
    assert(outputs[0][3] > 0.0f);
    double target[4] = {NAN, INFINITY, .5, 1.0}, envelope[4];
    double final_gain = bridge_limiter_env(target, envelope, 4, NAN, NAN);
    assert(isfinite(final_gain) && final_gain > 0.0 && final_gain <= 1.0);
    for (int i = 0; i < 4; i++) assert(isfinite(envelope[i]) && envelope[i] >= 0.0 && envelope[i] <= 1.0);
    reset_io();
    assert(bridge_write_stereo(samples, samples, 3) == -1);
    assert(bridge_get_ring_fill() == 0);
    assert(bridge_write_stereo(samples, samples, 4) == 1);
    process_callback(2, NULL);
    assert(bridge_get_graph_error() == GRAPH_ERROR_BUFFER_SIZE);
    assert(bridge_get_ring_fill() == 1); /* mismatched callback consumed nothing */
    assert(outputs[0][0] == 0.0f && outputs[0][1] == 0.0f);
    assert(bridge_write_stereo(samples, samples, 4) == -1);

    reset_io();
    saved_buffer_callback(8, NULL);
    assert(bridge_get_graph_error() == GRAPH_ERROR_BUFFER_SIZE);
    reset_io();
    saved_rate_callback(44100, NULL);
    assert(bridge_get_graph_error() == GRAPH_ERROR_SAMPLE_RATE);

    /* Overflow discards the ambiguous queued batch and synthesizes a global
       CC123. Traffic arriving after recovery is delivered normally. */
    reset_io();
    for (uint32_t i = 0; i < MIDI_RING_SIZE; i++) {
        injected[i][0] = 0x90;
        injected[i][1] = (unsigned char)(i % 128);
        injected[i][2] = 100;
    }
    injected_count = MIDI_RING_SIZE;
    process_callback(4, NULL);
    assert(bridge_get_midi_drop_count() == 1);
    uint8_t event[4] = {0};
    assert(bridge_read_midi(event) == 3);
    assert(event[0] == 0xB0 && event[1] == 123 && event[2] == 0);
    assert(bridge_get_midi_recovery_count() == 1);
    assert(bridge_read_midi(event) == 0);
    injected_count = 1;
    injected[0][0] = 0x80; injected[0][1] = 60; injected[0][2] = 0;
    process_callback(4, NULL);
    assert(bridge_read_midi(event) == 3 && event[0] == 0x80 && event[1] == 60);

    /* A stalled pre-gate writer prevents reset until it acknowledges exit. */
    reset_io();
    ring_write = 1;
    __atomic_store_n(&g_writer_active, 1, __ATOMIC_RELEASE);
    pthread_t thread;
    int result = 99;
    assert(pthread_create(&thread, NULL, clear_thread, &result) == 0);
    wait_for_transition();
    assert(ring_write == 1);
    assert(bridge_set_ring_slots(6) == -2); /* overlapping transition fails */
    __atomic_store_n(&g_writer_active, 0, __ATOMIC_RELEASE);
    assert(pthread_join(thread, NULL) == 0);
    assert(result == 0 && ring_read == 0 && ring_write == 0);

    /* Failed quiescence never resets live indices and always reopens gate. */
    ring_read = 2; ring_write = 3;
    __atomic_store_n(&g_callback_active, 1, __ATOMIC_RELEASE);
    assert(bridge_clear_ring() == -3);
    assert(ring_read == 2 && ring_write == 3);
    assert(__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE) == 0);
    __atomic_store_n(&g_callback_active, 0, __ATOMIC_RELEASE);

    bridge_stop();
    puts("native bridge graph/overflow/transition checks passed");
    return 0;
}
