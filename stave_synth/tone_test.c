/* Pure C test tone — generates 440Hz sine entirely in the JACK callback.
 * Build: gcc -shared -fPIC -O2 -o tone_test.so tone_test.c -ljack -lm
 */
#include <jack/jack.h>
#include <math.h>
#include <string.h>

static jack_client_t *client = NULL;
static jack_port_t *port_l = NULL;
static jack_port_t *port_r = NULL;
static double phase = 0.0;

static int process(jack_nframes_t nframes, void *arg) {
    float *out_l = (float *)jack_port_get_buffer(port_l, nframes);
    float *out_r = (float *)jack_port_get_buffer(port_r, nframes);
    double sr = (double)jack_get_sample_rate(client);
    double inc = 2.0 * M_PI * 440.0 / sr;

    for (jack_nframes_t i = 0; i < nframes; i++) {
        float s = 0.8f * (float)sin(phase);
        out_l[i] = s;
        out_r[i] = s;
        phase += inc;
    }
    if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
    return 0;
}

int tone_start(void) {
    jack_status_t status;
    client = jack_client_open("ToneTest", JackNoStartServer, &status);
    if (!client) return -1;

    port_l = jack_port_register(client, "out_L", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    port_r = jack_port_register(client, "out_R", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (!port_l || !port_r) return -2;

    jack_set_process_callback(client, process, NULL);
    if (jack_activate(client) != 0) return -3;

    const char **playback = jack_get_ports(client, NULL, JACK_DEFAULT_AUDIO_TYPE,
                                           JackPortIsPhysical | JackPortIsInput);
    if (playback) {
        if (playback[0]) jack_connect(client, "ToneTest:out_L", playback[0]);
        if (playback[1]) jack_connect(client, "ToneTest:out_R", playback[1]);
        jack_free(playback);
    }
    return 0;
}

void tone_stop(void) {
    if (client) {
        jack_deactivate(client);
        jack_client_close(client);
        client = NULL;
    }
}
