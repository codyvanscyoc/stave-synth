import asyncio
import importlib.util
from pathlib import Path
import unittest

import websockets


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_audition", ROOT / "tools" / "run_audition.py")
AUDITION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDITION)


def baseline():
    from stave_synth.config import DEFAULT_STATE
    state = {key: (value.copy() if isinstance(value, dict) else value)
             for key, value in DEFAULT_STATE.items()}
    import copy
    state = copy.deepcopy(state)
    state["piano"]["soundfont"] = "Fluid"
    return state


class AuditionPlanTests(unittest.TestCase):
    def test_setting_ack_requires_the_exact_canonical_value(self):
        class FakeSocket:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def send(self, payload):
                self.sent = payload
                await self.incoming.put(
                    '{"type":"setting_ack","section":"master",'
                    '"param":"volume","value":0.5}')

            async def recv(self):
                return await self.incoming.get()

        async def exercise():
            socket = await AUDITION.CommandSocket(FakeSocket()).start()
            try:
                with self.assertRaises(RuntimeError):
                    await AUDITION.send_command(
                        socket, AUDITION.setting("master", "volume", 0.75))
            finally:
                await socket.stop()
        asyncio.run(exercise())

    def test_dispatcher_continuously_discards_unsolicited_flood(self):
        class FloodSocket:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def send(self, payload):
                for sequence in range(100):
                    await self.incoming.put('{"type":"meter","sequence":%d}' % sequence)
                await self.incoming.put('{"type":"panic_ack","fade_reset":true}')

            async def recv(self):
                return await self.incoming.get()

        async def exercise():
            socket = await AUDITION.CommandSocket(FloodSocket()).start()
            try:
                reply = await AUDITION.send_command(socket, {"type": "panic"})
                self.assertEqual(reply["type"], "panic_ack")
                self.assertEqual(socket.unsolicited_count, 100)
            finally:
                await socket.stop()
        asyncio.run(exercise())

    def test_loopback_dispatcher_drains_while_idle_and_keeps_ping_alive(self):
        async def exercise():
            flooded = asyncio.Event()

            async def handler(websocket):
                for sequence in range(120):
                    await websocket.send('{"type":"meter","sequence":%d}' % sequence)
                # This state-shaped broadcast must not satisfy a later
                # authoritative get_state request because it has no health.
                await websocket.send('{"type":"state","state":{"stale":true}}')
                flooded.set()
                async for raw in websocket:
                    if '"get_state"' in raw:
                        await websocket.send(
                            '{"type":"state","state":{"fresh":true},'
                            '"health":{"native_profile":{"ready":true}}}')

            try:
                server = await websockets.serve(handler, "127.0.0.1", 0,
                                                ping_interval=0.02, ping_timeout=0.1)
            except PermissionError:
                self.skipTest("sandbox forbids even an ephemeral loopback listener")
            try:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(
                        f"ws://127.0.0.1:{port}", max_queue=16,
                        ping_interval=0.02, ping_timeout=0.1) as raw:
                    socket = await AUDITION.CommandSocket(raw).start()
                    try:
                        await flooded.wait()
                        await asyncio.sleep(0.2)
                        self.assertGreaterEqual(socket.unsolicited_count, 121)
                        reply = await AUDITION.send_command(socket, {"type": "get_state"})
                        self.assertEqual(reply["state"], {"fresh": True})
                    finally:
                        await socket.stop()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(asyncio.wait_for(exercise(), 1.5))

    def test_schedule_is_deterministic_sorted_and_has_fixed_bounds(self):
        one = AUDITION.build_schedule()
        two = AUDITION.build_schedule()
        self.assertEqual(one, two)
        self.assertEqual(one, sorted(one, key=lambda event: event[0]))
        self.assertEqual(AUDITION.DURATION_SECONDS, 55.0)
        note_ons = [event for event in one if event[1] & 0xF0 == 0x90 and event[3] > 0]
        self.assertEqual(min(event[0] for event in note_ons), 2.0)
        self.assertLessEqual(max(event[0] for event in one), 47.0)
        self.assertEqual(one[-3], (47.0, 0xB0, 66, 0))
        self.assertEqual(one[-2], (47.0, 0xB0, 64, 0))
        self.assertEqual(one[-1], (47.0, 0xB0, 123, 0))
        active = set()
        for _, status, note, velocity in one:
            if status & 0xF0 == 0x90 and velocity:
                active.add(note)
            elif status & 0xF0 == 0x80 or (status & 0xF0 == 0x90 and not velocity):
                active.discard(note)
        self.assertEqual(active, set())
        for onset in sorted({event[0] for event in note_ons}):
            velocities = [event[3] for event in note_ons if event[0] == onset]
            self.assertEqual(velocities[0], 66)
            self.assertTrue(all(value == 72 for value in velocities[1:]))

    def test_all_case_and_restore_commands_pass_public_schema(self):
        from stave_synth.state_schema import validate_message
        state = baseline()
        for case in AUDITION.CASES:
            commands = AUDITION.case_commands(case, state)
            self.assertTrue(commands)
            for command in commands:
                self.assertEqual(validate_message(command)["type"], "setting")
            for _, command in AUDITION.sweep_plan(case):
                self.assertEqual(validate_message(command)["type"], "setting")
        for command in AUDITION.restore_commands(state):
            self.assertEqual(validate_message(command)["type"], "setting")

    def test_cases_preserve_piano_except_explicit_open_tone(self):
        state = baseline()
        for case in AUDITION.CASES[:-1]:
            piano = [command for command in AUDITION.case_commands(case, state)
                     if command["section"] == "piano"]
            self.assertEqual(piano, [AUDITION.setting("piano", "enabled", True)])
        open_tone = AUDITION.case_commands(AUDITION.CASES[-1], state)
        self.assertIn(AUDITION.setting("piano", "filter_highcut_hz", 12000), open_tone)

    def test_setup_verification_uses_last_absolute_value(self):
        response = {"state": {"synth_pad": {"filter_cutoff_hz": 800}}}
        commands = [AUDITION.setting("synth_pad", "filter_cutoff_hz", 3500),
                    AUDITION.setting("synth_pad", "filter_cutoff_hz", 800)]
        self.assertEqual(AUDITION.verify_state(response, commands), [])

    def test_historical_drop_counts_only_fail_when_they_grow(self):
        def state(midi, controls, over_budget):
            return {"health": {"native_profile": {"ready": True},
                               "audio": {"midi_dropped": midi,
                                         "render_metrics": {"over_budget_count": over_budget}},
                               "controls": {"dropped": controls}}}
        before = state(4, 3, 2)
        self.assertEqual(AUDITION.health_failures(before), [])
        self.assertEqual(AUDITION.counter_growth(before, before,
                                                  {"bridge_xruns": 1, "bridge_underruns": 2},
                                                  {"bridge_xruns": 1, "bridge_underruns": 2}), [])
        after = state(5, 3, 4)
        growth = AUDITION.counter_growth(before, after, {}, {})
        self.assertTrue(any("midi_dropped" in item for item in growth))
        self.assertTrue(any("over_budget_count" in item for item in growth))

    def test_filter_sweep_is_five_hz_and_triangular(self):
        sweep = AUDITION.sweep_plan("03-filter-motion")
        self.assertEqual(len(sweep), 226)
        self.assertEqual(sweep[0][0], 2.0)
        self.assertEqual(sweep[-1][0], 47.0)
        self.assertTrue(all(abs((b[0] - a[0]) - 0.2) < 1e-8 for a, b in zip(sweep, sweep[1:])))
        values = [item[1]["value"] for item in sweep]
        self.assertEqual(values[0], 800.0)
        self.assertEqual(values[-1], 800.0)
        self.assertEqual(max(values), 6500.0)
        # The midpoint is multiplicative (log-frequency), not a linear-Hz ramp.
        self.assertLess(values[len(values) // 4], (800 + 6500) / 2)

    def test_shimmer_build_is_enabled_before_capture_and_uses_absolutes(self):
        commands = AUDITION.case_commands("04-shimmer-build", baseline())
        self.assertIn(AUDITION.setting("synth_pad", "shimmer_enabled", True), commands)
        self.assertTrue(all(command["type"] == "setting" for command in commands))
        self.assertTrue(all(command["type"] == "setting"
                            for _, command in AUDITION.sweep_plan("04-shimmer-build")))
        sweep = AUDITION.sweep_plan("04-shimmer-build")
        self.assertEqual(len(sweep), 226 * 3)
        self.assertAlmostEqual(sweep[0][1]["value"], 0.2)
        self.assertAlmostEqual(sweep[-3][1]["value"], 0.5)
        self.assertAlmostEqual(sweep[-2][1]["value"], 0.3)
        self.assertAlmostEqual(sweep[-1][1]["value"], 0.2)

    def test_runtime_identity_gate_accepts_only_exact_audition_payload(self):
        good = 'window.STAVE_RUNTIME = {"websocket_port": 18765, "instance": "audition"};\n'
        self.assertEqual(asyncio.run(AUDITION.verify_runtime(lambda: good)),
                         {"websocket_port": 18765, "instance": "audition"})
        for source in (
            'window.STAVE_RUNTIME = {"websocket_port": 8765, "instance": "stage"};',
            'window.STAVE_RUNTIME = {"websocket_port": 18765, "instance": "stage"};',
            'window.STAVE_RUNTIME = {"websocket_port": 18765, "instance": "audition", "extra": true};',
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(AUDITION.verify_runtime(lambda source=source: source))

    def test_cli_case_is_closed_enum_and_has_no_endpoint_override(self):
        parsed = AUDITION.parse_args(["--driver", "/tmp/driver", "--output-dir", "/tmp/evidence",
                                      "--case", "01-piano-reference"])
        self.assertFalse(hasattr(parsed, "host"))
        self.assertFalse(hasattr(parsed, "port"))
        with self.assertRaises(SystemExit):
            AUDITION.parse_args(["--driver", "/tmp/driver", "--output-dir", "/tmp/evidence",
                                 "--case", "stage"])


if __name__ == "__main__":
    unittest.main()
