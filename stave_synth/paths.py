"""Platform-appropriate filesystem paths for Stave Synth.

Linux: XDG layout — `~/.config/stave-synth/` for config, `~/.local/share/
stave-synth/` for data. Installer writes systemd units under the same tree.

macOS: Apple Human Interface Guidelines collapse both into
`~/Library/Application Support/stave-synth/` (Apple doesn't distinguish
config vs. data at the user level — that's a Unix thing).

Windows: not currently targeted; would be %APPDATA%\\stave-synth\\.
"""
import sys
from pathlib import Path

_APP = "stave-synth"


def config_dir() -> Path:
    """User config directory — holds current_state.json + presets/."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP
    return Path.home() / ".config" / _APP


def data_dir() -> Path:
    """User data directory — holds soundfonts, recordings, pad samples."""
    if sys.platform == "darwin":
        # Apple convention: config and data share Application Support.
        return Path.home() / "Library" / "Application Support" / _APP
    return Path.home() / ".local" / "share" / _APP


def soundfont_search_dirs() -> list[str]:
    """System-wide soundfont install locations to probe if a named soundfont
    isn't already in the user's data dir. Ordered most-likely-first."""
    if sys.platform == "darwin":
        # Homebrew prefixes: /opt/homebrew on Apple Silicon, /usr/local on Intel.
        return [
            "/opt/homebrew/share/sounds/sf2",
            "/opt/homebrew/share/soundfonts",
            "/usr/local/share/sounds/sf2",
            "/usr/local/share/soundfonts",
        ]
    # Linux — matches the apt package layouts for fluid-soundfont-gm etc.
    return [
        "/usr/share/sounds/sf2",
        "/usr/share/soundfonts",
        "/usr/local/share/soundfonts",
    ]
