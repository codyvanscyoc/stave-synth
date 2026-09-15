"""Offline extended-probe protocol, preservation and cancellation regressions."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from stave_synth.state_schema import GLOBAL_MASTER_KEYS, normalize_state
from stave_synth import state_persistence
from tools import run_extended_probe as probe
from tests.test_core_soak import CoreApplication, system_sample
from tests.test_feature_probe import FakeConnection


PRESETS = {
    "wash":dict(backend="fdn",decay_seconds=6.,predelay_ms=25.,low_cut_hz=80,high_cut_hz=7000,damp=.5),
    "hall":dict(backend="fdn",decay_seconds=9.,predelay_ms=45.,low_cut_hz=120,high_cut_hz=8500,damp=.35),
    "plate":dict(backend="plate",decay_seconds=3.,predelay_ms=5.,low_cut_hz=150,high_cut_hz=11000,damp=.3),
}


class ExtendedApplication(CoreApplication):
    def __init__(self, root, *, fault=None):
        super().__init__()
        self.root, self.fault = root, fault
        self.state=normalize_state(normalize_state(self.state))
        self.state["ui"]["preset_saved"]=[False]*10
        self.state["ui"]["preset_labels"]=[""]*10
        self.bank={"version":1,"presets":[None]*10,"labels":[""]*10}
        # Preserve an unrelated real scene/label, not merely an empty bank.
        self.bank["presets"][0]=normalize_state(self.state,scene=True)
        self.bank["labels"][0]="Existing"
        self.state["ui"]["preset_saved"][0]=True
        self.state["ui"]["preset_labels"][0]="Existing"
        self.state["macros"][7]["bipolar"]=True
        self.state["macros"][7]["value"]=.25
        self.original=copy.deepcopy(self.state)
        self.original_bank=copy.deepcopy(self.bank)
        (root/"config/presets").mkdir(parents=True)
        (root/"config/cc_map.json").write_bytes(b'{"untouched": true}')
        self.persist()

    def persist(self):
        (self.root/"config/current_state.json").write_bytes(probe.canonical(normalize_state(self.state)))
        (self.root/"config/presets/preset_bank.json").write_bytes(probe.canonical(self.bank))

    def dispatch(self,message,connection):
        kind=message["type"]
        if kind in {"preset_save","preset_delete","preset_load","macro_assign","macro_value"}:
            self.messages.append(copy.deepcopy(message))
            slot=message.get("slot")
            if kind=="preset_save":
                self.bank["presets"][slot]=normalize_state(self.state,scene=True)
                self.state["ui"]["preset_saved"][slot]=True
                self.persist()
                if self.fault=="lost_save_ack":
                    return {"type":"error","message":"save acknowledgement lost"}
                return {"type":"preset_saved","slot":slot}
            if kind=="preset_delete":
                self.bank["presets"][slot]=None
                self.bank["labels"][slot]=""
                self.state["ui"]["preset_saved"][slot]=False
                self.state["ui"]["preset_labels"][slot]=""
                self.persist()
                return {"type":"preset_deleted","slot":slot}
            if kind=="preset_load":
                scene=copy.deepcopy(self.bank["presets"][slot])
                for key in GLOBAL_MASTER_KEYS:
                    if key in self.state["master"]:
                        scene["master"][key]=self.state["master"][key]
                self.state.update(scene)
                if self.fault=="incomplete_morph":
                    self.state["synth_pad"]["filter_cutoff_hz"]+=1
                return {"type":"preset_transition","slot":slot,"pending":True}
            idx=message["idx"]; macro=self.state["macros"][idx]
            if kind=="macro_assign":
                if message["action"]=="clear": macro["assignments"]=[]
                elif message["action"]=="set_bipolar": macro["bipolar"]=message["bipolar"]
                else:
                    macro["assignments"].append({key:message[key] for key in
                        ("kind","section","param","min","max","is_bool")})
                return {"type":"macro_assign_ack","idx":idx,"assignments":copy.deepcopy(macro["assignments"]),"bipolar":macro["bipolar"]}
            if self.fault=="cancel" and message["value"]==.65:
                raise asyncio.CancelledError()
            macro["value"]=message["value"]
            for item in macro["assignments"]:
                value=item["min"]+(item["max"]-item["min"])*message["value"]
                self.state["synth_pad"][item["param"]]=value/100 if item["param"]=="reverb_dry_wet" else value
            if self.fault=="overbudget" and message["value"]==.65:
                self.health["audio"]["render_metrics"]["over_budget_count"]+=1
            return {"type":"macro_value_ack","idx":idx,"value":message["value"]}
        result=super().dispatch(message,connection)
        if kind=="setting" and message["param"]=="reverb_type":
            for source,destination in probe.REVERB_FIELDS.items():
                self.state["synth_pad"][destination]=PRESETS[message["value"]].get(source,0)
        if kind=="get_state":
            self.progress()
            result["health"]=copy.deepcopy(self.health)
            self.persist()  # Deterministic fake of the normal autosave owner.
        return result

    def connect(self,url,**kwargs):
        if url!=probe.WS_URL: raise AssertionError("non-audition endpoint")
        self.connections+=1
        return ExtendedConnection(self,self.connections)


class ExtendedConnection(FakeConnection):
    async def send(self,payload):
        message=json.loads(payload)
        result=self.app.dispatch(message,self.connection)
        await self.incoming.put(json.dumps({"type":"state","state":{"stale":True}}))
        if message["type"]=="preset_load":
            if self.app.fault=="missing_pending":
                await self.incoming.put(json.dumps({"type":"preset_loaded","slot":message["slot"]}))
                return
            await self.incoming.put(json.dumps(result))
            await self.incoming.put(json.dumps({"type":"preset_loaded","slot":message["slot"]}))
        else:
            await self.incoming.put(json.dumps(result))


class ExtendedProbeTests(unittest.TestCase):
    def run_fake(self, *, fault=None, occupied=False, assigned=False, wrong_pid=False):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); output=root/"evidence"
            driver=root/"native_audition"; driver.write_bytes(b"owned-driver"); driver.chmod(0o700)
            app=ExtendedApplication(root,fault=fault)
            if occupied:
                app.bank["presets"][9]=copy.deepcopy(app.bank["presets"][0])
            if assigned:
                app.state["macros"][7]["assignments"]=[dict(kind="param",section="synth_pad",param="reverb_dry_wet",min=0,max=100,is_bool=False)]
            app.persist()
            before=probe.config_inventory(root)
            args=SimpleNamespace(pid=1234,instance_root=str(root),driver=str(driver),output_dir=str(output),preset_slot=9,macro_index=7)
            async def native(*args,automation):
                await automation(args[3],args[4],asyncio.get_running_loop().time())
                return {"returncode":0,"events":{"capture_complete":{}}}
            async def at(owner,seconds,*,latest=None):
                if fault=="late" and seconds==46.2:
                    raise RuntimeError("preterminal window missed")
            with patch.object(probe,"validate_paths",return_value=(root,driver,output)), \
                 patch.object(probe,"verify_runtime",new=AsyncMock(return_value={"instance":"audition","websocket_port":18765})), \
                 patch.object(probe,"process_snapshot",side_effect=RuntimeError("wrong PID") if wrong_pid else lambda *a,**k:system_sample()), \
                 patch.object(probe,"reverb_presets",return_value=PRESETS), \
                 patch.object(probe.websockets,"connect",side_effect=app.connect), \
                 patch.object(probe,"run_driver",new=native), \
                 patch.object(probe.ExtendedProbe,"at",new=at), \
                 patch.object(probe.asyncio,"sleep",new=AsyncMock()), \
                 patch.object(probe,"validate_capture",return_value={"all_samples_finite":True,"peak":.05}):
                try: code=asyncio.run(probe.run(args))
                except asyncio.CancelledError: code="cancelled"
            report=json.loads((output/"extended-probe.result.json").read_text())
            after=probe.config_inventory(root)
        return code,report,app,before,after

    def test_complete_scoped_capture_preserves_every_original_file_and_state(self):
        code,report,app,before,after=self.run_fake()
        self.assertEqual(code,0,report["failures"]+report["restore_failures"])
        self.assertEqual(app.state,app.original)
        self.assertEqual(app.bank,app.original_bank)
        self.assertEqual(before,after)
        self.assertEqual(report["status"],"PASS_SCOPED_FUNCTIONS")
        self.assertFalse(report["stage_qualified"])
        self.assertFalse(report["setlist_functionally_tested"])
        self.assertFalse(report["active_reverb_backend_proven"])
        self.assertTrue(report["feature_sequence_complete"])
        self.assertIn("preterminal_counters",report)
        self.assertEqual(sum(item["type"]=="preset_save" for item in app.messages),1)
        self.assertEqual(sum(item["type"]=="preset_delete" for item in app.messages),1)
        self.assertFalse(any(item["type"].startswith("setlist_") for item in app.messages))
        self.assertFalse(any(item["type"] in {"record_toggle","set_audio_output","save_to_pad_slot"} for item in app.messages))
        transition=next(item for item in report["timeline"] if item["label"]=="preset_load_pending_to_complete")
        self.assertEqual([item["type"] for item in transition["events"]],["preset_transition","preset_loaded"])

    def test_existing_slot_macro_and_wrong_pid_refuse_without_mutation(self):
        for option in ("occupied","assigned","wrong_pid"):
            with self.subTest(option=option):
                code,report,app,before,after=self.run_fake(**{option:True})
                self.assertEqual(code,1)
                self.assertEqual(before,after)
                self.assertTrue(report["failures"])
                self.assertFalse(any(item["type"] not in {"get_state","list_pad_slots"} for item in app.messages))

    def test_lost_save_ack_cancel_bad_morph_and_late_terminal_restore(self):
        for fault in ("lost_save_ack","cancel","incomplete_morph","missing_pending","late"):
            with self.subTest(fault=fault):
                code,report,app,before,after=self.run_fake(fault=fault)
                self.assertEqual(code,"cancelled" if fault=="cancel" else 1)
                self.assertEqual(report["status"],"FAIL")
                self.assertEqual(app.state,app.original)
                self.assertEqual(before,after)
                self.assertEqual(report["restore_failures"],[])

    def test_raw_overbudget_failure_is_not_masked_by_functional_completion(self):
        code,report,app,before,after=self.run_fake(fault="overbudget")
        self.assertEqual(code,1)
        self.assertTrue(report["feature_sequence_complete"])
        self.assertEqual(report["raw_total_delta"]["render.over_budget_count"],1)
        self.assertTrue(report["failures"])
        self.assertEqual(before,after)

    def test_noncanonical_or_changed_existing_bank_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            app=ExtendedApplication(root)
            path=root/"config/presets/preset_bank.json"
            self.assertEqual(probe.read_bank(root),app.bank)
            path.write_text(json.dumps(app.bank))
            with self.assertRaisesRegex(RuntimeError,"noncanonical"):
                probe.read_bank(root)

    def test_configuration_inventory_refuses_links_and_keeps_private_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); ExtendedApplication(root)
            inventory=probe.config_inventory(root,root/"backup")
            self.assertIn("presets/preset_bank.json",inventory)
            self.assertEqual((root/"backup/current_state.json").stat().st_mode & 0o777,0o600)
            (root/"config/linked").symlink_to(root/"config/current_state.json")
            with self.assertRaises(RuntimeError): probe.config_inventory(root)

    def test_real_persistence_advances_history_without_losing_original_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = ExtendedApplication(root)
            path = root / "config/current_state.json"
            original_state = copy.deepcopy(app.state)
            state_persistence.save(path, original_state)
            before = probe.config_inventory(root, root / "backup")
            changed = copy.deepcopy(original_state)
            changed["synth_pad"]["filter_cutoff_hz"] = 1234
            state_persistence.save(path, changed)
            state_persistence.save(path, original_state)
            after, history, restored = probe.restoration_inventory(root, before)
            self.assertTrue(restored)
            self.assertNotEqual(history["before"]["sha256"], history["after"]["sha256"])
            self.assertEqual(json.loads((root / "backup/current_state.previous.json").read_bytes()), original_state)
            self.assertEqual(json.loads(state_persistence.previous_path(path).read_bytes()), changed)
            self.assertEqual(after["presets/preset_bank.json"], before["presets/preset_bank.json"])
            state_persistence.previous_path(path).write_bytes(b'{"bad":true}')
            with self.assertRaisesRegex(RuntimeError, "history"):
                probe.restoration_inventory(root, before)

    def test_restoration_rejects_other_file_changes_and_history_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = ExtendedApplication(root)
            path = root / "config/current_state.json"
            state_persistence.save(path, app.state)
            before = probe.config_inventory(root)
            state_persistence.previous_path(path).unlink()
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                probe.restoration_inventory(root, before)
            state_persistence.save(path, app.state)
            (root / "config/cc_map.json").write_bytes(b'{}')
            with self.assertRaisesRegex(RuntimeError, "file changed"):
                probe.restoration_inventory(root, before)

    def test_reverb_literal_is_read_without_loading_native_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/"source/stave_synth"; source.mkdir(parents=True)
            (source/"faust_reverb.py").write_text("raise RuntimeError('must not execute')\nREVERB_PRESETS = "+repr(PRESETS))
            self.assertEqual(probe.reverb_presets(root),PRESETS)

    def test_exact_private_paths_and_output_refusals(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary).resolve(); root=home/"stave-synth-pi4-backups"/"owned"
            for name in ("config/presets","data","source"):
                (root/name).mkdir(parents=True,exist_ok=True)
            driver=root/"native_audition"; driver.write_bytes(b"owned"); driver.chmod(0o700)
            args=SimpleNamespace(pid=1234,preset_slot=9,macro_index=7,driver=str(driver),
                instance_root=str(root),output_dir=str(root/"fresh"))
            with patch.object(probe.Path,"home",return_value=home):
                self.assertEqual(probe.validate_paths(args),(root,driver,root/"fresh"))
                args.output_dir=str(root/"config/forbidden")
                with self.assertRaises(RuntimeError): probe.validate_paths(args)
                args.output_dir=str(root/"fresh"); args.preset_slot=10
                with self.assertRaises(RuntimeError): probe.validate_paths(args)


if __name__=="__main__": unittest.main()
