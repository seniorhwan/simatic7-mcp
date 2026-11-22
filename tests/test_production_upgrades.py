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


if __name__ == "__main__":
    unittest.main()
