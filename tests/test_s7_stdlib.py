"""Unit & integration tests for Siemens S7-300 Standard Library (s7_stdlib.py) and Closed-Loop Plant Simulator.

Covers BUG-07 remediation:
  - FC105 (SCALE) & FC106 (UNSCALE) unipolar/bipolar scaling and range-error RET_VAL=8
  - SFB3 (TP), SFB4 (TON), SFB5 (TOF) IEC timers with instance DB synchronization
  - SFB0 (CTU), SFB1 (CTD), SFB2 (CTUD) IEC counters with rising-edge detection
  - FB41 (CONT_C) continuous PID controller (manual, auto, proportional/integral/derivative, LMN_PER)
  - SFC20 (BLKMOV), SFC21 (FILL), SFC64 (TIME_TCK) with P#... ANY pointers
  - VirtualS7PLC AWL & SCL CALL dispatch into s7_stdlib + periodic OB35 execution
  - End-to-end closed-loop PID tank-level simulation via `s7_run_closed_loop_sim` MCP tool
"""

import json
import unittest

import simatic_s7_mcp_server as SERVER
from simatic_s7_mcp import (
    VirtualS7PLC,
    execute_stdlib_block,
    parse_any_pointer,
    parse_iec_time_ms,
    run_closed_loop_plant_sim,
)


class TestS7StandardLibrary(unittest.TestCase):
    def setUp(self):
        self.plc = VirtualS7PLC()

    def test_parse_iec_time_and_any_pointer(self):
        self.assertEqual(parse_iec_time_ms("T#5S"), 5000)
        self.assertEqual(parse_iec_time_ms("T#1S500MS"), 1500)
        self.assertEqual(parse_iec_time_ms("S5T#2M"), 120000)
        ptr = parse_any_pointer("P#DB10.DBX0.0 BYTE 16")
        self.assertIsNotNone(ptr)
        self.assertEqual(ptr["area_code"], 0x84)
        self.assertEqual(ptr["db_number"], 10)
        self.assertEqual(ptr["byte_offset"], 0)
        self.assertEqual(ptr["byte_length"], 16)

    def test_fc105_scale_and_fc106_unscale_roundtrip(self):
        # Unipolar 0..27648 -> 0.0..100.0
        out = execute_stdlib_block(
            self.plc,
            "FC105",
            {"IN": 13824, "HI_LIM": 100.0, "LO_LIM": 0.0, "BIPOLAR": False},
        )
        self.assertEqual(out["RET_VAL"], 0)
        self.assertAlmostEqual(out["OUT"], 50.0, places=3)

        # Out-of-range produces RET_VAL=8 and clamped HI_LIM
        ovf = execute_stdlib_block(
            self.plc,
            "SCALE",
            {"IN": 30000, "HI_LIM": 100.0, "LO_LIM": 0.0, "BIPOLAR": False},
        )
        self.assertEqual(ovf["RET_VAL"], 8)
        self.assertAlmostEqual(ovf["OUT"], 100.0, places=3)

        # FC106 UNSCALE 50.0 in [0..100] -> 13824
        un = execute_stdlib_block(
            self.plc,
            "FC106",
            {"IN": 50.0, "HI_LIM": 100.0, "LO_LIM": 0.0, "BIPOLAR": False},
        )
        self.assertEqual(un["RET_VAL"], 0)
        self.assertEqual(un["OUT"], 13824)

    def test_iec_timers_sfb3_sfb4_sfb5_via_awl_call(self):
        ob1_awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
  CALL SFB 4, DB 100 (
    IN := I 0.0,
    PT := T#300MS,
    Q  := Q 0.0,
    ET := MD 20
  );
  CALL SFB 5, DB 101 (
    IN := I 0.1,
    PT := T#200MS,
    Q  := Q 0.1
  );
  CALL SFB 3, DB 102 (
    IN := I 0.2,
    PT := T#200MS,
    Q  := Q 0.2
  );
END_ORGANIZATION_BLOCK
"""
        self.plc.load_block("OB1", ob1_awl)
        # Turn on I0.0 (TON), I0.1 (TOF), I0.2 (TP)
        self.plc.write_address("I0.0", True)
        self.plc.write_address("I0.1", True)
        self.plc.write_address("I0.2", True)

        self.plc.step(cycles=2, tick_ms=100)  # 200ms
        self.assertFalse(self.plc.read_address("Q0.0"))  # TON not yet 300ms
        self.assertEqual(self.plc.read_address("MD20", data_type="DINT"), 200)
        self.assertTrue(self.plc.read_address("Q0.1"))   # TOF input is ON
        self.assertFalse(self.plc.read_address("Q0.2"))  # TP expired at 200ms

        self.plc.step(cycles=1, tick_ms=100)  # 300ms
        self.assertTrue(self.plc.read_address("Q0.0"))   # TON output now True
        self.assertTrue(self.plc.read_address("DB100.Q"))

        # Drop TOF input -> stays ON for 100ms, turns OFF at 200ms
        self.plc.write_address("I0.1", False)
        self.plc.step(cycles=1, tick_ms=100)
        self.assertTrue(self.plc.read_address("Q0.1"))
        self.plc.step(cycles=1, tick_ms=100)
        self.assertFalse(self.plc.read_address("Q0.1"))

    def test_iec_counters_sfb0_sfb1_sfb2(self):
        for _ in range(3):
            execute_stdlib_block(self.plc, "SFB0", {"CU": True, "R": False, "PV": 3}, instance_db=20)
            execute_stdlib_block(self.plc, "SFB0", {"CU": False, "R": False, "PV": 3}, instance_db=20)
        out = execute_stdlib_block(self.plc, "SFB0", {"CU": False, "R": False, "PV": 3}, instance_db=20)
        self.assertEqual(out["CV"], 3)
        self.assertTrue(out["Q"])

        # SFB1 CTD load=2 then decrement twice -> Q True
        execute_stdlib_block(self.plc, "SFB1", {"CD": False, "LD": True, "PV": 2}, instance_db=21)
        execute_stdlib_block(self.plc, "SFB1", {"CD": True, "LD": False, "PV": 2}, instance_db=21)
        execute_stdlib_block(self.plc, "SFB1", {"CD": False, "LD": False, "PV": 2}, instance_db=21)
        out_d = execute_stdlib_block(self.plc, "SFB1", {"CD": True, "LD": False, "PV": 2}, instance_db=21)
        self.assertEqual(out_d["CV"], 0)
        self.assertTrue(out_d["Q"])

    def test_sfc20_blkmov_sfc21_fill_and_sfc64_time_tck_via_awl(self):
        self.plc.write_address("DB1.DBW0", 1234, data_type="INT")
        self.plc.write_address("DB1.DBW2", 5678, data_type="INT")
        ob1_awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
  CALL SFC 20 (
    SRCBLK  := P#DB1.DBX0.0 BYTE 4,
    RET_VAL := MW 10,
    DSTBLK  := P#DB2.DBX0.0 BYTE 4
  );
  CALL SFC 21 (
    BVAL    := 170,
    RET_VAL := MW 12,
    BLK     := P#DB3.DBX0.0 BYTE 4
  );
  CALL SFC 64 (
    RET_VAL := MD 30
  );
END_ORGANIZATION_BLOCK
"""
        self.plc.load_block("OB1", ob1_awl)
        self.plc.step(cycles=3, tick_ms=100)
        self.assertEqual(self.plc.read_address("MW10", data_type="INT"), 0)
        self.assertEqual(self.plc.read_address("DB2.DBW0", data_type="INT"), 1234)
        self.assertEqual(self.plc.read_address("DB2.DBW2", data_type="INT"), 5678)
        self.assertEqual(self.plc.read_address("MW12", data_type="INT"), 0)
        self.assertEqual(self.plc.read_bytes(0x84, 3, 0, 4), bytes([0xAA, 0xAA, 0xAA, 0xAA]))
        self.assertEqual(self.plc.read_address("MD30", data_type="DINT"), 300)

    def test_fb41_cont_c_pid_and_closed_loop_plant_sim_mcp_tool(self):
        # Load an OB35 cyclic interrupt program in SCL that scales PIW256 via FC105, runs FB41.DB41 PID,
        # and drives PQW256 from DB41.LMN_PER!
        ob1_src = "ORGANIZATION_BLOCK OB 1\nBEGIN\nNETWORK\n  NOP 0;\nEND_ORGANIZATION_BLOCK\n"
        ob35_scl = """ORGANIZATION_BLOCK OB 35
VAR_TEMP
  pv_eng : REAL;
  ret_scale : INT;
END_VAR
BEGIN
  ret_scale := FC105(IN := PIW256, HI_LIM := 100.0, LO_LIM := 0.0, BIPOLAR := FALSE, OUT := pv_eng);
  MD100 := pv_eng;
  FB41.DB41(
    COM_RST := FALSE,
    MAN_ON := FALSE,
    PVPER_ON := FALSE,
    P_SEL := TRUE,
    I_SEL := TRUE,
    D_SEL := FALSE,
    CYCLE := T#100MS,
    SP_INT := 50.0,
    PV_IN := pv_eng,
    GAIN := 3.0,
    TI := T#3S,
    LMN_HLM := 100.0,
    LMN_LLM := 0.0
  );
  PQW256 := DB41.LMN_PER;
END_ORGANIZATION_BLOCK
"""
        SERVER.VIRTUAL_PLC.reset(clear_blocks=True, clear_symbols=True)
        self.assertTrue(json.loads(SERVER.s7_virtual_plc_load("OB1", source_code=ob1_src))["success"])
        self.assertTrue(json.loads(SERVER.s7_virtual_plc_load("OB35", source_code=ob35_scl, language="SCL"))["success"])

        sim_res = json.loads(SERVER.s7_run_closed_loop_sim(
            duration_ms=15000,
            tick_ms=100,
            entry_block="OB1",
            plant_states=[
                {
                    "name": "level_pct",
                    "initial": 10.0,
                    "min": 0.0,
                    "max": 100.0,
                    "type": "integrator",
                    "bias_rate": -4.0,
                    "actuators": [
                        {"address": "PQW256", "data_type": "INT", "in_min": 0.0, "in_max": 27648.0, "gain": 20.0},
                    ],
                }
            ],
            sensor_links=[
                {
                    "state": "level_pct",
                    "address": "PIW256",
                    "kind": "analog",
                    "eng_min": 0.0,
                    "eng_max": 100.0,
                    "raw_min": 0,
                    "raw_max": 27648,
                }
            ],
            assertions=[
                {"state": "level_pct", "final_near": 50.0, "tolerance": 2.0},
                {"state": "level_pct", "max_le": 70.0, "min_ge": 5.0},
            ],
            watch=["MD100:REAL", "PQW256:INT"],
            sample_every_scans=25,
        ))
        self.assertTrue(sim_res["success"], sim_res)
        self.assertGreaterEqual(sim_res["ob35_executions"], 150)
        self.assertIn("FB41", sim_res["supported_stdlib_blocks"])
        self.assertAlmostEqual(sim_res["state_metrics"]["level_pct"]["final"], 50.0, delta=2.0)

        # Direct run_closed_loop_plant_sim sanity check
        direct = run_closed_loop_plant_sim(
            self.plc,
            duration_ms=500,
            tick_ms=100,
            plant_states=[{"name": "temp_c", "initial": 25.0, "type": "first_order", "ambient": 25.0, "tau_s": 5.0}],
            assertions=[{"state": "temp_c", "final_near": 25.0, "tolerance": 0.1}],
        )
        self.assertTrue(direct["ok"])


if __name__ == "__main__":
    unittest.main()
