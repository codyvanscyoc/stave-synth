/* Isolated JACK MIDI driver + stereo capture for StaveSynth_audition only.
 * Build on the Pi: cc -std=c11 -O2 -Wall -Wextra -Werror -o native_audition
 *                  tools/native_audition.c -ljack -lm
 */
#define _POSIX_C_SOURCE 200809L
#include <jack/jack.h>
#include <jack/midiport.h>

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define TARGET_CLIENT "StaveSynth_audition"
#define CAPTURE_CLIENT "StaveCapture_audition"
#define MIDI_CLIENT "StaveMidi_audition"
#define MAX_EVENTS 65536u
#define MAX_SECONDS 120.0
#define MAX_AUDIO_BYTES (48u * 1024u * 1024u)

typedef struct {
    double seconds;
    uint64_t frame;
    uint8_t bytes[3];
} MidiEvent;

static jack_client_t *g_capture_client, *g_midi_client;
static jack_port_t *g_midi_out, *g_capture_l, *g_capture_r;
static MidiEvent *g_events;
static size_t g_event_count, g_next_event;
static float *g_audio_l, *g_audio_r;
static uint64_t g_frame_limit;
static atomic_uint_fast64_t g_frames, g_midi_frames, g_midi_sent;
static atomic_uint_fast64_t g_midi_first_ns, g_capture_first_ns;
static jack_nframes_t g_block_size;
static double g_peak;
static uint64_t g_nonfinite;
static atomic_int g_armed, g_callback_errors, g_midi_failures, g_xruns;
static volatile sig_atomic_t g_interrupted;

static void on_signal(int signum) { (void)signum; g_interrupted = 1; }

static uint64_t monotonic_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec;
}

static int midi_process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    void *midi = jack_port_get_buffer(g_midi_out, nframes);
    jack_midi_clear_buffer(midi);
    if (!atomic_load_explicit(&g_armed, memory_order_acquire)) return 0;
    if (nframes != g_block_size) {
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
        return 0;
    }

    uint64_t start = atomic_load_explicit(&g_midi_frames, memory_order_relaxed);
    if (start >= g_frame_limit) return 0;
    if (start == 0) {
        uint64_t stamp = monotonic_ns();
        atomic_store_explicit(&g_midi_first_ns, stamp, memory_order_release);
        if (!stamp) atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
    }
    uint64_t stop = start + nframes;
    while (g_next_event < g_event_count && g_events[g_next_event].frame < stop) {
        MidiEvent *event = &g_events[g_next_event];
        if (event->frame >= start) {
            jack_midi_data_t *dst = jack_midi_event_reserve(
                midi, (jack_nframes_t)(event->frame - start), 3);
            if (dst == NULL) {
                atomic_fetch_add_explicit(&g_midi_failures, 1, memory_order_relaxed);
            } else {
                memcpy(dst, event->bytes, 3);
                atomic_fetch_add_explicit(&g_midi_sent, 1, memory_order_relaxed);
            }
        }
        g_next_event++;
    }
    atomic_store_explicit(&g_midi_frames, start + nframes, memory_order_release);
    return 0;
}

static int capture_process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    if (!atomic_load_explicit(&g_armed, memory_order_acquire)) return 0;
    if (nframes != g_block_size) {
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
        return 0;
    }
    uint64_t start = atomic_load_explicit(&g_frames, memory_order_relaxed);
    if (start >= g_frame_limit) return 0;
    if (start == 0) {
        uint64_t stamp = monotonic_ns();
        atomic_store_explicit(&g_capture_first_ns, stamp, memory_order_release);
        if (!stamp) atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
    }

    float *left = (float *)jack_port_get_buffer(g_capture_l, nframes);
    float *right = (float *)jack_port_get_buffer(g_capture_r, nframes);
    if (left == NULL || right == NULL) {
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
        return 0;
    }
    uint64_t take = nframes;
    if (start + take > g_frame_limit) take = g_frame_limit - start;
    for (uint64_t i = 0; i < take; i++) {
        float l = left[i], r = right[i];
        g_audio_l[start + i] = l;
        g_audio_r[start + i] = r;
        if (!isfinite(l)) g_nonfinite++;
        else if (fabs((double)l) > g_peak) g_peak = fabs((double)l);
        if (!isfinite(r)) g_nonfinite++;
        else if (fabs((double)r) > g_peak) g_peak = fabs((double)r);
    }
    atomic_store_explicit(&g_frames, start + take, memory_order_release);
    return 0;
}

static int on_xrun(void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed))
        atomic_fetch_add_explicit(&g_xruns, 1, memory_order_relaxed);
    return 0;
}

static int on_buffer_size(jack_nframes_t nframes, void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed)
            && nframes != g_block_size)
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
    return 0;
}

static int on_sample_rate(jack_nframes_t rate, void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed) && rate != 48000)
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
    return 0;
}

static void on_port_connect(jack_port_id_t a, jack_port_id_t b,
                            int connected, void *arg) {
    (void)a; (void)b; (void)connected; (void)arg;
    /* A dedicated audition graph must stay unchanged for the whole take.
     * Latching all topology edits also catches a transient extra source that
     * disappears before final endpoint verification. */
    if (atomic_load_explicit(&g_armed, memory_order_relaxed))
        atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
}

static int parse_double(const char *text, double *value) {
    char *end = NULL;
    errno = 0;
    double parsed = strtod(text, &end);
    if (errno || end == text || *end != '\0' || !isfinite(parsed)) return -1;
    *value = parsed;
    return 0;
}

static int load_schedule(const char *path, double duration, char *error, size_t cap) {
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) { snprintf(error, cap, "schedule_open"); return -1; }
    struct stat schedule_stat;
    if (fstat(fd, &schedule_stat) || !S_ISREG(schedule_stat.st_mode)) {
        close(fd); snprintf(error, cap, "schedule_not_regular"); return -1;
    }
    FILE *file = fdopen(fd, "r");
    if (!file) { close(fd); snprintf(error, cap, "schedule_open"); return -1; }
    g_events = calloc(MAX_EVENTS, sizeof(*g_events));
    if (!g_events) { fclose(file); snprintf(error, cap, "schedule_alloc"); return -1; }
    char line[512];
    unsigned long line_no = 0;
    double previous = -1.0;
    while (fgets(line, sizeof(line), file)) {
        line_no++;
        if (!strchr(line, '\n') && !feof(file)) {
            snprintf(error, cap, "schedule_line_too_long_%lu", line_no);
            fclose(file); return -1;
        }
        char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '\0' || *p == '\n' || *p == '#') continue;
        double when;
        unsigned status, data1, data2;
        char extra;
        if (sscanf(p, "%lf %x %u %u %c", &when, &status, &data1, &data2, &extra) != 4
                || !isfinite(when) || when < 0.0 || when > duration
                || when < previous || status > 255 || data1 > 127 || data2 > 127
                || !((status & 0xf0u) == 0x80u || (status & 0xf0u) == 0x90u
                     || (status & 0xf0u) == 0xb0u || (status & 0xf0u) == 0xe0u)) {
            snprintf(error, cap, "schedule_invalid_line_%lu", line_no);
            fclose(file); return -1;
        }
        if (g_event_count == MAX_EVENTS) {
            snprintf(error, cap, "schedule_too_many_events"); fclose(file); return -1;
        }
        MidiEvent *event = &g_events[g_event_count++];
        event->seconds = when;
        event->bytes[0] = (uint8_t)status;
        event->bytes[1] = (uint8_t)data1;
        event->bytes[2] = (uint8_t)data2;
        previous = when;
    }
    if (ferror(file)) { snprintf(error, cap, "schedule_read"); fclose(file); return -1; }
    fclose(file);
    if (g_event_count < 2) { snprintf(error, cap, "schedule_missing_release"); return -1; }
    MidiEvent *pedal = &g_events[g_event_count - 2];
    MidiEvent *panic = &g_events[g_event_count - 1];
    if ((pedal->bytes[0] & 0xf0u) != 0xb0u || pedal->bytes[1] != 64u
            || pedal->bytes[2] >= 64u || (panic->bytes[0] & 0xf0u) != 0xb0u
            || panic->bytes[1] != 123u || panic->bytes[2] != 0u
            || panic->seconds < pedal->seconds || panic->seconds > duration - 0.5) {
        snprintf(error, cap, "schedule_requires_final_cc64up_cc123_and_tail"); return -1;
    }
    return 0;
}

static int exact_port(jack_port_t *port, const char *name, const char *type,
                      unsigned long required, char *error, size_t cap) {
    if (!port || strcmp(jack_port_name(port), name) != 0
            || strcmp(jack_port_type(port), type) != 0
            || (jack_port_flags(port) & (JackPortIsInput | JackPortIsOutput)) != required) {
        snprintf(error, cap, "port_contract"); return -1;
    }
    return 0;
}

static int connection_count(jack_client_t *client, jack_port_t *port, const char *only) {
    const char **connections = jack_port_get_all_connections(client, port);
    int count = 0, matched = 0;
    if (connections) {
        for (; connections[count]; count++) if (!strcmp(connections[count], only)) matched++;
        jack_free(connections);
    }
    return count == 1 && matched == 1 ? 1 : 0;
}

static int has_no_connections(jack_client_t *client, jack_port_t *port) {
    const char **connections = jack_port_get_all_connections(client, port);
    if (connections == NULL) return 1;
    int empty = connections[0] == NULL;
    jack_free(connections);
    return empty;
}

static void put_u16(FILE *f, uint16_t v) {
    fputc(v & 255, f); fputc((v >> 8) & 255, f);
}
static void put_u32(FILE *f, uint32_t v) {
    put_u16(f, v & 65535u); put_u16(f, v >> 16);
}

static int write_wav(const char *path, uint32_t rate, uint64_t frames) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return -1;
    FILE *file = fdopen(fd, "wb");
    if (!file) { close(fd); return -1; }
    uint32_t bytes = (uint32_t)(frames * 2u * sizeof(float));
    fwrite("RIFF", 1, 4, file); put_u32(file, 50u + bytes); fwrite("WAVE", 1, 4, file);
    fwrite("fmt ", 1, 4, file); put_u32(file, 18); put_u16(file, 3); put_u16(file, 2);
    put_u32(file, rate); put_u32(file, rate * 8u); put_u16(file, 8); put_u16(file, 32);
    put_u16(file, 0);  /* WAVEFORMATEX cbSize */
    fwrite("fact", 1, 4, file); put_u32(file, 4); put_u32(file, (uint32_t)frames);
    fwrite("data", 1, 4, file); put_u32(file, bytes);
    float chunk[8192];
    for (uint64_t pos = 0; pos < frames; ) {
        size_t n = (size_t)((frames - pos) > 4096 ? 4096 : (frames - pos));
        for (size_t i = 0; i < n; i++) {
            chunk[2 * i] = g_audio_l[pos + i]; chunk[2 * i + 1] = g_audio_r[pos + i];
        }
        if (fwrite(chunk, sizeof(float) * 2u, n, file) != n) { fclose(file); return -1; }
        pos += n;
    }
    int failed = ferror(file) != 0;
    if (fflush(file)) failed = 1;
    if (fsync(fd)) failed = 1;
    if (fclose(file)) failed = 1;
    return failed ? -1 : 0;
}

static double monotonic_seconds(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    int validate = argc == 4 && !strcmp(argv[1], "--validate");
    if ((!validate && argc != 4) || (validate && argc != 4)) {
        fprintf(stderr, "usage: %s SCHEDULE WAV SECONDS\n       %s --validate SCHEDULE SECONDS\n", argv[0], argv[0]);
        return 2;
    }
    const char *schedule_path = validate ? argv[2] : argv[1];
    const char *wav_path = validate ? NULL : argv[2];
    const char *duration_text = argv[3];
    double duration;
    char error[128] = "none";
    if (parse_double(duration_text, &duration) || duration < 1.0 || duration > MAX_SECONDS) {
        fprintf(stderr, "invalid duration (1..120 seconds)\n"); return 2;
    }
    if (load_schedule(schedule_path, duration, error, sizeof(error))) {
        fprintf(stderr, "%s\n", error); free(g_events); return 2;
    }
    if (validate) {
        printf("{\"event\":\"schedule_valid\",\"events\":%zu,\"seconds\":%.6f}\n",
               g_event_count, duration);
        free(g_events); return 0;
    }
    if (!wav_path[0]) { fprintf(stderr, "empty output path\n"); free(g_events); return 2; }
    /* Resolve/fault in the clock path before the armed callback's first use. */
    if (!monotonic_ns()) { snprintf(error, sizeof(error), "monotonic_clock_unavailable"); goto fail; }
    struct stat output_stat;
    if (lstat(wav_path, &output_stat) == 0 || errno != ENOENT) {
        fprintf(stderr, "output_exists_or_uncheckable\n"); free(g_events); return 2;
    }
    const uint16_t endian_probe = 1;
    if (*(const uint8_t *)&endian_probe != 1) {
        fprintf(stderr, "unsupported_big_endian\n"); free(g_events); return 2;
    }

    jack_status_t status = 0;
    g_capture_client = jack_client_open(CAPTURE_CLIENT, JackNoStartServer | JackUseExactName, &status);
    if (!g_capture_client || (status & JackNameNotUnique)) { snprintf(error, sizeof(error), "capture_client_open"); goto fail; }
    if (strcmp(jack_get_client_name(g_capture_client), CAPTURE_CLIENT)) { snprintf(error, sizeof(error), "capture_client_identity"); goto fail; }
    status = 0;
    g_midi_client = jack_client_open(MIDI_CLIENT, JackNoStartServer | JackUseExactName, &status);
    if (!g_midi_client || (status & JackNameNotUnique)) { snprintf(error, sizeof(error), "midi_client_open"); goto fail; }
    if (strcmp(jack_get_client_name(g_midi_client), MIDI_CLIENT)) { snprintf(error, sizeof(error), "midi_client_identity"); goto fail; }
    jack_nframes_t rate = jack_get_sample_rate(g_capture_client);
    g_block_size = jack_get_buffer_size(g_capture_client);
    if (rate != 48000 || g_block_size == 0) { snprintf(error, sizeof(error), "unsupported_graph"); goto fail; }
    if (jack_get_sample_rate(g_midi_client) != rate
            || jack_get_buffer_size(g_midi_client) != g_block_size) {
        snprintf(error, sizeof(error), "client_graph_mismatch"); goto fail;
    }
    g_frame_limit = (uint64_t)llround(duration * rate);
    size_t audio_bytes = (size_t)g_frame_limit * 2u * sizeof(float);
    if (audio_bytes > MAX_AUDIO_BYTES || g_frame_limit > SIZE_MAX / sizeof(float)) {
        snprintf(error, sizeof(error), "capture_too_large"); goto fail;
    }
    g_audio_l = calloc((size_t)g_frame_limit, sizeof(float));
    g_audio_r = calloc((size_t)g_frame_limit, sizeof(float));
    if (!g_audio_l || !g_audio_r) { snprintf(error, sizeof(error), "capture_alloc"); goto fail; }
    /* Fault in every audio page before either process callback is activated. */
    volatile float *prefault_l = g_audio_l;
    volatile float *prefault_r = g_audio_r;
    for (uint64_t i = 0; i < g_frame_limit; i += 1024u) {
        prefault_l[i] = 0.0f; prefault_r[i] = 0.0f;
    }
    prefault_l[g_frame_limit - 1] = prefault_r[g_frame_limit - 1] = 0.0f;
    for (size_t i = 0; i < g_event_count; i++)
        g_events[i].frame = (uint64_t)llround(g_events[i].seconds * rate);

    g_midi_out = jack_port_register(g_midi_client, "midi_out", JACK_DEFAULT_MIDI_TYPE, JackPortIsOutput, 0);
    g_capture_l = jack_port_register(g_capture_client, "capture_L", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    g_capture_r = jack_port_register(g_capture_client, "capture_R", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    jack_port_t *target_midi = jack_port_by_name(g_capture_client, TARGET_CLIENT ":midi_in");
    jack_port_t *target_l = jack_port_by_name(g_capture_client, TARGET_CLIENT ":out_L");
    jack_port_t *target_r = jack_port_by_name(g_capture_client, TARGET_CLIENT ":out_R");
    if (exact_port(g_midi_out, MIDI_CLIENT ":midi_out", JACK_DEFAULT_MIDI_TYPE, JackPortIsOutput, error, sizeof(error))
            || exact_port(g_capture_l, CAPTURE_CLIENT ":capture_L", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, error, sizeof(error))
            || exact_port(g_capture_r, CAPTURE_CLIENT ":capture_R", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, error, sizeof(error))
            || exact_port(target_midi, TARGET_CLIENT ":midi_in", JACK_DEFAULT_MIDI_TYPE, JackPortIsInput, error, sizeof(error))
            || exact_port(target_l, TARGET_CLIENT ":out_L", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, error, sizeof(error))
            || exact_port(target_r, TARGET_CLIENT ":out_R", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, error, sizeof(error))) goto fail;
    if (!has_no_connections(g_capture_client, target_midi)
            || !has_no_connections(g_capture_client, target_l)
            || !has_no_connections(g_capture_client, target_r)) {
        snprintf(error, sizeof(error), "target_has_existing_connections"); goto fail;
    }
    const char **target_ports = jack_get_ports(g_capture_client, "^" TARGET_CLIENT ":", NULL, 0);
    int target_port_count = 0;
    if (target_ports) { while (target_ports[target_port_count]) target_port_count++; jack_free(target_ports); }
    if (target_port_count != 3) { snprintf(error, sizeof(error), "unexpected_target_ports"); goto fail; }

    if (jack_set_process_callback(g_capture_client, capture_process, NULL)
            || jack_set_xrun_callback(g_capture_client, on_xrun, NULL)
            || jack_set_buffer_size_callback(g_capture_client, on_buffer_size, NULL)
            || jack_set_sample_rate_callback(g_capture_client, on_sample_rate, NULL)
            || jack_set_port_connect_callback(g_capture_client, on_port_connect, NULL)
            || jack_set_process_callback(g_midi_client, midi_process, NULL)
            || jack_set_xrun_callback(g_midi_client, on_xrun, NULL)
            || jack_set_buffer_size_callback(g_midi_client, on_buffer_size, NULL)
            || jack_set_sample_rate_callback(g_midi_client, on_sample_rate, NULL)) {
        snprintf(error, sizeof(error), "callback_registration"); goto fail;
    }
    if (jack_activate(g_capture_client)) { snprintf(error, sizeof(error), "capture_activate"); goto fail; }
    if (jack_activate(g_midi_client)) { snprintf(error, sizeof(error), "midi_activate"); goto fail; }
    if (jack_connect(g_midi_client, MIDI_CLIENT ":midi_out", TARGET_CLIENT ":midi_in")
            || jack_connect(g_capture_client, TARGET_CLIENT ":out_L", CAPTURE_CLIENT ":capture_L")
            || jack_connect(g_capture_client, TARGET_CLIENT ":out_R", CAPTURE_CLIENT ":capture_R")) {
        snprintf(error, sizeof(error), "capture_connect"); goto fail;
    }
    /* Let JACK finish publishing our three edges before exact verification
     * and arming; setup callbacks are deliberately outside evidence time. */
    struct timespec graph_settle = {0, 100000000};
    nanosleep(&graph_settle, NULL);
    if (!connection_count(g_midi_client, g_midi_out, TARGET_CLIENT ":midi_in")
            || !connection_count(g_capture_client, g_capture_l, TARGET_CLIENT ":out_L")
            || !connection_count(g_capture_client, g_capture_r, TARGET_CLIENT ":out_R")
            || !connection_count(g_capture_client, target_midi, MIDI_CLIENT ":midi_out")
            || !connection_count(g_capture_client, target_l, CAPTURE_CLIENT ":capture_L")
            || !connection_count(g_capture_client, target_r, CAPTURE_CLIENT ":capture_R")) {
        snprintf(error, sizeof(error), "connection_verification"); goto fail;
    }
    atomic_store_explicit(&g_armed, 1, memory_order_release);
    printf("{\"event\":\"capture_started\",\"sample_rate_hz\":%u,\"block_frames\":%u,\"seconds\":%.6f}\n",
           rate, g_block_size, duration);
    fflush(stdout);
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    double deadline = monotonic_seconds() + duration + 5.0;
    while (atomic_load_explicit(&g_frames, memory_order_acquire) < g_frame_limit && !g_interrupted
            && !atomic_load_explicit(&g_callback_errors, memory_order_relaxed)
            && monotonic_seconds() < deadline) {
        struct timespec nap = {0, 10000000}; nanosleep(&nap, NULL);
    }
    atomic_store_explicit(&g_armed, 0, memory_order_release);
    int connections_intact =
        connection_count(g_midi_client, g_midi_out, TARGET_CLIENT ":midi_in")
        && connection_count(g_capture_client, g_capture_l, TARGET_CLIENT ":out_L")
        && connection_count(g_capture_client, g_capture_r, TARGET_CLIENT ":out_R")
        && connection_count(g_capture_client, target_midi, MIDI_CLIENT ":midi_out")
        && connection_count(g_capture_client, target_l, CAPTURE_CLIENT ":capture_L")
        && connection_count(g_capture_client, target_r, CAPTURE_CLIENT ":capture_R");
    jack_deactivate(g_midi_client);
    jack_deactivate(g_capture_client);
    if (g_interrupted) { snprintf(error, sizeof(error), "interrupted"); goto fail; }
    if (!connections_intact) { snprintf(error, sizeof(error), "capture_connection_lost"); goto fail; }
    if (atomic_load(&g_frames) != g_frame_limit) { snprintf(error, sizeof(error), "capture_incomplete"); goto fail; }
    uint64_t captured_frames = atomic_load(&g_frames);
    if (write_wav(wav_path, rate, captured_frames)) { snprintf(error, sizeof(error), "wav_write"); goto fail; }
    /* A full raw capture remains useful forensic evidence even if its quality
     * counters fail. Keep the exclusively-created WAV, but never report pass. */
    if (g_next_event != g_event_count || atomic_load(&g_midi_sent) != g_event_count) {
        snprintf(error, sizeof(error), "midi_schedule_incomplete"); goto fail;
    }
    if (atomic_load(&g_callback_errors) || atomic_load(&g_midi_failures)
            || g_nonfinite || atomic_load(&g_xruns)) {
        snprintf(error, sizeof(error), "capture_evidence_failed"); goto fail;
    }
    jack_client_close(g_midi_client); g_midi_client = NULL;
    jack_client_close(g_capture_client); g_capture_client = NULL;
    printf("{\"event\":\"capture_complete\",\"sample_rate_hz\":%u,\"block_frames\":%u,"
           "\"frames\":%llu,\"midi_events\":%zu,\"peak\":%.9g,\"nonfinite\":%llu,"
           "\"xruns\":%d,\"errors\":%d,\"clock\":\"CLOCK_MONOTONIC\","
           "\"midi_first_callback_monotonic_ns\":%llu,"
           "\"capture_first_callback_monotonic_ns\":%llu}\n", rate, g_block_size,
           (unsigned long long)captured_frames, (size_t)atomic_load(&g_midi_sent), g_peak,
           (unsigned long long)g_nonfinite, atomic_load(&g_xruns),
           atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures),
           (unsigned long long)atomic_load(&g_midi_first_ns),
           (unsigned long long)atomic_load(&g_capture_first_ns));
    free(g_audio_l); free(g_audio_r); free(g_events); return 0;

fail:
    atomic_store(&g_armed, 0);
    if (g_midi_client) { jack_deactivate(g_midi_client); jack_client_close(g_midi_client); }
    if (g_capture_client) { jack_deactivate(g_capture_client); jack_client_close(g_capture_client); }
    fprintf(stderr, "%s\n", error);
    printf("{\"event\":\"capture_failed\",\"error\":\"%s\",\"frames\":%llu,"
           "\"midi_events_sent\":%zu,\"peak\":%.9g,\"nonfinite\":%llu,"
           "\"xruns\":%d,\"errors\":%d,\"clock\":\"CLOCK_MONOTONIC\","
           "\"midi_first_callback_monotonic_ns\":%llu,"
           "\"capture_first_callback_monotonic_ns\":%llu}\n",
           error, (unsigned long long)atomic_load(&g_frames),
           (size_t)atomic_load(&g_midi_sent), g_peak,
           (unsigned long long)g_nonfinite, atomic_load(&g_xruns),
           atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures),
           (unsigned long long)atomic_load(&g_midi_first_ns),
           (unsigned long long)atomic_load(&g_capture_first_ns));
    free(g_audio_l); free(g_audio_r); free(g_events); return 1;
}
