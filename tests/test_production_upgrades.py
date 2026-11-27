"""Comprehensive regression & feature test suite for SIMATIC STEP 7 Classic MCP v2.0 production upgrades.

Covers:
1. All 11 audit regressions (Python 3.11 syntax, strict dual-guard AND on remote/port-102 PLCs,
   AccessMode read-only enforcement, SHA-256 optimistic concurrency `state_changed`, JSONL AuditLogger).
2. Hardware Configuration & DIN-rail assembly (MLFB catalog search, S7-300/400 slot rules,
   power budget overflow, I/O auto-allocation & overlap detection, `.cfg` export/import round-trip).
3. Complete IEC 61131-3 language coverage (FBD compliance & conversion, SCL syntax/C-op/loop checks,
   S7-GRAPH BFS reachability & `.gr7` generation, Global/Instance DB & UDT exact S7 bit/word alignment).
4. High-speed pooled S7comm multi-item batch Read/Write Var PDUs, Watch Table (VAT) snapshots + deltas,
   and CPU SZL diagnostics buffer & RUN/STOP control over real loopback TCP sockets.
5. Deterministic Virtual S7 PLC closed-loop simulation (motor start/stop latch + S7 on-delay timer + counter).
6. Persistent 32-bit COM Worker mock transport (sub-ms dispatch, PLCSIM REAL float preservation & I-area reads).
7. CLI `doctor`, `init-client` configs (VS Code, Antigravity, Codex, Claude Code), MCP Resources, and Prompts.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

import simatic_s7_mcp_server as SERVER
from simatic_s7_mcp import (
    AccessMode,
    AuditLogger,
    EnhancedS7LoopbackServer,
    FailureCategory,
    PersistentComWorker,
    StationHardwareConfig,
    compute_content_hash,
    generate_client_config,
    parse_station_cfg,
)


class TestSafetyAndAuditRegressions(unittest.TestCase):
    def setUp(self):
        self._orig_mode = SERVER.ACTIVE_ACCESS_MODE
        self._orig_env = os.environ.get("S7_WRITES_ENABLED")

    def tearDown(self):
        SERVER.set_access_mode(self._orig_mode)
        if self._orig_env is None:
            os.environ.pop("S7_WRITES_ENABLED", None)
        else:
            os.environ["S7_WRITES_ENABLED"] = self._orig_env

    def test_dual_guard_strict_and_for_remote_and_port102(self):
        os.environ.pop("S7_WRITES_ENABLED", None)
        # Remote IP: confirm=True without env MUST be blocked
        self.assertFalse(SERVER.S7RuntimeClient.writes_enabled(True, host="192.168.0.10", port=102))
        # Local port 102: confirm=True without env MUST be blocked
        self.assertFalse(SERVER.S7RuntimeClient.writes_enabled(True, host="127.0.0.1", port=102))
        # Env=true without confirm=True MUST still be blocked
        os.environ["S7_WRITES_ENABLED"] = "true"
        self.assertFalse(SERVER.S7RuntimeClient.writes_enabled(False, host="192.168.0.10", port=102))
        # BOTH true -> allowed
        self.assertTrue(SERVER.S7RuntimeClient.writes_enabled(True, host="192.168.0.10", port=102))
        # Ephemeral loopback test port with confirm=True -> allowed even without env
        os.environ.pop("S7_WRITES_ENABLED", None)
        self.assertTrue(SERVER.S7RuntimeClient.writes_enabled(True, host="127.0.0.1", port=45123))

    def test_read_only_access_mode_blocks_mutations(self):
        SERVER.set_access_mode(AccessMode.READ_ONLY)
        res = json.loads(SERVER.s7_hw_add_module(slot=2, module="CPU 315-2 PN/DP"))
        self.assertFalse(res["success"])
        self.assertEqual(res["failure_category"], FailureCategory.ACCESS_DENIED.value)

        res_wr = json.loads(SERVER.s7_write_scl_block("FUNCTION FC1 : VOID\nBEGIN\nEND_FUNCTION\n", block_name="FC1"))
        self.assertFalse(res_wr["success"])
        self.assertEqual(res_wr["failure_category"], FailureCategory.ACCESS_DENIED.value)

    def test_optimistic_concurrency_hash_detects_state_changed(self):
        h1 = compute_content_hash("awl", "NETWORK\r\nA I 0.0;\r\n= Q 0.0;\r\n")
        h2 = compute_content_hash("awl", "NETWORK\nA I 0.0;\n= Q 0.0;\n")
        self.assertEqual(h1, h2)  # CRLF normalized identically
        err_json = SERVER._check_optimistic_hash("s7_com_write_block", "OB1", "awl:sha256:stale", "NETWORK\nA I 0.1;\n")
        self.assertIsNotNone(err_json)
        parsed = json.loads(err_json)
        self.assertEqual(parsed["failure_category"], FailureCategory.STATE_CHANGED.value)

    def test_audit_logger_appends_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "audit.jsonl"
            logger = AuditLogger(log_file)
            logger.record(tool="s7_com_write_block", action="write", target="OB1", success=True, status="SUCCESS", latency_ms=4.2)
            logger.record(tool="s7_runtime_write", action="write", target="M0.0", success=False, status="BLOCKED", failure_category="write_guardrail_blocked")
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            e1 = json.loads(lines[0])
            self.assertEqual(e1["tool"], "s7_com_write_block")
            self.assertTrue(e1["success"])


class TestHardwareConfigAndDinRail(unittest.TestCase):
    def test_catalog_search_and_station_cfg_roundtrip(self):
        search = json.loads(SERVER.s7_hw_catalog_search(query="315", family="S7-300"))
        self.assertTrue(search["success"])
        self.assertGreater(search["count"], 0)

        cfg_res = json.loads(
            SERVER.s7_hw_configure_station(
                station_name="LINE_A_300",
                plc_family="S7-300",
                modules=[
                    {"slot": 1, "module": "PS 307 5A"},
                    {"slot": 2, "module": "6ES7 315-2EH14-0AB0", "ip_address": "192.168.10.20"},
                    {"slot": 4, "module": "DI16xDC24V"},
                    {"slot": 5, "module": "DO16xDC24V"},
                    {"slot": 6, "module": "AI8x12Bit"},
                ],
            )
        )
        self.assertTrue(cfg_res["success"], f"HW config failed: {cfg_res}")
        self.assertTrue(cfg_res["validation"]["valid"])

        exp = json.loads(SERVER.s7_hw_export_cfg(station_name="LINE_A_300"))
        self.assertTrue(exp["success"])
        self.assertIn("6ES7 315-2EH14-0AB0", exp["cfg_text"])
        self.assertIn("192.168.10.20", exp["cfg_text"])

        # Round-trip parse
        parsed = parse_station_cfg(exp["cfg_text"])
        self.assertEqual(len(parsed.modules), 5)
        self.assertTrue(parsed.validate()["valid"])

    def test_din_rail_detects_slot_rules_and_io_overlaps(self):
        st = StationHardwareConfig(station_name="BAD_300", plc_family="S7-300")
        # CPU in wrong slot (slot 5 instead of slot 2 for S7-300) + overlapping DI bytes
        st.add_module(slot=1, module="PS 307 2A")
        st.add_module(slot=5, module="6ES7 315-2EH14-0AB0")
        st.add_module(slot=4, module="DI16xDC24V", input_start=0)
        st.add_module(slot=6, module="DI16xDC24V", input_start=1)  # overlaps byte 1
        val = st.validate()
        self.assertFalse(val["valid"])
        err_text = " ".join(val["errors"]).lower()
        self.assertIn("not cpu", err_text)
        self.assertIn("overlap", err_text)


class TestAllIec61131Languages(unittest.TestCase):
    def test_fbd_conversion_and_validation(self):
        awl = """FUNCTION FC 10 : VOID
BEGIN
NETWORK
TITLE = Pump Logic
  A I 0.0;
  AN I 0.1;
  = Q 0.0;
END_FUNCTION
"""
        conv = json.loads(SERVER.s7_convert_to_fbd(source_code=awl))
        self.assertTrue(conv["success"])
        self.assertTrue(conv["is_fbd_compliant"])
        self.assertEqual(conv["target_blklang"], "00003")

        val = json.loads(SERVER.s7_validate_fbd_compliance(conv["converted_code"]))
        self.assertTrue(val["is_fbd_compliant"])

    def test_scl_validation_accepts_valid_and_catches_c_operators(self):
        good_scl = """FUNCTION FC 20 : VOID
VAR_INPUT
  Start : BOOL;
  Stop  : BOOL;
END_VAR
VAR_OUTPUT
  Run   : BOOL;
END_VAR
BEGIN
  IF Start AND NOT Stop THEN
    Run := TRUE;
  ELSE
    Run := FALSE;
  END_IF;
END_FUNCTION
"""
        res_good = json.loads(SERVER.s7_validate_scl(good_scl))
        self.assertTrue(res_good["valid"], f"Expected valid SCL: {res_good}")

        bad_scl = """FUNCTION FC 21 : VOID
BEGIN
  IF a == 1 && b != 2 THEN
    x := 1;
END_FUNCTION
"""
        res_bad = json.loads(SERVER.s7_validate_scl(bad_scl))
        self.assertFalse(res_bad["valid"])
        self.assertTrue(len(res_bad["errors"]) >= 2)

    def test_s7_graph_sfc_reachability_and_gr7_emission(self):
        steps = [
            {"number": 1, "name": "S_IDLE", "initial": True, "actions": [{"qualifier": "N", "operand": "Q 0.0"}]},
            {"number": 2, "name": "S_FILL", "actions": [{"qualifier": "S", "operand": "Q 0.1"}]},
            {"number": 3, "name": "S_MIX", "actions": [{"qualifier": "N", "operand": "Q 0.2"}]},
        ]
        transitions = [
            {"number": 1, "name": "T_START", "from_step": 1, "to_step": 2, "condition": "I 0.0"},
            {"number": 2, "name": "T_FULL", "from_step": 2, "to_step": 3, "condition": "I 0.1"},
            {"number": 3, "name": "T_DONE", "from_step": 3, "to_step": 1, "condition": "I 0.2"},
        ]
        res = json.loads(SERVER.s7_validate_graph_sfc(fb_number=10, steps=steps, transitions=transitions))
        self.assertTrue(res["valid"], f"Valid GRAPH rejected: {res}")
        self.assertIn("FUNCTION_BLOCK FB 10", res["gr7_source"])

        # Add unreachable step 99 -> flagged as warning; bad transition -> error
        steps_bad = steps + [{"number": 99, "name": "S_ORPHAN", "actions": []}]
        res_warn = json.loads(SERVER.s7_validate_graph_sfc(fb_number=10, steps=steps_bad, transitions=transitions))
        self.assertTrue(any("unreachable" in w.lower() for w in res_warn["warnings"]))

        bad_tr = transitions + [{"number": 9, "name": "T_BAD", "from_step": 1, "to_step": 777, "condition": "I 0.5"}]
        res_err = json.loads(SERVER.s7_validate_graph_sfc(fb_number=10, steps=steps, transitions=bad_tr))
        self.assertFalse(res_err["valid"])

    def test_data_block_and_udt_s7_word_alignment(self):
        fields = [
            {"name": "RunCmd", "type": "BOOL", "initial": "FALSE"},
            {"name": "Fault", "type": "BOOL", "initial": "FALSE"},
            {"name": "SpeedRpm", "type": "INT", "initial": "1500"},
            {"name": "TempC", "type": "REAL", "initial": "23.5"},
        ]
        db = json.loads(SERVER.s7_create_data_block(db_number=10, fields=fields, title="MotorDB"))
        self.assertTrue(db["success"], f"DB failed: {db}")
        layout = {f["name"]: f for f in db["layout"]}
        # Two BOOLs pack into byte 0 (.0 and .1), then INT aligns to even byte 2, REAL at byte 4
        self.assertEqual(layout["RunCmd"]["s7_address"], "DB10.DBX 0.0")
        self.assertEqual(layout["Fault"]["s7_address"], "DB10.DBX 0.1")
        self.assertEqual(layout["SpeedRpm"]["s7_address"], "DB10.DBW 2")
        self.assertEqual(layout["TempC"]["s7_address"], "DB10.DBD 4")

        udt = json.loads(SERVER.s7_create_udt(udt_number=5, fields=fields, title="MotorUDT"))
        self.assertTrue(udt["success"])
        self.assertIn("TYPE UDT 5", udt["awl_source"])


class TestPooledRuntimeAndVirtualPlc(unittest.TestCase):
    def test_pooled_batch_read_write_watch_table_and_szl_diagnostics(self):
        srv = EnhancedS7LoopbackServer(host="127.0.0.1", port=0)
        port = srv.start()
        orig_mode = SERVER.ACTIVE_ACCESS_MODE
        SERVER.set_access_mode(AccessMode.FULL)
        try:
            # 1. Batch write 5 mixed items in 1 PDU
            bw = json.loads(
                SERVER.s7_runtime_batch_write(
                    items=[
                        {"address": "M0.0", "value": True},
                        {"address": "MB4", "value": 0x5A},
                        {"address": "MW10", "value": -321, "data_type": "INT"},
                        {"address": "DB1.DBD20", "value": 42.75, "data_type": "REAL"},
                        {"address": "C3", "value": 17},
                    ],
                    host="127.0.0.1",
                    port=port,
                    confirm_live_write=True,
                )
            )
            self.assertTrue(bw["success"], f"Batch write failed: {bw}")
            self.assertEqual(bw["pdu_count"], 1)

            # 2. Batch read back on pooled socket (verify connection reuse)
            br = json.loads(
                SERVER.s7_runtime_batch_read(
                    items=[
                        {"address": "M0.0"},
                        {"address": "MB4"},
                        {"address": "MW10", "data_type": "INT"},
                        {"address": "DB1.DBD20", "data_type": "REAL"},
                        {"address": "C3"},
                    ],
                    host="127.0.0.1",
                    port=port,
                )
            )
            self.assertTrue(br["success"], f"Batch read failed: {br}")
            self.assertEqual(br["pdu_count"], 1)
            self.assertGreaterEqual(br["pool"]["pool_reuses"], 1)
            vals = {it["address"]: it["value"] for it in br["items"]}
            self.assertIs(vals["M0.0"], True)
            self.assertEqual(vals["MB4"], 0x5A)
            self.assertEqual(vals["MW10"], -321)
            self.assertAlmostEqual(vals["DB1.DBD20"], 42.75, places=3)
            self.assertEqual(vals["C3"], 17)

            # 3. Watch Table snapshot + delta tracking
            vat1 = json.loads(
                SERVER.s7_monitor_watch_table(
                    tags=[{"symbol": "RUN_FLAG"}, {"address": "MW10", "data_type": "INT"}],
                    symbol_table={"RUN_FLAG": "M0.0"},
                    host="127.0.0.1",
                    port=port,
                    table_id="VAT_TEST",
                )
            )
            self.assertTrue(vat1["success"])
            # Mutate M0.0 and take second snapshot -> delta must detect change
            srv.plc.write_address("M0.0", False)
            vat2 = json.loads(
                SERVER.s7_monitor_watch_table(
                    tags=[{"symbol": "RUN_FLAG"}, {"address": "MW10", "data_type": "INT"}],
                    symbol_table={"RUN_FLAG": "M0.0"},
                    host="127.0.0.1",
                    port=port,
                    table_id="VAT_TEST",
                )
            )
            self.assertEqual(len(vat2["latest"]["changed"]), 1)
            self.assertEqual(vat2["latest"]["changed"][0]["current"], False)

            # 4. CPU SZL diagnostics + STOP/RUN control
            diag = json.loads(SERVER.s7_runtime_cpu_diagnostics(host="127.0.0.1", port=port))
            self.assertTrue(diag["success"])
            self.assertEqual(diag["cpu_mode"], "RUN")
            self.assertIn("315-2EH14", diag["order_number"])
            self.assertGreaterEqual(diag["diagnostics_count"], 2)

            stop_res = json.loads(SERVER.s7_runtime_cpu_control(action="STOP", host="127.0.0.1", port=port, confirm_live_write=True))
            self.assertTrue(stop_res["success"])
            diag2 = json.loads(SERVER.s7_runtime_cpu_diagnostics(host="127.0.0.1", port=port))
            self.assertEqual(diag2["cpu_mode"], "STOP")
        finally:
            SERVER.set_access_mode(orig_mode)
            SERVER.runtime_client.pool.close_all()
            srv.stop()

    def test_virtual_s7_plc_closed_loop_latch_timer_and_counter(self):
        ob1_src = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = Motor Start/Stop Seal-In Latch
  A(
  O I 0.0;
  O Q 0.0;
  )
  AN I 0.1;
  = Q 0.0;
NETWORK
TITLE = 300ms On-Delay Timer
  A Q 0.0;
  L S5T#300MS;
  SD T 1;
  A T 1;
  = Q 0.1;
NETWORK
TITLE = Pulse Counter
  A I 0.2;
  CU C 1;
END_ORGANIZATION_BLOCK
"""
        load_res = json.loads(SERVER.s7_virtual_plc_load(block_name="OB1", source_code=ob1_src, reset_memory=True))
        self.assertTrue(load_res["success"])

        scenario = json.loads(
            SERVER.s7_plcsim_run_scenario(
                target="virtual",
                steps=[
                    {"name": "Press Start", "inputs": {"I0.0": True, "I0.1": False}, "cycles": 1, "tick_ms": 100, "expect": {"Q0.0": True, "Q0.1": False}},
                    {"name": "Release Start (latched), wait 300ms", "inputs": {"I0.0": False}, "cycles": 3, "tick_ms": 100, "expect": {"Q0.0": True, "Q0.1": True}},
                    {"name": "Count pulse 1", "inputs": {"I0.2": True}, "cycles": 1, "tick_ms": 50, "expect": {"C1": 1}},
                    {"name": "Press Stop", "inputs": {"I0.1": True, "I0.2": False}, "cycles": 1, "tick_ms": 50, "expect": {"Q0.0": False, "Q0.1": False}},
                ],
            )
        )
        self.assertTrue(scenario["success"], f"Scenario failed: {scenario}")
        self.assertEqual(scenario["passed_steps"], 4)


class TestPersistentComWorkerAndCli(unittest.TestCase):
    def test_persistent_com_worker_mock_plcsim_real_and_input_read(self):
        store = {"REAL": 0.0, "I0.0": True}

        def mock_handler(req):
            cmd = req.get("cmd")
            if cmd == "plcsim_write" and req.get("data_size") == "REAL":
                store["REAL"] = float(req["value"])
                return {"ok": True, "state": "RUN_P"}
            if cmd == "plcsim_read":
                if req.get("area") == "I":
                    return {"ok": True, "value": store["I0.0"], "state": "RUN_P"}
                return {"ok": True, "value": store["REAL"], "state": "RUN_P"}
            if cmd == "list_projects":
                return {"ok": True, "projects": [{"name": "PlantA", "type": 1, "is_user_project": True}]}
            return {"ok": True}

        worker = PersistentComWorker(mock_handler=mock_handler)
        bridge = SERVER.SimaticComBridge(worker=worker)
        client = SERVER.S7RuntimeClient(com_bridge=bridge)

        # Verify no 'TEST' hardcoding and sub-ms dispatch
        ok, projs, _ = bridge.list_projects()
        self.assertTrue(ok)
        self.assertEqual(projs[0]["name"], "PlantA")

        # Verify REAL float write preserves fractional component (was int()-truncated before!)
        os.environ["S7_WRITES_ENABLED"] = "true"
        try:
            wr = client.write_memory("DB1.DBD0", 123.456, host="plcsim", data_type="REAL", confirm_live_write=True)
            self.assertTrue(wr["ok"], f"PLCSIM REAL write failed: {wr}")
            self.assertAlmostEqual(store["REAL"], 123.456, places=4)

            rd_in = client.read_memory("I 0.0", host="plcsim")
            self.assertTrue(rd_in["ok"], f"PLCSIM I-area read failed: {rd_in}")
            self.assertIs(rd_in["value"], True)
        finally:
            os.environ.pop("S7_WRITES_ENABLED", None)

    def test_doctor_client_configs_resources_and_prompts(self):
        doc = json.loads(SERVER.s7_doctor())
        self.assertTrue(doc["success"])
        self.assertGreater(doc["capabilities"]["hardware_catalog_modules"], 30)

        for cli in ("vscode", "antigravity", "codex", "claude"):
            cfg = generate_client_config(cli, mode="read-only", writes_enabled=False)
            self.assertTrue(cfg["ok"])
            self.assertIn("simatic-s7-classic", cfg["rendered"])

        cat_res = json.loads(SERVER.resource_s7_catalog())
        self.assertGreater(cat_res["module_count"], 30)
        self.assertIn("BLKLANG", SERVER.resource_s7_iec_reference())
        self.assertIn("MyPlant", SERVER.s7_new_project_wizard("MyPlant"))


class TestEndToEndSimaticManagerWorkflow(unittest.TestCase):
    """Verifies the complete SIMATIC Manager end-to-end workflow across COM, HW Config,
    Symbols, all IEC 61131-3 languages, PLCSIM, VirtualS7PLC, and Runtime Monitoring."""

    def setUp(self):
        self._orig_mode = SERVER.ACTIVE_ACCESS_MODE
        self._orig_env = os.environ.get("S7_WRITES_ENABLED")
        self._orig_bridge = SERVER.controller.com_bridge
        self._orig_rt_bridge = SERVER.runtime_client.com_bridge

    def tearDown(self):
        SERVER.set_access_mode(self._orig_mode)
        if self._orig_env is None:
            os.environ.pop("S7_WRITES_ENABLED", None)
        else:
            os.environ["S7_WRITES_ENABLED"] = self._orig_env
        SERVER.controller.com_bridge = self._orig_bridge
        SERVER.runtime_client.com_bridge = self._orig_rt_bridge

    def test_full_simatic_manager_lifecycle_via_mock_com_and_virtual_plc(self):
        recorded: list = []
        stored_blocks: dict = {}
        stored_symbols_text = ""
        stored_cfg_text = ""

        def mock_com(req):
            nonlocal stored_symbols_text, stored_cfg_text
            cmd = req.get("cmd")
            recorded.append((cmd, dict(req)))

            if cmd == "create_project":
                return {"ok": True, "project_name": req["project_name"], "project_dir": req.get("project_dir") or "/tmp/s7proj"}
            if cmd == "import_station_cfg":
                stored_cfg_text = Path(req["cfg_path"]).read_text(encoding="utf-8")
                return {"ok": True, "project_name": req["project_name"], "station_name": req.get("station_name") or "SIMATIC 300(1)", "programs": ["S7 Program(1)"]}
            if cmd == "export_station_cfg":
                out_p = Path(req.get("cfg_path") or req.get("output_path"))
                out_p.write_text(stored_cfg_text, encoding="utf-8")
                return {"ok": True, "cfg_path": str(out_p)}
            if cmd == "import_symbols":
                stored_symbols_text = Path(req["input_path"]).read_text(encoding="latin1")
                return {"ok": True, "project_name": req.get("project_name") or "PlantE2E"}
            if cmd == "export_symbols":
                out_p = Path(req["output_path"])
                out_p.write_text(stored_symbols_text, encoding="latin1")
                return {"ok": True, "output_path": str(out_p)}
            if cmd == "compile_source":
                src = Path(req["source_path"]).read_text(encoding="utf-8")
                stored_blocks[req["block_name"]] = src
                return {"ok": True, "project_name": req.get("project_name") or "PlantE2E", "compile_code": 0, "source_type": req.get("source_type")}
            if cmd == "export_block":
                blk = req["block_name"]
                if blk not in stored_blocks:
                    return {"ok": False, "error": f"Block {blk} not found"}
                Path(req["output_path"]).write_text(stored_blocks[blk], encoding="utf-8")
                return {"ok": True, "output_path": req["output_path"]}
            if cmd == "browse_project_tree":
                return {"ok": True, "project_name": req.get("project_name") or "PlantE2E", "stations": [{"name": "SIMATIC 300(1)"}], "programs": [{"name": "S7 Program(1)", "blocks": sorted(stored_blocks.keys()), "symbol_table_available": True}]}
            if cmd == "copy_block":
                stored_blocks[req["target_block"]] = stored_blocks.get(req["source_block"], "")
                return {"ok": True, "source_block": req["source_block"], "target_block": req["target_block"]}
            if cmd == "delete_block":
                stored_blocks.pop(req["block_name"], None)
                return {"ok": True, "deleted": req["block_name"]}
            if cmd == "download_blocks":
                return {"ok": True, "downloaded": req.get("blocks"), "overwrite": req.get("overwrite")}
            if cmd == "upload_station":
                return {"ok": True, "rack": req.get("rack"), "slot": req.get("slot"), "address": req.get("address"), "subnet_type": req.get("subnet_type")}
            if cmd == "plcsim_get_state":
                return {"ok": True, "cpu_state": "RUN_P", "scan_mode": "SINGLE"}
            if cmd == "plcsim_set_scan_mode":
                return {"ok": True, "scan_mode": req.get("scan_mode")}
            if cmd == "plcsim_step_scan":
                return {"ok": True, "cycles_executed": req.get("cycles")}
            if cmd == "plcsim_batch_write":
                return {"ok": True, "item_count": len(req.get("items", [])), "items": [{"address": i["address"], "ok": True} for i in req.get("items", [])]}
            if cmd == "plcsim_batch_read":
                return {"ok": True, "item_count": len(req.get("items", [])), "items": [{"address": i["address"], "value": True, "ok": True} for i in req.get("items", [])]}
            if cmd == "plcsim_read":
                return {"ok": True, "value": 12.5, "cpu_state": "RUN_P"}
            if cmd == "plcsim_write":
                return {"ok": True, "cpu_state": "RUN_P"}
            return {"ok": True}

        worker = PersistentComWorker(mock_handler=mock_com)
        bridge = SERVER.SimaticComBridge(worker=worker)
        bridge.is_available = lambda: True  # Simulate Windows COM availability
        SERVER.controller.com_bridge = bridge
        SERVER.runtime_client.com_bridge = bridge
        SERVER.set_access_mode(AccessMode.FULL)
        os.environ["S7_WRITES_ENABLED"] = "true"

        # 1. Create STEP 7 project via COM
        cp = json.loads(SERVER.s7_com_create_project("PlantE2E", project_path="/tmp/PlantE2E", create_station=True))
        self.assertTrue(cp["success"])
        cp_req = next(r for c, r in recorded if c == "create_project")
        self.assertEqual(cp_req["project_dir"], "/tmp/PlantE2E")
        self.assertTrue(cp_req["create_station"])

        # 2. Configure DIN rail, export .cfg, push to COM, and re-export from COM
        hw = json.loads(SERVER.s7_hw_configure_station(
            station_name="SIMATIC 300(1)",
            plc_family="S7-300",
            modules=[
                {"slot": 1, "module": "PS 307 5A"},
                {"slot": 2, "module": "6ES7 315-2EH14-0AB0", "ip_address": "192.168.0.10"},
                {"slot": 4, "module": "DI16xDC24V"},
                {"slot": 5, "module": "DO16xDC24V"},
            ],
        ))
        self.assertTrue(hw["success"])
        exp_cfg = json.loads(SERVER.s7_hw_export_cfg(station_name="SIMATIC 300(1)"))
        imp_cfg = json.loads(SERVER.s7_hw_import_cfg(cfg_path=exp_cfg["cfg_path"], project_name="PlantE2E", push_to_com=True))
        self.assertTrue(imp_cfg["success"])
        self.assertTrue(imp_cfg["com_import"]["ok"])
        exp_from_com = json.loads(SERVER.s7_hw_export_cfg(station_name="SIMATIC 300(1)", from_com_project="PlantE2E"))
        self.assertTrue(exp_from_com["success"])
        self.assertIn("6ES7 315-2EH14-0AB0", exp_from_com["cfg_text"])

        # 3. Import & Export live Symbol Table via COM
        imp_sym = json.loads(SERVER.s7_com_import_symbols(
            symbols={"START_PB": "I 0.0", "STOP_PB": "I 0.1", "MOTOR_Q": "Q 0.0"},
            project_name="PlantE2E",
        ))
        self.assertTrue(imp_sym["success"])
        self.assertEqual(imp_sym["symbol_count"], 3)
        exp_sym = json.loads(SERVER.s7_com_export_symbols(project_name="PlantE2E"))
        self.assertTrue(exp_sym["success"])
        self.assertEqual(exp_sym["symbol_count"], 3)

        # 4. Program all IEC 61131-3 types: UDT, DB (via s7_create_data_block AND s7_com_write_block!), LAD, FBD, SCL, S7-GRAPH
        udt = json.loads(SERVER.s7_create_udt(1, [{"name": "Run", "type": "BOOL"}, {"name": "Speed", "type": "INT"}], project_name="PlantE2E", compile_via_com=True))
        self.assertTrue(udt["success"])
        db = json.loads(SERVER.s7_create_data_block(10, [{"name": "Run", "type": "BOOL"}, {"name": "Speed", "type": "INT"}], project_name="PlantE2E", compile_via_com=True))
        self.assertTrue(db["success"])
        # Ensure s7_com_write_block with default convert_to_lad=True does NOT corrupt DATA_BLOCK source
        db_direct = json.loads(SERVER.s7_com_write_block(block_name="DB11", source_code=db["awl_source"].replace("DB 10", "DB 11"), project_name="PlantE2E", convert_to_lad=True))
        self.assertTrue(db_direct["success"], f"DB direct COM write failed: {db_direct}")
        self.assertIn("DATA_BLOCK DB 11", stored_blocks["DB11"])

        ob1_lad = json.loads(SERVER.s7_com_write_block(
            block_name="OB1",
            networks=[{"title": "Latch", "instructions": ["A I 0.0", "AN I 0.1", "= Q 0.0"]}],
            project_name="PlantE2E",
            convert_to_lad=True,
        ))
        self.assertTrue(ob1_lad["success"])
        self.assertEqual(ob1_lad["block_language"], "LAD")

        fc_fbd = json.loads(SERVER.s7_com_write_block(
            block_name="FC2",
            networks=[{"title": "Interlock", "instructions": ["A I 0.2", "= Q 0.1"]}],
            project_name="PlantE2E",
            convert_to_lad=True,
            target_language="FBD",
        ))
        self.assertTrue(fc_fbd["success"])
        self.assertEqual(fc_fbd["block_language"], "FBD")

        scl = json.loads(SERVER.s7_write_scl_block(
            scl_code="FUNCTION FC 3 : VOID\nVAR_INPUT\n  En : BOOL;\nEND_VAR\nBEGIN\n  IF En THEN\n    ;\n  END_IF;\nEND_FUNCTION\n",
            block_name="FC3",
            project_name="PlantE2E",
        ))
        self.assertTrue(scl["success"])
        scl_req = [r for c, r in recorded if c == "compile_source" and r["block_name"] == "FC3"][-1]
        self.assertEqual(scl_req["source_type"], 66)

        gr7 = json.loads(SERVER.s7_write_graph_block(
            fb_number=5,
            steps=[
                {"number": 1, "name": "S_IDLE", "initial": True, "actions": [{"qualifier": "N", "operand": "Q 0.0"}]},
                {"number": 2, "name": "S_RUN", "actions": [{"qualifier": "S", "operand": "Q 0.1"}]},
            ],
            transitions=[
                {"number": 1, "name": "T1", "from_step": 1, "to_step": 2, "condition": "I 0.0"},
                {"number": 2, "name": "T2", "from_step": 2, "to_step": 1, "condition": "I 0.1"},
            ],
            project_name="PlantE2E",
        ))
        self.assertTrue(gr7["success"])
        gr7_req = [r for c, r in recorded if c == "compile_source" and r["block_name"] == "FB5"][-1]
        self.assertEqual(gr7_req["source_type"], 67)

        # 5. Browse tree, copy block, compare blocks, delete block
        tree = json.loads(SERVER.s7_com_browse_project_tree("PlantE2E"))
        self.assertTrue(tree["success"])
        cp_blk = json.loads(SERVER.s7_com_copy_block("OB1", "OB100", project_name="PlantE2E"))
        self.assertTrue(cp_blk["success"])
        cp_blk_req = next(r for c, r in recorded if c == "copy_block")
        self.assertEqual(cp_blk_req["source_project"], "PlantE2E")
        self.assertEqual(cp_blk_req["target_block"], "OB100")

        diff = json.loads(SERVER.s7_compare_blocks(stored_blocks["OB1"], stored_blocks["OB100"], "OB1", "OB100"))
        self.assertTrue(diff["identical"])
        del_blk = json.loads(SERVER.s7_com_delete_block("OB100", project_name="PlantE2E", confirm_delete=True))
        self.assertTrue(del_blk["success"])

        # 6. Selective download & upload station with MPI/rack/slot parameters
        dl = json.loads(SERVER.s7_com_download_blocks(["OB1", "DB10"], project_name="PlantE2E", overwrite=False, confirm_live_write=True))
        self.assertTrue(dl["success"])
        dl_req = next(r for c, r in recorded if c == "download_blocks")
        self.assertEqual(dl_req["blocks"], ["OB1", "DB10"])
        self.assertFalse(dl_req["overwrite"])

        ul = json.loads(SERVER.s7_com_upload_station(project_name="PlantE2E", rack=0, slot=2, address="2", subnet_type="MPI"))
        self.assertTrue(ul["success"])
        ul_req = next(r for c, r in recorded if c == "upload_station")
        self.assertEqual(ul_req["subnet_type"], "MPI")
        self.assertEqual(ul_req["slot"], 2)

        # 7. PLCSIM control, ping, single & batch read/write (verifying scan_mode + cpu_state)
        sm = json.loads(SERVER.s7_plcsim_control(action="set_scan_mode", scan_mode="SINGLE"))
        self.assertTrue(sm["success"])
        self.assertEqual(sm["scan_mode"], "SINGLE")
        p_sim = json.loads(SERVER.s7_runtime_ping(host="plcsim"))
        self.assertTrue(p_sim["success"])
        self.assertEqual(p_sim["cpu_state"], "RUN_P")
        rd_sim = json.loads(SERVER.s7_runtime_read("DB1.DBD0", data_type="REAL", host="plcsim"))
        self.assertEqual(rd_sim["cpu_state"], "RUN_P")
        br_sim = json.loads(SERVER.s7_runtime_batch_read(["I0.0", "Q0.0"], host="plcsim"))
        self.assertTrue(br_sim["success"])
        self.assertEqual(br_sim["transport"], "plcsim_s7prosim_com")

        # 8. VirtualS7PLC first-class runtime target (`host='virtual'`) across ALL runtime tools
        self.assertTrue(json.loads(SERVER.s7_runtime_ping(host="virtual"))["success"])
        self.assertTrue(json.loads(SERVER.s7_runtime_write("MW20", 1234, data_type="INT", host="virtual", confirm_live_write=True))["success"])
        self.assertEqual(json.loads(SERVER.s7_runtime_read("MW20", data_type="INT", host="virtual"))["value"], 1234)
        self.assertTrue(json.loads(SERVER.s7_runtime_batch_write([{"address": "M5.0", "value": True}, {"address": "T2", "value": 50}], host="virtual", confirm_live_write=True))["success"])
        vbr = json.loads(SERVER.s7_runtime_batch_read(["M5.0", "MW20"], host="virtual"))
        self.assertTrue(vbr["success"])
        vat_v1 = json.loads(SERVER.s7_monitor_watch_table(["M5.0", "MW20"], host="virtual", table_id="VAT_VIRT"))
        self.assertTrue(vat_v1["success"])
        SERVER.VIRTUAL_PLC.write_address("M5.0", False)
        vat_v2 = json.loads(SERVER.s7_monitor_watch_table(["M5.0", "MW20"], host="virtual", table_id="VAT_VIRT"))
        self.assertEqual(len(vat_v2["latest"]["changed"]), 1)
        self.assertTrue(json.loads(SERVER.s7_runtime_cpu_control("STOP", host="virtual", confirm_live_write=True))["success"])
        self.assertEqual(json.loads(SERVER.s7_runtime_cpu_diagnostics(host="virtual"))["cpu_mode"], "STOP")
        SERVER.VIRTUAL_PLC.set_mode("RUN")


class TestAuditRemediationsAndAnnotations(unittest.TestCase):
    def test_s7_access_mode_env_var_matches_client_config(self):
        from simatic_s7_mcp.contracts import resolve_access_mode

        orig_a = os.environ.get("S7_ACCESS_MODE")
        orig_m = os.environ.get("S7_MCP_MODE")
        try:
            os.environ.pop("S7_MCP_MODE", None)
            os.environ["S7_ACCESS_MODE"] = "read-only"
            self.assertEqual(resolve_access_mode(None), AccessMode.READ_ONLY)
            os.environ["S7_ACCESS_MODE"] = "full"
            self.assertEqual(resolve_access_mode(None), AccessMode.FULL)
        finally:
            if orig_a is None:
                os.environ.pop("S7_ACCESS_MODE", None)
            else:
                os.environ["S7_ACCESS_MODE"] = orig_a
            if orig_m is None:
                os.environ.pop("S7_MCP_MODE", None)
            else:
                os.environ["S7_MCP_MODE"] = orig_m

    def test_iec_reference_and_prompts_use_valid_tool_names(self):
        ref_txt = SERVER.resource_s7_iec_reference()
        prompt_txt = SERVER.s7_convert_and_verify_lad("FC1")
        wizard_txt = SERVER.s7_new_project_wizard("Line1")
        self.assertIn("s7_convert_to_ladder", ref_txt)
        self.assertNotIn("s7_convert_to_lad`", ref_txt)
        self.assertIn("s7_convert_to_ladder", prompt_txt)
        self.assertIn("s7_com_import_symbols", wizard_txt)

    def test_native_bridge_fallback_required_delegates_to_ps32(self):
        calls = []

        def fake_fallback(req):
            calls.append(req["cmd"])
            return {"ok": True, "via": "ps32_fallback", "cmd": req["cmd"]}

        fallback_worker = PersistentComWorker(mock_handler=fake_fallback)
        primary = PersistentComWorker(mock_handler=lambda req: {"ok": False, "fallback_required": True, "error": "extended cmd"})
        primary._fallback_ps_worker = fallback_worker
        res = primary.call("import_station_cfg", {"cfg_path": "x.cfg"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "ps32_fallback")
        self.assertEqual(calls, ["import_station_cfg"])

    def test_every_registered_mcp_tool_has_annotations(self):
        if hasattr(SERVER.mcp, "_tool_manager"):
            tools = SERVER.mcp._tool_manager.list_tools()
            self.assertGreaterEqual(len(tools), 54)
            missing = [t.name for t in tools if t.annotations is None]
        else:
            tools_map = getattr(SERVER.mcp, "_tools", {})
            self.assertGreaterEqual(len(tools_map), 54)
            missing = [name for name, meta in tools_map.items() if meta.get("annotations") is None]
        self.assertEqual(missing, [], f"Tools missing MCP ToolAnnotations: {missing}")


class TestS7300EndToEndSimulationCases(unittest.TestCase):
    """Realistic S7-300 industrial control cases executed end-to-end through the MCP tools."""

    def setUp(self) -> None:
        SERVER.VIRTUAL_PLC.reset(clear_blocks=True, clear_symbols=True)

    def test_case1_star_delta_motor_starter_converted_to_pure_lad(self):
        """Write Star-Delta networks -> convert via s7_convert_to_ladder -> load & simulate full cycle."""
        symbols = {
            "START_PB": "I 0.0",
            "STOP_NC": "I 0.1",
            "OL_NC": "I 0.2",
            "KM_MAIN": "Q 0.0",
            "KM_STAR": "Q 0.1",
            "KM_DELTA": "Q 0.2",
        }
        networks = [
            {
                "title": "Main Contactor Latch",
                "instructions": [
                    "O I 0.0",
                    "O Q 0.0",
                    "A I 0.1",
                    "A I 0.2",
                    "= Q 0.0",
                ],
            },
            {
                "title": "Star-Delta Changeover Timer (500ms)",
                "instructions": [
                    "A Q 0.0",
                    "L S5T#500MS",
                    "SD T 1",
                ],
            },
            {
                "title": "Star Contactor (Mutually Interlocked)",
                "instructions": [
                    "A Q 0.0",
                    "AN T 1",
                    "AN Q 0.2",
                    "= Q 0.1",
                ],
            },
            {
                "title": "Delta Contactor (Mutually Interlocked)",
                "instructions": [
                    "A Q 0.0",
                    "A T 1",
                    "AN Q 0.1",
                    "= Q 0.2",
                ],
            },
        ]
        # Validate and convert to pure STEP 7 Classic LAD (uses BLD, NOP 0, L-stack & BR)
        val = json.loads(SERVER.s7_validate_program(networks=networks, symbols=symbols))
        self.assertTrue(val["valid"], val)
        gen_awl = SERVER.generate_s7_awl_source(
            networks=[
                SERVER.S7NetworkSpec(title=n["title"], comment="", instructions=n["instructions"])
                for n in networks
            ],
            block_type="OB",
            block_number=1,
        )
        conv = json.loads(SERVER.s7_convert_to_ladder(gen_awl))
        self.assertTrue(conv["success"], conv)
        lad_src = conv["converted_code"]

        loaded = json.loads(SERVER.s7_virtual_plc_load(
            block_name="OB1",
            source_code=lad_src,
            symbols=symbols,
            reset_memory=True,
            clear_blocks=True,
        ))
        self.assertTrue(loaded["success"], loaded)
        self.assertEqual(loaded["unsupported_instructions"], [])

        scenario = json.loads(SERVER.s7_plcsim_run_scenario(
            target="virtual",
            entry_block="OB1",
            steps=[
                {
                    "name": "Idle with NC Stop & Overload healthy",
                    "inputs": {"START_PB": False, "STOP_NC": True, "OL_NC": True},
                    "cycles": 1,
                    "tick_ms": 100,
                    "expect": {"KM_MAIN": False, "KM_STAR": False, "KM_DELTA": False},
                },
                {
                    "name": "Press Start -> Main + Star energized, Delta OFF",
                    "inputs": {"START_PB": True},
                    "cycles": 2,
                    "tick_ms": 100,
                    "watch": ["T1"],
                    "expect": {"KM_MAIN": True, "KM_STAR": True, "KM_DELTA": False, "T1": False},
                },
                {
                    "name": "Release Start (latched) & complete 500ms changeover -> Delta ON, Star OFF",
                    "inputs": {"START_PB": False},
                    "cycles": 5,
                    "tick_ms": 100,
                    "expect": {"KM_MAIN": True, "T1": True, "KM_STAR": False, "KM_DELTA": True},
                },
                {
                    "name": "Thermal overload trips -> all contactors de-energize",
                    "inputs": {"OL_NC": False},
                    "cycles": 2,
                    "tick_ms": 100,
                    "expect": {"KM_MAIN": False, "KM_STAR": False, "KM_DELTA": False, "T1": False},
                },
            ],
        ))
        self.assertTrue(scenario["success"], scenario)
        self.assertEqual(scenario["passed_steps"], 4)

    def test_case2_analog_temperature_controller_piw_real_pqw_and_signed_int(self):
        """S7-300 PIW256 (0..27648) -> REAL 0..100.0 C -> hysteresis heater Q0.0 + PQW256 + negative INT."""
        awl = """
ORGANIZATION_BLOCK OB 1
TITLE = "Analog Temp Controller"
VERSION : 0.1
BEGIN
NETWORK
TITLE = "Scale PIW 256 (0..27648) to 0.0..100.0 C in MD 20"
  L PIW 256
  ITD
  DTR
  L 100.0
  *R
  L 27648.0
  /R
  T MD 20

NETWORK
TITLE = "Heater hysteresis: ON below 40.0 C, OFF above 60.0 C"
  L MD 20
  L 40.0
  <R
  S Q 0.0
  L MD 20
  L 60.0
  >R
  R Q 0.0

NETWORK
TITLE = "Analog Valve Output PQW 256 = 27648 when Heater ON else 0"
  A Q 0.0
  JCN M_OFF
  L 27648
  T PQW 256
  JU M_ERR
M_OFF: L 0
  T PQW 256

NETWORK
TITLE = "Signed INT deviation MW 10 = TempInt - 50 (tests negative INT)"
M_ERR: L MD 20
  RND
  L 50
  -I
  T MW 10
END_ORGANIZATION_BLOCK
"""
        loaded = json.loads(SERVER.s7_virtual_plc_load(
            block_name="OB1",
            source_code=awl,
            reset_memory=True,
            clear_blocks=True,
        ))
        self.assertTrue(loaded["success"], loaded)
        self.assertEqual(loaded["unsupported_instructions"], [])

        scenario = json.loads(SERVER.s7_plcsim_run_scenario(
            target="virtual",
            steps=[
                {
                    "name": "Cold 25.0 C (PIW256=6912) -> Heater ON, PQW256=27648, Deviation=-25",
                    "inputs": {"PIW256": 6912},
                    "cycles": 1,
                    "tick_ms": 50,
                    "watch": ["MD20:REAL", "PQW256:INT", "MW10:INT"],
                    "expect": {
                        "MD20": {"value": 25.0, "data_type": "REAL", "tolerance": 0.05},
                        "Q0.0": True,
                        "PQW256": 27648,
                        "MW10": -25,
                    },
                },
                {
                    "name": "Midband 50.0 C (PIW256=13824) -> Hysteresis holds Heater ON, Deviation=0",
                    "inputs": {"PIW256": 13824},
                    "cycles": 1,
                    "tick_ms": 50,
                    "expect": {
                        "MD20": 50.0,
                        "Q0.0": True,
                        "PQW256": 27648,
                        "MW10": 0,
                    },
                },
                {
                    "name": "Hot 75.0 C (PIW256=20736) -> Heater OFF, PQW256=0, Deviation=+25",
                    "inputs": {"PIW256": 20736},
                    "cycles": 1,
                    "tick_ms": 50,
                    "expect": {
                        "MD20": 75.0,
                        "Q0.0": False,
                        "PQW256": 0,
                        "MW10": 25,
                    },
                },
            ],
        ))
        self.assertTrue(scenario["success"], scenario)
        # Verify S7RuntimeClient host="virtual" also reads PIW/PQW cleanly
        rd_piw = json.loads(SERVER.s7_runtime_read("PIW 256", data_type="INT", host="virtual"))
        self.assertTrue(rd_piw["success"], rd_piw)
        self.assertEqual(rd_piw["value"], 20736)

    def test_case3_modular_ob_fc_db_symbols_german_mnemonics_and_inspect(self):
        """OB1 calls FC1 with VAR_INPUT/VAR_OUTPUT + DB10 initial values + German U/UN/E/A + s7_virtual_plc_inspect."""
        db10 = """
DATA_BLOCK DB 10
TITLE = "Pump Parameters"
VERSION : 0.1
STRUCT
  AutoMode : BOOL := TRUE;
  MaxSpeed : INT := 1500;
  ActualSpeed : INT := 0;
END_STRUCT ;
BEGIN
  AutoMode := TRUE;
  MaxSpeed := 1500;
  ActualSpeed := 0;
END_DATA_BLOCK
"""
        fc1 = """
FUNCTION FC 1 : VOID
VAR_INPUT
  Enable : BOOL ;
  TargetSpd : INT ;
END_VAR
VAR_OUTPUT
  Running : BOOL ;
  OutSpd : INT ;
END_VAR
BEGIN
NETWORK
TITLE = "German Mnemonics Motor Interlock & Speed Pass-through"
  U #Enable
  = #Running
  SPBN M_STOP
  L #TargetSpd
  T #OutSpd
  SPA M_END
M_STOP: L 0
  T #OutSpd
M_END: NOP 0
END_FUNCTION
"""
        ob1 = """
ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = "Call FC 1 with Symbolic & DB Field Parameters"
  CALL FC 1 (
    Enable := "PUMP_CMD",
    TargetSpd := DB10.MaxSpeed,
    Running := "PUMP_RUN",
    OutSpd := DB10.ActualSpeed
  )
END_ORGANIZATION_BLOCK
"""
        symbols = {"PUMP_CMD": "E 0.0", "PUMP_RUN": "A 0.0", "PumpDB": "DB 10"}
        r_db = json.loads(SERVER.s7_virtual_plc_load("DB10", db10, symbols=symbols, reset_memory=True, clear_blocks=True))
        self.assertTrue(r_db["success"], r_db)
        r_fc = json.loads(SERVER.s7_virtual_plc_load("FC1", fc1))
        self.assertTrue(r_fc["success"], r_fc)
        r_ob = json.loads(SERVER.s7_virtual_plc_load("OB1", ob1))
        self.assertTrue(r_ob["success"], r_ob)

        step1 = json.loads(SERVER.s7_virtual_plc_step(
            cycles=1,
            tick_ms=100,
            write_inputs={"PUMP_CMD": True},
            read_addresses=["PUMP_RUN", "DB10.ActualSpeed", "DB10.AutoMode"],
        ))
        self.assertTrue(step1["success"], step1)
        self.assertTrue(step1["observed"]["PUMP_RUN"])
        self.assertEqual(step1["observed"]["DB10.ActualSpeed"], 1500)
        self.assertTrue(step1["observed"]["DB10.AutoMode"])

        insp = json.loads(SERVER.s7_virtual_plc_inspect())
        self.assertTrue(insp["success"], insp)
        self.assertIn("OB1", insp["loaded_blocks"])
        self.assertIn("FC1", insp["loaded_blocks"])
        self.assertIn("DB10", insp["data_blocks"])
        self.assertEqual(insp["unsupported_instructions"], [])

    def test_case4_scl_tank_batching_sequencer(self):
        """SCL block with CASE, IF/ELSIF, FOR loop loaded & simulated directly in VirtualS7PLC."""
        scl_ob1 = """
ORGANIZATION_BLOCK OB1
VAR_TEMP
  i : INT;
  acc : INT;
END_VAR
BEGIN
  IF I0.0 THEN
    MW0 := 1;
  ELSIF I0.1 THEN
    MW0 := 2;
  ELSE
    MW0 := 0;
  END_IF;

  CASE MW0 OF
    1:
      Q0.0 := TRUE;
      Q0.1 := FALSE;
      acc := 0;
      FOR i := 1 TO 4 DO
        acc := acc + i;
      END_FOR;
      MW2 := acc;
    2:
      Q0.0 := FALSE;
      Q0.1 := TRUE;
      MW2 := 100;
    ELSE
      Q0.0 := FALSE;
      Q0.1 := FALSE;
      MW2 := 0;
  END_CASE;
END_ORGANIZATION_BLOCK
"""
        loaded = json.loads(SERVER.s7_virtual_plc_load(
            block_name="OB1",
            source_code=scl_ob1,
            language="SCL",
            reset_memory=True,
            clear_blocks=True,
        ))
        self.assertTrue(loaded["success"], loaded)
        self.assertEqual(loaded["language"], "SCL")

        scenario = json.loads(SERVER.s7_plcsim_run_scenario(
            target="virtual",
            steps=[
                {
                    "name": "Fill state (I0.0=True) computes FOR sum 1+2+3+4=10",
                    "inputs": {"I0.0": True, "I0.1": False},
                    "cycles": 1,
                    "expect": {"MW0": 1, "Q0.0": True, "Q0.1": False, "MW2": 10},
                },
                {
                    "name": "Drain state (I0.1=True) sets Q0.1 and MW2=100",
                    "inputs": {"I0.0": False, "I0.1": True},
                    "cycles": 1,
                    "expect": {"MW0": 2, "Q0.0": False, "Q0.1": True, "MW2": 100},
                },
                {
                    "name": "Idle state clears outputs",
                    "inputs": {"I0.0": False, "I0.1": False},
                    "cycles": 1,
                    "expect": {"MW0": 0, "Q0.0": False, "Q0.1": False, "MW2": 0},
                },
            ],
        ))
        self.assertTrue(scenario["success"], scenario)

    def test_case5_s7_graph_packaging_line_sequencer(self):
        """Generate S7-GRAPH .gr7 via s7_write_graph_block -> load & simulate SFC step transitions."""
        gr7_res = json.loads(SERVER.s7_write_graph_block(
            fb_number=5,
            steps=[
                {"number": 1, "name": "S_IDLE", "initial": True, "actions": [{"qualifier": "N", "operand": "Q 0.0"}]},
                {"number": 2, "name": "S_FILL", "actions": [{"qualifier": "N", "operand": "Q 0.1"}]},
                {"number": 3, "name": "S_SEAL", "actions": [{"qualifier": "N", "operand": "Q 0.2"}]},
            ],
            transitions=[
                {"number": 1, "name": "T_START", "from_step": 1, "to_step": 2, "condition": "I 0.0"},
                {"number": 2, "name": "T_FULL", "from_step": 2, "to_step": 3, "condition": "I 0.1"},
                {"number": 3, "name": "T_DONE", "from_step": 3, "to_step": 1, "condition": "I 0.2"},
            ],
        ))
        self.assertTrue(gr7_res["success"], gr7_res)
        gr7_src = gr7_res["gr7_source"]

        loaded_fb = json.loads(SERVER.s7_virtual_plc_load(
            block_name="FB5",
            source_code=gr7_src,
            reset_memory=True,
            clear_blocks=True,
        ))
        self.assertTrue(loaded_fb["success"], loaded_fb)
        self.assertEqual(loaded_fb["language"], "GRAPH")

        loaded_ob = json.loads(SERVER.s7_virtual_plc_load(
            block_name="OB1",
            source_code="ORGANIZATION_BLOCK OB 1\nBEGIN\nNETWORK\n  CALL FB 5, DB 5\nEND_ORGANIZATION_BLOCK\n",
        ))
        self.assertTrue(loaded_ob["success"], loaded_ob)

        scenario = json.loads(SERVER.s7_plcsim_run_scenario(
            target="virtual",
            steps=[
                {
                    "name": "Initial step S_IDLE active -> Q0.0 ON",
                    "inputs": {"I0.0": False, "I0.1": False, "I0.2": False},
                    "cycles": 1,
                    "expect": {"Q0.0": True, "Q0.1": False, "Q0.2": False},
                },
                {
                    "name": "Trigger Start (I0.0) -> transitions S_IDLE -> S_FILL -> Q0.1 ON",
                    "inputs": {"I0.0": True},
                    "cycles": 2,
                    "expect": {"Q0.0": False, "Q0.1": True, "Q0.2": False},
                },
                {
                    "name": "Level Full (I0.1) -> transitions S_FILL -> S_SEAL -> Q0.2 ON",
                    "inputs": {"I0.0": False, "I0.1": True},
                    "cycles": 2,
                    "expect": {"Q0.0": False, "Q0.1": False, "Q0.2": True},
                },
                {
                    "name": "Seal Done (I0.2) -> returns to S_IDLE -> Q0.0 ON",
                    "inputs": {"I0.1": False, "I0.2": True},
                    "cycles": 2,
                    "expect": {"Q0.0": True, "Q0.1": False, "Q0.2": False},
                },
            ],
        ))
        self.assertTrue(scenario["success"], scenario)
        self.assertEqual(scenario["steps"][-1]["sfc_active_steps"]["FB5:DB5"], ["S_IDLE"])

        # Also verify s7_virtual_plc_load(networks=...) direct structured-network loading shortcut
        direct_net = json.loads(SERVER.s7_virtual_plc_load(
            block_name="OB1",
            networks=[{"title": "Direct Rung", "instructions": ["A I 0.0", "= Q 0.7"]}],
            reset_memory=True,
            clear_blocks=True,
        ))
        self.assertTrue(direct_net["success"], direct_net)
        st = json.loads(SERVER.s7_virtual_plc_step(write_inputs={"I0.0": True}, read_addresses=["Q0.7"]))
        self.assertTrue(st["observed"]["Q0.7"])


class TestAuditRemediationBugs01To14(unittest.TestCase):
    """Regression suite covering every bug from the empirical feature audit (BUG-01..BUG-14)."""

    def test_bug01_com_worker_uses_ps1_file_not_encoded_command(self):
        from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT
        # Ensure daemon script is well above the 8 KB EncodedCommand UTF-16LE limit and uses -File
        self.assertGreater(len(_PERSISTENT_PS32_DAEMON_SCRIPT), 8000)
        worker = PersistentComWorker()
        captured_argv = []

        class _FakeProc:
            def __init__(self, argv):
                captured_argv.extend(argv)
                self.stdin = mock.MagicMock()
                self.stdout = mock.MagicMock()
                self.stdout.readline.return_value = '{"ok": true, "ready": true, "pid": 999}\n'
                self.stderr = mock.MagicMock()
                self.pid = 999

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        worker._native_exe = Path("/nonexistent/s7_com_bridge.exe")
        worker._ps32_exe = Path("/fake/powershell.exe")
        with mock.patch("simatic_s7_mcp.com_worker.sys.platform", "win32"), \
             mock.patch("simatic_s7_mcp.com_worker.subprocess.Popen", side_effect=lambda argv, **kw: _FakeProc(argv)):
            ok, err = worker._start_locked()
            self.assertTrue(ok, err)
            self.assertIn("-File", captured_argv)
            self.assertNotIn("-EncodedCommand", captured_argv)
            ps1_path = Path(captured_argv[captured_argv.index("-File") + 1])
            self.assertTrue(ps1_path.is_file())
            worker._kill_locked()
            self.assertFalse(ps1_path.exists())

    def test_bug02_com_create_project_already_exists_and_station_codes(self):
        from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT
        self.assertEqual(FailureCategory.ALREADY_EXISTS.value, "already_exists")
        self.assertIn("failure_category = 'already_exists'", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("S7300Station=30, S7400Station=31, S7PCStation=32", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("station_error", _PERSISTENT_PS32_DAEMON_SCRIPT)

        # Verify s7_com_create_project propagates top-level failure_category
        with mock.patch.object(
            SERVER.controller.com_bridge,
            "create_project",
            return_value=(False, "Project 'DupProj' already exists.", {"ok": False, "failure_category": "already_exists"}),
        ):
            res = json.loads(SERVER.s7_com_create_project(project_name="DupProj"))
            self.assertFalse(res["success"])
            self.assertEqual(res["failure_category"], "already_exists")

    def test_bug03_and_bug08_flat_step7_cfg_and_classic_cpu314(self):
        # BUG-08: Classic CPU 314-1AE04-0AB0 must be in catalog
        search_res = json.loads(SERVER.s7_hw_catalog_search(query="314-1AE04"))
        self.assertTrue(search_res["success"])
        self.assertTrue(any(m["mlfb"] == "6ES7 314-1AE04-0AB0" for m in search_res["modules"]))

        # BUG-03: Flat STEP 7 V5.x grammar with quoted firmware and closed STATION block before RACK/SLOT
        cfg = StationHardwareConfig(station_name="SIMATIC 300(1)", plc_family="S7-300")
        self.assertTrue(cfg.add_module(slot=1, module="6ES7 307-1EA01-0AA0")[0])
        self.assertTrue(cfg.add_module(slot=2, module="6ES7 314-1AE04-0AB0")[0])
        self.assertTrue(cfg.add_module(slot=4, module="6ES7 321-1BL00-0AA0")[0])
        cfg_text = cfg.generate_cfg()

        self.assertIn('RACK 0, "6ES7 390-1???0-0AA0", "UR"', cfg_text)
        self.assertIn('RACK 0, SLOT 2, "6ES7 314-1AE04-0AB0" "V1.2", "CPU 314"', cfg_text)
        # STATION block must close with END before RACK 0 begins
        station_pos = cfg_text.index("STATION S7300")
        rack_pos = cfg_text.index('RACK 0, "6ES7 390')
        self.assertIn("\nEND\n", cfg_text[station_pos:rack_pos])

        # Round-trip parse_station_cfg must preserve quoted firmware & slots
        parsed = parse_station_cfg(cfg_text)
        self.assertEqual(parsed.station_name, "SIMATIC 300(1)")
        by_slot = {m.slot: m for m in parsed.modules}
        self.assertEqual(by_slot[2].mlfb, "6ES7 314-1AE04-0AB0")
        self.assertEqual(by_slot[2].firmware, "V1.2")
        self.assertEqual(by_slot[4].mlfb, "6ES7 321-1BL00-0AA0")

    def test_bug04_com_worker_guards_non_dict_json_and_suppresses_com_output(self):
        from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT
        self.assertIn("$null = $found.GenerateSource($outFile, 0)", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $ctx.program.SymbolTable.Export($outFile)", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $ctx.program.SymbolTable.Import($inFile, 1)", _PERSISTENT_PS32_DAEMON_SCRIPT)

        # Python side must gracefully unwrap [..., {dict}] JSON arrays instead of crashing with AttributeError
        worker = PersistentComWorker()
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.stdin = mock.MagicMock()
        fake_proc.stdout = mock.MagicMock()
        fake_proc.stdout.readline.return_value = '[null, {"ok": true, "symbol_count": 5}]\n'
        worker._proc = fake_proc
        res = worker.call("export_symbols", {"output_path": "x.sdf"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["symbol_count"], 5)

        # Pure non-dict JSON returns structured error dict
        fake_proc.stdout.readline.return_value = '[1, 2, 3]\n'
        res_bad = worker.call("export_symbols", {"output_path": "x.sdf"})
        self.assertFalse(res_bad["ok"])
        self.assertIn("Unexpected list response", res_bad["error"])

    def test_bug05_copy_block_non_modal_and_graph_scl_offline_default(self):
        from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT
        # copy_block uses GenerateSource + header rewrite + Compile rather than modal Copy()
        self.assertIn("method = 'generate_source_and_compile'", _PERSISTENT_PS32_DAEMON_SCRIPT)

        # Even if COM bridge reports available, s7_write_graph_block & s7_write_scl_block default to offline when project_name is omitted
        with mock.patch.object(SERVER.controller.com_bridge, "is_available", return_value=True), \
             mock.patch.object(SERVER.controller.com_bridge, "import_and_compile_awl") as mock_compile:
            gr7 = json.loads(SERVER.s7_write_graph_block(
                fb_number=12,
                steps=[{"number": 1, "name": "S_INIT", "initial": True, "actions": [{"qualifier": "N", "operand": "Q 0.0"}]}],
                transitions=[{"number": 1, "name": "T_LOOP", "from_step": 1, "to_step": 1, "condition": "I 0.0"}],
            ))
            self.assertTrue(gr7["success"])
            self.assertEqual(gr7["mode"], "offline")
            self.assertIn("gr7_source", gr7)
            mock_compile.assert_not_called()

            scl = json.loads(SERVER.s7_write_scl_block(
                scl_code="FUNCTION FC 12 : VOID\nBEGIN\n  Q0.0 := I0.0;\nEND_FUNCTION\n",
                block_name="FC12",
            ))
            self.assertTrue(scl["success"])
            self.assertEqual(scl["mode"], "offline")
            mock_compile.assert_not_called()

    def test_bug06_list_blocks_db_language_mapping(self):
        fake_worker_res = {
            "ok": True,
            "project_name": "PlantA",
            "blocks": [
                {"name": "OB1", "concrete_type": 1138945, "language": "LAD"},
                {"name": "DB10", "concrete_type": 1138947, "language": "STL"},
                {"name": "SDB0", "concrete_type": 1138955, "language": ""},
                {"name": "UDT1", "concrete_type": 1138949, "language": "STL"},
            ],
        }
        with mock.patch.object(SERVER.controller.com_bridge.worker, "call", return_value=fake_worker_res):
            ok, blocks, _ = SERVER.controller.com_bridge.list_blocks(project_name="PlantA")
            self.assertTrue(ok)
            by_name = {b["name"]: b["language"] for b in blocks}
            self.assertEqual(by_name["OB1"], "LAD")
            self.assertEqual(by_name["DB10"], "DB")
            self.assertEqual(by_name["SDB0"], "SDB")
            self.assertEqual(by_name["UDT1"], "UDT")

    def test_bug09_multi_block_program_analysis_eliminates_false_positives(self):
        # FC1 sets M10.0 and starts T1; FC2 resets M10.0 and reads T1
        blocks = {
            "FC1": [
                {"title": "Start Latch", "instructions": ["A I 0.0", "S M 10.0", "L S5T#5S", "SD T 1"]},
            ],
            "FC2": [
                {"title": "Reset & Timer Contact", "instructions": ["A T 1", "R M 10.0", "A M 10.0", "= Q 0.0"]},
            ],
        }
        res = json.loads(SERVER.s7_analyze_program(blocks=blocks, plc_type="S7-300"))
        self.assertTrue(res["success"])
        self.assertEqual(res["safety_score"], 100, res)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["hazards"], [])
        self.assertEqual(res["analyzed_blocks"], ["FC1", "FC2"])

    def test_bug10_offline_create_open_project_and_open_block_in_custom_path(self):
        orig_name = SERVER.controller.project_name
        orig_dir = SERVER.controller.project_dir
        orig_off = SERVER.controller.is_offline_project
        try:
            with tempfile.TemporaryDirectory() as td:
                cr = json.loads(SERVER.s7_create_project(
                    project_name="AuditOfflineProj",
                    plc_type="S7-300",
                    project_path=td,
                    launch_if_closed=False,
                ))
                self.assertTrue(cr["success"], cr)
                expected_dir = Path(td).resolve() / "AuditOfflineProj"
                self.assertTrue((expected_dir / "AuditOfflineProj.s7p").is_file())
                self.assertTrue((expected_dir / "Blocks" / "OB1.awl").is_file())

                op = json.loads(SERVER.s7_open_project(
                    project_path_or_name=str(expected_dir),
                    launch_if_closed=False,
                ))
                self.assertTrue(op["success"], op)
                self.assertEqual(op["project"]["project_name"], "AuditOfflineProj")

                ob = json.loads(SERVER.s7_open_block(block_name="OB1"))
                self.assertTrue(ob["success"], ob)
                self.assertEqual(ob["details"]["mode"], "offline")
                self.assertTrue(Path(ob["details"]["block_path"]).is_file())
        finally:
            SERVER.controller.project_name = orig_name
            SERVER.controller.project_dir = orig_dir
            SERVER.controller.is_offline_project = orig_off

    def test_bug11_standardized_response_keys(self):
        sym_res = json.loads(SERVER.s7_write_symbol_table(symbols={"START_PB": "I 0.0", "MOTOR_Q": "Q 0.0"}))
        self.assertTrue(sym_res["success"])
        self.assertIn("sdf_path", sym_res)
        self.assertIn("asc_path", sym_res)
        self.assertIn("csv_path", sym_res)
        self.assertEqual(sym_res["sdf_path"], sym_res["symbol_sdf_path"])

        sample_awl = "ORGANIZATION_BLOCK OB 1\nBEGIN\nNETWORK\n  A I 0.0;\n  = Q 0.0;\nEND_ORGANIZATION_BLOCK\n"
        def _fake_export(block_name, output_path, project_name=None):
            Path(output_path).write_text(sample_awl, encoding="utf-8")
            return True, "Exported"

        with mock.patch.object(SERVER.controller.com_bridge, "export_block_to_awl", side_effect=_fake_export):
            rd = json.loads(SERVER.s7_com_read_block(block_name="OB1", project_name="PlantA"))
            self.assertTrue(rd["success"])
            self.assertEqual(rd["awl_source"], sample_awl)
            self.assertEqual(rd["raw_awl"], sample_awl)

    def test_bug12_spawn_detached_never_uses_shell_true(self):
        with mock.patch("simatic_s7_mcp_server.subprocess.Popen") as mock_popen:
            ok = SERVER.spawn_detached_siemens_process('"/fake/path/s7tgtopx.exe" "/fake/proj.s7p"')
            self.assertTrue(ok)
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            self.assertIsInstance(args[0], list)
            self.assertFalse(kwargs.get("shell", True))


class TestPlcsimStartStopComTool(unittest.TestCase):
    """Tests for s7_plcsim_start_stop and 32-bit S7ProSim COM simulation start/stop automation."""

    def setUp(self):
        self._orig_mode = SERVER.ACTIVE_ACCESS_MODE
        self._orig_worker = SERVER.runtime_client.com_bridge.worker
        self._orig_audit_path = SERVER.AUDIT_LOGGER._log_path
        SERVER.set_access_mode(AccessMode.READ_WRITE)

    def tearDown(self):
        SERVER.set_access_mode(self._orig_mode)
        SERVER.runtime_client.com_bridge.worker = self._orig_worker
        SERVER.AUDIT_LOGGER._log_path = self._orig_audit_path

    def _make_stateful_prosim_worker(self):
        state = {"connected": False, "cpu_state": "STOP", "scan_mode": "CONTINUOUS", "calls": []}

        def _handler(req):
            state["calls"].append(dict(req))
            cmd = req.get("cmd")
            if cmd == "plcsim_start_simulation":
                prev_state = state["cpu_state"]
                prev_scan = state["scan_mode"]
                if req.get("auto_connect", True) or not state["connected"]:
                    state["connected"] = True
                if req.get("scan_mode"):
                    state["scan_mode"] = str(req["scan_mode"]).upper()
                target = str(req.get("run_mode") or "RUN_P").upper()
                state["cpu_state"] = target
                return {
                    "ok": True,
                    "action": "start",
                    "com_progid": "S7wspsmx.S7ProSim.1",
                    "com_architecture": "32-bit",
                    "connected": state["connected"],
                    "previous_state": prev_state,
                    "target_state": target,
                    "cpu_state": state["cpu_state"],
                    "previous_scan_mode": prev_scan,
                    "scan_mode": state["scan_mode"],
                    "verified": True,
                }
            if cmd == "plcsim_stop_simulation":
                prev_state = state["cpu_state"]
                prev_scan = state["scan_mode"]
                if req.get("auto_connect", True) or not state["connected"]:
                    state["connected"] = True
                state["cpu_state"] = "STOP"
                disc = bool(req.get("disconnect", False))
                if disc:
                    state["connected"] = False
                return {
                    "ok": True,
                    "action": "stop",
                    "com_progid": "S7wspsmx.S7ProSim.1",
                    "com_architecture": "32-bit",
                    "connected": state["connected"],
                    "disconnected": disc,
                    "previous_state": prev_state,
                    "target_state": "STOP",
                    "cpu_state": state["cpu_state"],
                    "previous_scan_mode": prev_scan,
                    "scan_mode": state["scan_mode"],
                    "verified": True,
                }
            return {"ok": False, "error": f"Unexpected cmd: {cmd}"}

        return PersistentComWorker(mock_handler=_handler), state

    def test_plcsim_start_default_run_p_and_stop_verified_with_audit_log(self):
        worker, state = self._make_stateful_prosim_worker()
        SERVER.runtime_client.com_bridge.worker = worker

        with tempfile.TemporaryDirectory() as td:
            audit_file = Path(td) / "plcsim_audit.jsonl"
            SERVER.AUDIT_LOGGER.set_log_path(audit_file)

            # 1. Start simulation with default RUN_P and CONTINUOUS scan mode
            res_start = json.loads(SERVER.s7_plcsim_start_stop(action="start", scan_mode="CONTINUOUS"))
            self.assertTrue(res_start["success"], res_start)
            self.assertEqual(res_start["action"], "start")
            self.assertEqual(res_start["com_progid"], "S7wspsmx.S7ProSim.1")
            self.assertEqual(res_start["com_architecture"], "32-bit")
            self.assertEqual(res_start["transport"], "plcsim_s7prosim_com")
            self.assertEqual(res_start["previous_state"], "STOP")
            self.assertEqual(res_start["target_state"], "RUN_P")
            self.assertEqual(res_start["cpu_state"], "RUN_P")
            self.assertEqual(res_start["scan_mode"], "CONTINUOUS")
            self.assertTrue(res_start["connected"])
            self.assertTrue(res_start["verified"])

            # 2. Stop simulation without disconnecting
            res_stop = json.loads(SERVER.s7_plcsim_start_stop(action="stop"))
            self.assertTrue(res_stop["success"], res_stop)
            self.assertEqual(res_stop["previous_state"], "RUN_P")
            self.assertEqual(res_stop["cpu_state"], "STOP")
            self.assertTrue(res_stop["connected"])
            self.assertFalse(res_stop["disconnected"])
            self.assertTrue(res_stop["verified"])

            # 3. Start in RUN + SINGLE scan mode, then stop with disconnect_on_stop=True
            res_run = json.loads(SERVER.s7_plcsim_start_stop(action="start", run_mode="RUN", scan_mode="SINGLE"))
            self.assertTrue(res_run["success"], res_run)
            self.assertEqual(res_run["cpu_state"], "RUN")
            self.assertEqual(res_run["scan_mode"], "SINGLE")

            res_stop_disc = json.loads(SERVER.s7_plcsim_start_stop(action="stop", disconnect_on_stop=True))
            self.assertTrue(res_stop_disc["success"], res_stop_disc)
            self.assertEqual(res_stop_disc["cpu_state"], "STOP")
            self.assertFalse(res_stop_disc["connected"])
            self.assertTrue(res_stop_disc["disconnected"])

            # Verify 4 structured audit log lines were written
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 4)
            first_entry = json.loads(lines[0])
            self.assertEqual(first_entry["tool"], "s7_plcsim_start_stop")
            self.assertEqual(first_entry["action"], "start")
            self.assertTrue(first_entry["success"])
            self.assertEqual(len(state["calls"]), 4)

    def test_plcsim_start_stop_normalizes_aliases_and_control_routing(self):
        worker, state = self._make_stateful_prosim_worker()
        SERVER.runtime_client.com_bridge.worker = worker

        res = json.loads(SERVER.s7_plcsim_start_stop(action="START", run_mode="RUN-P", scan_mode="SINGLE_SCAN"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["target_state"], "RUN_P")
        self.assertEqual(res["scan_mode"], "SINGLE")
        self.assertEqual(state["calls"][-1]["cmd"], "plcsim_start_simulation")
        self.assertEqual(state["calls"][-1]["run_mode"], "RUN_P")
        self.assertEqual(state["calls"][-1]["scan_mode"], "SINGLE")

        # s7_plcsim_control also supports 'start' and 'stop' aliases
        ctrl_stop = json.loads(SERVER.s7_plcsim_control(action="stop"))
        self.assertTrue(ctrl_stop["success"], ctrl_stop)
        self.assertEqual(ctrl_stop["cpu_state"], "STOP")

        ctrl_start = json.loads(SERVER.s7_plcsim_control(action="start", state="RUN", scan_mode="CONTINUOUS"))
        self.assertTrue(ctrl_start["success"], ctrl_start)
        self.assertEqual(ctrl_start["cpu_state"], "RUN")
        self.assertEqual(ctrl_start["scan_mode"], "CONTINUOUS")

    def test_plcsim_start_stop_validation_errors(self):
        worker, _ = self._make_stateful_prosim_worker()
        SERVER.runtime_client.com_bridge.worker = worker

        bad_act = json.loads(SERVER.s7_plcsim_start_stop(action="pause"))
        self.assertFalse(bad_act["success"])
        self.assertEqual(bad_act["failure_category"], FailureCategory.VALIDATION_ERROR.value)

        bad_run = json.loads(SERVER.s7_plcsim_start_stop(action="start", run_mode="FREEZE"))
        self.assertFalse(bad_run["success"])
        self.assertEqual(bad_run["failure_category"], FailureCategory.VALIDATION_ERROR.value)

        bad_scan = json.loads(SERVER.s7_plcsim_start_stop(action="start", scan_mode="TURBO"))
        self.assertFalse(bad_scan["success"])
        self.assertEqual(bad_scan["failure_category"], FailureCategory.VALIDATION_ERROR.value)

    def test_plcsim_start_stop_blocked_in_read_only_mode(self):
        SERVER.set_access_mode(AccessMode.READ_ONLY)
        for act in ("start", "stop"):
            res = json.loads(SERVER.s7_plcsim_start_stop(action=act))
            self.assertFalse(res["success"])
            self.assertEqual(res["failure_category"], FailureCategory.ACCESS_DENIED.value)

    def test_plcsim_start_stop_error_categories_for_com_plcsim_and_timeout(self):
        # 1. 32-bit COM host unavailable
        SERVER.runtime_client.com_bridge.worker = PersistentComWorker(
            mock_handler=lambda req: {"ok": False, "error": "32-bit Windows COM host is not available on this platform."}
        )
        r_unavail = json.loads(SERVER.s7_plcsim_start_stop(action="start"))
        self.assertFalse(r_unavail["success"])
        self.assertEqual(r_unavail["failure_category"], FailureCategory.COM_UNAVAILABLE.value)
        self.assertIn("hint", r_unavail)

        # 2. S7ProSim COM not registered / PLCSIM closed
        SERVER.runtime_client.com_bridge.worker = PersistentComWorker(
            mock_handler=lambda req: {"ok": False, "error": "S7ProSim COM (S7wspsmx.S7ProSim.1) not registered"}
        )
        r_plcsim = json.loads(SERVER.s7_plcsim_start_stop(action="start"))
        self.assertFalse(r_plcsim["success"])
        self.assertEqual(r_plcsim["failure_category"], FailureCategory.PLCSIM_UNAVAILABLE.value)
        self.assertIn("S7USSAPX.EXE", r_plcsim["hint"])

        # 3. COM timeout
        SERVER.runtime_client.com_bridge.worker = PersistentComWorker(
            mock_handler=lambda req: {"ok": False, "timed_out": True, "error": "32-bit COM command 'plcsim_stop_simulation' timed out after 5.0s."}
        )
        r_to = json.loads(SERVER.s7_plcsim_start_stop(action="stop", timeout_s=5.0))
        self.assertFalse(r_to["success"])
        self.assertEqual(r_to["failure_category"], FailureCategory.COM_TIMEOUT.value)

    def test_32bit_com_backends_implement_plcsim_start_stop_commands(self):
        from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT

        for snippet in (
            "S7wspsmx.S7ProSim.1",
            "if ($cmd -eq 'plcsim_start_simulation')",
            "if ($cmd -eq 'plcsim_stop_simulation')",
            "$null = $psim.Connect()",
            "$null = $psim.SetScanMode(",
            "$null = $psim.SetState(",
            "$null = $psim.Disconnect()",
            "$psim.GetState()",
        ):
            self.assertIn(snippet, _PERSISTENT_PS32_DAEMON_SCRIPT)

        cpp_path = Path(__file__).resolve().parents[1] / "native" / "s7_com_bridge.cpp"
        cpp_text = cpp_path.read_text(encoding="utf-8")
        for snippet in (
            'L"S7wspsmx.S7ProSim.1"',
            'cmd == "plcsim_start_simulation"',
            'cmd == "plcsim_stop_simulation"',
            "ensure_prosim_connected",
            'L"SetScanMode"',
            'L"SetState"',
            'L"Disconnect"',
        ):
            self.assertIn(snippet, cpp_text)

    def test_s7_plcsim_start_stop_mcp_registration_and_annotations(self):
        import inspect

        sig = inspect.signature(SERVER.s7_plcsim_start_stop)
        for pname, param in sig.parameters.items():
            self.assertFalse(
                isinstance(param.annotation, str),
                f"s7_plcsim_start_stop parameter '{pname}' has unevaluated string annotation",
            )
        self.assertFalse(isinstance(sig.return_annotation, str))
        self.assertTrue(callable(SERVER.s7_plcsim_start_stop))


if __name__ == "__main__":
    unittest.main()
