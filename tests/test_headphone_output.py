"""Headphone mixer validation and persistence without hardware changes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('headphone_stage_test', ROOT/'tools/native_v2_stage.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

class HeadphoneTests(unittest.TestCase):
    def test_invalid_values_never_touch_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = stage.HeadphoneMixer(Path(directory)/'output.json')
            with patch.object(mixer, 'run') as run:
                for data in ({'volume': True, 'muted': False}, {'volume': 101, 'muted': False},
                             {'volume': -1, 'muted': False}, {'volume': 85, 'muted': 0},
                             {'volume': '85', 'muted': False}, {'volume': 85}):
                    with self.assertRaises(ValueError): mixer.set(data)
                run.assert_not_called()

    def test_actual_mixer_state_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'output.json'
            with patch.object(stage.HeadphoneMixer, 'run', return_value={'volume':84,'muted':False}) as run:
                mixer = stage.HeadphoneMixer(path)
                self.assertEqual(mixer.set({'volume':85,'muted':False})['volume'],84)
                run.assert_called_with('sset','PCM','85%','unmute')
                stage.HeadphoneMixer(path)
                run.assert_called_with('sset','PCM','84%','unmute')

    def test_unavailable_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = stage.HeadphoneMixer(Path(directory)/'output.json')
            with patch.object(mixer,'run',side_effect=ValueError('unavailable')):
                self.assertFalse(mixer.get()['available'])

    def test_storage_failure_reports_live_level_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = stage.HeadphoneMixer(Path(directory)/'output.json')
            value = {'volume': 41, 'muted': True}
            with patch.object(mixer, 'run', return_value=value):
                with patch.object(stage.storage.atomic, 'atomic_write_json', side_effect=OSError('disk full')):
                    result = mixer.set(value)
                self.assertEqual(result['volume'], 41)
                self.assertIn('saving', result['warning'])
                self.assertEqual(mixer.set(value), dict(available=True, **value))

    def test_oversized_state_is_ignored_without_touching_the_mixer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'output.json'
            path.write_bytes(b' ' * 1025)
            with patch.object(stage.HeadphoneMixer, 'run') as run:
                self.assertIn('Oversized', stage.HeadphoneMixer(path).warning)
                run.assert_not_called()

    def test_only_named_pi_card_and_pcm_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = stage.HeadphoneMixer(Path(directory)/'output.json')
            with patch.object(stage.subprocess,'run') as run:
                run.return_value.returncode=0
                run.return_value.stdout='Mono: Playback -1196 [85%] [-11.96dB] [on]'
                self.assertEqual(mixer.get()['volume'],85)
                self.assertEqual(run.call_args.args[0],['/usr/bin/amixer','-c','Headphones','sget','PCM'])

if __name__ == '__main__': unittest.main()
