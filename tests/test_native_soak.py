import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/native_soak.c"
HEADERS = ROOT / "tests/native/audition_jack"

GOOD_PHRASE = """\
loop_seconds 5
0.000 b0 64 127
0.100 90 60 80
1.000 80 60 0
1.100 b0 64 0
"""


HARNESS = r'''
#define main native_soak_entry
#include "NATIVE_SOAK_SOURCE"
#undef main

struct jack_client { int id; };
struct jack_port { int id; };
static struct jack_client fake_client;
static struct jack_port midi_port = {1}, left_port = {2}, right_port = {3};
static unsigned char midi_bytes[32768][3];
static jack_nframes_t midi_offsets[32768];
static size_t midi_count;
static float left_audio[REQUIRED_BLOCK], right_audio[REQUIRED_BLOCK];

jack_client_t *jack_client_open(const char *n, unsigned o, jack_status_t *s)
{ (void)n; (void)o; (void)s; return &fake_client; }
int jack_client_close(jack_client_t *c) { (void)c; return 0; }
const char *jack_get_client_name(jack_client_t *c) { (void)c; return "fake"; }
jack_nframes_t jack_get_sample_rate(jack_client_t *c) { (void)c; return REQUIRED_RATE; }
jack_nframes_t jack_get_buffer_size(jack_client_t *c) { (void)c; return REQUIRED_BLOCK; }
jack_port_t *jack_port_register(jack_client_t *c, const char *n, const char *t,
                               unsigned long f, unsigned long z)
{ (void)c; (void)n; (void)t; (void)f; (void)z; return &midi_port; }
jack_port_t *jack_port_by_name(jack_client_t *c, const char *n)
{ (void)c; (void)n; return &midi_port; }
const char *jack_port_name(const jack_port_t *p) { (void)p; return "fake"; }
const char *jack_port_type(const jack_port_t *p) { (void)p; return JACK_DEFAULT_AUDIO_TYPE; }
unsigned long jack_port_flags(const jack_port_t *p) { (void)p; return JackPortIsInput; }
int jack_port_connected(const jack_port_t *p) { (void)p; return 0; }
int jack_port_connected_to(const jack_port_t *p, const char *n) { (void)p; (void)n; return 0; }
const char **jack_port_get_all_connections(const jack_client_t *c, const jack_port_t *p)
{ (void)c; (void)p; return NULL; }
const char **jack_get_ports(jack_client_t *c, const char *a, const char *b, unsigned long f)
{ (void)c; (void)a; (void)b; (void)f; return NULL; }
void jack_free(void *p) { (void)p; }
void *jack_port_get_buffer(jack_port_t *p, jack_nframes_t n)
{ (void)n; return p == NULL ? NULL : p == &midi_port ? (void *)&midi_port : p == &left_port ? left_audio : right_audio; }
int jack_set_process_callback(jack_client_t *c, int (*f)(jack_nframes_t, void *), void *a)
{ (void)c; (void)f; (void)a; return 0; }
int jack_set_xrun_callback(jack_client_t *c, int (*f)(void *), void *a)
{ (void)c; (void)f; (void)a; return 0; }
int jack_set_buffer_size_callback(jack_client_t *c, int (*f)(jack_nframes_t, void *), void *a)
{ (void)c; (void)f; (void)a; return 0; }
int jack_set_sample_rate_callback(jack_client_t *c, int (*f)(jack_nframes_t, void *), void *a)
{ (void)c; (void)f; (void)a; return 0; }
int jack_set_port_connect_callback(jack_client_t *c,
        void (*f)(jack_port_id_t, jack_port_id_t, int, void *), void *a)
{ (void)c; (void)f; (void)a; return 0; }
int jack_activate(jack_client_t *c) { (void)c; return 0; }
int jack_deactivate(jack_client_t *c) { (void)c; return 0; }
int jack_connect(jack_client_t *c, const char *a, const char *b)
{ (void)c; (void)a; (void)b; return 0; }
void jack_midi_clear_buffer(void *p) { (void)p; }
jack_midi_data_t *jack_midi_event_reserve(void *p, jack_nframes_t offset, size_t size)
{
    (void)p;
    if (size != 3 || midi_count >= 32768) return NULL;
    midi_offsets[midi_count] = offset;
    return midi_bytes[midi_count++];
}

static int callback_contract(const char *phrase) {
    char error[128] = "none";
    if (load_phrase(phrase, error, sizeof(error))) return 10;
    g_midi_out = &midi_port;
    g_capture_l = &left_port;
    g_capture_r = &right_port;
    g_requested_frames = 2 * g_loop_frames;
    atomic_store(&g_capture_stop_frame, g_requested_frames
                 + (TERMINAL_GRACE_SECONDS + TAIL_SECONDS) * REQUIRED_RATE);
    atomic_store(&g_armed, 1);
    while (atomic_load(&g_midi_frames) < g_requested_frames)
        midi_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_phrase_loops) != 2) return 11;
    for (size_t i = 0; i < midi_count; i++)
        if ((midi_bytes[i][0] & 0xf0u) == 0xb0u && midi_bytes[i][1] == 123u) return 12;
    size_t phrase_count = midi_count;
    midi_process(REQUIRED_BLOCK, NULL);
    if (!atomic_load(&g_music_complete) || midi_count != phrase_count) return 13;
    atomic_store(&g_release_authorized, 1);
    midi_process(REQUIRED_BLOCK, NULL);
    if (midi_count != phrase_count + 3 || midi_bytes[midi_count - 3][1] != 66
            || midi_bytes[midi_count - 2][1] != 64 || midi_bytes[midi_count - 1][1] != 123)
        return 14;
    midi_process(REQUIRED_BLOCK, NULL);
    if (midi_count != phrase_count + 3) return 15;

    for (unsigned i = 0; i < REQUIRED_BLOCK; i++) left_audio[i] = right_audio[i] = 0.25f;
    atomic_store(&g_active_notes, 1);
    atomic_store(&g_capture_frames, 0);
    capture_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_total_zero_active_blocks) != 0 || g_total_peak != 0.25) return 16;
    for (unsigned i = 0; i < REQUIRED_BLOCK; i++) left_audio[i] = right_audio[i] = 0.0f;
    capture_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_total_zero_active_blocks) != 1) return 17;
    left_audio[0] = NAN;
    capture_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_total_nonfinite) != 1) return 18;
    left_audio[0] = 1.0f;
    capture_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_total_clipped) != 1) return 20;
    int before = atomic_load(&g_callback_errors);
    on_port_connect(1, 2, 1, NULL);
    on_buffer_size(256, NULL);
    on_sample_rate(44100, NULL);
    on_xrun(NULL);
    if (atomic_load(&g_callback_errors) != before + 3 || atomic_load(&g_xruns) != 1)
        return 19;
    if (!is_eight_hour_qualification(28800.0, 0)
            || is_eight_hour_qualification(600.0, 0)
            || is_eight_hour_qualification(28800.0, 1)) return 21;
    g_interrupted = 1;
    if (strcmp(completion_failure(1, atomic_load(&g_capture_stop_frame)), "interrupted"))
        return 22;
    g_interrupted = 0;
    g_summary_index = MAX_SUMMARIES;
    g_summary_frames = 1;
    before = atomic_load(&g_callback_errors);
    publish_summary();
    if (g_summary_index != MAX_SUMMARIES || atomic_load(&g_callback_errors) != before + 1)
        return 23;
    g_midi_out = NULL;
    before = atomic_load(&g_callback_errors);
    midi_process(REQUIRED_BLOCK, NULL);
    if (atomic_load(&g_callback_errors) != before + 1) return 24;
    return 0;
}

static int grace_contract(void) {
    g_midi_out = &midi_port;
    g_requested_frames = UINT64_C(20) * REQUIRED_RATE;
    uint64_t initial_stop = g_requested_frames
        + (uint64_t)(TERMINAL_GRACE_SECONDS + TAIL_SECONDS) * REQUIRED_RATE;
    atomic_store(&g_capture_stop_frame, initial_stop);
    atomic_store(&g_capture_frames, g_requested_frames + UINT64_C(2) * REQUIRED_RATE);
    if (atomic_load(&g_capture_frames) >= atomic_load(&g_capture_stop_frame)) return 30;
    uint64_t marker = UINT64_C(1000000000);
    uint64_t deadline = marker + (uint64_t)TERMINAL_GRACE_SECONDS * UINT64_C(1000000000);
    if (grace_has_elapsed(deadline - 1, deadline) || !grace_has_elapsed(deadline, deadline))
        return 31;
    atomic_store(&g_armed, 1);
    atomic_store(&g_midi_frames, g_requested_frames);
    atomic_store(&g_music_complete, 1);
    atomic_store(&g_release_authorized, 1);
    atomic_store(&g_capture_frames,
                 g_requested_frames + (uint64_t)TERMINAL_GRACE_SECONDS * REQUIRED_RATE);
    midi_process(REQUIRED_BLOCK, NULL);
    uint64_t exact_stop = g_requested_frames
        + (uint64_t)(TERMINAL_GRACE_SECONDS + TAIL_SECONDS) * REQUIRED_RATE;
    if (!atomic_load(&g_terminal_release_sent)
            || atomic_load(&g_capture_stop_frame) != exact_stop) return 32;
    atomic_store(&g_capture_frames, exact_stop);
    if (completion_failure(1, exact_stop) != NULL) return 33;
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "--callback-contract"))
        return callback_contract(argv[2]);
    if (argc == 2 && !strcmp(argv[1], "--grace-contract"))
        return grace_contract();
    return native_soak_entry(argc, argv);
}
'''


class NativeSoakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if compiler is None:
            raise unittest.SkipTest("C compiler unavailable")
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        cls.phrase = directory / "phrase.txt"
        cls.phrase.write_text(GOOD_PHRASE, encoding="ascii")
        harness = directory / "harness.c"
        harness.write_text(
            HARNESS.replace("NATIVE_SOAK_SOURCE", str(SOURCE)), encoding="utf-8")
        cls.binary = directory / "native_soak_harness"
        subprocess.run([
            compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-I", str(HEADERS), str(harness), "-lm", "-o", str(cls.binary),
        ], check=True, timeout=15, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_strict_syntax_against_jack_api(self):
        subprocess.run([
            shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsyntax-only", "-I", str(HEADERS), str(SOURCE),
        ], check=True, timeout=10, capture_output=True, text=True)

    def test_phrase_validation_and_duration_modes_are_fail_closed(self):
        result = subprocess.run(
            [str(self.binary), "--validate", str(self.phrase), "20"],
            check=True, timeout=5, capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["event"], "soak_phrase_valid")
        self.assertEqual(report["events"], 4)
        self.assertFalse(report["qualification"])

        bad = Path(self.temp.name) / "bad.txt"
        bad.write_text(GOOD_PHRASE + "2.0 b0 123 0\n", encoding="ascii")
        rejected = subprocess.run(
            [str(self.binary), "--validate", str(bad), "20"],
            timeout=5, capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("phrase_forbids_loop_panic", rejected.stderr)
        too_short = subprocess.run(
            [str(self.binary), "--validate", str(self.phrase), "19.9"],
            timeout=5, capture_output=True, text=True)
        self.assertEqual(too_short.returncode, 2)

        rounded = Path(self.temp.name) / "rounded.txt"
        rounded.write_text(GOOD_PHRASE + "4.999999 b0 1 0\n", encoding="ascii")
        rejected = subprocess.run(
            [str(self.binary), "--validate", str(rounded), "20"],
            timeout=5, capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("phrase_event_rounds_past_loop", rejected.stderr)

        channel = Path(self.temp.name) / "channel.txt"
        channel.write_text(GOOD_PHRASE.replace("90 60", "91 60"), encoding="ascii")
        rejected = subprocess.run(
            [str(self.binary), "--validate", str(channel), "20"],
            timeout=5, capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("phrase_requires_channel_zero", rejected.stderr)

    def test_fast_fake_jack_loop_release_audio_and_latches(self):
        subprocess.run(
            [str(self.binary), "--callback-contract", str(self.phrase)],
            check=True, timeout=5, capture_output=True, text=True)

    def test_accelerated_grace_release_tail_and_final_accounting(self):
        subprocess.run(
            [str(self.binary), "--grace-contract"],
            check=True, timeout=5, capture_output=True, text=True)

    def test_callbacks_have_no_heap_io_locks_or_graph_mutation(self):
        source = SOURCE.read_text()
        callbacks = source[source.index("static int midi_process("):
                           source.index("static int on_xrun(")]
        for forbidden in (
            "malloc(", "calloc(", "realloc(", "free(", "fopen(", "fwrite(",
            "printf(", "fprintf(", "fflush(", "nanosleep(", "jack_connect(",
            "pthread_mutex", " mtx_", " sleep(",
        ):
            self.assertNotIn(forbidden, callbacks)
        self.assertIn("AudioSummary g_summaries[MAX_SUMMARIES]", source)
        self.assertIn("MAX_SUMMARIES 481u", source)
        self.assertIn('if (g_interrupted) return "interrupted"', source)
        self.assertIn("terminal_grace_seconds", source)
        self.assertIn("duration == MAX_SECONDS", source)
        self.assertIn('TARGET_CLIENT "StaveSynth_audition"', source)
        self.assertIn("JackNoStartServer | JackUseExactName", source)
        self.assertNotIn("system(", source)


if __name__ == "__main__":
    unittest.main()
