/* Exact-name/isolation lifecycle test using the real bridge and fake JACK. */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../../stave_synth/jack_bridge.c"

static jack_client_t fake_client;
static jack_port_t fake_ports[3];
static float output[2][MAX_BLOCK];
static int open_fail;
static int register_fail_at;
static int activate_fail;
static int register_calls;
static int activate_calls;
static int deactivate_calls;
static int close_calls;
static int get_ports_calls;
static int connect_calls;
static int open_flags;
static char open_name[128];

static void reset_mock(void) {
    open_fail = register_fail_at = activate_fail = 0;
    register_calls = activate_calls = deactivate_calls = close_calls = 0;
    get_ports_calls = connect_calls = open_flags = 0;
    open_name[0] = '\0';
    client = NULL;
    port_out_l = port_out_r = port_midi = NULL;
}

jack_client_t *jack_client_open(const char *name, int flags, jack_status_t *status) {
    (void)status;
    open_flags = flags;
    snprintf(open_name, sizeof open_name, "%s", name);
    return open_fail ? NULL : &fake_client;
}
jack_port_t *jack_port_register(jack_client_t *c, const char *name, const char *type,
                                unsigned long flags, unsigned long size) {
    (void)c; (void)name; (void)type; (void)flags; (void)size;
    register_calls++;
    if (register_fail_at == register_calls) return NULL;
    fake_ports[register_calls - 1].id = register_calls - 1;
    return &fake_ports[register_calls - 1];
}
void *jack_port_get_buffer(jack_port_t *port, jack_nframes_t frames) {
    (void)frames;
    return output[port->id < 2 ? port->id : 0];
}
jack_nframes_t jack_get_buffer_size(jack_client_t *c) { (void)c; return 512; }
jack_nframes_t jack_get_sample_rate(jack_client_t *c) { (void)c; return 48000; }
int jack_set_process_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
int jack_set_buffer_size_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
int jack_set_sample_rate_callback(jack_client_t *c, int (*cb)(jack_nframes_t, void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
int jack_set_xrun_callback(jack_client_t *c, int (*cb)(void *), void *arg) {
    (void)c; (void)cb; (void)arg; return 0;
}
void jack_on_shutdown(jack_client_t *c, void (*cb)(void *), void *arg) {
    (void)c; (void)cb; (void)arg;
}
int jack_activate(jack_client_t *c) { (void)c; activate_calls++; return activate_fail; }
int jack_deactivate(jack_client_t *c) { (void)c; deactivate_calls++; return 0; }
int jack_client_close(jack_client_t *c) { (void)c; close_calls++; return 0; }
const char **jack_get_ports(jack_client_t *c, const char *pattern, const char *type,
                            unsigned long flags) {
    (void)c; (void)pattern; (void)type; (void)flags; get_ports_calls++; return NULL;
}
int jack_connect(jack_client_t *c, const char *source, const char *destination) {
    (void)c; (void)source; (void)destination; connect_calls++; return 0;
}
const char *jack_port_name(jack_port_t *port) { (void)port; return "mock"; }
void jack_free(void *value) { (void)value; }
uint32_t jack_midi_get_event_count(void *buffer) { (void)buffer; return 0; }
int jack_midi_event_get(jack_midi_event_t *event, void *buffer, uint32_t index) {
    (void)event; (void)buffer; (void)index; return -1;
}

int main(void) {
    reset_mock();
    assert(bridge_start_named("StaveSynth_integration_a", 0) == 0);
    assert(strcmp(open_name, "StaveSynth_integration_a") == 0);
    assert((open_flags & JackUseExactName) != 0);
    assert((open_flags & JackNoStartServer) != 0);
    assert(activate_calls == 1);
    assert(get_ports_calls == 0 && connect_calls == 0);
    bridge_stop();
    assert(deactivate_calls == 1 && close_calls == 1 && client == NULL);

    reset_mock();
    open_fail = 1; /* exact-name collision is represented by JACK refusing open */
    assert(bridge_start_named("StaveSynth_integration_a", 0) == -1);
    assert(activate_calls == 0 && close_calls == 0 && client == NULL);

    reset_mock();
    register_fail_at = 2;
    assert(bridge_start_named("StaveSynth_partial", 0) == -2);
    assert(activate_calls == 0 && close_calls == 1 && client == NULL);
    assert(port_out_l == NULL && port_out_r == NULL && port_midi == NULL);
    /* Retry in the same lifecycle: the failure cleanup, not reset_mock(),
       must make the bridge reusable and remove dangling port pointers. */
    register_fail_at = 0;
    register_calls = 0;
    assert(bridge_start_named("StaveSynth_partial_retry", 0) == 0);
    bridge_stop();
    assert(client == NULL);
    assert(port_out_l == NULL && port_out_r == NULL && port_midi == NULL);

    reset_mock();
    activate_fail = 1;
    assert(bridge_start_named("StaveSynth_activate", 0) == -3);
    assert(activate_calls == 1 && close_calls == 1 && client == NULL);
    assert(port_out_l == NULL && port_out_r == NULL && port_midi == NULL);
    activate_fail = 0;
    register_calls = 0;
    assert(bridge_start_named("StaveSynth_activate_retry", 0) == 0);
    bridge_stop();
    assert(client == NULL);
    assert(port_out_l == NULL && port_out_r == NULL && port_midi == NULL);

    puts("native bridge identity/lifecycle checks passed");
    return 0;
}
