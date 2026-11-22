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

import simatic_s7_mcp_server as SERVER
from simatic_s7_mcp import (
    AccessMode,
    AuditLogger,
    EnhancedS7LoopbackServer,
    FailureCategory,
    PersistentComWorker,
    S7ConnectionPool,
    StationHardwareConfig,
    VirtualS7PLC,
    WatchTableMonitor,
    compute_content_hash,
    generate_client_config,
    parse_station_cfg,
    run_doctor,
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


if __name__ == "__main__":
    unittest.main()
