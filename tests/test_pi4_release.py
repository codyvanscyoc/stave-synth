"""Exercise release assembly from a tagged scratch checkout and a fixture build receipt."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, directory):
    spec = importlib.util.spec_from_file_location(name, directory / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseTests(unittest.TestCase):
    def test_archive_agrees_with_tag_build_and_rejects_stale_engine_and_sources(self):
        package = load("package_pi4", ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (*package.FILES, *(f"faust/{key}.dsp" for key in package.DSP)):
                source, target = ROOT / name, root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copyfile(source, target)
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false",
                "commit", "-qm", "fixture source")
            git("-c", "tag.gpgsign=false", "tag", "fixture")
            engine = root / "engine"
            engine.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00")
            receipt = {"format": 1, "binary_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
                       "source_sha256": load("build_pi4_native", root).source_hashes()}
            record = root / "engine.build.json"
            record.write_text(json.dumps(receipt))
            with patch.object(package, "ROOT", root):
                out = root / "out"
                package.package("fixture", engine, out)
                with tarfile.open(out / "stave-synth-pi4-arm64.tar.gz") as archive:
                    self.assertEqual(archive.extractfile("stave-synth-pi4/bin/audition-jack.build.json").read(),
                                     record.read_bytes())
                receipt["source_sha256"].pop("faust/reverb.dsp")
                record.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError, "sources do not match"):
                    package.package("fixture", engine, root / "bad-source")
                engine.write_bytes(engine.read_bytes() + b"stale")
                with self.assertRaisesRegex(ValueError, "does not match its build receipt"):
                    package.package("fixture", engine, root / "bad-engine")
                self.assertFalse((root / "bad-source").exists())
                self.assertFalse((root / "bad-engine").exists())
