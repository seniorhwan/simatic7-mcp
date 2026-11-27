"""Production-grade Siemens SIMATIC STEP 7 Classic Verification & Simulation Studio Engine.

Wraps ``VirtualS7PLC`` with everything an automation engineer needs to verify AI-generated
S7-300/S7-400 PLC programs prior to hardware commissioning:
  - 3 authentic multi-block Siemens STEP 7 projects (Conveyor/Motor Safety, Closed-Loop FB41 PID
    Tank Process, Sequential Batch Mixer S7-GRAPH SFC) + custom AI program loader
  - Live LAD/STL network power-flow evaluator & network execution coverage heatmap
  - SIMATIC Variable Table (VAT) & persistent Force Values overlay (FRCE LED)
  - Hardware & OB Fault Injection (E-Stop, PIW wire-break/overrange OB82, Contactor jam,
    PROFINET/PROFIBUS station drop OB86, 24V brownout warm restart OB100, OB80 cycle spike)
  - Authentic S7 CPU LED strip (SF, BF, DC5V, FRCE, RUN, STOP) & timestamped Diagnostic Buffer
  - Conditional breakpoints & 1-scan high-speed Trace Oscilloscope with PID step-response metrics
  - Coupled 2D closed-loop Plant Twin physics models
  - Static safety auditor & I/Q/M/PIW/PQW/DB/T/C cross-reference analyzer
  - Isolated-clone Automated FAT Suite runner & checksummed FAT Sign-Off Certificate exporter
  - Persistent newline-delimited JSON-RPC stdio daemon (--daemon) for the Node.js web workbench
"""

from __future__ import annotations

import copy
import csv
import datetime
import io
import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contracts import compute_content_hash
from .runtime_engine import VirtualS7PLC, parse_s7_address


# ---------------------------------------------------------------------------
# Authentic Siemens STEP 7 Classic Full-Program Presets
# ---------------------------------------------------------------------------

CONVEYOR_OB1_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = Main Conveyor & Safety Interlock Cycle
VERSION : 0.1
BEGIN
NETWORK
TITLE = Safety Relay & E-Stop Manual Reset Latch
// E-Stop NC (I0.0) & Guard Closed (I0.1) must be healthy.
// Restoring E-Stop never auto-restarts: operator must press Reset PB (I0.2).
  A   I 0.0;
  A   I 0.1;
  A   I 0.2;
  S   M 0.0;
  AN  I 0.0;
  O(
  AN  I 0.1;
  )
  R   M 0.0;

NETWORK
TITLE = Conveyor Motor Start/Stop Seal-In & Permissives
  A(
  O   I 0.3;
  O   M 0.1;
  )
  A   I 0.4;
  A   M 0.0;
  AN  M 0.3;
  =   M 0.1;
  =   Q 0.0;
  =   Q 0.1;

NETWORK
TITLE = Contactor Auxiliary Feedback Supervision (2.0s Trip)
// If main contactor Q0.0 is energized without feedback I0.5 for 2s -> Fault Latch M0.3
  A   Q 0.0;
  AN  I 0.5;
  L   S5T#2S;
  SD  T 1;
  A   T 1;
  S   M 0.3;
  S   Q 0.2;

NETWORK
TITLE = Fault Alarm Reset (Requires Safe Chain)
  A   I 0.2;
  A   M 0.0;
  R   M 0.3;
  R   Q 0.2;
  R   M 0.4;

NETWORK
TITLE = Part Photoeye Counter & Chute Jam Detection (3.0s)
  A   I 0.6;
  CU  C 1;
  A   I 0.6;
  A   Q 0.0;
  L   S5T#3S;
  SD  T 2;
  A   T 2;
  S   M 0.4;

NETWORK
TITLE = Safety Ready Status Indicator
  A   M 0.0;
  AN  M 0.3;
  =   Q 0.3;

END_ORGANIZATION_BLOCK
"""

CONVEYOR_DB1_AWL = """DATA_BLOCK DB 1
TITLE = Retentive Production Counters & Conveyor Config
STRUCT
  BatchTarget : INT := 100;
  TotalPartsRetain : INT := 0;
  MaxSpeedRpm : REAL := 1450.0;
  MaintenanceDue : BOOL := FALSE;
END_STRUCT;
BEGIN
  BatchTarget := 100;
  TotalPartsRetain := 0;
  MaxSpeedRpm := 1450.0;
  MaintenanceDue := FALSE;
END_DATA_BLOCK
"""

TANK_PID_DB41_AWL = """DATA_BLOCK DB 41
TITLE = FB41 CONT_C Level PID Instance Parameters
STRUCT
  SP_INT : REAL := 60.0;
  PV_IN : REAL := 20.0;
  GAIN : REAL := 2.4;
  LMN : REAL := 0.0;
  ER : REAL := 0.0;
  MAN_ON : BOOL := FALSE;
END_STRUCT;
BEGIN
  SP_INT := 60.0;
  PV_IN := 20.0;
  GAIN := 2.4;
  LMN := 0.0;
  ER := 0.0;
  MAN_ON := FALSE;
END_DATA_BLOCK
"""

TANK_PID_OB1_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = Tank Level Process Supervision & Interlocks
VERSION : 0.1
BEGIN
NETWORK
TITLE = Safety & Auto Enable Chain
  A   I 0.0;
  A   I 0.1;
  AN  M 1.2;
  =   M 0.0;
  =   Q 0.0;

NETWORK
TITLE = Analog Sensor Wire-Break (< 0) & Overrange (> 27648) Supervision
// Siemens 4-20mA channel reports -32768 (0x8000) on wire-break and +32767 (0x7FFF) on overflow
  L   PIW 256;
  L   0;
  <I;
  =   M 1.0;
  L   PIW 256;
  L   27648;
  >I;
  =   M 1.1;
  A   M 1.0;
  O   M 1.1;
  =   M 1.2;
  =   Q 0.2;

NETWORK
TITLE = High-High (> 90%) & Low-Low (< 10%) Level Alarms
  L   MD 10;
  L   90.0;
  >R;
  =   M 1.3;
  =   Q 0.1;
  L   MD 10;
  L   10.0;
  <R;
  =   M 1.4;

NETWORK
TITLE = Safe Valve Clamp on Sensor Fault or E-Stop
  A   M 1.2;
  O(
  AN  M 0.0;
  )
  JCN OKVL;
  L   0.0;
  T   MD 18;
  L   0;
  T   PQW 256;
OKVL: NOP 0;

END_ORGANIZATION_BLOCK
"""

TANK_PID_OB35_AWL = """ORGANIZATION_BLOCK OB 35
TITLE = 100ms Cyclic Interrupt — FC105 SCALE + FB41 CONT_C PID + FC106 UNSCALE
VERSION : 0.1
BEGIN
NETWORK
TITLE = Scale Raw PIW256 (0..27648) to Level PV (0.0..100.0 %) via FC105
  CALL FC 105 (
    IN := PIW 256,
    HI_LIM := 100.0,
    LO_LIM := 0.0,
    BIPOLAR := FALSE,
    RET_VAL := MW 30,
    OUT := MD 10
  );

NETWORK
TITLE = Execute FB41 CONT_C Closed-Loop Level PID Controller
  CALL FB 41, DB 41 (
    MAN_ON := M 1.2,
    PVPER_ON := FALSE,
    P_SEL := TRUE,
    I_SEL := TRUE,
    D_SEL := FALSE,
    CYCLE := T#100ms,
    SP_INT := MD 14,
    PV_IN := MD 10,
    MAN := 0.0,
    GAIN := 2.4,
    TI := T#6s,
    LMN_HLM := 100.0,
    LMN_LLM := 0.0,
    LMN := MD 18,
    ER := MD 22
  );

NETWORK
TITLE = Unscale PID Output MD18 (0..100 %) to Valve Analog PQW256 via FC106
  A   M 0.0;
  AN  M 1.2;
  JCN ZVAL;
  CALL FC 106 (
    IN := MD 18,
    HI_LIM := 100.0,
    LO_LIM := 0.0,
    BIPOLAR := FALSE,
    RET_VAL := MW 32,
    OUT := PQW 256
  );
  JU  ENDP;
ZVAL: L 0;
  T   PQW 256;
ENDP: NOP 0;

END_ORGANIZATION_BLOCK
"""

BATCH_MIXER_OB1_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = Sequential Batch Reactor Safety Wrapper & S7-GRAPH Dispatch
VERSION : 0.1
BEGIN
NETWORK
TITLE = Reactor Safety Chain & Manual Reset Latch
  A   I 0.0;
  A   I 0.5;
  S   M 0.0;
  AN  I 0.0;
  R   M 0.0;

NETWORK
TITLE = Execute S7-GRAPH Sequential Batch State Machine (FB20)
  A   M 0.0;
  JCN ABOR;
  CALL FB 20, DB 20;
  JU  DONE;

NETWORK
TITLE = Emergency Abort — De-Energize All Reactor Valves & Heater
ABOR: SET;
  R   Q 0.0;
  R   Q 0.1;
  R   Q 0.2;
  R   Q 0.3;
  R   Q 0.4;
  S   M 0.7;
DONE: NOP 0;

NETWORK
TITLE = Clear Abort Flag on Manual Reset
  A   M 0.0;
  A   I 0.5;
  R   M 0.7;

END_ORGANIZATION_BLOCK
"""

BATCH_MIXER_FB20_GR7 = """FUNCTION_BLOCK FB 20
TITLE = S7-GRAPH Batch Reactor Sequence (Idle -> Fill -> Heat/Mix -> Drain -> Done)
INITIAL_STEP S0_IDLE :
  R Q 0.0;
  R Q 0.1;
  R Q 0.2;
  R Q 0.3;
  R Q 0.4;
END_STEP

STEP S1_FILL :
  N Q 0.0;
END_STEP

STEP S2_HEAT_MIX :
  N Q 0.1;
  N Q 0.2;
END_STEP

STEP S3_DRAIN :
  N Q 0.3;
END_STEP

STEP S4_COMPLETE :
  N Q 0.4;
END_STEP

TRANSITION T1_START FROM S0_IDLE TO S1_FILL :=
  I0.0 AND I0.1 AND M0.0;
END_TRANSITION

TRANSITION T2_FULL FROM S1_FILL TO S2_HEAT_MIX :=
  I0.2;
END_TRANSITION

TRANSITION T3_TEMP_OK FROM S2_HEAT_MIX TO S3_DRAIN :=
  I0.3;
END_TRANSITION

TRANSITION T4_EMPTY FROM S3_DRAIN TO S4_COMPLETE :=
  I0.4;
END_TRANSITION

TRANSITION T5_RESET FROM S4_COMPLETE TO S0_IDLE :=
  I0.5;
END_TRANSITION

END_FUNCTION_BLOCK
"""

BATCH_MIXER_DB10_AWL = """DATA_BLOCK DB 10
TITLE = Batch Recipe & Retentive Batch Counter
STRUCT
  TargetTempC : REAL := 80.0;
  TargetLevelPct : REAL := 90.0;
  CompletedBatches : INT := 0;
END_STRUCT;
BEGIN
  TargetTempC := 80.0;
  TargetLevelPct := 90.0;
  CompletedBatches := 0;
END_DATA_BLOCK
"""


SIEMENS_PRESETS: Dict[str, Dict[str, Any]] = {
    "conveyor_motor": {
        "id": "conveyor_motor",
        "name": "Conveyor & Motor Safety Starter",
        "cpu_family": "S7-300",
        "order_number": "6ES7 315-2EH14-0AB0",
        "summary": "Discrete safety relay latch (no auto-restart), motor seal-in, 2s contactor feedback supervision (T1), part counter (C1), and chute jam detection (T2).",
        "plant_type": "conveyor_motor",
        "scan_interval_ms": 100,
        "watchdog_limit_ms": 150.0,
        "blocks": [
            {"name": "DB1", "language": "DB", "source": CONVEYOR_DB1_AWL},
            {"name": "OB1", "language": "AWL", "source": CONVEYOR_OB1_AWL},
        ],
        "symbols": {
            "ESTOP_OK": ("I0.0", "BOOL"),
            "GUARD_CLOSED": ("I0.1", "BOOL"),
            "RESET_PB": ("I0.2", "BOOL"),
            "START_PB": ("I0.3", "BOOL"),
            "STOP_PB_NC": ("I0.4", "BOOL"),
            "CONTACTOR_FB": ("I0.5", "BOOL"),
            "PART_PHOTOEYE": ("I0.6", "BOOL"),
            "MOTOR_CONTACTOR": ("Q0.0", "BOOL"),
            "RUN_LAMP": ("Q0.1", "BOOL"),
            "FAULT_BEACON": ("Q0.2", "BOOL"),
            "SAFETY_READY_LAMP": ("Q0.3", "BOOL"),
            "SAFETY_OK": ("M0.0", "BOOL"),
            "MOTOR_RUN_CMD": ("M0.1", "BOOL"),
            "FEEDBACK_FAULT": ("M0.3", "BOOL"),
            "CHUTE_JAM_ALM": ("M0.4", "BOOL"),
        },
        "initial_writes": [
            {"address": "I0.0", "value": True, "data_type": "BOOL"},
            {"address": "I0.1", "value": True, "data_type": "BOOL"},
            {"address": "I0.4", "value": True, "data_type": "BOOL"},
        ],
        "vat_tables": {
            "Safety & E-Stop": [
                {"address": "I0.0", "symbol": "ESTOP_OK", "data_type": "BOOL", "format": "BOOL", "comment": "E-Stop chain healthy (NC)"},
                {"address": "I0.1", "symbol": "GUARD_CLOSED", "data_type": "BOOL", "format": "BOOL", "comment": "Safety gate interlock"},
                {"address": "I0.2", "symbol": "RESET_PB", "data_type": "BOOL", "format": "BOOL", "comment": "Manual safety & fault reset"},
                {"address": "M0.0", "symbol": "SAFETY_OK", "data_type": "BOOL", "format": "BOOL", "comment": "Latched safety relay permissive"},
                {"address": "Q0.3", "symbol": "SAFETY_READY_LAMP", "data_type": "BOOL", "format": "BOOL", "comment": "Green safety ready lamp"},
            ],
            "Motors & Actuators": [
                {"address": "I0.3", "symbol": "START_PB", "data_type": "BOOL", "format": "BOOL", "comment": "Conveyor start pushbutton"},
                {"address": "I0.4", "symbol": "STOP_PB_NC", "data_type": "BOOL", "format": "BOOL", "comment": "Conveyor stop pushbutton (NC)"},
                {"address": "I0.5", "symbol": "CONTACTOR_FB", "data_type": "BOOL", "format": "BOOL", "comment": "Main contactor aux feedback"},
                {"address": "Q0.0", "symbol": "MOTOR_CONTACTOR", "data_type": "BOOL", "format": "BOOL", "comment": "Main motor contactor coil"},
                {"address": "Q0.1", "symbol": "RUN_LAMP", "data_type": "BOOL", "format": "BOOL", "comment": "Conveyor running lamp"},
                {"address": "I0.6", "symbol": "PART_PHOTOEYE", "data_type": "BOOL", "format": "BOOL", "comment": "Discharge photoeye sensor"},
            ],
            "DB & Alarms": [
                {"address": "M0.3", "symbol": "FEEDBACK_FAULT", "data_type": "BOOL", "format": "BOOL", "comment": "T1 2s contactor feedback trip"},
                {"address": "M0.4", "symbol": "CHUTE_JAM_ALM", "data_type": "BOOL", "format": "BOOL", "comment": "T2 3s photoeye jam alarm"},
                {"address": "Q0.2", "symbol": "FAULT_BEACON", "data_type": "BOOL", "format": "BOOL", "comment": "Red fault beacon output"},
                {"address": "DB1.DBW0", "symbol": "BatchTarget", "data_type": "INT", "format": "DEC", "comment": "Retentive batch target"},
                {"address": "DB1.DBW2", "symbol": "TotalPartsRetain", "data_type": "INT", "format": "DEC", "comment": "Retentive total parts"},
            ],
        },
        "trace_channels": [
            {"address": "I0.0", "label": "E-Stop OK", "kind": "digital", "data_type": "BOOL", "color": "#10B981"},
            {"address": "I0.3", "label": "Start PB", "kind": "digital", "data_type": "BOOL", "color": "#3B82F6"},
            {"address": "Q0.0", "label": "Motor Q0.0", "kind": "digital", "data_type": "BOOL", "color": "#22D3EE"},
            {"address": "I0.5", "label": "Aux FB I0.5", "kind": "digital", "data_type": "BOOL", "color": "#A78BFA"},
            {"address": "Q0.2", "label": "Fault Q0.2", "kind": "digital", "data_type": "BOOL", "color": "#EF4444"},
            {"address": "PLANT.motor_rpm", "label": "Motor RPM", "kind": "analog", "data_type": "REAL", "color": "#F59E0B", "unit": "RPM"},
        ],
        "fat_suite": [
            {
                "id": "FAT-CV-01",
                "name": "Safety Reset & Normal Conveyor Start/Stop",
                "category": "Functional",
                "description": "Verify safety chain requires manual reset before motor starts, seals in cleanly, and stops on NC Stop PB.",
                "steps": [
                    {"op": "write", "address": "I0.0", "value": True},
                    {"op": "write", "address": "I0.1", "value": True},
                    {"op": "write", "address": "I0.4", "value": True},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Motor blocked before safety reset"},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "write", "address": "I0.2", "value": True},
                    {"op": "step", "cycles": 2},
                    {"op": "write", "address": "I0.2", "value": False},
                    {"op": "assert", "address": "M0.0", "expect": True, "label": "Safety relay latched after Reset PB"},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 4},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "Q0.0", "expect": True, "label": "Motor contactor sealed in after Start PB release"},
                    {"op": "write", "address": "I0.4", "value": False},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Motor stops immediately on Stop PB NC"},
                ],
            },
            {
                "id": "FAT-CV-02",
                "name": "E-Stop Immediate De-Energization & No Auto-Restart",
                "category": "Safety Interlock",
                "description": "Trip E-Stop while motor is running; verify immediate Q0.0 drop and verify releasing E-Stop does NOT auto-restart.",
                "steps": [
                    {"op": "write", "address": "I0.2", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.2", "value": False},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 4},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "assert", "address": "Q0.0", "expect": True, "label": "Motor running prior to E-Stop"},
                    {"op": "fault", "fault_id": "estop_cut", "active": True},
                    {"op": "step", "cycles": 1},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Q0.0 de-energizes on first scan after E-Stop"},
                    {"op": "assert", "address": "M0.0", "expect": False, "label": "Safety latch cleared on E-Stop"},
                    {"op": "fault", "fault_id": "estop_cut", "active": False},
                    {"op": "step", "cycles": 3},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "No automatic restart after E-Stop release"},
                    {"op": "assert", "address": "M0.0", "expect": False, "label": "Safety remains unlatched until manual reset"},
                ],
            },
            {
                "id": "FAT-CV-03",
                "name": "Contactor Feedback Supervision Fault (T1 2.0s Trip)",
                "category": "Fault Resilience",
                "description": "Inject welded/open auxiliary feedback fault; verify T1 trips within 2.0s, latches M0.3/Q0.2, and drops Q0.0.",
                "steps": [
                    {"op": "fault", "fault_id": "contactor_feedback_jam", "active": True},
                    {"op": "write", "address": "I0.2", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.2", "value": False},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 2},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "step", "cycles": 22},
                    {"op": "assert", "address": "M0.3", "expect": True, "label": "Feedback fault latched after 2s timeout"},
                    {"op": "assert", "address": "Q0.2", "expect": True, "label": "Fault beacon energized"},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Main contactor tripped safe"},
                ],
            },
            {
                "id": "FAT-CV-04",
                "name": "24V Brownout & OB100 Warm Restart Retentive Memory",
                "category": "Power Recovery",
                "description": "Write retentive counter in DB1, run motor, trigger 24V power cycle (OB100), confirm Q0.0 drops while DB1 survives.",
                "steps": [
                    {"op": "write", "address": "DB1.DBW2", "value": 42, "data_type": "INT"},
                    {"op": "write", "address": "I0.2", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.2", "value": False},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 4},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "assert", "address": "Q0.0", "expect": True, "label": "Motor running before brownout"},
                    {"op": "brownout"},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Output Q0.0 safe-OFF after OB100 warm restart"},
                    {"op": "assert", "address": "DB1.DBW2", "data_type": "INT", "expect": 42, "label": "Retentive DB1 counter preserved across brownout"},
                ],
            },
            {
                "id": "FAT-CV-05",
                "name": "OB1 Scan-Time Watchdog Headroom (>= 50%)",
                "category": "Timing & Watchdog",
                "description": "Run 25 continuous scans and confirm max scan time leaves >= 50% headroom under CPU watchdog limit.",
                "steps": [
                    {"op": "write", "address": "I0.2", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.2", "value": False},
                    {"op": "write", "address": "I0.3", "value": True},
                    {"op": "step", "cycles": 2},
                    {"op": "write", "address": "I0.3", "value": False},
                    {"op": "step", "cycles": 25},
                    {"op": "assert_watchdog", "min_headroom_pct": 50.0, "label": "CPU scan time headroom >= 50%"},
                ],
            },
        ],
    },
    "tank_pid": {
        "id": "tank_pid",
        "name": "Closed-Loop Tank Level PID (OB35 + FB41)",
        "cpu_family": "S7-300",
        "order_number": "6ES7 315-2EH14-0AB0",
        "summary": "100ms cyclic interrupt OB35 calling Siemens FC105 SCALE, FB41 CONT_C PID, and FC106 UNSCALE with 4–20mA wire-break (0x8000) clamping and HH/LL alarms.",
        "plant_type": "tank_pid",
        "scan_interval_ms": 100,
        "watchdog_limit_ms": 150.0,
        "blocks": [
            {"name": "DB41", "language": "DB", "source": TANK_PID_DB41_AWL},
            {"name": "OB1", "language": "AWL", "source": TANK_PID_OB1_AWL},
            {"name": "OB35", "language": "AWL", "source": TANK_PID_OB35_AWL},
        ],
        "symbols": {
            "ESTOP_OK": ("I0.0", "BOOL"),
            "AUTO_ENABLE": ("I0.1", "BOOL"),
            "LEVEL_RAW_PIW": ("PIW256", "INT"),
            "VALVE_OUT_PQW": ("PQW256", "INT"),
            "PV_LEVEL_PCT": ("MD10", "REAL"),
            "SP_LEVEL_PCT": ("MD14", "REAL"),
            "PID_LMN_PCT": ("MD18", "REAL"),
            "PID_ERR_PCT": ("MD22", "REAL"),
            "FEED_PUMP_EN": ("Q0.0", "BOOL"),
            "LEVEL_HH_ALM": ("Q0.1", "BOOL"),
            "SENSOR_FLT_ALM": ("Q0.2", "BOOL"),
            "LOOP_ACTIVE": ("M0.0", "BOOL"),
            "WIRE_BREAK_FLT": ("M1.0", "BOOL"),
            "OVERRANGE_FLT": ("M1.1", "BOOL"),
            "ANALOG_ANY_FLT": ("M1.2", "BOOL"),
        },
        "initial_writes": [
            {"address": "I0.0", "value": True, "data_type": "BOOL"},
            {"address": "I0.1", "value": True, "data_type": "BOOL"},
            {"address": "MD14", "value": 60.0, "data_type": "REAL"},
        ],
        "vat_tables": {
            "Analog & PID": [
                {"address": "PIW256", "symbol": "LEVEL_RAW_PIW", "data_type": "INT", "format": "DEC", "comment": "Raw 4-20mA sensor (0..27648)"},
                {"address": "MD10", "symbol": "PV_LEVEL_PCT", "data_type": "REAL", "format": "REAL", "comment": "FC105 scaled tank level (%)"},
                {"address": "MD14", "symbol": "SP_LEVEL_PCT", "data_type": "REAL", "format": "REAL", "comment": "PID Setpoint SP_INT (%)"},
                {"address": "MD18", "symbol": "PID_LMN_PCT", "data_type": "REAL", "format": "REAL", "comment": "FB41 control output LMN (%)"},
                {"address": "MD22", "symbol": "PID_ERR_PCT", "data_type": "REAL", "format": "REAL", "comment": "FB41 control error ER (%)"},
                {"address": "PQW256", "symbol": "VALVE_OUT_PQW", "data_type": "INT", "format": "DEC", "comment": "FC106 valve analog output"},
            ],
            "Safety & E-Stop": [
                {"address": "I0.0", "symbol": "ESTOP_OK", "data_type": "BOOL", "format": "BOOL", "comment": "E-Stop healthy (NC)"},
                {"address": "I0.1", "symbol": "AUTO_ENABLE", "data_type": "BOOL", "format": "BOOL", "comment": "Auto PID mode selector"},
                {"address": "M0.0", "symbol": "LOOP_ACTIVE", "data_type": "BOOL", "format": "BOOL", "comment": "Loop enabled & healthy"},
                {"address": "Q0.0", "symbol": "FEED_PUMP_EN", "data_type": "BOOL", "format": "BOOL", "comment": "Feed pump enable output"},
            ],
            "DB & Alarms": [
                {"address": "M1.0", "symbol": "WIRE_BREAK_FLT", "data_type": "BOOL", "format": "BOOL", "comment": "PIW256 < 0 (0x8000 wire-break)"},
                {"address": "M1.1", "symbol": "OVERRANGE_FLT", "data_type": "BOOL", "format": "BOOL", "comment": "PIW256 > 27648 overflow"},
                {"address": "Q0.1", "symbol": "LEVEL_HH_ALM", "data_type": "BOOL", "format": "BOOL", "comment": "High-High level > 90%"},
                {"address": "Q0.2", "symbol": "SENSOR_FLT_ALM", "data_type": "BOOL", "format": "BOOL", "comment": "Analog sensor fault lamp"},
            ],
        },
        "trace_channels": [
            {"address": "MD14", "label": "Setpoint SP (%)", "kind": "analog", "data_type": "REAL", "color": "#3B82F6", "unit": "%"},
            {"address": "MD10", "label": "Level PV (%)", "kind": "analog", "data_type": "REAL", "color": "#10B981", "unit": "%"},
            {"address": "MD18", "label": "Valve LMN (%)", "kind": "analog", "data_type": "REAL", "color": "#F59E0B", "unit": "%"},
            {"address": "Q0.0", "label": "Pump Q0.0", "kind": "digital", "data_type": "BOOL", "color": "#22D3EE"},
            {"address": "Q0.2", "label": "Sensor Fault", "kind": "digital", "data_type": "BOOL", "color": "#EF4444"},
        ],
        "fat_suite": [
            {
                "id": "FAT-PID-01",
                "name": "Closed-Loop Step Response to 60% Setpoint (OB35 + FB41)",
                "category": "Control Loop",
                "description": "Step tank from 20% initial level to 60% SP over 12s; verify PV converges within +-3% steady-state error.",
                "steps": [
                    {"op": "write", "address": "MD14", "value": 60.0, "data_type": "REAL"},
                    {"op": "step", "cycles": 120},
                    {"op": "assert_range", "address": "MD10", "data_type": "REAL", "min": 57.0, "max": 63.0, "label": "Tank level PV settled near 60% SP"},
                    {"op": "assert", "address": "Q0.2", "expect": False, "label": "No analog sensor fault during normal operation"},
                ],
            },
            {
                "id": "FAT-PID-02",
                "name": "Setpoint Tracking (60% -> 75%)",
                "category": "Control Loop",
                "description": "Raise setpoint to 75% and verify FB41 tracks smoothly without High-High trip.",
                "steps": [
                    {"op": "write", "address": "MD14", "value": 60.0, "data_type": "REAL"},
                    {"op": "step", "cycles": 80},
                    {"op": "write", "address": "MD14", "value": 75.0, "data_type": "REAL"},
                    {"op": "step", "cycles": 100},
                    {"op": "assert_range", "address": "MD10", "data_type": "REAL", "min": 71.5, "max": 78.5, "label": "PV tracked new 75% setpoint"},
                    {"op": "assert", "address": "Q0.1", "expect": False, "label": "No spurious High-High (>90%) alarm"},
                ],
            },
            {
                "id": "FAT-PID-03",
                "name": "4-20mA Analog Wire-Break (PIW256 = -32768) Safe Valve Clamp",
                "category": "Fault Resilience",
                "description": "Inject open-circuit wire-break on PIW256; confirm M1.0/Q0.2 trip and PQW256 clamps immediately to 0.",
                "steps": [
                    {"op": "step", "cycles": 20},
                    {"op": "fault", "fault_id": "analog_wire_break", "active": True},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "M1.0", "expect": True, "label": "Wire-break bit M1.0 detected"},
                    {"op": "assert", "address": "Q0.2", "expect": True, "label": "Sensor fault alarm Q0.2 energized"},
                    {"op": "assert", "address": "PQW256", "data_type": "INT", "expect": 0, "label": "Control valve PQW256 clamped safe to 0"},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Feed pump Q0.0 disabled on sensor fault"},
                ],
            },
            {
                "id": "FAT-PID-04",
                "name": "Analog Overrange (PIW256 = +32767) Safe Valve Clamp",
                "category": "Fault Resilience",
                "description": "Inject sensor overrange (> 20.5mA); verify M1.1 trips and valve clamps to 0.",
                "steps": [
                    {"op": "step", "cycles": 10},
                    {"op": "fault", "fault_id": "analog_overrange", "active": True},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "M1.1", "expect": True, "label": "Overrange bit M1.1 detected"},
                    {"op": "assert", "address": "PQW256", "data_type": "INT", "expect": 0, "label": "Valve PQW256 clamped to 0 on overrange"},
                ],
            },
            {
                "id": "FAT-PID-05",
                "name": "E-Stop Drops Pump & Valve Output Immediately",
                "category": "Safety Interlock",
                "description": "Cut E-Stop while PID loop is driving valve open; verify PQW256 and Q0.0 immediately drop to 0.",
                "steps": [
                    {"op": "step", "cycles": 15},
                    {"op": "fault", "fault_id": "estop_cut", "active": True},
                    {"op": "step", "cycles": 1},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Feed pump off immediately on E-Stop"},
                    {"op": "assert", "address": "PQW256", "data_type": "INT", "expect": 0, "label": "Valve output PQW256 zeroed on E-Stop"},
                ],
            },
        ],
    },
    "batch_mixer_sfc": {
        "id": "batch_mixer_sfc",
        "name": "Sequential Batch Reactor (S7-GRAPH SFC)",
        "cpu_family": "S7-300",
        "order_number": "6ES7 315-2EH14-0AB0",
        "summary": "S7-GRAPH FB20 sequential state machine (S0_IDLE -> S1_FILL -> S2_HEAT_MIX -> S3_DRAIN -> S4_COMPLETE) with OB1 E-Stop abort & manual reset.",
        "plant_type": "batch_mixer_sfc",
        "scan_interval_ms": 100,
        "watchdog_limit_ms": 150.0,
        "blocks": [
            {"name": "DB10", "language": "DB", "source": BATCH_MIXER_DB10_AWL},
            {"name": "FB20", "language": "GRAPH", "source": BATCH_MIXER_FB20_GR7},
            {"name": "OB1", "language": "AWL", "source": BATCH_MIXER_OB1_AWL},
        ],
        "symbols": {
            "ESTOP_OK": ("I0.0", "BOOL"),
            "START_BATCH_PB": ("I0.1", "BOOL"),
            "LEVEL_HIGH_SW": ("I0.2", "BOOL"),
            "TEMP_REACHED_SW": ("I0.3", "BOOL"),
            "TANK_EMPTY_SW": ("I0.4", "BOOL"),
            "RESET_ACK_PB": ("I0.5", "BOOL"),
            "INLET_VALVE": ("Q0.0", "BOOL"),
            "AGITATOR_MTR": ("Q0.1", "BOOL"),
            "HEATER_COIL": ("Q0.2", "BOOL"),
            "DRAIN_VALVE": ("Q0.3", "BOOL"),
            "BATCH_DONE_LAMP": ("Q0.4", "BOOL"),
            "SAFETY_OK": ("M0.0", "BOOL"),
            "ABORT_LATCH": ("M0.7", "BOOL"),
        },
        "initial_writes": [
            {"address": "I0.0", "value": True, "data_type": "BOOL"},
            {"address": "I0.4", "value": True, "data_type": "BOOL"},
        ],
        "vat_tables": {
            "Safety & E-Stop": [
                {"address": "I0.0", "symbol": "ESTOP_OK", "data_type": "BOOL", "format": "BOOL", "comment": "Reactor E-Stop healthy (NC)"},
                {"address": "I0.5", "symbol": "RESET_ACK_PB", "data_type": "BOOL", "format": "BOOL", "comment": "Safety & cycle reset PB"},
                {"address": "M0.0", "symbol": "SAFETY_OK", "data_type": "BOOL", "format": "BOOL", "comment": "Latched safety permissive"},
                {"address": "M0.7", "symbol": "ABORT_LATCH", "data_type": "BOOL", "format": "BOOL", "comment": "Emergency abort active"},
            ],
            "Motors & Actuators": [
                {"address": "I0.1", "symbol": "START_BATCH_PB", "data_type": "BOOL", "format": "BOOL", "comment": "Start batch sequence PB"},
                {"address": "Q0.0", "symbol": "INLET_VALVE", "data_type": "BOOL", "format": "BOOL", "comment": "S1 Fill inlet valve"},
                {"address": "Q0.1", "symbol": "AGITATOR_MTR", "data_type": "BOOL", "format": "BOOL", "comment": "S2 Agitator motor"},
                {"address": "Q0.2", "symbol": "HEATER_COIL", "data_type": "BOOL", "format": "BOOL", "comment": "S2 Jacket heater"},
                {"address": "Q0.3", "symbol": "DRAIN_VALVE", "data_type": "BOOL", "format": "BOOL", "comment": "S3 Discharge drain valve"},
                {"address": "Q0.4", "symbol": "BATCH_DONE_LAMP", "data_type": "BOOL", "format": "BOOL", "comment": "S4 Batch complete lamp"},
            ],
            "DB & Alarms": [
                {"address": "I0.2", "symbol": "LEVEL_HIGH_SW", "data_type": "BOOL", "format": "BOOL", "comment": "Vessel level >= 90%"},
                {"address": "I0.3", "symbol": "TEMP_REACHED_SW", "data_type": "BOOL", "format": "BOOL", "comment": "Vessel temp >= 80 C"},
                {"address": "I0.4", "symbol": "TANK_EMPTY_SW", "data_type": "BOOL", "format": "BOOL", "comment": "Vessel level <= 5%"},
                {"address": "DB10.DBW8", "symbol": "CompletedBatches", "data_type": "INT", "format": "DEC", "comment": "Retentive batch count"},
            ],
        },
        "trace_channels": [
            {"address": "Q0.0", "label": "Fill Valve", "kind": "digital", "data_type": "BOOL", "color": "#3B82F6"},
            {"address": "Q0.1", "label": "Agitator", "kind": "digital", "data_type": "BOOL", "color": "#A78BFA"},
            {"address": "Q0.2", "label": "Heater", "kind": "digital", "data_type": "BOOL", "color": "#EF4444"},
            {"address": "Q0.3", "label": "Drain Valve", "kind": "digital", "data_type": "BOOL", "color": "#22D3EE"},
            {"address": "PLANT.level_pct", "label": "Vessel Level", "kind": "analog", "data_type": "REAL", "color": "#10B981", "unit": "%"},
            {"address": "PLANT.temp_c", "label": "Vessel Temp", "kind": "analog", "data_type": "REAL", "color": "#F59E0B", "unit": "C"},
        ],
        "fat_suite": [
            {
                "id": "FAT-BM-01",
                "name": "Idle Safety Hold Before Reset",
                "category": "Safety Interlock",
                "description": "Pressing Start Batch before manual Safety Reset must keep all reactor outputs de-energized.",
                "steps": [
                    {"op": "write", "address": "I0.1", "value": True},
                    {"op": "step", "cycles": 3},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Inlet valve closed before safety reset"},
                    {"op": "assert", "address": "Q0.2", "expect": False, "label": "Heater off before safety reset"},
                ],
            },
            {
                "id": "FAT-BM-02",
                "name": "Full Automatic Batch Progression (S0 -> S1 -> S2 -> S3 -> S4)",
                "category": "Functional",
                "description": "Start batch and verify clean progression through Fill, Heat/Mix, Drain, and Complete states.",
                "steps": [
                    {"op": "write", "address": "I0.5", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.5", "value": False},
                    {"op": "write", "address": "I0.1", "value": True},
                    {"op": "step", "cycles": 2},
                    {"op": "write", "address": "I0.1", "value": False},
                    {"op": "assert", "address": "Q0.0", "expect": True, "label": "S1_FILL opens inlet valve Q0.0"},
                    {"op": "step", "cycles": 25},
                    {"op": "assert", "address": "Q0.1", "expect": True, "label": "S2_HEAT_MIX starts agitator Q0.1"},
                    {"op": "assert", "address": "Q0.2", "expect": True, "label": "S2_HEAT_MIX energizes heater Q0.2"},
                    {"op": "step", "cycles": 35},
                    {"op": "assert", "address": "Q0.3", "expect": True, "label": "S3_DRAIN opens discharge valve Q0.3"},
                    {"op": "step", "cycles": 30},
                    {"op": "assert", "address": "Q0.4", "expect": True, "label": "S4_COMPLETE sets Batch Done lamp Q0.4"},
                ],
            },
            {
                "id": "FAT-BM-03",
                "name": "Mid-Batch E-Stop Immediate Abort of Valves & Heater",
                "category": "Safety Interlock",
                "description": "Trip E-Stop mid-fill; confirm inlet valve drops immediately and abort latch M0.7 sets.",
                "steps": [
                    {"op": "write", "address": "I0.5", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.5", "value": False},
                    {"op": "write", "address": "I0.1", "value": True},
                    {"op": "step", "cycles": 4},
                    {"op": "write", "address": "I0.1", "value": False},
                    {"op": "assert", "address": "Q0.0", "expect": True, "label": "Filling active prior to E-Stop"},
                    {"op": "fault", "fault_id": "estop_cut", "active": True},
                    {"op": "step", "cycles": 1},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Inlet valve closed immediately on E-Stop"},
                    {"op": "assert", "address": "M0.7", "expect": True, "label": "Emergency abort flag M0.7 latched"},
                    {"op": "fault", "fault_id": "estop_cut", "active": False},
                    {"op": "step", "cycles": 3},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "No auto-restart after E-Stop release"},
                ],
            },
            {
                "id": "FAT-BM-04",
                "name": "24V Brownout Drops Active Outputs Safely",
                "category": "Power Recovery",
                "description": "Cycle 24V power mid-batch; confirm OB100 warm restart leaves all valves/heaters off while DB10 recipe persists.",
                "steps": [
                    {"op": "write", "address": "DB10.DBW8", "value": 7, "data_type": "INT"},
                    {"op": "write", "address": "I0.5", "value": True},
                    {"op": "step", "cycles": 1},
                    {"op": "write", "address": "I0.5", "value": False},
                    {"op": "write", "address": "I0.1", "value": True},
                    {"op": "step", "cycles": 4},
                    {"op": "write", "address": "I0.1", "value": False},
                    {"op": "brownout"},
                    {"op": "step", "cycles": 2},
                    {"op": "assert", "address": "Q0.0", "expect": False, "label": "Inlet valve closed after OB100 warm restart"},
                    {"op": "assert", "address": "DB10.DBW8", "data_type": "INT", "expect": 7, "label": "Retentive DB10 batch count preserved"},
                ],
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# Static Safety Auditor, Cross-Reference Extractor & Ladder Network Parser
# ---------------------------------------------------------------------------

_ADDR_TOKEN_RE = re.compile(
    r"\b(DB\s*\d+\s*\.\s*DB(?:X\s*\d+\.\d+|B\s*\d+|W\s*\d+|D\s*\d+)|"
    r"PI[BWD]\s*\d+|PQ[BWD]\s*\d+|"
    r"[IQM]\s*\d+\.\d+|"
    r"[IQM][BWD]\s*\d+|"
    r"[TC]\s*\d+)\b",
    re.IGNORECASE,
)


def _canon_addr(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().upper())


def parse_block_networks(block_name: str, language: str, source: str) -> List[Dict[str, Any]]:
    """Parse AWL/LAD/SCL/S7-GRAPH block source into structured renderable networks for live power-flow."""
    lang_up = (language or "AWL").strip().upper()
    networks: List[Dict[str, Any]] = []

    if lang_up == "DB":
        return []

    if lang_up == "GRAPH":
        step_re = re.compile(
            r"\b(INITIAL_STEP|STEP)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:([\s\S]*?)\bEND_STEP\b",
            re.IGNORECASE,
        )
        tr_re = re.compile(
            r"\bTRANSITION\s+([A-Za-z_][A-Za-z0-9_]*)\s+FROM\s+([^\s:=]+)\s+TO\s+([^\s:=]+)\s*:=?([\s\S]*?)\bEND_TRANSITION\b",
            re.IGNORECASE,
        )
        transitions = [
            {
                "name": m.group(1).strip(),
                "from_step": m.group(2).strip(),
                "to_step": m.group(3).strip(),
                "condition": " ".join(m.group(4).split()).rstrip(";"),
            }
            for m in tr_re.finditer(source)
        ]
        for idx, m in enumerate(step_re.finditer(source), start=1):
            is_init = m.group(1).upper() == "INITIAL_STEP"
            sname = m.group(2).strip()
            actions = []
            for ln in m.group(3).splitlines():
                clean = ln.split("//", 1)[0].strip().rstrip(";")
                if not clean:
                    continue
                parts = clean.split(None, 1)
                if len(parts) == 2:
                    actions.append({"qualifier": parts[0].upper(), "operand": _canon_addr(parts[1])})
            out_tr = [t for t in transitions if t["from_step"].upper() == sname.upper()]
            networks.append(
                {
                    "id": f"{block_name}:NW{idx}",
                    "block": block_name,
                    "index": idx,
                    "language": "GRAPH",
                    "title": f"{'Initial ' if is_init else ''}Step {sname}",
                    "step_name": sname,
                    "is_initial": is_init,
                    "elements": [
                        {"kind": "sfc_step", "step_name": sname, "is_initial": is_init, "actions": actions},
                        *[
                            {
                                "kind": "sfc_transition",
                                "name": t["name"],
                                "to_step": t["to_step"],
                                "condition": t["condition"],
                            }
                            for t in out_tr
                        ],
                    ],
                    "code_lines": [ln.rstrip() for ln in m.group(0).splitlines()],
                }
            )
        return networks

    # Split AWL/LAD/SCL by NETWORK markers (or treat whole body as Network 1)
    lines = source.splitlines()
    chunks: List[Tuple[str, List[str]]] = []
    cur_title = f"{block_name} Main Logic"
    cur_lines: List[str] = []
    saw_network = False
    in_body = lang_up == "SCL"
    for raw in lines:
        stripped = raw.strip()
        up = stripped.upper()
        if up == "BEGIN":
            in_body = True
            continue
        if up.startswith(("END_ORGANIZATION_BLOCK", "END_FUNCTION", "END_FUNCTION_BLOCK")):
            break
        if not in_body:
            continue
        if up == "NETWORK" or up.startswith("NETWORK "):
            if saw_network and cur_lines:
                chunks.append((cur_title, cur_lines))
            saw_network = True
            cur_title = f"Network {len(chunks) + 1}"
            cur_lines = []
            continue
        if up.startswith("TITLE =") or up.startswith("TITLE="):
            cur_title = stripped.split("=", 1)[1].strip() or cur_title
            continue
        if stripped:
            cur_lines.append(raw.rstrip())
    if cur_lines:
        chunks.append((cur_title, cur_lines))

    for idx, (title, code_lines) in enumerate(chunks, start=1):
        elements: List[Dict[str, Any]] = []
        i = 0
        while i < len(code_lines):
            raw_line = code_lines[i]
            code = raw_line.split("//", 1)[0].strip().rstrip(";")
            i += 1
            if not code:
                continue
            # Merge multi-line CALL (...) blocks
            if code.upper().startswith("CALL ") and "(" in code and ")" not in code:
                buf = [code]
                while i < len(code_lines):
                    nxt = code_lines[i].split("//", 1)[0].strip().rstrip(";")
                    i += 1
                    buf.append(nxt)
                    if ")" in nxt:
                        break
                code = " ".join(buf)

            # Remove jump label prefix (e.g. "OKVL: NOP 0")
            if ":" in code and not code.upper().startswith("CALL") and ":=" not in code.split(":", 1)[0]:
                left, right = code.split(":", 1)
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", left.strip()):
                    code = right.strip()
                    if not code:
                        continue

            up = code.upper()
            if up in ("A(", "O(", ")", "NOP 0"):
                continue

            m_call = re.match(r"^CALL\s+(FC|FB|SFB|SFC)\s*(\d+)(?:\s*,\s*(DB\s*\d+))?", code, re.IGNORECASE)
            if m_call:
                blk_ref = f"{m_call.group(1).upper()}{m_call.group(2)}"
                idb = _canon_addr(m_call.group(3)) if m_call.group(3) else None
                params_preview = []
                if "(" in code and ")" in code:
                    inner = code[code.find("(") + 1 : code.rfind(")")]
                    for p in inner.split(","):
                        if ":=" in p:
                            k, v = p.split(":=", 1)
                            params_preview.append({"param": k.strip().upper(), "value": _canon_addr(v) if _ADDR_TOKEN_RE.search(v) else v.strip()})
                elements.append({"kind": "call", "block": blk_ref, "instance_db": idb, "params": params_preview[:8]})
                continue

            parts = code.split(None, 1)
            op = parts[0].upper()
            operand_raw = parts[1].strip() if len(parts) > 1 else ""
            operand = _canon_addr(operand_raw) if operand_raw else ""

            if op in ("A", "U"):
                elements.append({"kind": "contact", "mode": "NO", "logic": "AND", "operand": operand})
            elif op in ("AN", "UN"):
                elements.append({"kind": "contact", "mode": "NC", "logic": "AND", "operand": operand})
            elif op == "O":
                elements.append({"kind": "contact", "mode": "NO", "logic": "OR", "operand": operand})
            elif op == "ON":
                elements.append({"kind": "contact", "mode": "NC", "logic": "OR", "operand": operand})
            elif op == "=":
                elements.append({"kind": "coil", "mode": "ASSIGN", "operand": operand})
            elif op == "S":
                elements.append({"kind": "coil", "mode": "SET", "operand": operand})
            elif op == "R":
                elements.append({"kind": "coil", "mode": "RESET", "operand": operand})
            elif op in ("SD", "SE", "SS", "SF", "SP"):
                elements.append({"kind": "timer", "mode": op, "operand": operand})
            elif op in ("CU", "CD"):
                elements.append({"kind": "counter", "mode": op, "operand": operand})
            elif op in ("<I", ">I", "==I", "<=I", ">=I", "<R", ">R", "==R", "<=R", ">=R"):
                elements.append({"kind": "compare", "op": op})
            elif op == "L":
                elements.append({"kind": "load", "operand": operand_raw})
            elif op == "T":
                elements.append({"kind": "transfer", "operand": operand})

        networks.append(
            {
                "id": f"{block_name}:NW{idx}",
                "block": block_name,
                "index": idx,
                "language": lang_up,
                "title": title,
                "elements": elements,
                "code_lines": code_lines,
            }
        )
    return networks


def analyze_static_safety_and_xref(
    blocks: Sequence[Dict[str, str]],
    symbols: Dict[str, Tuple[str, Optional[str]]],
    cpu_family: str = "S7-300",
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Run static safety checks and build full Read/Write cross-reference table across all loaded blocks."""
    addr_to_sym = {_canon_addr(addr): sym for sym, (addr, _) in symbols.items()}
    xref: Dict[str, Dict[str, Any]] = {}
    findings: List[Dict[str, Any]] = []
    assign_coils: Dict[str, List[str]] = {}
    set_coils: Dict[str, List[str]] = {}
    reset_coils: Dict[str, List[str]] = {}

    def _touch(addr: str, block: str, nw_idx: int, access: str) -> None:
        ca = _canon_addr(addr)
        if not ca:
            return
        rec = xref.setdefault(
            ca,
            {
                "address": ca,
                "symbol": addr_to_sym.get(ca, ""),
                "reads": [],
                "writes": [],
                "read_count": 0,
                "write_count": 0,
            },
        )
        loc = f"{block}:NW{nw_idx}"
        if access == "W":
            if loc not in rec["writes"]:
                rec["writes"].append(loc)
            rec["write_count"] += 1
        else:
            if loc not in rec["reads"]:
                rec["reads"].append(loc)
            rec["read_count"] += 1

    for blk in blocks:
        bname = blk["name"].upper()
        lang = blk.get("language", "AWL").upper()
        src = blk.get("source", "")
        if lang == "DB":
            continue
        nws = parse_block_networks(bname, lang, src)
        for nw in nws:
            nw_idx = nw["index"]
            for el in nw["elements"]:
                kind = el.get("kind")
                opnd = el.get("operand", "")
                if kind == "contact" and opnd:
                    _touch(opnd, bname, nw_idx, "R")
                elif kind == "coil" and opnd:
                    _touch(opnd, bname, nw_idx, "W")
                    mode = el.get("mode")
                    loc = f"{bname}:NW{nw_idx}"
                    if mode == "ASSIGN":
                        assign_coils.setdefault(opnd, []).append(loc)
                    elif mode == "SET":
                        set_coils.setdefault(opnd, []).append(loc)
                    elif mode == "RESET":
                        reset_coils.setdefault(opnd, []).append(loc)
                elif kind == "timer" and opnd:
                    _touch(opnd, bname, nw_idx, "W")
                elif kind == "counter" and opnd:
                    _touch(opnd, bname, nw_idx, "W")
                elif kind == "load" and opnd and _ADDR_TOKEN_RE.fullmatch(opnd.strip()):
                    _touch(opnd, bname, nw_idx, "R")
                elif kind == "transfer" and opnd:
                    _touch(opnd, bname, nw_idx, "W")
                elif kind == "call":
                    for p in el.get("params", []):
                        pv = p.get("value", "")
                        if _ADDR_TOKEN_RE.fullmatch(pv):
                            is_out = p.get("param") in ("OUT", "LMN", "ER", "RET_VAL", "Q", "ET", "CV")
                            _touch(pv, bname, nw_idx, "W" if is_out else "R")
                elif kind == "sfc_step":
                    for act in el.get("actions", []):
                        _touch(act["operand"], bname, nw_idx, "W")
                elif kind == "sfc_transition":
                    for m in _ADDR_TOKEN_RE.finditer(el.get("condition", "")):
                        _touch(m.group(1), bname, nw_idx, "R")

    # 1. Duplicate '=' coil assignment check
    for addr, locs in sorted(assign_coils.items()):
        if len(locs) > 1:
            findings.append(
                {
                    "id": f"DUP_COIL_{addr}",
                    "severity": "error",
                    "rule": "Duplicate Output Coil",
                    "address": addr,
                    "locations": locs,
                    "message": f"Address {addr} is assigned with '=' in multiple networks ({', '.join(locs)}). Last network wins silently.",
                }
            )

    # 2. SET without corresponding RESET check
    for addr, locs in sorted(set_coils.items()):
        if addr not in reset_coils:
            findings.append(
                {
                    "id": f"UNRESET_SET_{addr}",
                    "severity": "warning",
                    "rule": "Latched Coil Without Reset",
                    "address": addr,
                    "locations": locs,
                    "message": f"Address {addr} is latched via S ({', '.join(locs)}) with no matching R instruction in the loaded blocks.",
                }
            )

    # 3. Direct write to physical Input (I) area check
    for addr, rec in sorted(xref.items()):
        if addr.startswith("I") and not addr.startswith("IN") and rec["write_count"] > 0:
            findings.append(
                {
                    "id": f"WRITE_INPUT_{addr}",
                    "severity": "error",
                    "rule": "Write to Process Input Image",
                    "address": addr,
                    "locations": rec["writes"],
                    "message": f"Program writes directly to input {addr} ({', '.join(rec['writes'])}). Physical inputs should only be read.",
                }
            )

    # 4. Unreferenced write (written Q/M never read or dead internal marker)
    for addr, rec in sorted(xref.items()):
        if addr.startswith("M") and rec["write_count"] > 0 and rec["read_count"] == 0:
            findings.append(
                {
                    "id": f"DEAD_MARKER_{addr}",
                    "severity": "info",
                    "rule": "Write-Only Internal Marker",
                    "address": addr,
                    "locations": rec["writes"],
                    "message": f"Marker {addr} ({rec['symbol'] or 'unnamed'}) is written in {', '.join(rec['writes'])} but never read by logic (HMI/alarm-only marker or unused).",
                }
            )

    # 5. E-Stop / Safety presence check
    has_estop = any("ESTOP" in s.upper() or "E_STOP" in s.upper() for s in symbols) or ("I0.0" in xref)
    if not has_estop:
        findings.append(
            {
                "id": "NO_ESTOP_DETECTED",
                "severity": "warning",
                "rule": "Safety Interlock Verification",
                "address": "I0.0",
                "locations": [],
                "message": "No E-Stop input symbol detected. Confirm hardwired safety relay and soft stop interlocks before commissioning.",
            }
        )

    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    audit = {
        "passed": errors == 0,
        "cpu_family": cpu_family,
        "error_count": errors,
        "warning_count": warnings,
        "info_count": sum(1 for f in findings if f["severity"] == "info"),
        "findings": findings,
        "address_count": len(xref),
    }
    return audit, xref


# ---------------------------------------------------------------------------
# Siemens Verification Session (Stateful Full-Stack Simulation Core)
# ---------------------------------------------------------------------------

class SiemensVerificationSession:
    """Full-stack Siemens STEP 7 Classic verification & simulation session."""

    FAULT_DEFINITIONS: List[Dict[str, Any]] = [
        {
            "id": "estop_cut",
            "name": "E-Stop / Safety Relay Cut",
            "ob": "OB1",
            "severity": "critical",
            "description": "Opens hardwired NC E-Stop input I0.0. Verifies immediate output de-energization and mandatory manual reset.",
        },
        {
            "id": "analog_wire_break",
            "name": "4-20mA Analog Wire-Break (0x8000)",
            "ob": "OB82",
            "severity": "critical",
            "description": "Forces PIW256 to -32768 (0x8000 / <3.5mA) and raises OB82 diagnostic interrupt.",
        },
        {
            "id": "analog_overrange",
            "name": "Analog Channel Overrange (0x7FFF)",
            "ob": "OB82",
            "severity": "warning",
            "description": "Forces PIW256 to +32767 (0x7FFF / >20.5mA) and raises OB82 overflow diagnostic.",
        },
        {
            "id": "contactor_feedback_jam",
            "name": "Contactor Aux Feedback Open / Jam",
            "ob": "OB1",
            "severity": "warning",
            "description": "Prevents motor contactor auxiliary contact I0.5 from closing when Q0.0 energizes.",
        },
        {
            "id": "profinet_io_drop",
            "name": "PROFINET / PROFIBUS Remote I/O Drop",
            "ob": "OB86",
            "severity": "critical",
            "description": "Simulates ET200M distributed I/O rack loss: triggers BF LED and logs OB86 Rack/Station Failure.",
        },
        {
            "id": "scan_load_spike",
            "name": "OB1 Cycle-Time Load Spike (Watchdog)",
            "ob": "OB80",
            "severity": "warning",
            "description": "Injects heavy synthetic cycle execution load approaching CPU watchdog threshold (OB80).",
        },
    ]

    def __init__(self, initial_preset: str = "conveyor_motor") -> None:
        self._lock = threading.RLock()
        self.plc = VirtualS7PLC()
        self.project_id: str = ""
        self.project_name: str = ""
        self.cpu_family: str = "S7-300"
        self.plant_type: str = "conveyor_motor"
        self.blocks: List[Dict[str, str]] = []
        self.symbols: Dict[str, Tuple[str, Optional[str]]] = {}
        self.vat_tables: Dict[str, List[Dict[str, Any]]] = {}
        self.networks: List[Dict[str, Any]] = []
        self.network_coverage: Dict[str, int] = {}
        self.program_sha256: str = ""
        self.static_audit: Dict[str, Any] = {}
        self.cross_references: Dict[str, Dict[str, Any]] = {}

        # Execution & Timing
        self.cpu_mode: str = "STOP"  # STOP, RUN, RUN_P, PAUSED_BREAKPOINT, FAULT_WATCHDOG
        self.scan_interval_ms: int = 100
        self.watchdog_limit_ms: float = 150.0
        self.scan_times_us: List[float] = []
        self.last_scan_us: float = 0.0

        # Force Table (address -> {address, value, data_type, unforced_value, forced_at_scan})
        self.forces: Dict[str, Dict[str, Any]] = {}

        # Faults & Diagnostic Buffer
        self.faults: Dict[str, bool] = {f["id"]: False for f in self.FAULT_DEFINITIONS}
        self.diagnostic_buffer: List[Dict[str, Any]] = []

        # Breakpoints
        self.network_breakpoints: set = set()
        self.condition_breakpoints: List[Dict[str, Any]] = []
        self.last_breakpoint_hit: Optional[Dict[str, Any]] = None

        # 1-Scan Trace Oscilloscope
        self.trace_channels: List[Dict[str, Any]] = []
        self.trace_buffer: List[Dict[str, Any]] = []
        self.trace_max_samples: int = 600
        self.trace_trigger: Dict[str, Any] = {
            "mode": "manual",  # manual | rising | falling | gt | lt
            "channel": "",
            "threshold": 50.0,
            "armed": False,
            "triggered": False,
            "trigger_scan": None,
        }
        self._trace_prev_val: Optional[float] = None

        # Plant Twin State
        self.plant: Dict[str, Any] = {}
        self.disturbance_load: float = 18.0

        # Automated FAT Suite & Last Results
        self.fat_suite: List[Dict[str, Any]] = []
        self.fat_results: Dict[str, Any] = {"executed": False, "passed": False, "cases": []}

        self.load_preset(initial_preset)

    # ---- Diagnostic Buffer & CPU LEDs ----

    def _log_diag(self, event_id: str, severity: str, ob_number: int, message: str) -> None:
        self.diagnostic_buffer.insert(
            0,
            {
                "seq": len(self.diagnostic_buffer) + 1,
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                "event_id": event_id,
                "severity": severity,
                "ob_number": ob_number,
                "scan_count": self.plc.scan_count,
                "sim_time_ms": self.plc.sim_time_ms,
                "message": message,
            },
        )
        if len(self.diagnostic_buffer) > 120:
            self.diagnostic_buffer = self.diagnostic_buffer[:120]

    def _compute_cpu_leds(self) -> Dict[str, Any]:
        sf = bool(
            self.faults.get("analog_wire_break")
            or self.faults.get("analog_overrange")
            or self.faults.get("scan_load_spike")
            or self.cpu_mode == "FAULT_WATCHDOG"
        )
        bf = bool(self.faults.get("profinet_io_drop"))
        frce = len(self.forces) > 0
        running = self.cpu_mode in ("RUN", "RUN_P")
        stopped = self.cpu_mode in ("STOP", "PAUSED_BREAKPOINT", "FAULT_WATCHDOG")
        return {
            "SF": sf,
            "BF": bf,
            "DC5V": True,
            "FRCE": frce,
            "RUN": running,
            "STOP": stopped,
        }

    # ---- Project Loading (Preset or Custom AI Program) ----

    def load_preset(self, preset_id: str) -> Dict[str, Any]:
        if preset_id not in SIEMENS_PRESETS:
            return {"ok": False, "error": f"Unknown Siemens preset '{preset_id}'. Valid: {sorted(SIEMENS_PRESETS.keys())}"}
        p = SIEMENS_PRESETS[preset_id]
        with self._lock:
            self.plc.reset(clear_blocks=True, clear_symbols=True)
            self.plc.order_number = p["order_number"]
            self.plc.ob35_interval_ms = p["scan_interval_ms"]
            self.project_id = p["id"]
            self.project_name = p["name"]
            self.cpu_family = p["cpu_family"]
            self.plant_type = p["plant_type"]
            self.scan_interval_ms = p["scan_interval_ms"]
            self.watchdog_limit_ms = p["watchdog_limit_ms"]
            self.blocks = copy.deepcopy(p["blocks"])
            self.symbols = copy.deepcopy(p["symbols"])
            self.vat_tables = copy.deepcopy(p["vat_tables"])
            self.trace_channels = copy.deepcopy(p["trace_channels"])
            self.fat_suite = copy.deepcopy(p["fat_suite"])
            self.fat_results = {"executed": False, "passed": False, "cases": []}
            self.forces.clear()
            self.faults = {f["id"]: False for f in self.FAULT_DEFINITIONS}
            self.network_breakpoints.clear()
            self.condition_breakpoints.clear()
            self.last_breakpoint_hit = None
            self.scan_times_us.clear()
            self.last_scan_us = 0.0
            self.trace_buffer.clear()
            self._trace_prev_val = None
            self.diagnostic_buffer.clear()

            # Register symbols & compile blocks into VirtualS7PLC
            self.plc.set_symbols({k: v[0] for k, v in self.symbols.items()})
            load_reports = []
            for blk in self.blocks:
                lr = self.plc.load_block(blk["name"], blk["source"], language=blk["language"])
                load_reports.append(lr)

            for wr in p.get("initial_writes", []):
                self.plc.write_address(wr["address"], wr["value"], data_type=wr.get("data_type"))

            self._rebuild_analysis_and_networks()
            self._init_plant_state()
            self.cpu_mode = "RUN_P"
            self.plc.cpu_mode = "RUN"
            self._log_diag("0x4302", "info", 100, f"Loaded Siemens preset '{self.project_name}' ({self.plc.order_number}).")
            self._log_diag("0x4304", "info", 1, "Mode transition: STOP -> RUN-P (ready for verification).")
            # Execute 1 initial scan to populate initial telemetry & power flow
            self._execute_single_scan(self.scan_interval_ms)
            return {"ok": True, "project_id": self.project_id, "blocks_loaded": len(load_reports)}

    def load_custom_program(
        self,
        *,
        name: str,
        source_code: str,
        cpu_family: str = "S7-300",
        symbols_text: str = "",
        block_name: str = "OB1",
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile and load a custom AI-generated Siemens STEP 7 program (multi-block supported)."""
        src = (source_code or "").strip()
        if not src:
            return {"ok": False, "error": "Source code cannot be empty."}

        parsed_blocks = self._split_multi_block_source(src, default_block=block_name, default_lang=language)
        if not parsed_blocks:
            return {"ok": False, "error": "Could not parse any valid Siemens STEP 7 blocks from source."}

        parsed_symbols = self._parse_symbols_input(symbols_text)

        # Validate compilation on a scratch VirtualS7PLC before mutating active session
        scratch = VirtualS7PLC()
        scratch.set_symbols({k: v[0] for k, v in parsed_symbols.items()})
        for blk in parsed_blocks:
            res = scratch.load_block(blk["name"], blk["source"], language=blk["language"])
            if not res.get("ok"):
                return {"ok": False, "error": f"Failed to compile {blk['name']}: {res.get('error')}"}

        with self._lock:
            self.plc.reset(clear_blocks=True, clear_symbols=True)
            self.project_id = "custom_program"
            self.project_name = (name or "Custom AI STEP 7 Program").strip()
            self.cpu_family = cpu_family or "S7-300"
            self.plant_type = "generic_io"
            self.blocks = parsed_blocks
            self.symbols = parsed_symbols
            self.forces.clear()
            self.faults = {f["id"]: False for f in self.FAULT_DEFINITIONS}
            self.network_breakpoints.clear()
            self.condition_breakpoints.clear()
            self.last_breakpoint_hit = None
            self.scan_times_us.clear()
            self.trace_buffer.clear()
            self.diagnostic_buffer.clear()

            self.plc.set_symbols({k: v[0] for k, v in self.symbols.items()})
            for blk in self.blocks:
                self.plc.load_block(blk["name"], blk["source"], language=blk["language"])

            self._rebuild_analysis_and_networks()
            self._build_auto_vat_and_trace_from_xref()
            self.fat_suite = self._build_default_custom_fat()
            self.fat_results = {"executed": False, "passed": False, "cases": []}
            self._init_plant_state()
            self.cpu_mode = "RUN_P"
            self.plc.cpu_mode = "RUN"
            self._log_diag("0x4302", "info", 100, f"Compiled & loaded custom program '{self.project_name}' ({len(self.blocks)} block(s)).")
            self._execute_single_scan(self.scan_interval_ms)
            return {"ok": True, "project_name": self.project_name, "blocks_loaded": [b["name"] for b in self.blocks]}

    @staticmethod
    def _split_multi_block_source(src: str, default_block: str = "OB1", default_lang: Optional[str] = None) -> List[Dict[str, str]]:
        block_re = re.compile(
            r"(ORGANIZATION_BLOCK\s+OB\s*\d+[\s\S]*?END_ORGANIZATION_BLOCK|"
            r"FUNCTION_BLOCK\s+FB\s*\d+[\s\S]*?END_FUNCTION_BLOCK|"
            r"FUNCTION\s+FC\s*\d+[\s\S]*?END_FUNCTION|"
            r"DATA_BLOCK\s+DB\s*\d+[\s\S]*?END_DATA_BLOCK)",
            re.IGNORECASE,
        )
        matches = list(block_re.finditer(src))
        if not matches:
            lang = default_lang or ("GRAPH" if "INITIAL_STEP" in src.upper() else "AWL")
            return [{"name": _canon_addr(default_block or "OB1"), "language": lang, "source": src}]

        out: List[Dict[str, str]] = []
        for m in matches:
            chunk = m.group(1).strip()
            hdr = chunk.splitlines()[0].strip()
            m_id = re.search(r"\b(OB|FB|FC|DB)\s*(\d+)\b", hdr, re.IGNORECASE)
            bname = f"{m_id.group(1).upper()}{m_id.group(2)}" if m_id else "OB1"
            up = chunk.upper()
            if bname.startswith("DB"):
                lang = "DB"
            elif "INITIAL_STEP" in up and "END_STEP" in up:
                lang = "GRAPH"
            elif ":=" in chunk and ("IF " in up or "END_IF" in up or "FOR " in up):
                lang = "SCL"
            else:
                lang = default_lang or "AWL"
            out.append({"name": bname, "language": lang, "source": chunk})
        return out

    @staticmethod
    def _parse_symbols_input(symbols_text: str) -> Dict[str, Tuple[str, Optional[str]]]:
        text = (symbols_text or "").strip()
        if not text:
            return {}
        out: Dict[str, Tuple[str, Optional[str]]] = {}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str):
                        out[k.strip()] = (_canon_addr(v), None)
                    elif isinstance(v, dict) and v.get("address"):
                        out[k.strip()] = (_canon_addr(str(v["address"])), v.get("data_type"))
                return out
        except Exception:
            pass
        for ln in text.splitlines():
            clean = ln.split("//", 1)[0].strip()
            if not clean:
                continue
            if "=" in clean:
                k, v = clean.split("=", 1)
                out[k.strip()] = (_canon_addr(v), None)
            elif "," in clean:
                parts = [p.strip().strip('"') for p in clean.split(",")]
                if len(parts) >= 2:
                    dtype = parts[2].upper() if len(parts) >= 3 and parts[2] else None
                    out[parts[0]] = (_canon_addr(parts[1]), dtype)
        return out

    def _rebuild_analysis_and_networks(self) -> None:
        combined = "\n\n".join(f"// --- {b['name']} ({b['language']}) ---\n{b['source']}" for b in self.blocks)
        self.program_sha256 = compute_content_hash("s7", combined)
        self.networks = []
        for blk in self.blocks:
            self.networks.extend(parse_block_networks(blk["name"], blk["language"], blk["source"]))
        self.network_coverage = {nw["id"]: 0 for nw in self.networks}
        self.static_audit, self.cross_references = analyze_static_safety_and_xref(
            self.blocks, self.symbols, cpu_family=self.cpu_family
        )

    def _build_auto_vat_and_trace_from_xref(self) -> None:
        inputs = []
        outputs = []
        internals = []
        for addr, rec in sorted(self.cross_references.items()):
            dtype = "BOOL"
            fmt = "BOOL"
            if any(addr.startswith(p) for p in ("PIW", "PQW", "MW", "IW", "QW")) or ".DBW" in addr:
                dtype, fmt = "INT", "DEC"
            elif any(addr.startswith(p) for p in ("MD", "ID", "QD")) or ".DBD" in addr:
                dtype, fmt = "REAL", "REAL"
            row = {
                "address": addr,
                "symbol": rec.get("symbol", ""),
                "data_type": dtype,
                "format": fmt,
                "comment": f"R:{rec['read_count']} / W:{rec['write_count']}",
            }
            if addr.startswith(("I", "PI")):
                inputs.append(row)
            elif addr.startswith(("Q", "PQ")):
                outputs.append(row)
            else:
                internals.append(row)
        self.vat_tables = {
            "Inputs (I / PI)": inputs or [{"address": "I0.0", "symbol": "IN_0", "data_type": "BOOL", "format": "BOOL", "comment": "Input bit"}],
            "Outputs (Q / PQ)": outputs or [{"address": "Q0.0", "symbol": "OUT_0", "data_type": "BOOL", "format": "BOOL", "comment": "Output bit"}],
            "Markers & DBs": internals,
        }
        palette = ["#10B981", "#3B82F6", "#22D3EE", "#F59E0B", "#A78BFA", "#EF4444"]
        chans = []
        for idx, row in enumerate((inputs + outputs + internals)[:6]):
            chans.append(
                {
                    "address": row["address"],
                    "label": row["symbol"] or row["address"],
                    "kind": "digital" if row["data_type"] == "BOOL" else "analog",
                    "data_type": row["data_type"],
                    "color": palette[idx % len(palette)],
                }
            )
        self.trace_channels = chans

    def _build_default_custom_fat(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "FAT-CUST-01",
                "name": "Static Safety Zero-Error Gate & 10-Scan Stability",
                "category": "Timing & Watchdog",
                "description": "Execute 10 deterministic OB1 scans and confirm CPU watchdog headroom >= 50%.",
                "steps": [
                    {"op": "step", "cycles": 10},
                    {"op": "assert_watchdog", "min_headroom_pct": 50.0, "label": "OB1 scan headroom >= 50%"},
                ],
            }
        ]

    # ---- Plant Twin Physics ----

    def _init_plant_state(self) -> None:
        if self.plant_type == "conveyor_motor":
            self.plant = {
                "type": "conveyor_motor",
                "belt_pos_pct": 12.0,
                "motor_rpm": 0.0,
                "motor_current_a": 0.0,
                "part_pos_pct": 20.0,
                "parts_passed": 0,
                "contactor_delay_ms": 0,
                "photoeye_blocked": False,
            }
        elif self.plant_type == "tank_pid":
            self.plant = {
                "type": "tank_pid",
                "level_pct": 20.0,
                "inlet_valve_pct": 0.0,
                "outlet_disturbance_pct": 18.0,
                "temp_c": 42.5,
                "flow_in_lpm": 0.0,
                "flow_out_lpm": 18.0,
            }
            self.disturbance_load = 18.0
            self._sync_tank_sensor_to_plc()
        elif self.plant_type == "batch_mixer_sfc":
            self.plant = {
                "type": "batch_mixer_sfc",
                "level_pct": 0.0,
                "temp_c": 24.0,
                "agitator_rpm": 0.0,
                "active_step": "S0_IDLE",
                "completed_batches": 0,
            }
        else:
            self.plant = {"type": "generic_io", "active_inputs": 0, "active_outputs": 0}

    def _sync_tank_sensor_to_plc(self) -> None:
        if self.faults.get("analog_wire_break"):
            raw = -32768
        elif self.faults.get("analog_overrange"):
            raw = 32767
        else:
            lvl = max(0.0, min(100.0, float(self.plant.get("level_pct", 0.0))))
            raw = int(round((lvl / 100.0) * 27648.0))
        self.plc.write_address("PIW256", raw, data_type="INT")

    def _apply_faults_pre_scan(self) -> None:
        if self.faults.get("estop_cut"):
            self.plc.write_address("I0.0", False, data_type="BOOL")
        if self.plant_type == "tank_pid":
            self._sync_tank_sensor_to_plc()
        elif self.faults.get("analog_wire_break"):
            self.plc.write_address("PIW256", -32768, data_type="INT")
        elif self.faults.get("analog_overrange"):
            self.plc.write_address("PIW256", 32767, data_type="INT")
        if self.plant_type == "conveyor_motor" and self.faults.get("contactor_feedback_jam"):
            self.plc.write_address("I0.5", False, data_type="BOOL")

    def _update_plant_post_scan(self, dt_ms: int) -> None:
        dt_s = max(0.01, dt_ms / 1000.0)
        if self.plant_type == "conveyor_motor":
            cmd_on = bool(self.plc.read_address("Q0.0", data_type="BOOL"))
            if cmd_on:
                self.plant["contactor_delay_ms"] = min(500, self.plant.get("contactor_delay_ms", 0) + dt_ms)
                fb_closed = (self.plant["contactor_delay_ms"] >= 200) and (not self.faults.get("contactor_feedback_jam"))
                self.plc.write_address("I0.5", fb_closed, data_type="BOOL")
                self.plant["motor_rpm"] = min(1450.0, self.plant["motor_rpm"] + 480.0 * dt_s)
                self.plant["motor_current_a"] = round(6.2 + (self.plant["motor_rpm"] / 1450.0) * 4.8, 2)
                self.plant["belt_pos_pct"] = (self.plant["belt_pos_pct"] + 28.0 * dt_s) % 100.0
                prev_part = self.plant["part_pos_pct"]
                nxt_part = prev_part + 28.0 * dt_s
                if nxt_part >= 100.0:
                    nxt_part -= 100.0
                    self.plant["parts_passed"] += 1
                    cur_ret = int(self.plc.read_address("DB1.DBW2", data_type="INT") or 0)
                    self.plc.write_address("DB1.DBW2", cur_ret + 1, data_type="INT")
                self.plant["part_pos_pct"] = round(nxt_part, 2)
                in_beam = 76.0 <= nxt_part <= 84.0
                self.plant["photoeye_blocked"] = in_beam
                if "I0.6" not in self.forces:
                    self.plc.write_address("I0.6", in_beam, data_type="BOOL")
            else:
                self.plant["contactor_delay_ms"] = 0
                if "I0.5" not in self.forces:
                    self.plc.write_address("I0.5", False, data_type="BOOL")
                self.plant["motor_rpm"] = max(0.0, self.plant["motor_rpm"] - 620.0 * dt_s)
                self.plant["motor_current_a"] = round(0.0 if self.plant["motor_rpm"] < 10 else 1.4, 2)
            self.plant["motor_rpm"] = round(self.plant["motor_rpm"], 1)
            self.plant["belt_pos_pct"] = round(self.plant["belt_pos_pct"], 1)

        elif self.plant_type == "tank_pid":
            pqw = int(self.plc.read_address("PQW256", data_type="INT") or 0)
            pump_on = bool(self.plc.read_address("Q0.0", data_type="BOOL"))
            valve_pct = max(0.0, min(100.0, (pqw / 27648.0) * 100.0)) if pump_on else 0.0
            inflow = valve_pct * 0.48
            outflow = max(0.0, min(60.0, float(self.disturbance_load))) * 0.45
            lvl = float(self.plant.get("level_pct", 20.0))
            lvl = max(0.0, min(100.0, lvl + (inflow - outflow) * dt_s * 0.55))
            self.plant["level_pct"] = round(lvl, 2)
            self.plant["inlet_valve_pct"] = round(valve_pct, 2)
            self.plant["outlet_disturbance_pct"] = round(self.disturbance_load, 1)
            self.plant["flow_in_lpm"] = round(inflow, 1)
            self.plant["flow_out_lpm"] = round(outflow, 1)
            self.plant["temp_c"] = round(38.0 + 0.12 * lvl, 1)
            self._sync_tank_sensor_to_plc()

        elif self.plant_type == "batch_mixer_sfc":
            q_fill = bool(self.plc.read_address("Q0.0", data_type="BOOL"))
            q_agit = bool(self.plc.read_address("Q0.1", data_type="BOOL"))
            q_heat = bool(self.plc.read_address("Q0.2", data_type="BOOL"))
            q_drain = bool(self.plc.read_address("Q0.3", data_type="BOOL"))
            q_done = bool(self.plc.read_address("Q0.4", data_type="BOOL"))

            lvl = float(self.plant.get("level_pct", 0.0))
            temp = float(self.plant.get("temp_c", 24.0))
            if q_fill:
                lvl = min(100.0, lvl + 42.0 * dt_s)
            if q_drain:
                lvl = max(0.0, lvl - 38.0 * dt_s)
            if q_heat:
                temp = min(92.0, temp + 21.0 * dt_s)
            else:
                temp = max(24.0, temp - 6.0 * dt_s)

            self.plant["level_pct"] = round(lvl, 2)
            self.plant["temp_c"] = round(temp, 1)
            self.plant["agitator_rpm"] = 240.0 if q_agit else 0.0

            if "I0.2" not in self.forces:
                self.plc.write_address("I0.2", lvl >= 90.0, data_type="BOOL")
            if "I0.3" not in self.forces:
                self.plc.write_address("I0.3", temp >= 80.0, data_type="BOOL")
            if "I0.4" not in self.forces:
                self.plc.write_address("I0.4", lvl <= 5.0, data_type="BOOL")

            sfc = self.plc.sfc_states.get("FB20:DB20") or self.plc.sfc_states.get("FB20") or {}
            active_steps = list(sfc.get("active_steps", []))
            prev_step = self.plant.get("active_step", "S0_IDLE")
            cur_step = active_steps[0] if active_steps else ("ABORTED" if self.plc.read_address("M0.7", "BOOL") else "S0_IDLE")
            if cur_step == "S4_COMPLETE" and prev_step != "S4_COMPLETE":
                self.plant["completed_batches"] = int(self.plant.get("completed_batches", 0)) + 1
                cur_db = int(self.plc.read_address("DB10.DBW8", "INT") or 0)
                self.plc.write_address("DB10.DBW8", cur_db + 1, data_type="INT")
            self.plant["active_step"] = cur_step
            self.plant["batch_done"] = q_done

    # ---- Force Table Overlay ----

    def _apply_forces(self, phase: str) -> None:
        """Apply persistent Force Values at PII (pre-scan) and PIQ/M/DB (post-scan) boundaries."""
        for addr, entry in self.forces.items():
            is_input = addr.startswith(("I", "PI"))
            if phase == "pre" and is_input:
                entry["unforced_value"] = self._safe_read(addr, entry["data_type"])
                self.plc.write_address(addr, entry["value"], data_type=entry["data_type"])
            elif phase == "post" and not is_input:
                entry["unforced_value"] = self._safe_read(addr, entry["data_type"])
                self.plc.write_address(addr, entry["value"], data_type=entry["data_type"])

    def set_force(self, address: str, value: Any, data_type: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            ca = _canon_addr(address)
            parsed = parse_s7_address(ca)
            if parsed is None:
                return {"ok": False, "error": f"Invalid Siemens S7 address '{address}'."}
            dtype = (data_type or parsed.data_type or "BOOL").upper()
            coerced = self._coerce_value(value, dtype)
            unforced = self._safe_read(ca, dtype)
            self.forces[ca] = {
                "address": ca,
                "value": coerced,
                "data_type": dtype,
                "unforced_value": unforced,
                "forced_at_scan": self.plc.scan_count,
            }
            self.plc.write_address(ca, coerced, data_type=dtype)
            self._log_diag("0x4541", "warning", 1, f"Force activated: {ca} = {coerced} ({dtype}). FRCE LED ON.")
            return {"ok": True, "force": self.forces[ca], "active_force_count": len(self.forces)}

    def remove_force(self, address: str) -> Dict[str, Any]:
        with self._lock:
            ca = _canon_addr(address)
            removed = self.forces.pop(ca, None)
            if removed is not None:
                self._log_diag("0x4542", "info", 1, f"Force released on {ca}. Active forces: {len(self.forces)}.")
            return {"ok": True, "removed": removed is not None, "active_force_count": len(self.forces)}

    def clear_all_forces(self) -> Dict[str, Any]:
        with self._lock:
            cnt = len(self.forces)
            self.forces.clear()
            if cnt:
                self._log_diag("0x4543", "info", 1, f"All {cnt} active force(s) cleared. FRCE LED OFF.")
            return {"ok": True, "cleared": cnt}

    # ---- Fault Injection & OB Simulation ----

    def set_fault(self, fault_id: str, active: bool) -> Dict[str, Any]:
        with self._lock:
            if fault_id not in self.faults:
                return {"ok": False, "error": f"Unknown fault '{fault_id}'."}
            prev = self.faults[fault_id]
            self.faults[fault_id] = bool(active)
            if bool(active) != prev:
                if fault_id == "estop_cut":
                    if active:
                        self.plc.write_address("I0.0", False, data_type="BOOL")
                        self._log_diag("0x3901", "error", 1, "E-Stop / Safety Relay opened (I0.0 = FALSE).")
                    else:
                        self.plc.write_address("I0.0", True, data_type="BOOL")
                        self._log_diag("0x3801", "info", 1, "E-Stop restored (I0.0 = TRUE). Manual safety reset required.")
                elif fault_id == "analog_wire_break":
                    self._log_diag(
                        "0x3942" if active else "0x3842",
                        "error" if active else "info",
                        82,
                        "OB82 Diagnostic Interrupt: Analog channel PIW256 wire-break (0x8000)." if active else "OB82 Outgoing: PIW256 wire-break cleared.",
                    )
                    if self.plant_type == "tank_pid":
                        self._sync_tank_sensor_to_plc()
                elif fault_id == "analog_overrange":
                    self._log_diag(
                        "0x3943" if active else "0x3843",
                        "warning" if active else "info",
                        82,
                        "OB82 Diagnostic Interrupt: Analog channel PIW256 overrange (0x7FFF)." if active else "OB82 Outgoing: PIW256 overrange cleared.",
                    )
                    if self.plant_type == "tank_pid":
                        self._sync_tank_sensor_to_plc()
                elif fault_id == "contactor_feedback_jam":
                    self._log_diag(
                        "0x3915" if active else "0x3815",
                        "warning" if active else "info",
                        1,
                        "Fault Injected: Contactor auxiliary feedback I0.5 stuck OPEN." if active else "Contactor auxiliary feedback fault cleared.",
                    )
                elif fault_id == "profinet_io_drop":
                    self._log_diag(
                        "0x39C4" if active else "0x38C4",
                        "error" if active else "info",
                        86,
                        "OB86 Rack/Station Failure: ET200M PROFINET IO station lost (BF LED ON)." if active else "OB86 Station Return: ET200M PROFINET IO recovered (BF LED OFF).",
                    )
                elif fault_id == "scan_load_spike":
                    self._log_diag(
                        "0x3501" if active else "0x3401",
                        "warning" if active else "info",
                        80,
                        "OB80 Cycle Time Warning: Heavy scan load injected." if active else "OB80 Cycle Time load returned to nominal.",
                    )
            return {"ok": True, "faults": dict(self.faults)}

    def trigger_brownout_warm_restart(self) -> Dict[str, Any]:
        """Simulate 24V DC power cycle + OB100 Warm Restart (drops Q/M/timers, preserves retentive DBs)."""
        with self._lock:
            # Preserve retentive DBs & loaded blocks/symbols
            saved_dbs = {k: bytearray(v) for k, v in self.plc.dbs.items()}
            self.plc.areas["Q"] = bytearray(len(self.plc.areas["Q"]))
            self.plc.areas["PQ"] = bytearray(len(self.plc.areas["PQ"]))
            self.plc.areas["M"] = bytearray(len(self.plc.areas["M"]))
            self.plc.timers.clear()
            self.plc.sfc_states.clear()
            self.plc.dbs = saved_dbs
            if self.plant_type == "conveyor_motor":
                self.plant["motor_rpm"] = 0.0
                self.plant["motor_current_a"] = 0.0
                self.plant["contactor_delay_ms"] = 0
            elif self.plant_type == "tank_pid":
                self.plant["inlet_valve_pct"] = 0.0
            elif self.plant_type == "batch_mixer_sfc":
                self.plant["agitator_rpm"] = 0.0
                self.plant["active_step"] = "S0_IDLE"
            self._log_diag(
                "0x4302",
                "warning",
                100,
                "24V DC Power Cycle -> OB100 Warm Restart: Non-retentive Q/PQ/M cleared; retentive DBs preserved.",
            )
            return {"ok": True, "ob_executed": "OB100"}

    # ---- CPU Mode Control & Single/Multi-Scan Stepping ----

    def control_cpu(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            act = (action or "").strip().lower()
            if act in ("run", "run_p"):
                self.cpu_mode = "RUN_P" if act == "run_p" else "RUN"
                self.plc.cpu_mode = "RUN"
                self.last_breakpoint_hit = None
                self._log_diag("0x4304", "info", 1, f"CPU mode switched to {self.cpu_mode}.")
                return {"ok": True, "cpu_mode": self.cpu_mode}
            if act == "stop":
                self.cpu_mode = "STOP"
                self.plc.cpu_mode = "STOP"
                self._log_diag("0x430E", "info", 1, "CPU switched to STOP mode by engineer.")
                return {"ok": True, "cpu_mode": self.cpu_mode}
            if act == "warm_restart":
                return self.trigger_brownout_warm_restart()
            if act in ("cold_reset", "mres"):
                preset = self.project_id
                if preset in SIEMENS_PRESETS:
                    self.load_preset(preset)
                else:
                    self.plc.reset(clear_blocks=False, clear_symbols=False)
                    self.forces.clear()
                    self.faults = {f["id"]: False for f in self.FAULT_DEFINITIONS}
                    self.scan_times_us.clear()
                    self.trace_buffer.clear()
                    self.network_coverage = {k: 0 for k in self.network_coverage}
                    self._init_plant_state()
                self._log_diag("0x4301", "warning", 102, "CPU MRES (Cold Reset) completed; memory & coverage cleared.")
                return {"ok": True, "cpu_mode": self.cpu_mode}
            if act == "set_scan_interval":
                ms = max(10, min(1000, int(kwargs.get("scan_interval_ms", 100))))
                self.scan_interval_ms = ms
                self.plc.ob35_interval_ms = ms
                return {"ok": True, "scan_interval_ms": ms}
            if act == "set_watchdog":
                wd = max(20.0, min(2000.0, float(kwargs.get("watchdog_limit_ms", 150.0))))
                self.watchdog_limit_ms = wd
                return {"ok": True, "watchdog_limit_ms": wd}
            if act == "set_disturbance":
                self.disturbance_load = max(0.0, min(60.0, float(kwargs.get("disturbance_load", 18.0))))
                return {"ok": True, "disturbance_load": self.disturbance_load}
            return {"ok": False, "error": f"Unknown CPU control action '{action}'."}

    def step(self, cycles: int = 1, tick_ms: Optional[int] = None, force_step: bool = False) -> Dict[str, Any]:
        with self._lock:
            dt = int(tick_ms if tick_ms is not None else self.scan_interval_ms)
            if self.cpu_mode in ("STOP", "FAULT_WATCHDOG") and not force_step:
                return {"ok": True, "executed_cycles": 0, "cpu_mode": self.cpu_mode, "warning": "CPU is stopped."}
            if force_step and self.cpu_mode in ("STOP", "PAUSED_BREAKPOINT"):
                self.plc.cpu_mode = "RUN"

            executed = 0
            hit_bp: Optional[Dict[str, Any]] = None
            for _ in range(max(1, min(int(cycles), 500))):
                bp = self._execute_single_scan(dt)
                executed += 1
                if bp is not None:
                    hit_bp = bp
                    self.cpu_mode = "PAUSED_BREAKPOINT"
                    self.last_breakpoint_hit = bp
                    self._log_diag("0x4510", "warning", 1, f"Breakpoint hit at scan #{self.plc.scan_count}: {bp['reason']}")
                    break
                if self.cpu_mode == "FAULT_WATCHDOG":
                    break

            if force_step and hit_bp is None and self.cpu_mode == "STOP":
                self.plc.cpu_mode = "STOP"

            return {
                "ok": True,
                "executed_cycles": executed,
                "scan_count": self.plc.scan_count,
                "sim_time_ms": self.plc.sim_time_ms,
                "cpu_mode": self.cpu_mode,
                "breakpoint_hit": hit_bp,
            }

    def _execute_single_scan(self, dt_ms: int) -> Optional[Dict[str, Any]]:
        t0 = time.perf_counter()
        self._apply_faults_pre_scan()
        self._apply_forces("pre")

        prev_mode = self.plc.cpu_mode
        self.plc.cpu_mode = "RUN"
        self.plc.step(cycles=1, tick_ms=dt_ms, entry_block="OB1")
        self.plc.cpu_mode = prev_mode

        self._apply_forces("post")
        self._update_plant_post_scan(dt_ms)

        elapsed_us = (time.perf_counter() - t0) * 1_000_000.0
        # Synthetic load injection when OB80 cycle-spike fault is active
        if self.faults.get("scan_load_spike"):
            elapsed_us = max(elapsed_us, self.watchdog_limit_ms * 880.0)  # ~88% of watchdog ms in us
        self.last_scan_us = round(elapsed_us, 1)
        self.scan_times_us.append(self.last_scan_us)
        if len(self.scan_times_us) > 200:
            self.scan_times_us = self.scan_times_us[-200:]

        if (elapsed_us / 1000.0) > self.watchdog_limit_ms:
            self.cpu_mode = "FAULT_WATCHDOG"
            self.plc.cpu_mode = "STOP"
            self._log_diag(
                "0x3501",
                "error",
                80,
                f"OB80 Watchdog Exceeded ({elapsed_us / 1000.0:.2f}ms > {self.watchdog_limit_ms:.1f}ms). CPU -> STOP.",
            )

        # Evaluate network power-flow & coverage
        evaluated = self._evaluate_networks_power_flow(update_coverage=True)

        # Sample 1-scan Trace Oscilloscope
        self._record_trace_sample()

        # Check network & conditional breakpoints
        for nw in evaluated:
            if nw["id"] in self.network_breakpoints and nw["energized"]:
                return {"type": "network", "target": nw["id"], "reason": f"Network {nw['id']} ({nw['title']}) energized"}
        for cb in self.condition_breakpoints:
            if self._check_condition_bp(cb):
                return {
                    "type": "condition",
                    "target": cb["id"],
                    "reason": f"{cb['address']} {cb['operator']} {cb['value']}",
                }
        return None

    # ---- Live Power-Flow & Coverage ----

    def _evaluate_networks_power_flow(self, update_coverage: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        sfc_active = set()
        for st in self.plc.sfc_states.values():
            sfc_active.update(st.get("active_steps", []))

        for nw in self.networks:
            nw_copy = {
                "id": nw["id"],
                "block": nw["block"],
                "index": nw["index"],
                "language": nw["language"],
                "title": nw["title"],
                "code_lines": nw["code_lines"],
                "has_breakpoint": nw["id"] in self.network_breakpoints,
                "elements": [],
                "energized": False,
                "coverage_scans": self.network_coverage.get(nw["id"], 0),
            }
            if nw["language"] == "GRAPH":
                sname = nw.get("step_name", "")
                active = sname in sfc_active
                nw_copy["energized"] = active
                for el in nw["elements"]:
                    e = dict(el)
                    e["energized"] = active if el["kind"] == "sfc_step" else False
                    nw_copy["elements"].append(e)
            else:
                rlo = True
                any_contact = False
                any_coil_on = False
                for el in nw["elements"]:
                    e = dict(el)
                    kind = el["kind"]
                    opnd = el.get("operand", "")
                    e["forced"] = opnd in self.forces if opnd else False
                    if kind == "contact":
                        val = bool(self._safe_read(opnd, "BOOL"))
                        passed = val if el["mode"] == "NO" else (not val)
                        if not any_contact:
                            rlo = passed
                            any_contact = True
                        elif el.get("logic") == "OR":
                            rlo = rlo or passed
                        else:
                            rlo = rlo and passed
                        e["value"] = val
                        e["energized"] = passed
                    elif kind == "coil":
                        val = bool(self._safe_read(opnd, "BOOL"))
                        e["value"] = val
                        e["energized"] = val
                        if val:
                            any_coil_on = True
                    elif kind == "timer":
                        tnum = int(re.sub(r"[^0-9]", "", opnd) or "0")
                        tstate = self.plc.timers.get(tnum)
                        if tstate:
                            elapsed = max(0, tstate.preset_ms - tstate.remaining_ms)
                            e["running"] = tstate.running
                            e["output"] = tstate.output
                            e["preset_ms"] = tstate.preset_ms
                            e["elapsed_ms"] = elapsed
                            e["energized"] = tstate.running or tstate.output
                            if e["energized"]:
                                any_coil_on = True
                        else:
                            e["running"] = False
                            e["output"] = False
                            e["preset_ms"] = 0
                            e["elapsed_ms"] = 0
                            e["energized"] = False
                    elif kind == "counter":
                        cnum = int(re.sub(r"[^0-9]", "", opnd) or "0")
                        cstate = self.plc.counters.get(cnum)
                        e["value"] = cstate.value if cstate else 0
                        e["energized"] = (cstate.value > 0) if cstate else False
                    elif kind == "call":
                        e["energized"] = self.cpu_mode in ("RUN", "RUN_P")
                        if e["energized"]:
                            any_coil_on = True
                    elif kind == "transfer":
                        e["value"] = self._safe_read(opnd, None)
                        e["energized"] = self.cpu_mode in ("RUN", "RUN_P")
                    else:
                        e["energized"] = rlo
                    nw_copy["elements"].append(e)
                nw_copy["energized"] = any_coil_on or (any_contact and rlo)

            if update_coverage and nw_copy["energized"]:
                self.network_coverage[nw["id"]] = self.network_coverage.get(nw["id"], 0) + 1
                nw_copy["coverage_scans"] = self.network_coverage[nw["id"]]
            out.append(nw_copy)
        return out

    # ---- Breakpoints ----

    def manage_breakpoints(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            act = (action or "").strip().lower()
            if act == "toggle_network":
                nwid = str(kwargs.get("network_id") or "")
                if nwid in self.network_breakpoints:
                    self.network_breakpoints.remove(nwid)
                elif nwid:
                    self.network_breakpoints.add(nwid)
                return {"ok": True, "network_breakpoints": sorted(self.network_breakpoints)}
            if act == "add_condition":
                addr = _canon_addr(str(kwargs.get("address") or ""))
                op = str(kwargs.get("operator") or "==").strip()
                val = kwargs.get("value")
                if not addr or op not in ("==", "!=", ">", "<", ">=", "<="):
                    return {"ok": False, "error": "Provide valid address and operator (==, !=, >, <, >=, <=)."}
                bp = {
                    "id": f"BP-{len(self.condition_breakpoints) + 1}-{addr}",
                    "address": addr,
                    "operator": op,
                    "value": val,
                    "data_type": kwargs.get("data_type"),
                }
                self.condition_breakpoints.append(bp)
                return {"ok": True, "condition_breakpoints": list(self.condition_breakpoints)}
            if act == "remove":
                bpid = str(kwargs.get("id") or "")
                self.condition_breakpoints = [b for b in self.condition_breakpoints if b["id"] != bpid]
                self.network_breakpoints.discard(bpid)
                return {"ok": True, "condition_breakpoints": list(self.condition_breakpoints)}
            if act == "clear_all":
                self.network_breakpoints.clear()
                self.condition_breakpoints.clear()
                self.last_breakpoint_hit = None
                return {"ok": True}
            return {"ok": False, "error": f"Unknown breakpoint action '{action}'."}

    def _check_condition_bp(self, bp: Dict[str, Any]) -> bool:
        cur = self._safe_read(bp["address"], bp.get("data_type"))
        target = bp["value"]
        op = bp["operator"]
        try:
            if isinstance(cur, bool) or str(target).lower() in ("true", "false"):
                c_b = bool(cur)
                t_b = target if isinstance(target, bool) else str(target).lower() in ("true", "1", "on")
                return (c_b == t_b) if op == "==" else (c_b != t_b)
            c_f = float(cur)
            t_f = float(target)
            return {
                "==": abs(c_f - t_f) < 1e-6,
                "!=": abs(c_f - t_f) >= 1e-6,
                ">": c_f > t_f,
                "<": c_f < t_f,
                ">=": c_f >= t_f,
                "<=": c_f <= t_f,
            }.get(op, False)
        except Exception:
            return False

    # ---- 1-Scan Trace Oscilloscope & PID Step-Response Metrics ----

    def _read_channel_numeric(self, ch: Dict[str, Any]) -> float:
        addr = ch["address"]
        if addr.startswith("PLANT."):
            key = addr.split(".", 1)[1]
            val = self.plant.get(key, 0.0)
            return float(val) if not isinstance(val, bool) else (1.0 if val else 0.0)
        val = self._safe_read(addr, ch.get("data_type"))
        if isinstance(val, bool):
            return 1.0 if val else 0.0
        try:
            return round(float(val), 4)
        except Exception:
            return 0.0

    def _record_trace_sample(self) -> None:
        sample: Dict[str, Any] = {
            "scan": self.plc.scan_count,
            "time_ms": self.plc.sim_time_ms,
            "values": {},
        }
        for ch in self.trace_channels:
            sample["values"][ch["address"]] = self._read_channel_numeric(ch)

        trig = self.trace_trigger
        if trig.get("armed") and not trig.get("triggered") and trig.get("mode") != "manual":
            t_ch = trig.get("channel") or (self.trace_channels[0]["address"] if self.trace_channels else "")
            cur_v = sample["values"].get(t_ch)
            if cur_v is not None:
                mode = trig["mode"]
                thr = float(trig.get("threshold", 0.5))
                fired = False
                if mode == "rising" and self._trace_prev_val is not None and self._trace_prev_val < 0.5 <= cur_v:
                    fired = True
                elif mode == "falling" and self._trace_prev_val is not None and self._trace_prev_val >= 0.5 > cur_v:
                    fired = True
                elif mode == "gt" and cur_v > thr:
                    fired = True
                elif mode == "lt" and cur_v < thr:
                    fired = True
                self._trace_prev_val = cur_v
                if fired:
                    trig["triggered"] = True
                    trig["armed"] = False
                    trig["trigger_scan"] = self.plc.scan_count
                    self.trace_buffer = self.trace_buffer[-20:]  # Keep 20 pre-trigger scans

        self.trace_buffer.append(sample)
        if len(self.trace_buffer) > self.trace_max_samples:
            self.trace_buffer = self.trace_buffer[-self.trace_max_samples :]

    def manage_trace(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            act = (action or "").strip().lower()
            if act == "clear":
                self.trace_buffer.clear()
                self.trace_trigger["triggered"] = False
                self.trace_trigger["trigger_scan"] = None
                self._trace_prev_val = None
                return {"ok": True}
            if act == "arm":
                self.trace_trigger["mode"] = str(kwargs.get("mode") or self.trace_trigger["mode"])
                self.trace_trigger["channel"] = str(kwargs.get("channel") or (self.trace_channels[0]["address"] if self.trace_channels else ""))
                if kwargs.get("threshold") is not None:
                    self.trace_trigger["threshold"] = float(kwargs["threshold"])
                self.trace_trigger["armed"] = True
                self.trace_trigger["triggered"] = False
                self.trace_trigger["trigger_scan"] = None
                self._trace_prev_val = None
                return {"ok": True, "trigger": dict(self.trace_trigger)}
            if act == "export_csv":
                buf = io.StringIO()
                cols = ["scan", "time_ms"] + [c["address"] for c in self.trace_channels]
                w = csv.writer(buf)
                w.writerow(cols)
                for row in self.trace_buffer:
                    w.writerow([row["scan"], row["time_ms"]] + [row["values"].get(c["address"], "") for c in self.trace_channels])
                return {"ok": True, "csv": buf.getvalue(), "row_count": len(self.trace_buffer)}
            return {"ok": False, "error": f"Unknown trace action '{action}'."}

    def _compute_pid_metrics(self) -> Optional[Dict[str, Any]]:
        if self.plant_type != "tank_pid" or len(self.trace_buffer) < 3:
            return None
        sps = [r["values"].get("MD14", 60.0) for r in self.trace_buffer]
        pvs = [r["values"].get("MD10", 0.0) for r in self.trace_buffer]
        ts = [r["time_ms"] for r in self.trace_buffer]
        sp = sps[-1]
        pv0 = pvs[0]
        pv_end = pvs[-1]
        delta = sp - pv0
        peak = max(pvs) if delta >= 0 else min(pvs)
        overshoot_pct = 0.0
        if abs(delta) > 1.0:
            overshoot_pct = max(0.0, ((peak - sp) / abs(delta)) * 100.0) if delta > 0 else max(0.0, ((sp - peak) / abs(delta)) * 100.0)
        rise_ms = None
        if abs(delta) > 1.0:
            target90 = pv0 + 0.9 * delta
            for t, pv in zip(ts, pvs):
                if (delta > 0 and pv >= target90) or (delta < 0 and pv <= target90):
                    rise_ms = t - ts[0]
                    break
        settling_ms = None
        band = max(1.5, abs(sp) * 0.03)
        for idx in range(len(pvs)):
            if all(abs(v - sp) <= band for v in pvs[idx:]):
                settling_ms = ts[idx] - ts[0]
                break
        return {
            "setpoint": round(sp, 2),
            "process_variable": round(pv_end, 2),
            "steady_state_error": round(sp - pv_end, 2),
            "overshoot_pct": round(overshoot_pct, 2),
            "rise_time_ms": rise_ms,
            "settling_time_ms": settling_ms,
        }

    # ---- VAT Write & Formatting ----

    def write_tag(self, address: str, value: Any, data_type: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            ca = _canon_addr(address)
            parsed = parse_s7_address(ca)
            if parsed is None:
                return {"ok": False, "error": f"Invalid Siemens S7 address '{address}'."}
            dtype = (data_type or parsed.data_type or "BOOL").upper()
            coerced = self._coerce_value(value, dtype)
            self.plc.write_address(ca, coerced, data_type=dtype)
            return {"ok": True, "address": ca, "value": coerced, "data_type": dtype}

    def manage_vat(self, action: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            act = (action or "").strip().lower()
            tab = str(kwargs.get("tab") or "Custom")
            if act == "add_tag":
                addr = _canon_addr(str(kwargs.get("address") or ""))
                parsed = parse_s7_address(addr)
                if parsed is None:
                    return {"ok": False, "error": f"Invalid S7 address '{kwargs.get('address')}'."}
                dtype = str(kwargs.get("data_type") or parsed.data_type or "BOOL").upper()
                fmt = str(kwargs.get("format") or ("BOOL" if dtype == "BOOL" else "REAL" if dtype == "REAL" else "DEC")).upper()
                rows = self.vat_tables.setdefault(tab, [])
                if not any(r["address"] == addr for r in rows):
                    rows.append(
                        {
                            "address": addr,
                            "symbol": str(kwargs.get("symbol") or ""),
                            "data_type": dtype,
                            "format": fmt,
                            "comment": str(kwargs.get("comment") or "Custom watch tag"),
                        }
                    )
                return {"ok": True, "tab": tab}
            if act == "remove_tag":
                addr = _canon_addr(str(kwargs.get("address") or ""))
                if tab in self.vat_tables:
                    self.vat_tables[tab] = [r for r in self.vat_tables[tab] if r["address"] != addr]
                return {"ok": True}
            if act == "set_format":
                addr = _canon_addr(str(kwargs.get("address") or ""))
                fmt = str(kwargs.get("format") or "DEC").upper()
                for rows in self.vat_tables.values():
                    for r in rows:
                        if r["address"] == addr:
                            r["format"] = fmt
                return {"ok": True}
            return {"ok": False, "error": f"Unknown VAT action '{action}'."}

    @staticmethod
    def _coerce_value(value: Any, dtype: str) -> Any:
        dt = dtype.upper()
        if dt == "BOOL":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "on", "yes")
        if dt == "REAL":
            return float(value)
        if isinstance(value, str) and value.strip().lower().startswith(("0x", "16#")):
            s = value.strip().lower().replace("16#", "0x")
            return int(s, 16)
        return int(float(value))

    def _safe_read(self, address: str, data_type: Optional[str]) -> Any:
        try:
            return self.plc.read_address(address, data_type=data_type)
        except Exception:
            return False if (data_type or "").upper() == "BOOL" else 0

    @staticmethod
    def _format_vat_value(val: Any, fmt: str, dtype: str) -> str:
        f = (fmt or dtype or "DEC").upper()
        if f == "BOOL" or dtype.upper() == "BOOL":
            return "TRUE (1)" if bool(val) else "FALSE (0)"
        if f == "REAL" or dtype.upper() == "REAL":
            try:
                return f"{float(val):.2f}"
            except Exception:
                return str(val)
        try:
            iv = int(val)
            if f == "HEX":
                return f"16#{iv & 0xFFFF:04X}"
            if f == "BIN":
                return f"2#{iv & 0xFF:08b}"
            if f in ("TIME", "S5TIME"):
                return f"T#{iv}ms"
            return str(iv)
        except Exception:
            return str(val)

    # ---- Isolated-Clone Automated FAT Runner & Certificate Exporter ----

    def run_fat_suite(self, test_id: Optional[str] = None) -> Dict[str, Any]:
        """Run one or all FAT cases in fresh isolated clones so manual workbench state stays clean."""
        cases_to_run = [c for c in self.fat_suite if (not test_id or c["id"] == test_id)]
        if not cases_to_run:
            return {"ok": False, "error": f"No FAT test cases matched '{test_id}'."}

        t0 = time.perf_counter()
        results: List[Dict[str, Any]] = []
        agg_coverage: Dict[str, int] = {nw["id"]: 0 for nw in self.networks}

        for case in cases_to_run:
            clone = self._spawn_isolated_clone()
            assertions: List[Dict[str, Any]] = []
            case_passed = True
            case_t0 = time.perf_counter()

            for step in case["steps"]:
                op = step["op"]
                if op == "write":
                    clone.write_tag(step["address"], step["value"], data_type=step.get("data_type"))
                elif op == "force":
                    clone.set_force(step["address"], step["value"], data_type=step.get("data_type"))
                elif op == "fault":
                    clone.set_fault(step["fault_id"], bool(step.get("active", True)))
                elif op == "brownout":
                    clone.trigger_brownout_warm_restart()
                elif op == "step":
                    clone.step(cycles=int(step.get("cycles", 1)))
                elif op == "assert":
                    actual = clone._safe_read(step["address"], step.get("data_type"))
                    expected = step["expect"]
                    ok = (bool(actual) == bool(expected)) if isinstance(expected, bool) else (actual == expected)
                    if not ok:
                        case_passed = False
                    assertions.append(
                        {
                            "label": step.get("label", f"{step['address']} == {expected}"),
                            "address": step["address"],
                            "expected": expected,
                            "actual": actual,
                            "passed": ok,
                        }
                    )
                elif op == "assert_range":
                    actual = float(clone._safe_read(step["address"], step.get("data_type") or "REAL"))
                    lo, hi = float(step["min"]), float(step["max"])
                    ok = lo <= actual <= hi
                    if not ok:
                        case_passed = False
                    assertions.append(
                        {
                            "label": step.get("label", f"{lo} <= {step['address']} <= {hi}"),
                            "address": step["address"],
                            "expected": f"{lo} .. {hi}",
                            "actual": round(actual, 2),
                            "passed": ok,
                        }
                    )
                elif op == "assert_watchdog":
                    stats = clone._scan_stats()
                    min_h = float(step.get("min_headroom_pct", 50.0))
                    ok = stats["headroom_pct"] >= min_h
                    if not ok:
                        case_passed = False
                    assertions.append(
                        {
                            "label": step.get("label", f"Watchdog headroom >= {min_h}%"),
                            "address": "OB1_CYCLE",
                            "expected": f">= {min_h:.1f}%",
                            "actual": f"{stats['headroom_pct']:.1f}% (max {stats['max_scan_ms']:.3f}ms)",
                            "passed": ok,
                        }
                    )

            for nwid, cnt in clone.network_coverage.items():
                agg_coverage[nwid] = agg_coverage.get(nwid, 0) + cnt

            results.append(
                {
                    "id": case["id"],
                    "name": case["name"],
                    "category": case["category"],
                    "description": case["description"],
                    "passed": case_passed,
                    "duration_ms": round((time.perf_counter() - case_t0) * 1000.0, 2),
                    "assertions": assertions,
                }
            )

        passed_count = sum(1 for r in results if r["passed"])
        total_nws = max(1, len(agg_coverage))
        covered_nws = sum(1 for v in agg_coverage.values() if v > 0)
        summary = {
            "ok": True,
            "executed": True,
            "executed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "project_name": self.project_name,
            "program_sha256": self.program_sha256,
            "passed": passed_count == len(results) and self.static_audit.get("passed", True),
            "passed_count": passed_count,
            "total_count": len(results),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "fat_network_coverage_pct": round((covered_nws / total_nws) * 100.0, 1),
            "cases": results,
        }
        with self._lock:
            if not test_id:
                self.fat_results = summary
            else:
                existing = {c["id"]: c for c in self.fat_results.get("cases", [])}
                for r in results:
                    existing[r["id"]] = r
                merged = [existing[c["id"]] for c in self.fat_suite if c["id"] in existing]
                self.fat_results = {
                    **summary,
                    "cases": merged,
                    "passed_count": sum(1 for r in merged if r["passed"]),
                    "total_count": len(merged),
                    "passed": all(r["passed"] for r in merged) and self.static_audit.get("passed", True),
                }
            self._log_diag(
                "0x4601",
                "info" if summary["passed"] else "error",
                1,
                f"FAT Suite completed: {passed_count}/{len(results)} passed (Coverage {summary['fat_network_coverage_pct']}%).",
            )
        return summary

    def _spawn_isolated_clone(self) -> "SiemensVerificationSession":
        if self.project_id in SIEMENS_PRESETS:
            return SiemensVerificationSession(initial_preset=self.project_id)
        clone = SiemensVerificationSession(initial_preset="conveyor_motor")
        combined = "\n\n".join(b["source"] for b in self.blocks)
        sym_json = json.dumps({k: v[0] for k, v in self.symbols.items()})
        clone.load_custom_program(name=self.project_name, source_code=combined, cpu_family=self.cpu_family, symbols_text=sym_json)
        return clone

    def export_fat_report(self, fmt: str = "markdown") -> Dict[str, Any]:
        with self._lock:
            if not self.fat_results.get("executed"):
                self.run_fat_suite()
            stats = self._scan_stats()
            covered = sum(1 for v in self.network_coverage.values() if v > 0)
            total_nw = max(1, len(self.network_coverage))
            cert = {
                "certificate_type": "SIEMENS_STEP7_FAT_SIGNOFF",
                "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "project_name": self.project_name,
                "cpu_family": self.cpu_family,
                "order_number": self.plc.order_number,
                "program_sha256": self.program_sha256,
                "blocks": [{"name": b["name"], "language": b["language"]} for b in self.blocks],
                "static_safety_audit": self.static_audit,
                "scan_watchdog": stats,
                "live_network_coverage_pct": round((covered / total_nw) * 100.0, 1),
                "fat_results": self.fat_results,
                "verdict": "APPROVED_FOR_COMMISSIONING" if (self.fat_results.get("passed") and self.static_audit.get("passed")) else "HOLD_DEFECTS_OPEN",
            }
            if fmt.lower() == "json":
                return {"ok": True, "format": "json", "content": json.dumps(cert, indent=2), "certificate": cert}

            lines = [
                f"# Siemens SIMATIC STEP 7 — Factory Acceptance Test (FAT) Sign-Off Certificate",
                "",
                f"- **Project**: {cert['project_name']}",
                f"- **Controller**: {cert['cpu_family']} (`{cert['order_number']}`)",
                f"- **Program SHA-256**: `{cert['program_sha256']}`",
                f"- **Generated (UTC)**: {cert['generated_at_utc']}",
                f"- **Verdict**: **{cert['verdict']}**",
                "",
                "## 1. Static Safety & Timing Summary",
                f"- **Static Audit**: {'PASS' if self.static_audit.get('passed') else 'FAIL'} ({self.static_audit.get('error_count', 0)} errors, {self.static_audit.get('warning_count', 0)} warnings)",
                f"- **OB1 Scan Time (Min / Avg / Max)**: {stats['min_scan_ms']:.3f} / {stats['avg_scan_ms']:.3f} / {stats['max_scan_ms']:.3f} ms",
                f"- **Watchdog Limit & Headroom**: {stats['watchdog_limit_ms']:.1f} ms ({stats['headroom_pct']:.1f}% headroom)",
                f"- **FAT Network Coverage**: {self.fat_results.get('fat_network_coverage_pct', 0.0)}%",
                "",
                "## 2. Automated FAT Test Matrix",
                "| Case ID | Test Case | Category | Result | Duration |",
                "| :--- | :--- | :--- | :--- | ---: |",
            ]
            for c in self.fat_results.get("cases", []):
                lines.append(f"| `{c['id']}` | {c['name']} | {c['category']} | **{'PASS' if c['passed'] else 'FAIL'}** | {c['duration_ms']} ms |")
            lines.append("")
            lines.append("## 3. Assertion Evidence")
            for c in self.fat_results.get("cases", []):
                lines.append(f"### {c['id']} — {c['name']}")
                for a in c.get("assertions", []):
                    lines.append(f"- [{'x' if a['passed'] else ' '}] **{a['label']}** (`{a['address']}`: expected `{a['expected']}`, actual `{a['actual']}`)")
                lines.append("")
            lines.extend(
                [
                    "## 4. Commissioning Engineer Sign-Off",
                    "- Lead Automation Engineer: ________________________  Date: ____________",
                    "- Client / Witness Representative: __________________  Date: ____________",
                ]
            )
            return {"ok": True, "format": "markdown", "content": "\n".join(lines), "certificate": cert}

    # ---- Snapshot Serialization ----

    def _scan_stats(self) -> Dict[str, Any]:
        if not self.scan_times_us:
            return {
                "scan_count": self.plc.scan_count,
                "ob35_count": self.plc.ob35_count,
                "sim_time_ms": self.plc.sim_time_ms,
                "scan_interval_ms": self.scan_interval_ms,
                "last_scan_ms": 0.0,
                "min_scan_ms": 0.0,
                "avg_scan_ms": 0.0,
                "max_scan_ms": 0.0,
                "watchdog_limit_ms": self.watchdog_limit_ms,
                "headroom_pct": 100.0,
            }
        mn = min(self.scan_times_us) / 1000.0
        av = (sum(self.scan_times_us) / len(self.scan_times_us)) / 1000.0
        mx = max(self.scan_times_us) / 1000.0
        headroom = max(0.0, min(100.0, (1.0 - (mx / max(1.0, self.watchdog_limit_ms))) * 100.0))
        return {
            "scan_count": self.plc.scan_count,
            "ob35_count": self.plc.ob35_count,
            "sim_time_ms": self.plc.sim_time_ms,
            "scan_interval_ms": self.scan_interval_ms,
            "last_scan_ms": round(self.last_scan_us / 1000.0, 3),
            "min_scan_ms": round(mn, 3),
            "avg_scan_ms": round(av, 3),
            "max_scan_ms": round(mx, 3),
            "watchdog_limit_ms": self.watchdog_limit_ms,
            "headroom_pct": round(headroom, 1),
        }

    def get_state_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            evaluated_nws = self._evaluate_networks_power_flow(update_coverage=False)
            covered = sum(1 for v in self.network_coverage.values() if v > 0)
            total_nw = max(1, len(self.network_coverage))

            vat_live: Dict[str, List[Dict[str, Any]]] = {}
            for tab, rows in self.vat_tables.items():
                live_rows = []
                for r in rows:
                    addr = r["address"]
                    dtype = r["data_type"]
                    raw_val = self._safe_read(addr, dtype)
                    force_info = self.forces.get(addr)
                    live_rows.append(
                        {
                            **r,
                            "value": raw_val,
                            "formatted": self._format_vat_value(raw_val, r.get("format", dtype), dtype),
                            "forced": force_info is not None,
                            "unforced_value": force_info["unforced_value"] if force_info else raw_val,
                        }
                    )
                vat_live[tab] = live_rows

            return {
                "ok": True,
                "project": {
                    "id": self.project_id,
                    "name": self.project_name,
                    "cpu_family": self.cpu_family,
                    "order_number": self.plc.order_number,
                    "plant_type": self.plant_type,
                    "sha256": self.program_sha256,
                    "blocks": self.blocks,
                    "coverage_pct": round((covered / total_nw) * 100.0, 1),
                    "covered_networks": covered,
                    "total_networks": len(self.networks),
                },
                "cpu": {
                    "mode": self.cpu_mode,
                    "leds": self._compute_cpu_leds(),
                    "stats": self._scan_stats(),
                    "timers": {
                        f"T{num}": {
                            "mode": t.mode,
                            "preset_ms": t.preset_ms,
                            "elapsed_ms": max(0, t.preset_ms - t.remaining_ms),
                            "remaining_ms": t.remaining_ms,
                            "running": t.running,
                            "output": t.output,
                        }
                        for num, t in sorted(self.plc.timers.items())
                    },
                    "counters": {f"C{num}": c.value for num, c in sorted(self.plc.counters.items())},
                },
                "networks": evaluated_nws,
                "vat_tables": vat_live,
                "forces": list(self.forces.values()),
                "faults": self.faults,
                "fault_catalog": self.FAULT_DEFINITIONS,
                "diagnostic_buffer": self.diagnostic_buffer[:60],
                "breakpoints": {
                    "networks": sorted(self.network_breakpoints),
                    "conditions": list(self.condition_breakpoints),
                    "last_hit": self.last_breakpoint_hit,
                },
                "trace": {
                    "channels": self.trace_channels,
                    "trigger": dict(self.trace_trigger),
                    "samples": self.trace_buffer[-200:],
                    "total_samples": len(self.trace_buffer),
                    "pid_metrics": self._compute_pid_metrics(),
                },
                "plant": dict(self.plant),
                "static_audit": self.static_audit,
                "cross_references": list(self.cross_references.values()),
                "fat_suite": [
                    {k: v for k, v in c.items() if k != "steps"}
                    for c in self.fat_suite
                ],
                "fat_results": self.fat_results,
                "presets": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "cpu_family": p["cpu_family"],
                        "order_number": p["order_number"],
                        "summary": p["summary"],
                        "plant_type": p["plant_type"],
                    }
                    for p in SIEMENS_PRESETS.values()
                ],
                "bridge": {
                    "platform": sys.platform,
                    "virtual_plc_ready": True,
                    "plcsim_com_available": sys.platform == "win32",
                    "s7comm_tcp_ready": True,
                },
            }

    # ---- Live Bridge Dispatch (S7-PLCSIM COM & Physical PLC Diagnostics) ----

    def invoke_plcsim_bridge(self, action: str, run_mode: str = "RUN_P", scan_mode: Optional[str] = None) -> Dict[str, Any]:
        try:
            import simatic_s7_mcp_server as srv

            return srv.runtime_client.com_bridge.plcsim_trigger_simulation(
                action=action,
                run_mode=run_mode,
                scan_mode=scan_mode,
                auto_connect=True,
                disconnect_on_stop=False,
            )
        except Exception as exc:
            return {
                "success": False,
                "ok": False,
                "error": f"S7-PLCSIM bridge call failed: {exc}",
                "com_progid": "S7wspsmx.S7ProSim.1",
                "com_architecture": "32-bit",
            }

    def invoke_physical_plc_diagnose(self, host: str, port: int = 102, rack: int = 0, slot: int = 2) -> Dict[str, Any]:
        try:
            import simatic_s7_mcp_server as srv

            raw = srv.s7_plc_diagnose_connectivity(
                host=host,
                port=int(port),
                rack=int(rack),
                slot=int(slot),
                probe_count=2,
                timeout_s=2.0,
            )
            return json.loads(raw)
        except Exception as exc:
            return {"success": False, "ok": False, "error": f"Physical PLC diagnostic failed: {exc}"}


# ---------------------------------------------------------------------------
# Persistent JSON-RPC Daemon Loop (stdin/stdout) for Node.js API Gateway
# ---------------------------------------------------------------------------

def _handle_rpc(session: SiemensVerificationSession, req: Dict[str, Any]) -> Dict[str, Any]:
    cmd = str(req.get("cmd") or "").strip().lower()
    if cmd == "ping":
        return {"ok": True, "service": "siemens_verification_studio", "pid": os.getpid() if hasattr(sys, "platform") else 0}
    if cmd == "get_state":
        return session.get_state_snapshot()
    if cmd == "list_presets":
        return {"ok": True, "presets": session.get_state_snapshot()["presets"]}
    if cmd == "load":
        if req.get("preset_id"):
            res = session.load_preset(str(req["preset_id"]))
        else:
            res = session.load_custom_program(
                name=str(req.get("name") or "Custom AI STEP 7 Program"),
                source_code=str(req.get("source_code") or ""),
                cpu_family=str(req.get("cpu_family") or "S7-300"),
                symbols_text=str(req.get("symbols") or ""),
                block_name=str(req.get("block_name") or "OB1"),
                language=req.get("language"),
            )
        if res.get("ok"):
            return {**res, "state": session.get_state_snapshot()}
        return res
    if cmd == "control":
        res = session.control_cpu(str(req.get("action") or ""), **req)
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "step":
        res = session.step(
            cycles=int(req.get("cycles", 1)),
            tick_ms=req.get("tick_ms"),
            force_step=bool(req.get("force_step", False)),
        )
        return {**res, "state": session.get_state_snapshot()}
    if cmd == "write":
        res = session.write_tag(str(req.get("address") or ""), req.get("value"), data_type=req.get("data_type"))
        if res.get("ok"):
            # Refresh immediate power flow view without advancing scan count if stopped
            return {**res, "state": session.get_state_snapshot()}
        return res
    if cmd == "force":
        act = str(req.get("action") or "set").lower()
        if act == "set":
            res = session.set_force(str(req.get("address") or ""), req.get("value"), data_type=req.get("data_type"))
        elif act == "remove":
            res = session.remove_force(str(req.get("address") or ""))
        elif act == "clear_all":
            res = session.clear_all_forces()
        else:
            return {"ok": False, "error": f"Unknown force action '{act}'."}
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "fault":
        act = str(req.get("action") or "").lower()
        if act == "brownout_warm_restart":
            res = session.trigger_brownout_warm_restart()
        elif act == "clear_all":
            for fid in list(session.faults.keys()):
                session.set_fault(fid, False)
            res = {"ok": True}
        else:
            res = session.set_fault(str(req.get("fault_id") or ""), bool(req.get("active", False)))
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "breakpoints":
        res = session.manage_breakpoints(str(req.get("action") or ""), **req)
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "trace":
        res = session.manage_trace(str(req.get("action") or ""), **req)
        if req.get("action") == "export_csv":
            return res
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "vat":
        res = session.manage_vat(str(req.get("action") or ""), **req)
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "fat_run":
        res = session.run_fat_suite(test_id=req.get("test_id"))
        return {**res, "state": session.get_state_snapshot()} if res.get("ok") else res
    if cmd == "fat_report":
        return session.export_fat_report(fmt=str(req.get("format") or "markdown"))
    if cmd == "bridge_plcsim":
        return session.invoke_plcsim_bridge(
            action=str(req.get("action") or "start"),
            run_mode=str(req.get("run_mode") or "RUN_P"),
            scan_mode=req.get("scan_mode"),
        )
    if cmd == "bridge_diagnose":
        return session.invoke_physical_plc_diagnose(
            host=str(req.get("host") or "127.0.0.1"),
            port=int(req.get("port", 102)),
            rack=int(req.get("rack", 0)),
            slot=int(req.get("slot", 2)),
        )
    return {"ok": False, "error": f"Unknown verification daemon command '{cmd}'."}


def _run_daemon() -> None:
    session = SiemensVerificationSession(initial_preset="conveyor_motor")
    sys.stdout.write(json.dumps({"ok": True, "ready": True, "pid": os.getpid()}) + "\n")
    sys.stdout.flush()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = _handle_rpc(session, req)
        except Exception as exc:
            resp = {"ok": False, "error": f"Daemon error: {exc}"}
        sys.stdout.write(json.dumps(resp, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        _run_daemon()
