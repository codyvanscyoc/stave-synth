"""Pure production-native readiness policy tests."""

import unittest
from types import SimpleNamespace

from stave_synth.native_profile import native_profile_failures, require_native_profile


FaustReverb = type("FaustReverb", (), {"__module__": "stave_synth.faust_reverb"})
FaustOrganEngine = type(
    "FaustOrganEngine", (), {"__module__": "stave_synth.faust_organ"}
)


ALL_FLAGS = {
    "STAVE_FAUST_REVERB": "1",
    "STAVE_FAUST_PING_PONG": "1",
    "STAVE_FAUST_OSC_BANK": "1",
    "STAVE_FAUST_SYMPATHETIC": "1",
    "STAVE_FAUST_PAD_BUS": "1",
    "STAVE_FAUST_MERGED": "1",
    "STAVE_FAUST_MASTER_FX": "1",
    "STAVE_FAUST_BUS_COMP": "1",
    "STAVE_FAUST_ORGAN": "1",
    "STAVE_FAUST_PIANO_CHAIN": "1",
}


def ready_components():
    reverb = FaustReverb()
    reverb.plate_available = True
    reverb.drone_available = True
    synth = SimpleNamespace(
        reverb=reverb,
        _faust_ping_pong=object(),
        _faust_osc_bank=object(),
        _faust_sympathetic=object(),
        _faust_pad_bus=object(),
        _faust_merged=object(),
    )
    piano = SimpleNamespace(
        enabled=True, fs=object(), sfid=0,
        _faust_chain=object(), _piano_room=object(),
    )
    organ = FaustOrganEngine()
    jack = SimpleNamespace(_faust_master_fx=object(), _faust_bus_comp=object())
    return synth, piano, organ, jack


class NativeProfileTests(unittest.TestCase):
    def call(self, function, *, env=None, mode="piano", components=None):
        synth, piano, organ, jack = components or ready_components()
        return function(
            synth=synth, piano=piano, organ=organ, jack=jack,
            instrument_mode=mode, environ=ALL_FLAGS if env is None else env,
        )

    def test_all_requested_backends_ready(self):
        self.assertEqual(self.call(native_profile_failures), ())

    def test_reports_every_requested_fallback_and_dependency(self):
        synth, _, _, jack = ready_components()
        synth.reverb = object()
        synth._faust_ping_pong = None
        synth._faust_osc_bank = None
        synth._faust_sympathetic = None
        synth._faust_pad_bus = None
        synth._faust_merged = None
        jack._faust_master_fx = None
        jack._faust_bus_comp = None
        failures = self.call(
            native_profile_failures,
            components=(synth, None, object(), jack),
        )
        joined = " | ".join(failures)
        for phrase in (
            "FaustReverb", "ping-pong", "oscillator", "sympathetic", "pad bus",
            "merged render", "prerequisite", "master FX", "bus compressor",
            "FaustOrganEngine", "FluidSynth", "piano chain",
        ):
            self.assertIn(phrase, joined)

    def test_room_backends_are_part_of_strict_profile(self):
        synth, piano, organ, jack = ready_components()
        synth.reverb.plate_available = False
        synth.reverb.drone_available = False
        piano._piano_room = None
        failures = self.call(
            native_profile_failures, components=(synth, piano, organ, jack)
        )
        self.assertEqual(len(failures), 3)

    def test_enabled_piano_requires_live_synth_and_valid_soundfont(self):
        synth, piano, organ, jack = ready_components()
        piano.fs = None
        piano.sfid = -1
        failures = self.call(
            native_profile_failures, components=(synth, piano, organ, jack)
        )
        self.assertTrue(any("not loaded" in item for item in failures))
        self.assertTrue(any("valid soundfont" in item for item in failures))

    def test_intentionally_disabled_but_loaded_piano_is_ready(self):
        synth, piano, organ, jack = ready_components()
        piano.enabled = False
        failures = self.call(
            native_profile_failures, components=(synth, piano, organ, jack)
        )
        self.assertFalse(any("FluidSynth is not loaded" in item for item in failures))
        self.assertFalse(any("valid soundfont" in item for item in failures))

    def test_disabled_piano_still_requires_resources_for_later_enable(self):
        synth, piano, organ, jack = ready_components()
        piano.enabled = False
        piano.fs = None
        piano.sfid = None
        failures = self.call(
            native_profile_failures, components=(synth, piano, organ, jack)
        )
        self.assertTrue(any("FluidSynth is not loaded" in item for item in failures))
        self.assertTrue(any("valid soundfont" in item for item in failures))

    def test_non_strict_returns_diagnostics_without_raising(self):
        synth, piano, organ, jack = ready_components()
        synth._faust_ping_pong = None
        env = dict(ALL_FLAGS, STAVE_REQUIRE_NATIVE="0")
        failures = self.call(
            require_native_profile, env=env,
            components=(synth, piano, organ, jack),
        )
        self.assertTrue(failures)

    def test_strict_profile_raises_before_audio_start(self):
        synth, piano, organ, jack = ready_components()
        synth._faust_ping_pong = None
        env = dict(ALL_FLAGS, STAVE_REQUIRE_NATIVE="1")
        with self.assertRaisesRegex(RuntimeError, "Native audio readiness failed"):
            self.call(
                require_native_profile, env=env,
                components=(synth, piano, organ, jack),
            )


if __name__ == "__main__":
    unittest.main()
