"""Static and mocked checks for whole-cycle render telemetry integration."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _class_method(path, class_name, method_name):
    tree = ast.parse((ROOT / path).read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == method_name)
    method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {"logger": logging.getLogger(__name__),
                 "__package__": "stave_synth"}
    exec(compile(module, str(ROOT / path), "exec"), namespace)
    return namespace[method_name]


class RenderMetricsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jack_tree = ast.parse((ROOT / "stave_synth/jack_engine.py").read_text())
        cls.jack_class = next(node for node in cls.jack_tree.body
                              if isinstance(node, ast.ClassDef)
                              and node.name == "JackEngine")

    def _method(self, name):
        return next(node for node in self.jack_class.body
                    if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_start_sizes_scratch_and_metrics_after_graph_check_before_threads(self):
        start = self._method("start")
        source = ast.unparse(start)
        graph_check = source.index("if sr != SAMPLE_RATE or bs <= 0 or graph_error")
        scratch = source.index("self._sat_scratch = np.empty((2, bs)")
        metrics = source.index("self.render_metrics = RenderMetrics(bs / sr)")
        worker = source.index("self._render_thread.start()")
        self.assertLess(graph_check, scratch)
        self.assertLess(scratch, metrics)
        self.assertLess(metrics, worker)

    def test_record_covers_piano_synth_limiter_and_bridge_write(self):
        render = self._method("_render_loop")
        source = ast.unparse(render)
        timer = source.index("_cycle_t0 = time.perf_counter()")
        piano = source.index("self.piano_player.render_block(bs)")
        synth = source.index("self.synth.render(")
        limiter = source.index("self._limiter.process_inplace(stereo)")
        write = source.index("write_result = self._bridge.bridge_write_stereo(")
        elapsed = source.index("_cycle_dt = time.perf_counter() - _cycle_t0")
        post_fill = source.index("post_write_fill = self._bridge.bridge_get_ring_fill()")
        record = source.index("self.render_metrics.record(")
        self.assertEqual(sorted((timer, piano, synth, limiter, write, elapsed,
                                 post_fill, record)),
                         [timer, piano, synth, limiter, write, elapsed,
                          post_fill, record])
        self.assertIn("written=write_result == 1", source[record:record + 180])

    def test_health_exposes_snapshot_and_tolerates_old_mocks(self):
        health = _class_method("stave_synth/main.py", "StaveSynth", "_health_status")

        class Bridge:
            bridge_get_sample_rate = lambda self: 48000
            bridge_get_buffer_size = lambda self: 512
            bridge_get_graph_error = lambda self: 0
            bridge_get_midi_drop_count = lambda self: 0
            bridge_get_midi_recovery_count = lambda self: 0
            bridge_get_ring_slots = lambda self: 16

        base = dict(
            _control_error=None, _native_profile=None,
            _controls=SimpleNamespace(status=lambda: {}),
            _ui_recovery=SimpleNamespace(status=lambda: {}), ws_server=None,
        )
        metrics = SimpleNamespace(snapshot=lambda: {"duration_count": 7})
        app = SimpleNamespace(**base, jack=SimpleNamespace(
            _bridge=Bridge(), _last_error=None, render_metrics=metrics,
        ))
        self.assertEqual(health(app)["audio"]["render_metrics"],
                         {"duration_count": 7})

        old_mock = SimpleNamespace(**base, jack=SimpleNamespace(
            _bridge=Bridge(), _last_error=None,
        ))
        self.assertNotIn("render_metrics", health(old_mock)["audio"])

        # Instrument-source continuity is independent of bridge underruns.
        # Expose its counters without requiring an optional/native app import.
        app.piano = SimpleNamespace(midi_render_status=lambda: {
            "pending": 0, "native_render_lock_misses": 3,
        })
        self.assertEqual(health(app)["audio"]["piano_midi"], {
            "pending": 0, "native_render_lock_misses": 3,
        })
        self.assertNotIn("piano_midi", health(old_mock)["audio"])

        def unavailable():
            raise RuntimeError("mock snapshot unavailable")

        app.piano.midi_render_status = unavailable
        self.assertEqual(health(app)["audio"]["piano_midi"], {"available": False})

        app.jack.render_diagnostics = SimpleNamespace(snapshot=lambda: {"enabled": True, "count": 9})
        app.synth = SimpleNamespace(reverb=SimpleNamespace(
            get_diagnostics_status=lambda: {"enabled": True, "missed": 0}))
        self.assertEqual(health(app)["audio"]["render_diagnostics"]["count"], 9)
        self.assertEqual(health(app)["audio"]["reverb_diagnostics"]["missed"], 0)
        app.jack.render_diagnostics.snapshot = unavailable
        app.synth.reverb.get_diagnostics_status = unavailable
        self.assertEqual(health(app)["audio"]["render_diagnostics"], {"available": False})
        self.assertEqual(health(app)["audio"]["reverb_diagnostics"], {"available": False})

    def test_opt_in_diagnostics_clock_calls_are_guarded(self):
        source = ast.unparse(self._method("start"))
        self.assertIn("os.environ.get('STAVE_DIAGNOSTICS') == '1' else None", source)
        render = self._method("_render_loop")
        parents = {child: node for node in ast.walk(render) for child in ast.iter_child_nodes(node)}
        calls = [node for node in ast.walk(render) if isinstance(node, ast.Call)
                 and ast.unparse(node.func) in {"time.monotonic_ns", "time.thread_time_ns"}]
        self.assertEqual(len(calls), 6)
        for node in calls:
            ancestors = []
            while node in parents:
                node = parents[node]
                ancestors.append(node)
            self.assertTrue(any(isinstance(parent, ast.If)
                                and ast.unparse(parent.test) == "_diagnostics is not None"
                                for parent in ancestors))


if __name__ == "__main__":
    unittest.main()
