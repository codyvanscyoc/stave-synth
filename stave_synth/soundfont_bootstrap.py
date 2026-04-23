"""First-launch soundfont bootstrap.

Mirrors the download-and-extract step in install-mac.sh / install.sh so a
fresh checkout can produce audible piano without the user running the
installer script first. If a usable soundfont is already present in
SOUNDFONT_DIR or a system search path, this is a no-op.

Default target is Salamander Grand Piano (freepats.zenvoid.org) — same
source the Linux installer uses. ~296 MB compressed → ~1.2 GB on disk.
"""
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .config import SOUNDFONT_DIR
from .paths import soundfont_search_dirs

logger = logging.getLogger(__name__)

# Same URL install-mac.sh + install.sh use. Kept in sync by hand; if this
# ever goes stale the installer scripts would break too and we'd notice.
_SALAMANDER_URL = (
    "https://freepats.zenvoid.org/Piano/SalamanderGrandPiano/"
    "SalamanderGrandPiano-SF2-V3+20200602.tar.xz"
)

# Names the engine accepts as "piano is available". Any one of these
# present (in SOUNDFONT_DIR or a system search path) means we skip the
# download. `_find_soundfont` in fluidsynth_player.py falls back through
# these in order, so matching its list here keeps the two honest.
_ACCEPTED_NAMES = ("Salamander", "FluidR3_GM", "default-GM")
_ACCEPTED_EXTS = (".sf2", ".sf3", ".SF2", ".SF3")


def _already_installed() -> bool:
    """True if any accepted soundfont is reachable via user or system paths."""
    SOUNDFONT_DIR.mkdir(parents=True, exist_ok=True)
    for name in _ACCEPTED_NAMES:
        for ext in _ACCEPTED_EXTS:
            if (SOUNDFONT_DIR / f"{name}{ext}").exists():
                return True
            for d in soundfont_search_dirs():
                if os.path.exists(os.path.join(d, f"{name}{ext}")):
                    return True
    return False


def _download_with_progress(url: str, dest: Path) -> None:
    """Stream URL to dest, logging percent + MB every ~5%.

    Uses urllib so we don't add a runtime dep on requests; the synth's
    render thread never touches this code, so perf doesn't matter."""
    logger.info("Downloading soundfont: %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "stave-synth/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length", "0")) or None
        read = 0
        last_logged_pct = -1
        chunk = 1024 * 256  # 256 KB
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            out.write(buf)
            read += len(buf)
            if total:
                pct = int(read * 100 / total)
                if pct >= last_logged_pct + 5:
                    logger.info("  %3d%% (%.1f / %.1f MB)",
                                pct, read / 1_048_576, total / 1_048_576)
                    last_logged_pct = pct
    logger.info("Download complete: %.1f MB", read / 1_048_576)


def _extract_salamander(tar_path: Path, dest_dir: Path) -> bool:
    """Pull the first .sf2 out of a Salamander tarball, rename it to
    Salamander.sf2, save the license. Returns True on success."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(tar_path) as tf:
            # Python 3.12+ prefers explicit filter; "data" is the safe default
            # (blocks absolute paths, symlinks outside the tree, devices).
            try:
                tf.extractall(tmp_path, filter="data")
            except TypeError:
                tf.extractall(tmp_path)

        sf2 = next(iter(tmp_path.rglob("*.sf2")), None)
        if sf2 is None:
            logger.error("No .sf2 in Salamander archive — aborting install")
            return False

        target = dest_dir / "Salamander.sf2"
        shutil.copy2(sf2, target)
        logger.info("Installed %s (%.1f MB)", target, target.stat().st_size / 1_048_576)

        license_txt = next(iter(tmp_path.rglob("readme.txt")), None)
        if license_txt:
            shutil.copy2(license_txt, dest_dir / "Salamander-LICENSE.txt")
    return True


def ensure_soundfonts() -> bool:
    """Bootstrap a usable soundfont if none exists. Safe to call every startup.

    Returns True if a soundfont is available after the call (either pre-existing
    or newly installed), False if the download/extract failed and the user will
    get a silent piano until they install one manually."""
    if _already_installed():
        return True

    logger.warning(
        "No soundfont found in %s or system paths — downloading Salamander "
        "Grand Piano (~296 MB compressed, ~1.2 GB on disk). First launch only.",
        SOUNDFONT_DIR,
    )
    SOUNDFONT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.xz", delete=False) as f:
        tar_path = Path(f.name)
    try:
        _download_with_progress(_SALAMANDER_URL, tar_path)
        if not _extract_salamander(tar_path, SOUNDFONT_DIR):
            return False
    except Exception as e:
        logger.error("Soundfont bootstrap failed: %s — piano will be silent "
                     "until a .sf2 is placed in %s", e, SOUNDFONT_DIR)
        return False
    finally:
        try:
            tar_path.unlink()
        except Exception:
            pass

    return True
