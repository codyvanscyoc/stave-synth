"""Runtime identity, isolation, and single-owner locking.

Loading configuration is side-effect free.  Directories and a lock file are
created only when :class:`InstanceLock` is acquired.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


_INSTANCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DEFAULT_HTTP_PORT = 8080
_DEFAULT_WEBSOCKET_PORT = 8765


@dataclass(frozen=True)
class RuntimeConfig:
    instance: str
    isolated: bool
    config_dir: Path
    data_dir: Path
    host: str
    http_port: int
    websocket_port: int
    jack_client_name: str


def _parse_isolated_port(environ: Mapping[str, str], name: str) -> int:
    raw = environ.get(name)
    if raw is None or not raw:
        raise ValueError(f"{name} is required for an isolated instance")
    try:
        port = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise ValueError(f"{name} must be between 1024 and 65535")
    if port in {_DEFAULT_HTTP_PORT, _DEFAULT_WEBSOCKET_PORT}:
        raise ValueError(f"{name} must not use a production port")
    return port


def _contains_or_is_contained(left: Path, right: Path) -> bool:
    """Return true when two resolved paths overlap by ancestry."""

    return left == right or left in right.parents or right in left.parents


def load_runtime(
    environ: Optional[Mapping[str, str]] = None, home: Optional[os.PathLike[str] | str] = None
) -> RuntimeConfig:
    """Load and validate the stage or explicitly isolated runtime identity."""

    env = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)
    home_path = home_path.resolve(strict=False)
    stage_config = (home_path / ".config" / "stave-synth").resolve(strict=False)
    stage_data = (home_path / ".local" / "share" / "stave-synth").resolve(strict=False)

    instance = env.get("STAVE_INSTANCE", "stage")
    if instance == "stage":
        if any(key in env for key in (
            "STAVE_INSTANCE_ROOT", "STAVE_HTTP_PORT", "STAVE_WEBSOCKET_PORT"
        )):
            raise ValueError("test paths/ports require an explicit non-stage STAVE_INSTANCE")
        return RuntimeConfig(
            instance="stage",
            isolated=False,
            config_dir=stage_config,
            data_dir=stage_data,
            host="0.0.0.0",
            http_port=_DEFAULT_HTTP_PORT,
            websocket_port=_DEFAULT_WEBSOCKET_PORT,
            jack_client_name="StaveSynth",
        )

    if not _INSTANCE_RE.fullmatch(instance):
        raise ValueError(
            "STAVE_INSTANCE must match lowercase [a-z][a-z0-9_-]{0,31}"
        )

    raw_root = env.get("STAVE_INSTANCE_ROOT")
    if raw_root is None or not raw_root:
        raise ValueError("STAVE_INSTANCE_ROOT is required for an isolated instance")
    root_input = Path(raw_root)
    if not root_input.is_absolute():
        raise ValueError("STAVE_INSTANCE_ROOT must be an absolute path")
    root = root_input.resolve(strict=False)
    if root == home_path or root in home_path.parents:
        raise ValueError("STAVE_INSTANCE_ROOT must not be the home directory or its ancestor")

    config_dir = (root / "config").resolve(strict=False)
    data_dir = (root / "data").resolve(strict=False)
    if _contains_or_is_contained(config_dir, data_dir):
        raise ValueError("isolated config and data paths must not overlap each other")
    for candidate in (config_dir, data_dir):
        for production_dir in (stage_config, stage_data):
            if _contains_or_is_contained(candidate, production_dir):
                raise ValueError(
                    "isolated config/data paths must not overlap production paths"
                )

    http_port = _parse_isolated_port(env, "STAVE_HTTP_PORT")
    websocket_port = _parse_isolated_port(env, "STAVE_WEBSOCKET_PORT")
    if http_port == websocket_port:
        raise ValueError("STAVE_HTTP_PORT and STAVE_WEBSOCKET_PORT must be distinct")

    return RuntimeConfig(
        instance=instance,
        isolated=True,
        config_dir=config_dir,
        data_dir=data_dir,
        host="127.0.0.1",
        http_port=http_port,
        websocket_port=websocket_port,
        jack_client_name=f"StaveSynth_{instance}",
    )


class InstanceLock:
    """Non-blocking, descriptor-scoped advisory lock for one runtime."""

    def __init__(self, runtime: RuntimeConfig):
        self.runtime = runtime
        self._fd: Optional[int] = None

    @property
    def path(self) -> Path:
        return self.runtime.config_dir / "instance.lock"

    def acquire(self) -> "InstanceLock":
        if self._fd is not None:
            raise RuntimeError(f"instance {self.runtime.instance!r} lock is already held")
        self.runtime.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError(
                f"instance {self.runtime.instance!r} is already running"
            ) from exc
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"cannot acquire lock for instance {self.runtime.instance!r}: {exc}"
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _description(runtime: RuntimeConfig) -> dict[str, object]:
    return {
        "instance": runtime.instance,
        "isolated": runtime.isolated,
        "config_dir": str(runtime.config_dir),
        "data_dir": str(runtime.data_dir),
        "host": runtime.host,
        "http_port": runtime.http_port,
        "websocket_port": runtime.websocket_port,
        "jack_client_name": runtime.jack_client_name,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Describe a Stave Synth runtime identity")
    parser.add_argument("--describe", action="store_true", help="print runtime JSON")
    args = parser.parse_args(argv)
    if not args.describe:
        parser.error("--describe is required")
    print(json.dumps(_description(load_runtime()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
