"""Offline coordinator tests: fake peers, fake clocks/probes, owned subprocesses."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools import run_core_soak as probe
from tests.test_performance_probe import PerformanceApplication


def system_sample():
    return dict(pid=1234, start_ticks=100, VmRSS=500000, VmHWM=510000, VmSwap=0,
                Threads=12, mem_available_kib=700000, temperature_c=51.0, throttled=0)


class CoreApplication(PerformanceApplication):
    def __init__(self):
        super().__init__()
        self.saved = True
        self.health["controls"] = dict(alive=True, closed=False, dropped=0, pending=0)
        self.health["ui"] = dict(clients=2, healthy=True, http_listener=True, websocket_listener=True,
            event_loop_progressing=True, handler_worker_available=True, handler_worker_progressing=True,
            producer_overflows=0, slow_client_disconnects=0)
        self.health["audio"].update(ring_slots=6, midi_dropped=0, midi_recoveries=0,
            render_metrics=dict(duration_count=100, duration_sum_seconds=0.6,
                                duration_max_seconds=0.006, min_ring_fill_blocks=2.0,
                                histogram={"bin_width_seconds":512/48000/100,"cumulative_counts":[100]*161},
                                **dict.fromkeys(probe.RENDER_COUNTERS, 0)))
        self.health["audio"]["piano_midi"].update(enqueued=0, applied=0, queue_capacity=256)
        self.debug = dict(bridge_underruns=7, bridge_xruns=0, bridge_callbacks=100)
        self.original = copy.deepcopy(self.state)
        self.peer_diverges = False

    def slots(self):
        reply = super().slots()
        for slot in reply["slots"]:
            slot["filename"] = "pad_C.wav" if slot["note"] == 60 else None
        return reply

    def dispatch(self, message, connection):
        if message["type"] == "debug":
            self.messages.append(copy.deepcopy(message))
            return dict(type="debug", **self.debug)
        result = super().dispatch(message, connection)
        if message["type"] == "get_state" and self.peer_diverges and connection == 2:
            result["state"]["master"]["volume"] = 0.123
        return result

    def progress(self):
        self.debug["bridge_callbacks"] += 100
        self.health["audio"]["render_metrics"]["duration_count"] += 100
        self.health["audio"]["render_metrics"]["duration_sum_seconds"] += 0.6
        self.health["audio"]["render_metrics"]["histogram"]["cumulative_counts"] = [
            self.health["audio"]["render_metrics"]["duration_count"]]*161
        for key in ("enqueued", "applied"):
            self.health["audio"]["piano_midi"][key] += 10


def native_events(seconds=20):
    """Exact native JSON protocol, accelerated without importing JACK."""
    audio = dict(nonfinite=0, xruns=0, errors=0, zero_active_blocks=0,
                 clipped=0, peak=0.05, rms=0.01, active_blocks=1200)
    requested = seconds * 48000
    captured = ((seconds + 7)*48000 + 511)//512*512
    midi = probe.expected_midi_events(seconds)
    start = dict(event="soak_started", qualification=seconds==28800, expect_bed=True,
        requested_seconds=seconds, tail_seconds=2, terminal_grace_seconds=5,
        sample_rate_hz=48000, block_frames=512, loop_seconds=60, phrase_events=60,
        started_monotonic_ns=10_000_000_000)
    marker = dict(event="soak_music_complete", requested_frames=requested,
        phrase_midi_events=midi, monotonic_ns=(10+seconds)*1_000_000_000)
    intervals = []
    for index, position in enumerate(range(0, captured, 60*48000)):
        frames = min(60*48000, captured-position)
        intervals.append(dict(event="soak_checkpoint", index=index, start_frame=position,
            frames=frames, midi_events=midi+3 if position+frames==captured else 0,
            monotonic_ns=(10+seconds+7)*1_000_000_000, **audio))
    final = dict(event="soak_complete", qualification=seconds==28800,
        requested_frames=requested, captured_frames=captured, checkpoints=len(intervals),
        midi_events=midi+3, phrase_midi_events=midi, phrase_loops=seconds//60,
        terminal_release_events=3, music_blocks=(requested+511)//512,
        zero_music_blocks=0, interrupted=False, clock="CLOCK_MONOTONIC",
        midi_first_callback_monotonic_ns=10_001_000_000,
        capture_first_callback_monotonic_ns=10_001_100_000, **audio)
    return [start, marker, *intervals, final]


class NativeProcess:
    def __init__(self, events, clock, *, stall=False):
        self.events = copy.deepcopy(events)
        self.clock = clock
        self.stall = stall
        self.returncode = None
        self.terminated = False
        self.stdout = self
        self.stderr = SimpleNamespace(read=AsyncMock(return_value=b""))

    async def readline(self):
        if self.events:
            event = self.events.pop(0)
            self.clock[0] = event.get("monotonic_ns", event.get("started_monotonic_ns", self.clock[0]))
            return (json.dumps(event)+"\n").encode()
        if self.stall:
            raise TimeoutError("injected stalled owned subprocess")
        return b""

    async def wait(self):
        self.returncode = 0 if not self.terminated else -15
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class CoreSoakTests(unittest.TestCase):
    def run_fake(self, *, cancel=False, fatal=False, overbudget=False, missing=False,
                 peer_diverges=False, changed_pid=False, changed_fixture=False):
        app = CoreApplication()
        app.peer_diverges = peer_diverges
        if missing:
            del app.debug["bridge_underruns"]
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary).resolve()
            output = private / "evidence"
            pad = private / "pad_C.wav"
            pad.write_bytes(b"accepted-private-C")
            digest = hashlib.sha256(pad.read_bytes()).hexdigest()
            args = SimpleNamespace(seconds=20, pid=1234, instance_root=str(private),
                                   output_dir=str(output), pad_sha256=digest, driver=sys.executable)
            async def capture(owner):
                owner.anchor = 10.0
                if cancel:
                    raise asyncio.CancelledError()
                app.progress()
                if fatal:
                    app.debug["bridge_underruns"] += 1
                if overbudget:
                    app.health["audio"]["render_metrics"]["over_budget_count"] += 1
                _, counters = await owner.checkpoint("musical", 18)
                owner.report["last_musical_counters"] = counters
                owner.report["last_musical_snapshot_seconds"] = 18
                owner.report["native_final"] = {"event": "soak_complete"}
                if changed_fixture:
                    pad.write_bytes(b"changed")
                app.progress()

            process_calls = []
            def process(*args, **kwargs):
                process_calls.append(1)
                if changed_pid and len(process_calls) >= 2:
                    raise RuntimeError("audition PID reused")
                return system_sample()

            with patch.object(probe, "validate_paths", return_value=(private, output, pad, Path(sys.executable).resolve())), \
                 patch.object(probe, "verify_runtime", new=AsyncMock(return_value={"instance":"audition"})), \
                 patch.object(probe, "system_snapshot", new=AsyncMock(side_effect=lambda *a:system_sample())), \
                 patch.object(probe, "process_snapshot", side_effect=process), \
                 patch.object(probe.websockets, "connect", side_effect=app.connect), \
                 patch.object(probe.Soak, "capture", new=capture), \
                 patch.object(probe.asyncio, "sleep", new=AsyncMock()), \
                 patch.object(probe.time, "monotonic", return_value=35.0):
                try:
                    code = asyncio.run(probe.run(args))
                except asyncio.CancelledError:
                    code = "cancelled"
            result = json.loads((output / "core-soak.result.json").read_text())
            streams = {p.name:p.read_text() for p in output.iterdir() if p.suffix==".ndjson"}
            modes = [stat.S_IMODE(p.stat().st_mode) for p in output.iterdir()]
        return code, result, app, streams, modes

    def test_short_smoke_restores_accepted_bed_and_never_qualifies_eight_hours(self):
        code, report, app, streams, modes = self.run_fake()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "PASS_SHORT_RUN")
        self.assertFalse(report["core_soak_qualified"])
        self.assertFalse(report["stage_qualified"])
        self.assertFalse(report["memory_gate"]["sufficient"])
        self.assertEqual(app.state, app.original)
        self.assertTrue(app.saved)
        self.assertFalse(app.active)
        self.assertTrue(all(mode==0o600 for mode in modes))
        self.assertEqual(sum(m["type"]=="panic" for m in app.messages), 2)
        forbidden={"record_toggle","save_to_pad_slot","preset_save","set_audio_output","instrument_cycle"}
        self.assertFalse(any(m["type"] in forbidden for m in app.messages))
        self.assertFalse(any(m.get("param")=="reverb_type" for m in app.messages))
        self.assertIn('"original_state"', streams["session.ndjson"].splitlines()[0])
        self.assertEqual(app.connections, 2)

    def test_overbudget_retained_as_strict_failure_without_losing_complete_run(self):
        code, report, app, _, _ = self.run_fake(overbudget=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["raw_total_delta"]["render.over_budget_count"], 1)
        self.assertTrue(report["raw_counter_failures"])
        self.assertIn("native_final", report)
        self.assertEqual(app.state, app.original)

    def test_continuity_fault_missing_counter_peer_divergence_abort_and_restore(self):
        for option in ("fatal", "missing", "peer_diverges"):
            with self.subTest(option=option):
                code, report, app, _, _ = self.run_fake(**{option: True})
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "FAIL")
                self.assertTrue(report["failures"])
                self.assertEqual(app.state, app.original)

    def test_cancellation_after_bed_trigger_restores_and_retains_failure(self):
        code, report, app, _, _ = self.run_fake(cancel=True)
        self.assertEqual(code, "cancelled")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(app.state, app.original)
        self.assertFalse(app.active)
        self.assertEqual(report["restore_failures"], [])

    def test_pid_reuse_refuses_cleanup_writes_and_fixture_change_is_failure(self):
        code, report, app, _, _ = self.run_fake(changed_pid=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("identity" in text for text in report["restore_failures"]))
        self.assertEqual(sum(m["type"]=="panic" for m in app.messages), 1)
        code, report, _, _, _ = self.run_fake(changed_fixture=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("pad file changed" in text for text in report["restore_failures"]))

    def test_counter_reset_missing_progress_and_missing_fields_refuse(self):
        app = CoreApplication()
        state = app.dispatch({"type":"get_state"},1)
        before = probe.strict_snapshot(state, app.debug)
        for key in before:
            broken = dict(before)
            broken[key] = before[key]-1
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                probe.compare_counters(before, broken)
        with self.assertRaises(RuntimeError):
            probe.compare_counters(before, before)
        for key in ("bridge_callbacks","bridge_xruns","bridge_underruns"):
            debug = dict(app.debug)
            del debug[key]
            with self.assertRaises(RuntimeError):
                probe.strict_snapshot(state, debug)

    def test_phrase_balances_notes_pedals_and_has_no_loop_panic(self):
        notes=set(); pedal=False
        rows=probe.phrase_text().splitlines()
        self.assertEqual(rows[0], "loop_seconds 60")
        previous=-1
        for row in rows[1:]:
            when,status,note,value=row.split(); when=float(when); status=int(status,16); note=int(note); value=int(value)
            self.assertGreaterEqual(when,previous); previous=when
            if status==0x90:
                self.assertNotIn(note,notes); notes.add(note)
            elif status==0x80:
                notes.remove(note)
            elif status==0xB0:
                self.assertEqual(note,64); pedal=value>=64
        self.assertFalse(notes); self.assertFalse(pedal)
        self.assertEqual(probe.expected_midi_events(20),24)
        self.assertEqual(probe.expected_midi_events(600),600)
        self.assertEqual(probe.expected_midi_events(28800),28800)

    def test_provisional_memory_requires_full_duration_and_all_hourly_samples(self):
        samples=[{"elapsed_seconds":minute*60+0.1,"VmRSS":500000+minute}
                 for minute in range(60,480)]
        good=probe.memory_gate(samples,28800)
        self.assertTrue(good["sufficient"]); self.assertTrue(good["passed"])
        self.assertFalse(probe.memory_gate(samples,600)["sufficient"])
        self.assertFalse(probe.memory_gate(samples[:60],28800)["sufficient"])
        bad=[{**item,"VmRSS":500000+i*100} for i,item in enumerate(samples)]
        self.assertFalse(probe.memory_gate(bad,28800)["passed"])

    def test_evidence_caps_and_exclusive_files_fail_without_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            evidence=probe.Evidence(root)
            evidence.write("native.ndjson",{"event":"one"})
            before=(root/"native.ndjson").read_bytes()
            with patch.object(probe,"MAX_EVIDENCE_BYTES",evidence.bytes), self.assertRaises(RuntimeError):
                evidence.write("native.ndjson",{"event":"two"})
            evidence.close()
            self.assertEqual((root/"native.ndjson").read_bytes(),before)
            with self.assertRaises(FileExistsError):
                probe.exclusive_text(root/"native.ndjson")

    def test_exact_pid_environment_starttime_and_source_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); source=root/"source"; source.mkdir()
            proc=root/"proc"; folder=proc/"1234"; folder.mkdir(parents=True)
            (folder/"cwd").symlink_to(source,target_is_directory=True)
            fields=["S"]+["0"]*18+["100"]+["0"]*8
            (folder/"stat").write_text("1234 (python worker) "+" ".join(fields))
            (folder/"cmdline").write_bytes(b"python\0-m\0stave_synth.main\0")
            env={"STAVE_INSTANCE":"audition","STAVE_INSTANCE_ROOT":str(root),"STAVE_HTTP_PORT":"18080",
                 "STAVE_WEBSOCKET_PORT":"18765","STAVE_REQUIRE_NATIVE":"1"}
            (folder/"environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k,v in env.items()))
            (folder/"status").write_text("VmRSS:\t100 kB\nVmHWM:\t200 kB\nVmSwap:\t0 kB\nThreads:\t4\n")
            result=probe.process_snapshot(1234,root,proc_root=proc)
            self.assertEqual(result["start_ticks"],100)
            with self.assertRaises(RuntimeError):
                probe.process_snapshot(1234,root,expected_start=101,proc_root=proc)
            (folder/"environ").write_bytes(b"STAVE_INSTANCE=stage\0")
            with self.assertRaises(RuntimeError):
                probe.process_snapshot(1234,root,proc_root=proc)

    def test_path_guards_pin_private_fixture_and_refuse_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary).resolve(); root=home/"stave-synth-pi4-backups"/"owned"
            for name in ("config","source","data/pad_samples"):
                (root/name).mkdir(parents=True,exist_ok=True)
            pad=root/"data/pad_samples/pad_C.wav"; pad.write_bytes(b"accepted")
            driver=root/"source/native_soak"; driver.write_bytes(b"fixture"); driver.chmod(0o700)
            args=SimpleNamespace(seconds=20,pid=1234,instance_root=str(root),output_dir=str(root/"new"),
                driver=str(driver),pad_sha256=hashlib.sha256(pad.read_bytes()).hexdigest())
            with patch.object(probe.Path,"home",return_value=home):
                self.assertEqual(probe.validate_paths(args)[0],root)
                (root/"new").mkdir()
                with self.assertRaises(RuntimeError): probe.validate_paths(args)
                args.output_dir=str(root/"another"); args.pad_sha256="0"*64
                with self.assertRaises(RuntimeError): probe.validate_paths(args)

    def test_native_audio_evidence_rejects_missing_nonfinite_clip_and_silence(self):
        owner=probe.Soak(None,None,None,None,None,{},None)
        good=dict(nonfinite=0,xruns=0,errors=0,zero_active_blocks=0,clipped=0,peak=0.05,rms=0.01)
        owner.validate_audio_event(good)
        for key,value in (("clipped",1),("zero_active_blocks",1),("peak",1.0),("rms",float("nan"))):
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                owner.validate_audio_event({**good,key:value})
        for key in good:
            item=dict(good); del item[key]
            with self.assertRaises(RuntimeError): owner.validate_audio_event(item)

    def run_native(self, events, *, seconds=20, stall=False, cancel=False):
        clock = [10_000_000_000]
        process = NativeProcess(events, clock, stall=stall)
        app = CoreApplication()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            evidence = probe.Evidence(output)
            owner = probe.Soak(SimpleNamespace(seconds=seconds), None, output, None,
                              Path("/private/owned/native_soak"), {}, evidence)
            owner.previous = probe.strict_snapshot(app.dispatch({"type":"get_state"},1), app.debug)
            async def command(peer, label, message, reply_type=None):
                if cancel:
                    raise asyncio.CancelledError()
                if message["type"]=="get_state":
                    app.progress()
                return app.dispatch(message, peer+1)
            owner.command = command
            error = None
            with patch.object(probe.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as launch, \
                 patch.object(probe.time, "monotonic", side_effect=lambda:clock[0]/1e9), \
                 patch.object(probe.time, "monotonic_ns", side_effect=lambda:clock[0]):
                try:
                    asyncio.run(owner.native())
                except BaseException as exc:
                    error = exc
            evidence.close()
            raw = (output/"native.ndjson").read_text()
        return owner, process, launch.call_args, error, raw

    def test_native_subprocess_full_protocol_marker_and_exact_smoke_arguments(self):
        owner, process, launched, error, raw = self.run_native(native_events())
        self.assertIsNone(error)
        self.assertEqual(launched.args[1:3], ("--smoke","--expect-bed"))
        self.assertEqual(launched.args[-1], "20")
        self.assertFalse(process.terminated)
        self.assertTrue(owner.music_complete)
        self.assertEqual(owner.report["last_musical_snapshot_seconds"], 20)
        self.assertEqual(owner.report["native_final"]["midi_events"], 27)
        self.assertEqual(len(raw.splitlines()),4)
        _, _, launched, error, _ = self.run_native(native_events(600),seconds=600)
        self.assertIsNone(error)
        self.assertEqual(launched.args[1], "--expect-bed")
        self.assertNotIn("--smoke",launched.args)

    def test_native_subprocess_missing_marker_wrong_mode_silence_and_interval_loss_fail(self):
        variants=[]
        variants.append([event for event in native_events() if event["event"]!="soak_music_complete"])
        for position,key,value in ((0,"expect_bed",False),(0,"qualification",True),
                                  (0,"terminal_grace_seconds",None),(2,"index",1),
                                  (2,"midi_events",26),(-1,"interrupted",True),
                                  (-1,"zero_music_blocks",1),(-1,"clipped",1)):
            events=native_events(); events[position][key]=value; variants.append(events)
        duplicate=native_events(); duplicate.insert(2,dict(duplicate[1])); variants.append(duplicate)
        for index, events in enumerate(variants):
            with self.subTest(index=index):
                owner, _, _, error, raw=self.run_native(events)
                self.assertIsInstance(error,RuntimeError)
                self.assertNotIn("native_final",owner.report)
                self.assertTrue(raw)

    def test_stalled_or_cancelled_native_subprocess_is_owned_and_terminated(self):
        for kwargs in ({"events":native_events()[:1],"stall":True},
                       {"events":native_events(),"cancel":True}):
            with self.subTest(kwargs=kwargs):
                _, process, _, error, _=self.run_native(**kwargs)
                self.assertIsNotNone(error)
                self.assertTrue(process.terminated)

    def test_gentle_curve_fake_clock_is_bounded_alternating_and_quiet_during_rests(self):
        clock=[0.0]; sent=[]
        async def sleep(seconds): clock[0]+=seconds
        async def command(peer,label,message,**kwargs): sent.append((clock[0],peer,message))
        async def run_controls():
            owner=probe.Soak(SimpleNamespace(seconds=60),None,None,None,None,{},None)
            owner.anchor=0.0; owner.started.set(); owner.command=command
            await owner.controls()
        with patch.object(probe.asyncio,"sleep",new=sleep), \
             patch.object(probe.time,"monotonic",side_effect=lambda:clock[0]):
            asyncio.run(run_controls())
        self.assertEqual(len(sent),150)
        self.assertTrue(all(10<=when<40 for when,_,_ in sent))
        for index,(when,peer,message) in enumerate(sent):
            self.assertEqual(peer,index%2)
            self.assertAlmostEqual(when,10+index*.2)
            if peer==0:
                self.assertEqual(message["param"],"filter_cutoff_hz")
                self.assertTrue(800<=message["value"]<=6000)
            else:
                self.assertEqual(message["param"],"reverb_dry_wet")
                self.assertTrue(.2<=message["value"]<=.45)

    def test_late_control_ack_never_causes_catchup_burst(self):
        clock=[0.0]; sent=[]
        async def sleep(seconds): clock[0]+=seconds
        async def command(peer,label,message,**kwargs):
            sent.append(clock[0])
            if len(sent)==1:
                clock[0]+=.35  # One acknowledgement crosses the next tick.
        async def run_controls():
            owner=probe.Soak(SimpleNamespace(seconds=20),None,None,None,None,{},None)
            owner.anchor=0.0; owner.started.set(); owner.command=command
            await owner.controls()
            return owner.max_curve_lateness_ms
        with patch.object(probe.asyncio,"sleep",new=sleep), \
             patch.object(probe.time,"monotonic",side_effect=lambda:clock[0]):
            late=asyncio.run(run_controls())
        self.assertGreater(late,100)
        self.assertTrue(all(b-a>=.2-1e-9 for a,b in zip(sent,sent[1:])))

    def test_incomplete_render_timing_telemetry_cannot_qualify(self):
        app=CoreApplication()
        state=app.dispatch({"type":"get_state"},1)
        for key in ("duration_max_seconds","min_ring_fill_blocks","histogram"):
            broken=copy.deepcopy(state)
            del broken["health"]["audio"]["render_metrics"][key]
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                probe.strict_snapshot(broken,app.debug)
        broken=copy.deepcopy(state)
        broken["health"]["audio"]["render_metrics"]["histogram"]["cumulative_counts"][-1]=99
        with self.assertRaises(RuntimeError):
            probe.strict_snapshot(broken,app.debug)

    def test_actual_render_metrics_float_ring_fill_contract(self):
        from stave_synth.render_metrics import RenderMetrics
        app = CoreApplication()
        state = app.dispatch({"type": "get_state"}, 1)
        metrics = RenderMetrics(512 / 48000)
        metrics.record(.006, 2)
        state["health"]["audio"]["render_metrics"] = metrics.snapshot()
        self.assertIs(type(metrics.snapshot()["min_ring_fill_blocks"]), float)
        probe.strict_snapshot(state, app.debug)
        for value in (None, True, "2", -1, 6.1, 1.5, float("nan"), float("inf")):
            state["health"]["audio"]["render_metrics"]["min_ring_fill_blocks"] = value
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "ring fill"):
                probe.strict_snapshot(state, app.debug)

    def test_system_probes_fail_closed_on_temperature_reserve_throttle_and_missing_data(self):
        async def check(memory=b"MemAvailable: 700000 kB\n",temperature=b"51000",throttle=b"throttled=0x0\n"):
            child=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(throttle,b"")))
            with patch.object(probe,"process_snapshot",return_value=system_sample()), \
                 patch.object(probe,"file_bytes",side_effect=[memory,temperature]), \
                 patch.object(probe.asyncio,"create_subprocess_exec",new=AsyncMock(return_value=child)):
                return await probe.system_snapshot(1234,Path("/private/owned"),100)
        self.assertEqual(asyncio.run(check())["throttled"],0)
        for kwargs in ({"memory":b"MemAvailable: 100 kB\n"},{"memory":b"MemFree: 700000 kB\n"},
                       {"temperature":b"75000"},{"throttle":b"throttled=0x10000\n"},
                       {"throttle":b"unavailable"}):
            with self.subTest(kwargs=kwargs),self.assertRaises(RuntimeError):
                asyncio.run(check(**kwargs))


if __name__ == "__main__":
    unittest.main()
