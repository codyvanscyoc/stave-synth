"""Bounded JACK discovery and verified own-client routing transactions.

All execution is injected through ``run`` except the bounded command wrapper.
The wrapper bounds caller wait and admitted workers, not cancellation of an
uninterruptible process. A timed-out unfinished job blocks further admission
until it finishes, preventing late commands from racing newer transactions.
"""

from __future__ import annotations

import re
import subprocess
import threading


_WORKERS_LOCK = threading.Lock()
_WORKERS = {}
MAX_COMMAND_WORKERS = 2


def run_bounded_command(cmd, timeout_s=3.0, hard_timeout_s=5.0):
    token = object()
    job = {"timed_out": False, "result": None}
    with _WORKERS_LOCK:
        if len(_WORKERS) >= MAX_COMMAND_WORKERS or any(item["timed_out"] for item in _WORKERS.values()):
            return None
        _WORKERS[token] = job

    def run():
        try:
            job["result"] = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except Exception:
            job["result"] = None
        finally:
            with _WORKERS_LOCK:
                _WORKERS.pop(token, None)

    worker = threading.Thread(target=run, daemon=True, name="stave-routing-command")
    try:
        worker.start()
    except Exception:
        with _WORKERS_LOCK:
            _WORKERS.pop(token, None)
        return None
    worker.join(max(0.0, hard_timeout_s))
    with _WORKERS_LOCK:
        if token in _WORKERS:
            job["timed_out"] = True
            return None
    return job["result"]


class RoutingError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def _command(run, operation, *args):
    result = run(["pw-jack", operation, *args], timeout_s=3.0, hard_timeout_s=5.0)
    if result is None:
        raise RoutingError("JACK command timed out or command workers are still pending", uncertain=True)
    return result


def _listing(run, *args):
    result = _command(run, "jack_lsp", *args)
    if result.returncode != 0:
        raise RoutingError(f"JACK discovery failed: {(result.stderr or '').strip()[:300]}")
    return result.stdout or ""


def parse_records(text):
    records = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if current is not None:
                records[current].append(line.strip())
        else:
            current = line.strip()
            records[current] = []
    return records


def read_ports(run):
    return parse_records(_listing(run, "-t", "-p"))


def read_connections(run):
    return {name: set(connections) for name, connections in parse_records(_listing(run, "-c")).items()}


def _audio_direction(rows, direction):
    return (any("audio" in row.lower() and "midi" not in row.lower() for row in rows)
            and any(re.search(r"\b" + direction + r"\b", row.lower())
                    for row in rows if "properties:" in row.lower()))


def stereo_sinks(records, client):
    sinks = {}
    for name, rows in records.items():
        if not _audio_direction(rows, "input") or ":" not in name:
            continue
        device, suffix = name.rsplit(":", 1)
        if device == client:
            continue
        for left, right in (("playback_FL", "playback_FR"), ("playback_1", "playback_2")):
            peer = f"{device}:{right}"
            if suffix == left and peer in records and _audio_direction(records[peer], "input"):
                # Prefer explicit FL/FR if a device exports both naming styles.
                if device not in sinks or left == "playback_FL":
                    sinks[device] = (name, peer)
    return sinks


def own_audio_connections(graph, client):
    return {f"{client}:out_L": set(graph.get(f"{client}:out_L", ())),
            f"{client}:out_R": set(graph.get(f"{client}:out_R", ()))}


def route_matches(graph, client, pair):
    return (graph.get(f"{client}:out_L", set()) == {pair[0]}
            and graph.get(f"{client}:out_R", set()) == {pair[1]})


def _edge(run, operation, source, target, should_stop):
    if should_stop():
        raise RoutingError("Routing stopped during application shutdown")
    result = _command(run, operation, source, target)
    graph = read_connections(run)
    connected = target in graph.get(source, set())
    expected = operation == "jack_connect"
    if connected != expected:
        detail = (result.stderr or "").strip()[:300]
        raise RoutingError(f"{operation} did not verify: {source} -> {target}" + (f": {detail}" if detail else ""))
    return graph


def switch_stereo(run, client, target, should_stop=lambda: False):
    """Verify the new pair before removing old links; recover known failures."""
    ports = read_ports(run)
    sinks = stereo_sinks(ports, client)
    if target not in sinks:
        raise RoutingError("Selected stereo output is unavailable; existing route was kept")
    sources = (f"{client}:out_L", f"{client}:out_R")
    if any(not _audio_direction(ports.get(source, []), "output") for source in sources):
        raise RoutingError("This synth's stereo JACK output ports are unavailable")
    pair = sinks[target]
    graph = read_connections(run)
    old = own_audio_connections(graph, client)
    desired = dict(zip(sources, pair))
    attempted_new = []
    try:
        for source, sink in desired.items():
            if sink not in old[source]:
                attempted_new.append((source, sink))
                _edge(run, "jack_connect", source, sink, should_stop)
        graph = read_connections(run)
        if any(sink not in graph.get(source, set()) for source, sink in desired.items()):
            raise RoutingError("New stereo pair disappeared before verification; old route was kept")
        for source, connections in old.items():
            for sink in sorted(connections - {desired[source]}):
                _edge(run, "jack_disconnect", source, sink, should_stop)
        if not route_matches(read_connections(run), client, pair):
            raise RoutingError("Final stereo route did not verify")
        return {"success": True, "ports": list(pair), "changed": old != {key: {value} for key, value in desired.items()}}
    except Exception as exc:
        uncertain = bool(getattr(exc, "uncertain", False))
        recovered = False
        rollback_error = None
        if not uncertain:
            try:
                graph = read_connections(run)
                # Restore original destinations before retiring new links.
                for source, connections in old.items():
                    for sink in sorted(connections - graph.get(source, set())):
                        _edge(run, "jack_connect", source, sink, lambda: False)
                graph = read_connections(run)
                if not all(connections <= graph.get(source, set()) for source, connections in old.items()):
                    raise RoutingError("Original stereo links could not be restored")
                for source, sink in attempted_new:
                    if sink not in old[source] and sink in graph.get(source, set()):
                        _edge(run, "jack_disconnect", source, sink, lambda: False)
                graph = read_connections(run)
                recovered = (all(connections <= graph.get(source, set()) for source, connections in old.items())
                             and all(sink not in graph.get(source, set()) for source, sink in attempted_new))
            except Exception as rollback:
                rollback_error = str(rollback)
                uncertain = uncertain or bool(getattr(rollback, "uncertain", False))
        return {"success": False, "error": str(exc), "uncertain": uncertain,
                "rollback_complete": recovered, "rollback_error": rollback_error}


def _normal_name(value):
    return re.sub(r"\s+", " ", value.strip()).casefold()


def midi_capture_selection(records):
    """Deduplicate unambiguous bridge aliases, not all PipeWire devices.

    Port names do not prove hardware identity for two identically named
    devices. Ambiguous aliases are retained instead of dropping a controller.
    Per-port matching preserves multi-port keyboards/interfaces.
    """
    a2j = {}
    pipewire = {}
    for name, rows in records.items():
        if "midi through" in name.casefold() or not any("midi" in row.casefold() for row in rows):
            continue
        if name.startswith("a2j:") and "(capture)" in name.casefold():
            body = name[4:]
            match = re.match(r"^(.*?)\s*(?:\[(\d+)\])?\s*\(capture\)\s*:\s*(.+)$", body, re.I)
            if match:
                device, identity, port = match.groups()
                a2j[name] = (_normal_name(device), identity, _normal_name(port))
            else:
                a2j[name] = (_normal_name(body), None, _normal_name(body))
        elif name.startswith("Midi-Bridge:") and re.search(r"\(capture(?:_\d+)?\)", name, re.I):
            body = name[len("Midi-Bridge:"):]
            match = re.match(r"^(.*?)\s*:\s*\(capture(?:_\d+)?\)\s*(.+)$", body, re.I)
            if match:
                pipewire[name] = tuple(_normal_name(part) for part in match.groups())
            else:
                pipewire[name] = (None, _normal_name(re.sub(r"\s*\(capture\)\s*$", "", body, flags=re.I)))
    matches = {}
    for pw, (device, port) in pipewire.items():
        matches[pw] = []
        for legacy, (old_device, _identity, old_port) in a2j.items():
            if ((device is not None and device == old_device and port == old_port)
                    or (device is None and port in {old_device, old_port})):
                matches[pw].append(legacy)
    duplicates = {}
    for pw, candidates in matches.items():
        if len(candidates) != 1:
            continue
        legacy = candidates[0]
        if sum(legacy in values for values in matches.values()) == 1:
            duplicates.setdefault(legacy, []).append(pw)
    redundant = {name for values in duplicates.values() for name in values}
    return list(a2j) + [name for name in pipewire if name not in redundant], duplicates


def connect_midi_sources(run, client, ports, duplicates, should_stop=lambda: False):
    midi_input = f"{client}:midi_in"
    graph = read_connections(run)
    any_connected = False
    for source in ports:
        if should_stop():
            break
        aliases = duplicates.get(source, [])
        existing_alias = next((alias for alias in aliases if midi_input in graph.get(alias, set())), None)
        if midi_input not in graph.get(source, set()):
            if existing_alias is not None:
                # Keep an already-working physical-device alias instead of
                # briefly doubling its MIDI events while changing bridges.
                any_connected = True
                continue
            try:
                graph = _edge(run, "jack_connect", source, midi_input, should_stop)
            except RoutingError as exc:
                if exc.uncertain:
                    raise
                continue
        any_connected = True
        for alias in aliases:
            if midi_input in graph.get(alias, set()):
                graph = _edge(run, "jack_disconnect", alias, midi_input, should_stop)
    return any_connected
