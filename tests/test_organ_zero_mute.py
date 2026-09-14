"""Focused regression for the native organ's exact-zero volume endpoint."""
import unittest

import numpy as np

from tests.test_native_controls import make_organ


class NativeOrganZeroMuteTests(unittest.TestCase):
    @staticmethod
    def _constant_native_output(organ, library, value=0.25):
        def compute(*_args):
            organ._out_l.fill(value)
            organ._out_r.fill(value)

        library.computeStaveOrgan = compute

    def test_zero_fades_one_block_then_is_exactly_silent(self):
        organ, library = make_organ()
        self._constant_native_output(organ, library)

        np.testing.assert_array_equal(organ.render_block(8), 0.25)
        organ.set_volume(0.0)
        organ.set_volume(0.0)  # idempotent scene/control ticks preserve fade

        faded = organ.render_block(8)
        expected = np.tile(np.linspace(0.25, 0.0, 8), (2, 1))
        np.testing.assert_allclose(faded, expected, rtol=0.0, atol=1e-15)
        np.testing.assert_array_equal(organ.render_block(8), 0.0)

    def test_every_positive_volume_leaves_native_output_unchanged(self):
        organ, library = make_organ()
        self._constant_native_output(organ, library)

        organ.set_volume(0.0)
        organ.render_block(8)
        organ.set_volume(np.nextafter(0.0, 1.0))

        np.testing.assert_array_equal(organ.render_block(8), 0.25)

    def test_zero_while_disabled_does_not_leak_when_enabled(self):
        organ, library = make_organ()
        self._constant_native_output(organ, library)

        organ.update_params({"enabled": False, "volume": 0.0})
        organ.update_params({"enabled": True})

        np.testing.assert_array_equal(organ.render_block(8), 0.0)


if __name__ == "__main__":
    unittest.main()
