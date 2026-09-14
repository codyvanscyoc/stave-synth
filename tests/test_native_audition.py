import shutil
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/native_audition.c"


class NativeAuditionSourceTests(unittest.TestCase):
    def test_strict_compiler_accepts_driver_against_jack_api(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        subprocess.run([
            compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsyntax-only", "-I", str(ROOT / "tests/native/audition_jack"),
            str(SOURCE),
        ], check=True, timeout=10, capture_output=True, text=True)

    def test_driver_is_fail_closed_and_callback_has_no_forbidden_work(self):
        source = SOURCE.read_text()
        self.assertIn('TARGET_CLIENT "StaveSynth_audition"', source)
        self.assertIn('CAPTURE_CLIENT "StaveCapture_audition"', source)
        self.assertIn('MIDI_CLIENT "StaveMidi_audition"', source)
        self.assertIn("JackNoStartServer | JackUseExactName", source)
        self.assertIn("capture_started", source)
        self.assertIn("capture_complete", source)
        self.assertIn("O_EXCL", source)
        self.assertIn("volatile float *prefault_l", source)
        self.assertIn("jack_set_port_connect_callback", source)
        self.assertNotIn("jack_port_connected(target_", source)

        callback = source[source.index("static int midi_process("):
                          source.index("static int on_xrun(")]
        for forbidden in ("malloc(", "calloc(", "fopen(", "fwrite(",
                          "printf(", "fprintf(", "jack_connect("):
            self.assertNotIn(forbidden, callback)


if __name__ == "__main__":
    unittest.main()
