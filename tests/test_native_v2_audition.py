"""Only isolated control/HTTP contracts: fake child, ephemeral loopback listener.

Never starts JACK, loads SF2, imports runtime or contacts the Pi.
"""
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_v2_audition", ROOT / "tools/native_v2_audition.py")
audition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audition)


class AuditionControlTests(unittest.TestCase):
    def controller(self):
        child = mock.Mock()
        child.poll.return_value = None
        c = audition.Controller(child)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 0, "routed": True, "blocks": 1})
        return c

    def test_untrusted_controls_rejected_before_queue(self):
        c = self.controller()
        for data in (None, [], {}, {"key": "piano", "value": True}, {"key": [], "value": 0},
                     {"key": "master", "value": float("nan")}, {"key": "master", "value": float("inf")},
                     {"key": "master", "value": 10**400}, {"key": "wave1", "value": 1.5},
                     {"key": "master", "value": .5, "extra": 1}, {"key": "drone", "value": 1}):
            with self.subTest(data=str(data)[:80]), self.assertRaises(ValueError):
                c.submit(data)
        self.assertEqual(c.sequence, 0)
        self.assertTrue(c.outgoing.empty())

    def test_pending_is_not_applied_and_rejection_not_replayed(self):
        c = self.controller()
        self.assertEqual(c.submit({"key": "master", "value": .5}), 1)
        self.assertEqual(c.snapshot()["values"]["master"], 0)
        c.receive({"type": "accepted", "id": 1, "ok": True})
        self.assertEqual(c.snapshot()["pending"], 1)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 1, "routed": True})
        self.assertEqual(c.snapshot()["values"]["master"], .5)
        c.submit({"key": "master", "value": .8})
        c.receive({"type": "accepted", "id": 2, "ok": False})
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 3})
        self.assertEqual(c.snapshot()["values"]["master"], .5)
        self.assertEqual(c.snapshot()["pending"], 0)

    def test_stale_failed_and_exited_owners_refuse_controls(self):
        for mode in ("stale", "fault", "exited", "stalled", "unrouted"):
            c = self.controller()
            if mode == "stale": c.last_status = time.monotonic() - 4
            if mode == "fault": c.status["fault"] = 2
            if mode == "exited": c.child.poll.return_value = 1
            if mode == "stalled": c.last_progress = time.monotonic() - 4
            if mode == "unrouted": c.status["routed"] = False
            with self.assertRaises(ValueError): c.submit({"key": "piano", "value": .4})

    def test_bounded_backlog(self):
        c = self.controller()
        for _ in range(96): c.submit({"key": "osc1", "value": .2})
        with self.assertRaises(ValueError): c.submit({"key": "osc1", "value": .3})
        self.assertEqual(len(c.pending), 96)
        self.assertEqual(c.outgoing.qsize(), 96)

    def test_recorded_keys_require_loaded_asset_and_authoritative_ack(self):
        c = self.controller()
        with self.assertRaises(ValueError):
            c.submit({"key": "bed_key", "value": 0})
        self.assertEqual(c.sequence, 0)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0,
                   "applied": 0, "routed": True, "blocks": 2, "bed_mask": 129, "bed_key": -1})
        for value in (1, .5, -1, 12, True):
            with self.assertRaises(ValueError):
                c.submit({"key": "bed_key", "value": value})
        c.submit({"key": "bed_key", "value": 7})
        self.assertEqual(c.values["bed_key"], -1)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0,
                   "applied": 1, "routed": True, "blocks": 3, "bed_mask": 129, "bed_key": 7})
        self.assertEqual(c.values["bed_key"], 7)
        c.submit({"key": "bed_release", "value": 1})
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0,
                   "applied": 2, "routed": True, "blocks": 4, "bed_mask": 129, "bed_key": -1})
        self.assertEqual(c.values["bed_key"], -1)

    def test_bed_controls_have_independent_bounded_ranges(self):
        for key, valid, invalid in (("bed_level", (0, .5, 1), (-1, 2)),
                                    ("bed_rise", (0, 2.5, 60), (-1, 61)),
                                    ("bed_rise_cutoff", (200, 3000, 20000), (0, 20001)),
                                    ("bed_mellow_cutoff", (100, 400, 8000), (99, 8001)),
                                    ("bed_mellow", (0, 1), (.5, 2)),
                                    ("bed_fade", (0, 1), (.5, 2))):
            for value in valid:
                self.assertEqual(audition.validate_control({"key": key, "value": value}), (key, value))
            for value in (*invalid, float("nan"), True):
                with self.assertRaises(ValueError):
                    audition.validate_control({"key": key, "value": value})

    def test_startup_prepares_private_bank_before_child_and_cleans_after_exit(self):
        from test_pad_preparation import wav_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); library = root / "pads"; library.mkdir()
            (library / "pad_C.wav").write_bytes(wav_bytes([0]*16))
            binary = root / "binary"; binary.touch()
            font = root / "font"; font.touch()
            child = mock.Mock(); child.poll.return_value = 0; child.returncode = 0
            child.stdout.readline.return_value = ""
            prepared = []
            def launch(argv, **_kwargs):
                bank = Path(argv[argv.index("--bed-bank")+1]); prepared.append(bank)
                self.assertEqual(bank.read_bytes()[:8], b"STVBANK1")
                self.assertNotEqual(bank.parent, library)
                return child
            args = ["audition", "--binary", str(binary), "--soundfont", str(font), "--listen", "127.0.0.1",
                    "--midi-source", "fake:midi", "--audio-left", "fake:l", "--audio-right", "fake:r",
                    "--allow-live-audition", "--pad-library", str(library)]
            with mock.patch("sys.argv", args), mock.patch.object(audition, "HTTPServer") as server, \
                 mock.patch.object(audition.subprocess, "Popen", side_effect=launch) as process:
                self.assertEqual(audition.main(), 0)
                process.assert_called_once(); server.return_value.server_close.assert_called_once()
            self.assertFalse(prepared[0].exists())
            self.assertEqual(list(library.iterdir()), [library / "pad_C.wav"])
            (library / "pad_C.wav").write_bytes(b"bad")
            with mock.patch("sys.argv", args), mock.patch.object(audition, "HTTPServer") as server, \
                 mock.patch.object(audition.subprocess, "Popen") as process:
                with self.assertRaises(ValueError): audition.main()
                process.assert_not_called(); server.return_value.server_close.assert_called_once()

    def test_piano_brightness_range_and_acknowledgment(self):
        c = self.controller()
        self.assertEqual(c.values["piano_tone"], 1)
        for value in (-.01, 1.01, float("nan"), True):
            with self.assertRaises(ValueError):
                c.submit({"key": "piano_tone", "value": value})
        c.submit({"key": "piano_tone", "value": .5})
        self.assertEqual(c.values["piano_tone"], 1)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0,
                   "applied": 1, "blocks": 2, "routed": True})
        self.assertEqual(c.values["piano_tone"], .5)
        self.assertEqual(c.values["cutoff"], 8000)  # independent synth filter

    def test_filter_sweep_bounds_are_ordered_and_clamp_authoritative_cutoff(self):
        prepared = {key: definition[3] for key, definition in audition.CONTROLS.items()}
        audition.validate_control_pair(prepared, "cutoff_min", 300)
        audition.apply_value(prepared, "cutoff_min", 300)
        audition.validate_control_pair(prepared, "cutoff_max", 4000)
        audition.apply_value(prepared, "cutoff_max", 4000)
        self.assertEqual((prepared["cutoff_min"], prepared["cutoff_max"], prepared["cutoff"]), (300, 4000, 4000))
        with self.assertRaises(ValueError):
            audition.validate_control_pair(prepared, "cutoff_min", 4000)
        with self.assertRaises(ValueError):
            audition.validate_control_pair(prepared, "cutoff_max", 300)

    def test_midi_learn_validation_queue_and_one_time_reconciliation(self):
        for bad in ({"key": "master", "cc": 64}, {"key": "release_all", "cc": 21}, {"key": "cutoff_min", "cc": 22},
                    {"key": "master", "cc": True}, {"key": "master", "cc": 128},
                    {"key": "missing", "cc": 21}, {"key": "master"}):
            with self.assertRaises(ValueError):
                audition.validate_midi_mapping(bad)
        self.assertEqual(audition.validate_midi_mapping({"key": "master", "cc": 21}), ("master", 21))
        self.assertEqual(audition.midi_value("cutoff", 0), 20)
        self.assertEqual(audition.midi_value("cutoff", 127), 20000)
        self.assertEqual(audition.midi_value("cutoff", 0, {"cutoff_min": 200, "cutoff_max": 5000}), 200)
        self.assertEqual(audition.midi_value("cutoff", 127, {"cutoff_min": 200, "cutoff_max": 5000}), 5000)
        fractional = {"cutoff_min": 400.5, "cutoff_max": 800.5}
        self.assertEqual(audition.midi_value("cutoff", 0, fractional), 401)
        self.assertEqual(audition.midi_value("cutoff", 127, fractional), 800.5)
        c = self.controller()
        sequence = c.map_cc({"key": "osc1", "cc": 21})
        self.assertEqual(c.outgoing.get_nowait(), ("map", sequence, 21, "osc1"))
        c.receive({"type": "mapped", "id": sequence, "ok": True})
        self.assertEqual(c.snapshot()["map_pending"], 0)
        raw = [-1] * len(audition.CONTROLS); serials = [0] * len(audition.CONTROLS)
        index = list(audition.CONTROLS).index("osc1"); raw[index] = 127; serials[index] = 1
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 0,
                   "routed": True, "blocks": 2, "midi_mapped_raw": raw, "midi_mapped_serial": serials})
        self.assertEqual(c.snapshot()["values"]["osc1"], 1)
        change = c.submit({"key": "osc1", "value": .2})
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": change,
                   "routed": True, "blocks": 3, "midi_mapped_raw": raw, "midi_mapped_serial": serials})
        self.assertEqual(c.snapshot()["values"]["osc1"], .2, "old MIDI telemetry must not overwrite a newer UI edit")

    def test_mapping_receipts_correlate_rejections_timeouts_and_disconnect(self):
        c = self.controller()
        first = c.clear_mapping("master")
        second = c.map_cc({"key": "osc1", "cc": 21})
        self.assertEqual(c.outgoing.get_nowait(), ("unmap", first, None, "master"))
        c.receive({"type": "mapped", "id": second, "ok": True})
        c.receive({"type": "mapped", "id": first, "ok": False})
        c.wait_mapping(second)
        with self.assertRaises(ValueError):
            c.wait_mapping(first)
        expired = c.clear_mapping("master")
        with self.assertRaises(ValueError):
            c.wait_mapping(expired, timeout=0)
        c.receive({"type": "mapped", "id": expired, "ok": True})
        self.assertNotIn(expired, c.map_receipts)
        lost = c.clear_mapping("osc2")
        c.child.poll.return_value = 1
        with self.assertRaises(ValueError):
            c.wait_mapping(lost)
        self.assertEqual(c.snapshot()["map_pending"], 0)

    def test_learning_ack_returns_the_native_cc_baseline(self):
        c = self.controller()
        for enabled in (True, False):
            seq = c.set_learning(enabled)
            self.assertEqual(c.outgoing.get_nowait(), ("learn", seq, int(enabled), None))
            c.receive({"type": "mapped", "id": seq, "ok": True, "midi_cc_serial": 27})
            self.assertEqual(c.wait_mapping(seq), 27)
        seq = c.set_learning(True)
        c.receive({"type": "mapped", "id": seq, "ok": True})
        with self.assertRaises(ValueError):
            c.wait_mapping(seq)

    def test_every_native_control_name_default_and_midi_value_matches_python(self):
        fixture = r"""
#include "stave/audition_session.hpp"
#include <iomanip>
#include <iostream>
int main() {
    std::cout << std::setprecision(17);
    for (unsigned i=0; i<stave::audition_names.size(); ++i) {
        auto c=static_cast<stave::AuditionControl>(i);
        double low, high, inside;
        if (!(std::cin >> low >> high >> inside)) return 2;
        std::cout<<stave::audition_names[i]<<" "<<stave::initial_controls[i]
                 <<" "<<stave::audition_value_valid(c,low)<<" "<<stave::audition_value_valid(c,high)
                 <<" "<<stave::audition_value_valid(c,low-.001)<<" "<<stave::audition_value_valid(c,high+.001)
                 <<" "<<stave::audition_value_valid(c,inside);
        if (stave::audition_mappable(c)) for(unsigned raw=0; raw<128; ++raw)
            std::cout<<" "<<stave::audition_midi_value(c,raw);
        std::cout<<"\n";
    }
}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, binary = root / "controls.cpp", root / "controls"
            source.write_text(fixture)
            subprocess.run(["c++", "-std=c++17", "-I", str(ROOT / "native_v2/include"),
                            str(source), "-o", str(binary)], check=True, capture_output=True)
            bounds = "".join(f"{lo} {hi} {lo + .125}\n" for lo, hi, _, _ in audition.CONTROLS.values())
            rows = subprocess.check_output([str(binary)], input=bounds, text=True).splitlines()
        self.assertEqual(len(rows), len(audition.CONTROLS))
        for row, (key, definition) in zip(rows, audition.CONTROLS.items()):
            name, initial, low_ok, high_ok, below_ok, above_ok, fractional_ok, *mapped = row.split()
            self.assertEqual((low_ok, high_ok, below_ok, above_ok), ("1", "1", "0", "0"), key)
            self.assertEqual(fractional_ok, "0" if key in audition.INTEGRAL else "1", key)
            self.assertEqual(name, key)
            self.assertEqual(float(initial), definition[3], key)
            self.assertEqual(len(mapped), 128 if key in audition.MIDI_MAPPABLE else 0, key)
            for raw, native in enumerate(mapped):
                self.assertAlmostEqual(float(native), audition.midi_value(key, raw), places=7, msg=f"{key} CC {raw}")

    def test_native_snapshot_precedes_saving_earlier_midi_later_mute(self):
        c = self.controller()
        seq = c.submit({"key":"master", "value":0})
        values = [entry[3] for entry in audition.CONTROLS.values()]
        raw = [-1]*len(values); serials = [0]*len(values)
        master = list(audition.CONTROLS).index("master")
        raw[master], serials[master] = 127, 1
        report = dict(type="status", instance=c.instance, blocks=10, applied=seq, routed=True,
                      fault=0, control_values=values, linked_offset=0, midi_mapped_raw=raw,
                      midi_mapped_serial=serials)
        c.receive(report)
        self.assertEqual(c.snapshot()["values"]["master"], 0)
        values[master] = 1
        c.receive(dict(report, blocks=11, control_values=values))
        self.assertEqual(c.snapshot()["values"]["master"], 1)

    def test_same_origin_and_host_guards(self):
        c = self.controller()
        server = audition.HTTPServer(("127.0.0.1", 0), audition.BaseHTTPRequestHandler)
        authority = f"127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = audition.handler_for(c, authority)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for origin, host, expected in (("http://"+authority, authority, 202), ("https://external.invalid", authority, 403),
                                           ("http://external.invalid", "external.invalid", 403)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("POST", "/control", json.dumps({"key": "master", "value": .2}),
                                   {"Host": host, "Origin": origin, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read(); connection.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()
        self.assertEqual(c.sequence, 1)

    def test_frontend_syntax_and_explicit_limitations(self):
        html = (ROOT / "native_v2/audition.html").read_text()
        javascript = html.split("<script>", 1)[1].split("</script>", 1)[0]
        subprocess.run(["node", "--check", "-"], input=javascript, text=True, capture_output=True, check=True)
        self.assertIn("Tap an outlined Stave control, then move one hardware knob", html)
        self.assertIn("data-midi-selected", html)
        self.assertIn("48 kHz with 512-frame buffers", html)
        self.assertIn("pending.clear()", html)

    def test_native_control_names_match_and_no_service_mutation(self):
        header = (ROOT / "native_v2/include/stave/audition_session.hpp").read_text()
        for name in audition.CONTROLS:
            self.assertIn('"'+name+'"', header)
        source = (ROOT / "tools/native_v2_audition.py").read_text()
        for forbidden in ("systemctl", "pw-metadata", "current_state.json", "shell=True"):
            self.assertNotIn(forbidden, source)

    def test_bed_frontend_loaded_keys_actions_and_stale_recovery(self):
        result = subprocess.run(["node", str(ROOT / "tests/test_native_bed_ui.js")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS:", result.stdout)
