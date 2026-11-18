#!/usr/bin/env python3
"""Package a committed native Pi4 release and its separately built ARM64 engine."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.txt", "README.md", "docs/pi4/SHARE_REVIEW.md",
    "install-pi4.sh", "systemd/stave-pi4-share.service", "config/pi4/52-stave-pi4-clock.conf",
    "tools/build_pi4_native.py", "tools/native_v2_stage.py", "tools/native_v2_audition.py",
    "tools/native_v2_lifecycle.py", "native_v2/audition.html", "native_v2/bed_assets.py",
    "native_v2/control_store.py", "stave_synth/state_store.py", "ui/logo.png",
    "native_v2/include", "native_v2/src", "faust/faust_cprelude.h",
)
DSP = ("osc_bank", "piano_chain", "pad_bus", "piano_room", "ping_pong",
       "reverb", "plate", "drone", "master_fx", "bus_comp")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def add(archive, name, data, mode, timestamp):
    member = tarfile.TarInfo(f"stave-synth-pi4/{name}")
    member.size = len(data)
    member.mode = mode
    member.mtime = timestamp
    archive.addfile(member, io.BytesIO(data))


def package(revision, binary, output):
    engine = binary.read_bytes()
    if engine[:6] != b"\x7fELF\x02\x01" or engine[18:20] != b"\xb7\x00":
        raise ValueError("Expected a little-endian ARM64 Linux executable")
    commit = git("rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    timestamp = int(git("show", "-s", "--format=%ct", commit))
    paths = (*FILES, *(f"faust/{module}.dsp" for module in DSP))
    manifest = git("ls-tree", "-rz", "--name-only", commit, "--", *paths).decode().split("\0")
    for path in paths:
        if not any(name == path or name.startswith(path + "/") for name in manifest):
            raise ValueError(f"Missing release input {path} at {commit}")
    output.mkdir(parents=True, exist_ok=False)
    artifact = output / "stave-synth-pi4-arm64.tar.gz"
    with artifact.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as archive:
            for name in sorted(filter(None, manifest)):
                add(archive, name, git("show", f"{commit}:{name}"),
                    0o755 if name == "install-pi4.sh" else 0o644, timestamp)
            add(archive, "bin/audition-jack", engine, 0o755, timestamp)
            add(archive, "SOURCE_COMMIT", (commit + "\n").encode(), 0o644, timestamp)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    package(args.revision, args.binary, args.output)
