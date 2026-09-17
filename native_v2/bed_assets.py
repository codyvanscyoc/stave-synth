"""Off-audio WAV preparation for a native immutable startup bank.

No engine/runtime import or home-directory default. Decode/normalization and
polyphase resampling follow SamplePlayer.prepare; tests use that original as
an independent oracle. Never normalize loud files or retune a missing key.
The native reader independently enforces format, finite PCM and byte limits.
"""
import math
import os
from pathlib import Path
import stat
import struct

import numpy as np

FILENAMES = tuple(f"pad_{key}.wav" for key in ("C", "Cs", "D", "Ds", "E", "F", "Fs", "G", "Gs", "A", "As", "B"))
SLOT_BYTES = 32 * 1024 * 1024
BANK_BYTES = 128 * 1024 * 1024
SOURCE_BYTES = 64 * 1024 * 1024
PREPARE_BYTES = 128 * 1024 * 1024


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def decode_wave(path, available=SLOT_BYTES):
    """Return immutable native48k stereo arrays, checking limits before decode."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > SOURCE_BYTES:
            raise ValueError("Pad requires a regular WAV no larger than64MiB")
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Expected little-endian RIFF WAVE")
        end = struct.unpack("<I", header[4:8])[0] + 8
        if end > info.st_size or end < 12:
            raise ValueError("Invalid RIFF size")
        fmt = payload = None
        chunks = 0
        while stream.tell() + 8 <= end:
            chunk, size = struct.unpack("<4sI", stream.read(8))
            offset = stream.tell()
            if offset + size + (size & 1) > end:
                raise ValueError("Truncated WAV chunk")
            chunks += 1
            if chunks > 1024:
                raise ValueError("Too many WAV chunks")
            if chunk == b"fmt ":
                if fmt is not None or size < 16:
                    raise ValueError("Invalid/duplicate WAV format")
                fmt = stream.read(min(size, 40))
            elif chunk == b"data":
                if payload is not None:
                    raise ValueError("Duplicate WAV payload")
                payload = offset, size
            stream.seek(offset + size + (size & 1))
        if fmt is None or payload is None or stream.tell() != end:
            raise ValueError("Missing WAV chunks or partial chunk header")
        tag, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == 0xFFFE:
            if (len(fmt) < 40 or struct.unpack("<H", fmt[16:18])[0] < 22 or
                    not 0 < struct.unpack("<H", fmt[18:20])[0] <= bits or
                    fmt[28:40] != b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"):
                raise ValueError("Unsupported extensible WAV format")
            tag = struct.unpack("<I", fmt[24:28])[0]
        if (channels not in (1, 2) or not 8000 <= rate <= 192000 or tag not in (1, 3) or
                (tag == 1 and bits not in (8, 16, 24, 32)) or (tag == 3 and bits not in (32, 64))):
            raise ValueError("Unsupported WAV format/rate/channels")
        if align != channels * (bits // 8) or byte_rate != rate * align:
            raise ValueError("Invalid WAV alignment/rate")
        offset, size = payload
        if size % align:
            raise ValueError("Partial WAV frame")
        frames = size // align
        output_frames = (frames * 48000 + rate - 1) // rate
        if min(frames, output_frames) < 4 or output_frames * 16 > min(SLOT_BYTES, available):
            raise ValueError("Native stereo pad exceeds slot/bank budget or is too short")
        divisor = math.gcd(rate, 48000)
        up, down = 48000 // divisor, rate // divisor
        if max(up, down) > 1024:
            raise ValueError("Resampling ratio exceeds preparation bound")
        dtype = np.float64 if rate != 48000 or bits == 64 or (tag == 1 and bits == 32) else np.float32
        output_bytes = output_frames * channels * np.dtype(dtype).itemsize
        working = size + frames * channels * np.dtype(dtype).itemsize + output_bytes + 1024*1024
        if bits == 24:
            working += frames * channels * 8
        if rate != 48000:
            working += output_bytes * 2 + max(up, down) * 256
        if working > PREPARE_BYTES:
            raise ValueError("WAV decode exceeds preparation memory budget")
        stream.seek(offset)
        data = stream.read(size)
        if len(data) != size or signature(info) != signature(os.fstat(stream.fileno())):
            raise ValueError("WAV changed during preparation")
    if tag == 3:
        decoded = np.frombuffer(data, dtype=f"<f{bits//8}").astype(dtype)
    elif bits == 24:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        signed = raw[:, 0].astype(np.int32)
        signed |= raw[:, 1].astype(np.int32) << 8
        signed |= raw[:, 2].astype(np.int32) << 16
        signed = (signed ^ 0x800000) - 0x800000
        decoded = signed.astype(dtype) / 8388608.0
        del raw, signed
    else:
        decoded = np.frombuffer(data, dtype=np.uint8 if bits == 8 else f"<i{bits//8}").astype(dtype)
        if bits == 8:
            decoded -= 128.0
        decoded /= float(1 << (bits - 1))
    del data
    if not np.isfinite(decoded).all():
        raise ValueError("Nonfinite WAV audio")
    decoded = decoded.reshape(frames, channels)
    left, right = decoded[:, 0], decoded[:, 1] if channels == 2 else decoded[:, 0]
    if rate != 48000:
        from scipy.signal import resample_poly
        left = resample_poly(left, up, down)
        right = resample_poly(right, up, down) if channels == 2 else left
    left = np.ascontiguousarray(left, dtype=dtype)
    right = np.ascontiguousarray(right, dtype=dtype) if channels == 2 else left
    if (left.size != output_frames or right.size != left.size or
            not np.isfinite(left).all() or not np.isfinite(right).all()):
        raise ValueError("Invalid resampled audio")
    left.setflags(write=False)
    right.setflags(write=False)
    return left, right


def prepare_bank(directory, destination):
    """Create an exclusive private bundle; absent slots stay absent.

    Any invalid present asset aborts the entire new bank. Caller uses a private
    TemporaryDirectory and launches native audio only after success. This never
    writes the source library or replaces a running bank. No AST runtime imports.
    """
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Explicit real pad-library directory required")
    loaded, resident = [], 0
    # Even if preparation fails, never mistake a partial bundle for ready:
    # caller receives no successful result and native validates complete length.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(struct.pack("<8sII", b"STVPEND1", 48000, 0))
        for slot, filename in enumerate(FILENAMES):
            path = directory / filename
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            left, right = decode_wave(path, BANK_BYTES - resident)
            out.write(struct.pack("<IIQ", slot, 0, left.size))
            # Small fixed export chunks avoid an extra full interleaved copy.
            for start in range(0, left.size, 4096):
                count = min(4096, left.size - start)
                block = np.empty((count, 2), dtype="<f8")
                block[:, 0], block[:, 1] = left[start:start+count], right[start:start+count]
                out.write(block.tobytes())
            resident += left.size * 16
            loaded.append({"slot": slot, "key": filename[4:-4], "frames": left.size})
            del left, right
        out.seek(0)
        out.write(struct.pack("<8sII", b"STVBANK1", 48000, len(loaded)))
        out.flush()
        os.fsync(out.fileno())
    return {"slots": loaded, "pcm_bytes": resident, "rate": 48000}
