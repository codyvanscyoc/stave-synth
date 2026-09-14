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
#include <time.h>
#include <math.h>
#include <stdatomic.h>

/* ── Audio ring buffer ──
 * Fixed number of slots, each holding one JACK block.
 * Writer (Python) advances write_pos, reader (JACK callback) advances read_pos.
 * Lock-free: single producer, single consumer.
 *
 * Ring depth is runtime-switchable between Low Latency (6 slots) and Normal
 * (16 slots) via bridge_set_ring_slots(). Arrays are sized to RING_SLOTS_MAX
 * so the upper bound is fixed; g_active_slots controls the live modulo.
 */

#define MAX_BLOCK        2048   /* max samples per JACK block */
#define RING_SLOTS_MAX   16     /* compile-time array size — max allowed depth */

static float ring_l[RING_SLOTS_MAX][MAX_BLOCK];
static float ring_r[RING_SLOTS_MAX][MAX_BLOCK];
/* ring_read / ring_write are accessed via __atomic_{load,store}_n with
 * ACQUIRE/RELEASE semantics. No `volatile` — atomics imply the ordering we
 * need and volatile adds nothing. On aarch64 the relaxed memory model lets
 * plain loads of ring_write hoist *above* subsequent slot[] reads, which
 * could return stale samples. ACQUIRE on the reader pairs with RELEASE on
 * the writer so slot data is guaranteed visible when the index is. */
static uint32_t ring_read  = 0;
static uint32_t ring_write = 0;
static volatile uint32_t ring_block_size = 512;

/* Runtime-variable ring depth. Default matches NORMAL mode (16 slots).
 * Switched by bridge_set_ring_slots(). Readers ACQUIRE-load this and use
 * the snapshotted value for modulo math; the setter gates changes behind
 * g_transition_flag so producer/consumer don't race with a reset. */
static volatile uint32_t g_active_slots    = 16;
static volatile uint32_t g_transition_flag = 0;
static uint32_t g_writer_active            = 0;
static uint32_t g_callback_active          = 0;

/* The graph size/rate are fixed for one process lifetime. Changing either
 * requires rebuilding Python/native DSP state, so callbacks latch an error
 * and the audio callback fails silent until the supervisor restarts us. */
static volatile uint32_t graph_block_size  = 0;
static volatile uint32_t graph_sample_rate = 0;
static uint32_t graph_error_flags          = 0;
#define GRAPH_ERROR_BUFFER_SIZE 1u
#define GRAPH_ERROR_SAMPLE_RATE 2u

/* ── MIDI ring buffer ── */
#define MIDI_RING_SIZE 512

typedef struct {
    uint8_t  data[4];
    uint32_t size;
} midi_event_t;

static midi_event_t midi_ring[MIDI_RING_SIZE];
static uint32_t midi_read  = 0;
static uint32_t midi_write = 0;
static uint32_t midi_recovery_required = 0;

/* ── JACK state ── */
static jack_client_t *client     = NULL;
static jack_port_t   *port_out_l = NULL;
static jack_port_t   *port_out_r = NULL;
static jack_port_t   *port_midi  = NULL;

static _Atomic float master_volume = 0.85f;
static float          master_volume_smooth = 0.85f; /* smoothed value for zipper-free changes */
static volatile int   btl_mode      = 0;     /* 0 = normal stereo, 1 = invert R */

/* ── Stats ── */
static volatile uint32_t stat_callbacks   = 0;
static volatile uint32_t stat_underruns   = 0;
static volatile uint32_t stat_xruns       = 0;
static volatile uint32_t stat_midi_events = 0;
static uint32_t          stat_midi_dropped = 0;
static uint32_t          stat_midi_recoveries = 0;
static volatile float    stat_peak        = 0.0f;

/* ── JACK shutdown flag ── */
static volatile int jack_shutdown_flag = 0;

/* ── Helpers ──
 *
 * ring_readable_acq is called by the JACK callback (consumer); it needs an
 * ACQUIRE load of ring_write so subsequent slot reads see fresh data. The
 * matching RELEASE store happens in the producer's bridge_write_* after the
 * memcpy completes.
 *
 * ring_writable_acq is called by the Python producer; it needs an ACQUIRE
 * load of ring_read so the producer knows when a slot has really been
 * consumed before it overwrites it. The matching RELEASE store happens in
 * process_callback after the slot has been drained.
 */
static inline uint32_t ring_readable_acq(void) {
    uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_ACQUIRE);
    uint32_t w = __atomic_load_n(&ring_write, __ATOMIC_ACQUIRE);
    uint32_t r = __atomic_load_n(&ring_read, __ATOMIC_RELAXED);
    return (w >= r) ? (w - r) : (slots - r + w);
}

static inline uint32_t ring_writable_acq(void) {
    uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_ACQUIRE);
    uint32_t w = __atomic_load_n(&ring_write, __ATOMIC_RELAXED);
    uint32_t r = __atomic_load_n(&ring_read, __ATOMIC_ACQUIRE);
    uint32_t readable = (w >= r) ? (w - r) : (slots - r + w);
    return slots - 1 - readable;
}

/* Read-side helper for stats and diagnostics — relaxed everywhere, no
 * ordering requirement against data. Used by bridge_get_ring_fill. */
static inline uint32_t ring_readable_relaxed(void) {
    uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_RELAXED);
    uint32_t w = __atomic_load_n(&ring_write, __ATOMIC_RELAXED);
    uint32_t r = __atomic_load_n(&ring_read, __ATOMIC_RELAXED);
    return (w >= r) ? (w - r) : (slots - r + w);
}

static void mark_graph_error(uint32_t flag) {
    __atomic_fetch_or(&graph_error_flags, flag, __ATOMIC_RELEASE);
}

/* Enter a ring transition from a non-RT control thread. The transition gate
 * prevents new ring critical sections; the counters acknowledge that earlier
 * producer/callback sections have actually left. Timeout aborts unchanged. */
static int begin_ring_transition(void) {
    uint32_t expected = 0;
    if (!__atomic_compare_exchange_n(&g_transition_flag, &expected, 1, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        return -2;
    }

    struct timespec start, now;
    const struct timespec pause = {0, 500000L}; /* 0.5 ms polling, non-RT only */
    clock_gettime(CLOCK_MONOTONIC, &start);
    while (__atomic_load_n(&g_writer_active, __ATOMIC_ACQUIRE) != 0 ||
           __atomic_load_n(&g_callback_active, __ATOMIC_ACQUIRE) != 0) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        int64_t elapsed_ns = (int64_t)(now.tv_sec - start.tv_sec) * 1000000000LL
                           + (int64_t)(now.tv_nsec - start.tv_nsec);
        if (elapsed_ns >= 250000000LL) {
            __atomic_store_n(&g_transition_flag, 0, __ATOMIC_RELEASE);
            return -3;
        }
        nanosleep(&pause, NULL);
    }
    return 0;
}

static void end_ring_transition(void) {
    __atomic_store_n(&g_transition_flag, 0, __ATOMIC_RELEASE);
}

/* ── JACK process callback ── */

static int process_callback(jack_nframes_t nframes, void *arg) {
    (void)arg;

    float *out_l = (float *)jack_port_get_buffer(port_out_l, nframes);
    float *out_r = (float *)jack_port_get_buffer(port_out_r, nframes);

    /* A changed graph cannot consume fixed-size producer slots safely. Fail
     * silent until restart, but continue accepting MIDI releases. */
    uint32_t expected_frames = __atomic_load_n(&graph_block_size, __ATOMIC_ACQUIRE);
    if (nframes != expected_frames || nframes > MAX_BLOCK) {
        mark_graph_error(GRAPH_ERROR_BUFFER_SIZE);
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        goto midi_in;
    }
    if (__atomic_load_n(&graph_error_flags, __ATOMIC_ACQUIRE) != 0) {
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        goto midi_in;
    }

    /* During a ring transition, output silence so we don't race with reset.
     * The double-check after increment closes the gate/counter race. */
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) {
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        goto midi_in;
    }
    __atomic_add_fetch(&g_callback_active, 1, __ATOMIC_ACQ_REL);
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) {
        __atomic_sub_fetch(&g_callback_active, 1, __ATOMIC_RELEASE);
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        goto midi_in;
    }

    if (ring_readable_acq() > 0 && nframes <= MAX_BLOCK) {
        uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_ACQUIRE);
        uint32_t r = __atomic_load_n(&ring_read, __ATOMIC_RELAXED);
        uint32_t slot = r % slots;
        const float *src_l = ring_l[slot];
        const float *src_r = ring_r[slot];
        float vol_target = atomic_load_explicit(&master_volume, memory_order_relaxed);
        if (!isfinite(vol_target)) vol_target = 0.0f;
        if (!isfinite(master_volume_smooth)) master_volume_smooth = 0.0f;
        /* Per-sample one-pole smoother: ~5ms at 48kHz (alpha ≈ 0.004) */
        float alpha = 1.0f - 0.99584f; /* exp(-1/(0.005*48000)) ≈ 0.99584 */
        float peak = 0.0f;

        for (jack_nframes_t i = 0; i < nframes; i++) {
            master_volume_smooth += alpha * (vol_target - master_volume_smooth);
            float l = src_l[i] * master_volume_smooth;
            float r = src_r[i] * master_volume_smooth;
            /* Final boundary: a faulty DSP value must never reach the DAC. */
            if (!isfinite(l)) l = 0.0f;
            if (!isfinite(r)) r = 0.0f;
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

        /* RELEASE: ensures the slot reads above are complete before the
         * producer sees the freed index. Producer's matching ACQUIRE is in
         * ring_writable_acq. */
        __atomic_store_n(&ring_read, (r + 1) % slots, __ATOMIC_RELEASE);
    } else {
        /* Underrun — output silence */
        memset(out_l, 0, nframes * sizeof(float));
        memset(out_r, 0, nframes * sizeof(float));
        stat_underruns++;
    }
    __atomic_sub_fetch(&g_callback_active, 1, __ATOMIC_RELEASE);

midi_in: ;
    /* ── MIDI input ── */
    void *midi_buf = jack_port_get_buffer(port_midi, nframes);
    uint32_t nevents = jack_midi_get_event_count(midi_buf);

    for (uint32_t i = 0; i < nevents; i++) {
        jack_midi_event_t ev;
        if (jack_midi_event_get(&ev, midi_buf, i) != 0) break;
        if (ev.size > 4) continue;

        if (__atomic_load_n(&midi_recovery_required, __ATOMIC_ACQUIRE)) {
            __atomic_add_fetch(&stat_midi_dropped, 1, __ATOMIC_RELAXED);
            continue;
        }
        uint32_t w = __atomic_load_n(&midi_write, __ATOMIC_RELAXED);
        uint32_t next = (w + 1) % MIDI_RING_SIZE;
        uint32_t r = __atomic_load_n(&midi_read, __ATOMIC_ACQUIRE);
        if (next == r) {
            __atomic_add_fetch(&stat_midi_dropped, 1, __ATOMIC_RELAXED);
            __atomic_store_n(&midi_recovery_required, 1, __ATOMIC_RELEASE);
            continue;
        }

        memcpy((void *)midi_ring[w].data, ev.buffer, ev.size);
        midi_ring[w].size = ev.size;
        /* RELEASE: event payload must be visible before Python sees the new
         * write index (matching ACQUIRE in bridge_read_midi). */
        __atomic_store_n(&midi_write, next, __ATOMIC_RELEASE);
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

static void shutdown_callback(void *arg) {
    (void)arg;
    jack_shutdown_flag = 1;
}

static int buffer_size_callback(jack_nframes_t nframes, void *arg) {
    (void)arg;
    if (nframes != __atomic_load_n(&graph_block_size, __ATOMIC_ACQUIRE))
        mark_graph_error(GRAPH_ERROR_BUFFER_SIZE);
    return 0;
}

static int sample_rate_callback(jack_nframes_t sample_rate, void *arg) {
    (void)arg;
    if (sample_rate != __atomic_load_n(&graph_sample_rate, __ATOMIC_ACQUIRE))
        mark_graph_error(GRAPH_ERROR_SAMPLE_RATE);
    return 0;
}

/* ── Public API ── */

int bridge_start_named(const char *name, int auto_connect) {
    if (!name || !name[0]) return -4;
    if (client) return -5;
    port_out_l = port_out_r = port_midi = NULL;
    jack_status_t status;
    client = jack_client_open(name, JackNoStartServer | JackUseExactName, &status);
    if (!client) return -1;

    port_out_l = jack_port_register(client, "out_L",  JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    port_out_r = jack_port_register(client, "out_R",  JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    port_midi  = jack_port_register(client, "midi_in", JACK_DEFAULT_MIDI_TYPE,  JackPortIsInput, 0);
    if (!port_out_l || !port_out_r || !port_midi) {
        jack_client_close(client);
        client = NULL;
        port_out_l = port_out_r = port_midi = NULL;
        return -2;
    }

    ring_block_size = jack_get_buffer_size(client);
    uint32_t sample_rate = jack_get_sample_rate(client);
    if (ring_block_size == 0 || ring_block_size > MAX_BLOCK || sample_rate == 0) {
        jack_client_close(client);
        client = NULL;
        port_out_l = port_out_r = port_midi = NULL;
        return -6;
    }
    __atomic_store_n(&graph_block_size, ring_block_size, __ATOMIC_RELEASE);
    __atomic_store_n(&graph_sample_rate, sample_rate, __ATOMIC_RELEASE);
    __atomic_store_n(&graph_error_flags, 0, __ATOMIC_RELEASE);

    /* Zero the ring buffers */
    memset(ring_l, 0, sizeof(ring_l));
    memset(ring_r, 0, sizeof(ring_r));
    ring_read = ring_write = 0;
    midi_read = midi_write = 0;
    __atomic_store_n(&midi_recovery_required, 0, __ATOMIC_RELEASE);

    jack_set_process_callback(client, process_callback, NULL);
    if (jack_set_buffer_size_callback(client, buffer_size_callback, NULL) != 0 ||
        jack_set_sample_rate_callback(client, sample_rate_callback, NULL) != 0) {
        jack_client_close(client);
        client = NULL;
        port_out_l = port_out_r = port_midi = NULL;
        return -7;
    }
    jack_set_xrun_callback(client, xrun_callback, NULL);
    jack_on_shutdown(client, shutdown_callback, NULL);

    __atomic_store_n(&stat_midi_dropped, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&stat_midi_recoveries, 0, __ATOMIC_RELAXED);
    jack_shutdown_flag = 0;

    if (jack_activate(client) != 0) {
        jack_client_close(client);
        client = NULL;
        port_out_l = port_out_r = port_midi = NULL;
        return -3;
    }

    /* Auto-connect audio */
    const char **playback = auto_connect ? jack_get_ports(client, NULL, JACK_DEFAULT_AUDIO_TYPE,
                                           JackPortIsPhysical | JackPortIsInput) : NULL;
    if (playback) {
        if (playback[0]) jack_connect(client, jack_port_name(port_out_l), playback[0]);
        if (playback[1]) jack_connect(client, jack_port_name(port_out_r), playback[1]);
        jack_free(playback);
    }

    return 0;
}

/* Legacy callers retain the stage identity; duplicate names now fail closed. */
int bridge_start(void) {
    return bridge_start_named("StaveSynth", 1);
}

void bridge_stop(void) {
    if (client) {
        jack_deactivate(client);
        jack_client_close(client);
        client = NULL;
        port_out_l = port_out_r = port_midi = NULL;
    }
}

/* Push one mono block into the ring buffer (duplicated to both channels).
 * Returns 1 on success, 0 if ring is full (caller should wait). */
int bridge_write_audio(const float *samples, int nframes) {
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) return 0;
    __atomic_add_fetch(&g_writer_active, 1, __ATOMIC_ACQ_REL);
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return 0;
    }
    if (nframes <= 0 || (uint32_t)nframes !=
            __atomic_load_n(&graph_block_size, __ATOMIC_ACQUIRE) ||
            __atomic_load_n(&graph_error_flags, __ATOMIC_ACQUIRE) != 0) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return -1;
    }
    if (ring_writable_acq() == 0) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return 0;  /* full */
    }

    uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_ACQUIRE);
    uint32_t w = __atomic_load_n(&ring_write, __ATOMIC_RELAXED);
    uint32_t slot = w % slots;
    memcpy(ring_l[slot], samples, nframes * sizeof(float));
    memcpy(ring_r[slot], samples, nframes * sizeof(float));
    /* RELEASE: audio payload must be visible before the JACK callback sees
     * the new write index (matching ACQUIRE in ring_readable_acq). */
    __atomic_store_n(&ring_write, (w + 1) % slots, __ATOMIC_RELEASE);
    __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
    return 1;
}

/* Push one stereo block (separate L/R arrays) into the ring buffer.
 * Returns 1 on success, 0 if ring is full (caller should wait). */
int bridge_write_stereo(const float *left, const float *right, int nframes) {
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) return 0;
    __atomic_add_fetch(&g_writer_active, 1, __ATOMIC_ACQ_REL);
    if (__atomic_load_n(&g_transition_flag, __ATOMIC_ACQUIRE)) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return 0;
    }
    if (nframes <= 0 || (uint32_t)nframes !=
            __atomic_load_n(&graph_block_size, __ATOMIC_ACQUIRE) ||
            __atomic_load_n(&graph_error_flags, __ATOMIC_ACQUIRE) != 0) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return -1;
    }
    if (ring_writable_acq() == 0) {
        __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
        return 0;  /* full */
    }

    uint32_t slots = __atomic_load_n(&g_active_slots, __ATOMIC_ACQUIRE);
    uint32_t w = __atomic_load_n(&ring_write, __ATOMIC_RELAXED);
    uint32_t slot = w % slots;
    memcpy(ring_l[slot], left, nframes * sizeof(float));
    memcpy(ring_r[slot], right, nframes * sizeof(float));
    __atomic_store_n(&ring_write, (w + 1) % slots, __ATOMIC_RELEASE);
    __atomic_sub_fetch(&g_writer_active, 1, __ATOMIC_RELEASE);
    return 1;
}

void bridge_set_master_volume(float vol) {
    if (!isfinite(vol)) vol = 0.0f;
    if (vol < 0.0f) vol = 0.0f;
    if (vol > 1.0f) vol = 1.0f;
    atomic_store_explicit(&master_volume, vol, memory_order_relaxed);
}

void bridge_set_btl_mode(int enabled) {
    btl_mode = enabled;
}

/* Switch active ring depth after explicit producer/callback quiescence. */
int bridge_set_ring_slots(int slots) {
    if (slots < 4 || slots > RING_SLOTS_MAX) return -1;
    int transition = begin_ring_transition();
    if (transition != 0) return transition;

    memset(ring_l, 0, sizeof(ring_l));
    memset(ring_r, 0, sizeof(ring_r));
    __atomic_store_n(&ring_read,  0, __ATOMIC_RELAXED);
    __atomic_store_n(&ring_write, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&g_active_slots, (uint32_t)slots, __ATOMIC_RELEASE);

    end_ring_transition();
    return 0;
}

int bridge_get_ring_slots(void) {
    return (int)__atomic_load_n(&g_active_slots, __ATOMIC_RELAXED);
}

/* Flush the ring without changing slot count, after explicit quiescence. */
int bridge_clear_ring(void) {
    int transition = begin_ring_transition();
    if (transition != 0) return transition;

    memset(ring_l, 0, sizeof(ring_l));
    memset(ring_r, 0, sizeof(ring_r));
    __atomic_store_n(&ring_read,  0, __ATOMIC_RELAXED);
    __atomic_store_n(&ring_write, 0, __ATOMIC_RELAXED);

    end_ring_transition();
    return 0;
}

/* Read one MIDI event. Returns byte count (0 = empty). */
int bridge_read_midi(uint8_t *out) {
    /* Any overflow makes event ordering ambiguous and could hide a release.
     * Discard that queued batch and deliver one global all-notes-off before
     * later traffic. This favors bounded silence over a stuck stage note. */
    if (__atomic_exchange_n(&midi_recovery_required, 0, __ATOMIC_ACQ_REL)) {
        uint32_t w = __atomic_load_n(&midi_write, __ATOMIC_ACQUIRE);
        __atomic_store_n(&midi_read, w, __ATOMIC_RELEASE);
        out[0] = 0xB0;
        out[1] = 123;
        out[2] = 0;
        __atomic_add_fetch(&stat_midi_recoveries, 1, __ATOMIC_RELAXED);
        return 3;
    }
    uint32_t r = __atomic_load_n(&midi_read, __ATOMIC_RELAXED);
    /* ACQUIRE pairs with the RELEASE in process_callback's MIDI producer so
     * the event payload is visible before we read it. */
    uint32_t w = __atomic_load_n(&midi_write, __ATOMIC_ACQUIRE);
    if (r == w) return 0;
    uint32_t sz = midi_ring[r].size;
    memcpy(out, (void *)midi_ring[r].data, sz);
    /* RELEASE: ensures the consumer's reads complete before the slot is
     * reported as free to the producer. */
    __atomic_store_n(&midi_read, (r + 1) % MIDI_RING_SIZE, __ATOMIC_RELEASE);
    return (int)sz;
}

/* Queries */
/* Order-2 IIR (biquad), direct-form II transposed — the exact algorithm
 * scipy.signal.lfilter runs for a 3/3-coefficient filter with 2-state zi.
 * Output matches scipy within double rounding (measured max delta ~1e-14
 * ≈ -270 dB — scipy's compiled loop associates the arithmetic slightly
 * differently). The Python Biquad* classes call this instead of lfilter:
 * scipy's per-call overhead on 512-sample blocks, times dozens of biquads
 * per block, was ~8-10%% of the small-Pi render budget. zi is updated in
 * place. Render-thread only — no locking. */
void bridge_biquad(const double *x, double *y, int n,
                   const double *b, const double *a, double *zi) {
    double z0 = zi[0], z1 = zi[1];
    const double b0 = b[0], b1 = b[1], b2 = b[2];
    const double a1 = a[1], a2 = a[2];
    for (int i = 0; i < n; i++) {
        double xi = x[i];
        double yi = b0 * xi + z0;
        z0 = z1 + b1 * xi - a1 * yi;   /* scipy's exact op order */
        z1 = b2 * xi - a2 * yi;
        y[i] = yi;
    }
    zi[0] = z0; zi[1] = z1;
}

/* Order-1 IIR, same scipy-lfilter semantics: b = [b0, b1] (b1 may be 0),
 * a = [1, a1], 1-state zi. Covers both OnePole6dB* classes (lowpass has
 * b1 = 0; highpass uses the zero at b1). scipy op order for bit-parity. */
void bridge_onepole(const double *x, double *y, int n,
                    double b0, double b1, double a1, double *zi) {
    double z0 = zi[0];
    for (int i = 0; i < n; i++) {
        double xi = x[i];
        double yi = b0 * xi + z0;
        z0 = b1 * xi - a1 * yi;
        y[i] = yi;
    }
    zi[0] = z0;
}

/* Lookahead-limiter gain envelope: instant attack, exponential release.
 * Exact port of the per-sample Python loop formerly in jack_engine.py's
 * LookaheadLimiter.process_inplace — that loop ran 48k iterations/sec of
 * interpreted Python on the render thread (~1/3 of the render budget on a
 * Pi 4). Bit-identical math, called via ctypes; returns the final gain so
 * Python can carry state across blocks. Not called from the JACK RT
 * callback — render-thread only, no locking needed. */
double bridge_limiter_env(const double *target, double *env, int n,
                          double gain, double rel) {
    if (!isfinite(gain) || gain < 0.0 || gain > 1.0) gain = 0.0;
    if (!isfinite(rel) || rel < 0.0 || rel > 1.0) rel = 0.999;
    double one_m_rel = 1.0 - rel;
    for (int i = 0; i < n; i++) {
        double t = target[i];
        if (!isfinite(t) || t < 0.0) t = 0.0;
        if (t > 1.0) t = 1.0;
        if (t < gain) gain = t;
        else gain = rel * gain + one_m_rel;
        env[i] = gain;
    }
    return gain;
}

int   bridge_get_sample_rate(void)    { return client ? (int)jack_get_sample_rate(client) : 0; }
int   bridge_get_buffer_size(void)    { return client ? (int)jack_get_buffer_size(client) : 0; }
int   bridge_get_callback_count(void) { return (int)stat_callbacks; }
float bridge_get_peak_output(void)    { return stat_peak; }
int   bridge_get_xrun_count(void)     { return (int)stat_xruns; }
int   bridge_get_underrun_count(void) { return (int)stat_underruns; }
int   bridge_get_midi_event_count(void) { return (int)stat_midi_events; }
int   bridge_get_midi_drop_count(void) { return (int)__atomic_load_n(&stat_midi_dropped, __ATOMIC_RELAXED); }
int   bridge_get_midi_recovery_count(void) { return (int)__atomic_load_n(&stat_midi_recoveries, __ATOMIC_RELAXED); }
int   bridge_get_ring_fill(void)      { return (int)ring_readable_relaxed(); }
int   bridge_get_btl_mode(void)       { return btl_mode; }
int   bridge_is_shutdown(void)        { return jack_shutdown_flag; }
int   bridge_get_graph_error(void)    { return (int)__atomic_load_n(&graph_error_flags, __ATOMIC_ACQUIRE); }
