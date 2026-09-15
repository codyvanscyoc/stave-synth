/* Bounded, isolated JACK MIDI + finite-output monitor for Stave's core soak.
 *
 * Build on the Pi:
 *   cc -std=c11 -O2 -Wall -Wextra -Werror -o native_soak \
 *      tools/native_soak.c -ljack -lm
 *
 * This program never starts JACK, touches physical ports, or stores audio.
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
#define REQUIRED_RATE 48000u
#define REQUIRED_BLOCK 512u
#define MAX_EVENTS 4096u
#define MAX_SUMMARIES 481u
#define INTERVAL_SECONDS 60u
#define TAIL_SECONDS 2u
#define TERMINAL_GRACE_SECONDS 5u
#define MIN_SECONDS 600.0
#define MAX_SECONDS 28800.0
#define SMOKE_MIN_SECONDS 20.0

typedef struct {
    double seconds;
    uint64_t frame;
    uint8_t bytes[3];
} MidiEvent;

typedef struct {
    atomic_int ready;
    uint64_t start_frame, frames, midi_events;
    uint64_t active_blocks, zero_active_blocks, nonfinite;
    uint64_t clipped;
    double peak, square_sum;
} AudioSummary;

static jack_client_t *g_capture_client, *g_midi_client;
static jack_port_t *g_midi_out, *g_capture_l, *g_capture_r;
static MidiEvent *g_events;
static size_t g_event_count, g_next_event;
static uint64_t g_cycle_base, g_loop_frames, g_requested_frames;
static atomic_uint_fast64_t g_capture_stop_frame;
static atomic_uint_fast64_t g_capture_frames, g_midi_frames, g_midi_sent;
static atomic_uint_fast64_t g_phrase_loops, g_release_frame;
static atomic_uint_fast64_t g_midi_first_ns, g_capture_first_ns;
static atomic_uint_fast64_t g_total_nonfinite, g_total_active_blocks;
static atomic_uint_fast64_t g_total_zero_active_blocks;
static atomic_uint_fast64_t g_total_clipped;
static atomic_uint_fast32_t g_active_notes;
static atomic_uint_fast32_t g_pedal_mask;
static atomic_int g_armed, g_stop_requested, g_terminal_release_sent;
static atomic_int g_music_complete, g_release_authorized;
static atomic_int g_callback_errors, g_midi_failures, g_xruns;
static AudioSummary g_summaries[MAX_SUMMARIES];
static unsigned g_summary_index;
static uint64_t g_summary_start, g_summary_frames, g_summary_midi_start;
static uint64_t g_summary_active_blocks, g_summary_zero_active, g_summary_nonfinite;
static uint64_t g_summary_clipped;
static double g_summary_peak, g_summary_squares;
static double g_total_peak, g_total_squares;
static atomic_uint_fast64_t g_music_blocks, g_zero_music_blocks;
static int g_expect_bed;
static volatile sig_atomic_t g_interrupted;

static void on_signal(int signum) {
    (void)signum;
    g_interrupted = 1;
}

static uint64_t monotonic_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

static double monotonic_seconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0.0;
    return now.tv_sec + now.tv_nsec / 1e9;
}

static void latch_error(void) {
    atomic_fetch_add_explicit(&g_callback_errors, 1, memory_order_relaxed);
}

static int reserve_midi(void *buffer, jack_nframes_t offset,
                        uint8_t status, uint8_t data1, uint8_t data2) {
    jack_midi_data_t *dst = jack_midi_event_reserve(buffer, offset, 3);
    if (dst == NULL) {
        atomic_fetch_add_explicit(&g_midi_failures, 1, memory_order_relaxed);
        return -1;
    }
    dst[0] = status; dst[1] = data1; dst[2] = data2;
    atomic_fetch_add_explicit(&g_midi_sent, 1, memory_order_relaxed);
    return 0;
}

static void track_event(const MidiEvent *event) {
    unsigned command = event->bytes[0] & 0xf0u;
    if (command == 0x90u && event->bytes[2] != 0) {
        atomic_fetch_add_explicit(&g_active_notes, 1, memory_order_relaxed);
    } else if (command == 0x80u || (command == 0x90u && event->bytes[2] == 0)) {
        uint_fast32_t count = atomic_load_explicit(&g_active_notes, memory_order_relaxed);
        if (count) atomic_fetch_sub_explicit(&g_active_notes, 1, memory_order_relaxed);
    } else if (command == 0xb0u && (event->bytes[1] == 64u || event->bytes[1] == 66u)) {
        uint_fast32_t bit = UINT32_C(1) << ((event->bytes[0] & 0x0fu) * 2u
                                            + (event->bytes[1] == 66u));
        uint_fast32_t mask = atomic_load_explicit(&g_pedal_mask, memory_order_relaxed);
        atomic_store_explicit(&g_pedal_mask,
                              event->bytes[2] >= 64u ? mask | bit : mask & ~bit,
                              memory_order_relaxed);
    }
}

static int midi_process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    void *midi = jack_port_get_buffer(g_midi_out, nframes);
    if (midi == NULL) {
        if (atomic_load_explicit(&g_armed, memory_order_acquire)) latch_error();
        return 0;
    }
    jack_midi_clear_buffer(midi);
    if (!atomic_load_explicit(&g_armed, memory_order_acquire)) return 0;
    if (nframes != REQUIRED_BLOCK) { latch_error(); return 0; }

    uint64_t start = atomic_load_explicit(&g_midi_frames, memory_order_relaxed);
    if (start == 0) {
        uint64_t stamp = monotonic_ns();
        atomic_store_explicit(&g_midi_first_ns, stamp, memory_order_release);
        if (!stamp) latch_error();
    }
    int interrupted_stop = atomic_load_explicit(&g_stop_requested, memory_order_relaxed);
    if (start >= g_requested_frames)
        atomic_store_explicit(&g_music_complete, 1, memory_order_release);
    if (interrupted_stop || (start >= g_requested_frames
            && atomic_load_explicit(&g_release_authorized, memory_order_acquire))) {
        if (!atomic_exchange_explicit(&g_terminal_release_sent, 1, memory_order_acq_rel)) {
            int failed = 0;
            failed |= reserve_midi(midi, 0, 0xb0, 66, 0);
            failed |= reserve_midi(midi, 0, 0xb0, 64, 0);
            failed |= reserve_midi(midi, 0, 0xb0, 123, 0);
            atomic_store_explicit(&g_active_notes, 0, memory_order_relaxed);
            atomic_store_explicit(&g_pedal_mask, 0, memory_order_relaxed);
            uint64_t release = atomic_load_explicit(&g_capture_frames, memory_order_relaxed);
            atomic_store_explicit(&g_release_frame, release, memory_order_release);
            atomic_store_explicit(&g_capture_stop_frame,
                                  release + (uint64_t)TAIL_SECONDS * REQUIRED_RATE,
                                  memory_order_release);
            if (failed) latch_error();
        }
        atomic_store_explicit(&g_midi_frames, start + nframes, memory_order_release);
        return 0;
    }

    uint64_t stop = start + nframes;
    if (stop > g_requested_frames) stop = g_requested_frames;
    while (g_event_count && g_cycle_base + g_events[g_next_event].frame < stop) {
        uint64_t absolute = g_cycle_base + g_events[g_next_event].frame;
        MidiEvent *event = &g_events[g_next_event];
        if (absolute >= start) {
            if (!reserve_midi(midi, (jack_nframes_t)(absolute - start),
                              event->bytes[0], event->bytes[1], event->bytes[2]))
                track_event(event);
        }
        g_next_event++;
        if (g_next_event == g_event_count) {
            g_next_event = 0;
            g_cycle_base += g_loop_frames;
            atomic_fetch_add_explicit(&g_phrase_loops, 1, memory_order_relaxed);
        }
    }
    atomic_store_explicit(&g_midi_frames, start + nframes, memory_order_release);
    return 0;
}

static void publish_summary(void) {
    if (g_summary_index >= MAX_SUMMARIES || g_summary_frames == 0) {
        if (g_summary_index >= MAX_SUMMARIES) latch_error();
        return;
    }
    AudioSummary *summary = &g_summaries[g_summary_index++];
    uint64_t midi_now = atomic_load_explicit(&g_midi_sent, memory_order_acquire);
    summary->start_frame = g_summary_start;
    summary->frames = g_summary_frames;
    summary->midi_events = midi_now - g_summary_midi_start;
    summary->active_blocks = g_summary_active_blocks;
    summary->zero_active_blocks = g_summary_zero_active;
    summary->nonfinite = g_summary_nonfinite;
    summary->clipped = g_summary_clipped;
    summary->peak = g_summary_peak;
    summary->square_sum = g_summary_squares;
    atomic_store_explicit(&summary->ready, 1, memory_order_release);
    g_summary_start += g_summary_frames;
    g_summary_frames = 0;
    g_summary_midi_start = midi_now;
    g_summary_active_blocks = g_summary_zero_active = g_summary_nonfinite = 0;
    g_summary_clipped = 0;
    g_summary_peak = g_summary_squares = 0.0;
}

static int capture_process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    if (!atomic_load_explicit(&g_armed, memory_order_acquire)) return 0;
    if (nframes != REQUIRED_BLOCK) { latch_error(); return 0; }
    float *left = (float *)jack_port_get_buffer(g_capture_l, nframes);
    float *right = (float *)jack_port_get_buffer(g_capture_r, nframes);
    if (left == NULL || right == NULL) { latch_error(); return 0; }
    uint64_t start = atomic_load_explicit(&g_capture_frames, memory_order_relaxed);
    if (start == 0) {
        uint64_t stamp = monotonic_ns();
        atomic_store_explicit(&g_capture_first_ns, stamp, memory_order_release);
        if (!stamp) latch_error();
    }
    uint64_t capture_limit = atomic_load_explicit(&g_capture_stop_frame, memory_order_acquire);
    if (start >= capture_limit) return 0;
    uint64_t take = nframes;
    if (start + take > capture_limit) take = capture_limit - start;
    int active = (atomic_load_explicit(&g_active_notes, memory_order_relaxed) > 0
                  || atomic_load_explicit(&g_pedal_mask, memory_order_relaxed) != 0);
    int block_nonzero = 0;
    int block_nonfinite = 0, block_clipped = 0, block_zero_active = 0;
    for (uint64_t i = 0; i < take; i++) {
        float values[2] = {left[i], right[i]};
        for (unsigned channel = 0; channel < 2; channel++) {
            double value = values[channel];
            if (!isfinite(value)) {
                block_nonfinite = 1;
                g_summary_nonfinite++;
                atomic_fetch_add_explicit(&g_total_nonfinite, 1, memory_order_relaxed);
                continue;
            }
            double magnitude = fabs(value);
            if (magnitude >= 1.0) {
                block_clipped = 1;
                g_summary_clipped++;
                atomic_fetch_add_explicit(&g_total_clipped, 1, memory_order_relaxed);
            }
            if (magnitude != 0.0) block_nonzero = 1;
            if (magnitude > g_summary_peak) g_summary_peak = magnitude;
            if (magnitude > g_total_peak) g_total_peak = magnitude;
            g_summary_squares += value * value;
            g_total_squares += value * value;
        }
    }
    if (active) {
        g_summary_active_blocks++;
        atomic_fetch_add_explicit(&g_total_active_blocks, 1, memory_order_relaxed);
        if (!block_nonzero) {
            block_zero_active = 1;
            g_summary_zero_active++;
            atomic_fetch_add_explicit(&g_total_zero_active_blocks, 1, memory_order_relaxed);
        }
    }
    if (start < g_requested_frames) {
        atomic_fetch_add_explicit(&g_music_blocks, 1, memory_order_relaxed);
        if (!block_nonzero)
            atomic_fetch_add_explicit(&g_zero_music_blocks, 1, memory_order_relaxed);
    }
    if (block_nonfinite || block_clipped || block_zero_active
            || (g_expect_bed && start < g_requested_frames && !block_nonzero))
        latch_error();
    g_summary_frames += take;
    atomic_store_explicit(&g_capture_frames, start + take, memory_order_release);
    if (g_summary_frames >= (uint64_t)REQUIRED_RATE * INTERVAL_SECONDS)
        publish_summary();
    return 0;
}

static int on_xrun(void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed))
        atomic_fetch_add_explicit(&g_xruns, 1, memory_order_relaxed);
    return 0;
}
static int on_buffer_size(jack_nframes_t frames, void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed) && frames != REQUIRED_BLOCK)
        latch_error();
    return 0;
}
static int on_sample_rate(jack_nframes_t rate, void *arg) {
    (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed) && rate != REQUIRED_RATE)
        latch_error();
    return 0;
}
static void on_port_connect(jack_port_id_t a, jack_port_id_t b, int connected, void *arg) {
    (void)a; (void)b; (void)connected; (void)arg;
    if (atomic_load_explicit(&g_armed, memory_order_relaxed)) latch_error();
}

static int parse_double(const char *text, double *value) {
    char *end = NULL;
    errno = 0;
    double parsed = strtod(text, &end);
    if (errno || end == text || *end != '\0' || !isfinite(parsed)) return -1;
    *value = parsed;
    return 0;
}

static int load_phrase(const char *path, char *error, size_t cap) {
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) { snprintf(error, cap, "phrase_open"); return -1; }
    struct stat file_stat;
    if (fstat(fd, &file_stat) || !S_ISREG(file_stat.st_mode) || file_stat.st_size > 1024 * 1024) {
        close(fd); snprintf(error, cap, "phrase_not_bounded_regular"); return -1;
    }
    FILE *file = fdopen(fd, "r");
    if (!file) { close(fd); snprintf(error, cap, "phrase_open"); return -1; }
    g_events = calloc(MAX_EVENTS, sizeof(*g_events));
    if (!g_events) { fclose(file); snprintf(error, cap, "phrase_alloc"); return -1; }
    unsigned char notes[16][128] = {{0}}, pedal64[16] = {0}, pedal66[16] = {0};
    int have_loop = 0;
    double loop_seconds = 0.0, previous = -1.0;
    char line[512]; unsigned long line_no = 0;
    while (fgets(line, sizeof(line), file)) {
        line_no++;
        if (!strchr(line, '\n') && !feof(file)) {
            snprintf(error, cap, "phrase_line_too_long_%lu", line_no); fclose(file); return -1;
        }
        char *p = line; while (*p == ' ' || *p == '\t') p++;
        if (*p == '\0' || *p == '\n' || *p == '#') continue;
        if (!have_loop) {
            char extra;
            if (sscanf(p, "loop_seconds %lf %c", &loop_seconds, &extra) != 1
                    || !isfinite(loop_seconds) || loop_seconds < 5.0 || loop_seconds > 300.0) {
                snprintf(error, cap, "phrase_requires_loop_seconds"); fclose(file); return -1;
            }
            have_loop = 1; continue;
        }
        double when; unsigned status, data1, data2; char extra;
        if (sscanf(p, "%lf %x %u %u %c", &when, &status, &data1, &data2, &extra) != 4
                || !isfinite(when) || when < 0.0 || when >= loop_seconds || when < previous
                || status > 255 || data1 > 127 || data2 > 127
                || !((status & 0xf0u) == 0x80u || (status & 0xf0u) == 0x90u
                     || (status & 0xf0u) == 0xb0u || (status & 0xf0u) == 0xe0u)) {
            snprintf(error, cap, "phrase_invalid_line_%lu", line_no); fclose(file); return -1;
        }
        if (g_event_count == MAX_EVENTS) {
            snprintf(error, cap, "phrase_too_many_events"); fclose(file); return -1;
        }
        unsigned command = status & 0xf0u, channel = status & 0x0fu;
        if (channel != 0u) {
            snprintf(error, cap, "phrase_requires_channel_zero"); fclose(file); return -1;
        }
        if (command == 0xb0u && (data1 == 120u || data1 == 123u)) {
            snprintf(error, cap, "phrase_forbids_loop_panic"); fclose(file); return -1;
        }
        if (command == 0x90u && data2 != 0) {
            if (notes[channel][data1]) { snprintf(error, cap, "phrase_retrigger_without_release"); fclose(file); return -1; }
            notes[channel][data1] = 1;
        } else if (command == 0x80u || (command == 0x90u && data2 == 0)) {
            if (!notes[channel][data1]) { snprintf(error, cap, "phrase_unmatched_noteoff"); fclose(file); return -1; }
            notes[channel][data1] = 0;
        } else if (command == 0xb0u && data1 == 64u) pedal64[channel] = data2 >= 64u;
        else if (command == 0xb0u && data1 == 66u) pedal66[channel] = data2 >= 64u;
        MidiEvent *event = &g_events[g_event_count++];
        event->seconds = when; event->bytes[0] = status;
        event->bytes[1] = data1; event->bytes[2] = data2;
        previous = when;
    }
    if (ferror(file)) { snprintf(error, cap, "phrase_read"); fclose(file); return -1; }
    fclose(file);
    if (!have_loop || g_event_count == 0) { snprintf(error, cap, "phrase_empty"); return -1; }
    for (unsigned channel = 0; channel < 16; channel++) {
        if (pedal64[channel] || pedal66[channel]) { snprintf(error, cap, "phrase_pedal_left_down"); return -1; }
        for (unsigned note = 0; note < 128; note++) if (notes[channel][note]) {
            snprintf(error, cap, "phrase_note_left_on"); return -1;
        }
    }
    g_loop_frames = (uint64_t)llround(loop_seconds * REQUIRED_RATE);
    for (size_t i = 0; i < g_event_count; i++) {
        g_events[i].frame = (uint64_t)llround(g_events[i].seconds * REQUIRED_RATE);
        if (g_events[i].frame >= g_loop_frames) {
            snprintf(error, cap, "phrase_event_rounds_past_loop"); return -1;
        }
    }
    return 0;
}

static int exact_port(jack_port_t *port, const char *name, const char *type,
                      unsigned long direction, char *error, size_t cap) {
    if (!port || strcmp(jack_port_name(port), name) || strcmp(jack_port_type(port), type)
            || (jack_port_flags(port) & (JackPortIsInput | JackPortIsOutput)) != direction) {
        snprintf(error, cap, "port_contract"); return -1;
    }
    return 0;
}
static int connection_count(jack_client_t *client, jack_port_t *port, const char *only) {
    const char **items = jack_port_get_all_connections(client, port);
    int count = 0, matched = 0;
    if (items) { for (; items[count]; count++) if (!strcmp(items[count], only)) matched++; jack_free(items); }
    return count == 1 && matched == 1;
}
static int has_no_connections(jack_client_t *client, jack_port_t *port) {
    const char **items = jack_port_get_all_connections(client, port);
    if (!items) return 1;
    int empty = items[0] == NULL; jack_free(items); return empty;
}

static void print_summary(unsigned index, const AudioSummary *summary) {
    double rms = summary->frames ? sqrt(summary->square_sum / (2.0 * summary->frames)) : 0.0;
    printf("{\"event\":\"soak_checkpoint\",\"index\":%u,\"start_frame\":%llu,"
           "\"frames\":%llu,\"midi_events\":%llu,\"active_blocks\":%llu,"
           "\"zero_active_blocks\":%llu,\"nonfinite\":%llu,\"clipped\":%llu,\"peak\":%.9g,"
           "\"rms\":%.9g,\"xruns\":%d,\"errors\":%d,\"monotonic_ns\":%llu}\n",
           index, (unsigned long long)summary->start_frame,
           (unsigned long long)summary->frames, (unsigned long long)summary->midi_events,
           (unsigned long long)summary->active_blocks,
           (unsigned long long)summary->zero_active_blocks,
           (unsigned long long)summary->nonfinite, (unsigned long long)summary->clipped,
           summary->peak, rms,
           atomic_load(&g_xruns), atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures),
           (unsigned long long)monotonic_ns());
    fflush(stdout);
}

static int is_eight_hour_qualification(double duration, int smoke) {
    return !smoke && duration == MAX_SECONDS;
}

static int grace_has_elapsed(uint64_t now_ns, uint64_t deadline_ns) {
    return deadline_ns != 0 && now_ns >= deadline_ns;
}

static const char *completion_failure(int connections_intact, uint64_t captured) {
    if (g_interrupted) return "interrupted";
    if (!connections_intact) return "soak_connection_lost";
    if (captured != atomic_load(&g_capture_stop_frame)) return "capture_incomplete";
    if (!atomic_load(&g_terminal_release_sent)) return "terminal_release_missing";
    if (atomic_load(&g_xruns)
            || atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures)
            || atomic_load(&g_total_nonfinite) || atomic_load(&g_total_clipped)
            || atomic_load(&g_total_zero_active_blocks)
            || (g_expect_bed && atomic_load(&g_zero_music_blocks)))
        return "soak_evidence_failed";
    return NULL;
}

int main(int argc, char **argv) {
    int validate = argc == 4 && !strcmp(argv[1], "--validate");
    int smoke = (argc >= 2 && !strcmp(argv[1], "--smoke"));
    int option_count = validate || smoke ? 1 : 0;
    if (smoke && argc >= 3 && !strcmp(argv[2], "--expect-bed")) {
        g_expect_bed = 1; option_count++;
    } else if (!validate && !smoke && argc >= 2 && !strcmp(argv[1], "--expect-bed")) {
        g_expect_bed = 1; option_count++;
    }
    if (validate ? argc != 4 : argc != 3 + option_count) {
        fprintf(stderr, "usage: %s [--expect-bed] PHRASE SECONDS\n       %s --smoke [--expect-bed] PHRASE SECONDS\n       %s --validate PHRASE SECONDS\n", argv[0], argv[0], argv[0]);
        return 2;
    }
    const char *phrase_path = argv[1 + option_count];
    const char *duration_text = argv[2 + option_count];
    double duration; char error[128] = "none";
    if (parse_double(duration_text, &duration)
            || (validate && (duration < SMOKE_MIN_SECONDS || duration > MAX_SECONDS))
            || (smoke && (duration < SMOKE_MIN_SECONDS || duration >= MIN_SECONDS))
            || (!validate && !smoke && (duration < MIN_SECONDS || duration > MAX_SECONDS))) {
        fprintf(stderr, "invalid duration for selected mode\n"); return 2;
    }
    if (load_phrase(phrase_path, error, sizeof(error))) {
        fprintf(stderr, "%s\n", error); free(g_events); return 2;
    }
    if (validate) {
        printf("{\"event\":\"soak_phrase_valid\",\"qualification\":false,"
               "\"events\":%zu,\"loop_seconds\":%.6f,\"requested_seconds\":%.6f}\n",
               g_event_count, (double)g_loop_frames / REQUIRED_RATE, duration);
        free(g_events); return 0;
    }
    if (!monotonic_ns()) { snprintf(error, sizeof(error), "monotonic_clock_unavailable"); goto fail; }
    /* The callbacks and signal handoff rely on truly lock-free C11 atomics;
     * reject an ABI where these operations could hide a library mutex. */
    if (!atomic_is_lock_free(&g_capture_frames)
            || !atomic_is_lock_free(&g_active_notes)
            || !atomic_is_lock_free(&g_armed)) {
        snprintf(error, sizeof(error), "required_atomics_not_lock_free"); goto fail;
    }
    g_requested_frames = (uint64_t)llround(duration * REQUIRED_RATE);
    /* This is only a safe initial ceiling. The MIDI callback replaces it with
     * the exact observed release frame + tail when terminal release is sent. */
    atomic_store(&g_capture_stop_frame,
                 g_requested_frames
                 + (uint64_t)(TERMINAL_GRACE_SECONDS + TAIL_SECONDS) * REQUIRED_RATE);
    jack_status_t status = 0;
    g_capture_client = jack_client_open(CAPTURE_CLIENT, JackNoStartServer | JackUseExactName, &status);
    if (!g_capture_client || (status & JackNameNotUnique)
            || strcmp(jack_get_client_name(g_capture_client), CAPTURE_CLIENT)) {
        snprintf(error, sizeof(error), "capture_client_identity"); goto fail;
    }
    status = 0;
    g_midi_client = jack_client_open(MIDI_CLIENT, JackNoStartServer | JackUseExactName, &status);
    if (!g_midi_client || (status & JackNameNotUnique)
            || strcmp(jack_get_client_name(g_midi_client), MIDI_CLIENT)) {
        snprintf(error, sizeof(error), "midi_client_identity"); goto fail;
    }
    if (jack_get_sample_rate(g_capture_client) != REQUIRED_RATE
            || jack_get_buffer_size(g_capture_client) != REQUIRED_BLOCK
            || jack_get_sample_rate(g_midi_client) != REQUIRED_RATE
            || jack_get_buffer_size(g_midi_client) != REQUIRED_BLOCK) {
        snprintf(error, sizeof(error), "unsupported_graph"); goto fail;
    }
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
    if (jack_activate(g_capture_client) || jack_activate(g_midi_client)) {
        snprintf(error, sizeof(error), "client_activate"); goto fail;
    }
    if (jack_connect(g_midi_client, MIDI_CLIENT ":midi_out", TARGET_CLIENT ":midi_in")
            || jack_connect(g_capture_client, TARGET_CLIENT ":out_L", CAPTURE_CLIENT ":capture_L")
            || jack_connect(g_capture_client, TARGET_CLIENT ":out_R", CAPTURE_CLIENT ":capture_R")) {
        snprintf(error, sizeof(error), "soak_connect"); goto fail;
    }
    struct timespec settle = {0, 100000000}; nanosleep(&settle, NULL);
    if (!connection_count(g_midi_client, g_midi_out, TARGET_CLIENT ":midi_in")
            || !connection_count(g_capture_client, g_capture_l, TARGET_CLIENT ":out_L")
            || !connection_count(g_capture_client, g_capture_r, TARGET_CLIENT ":out_R")
            || !connection_count(g_capture_client, target_midi, MIDI_CLIENT ":midi_out")
            || !connection_count(g_capture_client, target_l, CAPTURE_CLIENT ":capture_L")
            || !connection_count(g_capture_client, target_r, CAPTURE_CLIENT ":capture_R")) {
        snprintf(error, sizeof(error), "connection_verification"); goto fail;
    }
    uint64_t started = monotonic_ns();
    atomic_store_explicit(&g_armed, 1, memory_order_release);
    int qualification = is_eight_hour_qualification(duration, smoke);
    printf("{\"event\":\"soak_started\",\"qualification\":%s,\"expect_bed\":%s,\"requested_seconds\":%.6f,"
           "\"tail_seconds\":%u,\"terminal_grace_seconds\":%u,\"sample_rate_hz\":%u,\"block_frames\":%u,"
           "\"loop_seconds\":%.6f,\"phrase_events\":%zu,\"started_monotonic_ns\":%llu}\n",
           qualification ? "true" : "false", g_expect_bed ? "true" : "false",
           duration, TAIL_SECONDS, TERMINAL_GRACE_SECONDS, REQUIRED_RATE, REQUIRED_BLOCK,
           (double)g_loop_frames / REQUIRED_RATE, g_event_count, (unsigned long long)started);
    fflush(stdout);
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    double deadline = monotonic_seconds() + duration + TAIL_SECONDS + 10.0;
    unsigned emitted = 0;
    int music_marker_emitted = 0;
    uint64_t release_authorize_ns = 0;
    while (atomic_load_explicit(&g_capture_frames, memory_order_acquire)
               < atomic_load_explicit(&g_capture_stop_frame, memory_order_acquire)
            && !atomic_load_explicit(&g_callback_errors, memory_order_relaxed)
            && monotonic_seconds() < deadline) {
        if (g_interrupted)
            atomic_store_explicit(&g_stop_requested, 1, memory_order_release);
        while (emitted < MAX_SUMMARIES
                && atomic_load_explicit(&g_summaries[emitted].ready, memory_order_acquire)) {
            print_summary(emitted, &g_summaries[emitted]); emitted++;
        }
        if (!music_marker_emitted
                && atomic_load_explicit(&g_music_complete, memory_order_acquire)) {
            uint64_t marker_ns = monotonic_ns();
            printf("{\"event\":\"soak_music_complete\",\"requested_frames\":%llu,"
                   "\"phrase_midi_events\":%llu,\"monotonic_ns\":%llu}\n",
                   (unsigned long long)g_requested_frames,
                   (unsigned long long)atomic_load(&g_midi_sent),
                   (unsigned long long)marker_ns);
            fflush(stdout);
            music_marker_emitted = 1;
            if (!marker_ns) latch_error();
            else release_authorize_ns = marker_ns
                 + (uint64_t)TERMINAL_GRACE_SECONDS * UINT64_C(1000000000);
        }
        if (music_marker_emitted && !g_interrupted
                && grace_has_elapsed(monotonic_ns(), release_authorize_ns))
            atomic_store_explicit(&g_release_authorized, 1, memory_order_release);
        struct timespec nap = {0, 10000000}; nanosleep(&nap, NULL);
    }
    /* On a failed monitor path, give the still-live MIDI callback a short
     * best-effort chance to deliver the same terminal release. Evidence still
     * fails; this only reduces the chance of leaving audition voices held. */
    if (!atomic_load_explicit(&g_terminal_release_sent, memory_order_acquire)) {
        atomic_store_explicit(&g_stop_requested, 1, memory_order_release);
        double release_deadline = monotonic_seconds() + 0.25;
        while (!atomic_load_explicit(&g_terminal_release_sent, memory_order_acquire)
                && monotonic_seconds() < release_deadline) {
            struct timespec nap = {0, 10000000}; nanosleep(&nap, NULL);
        }
    }
    atomic_store_explicit(&g_armed, 0, memory_order_release);
    int connections_intact = connection_count(g_midi_client, g_midi_out, TARGET_CLIENT ":midi_in")
        && connection_count(g_capture_client, g_capture_l, TARGET_CLIENT ":out_L")
        && connection_count(g_capture_client, g_capture_r, TARGET_CLIENT ":out_R")
        && connection_count(g_capture_client, target_midi, MIDI_CLIENT ":midi_out")
        && connection_count(g_capture_client, target_l, CAPTURE_CLIENT ":capture_L")
        && connection_count(g_capture_client, target_r, CAPTURE_CLIENT ":capture_R");
    jack_deactivate(g_midi_client); jack_deactivate(g_capture_client);
    if (g_summary_frames) publish_summary();
    while (emitted < g_summary_index) {
        print_summary(emitted, &g_summaries[emitted]);
        emitted++;
    }
    uint64_t captured = atomic_load(&g_capture_frames), nonfinite = atomic_load(&g_total_nonfinite);
    uint64_t active_blocks = atomic_load(&g_total_active_blocks);
    uint64_t zero_active = atomic_load(&g_total_zero_active_blocks);
    uint64_t clipped = atomic_load(&g_total_clipped);
    int errors = atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures);
    const char *failure = completion_failure(connections_intact, captured);
    if (failure) snprintf(error, sizeof(error), "%s", failure);
    else error[0] = '\0';
    double rms = captured ? sqrt(g_total_squares / (2.0 * captured)) : 0.0;
    if (error[0]) goto fail_after_close;
    jack_client_close(g_midi_client); g_midi_client = NULL;
    jack_client_close(g_capture_client); g_capture_client = NULL;
    printf("{\"event\":\"soak_complete\",\"qualification\":%s,\"requested_frames\":%llu,"
           "\"captured_frames\":%llu,\"checkpoints\":%u,\"midi_events\":%llu,"
           "\"phrase_midi_events\":%llu,\"phrase_loops\":%llu,\"terminal_release_events\":3,\"active_blocks\":%llu,"
           "\"music_blocks\":%llu,\"zero_music_blocks\":%llu,"
           "\"zero_active_blocks\":%llu,\"peak\":%.9g,\"rms\":%.9g,\"nonfinite\":%llu,\"clipped\":%llu,"
           "\"xruns\":%d,\"errors\":%d,\"interrupted\":%s,\"clock\":\"CLOCK_MONOTONIC\","
           "\"midi_first_callback_monotonic_ns\":%llu,\"capture_first_callback_monotonic_ns\":%llu}\n",
           qualification ? "true" : "false", (unsigned long long)g_requested_frames,
           (unsigned long long)captured, g_summary_index, (unsigned long long)atomic_load(&g_midi_sent),
           (unsigned long long)(atomic_load(&g_midi_sent) - 3),
           (unsigned long long)atomic_load(&g_phrase_loops), (unsigned long long)active_blocks,
           (unsigned long long)atomic_load(&g_music_blocks),
           (unsigned long long)atomic_load(&g_zero_music_blocks),
           (unsigned long long)zero_active, g_total_peak, rms, (unsigned long long)nonfinite,
           (unsigned long long)clipped,
           atomic_load(&g_xruns), errors, g_interrupted ? "true" : "false",
           (unsigned long long)atomic_load(&g_midi_first_ns),
           (unsigned long long)atomic_load(&g_capture_first_ns));
    free(g_events); return 0;

fail_after_close:
    jack_client_close(g_midi_client); g_midi_client = NULL;
    jack_client_close(g_capture_client); g_capture_client = NULL;
fail:
    atomic_store(&g_armed, 0);
    if (g_midi_client) { jack_deactivate(g_midi_client); jack_client_close(g_midi_client); }
    if (g_capture_client) { jack_deactivate(g_capture_client); jack_client_close(g_capture_client); }
    fprintf(stderr, "%s\n", error);
    printf("{\"event\":\"soak_failed\",\"error\":\"%s\",\"captured_frames\":%llu,"
           "\"midi_events\":%llu,\"music_blocks\":%llu,\"zero_music_blocks\":%llu,"
           "\"nonfinite\":%llu,\"clipped\":%llu,\"xruns\":%d,\"errors\":%d,"
           "\"interrupted\":%s}\n", error,
           (unsigned long long)atomic_load(&g_capture_frames),
           (unsigned long long)atomic_load(&g_midi_sent),
           (unsigned long long)atomic_load(&g_music_blocks),
           (unsigned long long)atomic_load(&g_zero_music_blocks),
           (unsigned long long)atomic_load(&g_total_nonfinite),
           (unsigned long long)atomic_load(&g_total_clipped), atomic_load(&g_xruns),
           atomic_load(&g_callback_errors) + atomic_load(&g_midi_failures),
           g_interrupted ? "true" : "false");
    free(g_events); return 1;
}
