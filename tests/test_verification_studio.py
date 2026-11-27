"""Comprehensive unit tests for Siemens SIMATIC Verify Studio backend & MCP tools."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from simatic_s7_mcp.verification_studio import (
    SIEMENS_PRESETS,
    SiemensVerificationSession,
    analyze_static_safety_and_xref,
)
import simatic_s7_mcp_server as srv


class TestSiemensVerificationStudio(unittest.TestCase):
    def test_all_three_presets_load_and_pass_full_fat_suites(self):
        self.assertEqual(
            set(SIEMENS_PRESETS.keys()),
            {"conveyor_motor", "tank_pid", "batch_mixer_sfc"},
        )
        for preset_id in ("conveyor_motor", "tank_pid", "batch_mixer_sfc"):
            with self.subTest(preset=preset_id):
                session = SiemensVerificationSession(preset_id)
                fat = session.run_fat_suite()
                self.assertTrue(fat["ok"])
                self.assertTrue(fat["executed"])
                self.assertGreaterEqual(fat["total_count"], 4)
                failed = [c for c in fat["cases"] if not c["passed"]]
                self.assertEqual(
                    failed,
                    [],
                    f"Preset {preset_id} had failing FAT cases: {json.dumps(failed, indent=2)}",
                )
                self.assertTrue(fat["passed"])

    def test_force_table_overrides_logic_and_lights_frce_led(self):
        session = SiemensVerificationSession("conveyor_motor")
        res = session.set_force("Q0.0", True, "BOOL")
        self.assertTrue(res["ok"])
        snap1 = session.get_state_snapshot()
        self.assertTrue(snap1["cpu"]["leds"]["FRCE"])

        # Step CPU; forced value must persist across OB1 execution
        stepped = session.step(cycles=3, force_step=True)
        self.assertTrue(stepped["ok"])
        snap2 = session.get_state_snapshot()
        self.assertTrue(snap2["cpu"]["leds"]["FRCE"])
        motor_rows = snap2["vat_tables"]["Motors & Actuators"]
        q0_row = next(r for r in motor_rows if r["address"] == "Q0.0")
        self.assertTrue(q0_row["forced"])
        self.assertIs(q0_row["value"], True)

        # Unforce All clears FRCE LED
        session.clear_all_forces()
        snap3 = session.get_state_snapshot()
        self.assertFalse(snap3["cpu"]["leds"]["FRCE"])

    def test_hardware_fault_injection_and_diagnostic_buffer(self):
        session = SiemensVerificationSession("conveyor_motor")
        # Inject PROFINET I/O drop (OB86 -> BF LED)
        r1 = session.set_fault("profinet_io_drop", True)
        self.assertTrue(r1["ok"])
        snap1 = session.get_state_snapshot()
        self.assertTrue(snap1["cpu"]["leds"]["BF"])
        self.assertTrue(any(e["ob_number"] == 86 for e in snap1["diagnostic_buffer"]))

        # Inject scan load spike (OB80 -> SF LED + watchdog headroom drop)
        r2 = session.set_fault("scan_load_spike", True)
        self.assertTrue(r2["ok"])
        session.step(cycles=2, force_step=True)
        snap2 = session.get_state_snapshot()
        self.assertTrue(snap2["cpu"]["leds"]["SF"])
        self.assertGreater(snap2["cpu"]["stats"]["max_scan_ms"], 100.0)

        # 24V brownout -> OB100 warm restart
        r3 = session.trigger_brownout_warm_restart()
        self.assertTrue(r3["ok"])
        snap3 = session.get_state_snapshot()
        self.assertTrue(any(e["ob_number"] == 100 for e in snap3["diagnostic_buffer"]))

    def test_conditional_breakpoint_halts_execution(self):
        session = SiemensVerificationSession("conveyor_motor")
        bp = session.manage_breakpoints("add_condition", address="Q0.0", operator="==", value=True)
        self.assertTrue(bp["ok"])
        # Latch safety relay M0.0 via I0.2, then press Start PB I0.3 -> Q0.0 turns True
        session.write_tag("I0.2", True, "BOOL")
        session.step(cycles=1, force_step=True)
        session.write_tag("I0.2", False, "BOOL")
        session.write_tag("I0.3", True, "BOOL")
        session.step(cycles=5, force_step=True)
        snap = session.get_state_snapshot()
        self.assertEqual(snap["cpu"]["mode"], "PAUSED_BREAKPOINT")
        self.assertIsNotNone(snap["breakpoints"]["last_hit"])

    def test_one_scan_trace_and_csv_export(self):
        session = SiemensVerificationSession("tank_pid")
        session.step(cycles=15, force_step=True)
        exported = session.manage_trace("export_csv")
        self.assertTrue(exported["ok"])
        self.assertGreaterEqual(exported["row_count"], 15)
        self.assertIn("scan,time_ms", exported["csv"])
        self.assertIn("MD10", exported["csv"])

    def test_custom_ai_program_load_and_static_duplicate_coil_detection(self):
        bad_code = """
ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = First coil write
      A I0.0;
      = Q0.0;
NETWORK
TITLE = Duplicate coil write
      A I0.1;
      = Q0.0;
END_ORGANIZATION_BLOCK
"""
        blocks = [{"name": "OB1", "language": "AWL", "source": bad_code}]
        audit, xrefs = analyze_static_safety_and_xref(blocks, {}, "S7-300")
        self.assertFalse(audit["passed"])
        self.assertTrue(
            any(f["id"] == "DUP_COIL_Q0.0" and f["address"] == "Q0.0" for f in audit["findings"])
        )
        self.assertEqual(xrefs["Q0.0"]["write_count"], 2)

        session = SiemensVerificationSession("conveyor_motor")
        loaded = session.load_custom_program(
            name="AI Test",
            source_code=bad_code,
            symbols_text="Start,I0.0,BOOL",
            cpu_family="S7-300",
        )
        self.assertTrue(loaded["ok"])
        snap = session.get_state_snapshot()
        self.assertEqual(snap["project"]["name"], "AI Test")
        self.assertGreaterEqual(snap["static_audit"]["error_count"], 1)

    def test_fat_certificate_markdown_and_json_export(self):
        session = SiemensVerificationSession("conveyor_motor")
        session.run_fat_suite()
        md = session.export_fat_report("markdown")
        self.assertTrue(md["ok"])
        self.assertIn("Factory Acceptance Test (FAT) Sign-Off Certificate", md["content"])
        self.assertIn("PASS", md["content"])

        js = session.export_fat_report("json")
        self.assertTrue(js["ok"])
        parsed = json.loads(js["content"])
        self.assertTrue(parsed["fat_results"]["passed"])

    def test_daemon_stdio_jsonrpc_roundtrip(self):
        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "simatic_s7_mcp.verification_studio", "--daemon"],
            cwd=str(repo_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            ready_line = proc.stdout.readline()
            ready = json.loads(ready_line)
            self.assertTrue(ready["ok"])
            self.assertTrue(ready["ready"])

            proc.stdin.write(json.dumps({"cmd": "get_state"}) + "\n")
            proc.stdin.flush()
            resp_line = proc.stdout.readline()
            resp = json.loads(resp_line)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["project"]["id"], "conveyor_motor")
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.terminate()
            proc.wait(timeout=5)

    def test_mcp_verification_tools(self):
        load_res = json.loads(srv.s7_sim_load_verification_preset("conveyor_motor"))
        self.assertTrue(load_res["success"])
        self.assertEqual(load_res["project"]["id"], "conveyor_motor")

        fat_res = json.loads(srv.s7_sim_run_fat_suite("conveyor_motor"))
        self.assertTrue(fat_res["success"])
        self.assertTrue(fat_res["passed"])

        fault_res = json.loads(srv.s7_sim_inject_fault("profinet_io_drop", True))
        self.assertTrue(fault_res["success"])
        self.assertTrue(fault_res["leds"]["BF"])


if __name__ == "__main__":
    unittest.main()
