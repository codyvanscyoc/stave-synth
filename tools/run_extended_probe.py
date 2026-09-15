#!/usr/bin/env python3
"""Bounded private-audition macro/preset/reverb capture, never stage qualification.

Only one explicitly empty preset slot and one empty macro are used. Existing
setlists are read-only: their preservation is NOT a functional setlist test.
The supervisor owns service lifecycle and recovery from a killed coordinator.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Schema/config imports do not construct audio engines or create directories.
from stave_synth.state_schema import normalize_state
from tools.run_audition import (
    CommandSocket, WS_URL, _path_value, case_commands, render_schedule,
    run_driver, setting, verify_runtime, verify_state,
)
from tools.run_core_soak import (
    PROGRESS, compare_counters, exclusive_text, file_bytes, process_snapshot,
    sha256_file, strict_snapshot,
)
from tools.run_feature_probe import require, validate_capture
from tools.run_performance_probe import (
    PerformanceProbe, build_schedule, validate_idle, validate_inactive_slots,
)

REVERB_FIELDS = {"decay_seconds": "reverb_decay_seconds", "predelay_ms": "reverb_predelay_ms",
                 "low_cut_hz": "reverb_low_cut", "high_cut_hz": "reverb_high_cut",
                 "damp": "reverb_damp", "shimmer_fb": "reverb_shimmer_fb", "noise_mod": "reverb_noise_mod"}
MAX_CONFIG_BYTES = 32*1024*1024
MAX_REPORT_BYTES = 32*1024*1024
WORK_TIMEOUT = 120  #55s audio plus normal autosave-based restoration allowance.
CLEANUP_TIMEOUT = 50


def canonical(value):
    return json.dumps(value, indent=2, allow_nan=False).encode("utf-8")


def write_json(path, value):
    payload = canonical(value)
    require(len(payload) <= MAX_REPORT_BYTES, "evidence report exceeds bound")
    with exclusive_text(path) as handle:
        handle.write(payload.decode("utf-8"))


def config_inventory(private, backup=None):
    """Hash all original configuration/library files; refuse links/large trees."""
    root = private / "config"
    result, total = {}, 0
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "configuration symlink is not an owned fixture")
        if path.is_dir():
            continue
        require(path.is_file() and path.stat().st_size <= 4*1024*1024,
                "configuration contains a nonregular/oversized file")
        require(len(result) < 128, "configuration file inventory exceeds bound")
        raw = file_bytes(path, 4*1024*1024)
        total += len(raw)
        require(total <= MAX_CONFIG_BYTES, "configuration inventory exceeds byte bound")
        relative = str(path.relative_to(root))
        result[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                            "mode": stat.S_IMODE(path.stat().st_mode)}
        if backup is not None:
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(destination, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
    return result


def restoration_inventory(private, original):
    """Preserve current/library bytes; validate, but do not rewind, autosave history.

    The pre-test history is retained in original-config. The application's
    legitimate saves advance current_state.previous.json; this disposable
    instance must not overwrite a live history file to fake byte preservation.
    """
    current = config_inventory(private)
    history = "current_state.previous.json"
    require(set(current) - {history} == set(original) - {history},
            "configuration file inventory changed")
    if history in original:
        require(history in current, "autosave history disappeared")
    for name, before in original.items():
        if name not in ("current_state.json", history):
            require(current[name] == before, "existing configuration/preset file changed")
    history_report = {"before": original.get(history), "after": current.get(history),
                      "original_bytes_backed_up": history in original}
    if history in current:
        path = private / "config" / history
        require(path.stat().st_uid == os.getuid(), "autosave history ownership changed")
        expected_mode = original.get(history, {}).get("mode", 0o600)
        require(current[history]["mode"] == expected_mode, "autosave history permissions changed")
        raw = file_bytes(path, 4*1024*1024)
        require(raw == canonical(normalize_state(json.loads(raw))), "invalid/noncanonical autosave history")
        require(hashlib.sha256(raw).hexdigest() == current[history]["sha256"],
                "autosave history changed during verification")
    return current, history_report, current["current_state.json"] == original["current_state.json"]


def validate_paths(args):
    require(type(args.pid) is int and args.pid > 1 and args.pid != os.getpid(), "explicit audition PID required")
    require(type(args.preset_slot) is int and 0 <= args.preset_slot < 10, "preset slot must be0..9")
    require(type(args.macro_index) is int and 0 <= args.macro_index < 8, "macro index must be0..7")
    private = Path(args.instance_root)
    require(private.is_absolute() and private.is_dir() and not private.is_symlink(), "private root missing/linked")
    private = private.resolve()
    base = (Path.home()/"stave-synth-pi4-backups").resolve()
    require(base in private.parents, "root is outside private Pi4 backup subtree")
    for name in ("config", "data", "source", "config/presets"):
        path = private/name
        require(path.is_dir() and not path.is_symlink() and private in path.resolve().parents,
                "runtime directory missing/escaped")
    driver = Path(args.driver)
    require(driver.is_absolute() and not driver.is_symlink() and private in driver.resolve().parents
            and driver.is_file() and os.access(driver, os.X_OK), "driver is outside explicit private root")
    output = Path(args.output_dir)
    require(output.is_absolute() and not output.exists() and not output.is_symlink()
            and output.parent.is_dir(), "evidence directory must be fresh")
    parent = output.parent.resolve()
    require(parent == private or private in parent.parents, "evidence escaped private root")
    require(not any(parent == private/name or private/name in parent.parents
                    for name in ("source", "config", "data")), "evidence overlaps runtime/source")
    return private, driver.resolve(), output


def read_bank(private):
    path = private/"config/presets/preset_bank.json"
    require(path.is_file() and not path.is_symlink(), "existing canonical private preset manifest required")
    raw = file_bytes(path, 4*1024*1024)
    bank = json.loads(raw)
    require(isinstance(bank, dict) and set(bank) == {"version", "presets", "labels"}
            and bank["version"] == 1, "unexpected preset manifest schema")
    require(isinstance(bank["presets"], list) and len(bank["presets"]) == 10
            and isinstance(bank["labels"], list) and len(bank["labels"]) == 10,
            "preset bank must have ten scenes/labels")
    normalized = {"version": 1, "presets": [None if scene is None else normalize_state(scene, scene=True)
                  for scene in bank["presets"]], "labels": bank["labels"]}
    require(all(isinstance(label, str) and len(label) <= 16 for label in bank["labels"]), "invalid preset labels")
    require(raw == canonical(normalized), "legacy/noncanonical preset bank would be rewritten; refusing")
    return bank


def reverb_presets(private):
    source = private/"source/stave_synth/faust_reverb.py"
    module = ast.parse(file_bytes(source, 128*1024).decode("utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "REVERB_PRESETS"
                                               for target in node.targets):
            presets = ast.literal_eval(node.value)
            require(all(name in presets for name in ("wash", "hall", "plate")), "reviewed reverb presets missing")
            return {name: presets[name] for name in ("wash", "hall", "plate")}
    raise RuntimeError("REVERB_PRESETS literal not found in owned source")


def plans(original):
    setup = [item for item in case_commands("02-warm-layers", original) if item["param"] != "reverb_type"]
    keys = list(dict.fromkeys((item["section"], item["param"]) for item in setup))
    keys += [("synth_pad", "reverb_type"), ("master", "split_octave_snapshot"), ("organ", "enabled")]
    keys += [("synth_pad", name) for name in REVERB_FIELDS.values()]
    keys = list(dict.fromkeys(keys))
    def priority(key):
        if key == ("synth_pad", "reverb_type"): return 0
        if key == ("master", "instrument_mode"): return 1
        if key == ("master", "split_enabled"): return 2
        if key == ("master", "split_octave_snapshot"): return 4
        if key in (("piano", "enabled"), ("organ", "enabled")): return 5
        return 3
    restore = [setting(s, p, _path_value(original, s, p)) for s, p in sorted(keys, key=priority)]
    canonical(restore)
    return setup, restore


class ExtendedProbe(PerformanceProbe):
    def __init__(self, report, ws, peer, args, private, output):
        super().__init__(report, ws)
        self.peer, self.args, self.private, self.output = peer, args, private, output
        self.created_slot = False
        self.expected_scene = None

    async def check(self, label, expected=()):
        state = await self.checkpoint(label, list(expected))
        require(state.get("faded_out") is False, "unexpected master fade")
        require(state["state"]["setlists"] == self.report["original_state"]["state"]["setlists"],
                "read-only setlist library changed")
        debug = await self.command(label+"_debug", {"type": "debug"})
        counters = strict_snapshot(state, debug)
        self.report.setdefault("counter_snapshots", {})[label] = counters
        return state, debug, counters

    def check_bank(self, *, occupied):
        bank = read_bank(self.private)
        original = self.report["original_bank"]
        slot = self.args.preset_slot
        require(bank["labels"] == original["labels"], "a preset label changed")
        require(all(bank["presets"][i] == original["presets"][i] for i in range(10) if i != slot),
                "existing preset scene changed")
        expected = self.expected_scene if occupied else None
        require(bank["presets"][slot] == expected, "owned preset slot has unexpected contents")

    async def load_created(self):
        """One pending receiver observes both acceptance and final completion."""
        slot = self.args.preset_slot
        entry = {"label": "preset_load_pending_to_complete", "command": {"type": "preset_load", "slot": slot},
                 "sent_monotonic_ns": time.monotonic_ns(), "events": []}
        self.report["timeline"].append(entry)
        pending_seen = False
        protocol_error = None
        def response(item):
            nonlocal pending_seen, protocol_error
            if item.get("slot") != slot:
                return False
            if item.get("type") == "preset_transition":
                if pending_seen or item.get("pending") is not True:
                    protocol_error = "invalid/duplicate preset acceptance"
                    return True
                pending_seen = True
                entry["events"].append({**item, "observed_monotonic_ns": time.monotonic_ns()})
            elif item.get("type") == "preset_loaded":
                if not pending_seen:
                    protocol_error = "preset completed without explicit pending acceptance"
                entry["events"].append({**item, "observed_monotonic_ns": time.monotonic_ns()})
                return True
            return False
        try:
            await self.ws.request(entry["command"], response, timeout=3)
            require(protocol_error is None, protocol_error)
            state, _, _ = await self.check("preset_completed_state")
            require(state["state"] == self.report["saved_setup_state"], "completed preset did not restore its full saved scene")
            peer = await self.command("preset_completed_peer", {"type": "get_state"}, ws=self.peer)
            require(peer["state"] == state["state"], "preset completion did not converge on second peer")
        finally:
            entry["finished_monotonic_ns"] = time.monotonic_ns()

    async def automate(self, ws, case, anchor):
        first = len(self.report["timeline"])
        self.anchor = anchor
        self.report["automation_anchor_monotonic_ns"] = round(anchor*1e9)
        self.report["automation_anchor_kind"] = "stdout_receive_loop_monotonic_not_exact_audio_frame"
        await self.at(4, latest=4.5)
        self.check_bank(occupied=False)
        self.created_slot = True  # A lost save acknowledgement still owns cleanup.
        reply = await self.command("save_empty_private_slot", {"type": "preset_save", "slot": self.args.preset_slot}, "preset_saved")
        require(reply.get("slot") == self.args.preset_slot, "save acknowledgement has wrong slot")
        saved, _, _ = await self.check("saved_preset_state")
        self.report["saved_setup_state"] = copy.deepcopy(saved["state"])
        self.expected_scene = normalize_state(saved["state"], scene=True)
        self.check_bank(occupied=True)
        idx = self.args.macro_index
        await self.command("macro_unipolar", {"type":"macro_assign", "idx":idx, "action":"set_bipolar", "bipolar":False}, "macro_assign_ack")
        for param, low, high in (("filter_cutoff_hz",1200,4800),("reverb_dry_wet",20,45)):
            await self.command("assign_empty_macro_"+param, {"type":"macro_assign", "idx":idx, "action":"toggle",
                "kind":"param", "section":"synth_pad", "param":param, "min":low, "max":high, "is_bool":False}, "macro_assign_ack")
        for seconds, value in ((7,.15),(10,.65),(13,.35)):
            await self.at(seconds, latest=seconds+.5)
            reply = await self.command("macro_value_"+str(value), {"type":"macro_value", "idx":idx, "value":value}, "macro_value_ack")
            require(reply.get("idx") == idx and reply.get("value") == value, "macro acknowledgement mismatch")
            state, _, _ = await self.check("macro_state_"+str(value), [setting("synth_pad","filter_cutoff_hz",1200+3600*value),
                                                       setting("synth_pad","reverb_dry_wet",(.2+.25*value))])
            require(state["state"]["macros"][idx]["value"] == value, "macro value not stored")
        await self.at(17, latest=17.5)
        await self.load_created()
        for seconds, kind in ((22,"hall"),(28,"plate"),(34,"wash")):
            await self.at(seconds, latest=seconds+.5)
            await self.change("reverb_type_"+kind, "synth_pad", "reverb_type", kind)
            expected = [setting("synth_pad", "reverb_type", kind)] + [setting("synth_pad", destination,
                        self.report["reverb_presets"][kind].get(source,0)) for source,destination in REVERB_FIELDS.items()]
            await self.check("reverb_state_"+kind, expected)
        await self.at(40, latest=40.5)
        for name in REVERB_FIELDS.values():
            await self.change("restore_warm_reverb_"+name, "synth_pad", name,
                              self.report["saved_setup_state"]["synth_pad"][name])
        await self.at(46.2, latest=46.5)
        state, debug, counters = await self.check("preterminal")
        require(asyncio.get_running_loop().time() < anchor+46.9,
                "preterminal health collection overlapped terminal MIDI release")
        self.report.update(preterminal_state=state, preterminal_debug=debug, preterminal_counters=counters)
        await self.at(48, latest=48.5)
        await self.command("intentional_terminal_panic", {"type":"panic"})
        self.report["feature_sequence_complete"] = True
        return self.report["timeline"][first:]


async def cleanup(probe, restore, original, identity, inventory):
    errors = probe.report["restore_failures"] = []
    try:
        await asyncio.to_thread(process_snapshot, probe.args.pid, probe.private, expected_start=identity["start_ticks"])
        await verify_runtime()
    except Exception as exc:
        errors.append(f"cleanup refused changed identity: {exc}")
        return
    try:
        await probe.command("cleanup_panic", {"type":"panic"})
        idx = probe.args.macro_index
        for message in ({"type":"macro_assign", "idx":idx, "action":"clear"},
                        {"type":"macro_assign", "idx":idx, "action":"set_bipolar", "bipolar":original["macros"][idx]["bipolar"]},
                        {"type":"macro_value", "idx":idx, "value":original["macros"][idx]["value"]}):
            await probe.command("restore_empty_macro", message, "macro_value_ack" if message["type"]=="macro_value" else "macro_assign_ack")
    except Exception as exc:
        errors.append(f"panic/macro restore: {exc}")
    for command in restore:
        try:
            await probe.command("restore_"+command["section"]+"."+command["param"], command)
        except Exception as exc:
            errors.append(f"restore control: {exc}")
    if probe.created_slot:
        try:
            # A save may have succeeded before its acknowledgement was lost.
            bank = read_bank(probe.private)
            expected = probe.expected_scene
            if expected is None:
                expected = probe.report.get("planned_saved_scene")
            require(bank["presets"][probe.args.preset_slot] in (None, expected),
                    "created slot ownership changed; refusing deletion")
            if bank["presets"][probe.args.preset_slot] is not None:
                await probe.command("delete_only_created_preset", {"type":"preset_delete", "slot":probe.args.preset_slot}, "preset_deleted")
            probe.check_bank(occupied=False)
        except Exception as exc:
            errors.append(f"owned preset cleanup: {exc}")
    try:
        restored = await probe.state("restored_state")
        probe.report["restored_state"] = restored
        require(restored["state"] == original, "complete original runtime state not restored")
        require(restored.get("faded_out") is False and restored.get("drone_faded_out") is False,
                "fade target not restored")
        # Existing autosave owns disk serialization. Never rewrite live files.
        for attempt in range(65):
            current, history, restored_bytes = await asyncio.to_thread(restoration_inventory, probe.private, inventory)
            if restored_bytes:
                probe.report["restored_configuration_inventory"] = current
                probe.report["autosave_history_evolution"] = history
                break
            if attempt == 64:
                raise RuntimeError("autosave did not restore original state-file bytes within32 seconds")
            await asyncio.sleep(.5)
    except Exception as exc:
        errors.append(f"restoration verification: {exc}")


async def session(args, private, driver, output, report):
    await verify_runtime()
    identity = await asyncio.to_thread(process_snapshot, args.pid, private)
    report["process_identity"] = identity
    bank = read_bank(private)
    require(bank["presets"][args.preset_slot] is None and bank["labels"][args.preset_slot] == "",
            "explicit preset slot is occupied/labeled; refusing")
    report["original_bank"] = bank
    inventory = config_inventory(private, output/"original-config")
    report["original_configuration_inventory"] = inventory
    report["reverb_presets"] = reverb_presets(private)
    async with websockets.connect(WS_URL, open_timeout=5, close_timeout=2, max_size=4*1024*1024, max_queue=16) as a, \
               websockets.connect(WS_URL, open_timeout=5, close_timeout=2, max_size=4*1024*1024, max_queue=16) as b:
        ws, peer = await CommandSocket(a).start(), await CommandSocket(b).start()
        probe = ExtendedProbe(report, ws, peer, args, private, output)
        mutated, original, restore = False, None, []
        try:
            response = await probe.state("original_state")
            validate_idle(response)
            original = copy.deepcopy(response["state"])
            report["original_state"] = response
            state_bytes = file_bytes(private/"config/current_state.json",4*1024*1024)
            require(state_bytes == canonical(normalize_state(original)) and json.loads(state_bytes) == original,
                    "saved state must already match canonical authoritative runtime; refusing rewrite")
            require(original["ui"]["preset_saved"] == [scene is not None for scene in bank["presets"]]
                    and original["ui"]["preset_labels"] == bank["labels"], "runtime preset inventory differs from file")
            require(original["macros"][args.macro_index]["assignments"] == [], "explicit macro is assigned; refusing")
            slots = await probe.command("original_pad_slots", {"type":"list_pad_slots"}, "pad_slots")
            validate_inactive_slots(slots)
            setup, restore = plans(original)
            report.update(setup_commands=setup, restore_commands=restore)
            write_json(output/"00-original-state.json", response)
            write_json(output/"01-plan.json", {"setup":setup,"restore":restore,"bank":bank,"files":inventory})
            schedule = render_schedule(build_schedule())
            schedule_path = output/"midi_schedule.txt"
            with exclusive_text(schedule_path) as handle:
                handle.write(schedule)
            report["schedule_sha256"] = hashlib.sha256(schedule.encode()).hexdigest()
            report["schedule_events"] = len(build_schedule())
            await verify_runtime()
            await asyncio.to_thread(process_snapshot, args.pid, private, expected_start=identity["start_ticks"])
            require(config_inventory(private) == inventory, "configuration changed before mutation")
            mutated = True
            await probe.command("setup_panic", {"type":"panic"})
            for command in setup:
                await probe.command("setup_"+command["section"]+"."+command["param"], command)
            await asyncio.sleep(1)
            before, before_debug, baseline = await probe.check("baseline", setup)
            report.update(before_state=before, before_debug=before_debug, baseline_counters=baseline,
                          planned_saved_scene=normalize_state(before["state"],scene=True))
            report["driver"] = await run_driver(driver, schedule_path, output/"extended-probe.wav", ws,
                                               "extended-controls", automation=probe.automate)
            after, debug, final = await probe.check("capture_after")
            report.update(after_state=after, after_debug=debug, final_counters=final)
            delta, _ = compare_counters(baseline, final)
            report["raw_total_delta"] = delta
            report["failures"] += [f"{key} grew by {value}" for key,value in delta.items() if value and key not in PROGRESS]
            require(report.get("feature_sequence_complete") is True, "extended sequence incomplete")
            musical, _ = compare_counters(baseline, report["preterminal_counters"])
            report["musical_delta"] = musical
            report["terminal_window_delta"] = {key: final[key]-report["preterminal_counters"][key] for key in final}
            require(all(value >= 0 for value in report["terminal_window_delta"].values()), "terminal counters reset")
            report["capture_validation"] = validate_capture(report["driver"], output/"extended-probe.wav",report["schedule_events"])
            require(report["capture_validation"]["peak"] < 1, "captured output reached full scale")
        finally:
            try:
                if mutated:
                    try:
                        await asyncio.wait_for(cleanup(probe,restore,original,identity,inventory),CLEANUP_TIMEOUT)
                    except BaseException as exc:
                        report.setdefault("restore_failures",[]).append(f"cleanup incomplete: {type(exc).__name__}: {exc}")
                        if isinstance(exc,asyncio.CancelledError): raise
            finally:
                await asyncio.gather(ws.stop(),peer.stop(),return_exceptions=True)


async def run(args):
    private, driver, output = validate_paths(args)
    output.mkdir(mode=0o700)
    report = {"format":1,"status":"FAIL","stage_qualified":False,"duration_seconds":55,
              "failures":[],"restore_failures":[],"timeline":[],"pid":args.pid,"instance_root":str(private),
              "preset_slot":args.preset_slot,"macro_index":args.macro_index,"driver_sha256":sha256_file(driver),
              "setlist_functionally_tested":False,"active_reverb_backend_proven":False,
              "limits":["One empty private preset/macro only; current state and library bytes must return unchanged. Autosave history may advance; original history is backed up and evolution is validated/reported.",
                        "Setlists are enumerated/preserved read-only; save/load replacement is a separate disposable-fixture test.",
                        "Reverb requested state and mirrored controls are verified, not runtime backend identity; startup native-ready is cached.",
                        "Reverb backend replacement may intentionally clear tails; no gapless algorithm spillover claim.",
                        "All raw counter growth retained, including algorithm/preset changes and terminal release.",
                        "Mixed output is not individual-source continuity, hardware/analogue latency, listening or stage qualification.",
                        "Service supervisor owns lifecycle/SIGKILL recovery; coordinator never changes services or live files directly."]}
    try:
        # Cleanup has its own bound and runs after cancellation of the work.
        await asyncio.wait_for(session(args,private,driver,output,report),WORK_TIMEOUT)
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
        if not isinstance(exc,Exception): raise
    finally:
        if not report["failures"] and not report["restore_failures"] and report.get("feature_sequence_complete"):
            report["status"] = "PASS_SCOPED_FUNCTIONS"
        write_json(output/"extended-probe.result.json",report)
    return 0 if report["status"]=="PASS_SCOPED_FUNCTIONS" else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("driver","instance-root","output-dir"):
        parser.add_argument("--"+name,required=True)
    for name in ("pid","preset-slot","macro-index"):
        parser.add_argument("--"+name,type=int,required=True)
    args=parser.parse_args(argv)
    async def supervised():
        loop, task=asyncio.get_running_loop(),asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM,task.cancel)
        try:
            return await run(args)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    try:
        return asyncio.run(supervised())
    except (KeyboardInterrupt,asyncio.CancelledError,RuntimeError,OSError,ValueError,TimeoutError) as exc:
        print(f"extended probe refused/failed: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
