#!/usr/bin/env python3
"""Read-only release preflight; never starts Stave, DSP, listeners or devices.

Default: files, hashes, runtime identity, package metadata, disk and /proc RAM.
--system-probes additionally runs bounded read-only interpreter/system/device
listings. No installer, service control, mixer writes or JACK connections occur.
This is a prerequisite report, NOT stage/audio/hardware qualification.

Example (use the intended existing virtualenv, do not install anything):
  python3 -B tools/stage_preflight.py --python /path/to/venv/bin/python \
      --manifest docs/pi4/TARGET_BUILD.md --system-probes
Exit 1 means a failed prerequisite; warnings still require operator review.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NATIVE_FILES = ("stave_synth/jack_bridge.so",) + tuple(
    f"faust/libstave_{name}.so" for name in (
        "bus_comp", "drone", "master_fx", "merged_shim", "organ", "osc_bank",
        "osc_bank_lite", "pad_bus", "piano_chain", "piano_room", "ping_pong",
        "plate", "reverb", "sympathetic", "sympathetic_lite"))
SOURCE_FILES = tuple("stave_synth/" + name + ".py" for name in (
    "__init__", "main", "runtime", "config", "state_schema", "state_persistence",
    "state_store", "control_queue", "control_mapping", "preset_manager", "recorder",
    "pad_store", "routing", "ui_recovery", "native_profile", "render_metrics", "midi_handler",
    "websocket_server", "jack_engine", "fluidsynth_player", "synth_engine", "organ_engine",
    "faust_bus_comp", "faust_drone", "faust_master_fx", "faust_merged", "faust_organ",
    "faust_osc_bank", "faust_pad_bus", "faust_piano_chain", "faust_piano_room",
    "faust_ping_pong", "faust_plate", "faust_reverb", "faust_sympathetic")) + (
                "stave-synth.sh", "systemd/stave-synth.service",
                "systemd/stave-synth.service.d/faust.conf", "ui/index.html",
                "ui/script.js", "ui/style.css", "requirements.txt")
DEPENDENCIES = {"numpy": (1, 24), "websockets": (11,), "pyfluidsynth": (1, 3),
                "scipy": (1, 11), "psutil": (5, 9), "cffi": (1, 16)}
FLAGS = tuple("STAVE_FAUST_" + name for name in (
    "REVERB", "PING_PONG", "OSC_BANK", "SYMPATHETIC", "MASTER_FX", "BUS_COMP",
    "ORGAN", "PAD_BUS", "PIANO_CHAIN", "MERGED")) + ("STAVE_REQUIRE_NATIVE",)
FONT_DIRS = (Path("/usr/share/sounds/sf2"), Path("/usr/share/soundfonts"),
             Path("/usr/local/share/soundfonts"))
MAX_TEXT = 4 * 1024 * 1024
MAX_PROBE_OUTPUT = 128 * 1024


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, status, detail, **evidence):
        self.checks.append(dict(name=name, status=status, detail=detail, **evidence))

    def result(self):
        counts = {status: sum(c["status"] == status for c in self.checks)
                  for status in ("pass", "warn", "fail")}
        return {"schema_version": 1, "read_only": True,
                "stage_qualified": False,
                "qualification": "Prerequisites only; no audio, latency, recovery, rehearsal or rollback qualification.",
                "status": "fail" if counts["fail"] else "warn" if counts["warn"] else "pass",
                "counts": counts, "checks": self.checks}


def read_text(path, limit=MAX_TEXT):
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise ValueError("expected a regular file, not a device or pipe")
    with Path(path).open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise ValueError(f"file exceeds {limit} byte read limit")
    return value.decode("utf-8")


def hash_file(path):
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise ValueError("expected a regular artifact, not a device or pipe")
    with Path(path).open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 256 * 1024 * 1024:
            raise ValueError("expected regular release artifact no larger than 256 MiB")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            data = source.read(min(1024 * 1024, remaining))
            if not data:
                raise ValueError("artifact changed while hashing")
            digest.update(data)
            remaining -= len(data)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("artifact changed while hashing")
        return digest.hexdigest()


def parse_manifest(path):
    """Accept sha256sum lines, including the fenced manifest in TARGET_BUILD.md."""
    entries = {}
    for line in read_text(path, 1024 * 1024).splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if not match:
            continue
        digest, name = match.groups()
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("manifest paths must stay inside the checkout")
        if name in entries:
            raise ValueError(f"duplicate manifest path: {name}")
        entries[name] = digest.lower()
    if not entries:
        raise ValueError("no SHA-256 entries found")
    return entries


def check_artifacts(report, root, manifest=None):
    expected = {}
    if manifest is not None:
        try:
            expected = parse_manifest(manifest)
        except (OSError, ValueError) as exc:
            report.add("manifest", "fail", str(exc))
    else:
        report.add("manifest", "warn", "Hashes are inventory only: no trusted expected manifest supplied.")
    for name in dict.fromkeys((*NATIVE_FILES, *SOURCE_FILES, *expected)):
        path = root / name
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("release artifact resolves outside candidate checkout")
            digest = hash_file(path)
            native = name in NATIVE_FILES
            if native:
                with path.open("rb") as source:
                    header = source.read(20)
                if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
                        or int.from_bytes(header[18:20], "little") != 183):
                    raise ValueError("expected ELF64 little-endian AArch64 target artifact")
            if manifest is not None and native and name not in expected:
                raise ValueError("required native artifact is missing from expected manifest")
            if name in expected and expected[name] != digest:
                raise ValueError("SHA-256 does not match expected artifact")
            report.add("artifact:" + name, "pass", "Readable; hash matched" if name in expected
                       else "Readable; hash inventoried, not provenance-verified", sha256=digest)
        except (OSError, ValueError) as exc:
            report.add("artifact:" + name, "fail", str(exc))
    report.add("native_load", "warn", "ELF/hash inspection does not load or verify ABI/dependencies; require separate native-build evidence for these exact artifacts.")


def package_versions(interpreter):
    """Read distribution metadata without importing packages or running Python."""
    if str(interpreter) == str(Path(sys.executable).absolute()):
        search = None
    else:
        prefix = interpreter.parent.parent
        if not (prefix / "pyvenv.cfg").is_file():
            return None
        search = [str(p) for p in (prefix / "lib").glob("python*/site-packages")]
        if not search:
            return None
    distributions = (importlib.metadata.distributions() if search is None
                     else importlib.metadata.distributions(path=search))
    versions = {}
    for item in distributions:
        name = (item.metadata.get("Name") or "").lower().replace("_", "-")
        if name in DEPENDENCIES:
            versions[name] = item.version
    return versions


def check_dependencies(report, interpreter, versions=None):
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        report.add("interpreter", "fail", "Selected Python executable is missing or not executable", path=str(interpreter))
        return
    report.add("interpreter", "pass", "Executable path exists; this does not prove its version or imports", path=str(interpreter), resolved=str(interpreter.resolve()))
    try:
        versions = package_versions(interpreter) if versions is None else versions
        if versions is None:
            report.add("dependencies", "warn", "Cannot inspect this interpreter's package metadata offline; use --system-probes.")
            return
        for name, minimum in DEPENDENCIES.items():
            version = versions.get(name)
            match = re.match(r"^(\d+(?:\.\d+)*)", version or "")
            observed = tuple(int(n) for n in match.group(1).split(".")) if match else ()
            padded = observed + (0,) * max(0, len(minimum) - len(observed))
            status = "pass" if padded >= minimum else "fail"
            report.add("dependency:" + name, status, "Distribution metadata only; native import compatibility not tested", version=version, minimum=".".join(map(str, minimum)))
    except Exception as exc:
        report.add("dependencies", "warn", f"Metadata inspection failed: {exc}")


def find_font(stem, local_dir, system_dirs=FONT_DIRS):
    for directory, extensions in ((local_dir, (".sf2", ".sf3", ".SF2", ".SF3")),
                                  *((p, (".sf2", ".sf3")) for p in system_dirs)):
        for extension in extensions:
            candidate = directory / (stem + extension)
            if candidate.exists():
                return candidate
    return None


def check_font(report, stem, local_dir, system_dirs=FONT_DIRS):
    try:
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", stem) or ".." in stem:
            raise ValueError("unsupported soundfont file stem")
        path = find_font(stem, local_dir, system_dirs)
        if path is None:
            raise ValueError("required named bank absent; do not silently substitute a different stage sound")
        target = path.resolve(strict=True)
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("soundfont target is not a regular file")
        with target.open("rb") as source:
            header = source.read(12)
            size = os.fstat(source.fileno()).st_size
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"sfbk":
            raise ValueError("not a readable RIFF SoundFont bank")
        if size < int.from_bytes(header[4:8], "little") + 8:
            raise ValueError("truncated SoundFont bank")
        report.add("soundfont:" + stem, "pass", "Readable resolved target/header; programs/load and asset backup not verified", path=str(path), target=str(target), bytes=size, symlink=path.is_symlink())
    except (OSError, ValueError) as exc:
        report.add("soundfont:" + stem, "fail", str(exc))


def memory_info(proc_root=Path("/proc")):
    data = {}
    for line in read_text(proc_root / "meminfo", 65536).splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s+(\d+) kB", line)
        if match:
            data[match[1]] = int(match[2]) * 1024
    return data


def check_capacity(report, paths, proc_root):
    try:
        memory = memory_info(proc_root)
        available = memory.get("MemAvailable", 0)
        report.add("memory", "pass" if available >= 384 * 1024 * 1024 else "warn",
                   "Point-in-time capacity only; 384 MiB warning threshold is not a measured synth reserve", **memory)
    except (OSError, ValueError) as exc:
        report.add("memory", "warn", f"MemAvailable unavailable: {exc}")
    for label, path in paths.items():
        try:
            nearest = path
            while not nearest.exists() and nearest != nearest.parent:
                nearest = nearest.parent
            usage = shutil.disk_usage(nearest)
            report.add("disk:" + label, "pass" if usage.free >= 512 * 1024 * 1024 else "warn",
                       "Free filesystem bytes; 512 MiB warning threshold does not reserve recording/backup space", path=str(path), filesystem_at=str(nearest), free_bytes=usage.free)
        except OSError as exc:
            report.add("disk:" + label, "warn", str(exc))


def run_probe(argv, *, timeout=3.0):
    """Bound one fixed read-only command's output and caller wait; never retry.

    Kill only our own newly created probe process group on timeout/output cap.
    An uninterruptible OS process may remain despite SIGKILL; never wait forever.
    """
    env = dict(os.environ, LC_ALL="C", PYTHONDONTWRITEBYTECODE="1", JACK_NO_START_SERVER="1")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("read-only probe timed out; no service changes were made")
            for key, _ in selector.select(min(remaining, .1)):
                part = os.read(key.fileobj.fileno(), 8192)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(part)
                if len(output) > MAX_PROBE_OUTPUT:
                    raise ValueError("read-only probe output exceeded 128 KiB")
        status = process.wait(timeout=max(.001, deadline - time.monotonic()))
        text = output.decode("utf-8", errors="replace")
        if status:
            # Some queries include Environment; never echo raw failed-command
            # output that could contain unrelated secrets.
            raise RuntimeError(f"read-only probe exited {status}")
        return text
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
        process.stdout.close()


def properties(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def check_system(report, root, interpreter, runtime, preferred, service, proc_root, sys_root, runner):
    def probe(name, argv, failure_status="warn"):
        try:
            return runner(argv)
        except Exception as exc:
            report.add(name, failure_status, f"Read-only probe unavailable: {exc}")
            return None

    script = ("import importlib.metadata as m,json,sys,platform; "
              "v={}; "
              "exec('for n in '+repr(" + repr(list(DEPENDENCIES)) + ")+':\\n try: v[n]=m.version(n)\\n except m.PackageNotFoundError: pass'); "
              "print(json.dumps(dict(version=list(sys.version_info[:3]), machine=platform.machine(), prefix=sys.prefix, packages=v)))")
    result = probe("interpreter_probe", [str(interpreter), "-I", "-B", "-c", script], "fail")
    if result is not None:
        try:
            info = json.loads(result)
            report.add("interpreter_probe", "pass" if tuple(info["version"]) >= (3, 10) else "fail",
                       "Executed stdlib metadata query only; no audio libraries imported", version=info["version"], machine=info["machine"], prefix=info["prefix"])
            check_dependencies(report, interpreter, info["packages"])
        except (KeyError, TypeError, ValueError) as exc:
            report.add("interpreter_probe", "fail", f"Invalid interpreter probe response: {exc}")
    fields = ("LoadState", "ActiveState", "SubState", "MainPID", "NRestarts", "WorkingDirectory",
              "FragmentPath", "DropInPaths", "LimitRTPRIO", "LimitMEMLOCK", "MemoryAccounting",
              "MemoryCurrent", "MemoryHigh", "MemoryMax", "Environment")
    result = probe("service", ["systemctl", "--user", "show", service, "--no-pager", "--property=" + ",".join(fields)])
    if result is not None:
        props = properties(result)
        # Never emit arbitrary environment values (they may contain secrets).
        try:
            environment = dict(part.split("=", 1) for part in shlex.split(props.pop("Environment", "")) if "=" in part)
        except ValueError:
            environment = {}
            report.add("service_environment", "warn", "Effective unit environment could not be parsed; raw values withheld")
        flags = {name: environment.get(name) for name in FLAGS}
        report.add("service", "pass" if props.get("ActiveState") == "active" else "warn",
                   "Current unit only; candidate has NOT been deployed or started", unit=service, properties=props)
        report.add("service_checkout", "pass" if props.get("WorkingDirectory") == str(root) else "warn",
                   "Compare current unit's checkout with candidate; mismatch is expected before deployment",
                   candidate=str(root), current=props.get("WorkingDirectory"))
        report.add("service_native_flags", "pass" if all(v not in (None, "", "0", "false", "False") for v in flags.values()) else "warn",
                   "Effective unit environment; backend engagement still needs runtime evidence", flags=flags)
        identity = {name: environment[name] for name in (
            "STAVE_INSTANCE", "STAVE_INSTANCE_ROOT", "STAVE_HTTP_PORT",
            "STAVE_WEBSOCKET_PORT", "STAVE_LOW_RAM") if name in environment}
        report.add("service_identity", "pass" if environment.get("STAVE_INSTANCE", "stage") == runtime.instance else "warn",
                   "Selected preflight identity versus current unit; unset values use application defaults",
                   selected=runtime.instance, current=identity)
        pid = props.get("MainPID", "0")
        if pid.isdigit() and int(pid) > 0:
            try:
                limits = read_text(proc_root / pid / "limits", 65536)
                relevant = [line for line in limits.splitlines() if line.startswith(("Max locked memory", "Max realtime priority"))]
                fields = {parts[0]: parts[1] for line in relevant
                          if len(parts := re.split(r"\s{2,}", line.strip())) >= 3}
                priority = fields.get("Max realtime priority", "0")
                sufficient = (priority == "unlimited" or priority.isdigit() and int(priority) >= 80)
                sufficient = sufficient and fields.get("Max locked memory") == "unlimited"
                report.add("process_limits", "pass" if sufficient else "warn",
                           "Actual running PID soft limits checked for RT >=80 and locked memory unlimited", pid=int(pid), rows=relevant)
            except OSError as exc:
                report.add("process_limits", "warn", str(exc))
            scheduler = probe("scheduler", ["ps", "-L", "-p", pid, "-o", "pid,tid,cls,rtprio,comm"])
            if scheduler is not None:
                report.add("scheduler", "pass" if re.search(r"\b(?:FF|RR)\s+80\b", scheduler) else "warn",
                           "Thread scheduling snapshot; RT80 presence alone does not identify or qualify the render thread", listing=scheduler[:8192])
        else:
            report.add("process_limits", "warn", "No running MainPID to inspect")
    linger = probe("linger", ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"])
    if linger is not None:
        report.add("linger", "pass" if linger.strip() == "yes" else "fail", "User manager boot-without-login prerequisite", value=linger.strip())
    try:
        controllers = read_text(sys_root / "fs/cgroup/cgroup.controllers", 4096).split()
        enabled = "memory" in controllers
        report.add("memory_controller", "pass" if enabled else "warn",
                   "Root cgroup v2 memory controller availability, not proof of unit enforcement", controllers=controllers)
    except OSError:
        try:
            rows = [line.split() for line in read_text(proc_root / "cgroups", 65536).splitlines() if line.startswith("memory")]
            enabled = bool(rows and rows[0][-1] == "1")
            report.add("memory_controller", "pass" if enabled else "warn", "cgroup v1 memory controller availability, not proof of unit enforcement", enabled=enabled)
        except OSError as exc:
            report.add("memory_controller", "warn", str(exc))
    for name, argv in (("usb", ["lsusb"]), ("alsa_audio", ["aplay", "-l"]),
                       ("alsa_midi", ["aconnect", "-i", "-l"]),
                       ("native_dependencies", ["ldconfig", "-p"])):
        listing = probe(name, argv)
        if listing is not None:
            if name == "native_dependencies":
                found = [line.strip() for line in listing.splitlines() if re.search(r"\blib(?:jack|fluidsynth)\.so", line)]
                report.add(name, "pass" if all(any("lib" + key + ".so" in row for row in found) for key in ("jack", "fluidsynth")) else "warn",
                           "Loader-cache inventory only; no library loaded", listing=found)
            else:
                report.add(name, "warn", "OS device inventory only: verify intended keyboard/interface, not root hubs, MIDI Through or onboard audio", listing=listing[:16384])
    listing = probe("jack_ports", ["pw-jack", "jack_lsp", "-t", "-p"])
    if listing is not None:
        from stave_synth.routing import parse_records, stereo_sinks, midi_capture_selection
        records = parse_records(listing)
        sinks = stereo_sinks(records, runtime.jack_client_name)
        midi, _ = midi_capture_selection(records)
        report.add("jack_ports", "pass" if sinks and midi else "warn",
                   "OS-exposed stereo/MIDI ports; does not prove physical device identity or sound", stereo_sinks=sinks, midi_capture=midi)
        report.add("preferred_output", "pass" if preferred in sinks else "warn",
                   "Saved preference is present" if preferred in sinks else "Saved output preference missing or not currently exposed", preferred=preferred)
    try:
        model = read_text(proc_root / "device-tree/model", 4096).rstrip("\x00\n")
        report.add("target_model", "pass" if "Raspberry Pi 4" in model else "warn", "Model inventory; Pi5 is a separate product line", model=model)
    except OSError as exc:
        report.add("target_model", "warn", str(exc))


def collect_preflight(root, interpreter, *, manifest=None, system_probes=False,
                      service="stave-synth.service", environ=None, home=None,
                      proc_root=Path("/proc"), sys_root=Path("/sys"),
                      font_dirs=FONT_DIRS, runner=run_probe):
    report = Report()
    try:
        from stave_synth.runtime import load_runtime
    except ImportError as exc:
        report.add("runtime_identity", "fail", f"Cannot import identity validator: {exc}")
        return report.result()
    root = Path(root).resolve()
    interpreter = Path(interpreter).absolute()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service) or service.startswith("-"):
        report.add("service", "fail", "Invalid service unit name")
        return report.result()
    try:
        runtime = load_runtime(environ=environ, home=home)
        report.add("runtime_identity", "pass", "Validated identity; no directories or lock files created",
                   instance=runtime.instance, config_dir=str(runtime.config_dir), data_dir=str(runtime.data_dir),
                   jack_client=runtime.jack_client_name, host=runtime.host,
                   http_port=runtime.http_port, websocket_port=runtime.websocket_port)
    except (ValueError, OSError) as exc:
        report.add("runtime_identity", "fail", str(exc))
        return report.result()
    report.add("candidate", "pass" if root.is_dir() else "fail", "Candidate files only; no deployment performed", root=str(root), host_platform=platform.platform())
    check_artifacts(report, root, manifest)
    check_dependencies(report, interpreter)
    preferred = None
    selected = "Fluid"
    state_file = runtime.config_dir / "current_state.json"
    try:
        from stave_synth.state_schema import normalize_state, DEFAULT_STATE
        selected = DEFAULT_STATE["piano"]["soundfont"]
        payload = read_text(state_file)
        raw = json.loads(payload)
        state = normalize_state(raw)
        selected = state["piano"]["soundfont"]
        preferred = state["master"].get("audio_output_pref")
        report.add("saved_state", "pass", "Tool-checkout schema accepted read-only; normalization was NOT saved", path=str(state_file), sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(), soundfont=selected, schema_checkout=str(ROOT))
    except FileNotFoundError:
        report.add("saved_state", "warn", "No current state: startup uses profile defaults; preserve intended favorite sound before release", path=str(state_file))
    except (ImportError, OSError, ValueError, TypeError, RecursionError) as exc:
        report.add("saved_state", "fail", f"Current state rejected; do not deploy assuming the intended sound: {exc}")
    for stem in dict.fromkeys(("FluidR3_GM", "Salamander" if selected == "Salamander" else "FluidR3_GM")):
        check_font(report, stem, runtime.data_dir / "soundfonts", font_dirs)
    check_capacity(report, {"candidate": root, "runtime_data": runtime.data_dir}, proc_root)
    if system_probes:
        check_system(report, root, interpreter, runtime, preferred, service, proc_root, sys_root, runner)
    else:
        report.add("system_probes", "warn", "Not run. --system-probes explicitly permits read-only service/interpreter/device listings; no devices or listeners opened.")
    return report.result()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, help="Existing intended virtualenv interpreter; default ROOT/venv/bin/python")
    parser.add_argument("--manifest", type=Path, help="Trusted SHA256 manifest or TARGET_BUILD.md")
    parser.add_argument("--system-probes", action="store_true")
    parser.add_argument("--service", default="stave-synth.service")
    parser.add_argument("--text", action="store_true", help="Human-readable report instead of JSON")
    args = parser.parse_args(argv)
    result = collect_preflight(args.root, args.python or args.root / "venv/bin/python",
                               manifest=args.manifest, system_probes=args.system_probes, service=args.service)
    if args.text:
        print("Stave read-only preflight:", result["status"].upper())
        print(result["qualification"])
        for item in result["checks"]:
            print(f"{item['status'].upper():4} {item['name']}: {item['detail']}")
            for key, value in item.items():
                if key not in ("name", "status", "detail"):
                    print(f"     {key}: {json.dumps(value, ensure_ascii=False)}")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["counts"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
