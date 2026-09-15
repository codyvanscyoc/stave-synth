"""Offline routing transactions and bounded-worker tests; no JACK commands run.

The real routing helper runs against an in-memory typed-port/connection graph.
Actual main method bodies are extracted without importing the application.
Worker tests replace subprocess.run with event-gated Python fakes.
"""

import ast
import copy
import logging
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT = "isolated-routing-test"
A2J_KEYS = "a2j:Keys [24] (capture): Keys MIDI 1"
PW_KEYS = "Midi-Bridge:Keys:(capture_0) Keys MIDI 1"
PW_FOOT = "Midi-Bridge:Foot:(capture_0) Foot MIDI 1"


def load_routing():
    path = ROOT / "stave_synth/routing.py"
    namespace = {"__name__": "isolated_routing_tests"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace


class FakeJack:
    def __init__(self):
        self.records = {}
        self.graph = {}
        self.commands = []
        self.hook = None
        for suffix in ("out_L", "out_R"):
            self.add(f"{CLIENT}:{suffix}", "audio", "output")
        self.add(f"{CLIENT}:midi_in", "midi", "input")
        for device, names in (("system", ("playback_1", "playback_2")),
                              ("new DAC", ("playback_FL", "playback_FR"))):
            for suffix in names:
                self.add(f"{device}:{suffix}", "audio", "input")
        self.add("mono:playback_FL", "audio", "input")
        self.add("not-a-sink:playback_FL", "audio", "output")
        self.add("not-a-sink:playback_FR", "audio", "output")
        self.add("production-synth:out_L", "audio", "output")
        self.connect("production-synth:out_L", "system:playback_1")
        self.connect(f"{CLIENT}:out_L", "system:playback_1")
        self.connect(f"{CLIENT}:out_R", "system:playback_2")

    def add(self, name, kind, direction):
        self.records[name] = ["8 bit raw midi" if kind == "midi" else "32 bit float mono audio",
                              f"properties: {direction},physical,terminal,"]
        self.graph.setdefault(name, set())

    def connect(self, source, target):
        self.graph.setdefault(source, set()).add(target)
        self.graph.setdefault(target, set()).add(source)

    def disconnect(self, source, target):
        self.graph.setdefault(source, set()).discard(target)
        self.graph.setdefault(target, set()).discard(source)

    def output(self, records):
        return "\n".join(name + "\n" + "\n".join("   " + row for row in rows)
                         for name, rows in records.items())

    def result(self, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def run(self, command, **_kwargs):
        self.commands.append(tuple(command))
        if self.hook:
            response = self.hook(command)
            if response is not NotImplemented:
                return response
        assert command[0] == "pw-jack"
        operation = command[1]
        if operation == "jack_lsp":
            return self.result(stdout=self.output(self.graph if "-c" in command else self.records))
        source, target = command[2:]
        # No operation may modify another application's output/input pair.
        assert source in {f"{CLIENT}:out_L", f"{CLIENT}:out_R"} or target == f"{CLIENT}:midi_in"
        if source not in self.records or target not in self.records:
            return self.result(1, stderr="port missing")
        if operation == "jack_connect":
            self.connect(source, target)
        elif operation == "jack_disconnect":
            self.disconnect(source, target)
        else:
            raise AssertionError(operation)
        return self.result()

    def mutations(self):
        return [command for command in self.commands if command[1] != "jack_lsp"]


def load_app(routing, jack):
    path = ROOT / "stave_synth/main.py"
    tree = ast.parse(path.read_text())
    app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StaveSynth")
    methods = {"_handle_get_audio_outputs", "_handle_set_audio_output", "_get_midi_capture_ports",
               "_get_port_connections", "_connect_midi_ports", "_audio_watch_loop"}
    app.body = [node for node in app.body if isinstance(node, ast.FunctionDef) and node.name in methods]

    class RemoveImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.level == 1 and node.module == "routing":
                return ast.copy_location(ast.Pass(), node)
            return node

    app = RemoveImports().visit(app)
    namespace = dict(routing, copy=copy, threading=threading, ISOLATED=False,
                     JACK_CLIENT_NAME=CLIENT, _run_with_hard_timeout=jack.run,
                     save_state=Mock(), logger=logging.getLogger("isolated_routing_main"))
    exec(compile(ast.Module(body=[app], type_ignores=[]), str(path), "exec"), namespace)
    instance = namespace["StaveSynth"]()
    instance._control_lock = threading.RLock()
    instance._stop_event = threading.Event()
    instance._stopping = False
    instance._running = True
    instance.state = {"master": {"audio_output_pref": "system"}}
    return instance, namespace


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.routing = load_routing()
        self.jack = FakeJack()
        self.app, self.main = load_app(self.routing, self.jack)

    def route(self, target="new DAC"):
        return self.app._handle_set_audio_output({"name": target})

    def assert_old(self):
        self.assertEqual(self.jack.graph[f"{CLIENT}:out_L"], {"system:playback_1"})
        self.assertEqual(self.jack.graph[f"{CLIENT}:out_R"], {"system:playback_2"})
        self.assertEqual(self.jack.graph["production-synth:out_L"], {"system:playback_1"})
        self.assertEqual(self.app.state["master"]["audio_output_pref"], "system")

    def test_only_complete_typed_stereo_pairs_are_listed_with_exact_active_links(self):
        listed = self.app._handle_get_audio_outputs()
        outputs = {item["name"]: item for item in listed["outputs"]}
        self.assertEqual(set(outputs), {"system", "new DAC"})
        self.assertTrue(outputs["system"]["active"])
        self.assertEqual(outputs["system"]["ports"], ["system:playback_1", "system:playback_2"])
        self.jack.disconnect(f"{CLIENT}:out_R", "system:playback_2")
        self.jack.connect("production-synth:out_L", "system:playback_2")
        self.assertFalse(next(item for item in self.app._handle_get_audio_outputs()["outputs"]
                              if item["name"] == "system")["active"])

    def test_missing_or_mono_target_causes_no_disconnect_or_preference_change(self):
        for target in ("missing", "mono", "not-a-sink"):
            with self.subTest(target=target):
                self.assertFalse(self.route(target)["success"])
                self.assertEqual(self.jack.mutations(), [])
                self.assert_old()
        self.main["save_state"].assert_not_called()

    def test_new_stereo_pair_is_verified_before_first_old_disconnect(self):
        def check(command):
            self.assertTrue(self.app._control_lock._is_owned())
            if command[1] == "jack_disconnect" and command[3].startswith("system:"):
                self.assertIn("new DAC:playback_FL", self.jack.graph[f"{CLIENT}:out_L"])
                self.assertIn("new DAC:playback_FR", self.jack.graph[f"{CLIENT}:out_R"])
            return NotImplemented

        self.jack.hook = check
        result = self.route()
        self.assertTrue(result["success"])
        self.assertTrue(result["persisted"])
        self.assertEqual(self.jack.graph[f"{CLIENT}:out_L"], {"new DAC:playback_FL"})
        self.assertEqual(self.jack.graph[f"{CLIENT}:out_R"], {"new DAC:playback_FR"})
        self.assertEqual(self.app.state["master"]["audio_output_pref"], "new DAC")
        self.main["save_state"].assert_called_once()
        self.assertEqual(self.jack.graph["production-synth:out_L"], {"system:playback_1"})

    def test_second_connect_failure_rolls_back_new_half_without_touching_old_links(self):
        self.jack.hook = lambda command: (self.jack.result(1, stderr="injected connect failure")
            if command[1:] == ["jack_connect", f"{CLIENT}:out_R", "new DAC:playback_FR"] else NotImplemented)
        result = self.route()
        self.assertFalse(result["success"])
        self.assertTrue(result["rollback_complete"])
        self.assert_old()
        self.assertFalse(any(command[1] == "jack_disconnect" and command[3].startswith("system:")
                             for command in self.jack.commands))
        self.main["save_state"].assert_not_called()

    def test_already_message_without_verified_link_is_not_success(self):
        self.jack.hook = lambda command: (self.jack.result(1, stderr="Already connected")
            if command[1] == "jack_connect" else NotImplemented)
        result = self.route()
        self.assertFalse(result["success"])
        self.assert_old()

    def test_nonzero_connect_status_is_accepted_only_when_graph_confirms_link(self):
        def already(command):
            if command[1] == "jack_connect":
                self.jack.connect(command[2], command[3])
                return self.jack.result(1, stderr="already connected")
            return NotImplemented

        self.jack.hook = already
        self.assertTrue(self.route()["success"])

    def test_disconnect_failure_restores_old_pair_before_removing_new_pair(self):
        failed = []

        def fail_once(command):
            if command[1:] == ["jack_disconnect", f"{CLIENT}:out_R", "system:playback_2"] and not failed:
                failed.append(True)
                return self.jack.result(1, stderr="injected disconnect failure")
            return NotImplemented

        self.jack.hook = fail_once
        result = self.route()
        self.assertFalse(result["success"])
        self.assertTrue(result["rollback_complete"])
        self.assert_old()
        mutations = self.jack.mutations()
        restore = mutations.index(("pw-jack", "jack_connect", f"{CLIENT}:out_L", "system:playback_1"))
        remove_new = mutations.index(("pw-jack", "jack_disconnect", f"{CLIENT}:out_L", "new DAC:playback_FL"))
        self.assertLess(restore, remove_new)

    def test_pending_connect_is_explicitly_uncertain_and_keeps_old_links(self):
        self.jack.hook = lambda command: (None
            if command[1:] == ["jack_connect", f"{CLIENT}:out_R", "new DAC:playback_FR"] else NotImplemented)
        result = self.route()
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["rollback_complete"])
        self.assertIn("system:playback_1", self.jack.graph[f"{CLIENT}:out_L"])
        self.assertIn("system:playback_2", self.jack.graph[f"{CLIENT}:out_R"])
        self.assertEqual(self.app.state["master"]["audio_output_pref"], "system")
        # A late command may complete. Once admission recovers, reconciling
        # the saved preference removes its extra links without changing pref.
        self.jack.connect(f"{CLIENT}:out_R", "new DAC:playback_FR")
        self.jack.hook = None
        self.assertTrue(self.route("system")["success"])
        self.assert_old()

    def test_read_failure_reports_error_and_performs_no_mutations(self):
        self.jack.hook = lambda _command: self.jack.result(1, stderr="server unavailable")
        self.assertIn("error", self.app._handle_get_audio_outputs())
        self.assertFalse(self.route()["success"])
        self.assertEqual(self.jack.mutations(), [])

    def test_verified_audio_survives_preference_save_failure_with_explicit_warning(self):
        self.main["save_state"].side_effect = OSError("injected storage failure")
        result = self.route()
        self.assertTrue(result["success"])
        self.assertFalse(result["persisted"])
        self.assertIn("not saved", result["warning"])
        self.assertIn("storage failure", self.app._control_error)
        self.assertEqual(self.app.state["master"]["audio_output_pref"], "new DAC")

    def test_isolated_and_stopping_instances_never_mutate_routes(self):
        self.midi("Unknown physical controller:output")
        self.main["ISOLATED"] = True
        self.assertFalse(self.route()["success"])
        self.app._connect_midi_ports()
        self.app._audio_watch_loop()
        self.assertEqual(self.jack.mutations(), [])
        self.main["ISOLATED"] = False
        self.app._stopping = True
        self.assertFalse(self.route()["success"])
        self.assertFalse(self.app._connect_midi_ports())
        self.app._audio_watch_loop()
        self.assertEqual(self.jack.mutations(), [])

    def midi(self, *names):
        for name in names:
            self.jack.add(name, "midi", "output")

    def test_generic_physical_outputs_use_typed_property_listing_without_brand_names(self):
        generic = ("system:midi_capture_1", "Unknown Vendor USB:Port 1",
                   "Unknown Vendor USB:Port 2")
        self.midi(*generic)
        ports = self.app._get_midi_capture_ports()
        self.assertEqual(set(ports), set(generic))
        self.assertIn(("pw-jack", "jack_lsp", "-t", "-p"), self.jack.commands)
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], set(generic))
        self.assertEqual(self.app._midi_capture_duplicates, {})

    def test_generic_sources_require_exact_type_output_and_physical_metadata(self):
        valid = "Typed hardware:output"
        records = {
            valid: ["8 bit raw midi", "properties: output, physical, terminal,"],
            "Software sequencer:output": ["8 bit raw midi", "properties: output,"],
            "Missing flags:output": ["8 bit raw midi"],
            "Missing type:output": ["properties: output, physical,"],
            "Wrong type:output": ["audio containing midi in a label",
                                  "properties: output, physical,"],
            "Hardware:input": ["8 bit raw midi", "properties: input, physical,"],
            "Hardware:ambiguous": ["8 bit raw midi", "properties: input, output, physical,"],
            "Hardware:missing direction": ["8 bit raw midi", "properties: physical,"],
            "Hardware:near flags": ["8 bit raw midi", "properties: output, nonphysical,"],
            "Midi Through:output": ["8 bit raw midi", "properties: output, physical,"],
            f"{CLIENT}:midi_output": ["8 bit raw midi", "properties: output, physical,"],
        }
        selected, duplicates = self.routing["midi_capture_selection"](records, client=CLIENT)
        self.assertEqual(selected, [valid])
        self.assertEqual(duplicates, {})

    def test_recognized_bridges_keep_legacy_missing_physical_or_properties_support(self):
        records = {
            A2J_KEYS: ["8 bit raw midi", "properties: output, terminal,"],
            PW_KEYS: ["8 bit raw midi", "properties: output,"],
            PW_FOOT: ["8 bit raw midi"],
        }
        selected, duplicates = self.routing["midi_capture_selection"](records, client=CLIENT)
        self.assertEqual(set(selected), {A2J_KEYS, PW_FOOT})
        self.assertEqual(duplicates, {A2J_KEYS: [PW_KEYS]})

    def test_recognized_capture_names_never_override_missing_type_or_wrong_direction(self):
        for rows in (["8 bit raw midi", "properties: input, physical,"],
                     ["8 bit raw midi", "properties: input, output,"],
                     ["8 bit raw midi", "properties: terminal,"],
                     ["8 bit raw midi", "properties:"],
                     ["properties: output, physical,"],
                     ["32 bit float mono audio", "properties: output, physical,"]):
            with self.subTest(rows=rows):
                selected, duplicates = self.routing["midi_capture_selection"](
                    {A2J_KEYS: rows, PW_KEYS: rows}, client=CLIENT
                )
                self.assertEqual(selected, [])
                self.assertEqual(duplicates, {})

    def test_own_client_exclusion_precedes_bridge_alias_matching(self):
        self.midi(A2J_KEYS, PW_KEYS, "Other physical:output")
        selected, duplicates = self.routing["midi_capture_selection"](
            self.jack.records, client="a2j"
        )
        self.assertEqual(set(selected), {PW_KEYS, "Other physical:output"})
        self.assertEqual(duplicates, {})

    def test_main_discovery_passes_own_identity_and_never_self_connects(self):
        own, hardware = f"{CLIENT}:midi_output", "Other physical:output"
        self.midi(own, hardware)
        self.assertEqual(self.app._get_midi_capture_ports(), [hardware])
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {hardware})
        self.assertFalse(any(command[1] == "jack_connect" and command[2] == own
                             for command in self.jack.commands))

    def test_generic_sources_coexist_with_bridge_aliases_without_duplicate_rewiring(self):
        generic = ("Other keys:output", "Other keys:second output", "Pedal:output")
        self.midi(A2J_KEYS, PW_KEYS, *generic)
        self.jack.connect(PW_KEYS, f"{CLIENT}:midi_in")
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {PW_KEYS, *generic})
        self.assertEqual(self.app._midi_capture_duplicates, {A2J_KEYS: [PW_KEYS]})
        self.assertFalse(any(command[1] == "jack_disconnect" for command in self.jack.commands))
        self.assertFalse(any(command[1] == "jack_connect" and command[2] == A2J_KEYS
                             for command in self.jack.commands))

    def test_one_generic_connection_failure_does_not_drop_other_controller_and_recovers(self):
        unavailable, working = "First controller:output", "Second controller:output"
        self.midi(unavailable, working)
        self.jack.hook = lambda command: (
            self.jack.result(1, stderr="device disappeared")
            if command[1:] == ["jack_connect", unavailable, f"{CLIENT}:midi_in"]
            else NotImplemented
        )
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {working})
        self.jack.hook = None
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {unavailable, working})
        self.assertFalse(any(command[1] == "jack_disconnect" for command in self.jack.commands))

    def test_generic_discovery_failure_is_visible_keeps_existing_links_and_recovers(self):
        generic = "Independent controller:output"
        self.midi(generic)
        self.assertTrue(self.app._connect_midi_ports())
        mutations = list(self.jack.mutations())
        self.jack.hook = lambda command: (
            self.jack.result(1, stderr="temporary graph failure")
            if command[1] == "jack_lsp" else NotImplemented
        )
        self.assertFalse(self.app._connect_midi_ports())
        self.assertIn("temporary graph failure", self.app._midi_discovery_error)
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {generic})
        self.assertEqual(self.jack.mutations(), mutations)
        self.jack.hook = None
        self.assertTrue(self.app._connect_midi_ports())
        self.assertIsNone(self.app._midi_discovery_error)
        self.assertEqual(self.jack.mutations(), mutations)

    def test_midi_bridge_pairs_dedupe_without_dropping_a_different_device(self):
        self.midi(A2J_KEYS, PW_KEYS, PW_FOOT, "a2j:Midi Through [14] (capture): Midi Through Port-0")
        ports = self.app._get_midi_capture_ports()
        self.assertEqual(set(ports), {A2J_KEYS, PW_FOOT})
        self.assertEqual(self.app._midi_capture_duplicates, {A2J_KEYS: [PW_KEYS]})
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {A2J_KEYS, PW_FOOT})

    def test_midi_multiport_and_identically_named_ambiguous_devices_are_not_dropped(self):
        second_port = "a2j:Keys [24] (capture): Keys MIDI 2"
        self.midi(A2J_KEYS, second_port, PW_KEYS)
        self.assertEqual(set(self.app._get_midi_capture_ports()), {A2J_KEYS, second_port})
        twin = "a2j:Keys [25] (capture): Keys MIDI 1"
        self.midi(twin)
        self.assertEqual(set(self.app._get_midi_capture_ports()), {A2J_KEYS, second_port, twin, PW_KEYS})

    def test_simple_pipewire_capture_name_matches_only_its_own_device(self):
        simple = "Midi-Bridge:Keys MIDI 1 (capture)"
        self.midi(A2J_KEYS, simple, PW_FOOT)
        self.assertEqual(set(self.app._get_midi_capture_ports()), {A2J_KEYS, PW_FOOT})

    def test_working_pipewire_alias_is_kept_without_temporarily_doubling_notes(self):
        self.midi(A2J_KEYS, PW_KEYS)
        self.jack.connect(PW_KEYS, f"{CLIENT}:midi_in")
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.mutations(), [])
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {PW_KEYS})

    def test_existing_duplicate_link_is_removed_only_with_verified_preferred_link(self):
        self.midi(A2J_KEYS, PW_KEYS)
        self.jack.connect(A2J_KEYS, f"{CLIENT}:midi_in")
        self.jack.connect(PW_KEYS, f"{CLIENT}:midi_in")
        self.assertTrue(self.app._connect_midi_ports())
        self.assertEqual(self.jack.graph[f"{CLIENT}:midi_in"], {A2J_KEYS})
        self.assertEqual(self.jack.mutations(), [("pw-jack", "jack_disconnect", PW_KEYS, f"{CLIENT}:midi_in")])

    def one_watch_iteration(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            return len(waits) > 1

        self.app._stop_event = SimpleNamespace(wait=wait)
        self.app._audio_watch_loop()
        self.assertEqual(waits, [0.5, 2.5])

    def test_watch_does_not_fallback_when_saved_device_is_missing(self):
        self.app.state["master"]["audio_output_pref"] = "missing venue DAC"
        self.one_watch_iteration()
        self.assertEqual(self.jack.mutations(), [])
        self.assertEqual(self.app.state["master"]["audio_output_pref"], "missing venue DAC")

    def test_watch_repairs_missing_right_channel_for_numbered_system_ports(self):
        self.jack.disconnect(f"{CLIENT}:out_R", "system:playback_2")
        self.one_watch_iteration()
        self.assert_old()
        self.assertIn(("pw-jack", "jack_connect", f"{CLIENT}:out_R", "system:playback_2"), self.jack.commands)

    def test_watch_stop_event_exits_before_any_probe(self):
        self.app._stop_event.set()
        self.app._audio_watch_loop()
        self.assertEqual(self.jack.commands, [])


class BoundedCommandTests(unittest.TestCase):
    def test_two_inflight_workers_then_timeout_blocks_new_admission_until_finished(self):
        routing = load_routing()
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        workers = []
        calls = []
        callers = []

        def blocked(command, **_kwargs):
            index = len(calls)
            calls.append(command)
            workers.append(threading.current_thread())
            entered[index].set()
            release.wait(3)
            return SimpleNamespace(returncode=0, stdout="done", stderr="")

        try:
            with patch.object(subprocess, "run", side_effect=blocked):
                first = threading.Thread(target=lambda: callers.append(routing["run_bounded_command"](["fake1"], 1, 0.1)))
                second = threading.Thread(target=lambda: callers.append(routing["run_bounded_command"](["fake2"], 1, 0.1)))
                first.start()
                self.assertTrue(entered[0].wait(1))
                second.start()
                self.assertTrue(entered[1].wait(1))
                self.assertIsNone(routing["run_bounded_command"](["refused-full"], 1, 0.01))
                first.join(1)
                second.join(1)
                self.assertEqual(callers, [None, None])
                began = time.monotonic()
                for _ in range(20):
                    self.assertIsNone(routing["run_bounded_command"](["refused-pending"], 1, 0.01))
                self.assertLess(time.monotonic() - began, 0.2)
                self.assertEqual(len(calls), 2)
                release.set()
                for worker in workers:
                    worker.join(1)
                self.assertEqual(routing["_WORKERS"], {})
            expected = SimpleNamespace(returncode=0, stdout="recovered", stderr="")
            with patch.object(subprocess, "run", return_value=expected):
                self.assertIs(routing["run_bounded_command"](["fake3"], 1, 1), expected)
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

    def test_failure_and_thread_start_failure_release_admission(self):
        routing = load_routing()
        with patch.object(subprocess, "run", side_effect=OSError("injected launch error")):
            self.assertIsNone(routing["run_bounded_command"](["fake"], 1, 1))
        self.assertEqual(routing["_WORKERS"], {})
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("injected thread error")):
            self.assertIsNone(routing["run_bounded_command"](["fake"], 1, 1))
        self.assertEqual(routing["_WORKERS"], {})


if __name__ == "__main__":
    unittest.main()
