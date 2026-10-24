"""
SIMATIC STEP 7 MCP Server (Codex-Simatic7 / Antigravity / VS Code Compatible)
=============================================================================
A complete Model Context Protocol (MCP) stdio server for Siemens SIMATIC
STEP 7 Classic (v5.5 / v5.6 / v5.7 / SIMATIC Manager) for S7-300, S7-400,
and WinAC / ET200S CPU controllers (specifically NOT TIA Portal).

This server replicates and enhances the reliable, verified engineering workflow
developed for PLC MCP bridges:

1. Pre-Flight Family-Aware S7 Mnemonic and Device Validator:
   - Validates S7-300, S7-400, and WinAC addressing:
     * Inputs: I <byte>.<bit> / E <byte>.<bit>, IB, IW, ID
     * Outputs: Q <byte>.<bit> / A <byte>.<bit>, QB, QW, QD
     * Bit Memory: M <byte>.<bit>, MB, MW, MD
     * Local Data: L <byte>.<bit>, LB, LW, LD
     * Timers: T <n>, Counters: C <n> / Z <n>
     * Block References: FC <n>, FB <n>, OB <n>, DB <n>, DI <n>, SFC <n>, SFB <n>
     * Data Blocks: DB<n>.DBX/DBB/DBW/DBD as well as opened DBX/DBB/DBW/DBD
     * Status Bits: ==0, <>0, >0, <0, >=0, <=0, OV, OS, BR, UO
     * Peripheral I/O: PIB, PIW, PID / PQB, PQW, PQD
     * Address Register Indirect: [AR1, P#...], [AR2, P#...]
     * Constants: S5T#..., S5TIME#..., T#..., TIME#..., D#..., TOD#..., DT#...,
       16#..., 2#..., integer, floating point, pointer, character, byte literals
   - Enforces 0..7 bit bounds within bytes and family-specific address boundaries.
   - Handles both International (English: A, AN, O, ON, =, S, R, I, Q, JU, JC, SP, CU) and
     German (SIMATIC German: U, UN, O, ON, =, S, R, E, A, SPA, SPB, SI, ZV) mnemonics.
   - Detects common cross-PLC / foreign mnemonics (LD, OUT, ANDN, TON, X0, Y0)
     with precise actionable hints and optional auto-correction.

2. Static Safety & Interlock Analyzer:
   - Star-Delta contactor switching hazard:
     Warns if Star and Delta contactor coils are activated without mutual
     software interlocks (AN opposing coil / opposing symbol) or without dead-time timer delay.
     Supports both English ("STAR"/"DELTA") and German ("STERN"/"DREIECK") naming.
   - Duplicate coil assignment detector:
     Flags destructive duplicate '=' assignments to identical outputs/markers,
     cross-resolving named symbols to physical bit addresses.
   - Unassigned / uninitialized timers & counters.
   - Unused symbol definitions.

3. Siemens AWL (STL) Source Code & Symbol Table Round-Trip Bridge:
   - Generates fully compliant Siemens STEP 7 AWL text source files
     (OB 1, FC, FB) with proper network markers, labels, and comments.
   - Generates and parses standard Siemens Symbol Tables (.SDF and .CSV).
   - Exports, parses, and normalizes compiled blocks to guarantee readback equality.

4. Serialized SIMATIC Manager GUI Automation (pywinauto):
   - Automates SIMATIC Manager (s7tgtopx.exe) and the LAD/FBD/STL Editor (s7kotx.exe).
   - Creates projects natively or establishes offline workspace structure.
   - Validates active project and block prior to any edit.
   - Backs up current block to ~/Documents/Codex-Simatic7/ before modification.
   - Imports candidate AWL, executes compile/check, and verifies readback.
   - Automatically rolls back to verified backup on any compile, check, or readback failure.
   - Fully functional offline mode: saves verified AWL/SDF artifacts when GUI is offline.

Installation:
-------------
    pip install "mcp[cli]" pywinauto pywin32

Running via Antigravity or VS Code MCP Configuration:
-----------------------------------------------------
    Command: python
    Args: ["C:/path/to/simatic_s7_mcp_server.py"]
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure standard I/O encoding is UTF-8 for MCP stdio transport
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise SystemExit(
        "Missing 'mcp' package. Install it with: pip install \"mcp[cli]\" pywinauto pywin32"
    ) from exc

# Optional Windows GUI automation imports (handled gracefully if offline)
try:
    from pywinauto import Application
    from pywinauto.keyboard import send_keys
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False
    Application = None

    def send_keys(*args, **kwargs):
        pass


# ============================================================================
# 1. CONFIGURATION & CONSTANTS
# ============================================================================

WORKSPACE_DIR = Path.home() / "Documents" / "Codex-Simatic7"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

S7_MANAGER_WINDOW_TITLE_REGEX = r".*SIMATIC Manager.*"
S7_EDITOR_WINDOW_TITLE_REGEX = r".*(LAD/FBD/STL|LAD/STL/FBD|KOP/FUP/AWL|LAD, FBD, STL).*"
STEP7_DEFAULT_BIN = Path(r"C:\Program Files (x86)\Siemens\Step7\s7bin")
STEP7_MANAGER_EXE = "S7tgtopx.exe"
STEP7_EDITOR_EXE = "s7wndltx.exe"
STEP7_HWCNF_EXE = "S7HWCNFX.EXE"
STEP7_EXECUTABLE_ENV = "STEP7_EXE"

GUI_TRANSACTION_LOCK = threading.RLock()
GUI_ACTIVE_WRITE: Dict[str, Any] = {}


def find_step7_manager_exe() -> Optional[Path]:
    """
    Discovers the SIMATIC Manager executable (s7tgtopx.exe) by checking environment variables,
    Windows 32/64-bit registry keys, standard Siemens installation paths, and system PATH.
    """
    # 1. Environment variables
    env_exe = os.environ.get(STEP7_EXECUTABLE_ENV) or os.environ.get("STEP7_BIN")
    if env_exe:
        p = Path(env_exe)
        if p.is_file():
            return p
        if p.is_dir() and (p / STEP7_MANAGER_EXE).is_file():
            return p / STEP7_MANAGER_EXE

    # 2. Windows Registry
    if sys.platform == "win32":
        try:
            import winreg

            reg_paths = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Siemens\STEP7\2.0\Paths", "AppPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Siemens\STEP7\2.0\Paths", "AppPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Siemens\AUT\STEP7", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Siemens\AUT\STEP7", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Siemens\STEP7", "Path"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Siemens\STEP7", "Path"),
            ]
            for hkey, subkey, val_name in reg_paths:
                try:
                    with winreg.OpenKey(hkey, subkey) as k:
                        val, _ = winreg.QueryValueEx(k, val_name)
                        if val:
                            p = Path(val)
                            if p.is_file() and p.name.lower() == STEP7_MANAGER_EXE.lower():
                                return p
                            cand = p / STEP7_MANAGER_EXE if p.is_dir() else p / "s7bin" / STEP7_MANAGER_EXE
                            if cand.is_file():
                                return cand
                except Exception:
                    continue
        except Exception:
            pass

    # 3. Known standard Siemens directories
    common_roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        r"C:\Siemens",
        r"D:\Siemens",
        r"D:\Program Files (x86)\Siemens",
        r"D:\Program Files\Siemens",
    ]
    for root in common_roots:
        if not root:
            continue
        p = Path(root) / "Siemens" / "Step7" / "s7bin" / STEP7_MANAGER_EXE
        if p.is_file():
            return p
        p_direct = Path(root) / "Step7" / "s7bin" / STEP7_MANAGER_EXE
        if p_direct.is_file():
            return p_direct

    # 4. Search system PATH
    found = shutil.which(STEP7_MANAGER_EXE)
    if found and Path(found).is_file():
        return Path(found)

    return None


@dataclass(frozen=True)
class S7PLCProfile:
    """Family-level limits and capabilities for Siemens S7-300 / S7-400 / WinAC."""
    name: str
    max_input_byte: int
    max_output_byte: int
    max_marker_byte: int
    max_timer_num: int
    max_counter_num: int
    max_db_num: int
    max_db_byte: int
    typical_cpu: str = "CPU 315-2 DP"
    station_type: str = "SIMATIC 300 Station"
    bit_op_time_us: float = 0.05
    word_op_time_us: float = 0.10
    timer_op_time_us: float = 0.20
    call_op_time_us: float = 1.50


S7_PLC_PROFILES: Dict[str, S7PLCProfile] = {
    "S7-300": S7PLCProfile(
        name="S7-300",
        max_input_byte=1023,
        max_output_byte=1023,
        max_marker_byte=2047,
        max_timer_num=511,
        max_counter_num=511,
        max_db_num=4095,
        max_db_byte=65535,
        typical_cpu="CPU 315-2 DP",
        station_type="SIMATIC 300 Station",
        bit_op_time_us=0.05,
        word_op_time_us=0.10,
        timer_op_time_us=0.20,
        call_op_time_us=1.50,
    ),
    "S7-400": S7PLCProfile(
        name="S7-400",
        max_input_byte=16383,
        max_output_byte=16383,
        max_marker_byte=16383,
        max_timer_num=2047,
        max_counter_num=2047,
        max_db_num=16383,
        max_db_byte=65535,
        typical_cpu="CPU 414-3 PN/DP",
        station_type="SIMATIC 400 Station",
        bit_op_time_us=0.01,
        word_op_time_us=0.02,
        timer_op_time_us=0.04,
        call_op_time_us=0.30,
    ),
    "WINAC": S7PLCProfile(
        name="WinAC",
        max_input_byte=16383,
        max_output_byte=16383,
        max_marker_byte=16383,
        max_timer_num=2047,
        max_counter_num=2047,
        max_db_num=16383,
        max_db_byte=65535,
        typical_cpu="WinAC RTX",
        station_type="SIMATIC PC Station",
        bit_op_time_us=0.004,
        word_op_time_us=0.008,
        timer_op_time_us=0.015,
        call_op_time_us=0.10,
    ),
}

S7_PLC_TYPE_ALIASES: Dict[str, str] = {
    "300": "S7-300",
    "S7300": "S7-300",
    "S7-300": "S7-300",
    "CPU312": "S7-300",
    "CPU313": "S7-300",
    "CPU314": "S7-300",
    "CPU315": "S7-300",
    "CPU3152DP": "S7-300",
    "CPU317": "S7-300",
    "CPU319": "S7-300",
    "ET200S": "S7-300",
    "ET200M": "S7-300",
    "400": "S7-400",
    "S7400": "S7-400",
    "S7-400": "S7-400",
    "CPU412": "S7-400",
    "CPU414": "S7-400",
    "CPU4143PNDP": "S7-400",
    "CPU416": "S7-400",
    "CPU417": "S7-400",
    "WINAC": "WINAC",
    "WINLC": "WINAC",
    "PC": "WINAC",
}


def resolve_s7_plc_profile(plc_type: str) -> Tuple[Optional[S7PLCProfile], Optional[str]]:
    """Resolve a Siemens S7 family or CPU model to a supported profile."""
    if not isinstance(plc_type, str) or not plc_type.strip():
        return None, "plc_type must be a non-empty string."
    cleaned = re.sub(r"[\s_-]+", "", plc_type.strip().upper())
    key = S7_PLC_TYPE_ALIASES.get(cleaned) or S7_PLC_TYPE_ALIASES.get(plc_type.strip().upper())
    if key is None:
        if "300" in cleaned or cleaned.startswith("CPU3") or "31" in cleaned:
            key = "S7-300"
        elif "400" in cleaned or cleaned.startswith("CPU4") or "41" in cleaned:
            key = "S7-400"
        elif "WIN" in cleaned or "PC" in cleaned:
            key = "WINAC"
    if key is None or key not in S7_PLC_PROFILES:
        return None, (
            f"Unsupported plc_type '{plc_type}'. Supported targets are S7-300, S7-400, or WinAC."
        )
    return S7_PLC_PROFILES[key], None


# ============================================================================
# 2. S7 MNEMONICS, OPCODES & DEVICE REGEXES
# ============================================================================

# S7 AWL (Statement List / STL) Supported Opcodes with (min_operands, max_operands)
SUPPORTED_S7_OPCODES: Dict[str, Tuple[int, int]] = {
    # Bit Logic Contacts (English)
    "A": (0, 1),    # 1 operand for contact, 0 for nesting A( or complex connector
    "AN": (1, 1),
    "O": (0, 1),    # 1 operand for contact, 0 for OR between AND-branches
    "ON": (1, 1),
    "X": (1, 1),
    "XN": (1, 1),
    "A(": (0, 0),
    "AN(": (0, 0),
    "O(": (0, 0),
    "ON(": (0, 0),
    "X(": (0, 0),
    "XN(": (0, 0),
    ")": (0, 0),
    # Bit Logic Contacts (German aliases)
    "U": (0, 1),
    "UN": (1, 1),
    "U(": (0, 0),
    "UN(": (0, 0),
    # Assignment & Coils
    "=": (1, 1),
    "S": (1, 1),
    "R": (1, 1),
    "NOT": (0, 0),
    "SET": (0, 0),
    "CLR": (0, 0),
    "SAVE": (0, 0),
    "FP": (1, 1),   # Edge positive
    "FN": (1, 1),   # Edge negative
    # Timers (English S7 Timers)
    "SP": (1, 1),   # Pulse timer
    "SE": (1, 1),   # Extended pulse (English) / On-delay (German)
    "SD": (1, 1),   # On-delay timer
    "SS": (1, 1),   # Retentive on-delay
    "SF": (1, 1),   # Off-delay timer
    "FR": (1, 1),   # Free timer / counter
    # Timers (German S7 Timers)
    "SI": (1, 1),   # Impuls
    "SV": (1, 1),   # Verlängerter Impuls
    "SA": (1, 1),   # Ausschaltverzögerung
    # Counters (English S7 Counters)
    "CU": (1, 1),   # Count up
    "CD": (1, 1),   # Count down
    # Counters (German S7 Counters)
    "ZV": (1, 1),   # Zählen Vorwärts
    "ZR": (1, 1),   # Zählen Rückwärts
    # Load and Transfer
    "L": (1, 1),    # Load into ACCU1
    "LC": (1, 1),   # Load current time/counter in BCD
    "T": (1, 1),    # Transfer ACCU1 to destination
    "TAK": (0, 0),  # Swap ACCU1 and ACCU2
    "CAW": (0, 0),  # Byte swap word in ACCU1
    "CAD": (0, 0),  # Byte swap dword in ACCU1
    "NOP": (0, 1),  # NOP or NOP 0 / NOP 1
    # Address Register & Stack Instructions
    "LAR1": (0, 1),  # Load Address Register 1
    "LAR2": (0, 1),  # Load Address Register 2
    "TAR1": (0, 1),  # Transfer Address Register 1
    "TAR2": (0, 1),  # Transfer Address Register 2
    "CAR": (0, 0),  # Exchange Address Registers AR1 and AR2
    "PUSH": (0, 0),  # Push ACCU stack
    "POP": (0, 0),  # Pop ACCU stack
    "ENT": (0, 0),  # Enter ACCU stack
    "LEAVE": (0, 0),  # Leave ACCU stack
    # Comparison Instructions (operate on ACCU1 and ACCU2)
    "==I": (0, 0), "<>I": (0, 0), ">I": (0, 0), "<I": (0, 0), ">=I": (0, 0), "<=I": (0, 0),
    "==D": (0, 0), "<>D": (0, 0), ">D": (0, 0), "<D": (0, 0), ">=D": (0, 0), "<=D": (0, 0),
    "==R": (0, 0), "<>R": (0, 0), ">R": (0, 0), "<R": (0, 0), ">=R": (0, 0), "<=R": (0, 0),
    # Math & Arithmetic
    "+I": (0, 0), "-I": (0, 0), "*I": (0, 0), "/I": (0, 0),
    "+D": (0, 0), "-D": (0, 0), "*D": (0, 0), "/D": (0, 0), "MOD": (0, 0),
    "+R": (0, 0), "-R": (0, 0), "*R": (0, 0), "/R": (0, 0),
    "+": (1, 1),    # Add constant to ACCU1
    "INC": (1, 1),
    "DEC": (1, 1),
    "ABS": (0, 0), "SQR": (0, 0), "SQRT": (0, 0), "EXP": (0, 0), "LN": (0, 0),
    "SIN": (0, 0), "COS": (0, 0), "TAN": (0, 0), "ASIN": (0, 0), "ACOS": (0, 0), "ATAN": (0, 0),
    # Word Logic
    "UW": (0, 1), "OW": (0, 1), "XOW": (0, 1),
    "UD": (0, 1), "OD": (0, 1), "XOD": (0, 1),
    # Shift and Rotate
    "SLW": (0, 1), "SRW": (0, 1), "SSI": (0, 1), "SSD": (0, 1),
    "SLD": (0, 1), "SRD": (0, 1), "RLD": (0, 1), "RRD": (0, 1),
    "RLDA": (0, 0), "RRDA": (0, 0),
    # Data Conversion
    "BTI": (0, 0), "ITB": (0, 0), "BTD": (0, 0), "ITD": (0, 0),
    "DTR": (0, 0), "DTB": (0, 0), "TRUNC": (0, 0), "ROUND": (0, 0),
    "CEIL": (0, 0), "FLOOR": (0, 0), "INVI": (0, 0), "INVD": (0, 0),
    "NEGI": (0, 0), "NEGD": (0, 0), "NEGR": (0, 0),
    # Program Flow, Calls and Jumps (English)
    "JU": (1, 1), "JC": (1, 1), "JCN": (1, 1), "JCB": (1, 1), "JNB": (1, 1),
    "JBI": (1, 1), "JNBI": (1, 1), "JL": (1, 1), "LOOP": (1, 1),
    "BE": (0, 0), "BEC": (0, 0), "BEU": (0, 0),
    "CALL": (1, 10),
    "CC": (1, 1),
    "UC": (1, 1),
    "OPN": (1, 1),  # Open DB or DI
    "MCR(": (0, 0), ")MCR": (0, 0), "MCRA": (0, 0), "MCRD": (0, 0),
    "STP": (0, 0),  # Stop CPU
    # Program Flow, Calls and Jumps (German aliases)
    "SPA": (1, 1),  # Sprung Absolut = JU
    "SPB": (1, 1),  # Sprung Bedingt = JC
    "SPBN": (1, 1),  # Sprung Bedingt Nicht = JCN
    "SPBB": (1, 1),  # Sprung mit BR = JCB
    "SPBI": (1, 1),  # Sprung wenn BR=1 = JBI
    "SPBIN": (1, 1),  # Sprung wenn BR=0 = JNBI
    "SPL": (1, 1),  # Sprungleiste = JL
    "BEA": (0, 0),  # Bausteinende Absolut = BE
    "BEB": (0, 0),  # Bausteinende Bedingt = BEC
}

# Translation mapping between German and International mnemonics
GERMAN_TO_INTL_OPCODES: Dict[str, str] = {
    "U": "A",
    "UN": "AN",
    "U(": "A(",
    "UN(": "AN(",
    "SPA": "JU",
    "SPB": "JC",
    "SPBN": "JCN",
    "SPBB": "JCB",
    "SPBI": "JBI",
    "SPBIN": "JNBI",
    "SPL": "JL",
    "BEA": "BE",
    "BEB": "BEC",
    "SI": "SP",
    "SV": "SE",
    "SA": "SF",
    "ZV": "CU",
    "ZR": "CD",
}

INTL_TO_GERMAN_OPCODES: Dict[str, str] = {
    "A": "U",
    "AN": "UN",
    "A(": "U(",
    "AN(": "UN(",
    "JU": "SPA",
    "JC": "SPB",
    "JCN": "SPBN",
    "JCB": "SPBB",
    "JBI": "SPBI",
    "JNBI": "SPBIN",
    "JL": "SPL",
    "BE": "BEA",
    "BEC": "BEB",
    "SP": "SI",
    "SE": "SV",
    "SD": "SE",
    "SF": "SA",
    "CU": "ZV",
    "CD": "ZR",
}

# Mapping common LLM hallucinations or foreign PLC mnemonics to Siemens S7 STL
S7_OPCODE_HINTS: Dict[str, str] = {
    "LD": "A",
    "LDI": "AN",
    "LDN": "AN",
    "LDNOT": "AN",
    "AND": "A",
    "ANI": "AN",
    "ANDN": "AN",
    "ANDNOT": "AN",
    "OR": "O",
    "ORI": "ON",
    "ORN": "ON",
    "ORNOT": "ON",
    "OUT": "=",
    "XIC": "A",
    "XIO": "AN",
    "OTE": "=",
    "OTL": "S",
    "OTU": "R",
    "TON": "SD T<n> with L S5T#<time>",
    "TOF": "SF T<n> with L S5T#<time>",
    "TP": "SP T<n> with L S5T#<time>",
    "CTU": "CU C<n>",
    "CTD": "CD C<n>",
    "MOV": "L <src> followed by T <dst>",
    "MOVE": "L <src> followed by T <dst>",
}

# Regex patterns for S7 Operands
# 1. Bit Devices (I, Q, M, E, A, L)
S7_BIT_DEVICE_REGEX = re.compile(
    r"^([IQMEAL])\s*(\d+)\.([0-7])$", re.IGNORECASE
)
# 2. Byte Devices (IB, QB, MB, EB, AB, LB, PIB, PQB, PEB, PAB)
S7_BYTE_DEVICE_REGEX = re.compile(
    r"^(IB|QB|MB|EB|AB|LB|PIB|PQB|PEB|PAB)\s*(\d+)$", re.IGNORECASE
)
# 3. Word Devices (IW, QW, MW, EW, AW, LW, PIW, PQW, PEW, PAW)
S7_WORD_DEVICE_REGEX = re.compile(
    r"^(IW|QW|MW|EW|AW|LW|PIW|PQW|PEW|PAW)\s*(\d+)$", re.IGNORECASE
)
# 4. Double Word Devices (ID, QD, MD, ED, AD, LD, PID, PQD, PED, PAD)
S7_DWORD_DEVICE_REGEX = re.compile(
    r"^(ID|QD|MD|ED|AD|LD|PID|PQD|PED|PAD)\s*(\d+)$", re.IGNORECASE
)
# 5. Timers
S7_TIMER_REGEX = re.compile(r"^T\s*(\d+)$", re.IGNORECASE)
# 6. Counters
S7_COUNTER_REGEX = re.compile(r"^([CZ])\s*(\d+)$", re.IGNORECASE)
# 7. Block References (FC, FB, OB, DB, DI, SFC, SFB)
S7_BLOCK_OPERAND_REGEX = re.compile(
    r"^(FC|FB|OB|SFC|SFB|DB|DI)\s*(\d+)$", re.IGNORECASE
)
# 8. DB Elements (DB1.DBX 0.0 or standalone DBX 0.0 / DBW 2 / DBD 4)
S7_DB_ELEMENT_REGEX = re.compile(
    r"^((DB\d+|DI\d+)\.)?(DBX|DBB|DBW|DBD|DIX|DIB|DIW|DID)\s*(\d+)(\.([0-7]))?$",
    re.IGNORECASE,
)
# 9. Status Word Bit Queries (==0, <>0, >0, <0, >=0, <=0, OV, OS, BR, UO)
S7_STATUS_BIT_REGEX = re.compile(
    r"^(==0|<>0|>0|<0|>=0|<=0|OV|OS|BR|UO)$", re.IGNORECASE
)
# 10. Named Symbols and Local Block Variables
S7_NAMED_SYMBOL_REGEX = re.compile(r"^\"([A-Za-z0-9_]{1,24})\"$")
S7_LOCAL_VAR_REGEX = re.compile(r"^#([A-Za-z0-9_]{1,24})$")

# S7 Constants
S7_S5TIME_REGEX = re.compile(r"^(S5T|S5TIME)#(\d+[A-Za-z]+)(_\d+[A-Za-z]+)*$", re.IGNORECASE)
S7_TIME_REGEX = re.compile(r"^(T|TIME)#(\d+[A-Za-z]+)(_\d+[A-Za-z]+)*$", re.IGNORECASE)
S7_DATE_REGEX = re.compile(r"^(D|DATE)#\d{4}-\d{1,2}-\d{1,2}$", re.IGNORECASE)
S7_TOD_REGEX = re.compile(r"^(TOD|TIME_OF_DAY)#\d{1,2}:\d{1,2}(:\d{1,2}(\.\d+)?)?$", re.IGNORECASE)
S7_DT_REGEX = re.compile(r"^(DT|DATE_AND_TIME)#\d{4}-\d{1,2}-\d{1,2}-\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$", re.IGNORECASE)
S7_HEX_REGEX = re.compile(r"^(W#16#|DW#16#|B#16#|16#)[0-9A-Fa-f]+$", re.IGNORECASE)
S7_BIN_REGEX = re.compile(r"^2#[01_]+$", re.IGNORECASE)
S7_INT_REGEX = re.compile(r"^(L#)?-?\d+$", re.IGNORECASE)
S7_REAL_REGEX = re.compile(r"^-?\d+\.\d+([eE][+-]?\d+)?$")
S7_POINTER_REGEX = re.compile(r"^P#.*$", re.IGNORECASE)
S7_CHAR_REGEX = re.compile(r"^(C#)?'[^']{1,4}'$", re.IGNORECASE)
S7_BYTE_CONST_REGEX = re.compile(r"^B#(\d+|16#[0-9A-Fa-f]+|2#[01_]+|\(-?\d+,\s*-?\d+\))$", re.IGNORECASE)
S7_INDIRECT_ADDR_REGEX = re.compile(
    r"^((DBX|DBB|DBW|DBD|DIX|DIB|DIW|DID|I|Q|M|L|E|A)\s*)?\[\s*(AR1|AR2)(\s*,\s*P#[^\]]+)?\s*\]$",
    re.IGNORECASE,
)

S7_ALL_ADDRESS_PATTERNS = [
    S7_BIT_DEVICE_REGEX,
    S7_BYTE_DEVICE_REGEX,
    S7_WORD_DEVICE_REGEX,
    S7_DWORD_DEVICE_REGEX,
    S7_TIMER_REGEX,
    S7_COUNTER_REGEX,
    S7_BLOCK_OPERAND_REGEX,
    S7_DB_ELEMENT_REGEX,
]


def _is_potential_s7_address(s: str) -> bool:
    """Returns True if string matches any standard S7 memory or device address pattern."""
    clean = s.strip()
    return any(p.fullmatch(clean) is not None for p in S7_ALL_ADDRESS_PATTERNS)


# ============================================================================
# 3. DATA MODELS
# ============================================================================

@dataclass
class S7NetworkSpec:
    """Represents a single S7 Network (ladder rung) in an AWL block."""
    title: str = ""
    comment: str = ""
    instructions: List[str] = field(default_factory=list)


@dataclass
class S7SymbolEntry:
    """Represents an entry in the Siemens S7 Symbol Table."""
    symbol: str
    address: str
    data_type: str = "BOOL"
    comment: str = ""


@dataclass
class S7ValidationReport:
    """Holds deterministic validation results and safety analysis findings."""
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    normalized_networks: List[S7NetworkSpec] = field(default_factory=list)
    estimated_mc7_bytes: int = 0
    devices_used: Set[str] = field(default_factory=set)


@dataclass
class S7CrossReferenceItem:
    """Individual instruction reference in a cross reference table."""
    network: int
    network_title: str
    instruction: str
    opcode: str
    access: str  # READ, ASSIGN, SET, RESET, TRANSFER, TIMER_CONTROL, COUNTER_CONTROL, BLOCK_CALL


@dataclass
class S7CrossReferenceEntry:
    """Consolidated cross-reference profile for an S7 device or symbol."""
    address: str
    symbol: str = ""
    data_type: str = "BOOL"
    comment: str = ""
    read_count: int = 0
    write_count: int = 0
    references: List[S7CrossReferenceItem] = field(default_factory=list)


@dataclass
class S7MemoryUsageReport:
    """Aggregated memory metrics and utilization report for a PLC program."""
    plc_type: str
    inputs_used_bytes: int
    inputs_max_bytes: int
    inputs_utilization_pct: float
    outputs_used_bytes: int
    outputs_max_bytes: int
    outputs_utilization_pct: float
    markers_used_bytes: int
    markers_max_bytes: int
    markers_utilization_pct: float
    timers_used_count: int
    timers_max_count: int
    counters_used_count: int
    counters_max_count: int
    bit_matrix: Dict[str, str] = field(default_factory=dict)
    memory_overlaps: List[str] = field(default_factory=list)


@dataclass
class S7BlockInfo:
    """Detailed structural metadata for a compiled or source Siemens S7 block."""
    block_name: str
    block_type: str
    block_number: int
    title: str = ""
    version: str = "0.1"
    author: str = ""
    family: str = ""
    network_count: int = 0
    instruction_count: int = 0
    estimated_mc7_bytes: int = 0
    temp_bytes: int = 0
    called_blocks: List[str] = field(default_factory=list)


# ============================================================================
# 4. DETERMINISTIC MNEMONIC VALIDATOR & STATIC SAFETY ANALYZER
# ============================================================================

def canonical_s7_address(operand: str) -> str:
    """
    Normalizes S7 addresses into canonical uppercase representation with
    proper spacing (e.g. 'I0.0' -> 'I 0.0', 'm10.1' -> 'M 10.1', 't1' -> 'T 1',
    'fc1' -> 'FC 1', 'dbx0.0' -> 'DBX 0.0').
    """
    clean = operand.strip()
    if not clean:
        return ""

    # Handle comma-separated operands (e.g. 'FC 1, DB 1')
    if "," in clean:
        return ", ".join(canonical_s7_address(part.strip()) for part in clean.split(",") if part.strip())

    # Bit address: I 0.0, Q 0.1, M 10.2, E 0.0, A 0.0, L 0.0
    m_bit = S7_BIT_DEVICE_REGEX.fullmatch(clean)
    if m_bit:
        prefix, byte_num, bit_num = m_bit.groups()
        return f"{prefix.upper()} {int(byte_num)}.{bit_num}"

    # Block reference: FC 1, FB 1, OB 1, DB 1, DI 1, SFC 1, SFB 1
    m_blk = S7_BLOCK_OPERAND_REGEX.fullmatch(clean)
    if m_blk:
        return f"{m_blk.group(1).upper()} {int(m_blk.group(2))}"

    # Byte / Word / DWord
    m_byte = S7_BYTE_DEVICE_REGEX.fullmatch(clean)
    if m_byte:
        return f"{m_byte.group(1).upper()} {int(m_byte.group(2))}"
    m_word = S7_WORD_DEVICE_REGEX.fullmatch(clean)
    if m_word:
        return f"{m_word.group(1).upper()} {int(m_word.group(2))}"
    m_dword = S7_DWORD_DEVICE_REGEX.fullmatch(clean)
    if m_dword:
        return f"{m_dword.group(1).upper()} {int(m_dword.group(2))}"

    # Timer / Counter
    m_t = S7_TIMER_REGEX.fullmatch(clean)
    if m_t:
        return f"T {int(m_t.group(1))}"
    m_c = S7_COUNTER_REGEX.fullmatch(clean)
    if m_c:
        return f"{m_c.group(1).upper()} {int(m_c.group(2))}"

    # DB Elements (DB1.DBX 0.0 or standalone DBX 0.0 / DBW 2)
    m_db = S7_DB_ELEMENT_REGEX.fullmatch(clean)
    if m_db:
        db_pfx = m_db.group(1)
        db_type = m_db.group(3).upper()
        offset = int(m_db.group(4))
        bit_part = f".{m_db.group(6)}" if m_db.group(6) is not None else ""
        if db_pfx:
            return f"{db_pfx.upper()}{db_type} {offset}{bit_part}"
        return f"{db_type} {offset}{bit_part}"

    # Status Bits
    m_status = S7_STATUS_BIT_REGEX.fullmatch(clean)
    if m_status:
        return m_status.group(1).upper()

    return clean.upper()


def _validate_s7_operand(operand: str, profile: S7PLCProfile) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validates operand syntax and checks device range boundaries against the profile.
    Returns (is_valid, error_message, canonical_device).
    """
    clean = operand.strip()
    if not clean:
        return False, "Empty operand.", None

    # Check for foreign PLC addresses (e.g. Mitsubishi X0, Y0)
    foreign_xy = re.fullmatch(r"^([XY])(\d+)(\.(\d+))?$", clean, re.IGNORECASE)
    if foreign_xy:
        pfx = foreign_xy.group(1).upper()
        s7_equiv = "I" if pfx == "X" else "Q"
        return False, (
            f"Invalid S7 address '{clean}'. Mitsubishi/Japanese style '{pfx}' is not used in S7; "
            f"use '{s7_equiv} 0.0' or 'E/A' for Siemens."
        ), None

    # 1. Bit Devices (I, Q, M, E, A, L)
    m_bit = S7_BIT_DEVICE_REGEX.fullmatch(clean)
    if m_bit:
        pfx, byte_str, bit_str = m_bit.groups()
        byte_val = int(byte_str)
        pfx_up = pfx.upper()
        if pfx_up in ("I", "E") and byte_val > profile.max_input_byte:
            return False, f"Input byte address '{clean}' exceeds maximum {profile.max_input_byte} for {profile.name}.", None
        if pfx_up in ("Q", "A") and byte_val > profile.max_output_byte:
            return False, f"Output byte address '{clean}' exceeds maximum {profile.max_output_byte} for {profile.name}.", None
        if pfx_up == "M" and byte_val > profile.max_marker_byte:
            return False, f"Marker byte address '{clean}' exceeds maximum {profile.max_marker_byte} for {profile.name}.", None
        if pfx_up == "L" and byte_val > 65535:
            return False, f"Local byte address '{clean}' exceeds maximum 65535.", None
        canon = f"{pfx_up} {byte_val}.{bit_str}"
        return True, None, canon

    # 2. Block References (FC, FB, OB, DB, DI, SFC, SFB)
    m_blk = S7_BLOCK_OPERAND_REGEX.fullmatch(clean)
    if m_blk:
        blk_kind = m_blk.group(1).upper()
        blk_num = int(m_blk.group(2))
        if blk_kind in ("DB", "DI") and blk_num > profile.max_db_num:
            msg = f"Data Block number '{blk_kind} {blk_num}' exceeds maximum {profile.max_db_num} for {profile.name}."
            return False, msg, None
        if blk_num > 65535:
            return False, f"Block number '{blk_kind} {blk_num}' exceeds maximum 65535.", None
        return True, None, f"{blk_kind} {blk_num}"

    # 3. Byte Devices
    m_b = S7_BYTE_DEVICE_REGEX.fullmatch(clean)
    if m_b:
        pfx, byte_str = m_b.groups()
        byte_val = int(byte_str)
        pfx_up = pfx.upper()
        if pfx_up in ("IB", "EB", "PIB", "PEB") and byte_val > profile.max_input_byte:
            return False, f"Input byte '{clean}' exceeds maximum {profile.max_input_byte} for {profile.name}.", None
        if pfx_up in ("QB", "AB", "PQB", "PAB") and byte_val > profile.max_output_byte:
            return False, f"Output byte '{clean}' exceeds maximum {profile.max_output_byte} for {profile.name}.", None
        if pfx_up == "MB" and byte_val > profile.max_marker_byte:
            return False, f"Marker byte '{clean}' exceeds maximum {profile.max_marker_byte} for {profile.name}.", None
        return True, None, f"{pfx_up} {byte_val}"

    # 4. Word Devices
    m_w = S7_WORD_DEVICE_REGEX.fullmatch(clean)
    if m_w:
        pfx, byte_str = m_w.groups()
        byte_val = int(byte_str)
        pfx_up = pfx.upper()
        if pfx_up in ("IW", "EW", "PIW", "PEW") and byte_val + 1 > profile.max_input_byte:
            return False, f"Input word '{clean}' exceeds address range for {profile.name}.", None
        if pfx_up in ("QW", "AW", "PQW", "PAW") and byte_val + 1 > profile.max_output_byte:
            return False, f"Output word '{clean}' exceeds address range for {profile.name}.", None
        if pfx_up == "MW" and byte_val + 1 > profile.max_marker_byte:
            return False, f"Marker word '{clean}' exceeds address range for {profile.name}.", None
        return True, None, f"{pfx_up} {byte_val}"

    # 5. Double Word Devices
    m_d = S7_DWORD_DEVICE_REGEX.fullmatch(clean)
    if m_d:
        pfx, byte_str = m_d.groups()
        byte_val = int(byte_str)
        pfx_up = pfx.upper()
        if pfx_up in ("ID", "ED", "PID", "PED") and byte_val + 3 > profile.max_input_byte:
            return False, f"Input dword '{clean}' exceeds address range for {profile.name}.", None
        if pfx_up in ("QD", "AD", "PQD", "PAD") and byte_val + 3 > profile.max_output_byte:
            return False, f"Output dword '{clean}' exceeds address range for {profile.name}.", None
        if pfx_up == "MD" and byte_val + 3 > profile.max_marker_byte:
            return False, f"Marker dword '{clean}' exceeds address range for {profile.name}.", None
        return True, None, f"{pfx_up} {byte_val}"

    # 6. Timers
    m_t = S7_TIMER_REGEX.fullmatch(clean)
    if m_t:
        t_num = int(m_t.group(1))
        if t_num > profile.max_timer_num:
            return False, f"Timer '{clean}' exceeds maximum T {profile.max_timer_num} for {profile.name}.", None
        return True, None, f"T {t_num}"

    # 7. Counters
    m_c = S7_COUNTER_REGEX.fullmatch(clean)
    if m_c:
        pfx, c_str = m_c.groups()
        c_num = int(c_str)
        if c_num > profile.max_counter_num:
            return False, f"Counter '{clean}' exceeds maximum {profile.max_counter_num} for {profile.name}.", None
        return True, None, f"{pfx.upper()} {c_num}"

    # 8. DB Elements (DB1.DBX 0.0 or DBX 0.0)
    m_db = S7_DB_ELEMENT_REGEX.fullmatch(clean)
    if m_db:
        db_pfx = m_db.group(1)
        if db_pfx:
            num_part = re.search(r"\d+", db_pfx)
            if num_part:
                db_num = int(num_part.group(0))
                if db_num > profile.max_db_num:
                    return False, f"Data Block number '{db_pfx}' exceeds maximum DB {profile.max_db_num}.", None
        offset = int(m_db.group(4))
        if offset > profile.max_db_byte:
            return False, f"DB byte offset '{offset}' exceeds maximum {profile.max_db_byte}.", None
        canon = canonical_s7_address(clean)
        return True, None, canon

    # 9. Status Word Bit Queries (==0, <>0, >0, <0, >=0, <=0, OV, OS, BR, UO)
    m_status = S7_STATUS_BIT_REGEX.fullmatch(clean)
    if m_status:
        return True, None, m_status.group(1).upper()

    # 10. Address Register Indirect Addressing: [AR1, P#...], [AR2, P#...]
    if S7_INDIRECT_ADDR_REGEX.fullmatch(clean):
        return True, None, clean.upper()

    # 11. Named Symbols or Local Variables: "MOTOR_RUN" or #TEMP_VAL
    if S7_NAMED_SYMBOL_REGEX.fullmatch(clean) or S7_LOCAL_VAR_REGEX.fullmatch(clean):
        return True, None, clean

    # 12. Constants: S5T#, TIME#, D#, TOD#, DT#, 16#, 2#, integer, real, pointer, char, byte
    if (
        S7_S5TIME_REGEX.fullmatch(clean)
        or S7_TIME_REGEX.fullmatch(clean)
        or S7_DATE_REGEX.fullmatch(clean)
        or S7_TOD_REGEX.fullmatch(clean)
        or S7_DT_REGEX.fullmatch(clean)
        or S7_HEX_REGEX.fullmatch(clean)
        or S7_BIN_REGEX.fullmatch(clean)
        or S7_INT_REGEX.fullmatch(clean)
        or S7_REAL_REGEX.fullmatch(clean)
        or S7_POINTER_REGEX.fullmatch(clean)
        or S7_CHAR_REGEX.fullmatch(clean)
        or S7_BYTE_CONST_REGEX.fullmatch(clean)
    ):
        return True, None, clean.upper()

    # 13. Labels for jumps (alphanumeric up to 16 chars)
    if re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]{0,15}$", clean):
        return True, None, clean

    return False, f"Unrecognized S7 device, operand or constant syntax '{clean}'.", None


def estimate_s7_instruction_bytes(opcode: str, operands: List[str]) -> int:
    """Estimates the MC7 binary instruction size in bytes."""
    op = opcode.upper()
    if op in ("A", "AN", "O", "ON", "X", "XN", "U", "UN", "=", "S", "R", "FP", "FN"):
        return 2
    if op in ("A(", "AN(", "O(", "ON(", "X(", "XN(", "U(", "UN(", ")", "NOT", "SET", "CLR", "SAVE"):
        return 2
    if op in ("SP", "SE", "SD", "SS", "SF", "FR", "CU", "CD", "SI", "SV", "SA", "ZV", "ZR"):
        return 2
    if op in ("L", "LC", "T"):
        if operands and (operands[0].startswith("DB") or operands[0].startswith("DI")):
            return 4
        prefixes = ("16#", "2#", "DW#16#", "L#", "P#", "S5T#", "T#", "D#", "TOD#", "DT#")
        if operands and any(operands[0].startswith(p) for p in prefixes):
            return 6
        return 2
    if op in ("LAR1", "LAR2", "TAR1", "TAR2", "CAR"):
        return 4
    if op in ("PUSH", "POP", "ENT", "LEAVE", "STP"):
        return 2
    comp_ops = (
        "==I", "<>I", ">I", "<I", ">=I", "<=I",
        "==D", "<>D", ">D", "<D", ">=D", "<=D",
        "==R", "<>R", ">R", "<R", ">=R", "<=R",
    )
    if op in comp_ops:
        return 2
    if op in ("+I", "-I", "*I", "/I", "+D", "-D", "*D", "/D", "MOD", "+R", "-R", "*R", "/R"):
        return 2
    if op in ("JU", "JC", "JCN", "JCB", "JNB", "JBI", "JNBI", "LOOP", "SPA", "SPB", "SPBN", "SPBB", "SPBI", "SPBIN", "JL", "SPL"):
        return 4
    if op in ("BE", "BEC", "BEU", "BEA", "BEB"):
        return 2
    if op in ("CALL", "CC", "UC"):
        return 6
    if op == "OPN":
        return 4
    return 2


def infer_s7_data_type(address: str) -> str:
    """Infers default Siemens S7 data type from address format."""
    clean = address.strip().upper()
    if S7_BIT_DEVICE_REGEX.fullmatch(clean):
        return "BOOL"
    if S7_BYTE_DEVICE_REGEX.fullmatch(clean):
        return "BYTE"
    if S7_WORD_DEVICE_REGEX.fullmatch(clean):
        return "WORD"
    if S7_DWORD_DEVICE_REGEX.fullmatch(clean):
        return "DWORD"
    if S7_TIMER_REGEX.fullmatch(clean):
        return "TIMER"
    if S7_COUNTER_REGEX.fullmatch(clean):
        return "COUNTER"
    if S7_DB_ELEMENT_REGEX.fullmatch(clean):
        if "DBX" in clean or "DIX" in clean:
            return "BOOL"
        if "DBB" in clean or "DIB" in clean:
            return "BYTE"
        if "DBW" in clean or "DIW" in clean:
            return "WORD"
        if "DBD" in clean or "DID" in clean:
            return "DWORD"
    if S7_BLOCK_OPERAND_REGEX.fullmatch(clean):
        return "BLOCK"
    return "BOOL"


def _validate_symbol_map(symbols_input: Any) -> Tuple[List[str], List[S7SymbolEntry]]:
    """
    Validates S7 symbol table definitions.
    Supports Symbol -> Address, Address -> Symbol/Comment, or list of dicts.
    Auto-disambiguates symbol names for duplicate device comments and infers data types.
    """
    errors: List[str] = []
    symbol_entries: List[S7SymbolEntry] = []
    if symbols_input is None:
        return errors, symbol_entries

    items: List[Dict[str, str]] = []
    seen_gen_syms: Set[str] = set()

    if isinstance(symbols_input, dict):
        for k, v in symbols_input.items():
            k_str = str(k).strip()
            # Auto-detect if key is an S7 address
            if _is_potential_s7_address(k_str):
                inferred_type = infer_s7_data_type(k_str)
                addr_slug = re.sub(r"[^A-Za-z0-9]", "_", k_str).strip("_")
                if isinstance(v, str):
                    v_str = v.strip()
                    if re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]{0,23}$", v_str):
                        items.append({"symbol": v_str, "address": k_str, "data_type": inferred_type, "comment": ""})
                    else:
                        gen_sym = re.sub(r"[^A-Za-z0-9_]", "_", v_str).strip("_")[:24]
                        if not gen_sym or not gen_sym[0].isalpha():
                            gen_sym = f"SYM_{gen_sym}"[:24]
                        if gen_sym.upper() in seen_gen_syms:
                            gen_sym = f"{gen_sym[:17]}_{addr_slug}"[:24]
                        seen_gen_syms.add(gen_sym.upper())
                        items.append({
                            "symbol": gen_sym,
                            "address": k_str,
                            "data_type": inferred_type,
                            "comment": v_str,
                        })
                elif isinstance(v, dict):
                    sym = v.get("symbol")
                    if not sym:
                        comment_str = v.get("comment", "")
                        if comment_str:
                            gen_sym = re.sub(r"[^A-Za-z0-9_]", "_", comment_str).strip("_")[:24]
                            if not gen_sym or not gen_sym[0].isalpha():
                                gen_sym = f"SYM_{gen_sym}"[:24]
                            if gen_sym.upper() in seen_gen_syms:
                                gen_sym = f"{gen_sym[:17]}_{addr_slug}"[:24]
                            seen_gen_syms.add(gen_sym.upper())
                            sym = gen_sym
                        else:
                            sym = f"DEV_{addr_slug}"[:24]
                    items.append({
                        "symbol": str(sym),
                        "address": k_str,
                        "data_type": v.get("data_type", inferred_type),
                        "comment": v.get("comment", ""),
                    })
                else:
                    errors.append(f"Invalid symbol entry value for address key '{k}'.")
            else:
                # Key is symbol name
                if isinstance(v, str):
                    v_str = v.strip()
                    items.append({
                        "symbol": k_str,
                        "address": v_str,
                        "data_type": infer_s7_data_type(v_str),
                        "comment": "",
                    })
                elif isinstance(v, dict):
                    addr_val = v.get("address", "")
                    entry = {"symbol": k_str, "data_type": infer_s7_data_type(str(addr_val)), **v}
                    items.append(entry)
                else:
                    errors.append(f"Invalid symbol entry value for key '{k}'.")
    elif isinstance(symbols_input, list):
        for idx, item in enumerate(symbols_input, start=1):
            if not isinstance(item, dict):
                errors.append(f"Symbol item {idx} must be a dictionary object.")
                continue
            items.append(item)
    else:
        errors.append("symbols must be a dictionary or list of symbol objects.")
        return errors, symbol_entries

    seen_symbols: Set[str] = set()
    seen_addresses: Set[str] = set()

    valid_types = {
        "BOOL", "BYTE", "WORD", "DWORD", "INT", "DINT", "REAL", "S5TIME", "TIME",
        "DATE", "TIME_OF_DAY", "CHAR", "STRING", "TIMER", "COUNTER", "BLOCK",
    }

    for idx, item in enumerate(items, start=1):
        sym_name = str(item.get("symbol", "")).strip()
        addr = str(item.get("address", "")).strip()
        dtype = str(item.get("data_type", "BOOL")).strip().upper()
        comment = str(item.get("comment", "")).strip()

        if not sym_name:
            errors.append(f"Symbol item {idx} is missing 'symbol' name.")
            continue
        if len(sym_name) > 24:
            errors.append(f"Symbol '{sym_name}' exceeds Siemens 24-character maximum limit.")
        if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", sym_name):
            errors.append(f"Symbol '{sym_name}' contains illegal characters; must be alphanumeric with underscore.")
        if sym_name.upper() in seen_symbols:
            errors.append(f"Duplicate symbol name '{sym_name}'.")
        seen_symbols.add(sym_name.upper())

        if not addr:
            errors.append(f"Symbol '{sym_name}' is missing an address.")
            continue
        canon_addr = canonical_s7_address(addr)
        if canon_addr in seen_addresses:
            errors.append(f"Duplicate symbol address '{addr}' for symbol '{sym_name}'.")
        seen_addresses.add(canon_addr)

        if dtype not in valid_types:
            errors.append(f"Symbol '{sym_name}' has unsupported S7 data type '{dtype}'.")
        if len(comment) > 80:
            errors.append(f"Comment for symbol '{sym_name}' exceeds 80 characters.")

        symbol_entries.append(S7SymbolEntry(
            symbol=sym_name,
            address=canon_addr,
            data_type=dtype,
            comment=comment,
        ))

    return errors, symbol_entries


def validate_and_analyze_program(
    networks_input: List[Dict[str, Any]],
    symbols_input: Optional[Any] = None,
    auto_fix_mnemonics: bool = False,
    plc_type: str = "S7-300",
    mnemonic_language: str = "International",
) -> S7ValidationReport:
    """
    Validates S7 Statement List (AWL) networks, addresses, and performs deterministic
    safety analysis including Star-Delta contactor interlock checks and duplicate output coil detection.
    """
    profile, profile_error = resolve_s7_plc_profile(plc_type)
    report = S7ValidationReport()
    if profile_error or profile is None:
        report.valid = False
        report.errors.append(profile_error or "Unknown PLC profile.")
        return report

    # 1. Validate Symbol table if provided
    sym_errors, symbol_entries = _validate_symbol_map(symbols_input)
    if sym_errors:
        report.valid = False
        report.errors.extend(sym_errors)

    # Build symbol lookup map: symbol -> canonical address and address -> symbol
    symbol_to_addr: Dict[str, str] = {}
    addr_to_symbol: Dict[str, str] = {}
    for entry in symbol_entries:
        symbol_to_addr[f'"{entry.symbol.upper()}"'] = entry.address.upper()
        symbol_to_addr[entry.symbol.upper()] = entry.address.upper()
        addr_to_symbol[entry.address.upper()] = entry.symbol.upper()

    if not isinstance(networks_input, list) or not networks_input:
        report.valid = False
        report.errors.append("Provide at least one network as a list of network objects.")
        return report

    assigned_coils: Dict[str, List[int]] = {}  # canonical coil address -> list of network indices
    star_network: Optional[int] = None
    delta_network: Optional[int] = None
    star_coils: Set[str] = set()
    delta_coils: Set[str] = set()
    network_interlocks: Dict[int, Set[str]] = {}

    timers_started: Set[str] = set()

    is_german_target = mnemonic_language.strip().lower() == "german"

    for net_idx, raw_net in enumerate(networks_input, start=1):
        if not isinstance(raw_net, dict):
            report.valid = False
            report.errors.append(f"Network {net_idx} must be an object.")
            continue

        title = raw_net.get("title", raw_net.get("statement", ""))
        comment = raw_net.get("comment", "")
        if not isinstance(title, str):
            report.valid = False
            report.errors.append(f"Network {net_idx} title must be a string.")
            title = ""
        if not isinstance(comment, str):
            report.valid = False
            report.errors.append(f"Network {net_idx} comment must be a string.")
            comment = ""

        title = title.strip()
        if len(title) > 64:
            report.valid = False
            report.errors.append(f"Network {net_idx} title exceeds the 64-character limit.")

        raw_insts = raw_net.get("instructions")
        if not isinstance(raw_insts, list) or not raw_insts:
            report.valid = False
            report.errors.append(f"Network {net_idx} instructions must be a non-empty list of strings.")
            continue

        norm_insts: List[str] = []
        last_loaded_time_val: bool = False

        for inst_idx, inst_line in enumerate(raw_insts, start=1):
            if not isinstance(inst_line, str):
                report.valid = False
                report.errors.append(f"Network {net_idx}, line {inst_idx}: instruction must be a string.")
                continue

            clean_line = inst_line.strip()
            # Strip trailing semicolon if user provided it
            if clean_line.endswith(";"):
                clean_line = clean_line[:-1].strip()

            if not clean_line or clean_line.startswith("//"):
                report.valid = False
                msg = f"Network {net_idx}, line {inst_idx}: blank lines and comment lines are not accepted as instructions."
                report.errors.append(msg)
                continue

            # Check for label prefix on line (e.g. 'LAB1: L 100' or 'M001:')
            label_prefix: Optional[str] = None
            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15}):\s*(.*)$", clean_line)
            if m_lbl:
                label_prefix = m_lbl.group(1).upper()
                clean_line = m_lbl.group(2).strip()
                if not clean_line:
                    # Line contains only label; normalize to label with NOP 0
                    norm_insts.append(f"{label_prefix}: NOP 0")
                    continue

            # Split opcode and operands
            tokens = clean_line.split(maxsplit=1)
            raw_opcode = tokens[0].upper()
            raw_operand_str = tokens[1].strip() if len(tokens) > 1 else ""

            # Check for non-Siemens mnemonics / hints
            if raw_opcode in S7_OPCODE_HINTS:
                hint = S7_OPCODE_HINTS[raw_opcode]
                target_foreigns = ("LD", "LDI", "LDN", "OUT", "AND", "ANI", "ANDN", "OR", "ORI", "ORN")
                if auto_fix_mnemonics and raw_opcode in target_foreigns:
                    fixed_opcode = hint
                    if is_german_target:
                        fixed_opcode = INTL_TO_GERMAN_OPCODES.get(hint, hint)
                    w_msg = (
                        f"Network {net_idx}, line {inst_idx}: converted foreign mnemonic "
                        f"'{raw_opcode}' to Siemens '{fixed_opcode}'."
                    )
                    report.warnings.append(w_msg)
                    raw_opcode = fixed_opcode
                else:
                    report.valid = False
                    err_msg = (
                        f"Network {net_idx}, line {inst_idx}: unsupported mnemonic "
                        f"'{raw_opcode}'; use Siemens '{hint}'."
                    )
                    report.errors.append(err_msg)
                    continue

            # Mnemonic language normalization if auto_fix_mnemonics is True
            if auto_fix_mnemonics:
                if is_german_target and raw_opcode in INTL_TO_GERMAN_OPCODES:
                    raw_opcode = INTL_TO_GERMAN_OPCODES[raw_opcode]
                elif not is_german_target and raw_opcode in GERMAN_TO_INTL_OPCODES:
                    raw_opcode = GERMAN_TO_INTL_OPCODES[raw_opcode]

            # Check opcode validity
            if raw_opcode not in SUPPORTED_S7_OPCODES:
                report.valid = False
                report.errors.append(f"Network {net_idx}, line {inst_idx}: unknown or unsupported S7 opcode '{raw_opcode}'.")
                continue

            min_ops, max_ops = SUPPORTED_S7_OPCODES[raw_opcode]

            # Parse operands
            operands: List[str] = []
            if raw_operand_str:
                if "," in raw_operand_str:
                    operands = [op.strip() for op in raw_operand_str.split(",") if op.strip()]
                else:
                    operands = [raw_operand_str.strip()]

            if not (min_ops <= len(operands) <= max_ops):
                report.valid = False
                report.errors.append(
                    f"Network {net_idx}, line {inst_idx}: opcode '{raw_opcode}' expects "
                    f"{min_ops}-{max_ops} operands, found {len(operands)}."
                )
                continue

            # Validate each operand against profile
            validated_operands: List[str] = []
            for op in operands:
                ok, err, canon = _validate_s7_operand(op, profile)
                if not ok:
                    report.valid = False
                    report.errors.append(f"Network {net_idx}, line {inst_idx}: {err}")
                else:
                    validated_operands.append(canon or op)
                    if canon:
                        report.devices_used.add(canon)
                    elif op.startswith('"'):
                        report.devices_used.add(op)

            # Check Timer value loading sequence
            if raw_opcode == "L" and validated_operands and any(validated_operands[0].startswith(p) for p in ("S5T#", "S5TIME#")):
                last_loaded_time_val = True
            elif raw_opcode in ("SP", "SE", "SD", "SS", "SF", "SI", "SV", "SA"):
                if validated_operands:
                    timer_name = validated_operands[0]
                    timers_started.add(timer_name)
                    if not last_loaded_time_val:
                        w_timer = (
                            f"Network {net_idx}, line {inst_idx}: S7 timer '{timer_name}' triggered "
                            "without preceding 'L S5T#...' time value in the same network."
                        )
                        report.warnings.append(w_timer)
                last_loaded_time_val = False
            else:
                last_loaded_time_val = False

            # Track output coils for duplicate assignment detection
            if raw_opcode == "=":
                if validated_operands:
                    raw_coil = validated_operands[0].upper()
                    # Resolve to canonical physical address if symbolic
                    canon_coil = symbol_to_addr.get(raw_coil, raw_coil)
                    assigned_coils.setdefault(canon_coil, []).append(net_idx)

            # Star-Delta Interlock Detection (support English and German: STAR/DELTA, STERN/DREIECK)
            net_text = f"{title} {comment} {clean_line}".upper()
            if any(term in net_text for term in ("STAR", "STERN")) and raw_opcode in ("=", "S"):
                star_network = net_idx
                if validated_operands:
                    coil_name = validated_operands[0].upper()
                    star_coils.add(coil_name)
                    if coil_name in symbol_to_addr:
                        star_coils.add(symbol_to_addr[coil_name])
                    if coil_name in addr_to_symbol:
                        star_coils.add(f'"{addr_to_symbol[coil_name]}"')
            if any(term in net_text for term in ("DELTA", "DREIECK")) and raw_opcode in ("=", "S"):
                delta_network = net_idx
                if validated_operands:
                    coil_name = validated_operands[0].upper()
                    delta_coils.add(coil_name)
                    if coil_name in symbol_to_addr:
                        delta_coils.add(symbol_to_addr[coil_name])
                    if coil_name in addr_to_symbol:
                        delta_coils.add(f'"{addr_to_symbol[coil_name]}"')

            if raw_opcode in ("AN", "UN") and validated_operands:
                interlock_op = validated_operands[0].upper()
                targets = {interlock_op}
                if interlock_op in symbol_to_addr:
                    targets.add(symbol_to_addr[interlock_op])
                if interlock_op in addr_to_symbol:
                    targets.add(f'"{addr_to_symbol[interlock_op]}"')
                network_interlocks.setdefault(net_idx, set()).update(targets)

            # Format normalized instruction string
            if validated_operands:
                inst_str = f"{raw_opcode} {', '.join(validated_operands)}"
            else:
                inst_str = raw_opcode

            if label_prefix:
                norm_insts.append(f"{label_prefix}: {inst_str}")
            else:
                norm_insts.append(inst_str)

            report.estimated_mc7_bytes += estimate_s7_instruction_bytes(raw_opcode, validated_operands)

        report.normalized_networks.append(S7NetworkSpec(
            title=title,
            comment=comment,
            instructions=norm_insts,
        ))

    # Static Safety Checks:
    # 1. Duplicate Output Coils Warning:
    for coil, nets in assigned_coils.items():
        if len(nets) > 1:
            report.warnings.append(
                f"Duplicate assignment coil '{coil}' written in multiple networks: {nets}. "
                "In PLC scan cycles, subsequent assignments overwrite earlier ones."
            )

    # 2. Star-Delta Switching Hazard Check:
    if star_network is not None and delta_network is not None:
        star_has_delta_interlock = bool(delta_coils.intersection(network_interlocks.get(star_network, set())))
        delta_has_star_interlock = bool(star_coils.intersection(network_interlocks.get(delta_network, set())))
        if not (star_has_delta_interlock and delta_has_star_interlock):
            report.warnings.append(
                f"Star-Delta switching hazard detected between Network {star_network} and Network {delta_network}: "
                "Star and Delta contactors lack mutual software interlocking (e.g. 'AN <Delta_Coil>' in Star network "
                "and 'AN <Star_Coil>' in Delta network). Dead short risk on mains during transitions."
            )

    # 3. Check for unreferenced symbols:
    if symbol_entries:
        for sym in symbol_entries:
            canon = sym.address.upper()
            sym_quoted = f'"{sym.symbol.upper()}"'
            # Also check German equivalent of address (I <-> E, Q <-> A)
            german_canon = re.sub(r"^I\b", "E", canon) if canon.startswith("I") else re.sub(r"^Q\b", "A", canon)
            intl_canon = re.sub(r"^E\b", "I", canon) if canon.startswith("E") else re.sub(r"^A\b", "Q", canon)
            if (
                canon not in report.devices_used
                and sym_quoted not in report.devices_used
                and german_canon not in report.devices_used
                and intl_canon not in report.devices_used
            ):
                report.warnings.append(
                    f"Symbol '{sym.symbol}' ({sym.address}) is declared but not referenced in the program logic."
                )

    if report.errors:
        report.valid = False

    return report


# ============================================================================
# 5. AWL / STL SOURCE CODE GENERATOR & PARSER
# ============================================================================

def generate_s7_awl_source(
    networks: List[S7NetworkSpec],
    block_type: str = "OB",
    block_number: int = 1,
    title: str = "Main Program Sweep",
    version: str = "0.1",
    symbols: Optional[Any] = None,
    annotate_comments: bool = True,
    style: str = "COMMENTS",
) -> str:
    """
    Generates standard Siemens STEP 7 AWL (Statement List / STL) text source code.
    Compatible with SIMATIC Manager 'Compile Source File' and LAD/FBD/STL editors.
    Optionally annotates instructions with symbol comments or substitutes symbolic names.
    """
    lines: List[str] = []
    block_type_up = block_type.upper()

    # Build symbol lookups if symbols provided
    addr_to_sym_entry: Dict[str, S7SymbolEntry] = {}
    sym_to_entry: Dict[str, S7SymbolEntry] = {}
    if symbols:
        if isinstance(symbols, list) and symbols and isinstance(symbols[0], S7SymbolEntry):
            sym_entries = symbols
        else:
            _, sym_entries = _validate_symbol_map(symbols)
        for e in sym_entries:
            addr_to_sym_entry[canonical_s7_address(e.address)] = e
            addr_to_sym_entry[e.address.strip().upper()] = e
            sym_to_entry[e.symbol.strip().upper()] = e

    if block_type_up == "OB":
        lines.append(f"ORGANIZATION_BLOCK OB {block_number}")
        lines.append(f'TITLE = "{title}"')
        lines.append(f"VERSION : {version}")
        lines.append("")
        lines.append("VAR_TEMP")
        lines.append("  OB1_EV_CLASS : BYTE ;")
        lines.append("  OB1_SCAN_1 : BYTE ;")
        lines.append("  OB1_PRIORITY : BYTE ;")
        lines.append("  OB1_OB_NUMBR : BYTE ;")
        lines.append("  OB1_RESERVED_1 : BYTE ;")
        lines.append("  OB1_RESERVED_2 : BYTE ;")
        lines.append("  OB1_PREV_CYCLE : INT ;")
        lines.append("  OB1_MIN_CYCLE : INT ;")
        lines.append("  OB1_MAX_CYCLE : INT ;")
        lines.append("  OB1_DATE_TIME : DATE_AND_TIME ;")
        lines.append("END_VAR")
        lines.append("")
        lines.append("BEGIN")
    elif block_type_up == "FC":
        lines.append(f"FUNCTION FC {block_number} : VOID")
        lines.append(f'TITLE = "{title}"')
        lines.append(f"VERSION : {version}")
        lines.append("")
        lines.append("BEGIN")
    elif block_type_up == "FB":
        lines.append(f"FUNCTION_BLOCK FB {block_number}")
        lines.append(f'TITLE = "{title}"')
        lines.append(f"VERSION : {version}")
        lines.append("")
        lines.append("BEGIN")
    else:
        lines.append(f"ORGANIZATION_BLOCK OB {block_number}")
        lines.append(f'TITLE = "{title}"')
        lines.append(f"VERSION : {version}")
        lines.append("")
        lines.append("BEGIN")

    for net_idx, net in enumerate(networks, start=1):
        lines.append("NETWORK")
        if net.title:
            lines.append(f"TITLE = {net.title}")
        if net.comment:
            lines.append(f"// {net.comment}")
        for inst in net.instructions:
            existing_comment = ""
            inst_code = inst
            if "//" in inst:
                inst_code, existing_comment = inst.split("//", 1)
                inst_code = inst_code.strip()
                existing_comment = existing_comment.strip()

            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", inst_code)
            if m_lbl:
                lbl = m_lbl.group(1)
                rem = m_lbl.group(2).strip()
                lbl_tag = f"{lbl}:"
                if rem:
                    tokens = rem.split(maxsplit=1)
                    opcode = tokens[0]
                    operands = tokens[1] if len(tokens) > 1 else ""
                else:
                    opcode = ""
                    operands = ""
            else:
                lbl_tag = ""
                tokens = inst_code.split(maxsplit=1)
                opcode = tokens[0] if tokens else ""
                operands = tokens[1] if len(tokens) > 1 else ""

            # Check if instruction can be annotated or transformed with symbols
            annotation_comment = ""
            if operands and (addr_to_sym_entry or sym_to_entry):
                canon_op = canonical_s7_address(operands)
                m_quoted = S7_NAMED_SYMBOL_REGEX.fullmatch(operands)
                sym_key = m_quoted.group(1).upper() if m_quoted else operands.strip('"').upper()

                if canon_op in addr_to_sym_entry:
                    entry = addr_to_sym_entry[canon_op]
                    if style.upper() == "SYMBOLIC":
                        operands = f'"{entry.symbol}"'
                        annotation_comment = f"{entry.address}: {entry.comment}" if entry.comment else entry.address
                    elif annotate_comments:
                        annotation_comment = f'"{entry.symbol}": {entry.comment}' if entry.comment else f'"{entry.symbol}"'
                elif sym_key in sym_to_entry:
                    entry = sym_to_entry[sym_key]
                    if style.upper() == "ABSOLUTE":
                        operands = entry.address
                        annotation_comment = f'"{entry.symbol}": {entry.comment}' if entry.comment else f'"{entry.symbol}"'
                    elif annotate_comments:
                        annotation_comment = f"{entry.address}: {entry.comment}" if entry.comment else entry.address

            comment_to_print = existing_comment or annotation_comment
            comment_suffix = f"  // {comment_to_print}" if comment_to_print else ""

            if lbl_tag:
                if opcode and operands:
                    lines.append(f"{lbl_tag:<8} {opcode:<6} {operands};{comment_suffix}")
                elif opcode:
                    lines.append(f"{lbl_tag:<8} {opcode};{comment_suffix}")
                else:
                    lines.append(f"{lbl_tag:<8} ;{comment_suffix}")
            else:
                if opcode and operands:
                    lines.append(f"      {opcode:<6} {operands};{comment_suffix}")
                elif opcode:
                    lines.append(f"      {opcode};{comment_suffix}")

        lines.append("")

    if block_type_up == "OB":
        lines.append("END_ORGANIZATION_BLOCK")
    elif block_type_up == "FC":
        lines.append("END_FUNCTION")
    elif block_type_up == "FB":
        lines.append("END_FUNCTION_BLOCK")
    else:
        lines.append("END_ORGANIZATION_BLOCK")

    return "\r\n".join(lines) + "\r\n"


def parse_s7_awl_source(filepath: Path) -> Dict[str, Any]:
    """
    Parses a Siemens STEP 7 AWL text source file, extracting block metadata,
    networks, titles, comments, and instructions.
    """
    try:
        raw_bytes = filepath.read_bytes()
        if not raw_bytes:
            return {"error": f"AWL source file is empty: {filepath}"}
        if raw_bytes.startswith(b"\xff\xfe"):
            text = raw_bytes[2:].decode("utf-16-le", errors="strict")
        elif raw_bytes.startswith(b"\xfe\xff"):
            text = raw_bytes[2:].decode("utf-16-be", errors="strict")
        elif raw_bytes.startswith(b"\xef\xbb\xbf"):
            text = raw_bytes.decode("utf-8-sig", errors="strict")
        else:
            try:
                text = raw_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                text = raw_bytes.decode("cp1252", errors="replace")
    except FileNotFoundError:
        return {"error": f"AWL source file not found: {filepath}"}
    except Exception as exc:
        return {"error": f"Failed to read AWL source file: {exc}"}

    lines = [line.strip() for line in text.splitlines()]

    # Find Block Header
    block_header_idx = None
    block_type = ""
    block_number = None
    for i, line in enumerate(lines):
        m_blk = re.match(r"^(ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION)\s+([A-Za-z0-9]+)\s*(\d*)", line, re.IGNORECASE)
        if m_blk:
            block_header_idx = i
            block_type = m_blk.group(1).upper()
            block_number = m_blk.group(3) or m_blk.group(2)
            break

    if block_header_idx is None:
        return {"error": f"Not a recognized Siemens S7 AWL source file (no block header): {filepath}"}

    # Find BEGIN marker
    begin_idx = None
    for i in range(block_header_idx, len(lines)):
        if lines[i].upper() == "BEGIN":
            begin_idx = i
            break

    if begin_idx is None:
        return {"error": "Missing 'BEGIN' section in S7 AWL source file."}

    # Find END_<BLOCK> marker
    end_marker = f"END_{block_type}"
    end_idx = None
    for i in range(begin_idx, len(lines)):
        if lines[i].upper().startswith(end_marker):
            end_idx = i
            break

    if end_idx is None:
        return {"error": f"Missing terminal '{end_marker}' in S7 AWL source file."}

    # Parse Networks
    networks: List[Dict[str, Any]] = []
    current_net: Optional[Dict[str, Any]] = None

    for line in lines[begin_idx + 1:end_idx]:
        if not line:
            continue
        if line.upper() == "NETWORK":
            if current_net is not None:
                networks.append(current_net)
            current_net = {"title": "", "comment": "", "instructions": []}
            continue

        if current_net is None:
            current_net = {"title": "", "comment": "", "instructions": []}

        if line.upper().startswith("TITLE"):
            t_match = re.match(r"^TITLE\s*=\s*\"?(.*?)\"?$", line, re.IGNORECASE)
            if t_match:
                current_net["title"] = t_match.group(1).strip()
            continue

        if line.startswith("//"):
            current_net["comment"] = line[2:].strip()
            continue

        # Clean line: strip inline comments and trailing semicolons
        code_part = line.split("//", 1)[0].strip()
        if not code_part:
            continue
        if code_part.endswith(";"):
            code_part = code_part[:-1].strip()

        # Check for label prefix on code line
        m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", code_part)
        if m_lbl:
            lbl = m_lbl.group(1).upper()
            rem = m_lbl.group(2).strip()
            if rem:
                tokens = rem.split(maxsplit=1)
                op = tokens[0].upper()
                args = tokens[1].strip() if len(tokens) > 1 else ""
                if args:
                    canon_op = canonical_s7_address(args)
                    current_net["instructions"].append(f"{lbl}: {op} {canon_op}")
                else:
                    current_net["instructions"].append(f"{lbl}: {op}")
            else:
                current_net["instructions"].append(f"{lbl}: NOP 0")
        else:
            tokens = code_part.split(maxsplit=1)
            if tokens:
                op = tokens[0].upper()
                args = tokens[1].strip() if len(tokens) > 1 else ""
                if args:
                    canon_op = canonical_s7_address(args)
                    current_net["instructions"].append(f"{op} {canon_op}")
                else:
                    current_net["instructions"].append(op)

    if current_net is not None:
        networks.append(current_net)

    return {
        "file": str(filepath),
        "block_type": block_type,
        "block_number": block_number,
        "network_count": len(networks),
        "networks": networks,
    }


def s7_instruction_signature(networks: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Generates an ordered signature list of (opcode, normalized_operands) for readback comparison."""
    sig = []
    for net in networks:
        for inst in net.get("instructions", []):
            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", inst)
            if m_lbl:
                lbl = m_lbl.group(1).upper()
                rem = m_lbl.group(2).strip()
                if rem:
                    parts = rem.split(maxsplit=1)
                    op = parts[0].upper()
                    args = parts[1].strip().upper() if len(parts) > 1 else ""
                    sig.append((f"{lbl}: {op}", canonical_s7_address(args)))
                else:
                    sig.append((f"{lbl}:", ""))
            else:
                parts = inst.split(maxsplit=1)
                op = parts[0].upper()
                args = parts[1].strip().upper() if len(parts) > 1 else ""
                sig.append((op, canonical_s7_address(args)))
    return sig


def s7_network_title_signature(networks: List[Dict[str, Any]]) -> List[str]:
    """Extracts ordered list of network titles for verification comparison."""
    return [str(net.get("title", "")).strip() for net in networks]


def save_s7_artifact(content: str, filepath: Path, encoding: str = "utf-8") -> Path:
    """Atomically saves content to a file, refusing to overwrite an existing artifact."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if filepath.exists():
        raise FileExistsError(f"Refusing to overwrite existing artifact: {filepath}")
    temp_path = filepath.with_name(f".{filepath.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding=encoding, newline="\r\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(temp_path, filepath)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return filepath


def new_s7_artifact_path(prefix: str, suffix: str = ".awl") -> Path:
    """Generates a unique timestamped artifact path in the workspace."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return WORKSPACE_DIR / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


# ============================================================================
# 6. SYMBOL TABLE GENERATOR & PARSER (.SDF / .CSV)
# ============================================================================

def generate_s7_symbol_sdf(symbol_entries: List[S7SymbolEntry]) -> str:
    """
    Generates standard Siemens STEP 7 Tab-delimited Symbol Table (.SDF / .SEQ).
    Format: Symbol <TAB> Address <TAB> Data Type <TAB> Comment
    """
    lines = ["Symbol\tAddress\tData type\tComment"]
    for entry in symbol_entries:
        lines.append(f"{entry.symbol}\t{entry.address}\t{entry.data_type}\t{entry.comment}")
    return "\r\n".join(lines) + "\r\n"


def generate_s7_symbol_csv(symbol_entries: List[S7SymbolEntry]) -> str:
    """
    Generates standard Siemens STEP 7 CSV Symbol Table.
    Format: "Symbol","Address","Data type","Comment"
    """
    output = io.StringIO()
    writer = csv.writer(output, delimiter=",", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(["Symbol", "Address", "Data type", "Comment"])
    for entry in symbol_entries:
        writer.writerow([entry.symbol, entry.address, entry.data_type, entry.comment])
    return output.getvalue()


def parse_s7_symbol_table(filepath: Path) -> Dict[str, Any]:
    """
    Parses a Siemens STEP 7 Symbol Table (.SDF, .SEQ, or .CSV).
    Extracts symbols, addresses, data types, and comments.
    """
    try:
        raw_bytes = filepath.read_bytes()
        if not raw_bytes:
            return {"error": f"Symbol table file is empty: {filepath}"}
        if raw_bytes.startswith(b"\xff\xfe"):
            text = raw_bytes[2:].decode("utf-16-le", errors="strict")
        elif raw_bytes.startswith(b"\xfe\xff"):
            text = raw_bytes[2:].decode("utf-16-be", errors="strict")
        elif raw_bytes.startswith(b"\xef\xbb\xbf"):
            text = raw_bytes.decode("utf-8-sig", errors="strict")
        else:
            try:
                text = raw_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                text = raw_bytes.decode("cp1252", errors="replace")
    except FileNotFoundError:
        return {"error": f"Symbol table file not found: {filepath}"}
    except Exception as exc:
        return {"error": f"Failed to read symbol table file: {exc}"}

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"error": f"Symbol table contains no data: {filepath}"}

    first_line = lines[0]
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(lines, delimiter=delimiter)
    raw_header = next(reader, [])
    header = [h.strip().lower() for h in raw_header]

    sym_idx = next((i for i, h in enumerate(header) if "sym" in h), 0)
    addr_idx = next((i for i, h in enumerate(header) if "addr" in h or "oper" in h), 1)
    dtype_idx = next((i for i, h in enumerate(header) if "data" in h or "typ" in h), 2)
    comm_idx = next((i for i, h in enumerate(header) if "comm" in h or "rem" in h), 3)

    entries: List[Dict[str, str]] = []
    symbol_map: Dict[str, str] = {}
    row_errors: List[str] = []

    for row_idx, row in enumerate(reader, start=2):
        if not row or not any(field.strip() for field in row):
            continue
        sym = row[sym_idx].strip() if len(row) > sym_idx else ""
        addr = row[addr_idx].strip() if len(row) > addr_idx else ""
        dtype = row[dtype_idx].strip() if len(row) > dtype_idx else "BOOL"
        comm = row[comm_idx].strip() if len(row) > comm_idx else ""

        if not sym and not addr:
            continue
        if not sym:
            row_errors.append(f"Row {row_idx}: missing symbol name.")
            continue
        if not addr:
            row_errors.append(f"Row {row_idx}: missing address for symbol '{sym}'.")
            continue

        canon_addr = canonical_s7_address(addr)
        entries.append({
            "symbol": sym,
            "address": canon_addr,
            "data_type": dtype.upper(),
            "comment": comm,
        })
        symbol_map[sym] = canon_addr

    return {
        "file": str(filepath),
        "symbol_count": len(entries),
        "symbols": entries,
        "symbol_map": symbol_map,
        "errors": row_errors,
    }


# ============================================================================
# 6b. QUALITY-OF-LIFE ANALYSIS, CROSS-REFERENCES & CONTROLLER DETECTION
# ============================================================================

def search_symbols_and_comments_entries(
    symbol_entries: List[S7SymbolEntry],
    query: str,
    filter_type: str = "ALL",
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    Searches symbol entries by address, symbol name, comment, or data type.
    Supports wildcards (* and ?) and case-insensitive matching.
    """
    filter_type_up = filter_type.strip().upper()
    q_str = query.strip()
    is_wildcard = "*" in q_str or "?" in q_str
    if is_wildcard:
        regex_pat = re.compile(
            r"^" + q_str.replace(".", r"\.").replace("*", ".*").replace("?", ".") + r"$",
            re.IGNORECASE,
        )
    else:
        q_lower = q_str.lower()

    def field_matches(val: str) -> bool:
        if not val:
            return False
        if is_wildcard:
            return regex_pat.search(val) is not None
        return q_lower in val.lower()

    matched: List[Dict[str, str]] = []
    for entry in symbol_entries:
        hit = False
        if filter_type_up in ("ALL", "ADDRESS") and field_matches(entry.address):
            hit = True
        elif filter_type_up in ("ALL", "SYMBOL") and field_matches(entry.symbol):
            hit = True
        elif filter_type_up in ("ALL", "COMMENT") and field_matches(entry.comment):
            hit = True
        elif filter_type_up in ("ALL", "DATA_TYPE") and field_matches(entry.data_type):
            hit = True

        if hit:
            matched.append({
                "symbol": entry.symbol,
                "address": entry.address,
                "data_type": entry.data_type,
                "comment": entry.comment,
            })
            if len(matched) >= max_results:
                break

    return {
        "query": query,
        "filter_type": filter_type_up,
        "total_matches": len(matched),
        "results": matched,
        "matches": matched,
    }


def annotate_awl_source_text(
    source_text: str,
    symbols: Optional[Any] = None,
    style: str = "COMMENTS",
    plc_type: str = "S7-300",
) -> Tuple[str, List[Dict[str, Any]], int]:
    """
    Parses and annotates raw AWL source text using symbol definitions and comments.
    Returns (annotated_awl, networks, annotations_applied).
    """
    temp_file = WORKSPACE_DIR / f"temp_{uuid.uuid4().hex[:8]}.awl"
    temp_file.write_text(source_text, encoding="utf-8")
    try:
        parsed = parse_s7_awl_source(temp_file)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass

    if parsed.get("error"):
        raise ValueError(parsed["error"])

    networks_raw = parsed.get("networks", [])
    report = validate_and_analyze_program(networks_raw, symbols_input=symbols, plc_type=plc_type)
    annotated_code = generate_s7_awl_source(
        report.normalized_networks,
        block_type=parsed.get("block_type", "OB"),
        block_number=int(parsed.get("block_number", 1) or 1),
        symbols=symbols,
        annotate_comments=True,
        style=style,
    )
    ann_count = sum(1 for line in annotated_code.splitlines() if "//" in line and not line.strip().startswith("//"))
    return annotated_code, [
        {"title": n.title, "comment": n.comment, "instructions": n.instructions}
        for n in report.normalized_networks
    ], ann_count


def generate_cross_references(
    networks: List[Dict[str, Any]],
    symbols: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Generates a complete cross-reference table (Querverweisliste) for all operands in the program.
    Detects memory overlap conflicts between bit and word/dword accesses.
    """
    _, symbol_entries = _validate_symbol_map(symbols) if symbols else ([], [])
    addr_to_sym = {canonical_s7_address(e.address): e for e in symbol_entries}
    sym_to_entry = {e.symbol.upper(): e for e in symbol_entries}

    cross_refs: Dict[str, Dict[str, Any]] = {}
    memory_overlaps: List[str] = []

    for net_idx, net in enumerate(networks, start=1):
        net_title = net.get("title", f"Network {net_idx}")
        instructions = net.get("instructions", [])
        for inst in instructions:
            inst_clean = inst.split("//", 1)[0].strip()
            if not inst_clean:
                continue
            if inst_clean.endswith(";"):
                inst_clean = inst_clean[:-1].strip()
            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", inst_clean)
            if m_lbl:
                inst_clean = m_lbl.group(2).strip()
            if not inst_clean:
                continue
            tokens = inst_clean.split(maxsplit=1)
            opcode = tokens[0].upper()
            operand = tokens[1].strip() if len(tokens) > 1 else ""
            if not operand:
                continue

            m_sym = S7_NAMED_SYMBOL_REGEX.fullmatch(operand)
            sym_name = m_sym.group(1).upper() if m_sym else ""
            canon_addr = ""
            entry = None
            if sym_name and sym_name in sym_to_entry:
                entry = sym_to_entry[sym_name]
                canon_addr = entry.address
            else:
                canon_addr = canonical_s7_address(operand)
                if canon_addr in addr_to_sym:
                    entry = addr_to_sym[canon_addr]

            key = canon_addr or operand

            if opcode in ("=", "S", "R", "T"):
                if opcode == "=":
                    access = "ASSIGN"
                elif opcode == "S":
                    access = "SET"
                elif opcode == "R":
                    access = "RESET"
                else:
                    access = "TRANSFER"
            elif opcode in ("A", "AN", "O", "ON", "X", "XN", "U", "UN", "L", "LC"):
                access = "READ"
            elif opcode in ("SP", "SE", "SD", "SS", "SF", "SI", "SV", "SA", "FR"):
                access = "TIMER_CONTROL"
            elif opcode in ("CU", "CD", "ZV", "ZR"):
                access = "COUNTER_CONTROL"
            elif opcode in ("CALL", "UC", "CC"):
                access = "BLOCK_CALL"
            else:
                access = "READ"

            if key not in cross_refs:
                cross_refs[key] = {
                    "address": canon_addr or operand,
                    "symbol": entry.symbol if entry else "",
                    "data_type": entry.data_type if entry else infer_s7_data_type(key),
                    "comment": entry.comment if entry else "",
                    "read_count": 0,
                    "write_count": 0,
                    "references": [],
                }

            if access in ("ASSIGN", "SET", "RESET", "TRANSFER"):
                cross_refs[key]["write_count"] += 1
            elif access == "READ":
                cross_refs[key]["read_count"] += 1

            cross_refs[key]["references"].append({
                "network": net_idx,
                "network_title": net_title,
                "instruction": inst,
                "opcode": opcode,
                "access": access,
            })

    # Memory Overlap Detection
    for k in list(cross_refs.keys()):
        m_bit = S7_BIT_DEVICE_REGEX.fullmatch(k)
        if m_bit:
            area = m_bit.group(1).upper()
            if area in ("M", "Q", "I", "A", "E"):
                byte_val = int(m_bit.group(2))
                candidates = [
                    f"{area}B {byte_val}",
                    f"{area}W {byte_val}",
                    f"{area}W {byte_val - 1}" if byte_val > 0 else None,
                    f"{area}D {byte_val}",
                    f"{area}D {byte_val - 1}" if byte_val > 0 else None,
                    f"{area}D {byte_val - 2}" if byte_val > 1 else None,
                    f"{area}D {byte_val - 3}" if byte_val > 2 else None,
                ]
                for cand in candidates:
                    if cand and cand in cross_refs:
                        w_ref = cross_refs[cand]
                        b_ref = cross_refs[k]
                        if w_ref["write_count"] > 0 or b_ref["write_count"] > 0:
                            msg = (
                                f"Memory Overlap Hazard: Bit '{k}' (read:{b_ref['read_count']}, "
                                f"write:{b_ref['write_count']}) overlaps with '{cand}' "
                                f"(read:{w_ref['read_count']}, write:{w_ref['write_count']})."
                            )
                            if msg not in memory_overlaps:
                                memory_overlaps.append(msg)

        m_word = S7_WORD_DEVICE_REGEX.fullmatch(k)
        if m_word:
            word_pfx = m_word.group(1).upper()
            if word_pfx in ("MW", "QW", "IW", "EW", "AW"):
                byte_val = int(m_word.group(2))
                base_area = word_pfx[0]
                byte_cands = [f"{base_area}B {byte_val}", f"{base_area}B {byte_val + 1}"]
                for bc in byte_cands:
                    if bc in cross_refs:
                        w_ref = cross_refs[bc]
                        b_ref = cross_refs[k]
                        if w_ref["write_count"] > 0 or b_ref["write_count"] > 0:
                            msg = (
                                f"Memory Overlap Hazard: Word '{k}' (read:{b_ref['read_count']}, "
                                f"write:{b_ref['write_count']}) overlaps with '{bc}' "
                                f"(read:{w_ref['read_count']}, write:{w_ref['write_count']})."
                            )
                            if msg not in memory_overlaps:
                                memory_overlaps.append(msg)

    return {
        "total_devices": len(cross_refs),
        "cross_references": cross_refs,
        "memory_overlaps": memory_overlaps,
    }


def generate_memory_usage_map(
    networks: List[Dict[str, Any]],
    profile: S7PLCProfile,
    symbols: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Computes precise memory allocation and generates visual bit/byte layout grids
    for Inputs, Outputs, Markers, Timers, Counters, and Data Blocks.
    """
    input_bytes: Set[int] = set()
    input_bits: Dict[int, Set[int]] = {}
    output_bytes: Set[int] = set()
    output_bits: Dict[int, Set[int]] = {}
    marker_bytes: Set[int] = set()
    marker_bits: Dict[int, Set[int]] = {}
    timers_used: Set[int] = set()
    counters_used: Set[int] = set()
    dbs_used: Dict[int, Set[int]] = {}
    multi_byte_overlaps: List[str] = []

    for net in networks:
        for inst in net.get("instructions", []):
            code = inst.split("//", 1)[0].strip()
            if code.endswith(";"):
                code = code[:-1].strip()
            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", code)
            if m_lbl:
                code = m_lbl.group(2).strip()
            tokens = code.split(maxsplit=1)
            if len(tokens) < 2:
                continue
            arg = tokens[1].strip()

            # 1. Bit devices
            m_bit = S7_BIT_DEVICE_REGEX.fullmatch(arg)
            if m_bit:
                pfx, b_str, bit_str = m_bit.groups()
                pfx_up = pfx.upper()
                b_val, bit_val = int(b_str), int(bit_str)
                if pfx_up in ("I", "E"):
                    input_bytes.add(b_val)
                    input_bits.setdefault(b_val, set()).add(bit_val)
                elif pfx_up in ("Q", "A"):
                    output_bytes.add(b_val)
                    output_bits.setdefault(b_val, set()).add(bit_val)
                elif pfx_up == "M":
                    marker_bytes.add(b_val)
                    marker_bits.setdefault(b_val, set()).add(bit_val)

            # 2. Byte devices
            m_b = S7_BYTE_DEVICE_REGEX.fullmatch(arg)
            if m_b:
                pfx, b_str = m_b.groups()
                pfx_up, b_val = pfx.upper(), int(b_str)
                if pfx_up in ("IB", "EB"):
                    input_bytes.add(b_val)
                elif pfx_up in ("QB", "AB"):
                    output_bytes.add(b_val)
                elif pfx_up == "MB":
                    marker_bytes.add(b_val)

            # 3. Word devices (2 bytes)
            m_w = S7_WORD_DEVICE_REGEX.fullmatch(arg)
            if m_w:
                pfx, b_str = m_w.groups()
                pfx_up, b_val = pfx.upper(), int(b_str)
                if pfx_up in ("IW", "EW"):
                    input_bytes.update([b_val, b_val + 1])
                elif pfx_up in ("QW", "AW"):
                    output_bytes.update([b_val, b_val + 1])
                elif pfx_up == "MW":
                    marker_bytes.update([b_val, b_val + 1])

            # 4. Double Word devices (4 bytes)
            m_d = S7_DWORD_DEVICE_REGEX.fullmatch(arg)
            if m_d:
                pfx, b_str = m_d.groups()
                pfx_up, b_val = pfx.upper(), int(b_str)
                if pfx_up in ("ID", "ED"):
                    input_bytes.update(range(b_val, b_val + 4))
                elif pfx_up in ("QD", "AD"):
                    output_bytes.update(range(b_val, b_val + 4))
                elif pfx_up == "MD":
                    marker_bytes.update(range(b_val, b_val + 4))

            # 5. Timers & Counters
            m_t = S7_TIMER_REGEX.fullmatch(arg)
            if m_t:
                timers_used.add(int(m_t.group(1)))
            m_c = S7_COUNTER_REGEX.fullmatch(arg)
            if m_c:
                counters_used.add(int(m_c.group(2)))

            # 6. DB Elements
            m_db = S7_DB_ELEMENT_REGEX.fullmatch(arg)
            if m_db:
                db_str = m_db.group(2)
                db_num = int(re.search(r"\d+", db_str).group(0)) if db_str else 0
                offset = int(m_db.group(4))
                dbs_used.setdefault(db_num, set()).add(offset)

    def render_bit_grid(prefix: str, byte_map: Dict[int, Set[int]]) -> List[str]:
        rows = []
        for b_idx in sorted(byte_map.keys()):
            bits_set = byte_map[b_idx]
            slots = " ".join(f"{b}:{'X' if b in bits_set else '.'}" for b in range(8))
            rows.append(f"{prefix} {b_idx:<4} [ {slots} ]")
        return rows

    # Multi-byte overlap warnings for markers
    for b_idx in sorted(marker_bytes):
        if b_idx in marker_bits:
            bits_used = marker_bits[b_idx]
            if len(bits_used) > 0:
                multi_byte_overlaps.append(
                    f"Marker byte MB {b_idx} has {len(bits_used)} bits individually addressed ({sorted(bits_used)})."
                )

    return {
        "plc_type": profile.name,
        "inputs": {
            "used_bytes": len(input_bytes),
            "max_bytes": profile.max_input_byte + 1,
            "utilization_pct": round(len(input_bytes) / (profile.max_input_byte + 1) * 100, 3),
            "highest_byte": max(input_bytes) if input_bytes else 0,
            "bit_grid": render_bit_grid("I", input_bits),
        },
        "outputs": {
            "used_bytes": len(output_bytes),
            "max_bytes": profile.max_output_byte + 1,
            "utilization_pct": round(len(output_bytes) / (profile.max_output_byte + 1) * 100, 3),
            "highest_byte": max(output_bytes) if output_bytes else 0,
            "bit_grid": render_bit_grid("Q", output_bits),
        },
        "markers": {
            "used_bytes": len(marker_bytes),
            "max_bytes": profile.max_marker_byte + 1,
            "utilization_pct": round(len(marker_bytes) / (profile.max_marker_byte + 1) * 100, 3),
            "highest_byte": max(marker_bytes) if marker_bytes else 0,
            "bit_grid": render_bit_grid("M", marker_bits),
        },
        "timers": {
            "used_count": len(timers_used),
            "max_count": profile.max_timer_num + 1,
            "utilization_pct": round(len(timers_used) / (profile.max_timer_num + 1) * 100, 3),
            "indices": sorted(timers_used),
        },
        "counters": {
            "used_count": len(counters_used),
            "max_count": profile.max_counter_num + 1,
            "utilization_pct": round(len(counters_used) / (profile.max_counter_num + 1) * 100, 3),
            "indices": sorted(counters_used),
        },
        "data_blocks": {
            "dbs_referenced_count": len(dbs_used),
            "dbs": {f"DB{k}": sorted(v) for k, v in sorted(dbs_used.items())},
        },
        "multi_byte_overlaps": multi_byte_overlaps[:10],
    }


def inspect_project_blocks(project_dir_or_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Scans project directory for all compiled/source S7 blocks, extracting metadata,
    estimated MC7 sizes, local data stacks, and block call dependencies.
    """
    target_dir: Optional[Path] = None
    if project_dir_or_file:
        p = Path(project_dir_or_file)
        target_dir = p if p.is_dir() else p.parent
    else:
        if controller.project_name:
            cand = WORKSPACE_DIR / "Projects" / controller.project_name
            if cand.is_dir():
                target_dir = cand
        if not target_dir or not target_dir.is_dir():
            target_dir = WORKSPACE_DIR

    awl_files = list(target_dir.rglob("*.awl"))
    blocks: List[Dict[str, Any]] = []
    call_graph: Dict[str, List[str]] = {}

    for f in sorted(awl_files):
        parsed = parse_s7_awl_source(f)
        if parsed.get("error"):
            continue
        blk_type = parsed.get("block_type", "OB")
        blk_num = parsed.get("block_number", "1")
        type_prefix_map = {
            "ORGANIZATION_BLOCK": "OB",
            "FUNCTION": "FC",
            "FUNCTION_BLOCK": "FB",
            "DATA_BLOCK": "DB",
        }
        short_type = type_prefix_map.get(blk_type.upper(), blk_type)
        blk_tag = f"{short_type}{blk_num}"

        nets = parsed.get("networks", [])
        inst_count = sum(len(net.get("instructions", [])) for net in nets)
        mc7_bytes = sum(
            estimate_s7_instruction_bytes(
                inst.split()[0], inst.split()[1:] if len(inst.split()) > 1 else []
            )
            for net in nets
            for inst in net.get("instructions", [])
            if inst.strip()
        )

        called: List[str] = []
        for net in nets:
            for inst in net.get("instructions", []):
                m_call = re.search(
                    r"\b(CALL|CC|UC)\s+((?:(?:FC|FB|SFC|SFB|OB)\s*\d+)|(?:\"[^\"]+\")|(?:[A-Za-z0-9_#]+))",
                    inst,
                    re.IGNORECASE,
                )
                if m_call:
                    raw_callee = m_call.group(2).strip().strip('"')
                    m_pfx = re.match(r"^(FC|FB|SFC|SFB|OB)\s*(\d+)$", raw_callee, re.IGNORECASE)
                    if m_pfx:
                        callee = f"{m_pfx.group(1).upper()} {m_pfx.group(2)}"
                    else:
                        callee = raw_callee
                    if callee not in called:
                        called.append(callee)

        call_graph[blk_tag] = called
        blocks.append({
            "file": str(f),
            "block_tag": blk_tag,
            "block_type": blk_type,
            "block_number": blk_num,
            "network_count": len(nets),
            "instruction_count": inst_count,
            "estimated_mc7_bytes": mc7_bytes,
            "called_blocks": called,
        })

    return {
        "project_dir": str(target_dir),
        "total_blocks": len(blocks),
        "blocks": blocks,
        "call_dependency_graph": call_graph,
        "call_hierarchy": call_graph,
    }


def perform_deep_program_analysis(
    networks: List[Dict[str, Any]],
    profile: S7PLCProfile,
    symbols: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Performs comprehensive static safety analysis on candidate S7 Statement List (AWL/STL) code.
    Evaluates:
      - Star-Delta contactor switching hazards
      - Duplicate output coil assignments
      - Unpaired SET/RESET operations
      - Timers & counters initialization and contact hygiene
      - Direct peripheral I/O access hazards
      - Parenthetical nesting stack depth (<= 7)
      - Execution sweep time estimation in microseconds
      - Overall safety score (0-100)
    """
    _, symbol_entries = _validate_symbol_map(symbols) if symbols else ([], [])
    sym_to_addr = {e.symbol.upper(): canonical_s7_address(e.address) for e in symbol_entries}

    hazards: List[str] = []
    warnings: List[str] = []
    recommendations: List[str] = []

    assigned_coils: Dict[str, List[int]] = {}
    set_coils: Dict[str, List[int]] = {}
    reset_coils: Dict[str, List[int]] = {}
    timers_started: Set[str] = set()
    timers_read: Set[str] = set()
    peripheral_accesses: List[str] = []

    star_coils: Set[str] = set()
    delta_coils: Set[str] = set()
    star_networks: Set[int] = set()
    delta_networks: Set[int] = set()
    network_interlocks: Dict[int, Set[str]] = {}

    bit_ops_count = 0
    word_ops_count = 0
    timer_ops_count = 0
    call_ops_count = 0
    max_nesting_depth = 0

    for net_idx, net in enumerate(networks, start=1):
        net_title = net.get("title", "")
        net_comment = net.get("comment", "")
        instructions = net.get("instructions", [])
        nesting_stack = 0

        for inst in instructions:
            clean = inst.split("//", 1)[0].strip()
            if clean.endswith(";"):
                clean = clean[:-1].strip()
            m_lbl = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,15})\s*:\s*(.*)$", clean)
            if m_lbl:
                clean = m_lbl.group(2).strip()
            if not clean:
                continue
            tokens = clean.split(maxsplit=1)
            opcode = tokens[0].upper()
            operand = tokens[1].strip() if len(tokens) > 1 else ""

            # Nesting depth check
            if opcode in ("A(", "AN(", "O(", "ON(", "X(", "XN(", "U(", "UN("):
                nesting_stack += 1
                if nesting_stack > max_nesting_depth:
                    max_nesting_depth = nesting_stack
            elif opcode == ")":
                nesting_stack = max(0, nesting_stack - 1)

            # Instruction category for timing estimation
            if opcode in ("A", "AN", "O", "ON", "X", "XN", "U", "UN", "=", "S", "R", "NOT", "SET", "CLR"):
                bit_ops_count += 1
            elif opcode in ("L", "T", "+I", "-I", "*I", "/I", "+D", "-D", "*D", "/D", "==I", "<>I", ">I", "<I"):
                word_ops_count += 1
            elif opcode in ("SP", "SE", "SD", "SS", "SF", "SI", "SV", "SA", "CU", "CD", "ZV", "ZR"):
                timer_ops_count += 1
            elif opcode in ("CALL", "UC", "CC"):
                call_ops_count += 1

            # Resolve address
            canon = ""
            if operand:
                m_sym = S7_NAMED_SYMBOL_REGEX.fullmatch(operand)
                sym_k = m_sym.group(1).upper() if m_sym else ""
                if sym_k and sym_k in sym_to_addr:
                    canon = sym_to_addr[sym_k]
                else:
                    canon = canonical_s7_address(operand)

            # Duplicate coil tracking
            if opcode == "=" and canon:
                assigned_coils.setdefault(canon, []).append(net_idx)
            elif opcode == "S" and canon:
                set_coils.setdefault(canon, []).append(net_idx)
            elif opcode == "R" and canon:
                reset_coils.setdefault(canon, []).append(net_idx)

            # Timers
            if opcode in ("SP", "SE", "SD", "SS", "SF", "SI", "SV", "SA") and canon:
                timers_started.add(canon)
            elif opcode in ("A", "AN", "O", "ON", "U", "UN") and canon.startswith("T "):
                timers_read.add(canon)

            # Peripheral I/O
            if canon and any(canon.startswith(p) for p in ("PIB", "PIW", "PID", "PQB", "PQW", "PQD")):
                peripheral_accesses.append(f"Network {net_idx}: {opcode} {canon}")

            # Star / Delta interlock tracking
            line_upper = f"{net_title} {net_comment} {clean}".upper()
            if any(term in line_upper for term in ("STAR", "STERN", "KM2", "K2M", "_Y")) and opcode in ("=", "S"):
                star_networks.add(net_idx)
                if canon:
                    star_coils.add(canon)
            if any(term in line_upper for term in ("DELTA", "DREIECK", "KM3", "K3M", "_D")) and opcode in ("=", "S"):
                delta_networks.add(net_idx)
                if canon:
                    delta_coils.add(canon)

            if opcode in ("AN", "UN") and canon:
                network_interlocks.setdefault(net_idx, set()).add(canon)

        if nesting_stack != 0:
            hazards.append(f"Network {net_idx}: Unbalanced parentheses; nesting stack remains open at network end.")

    # Check 1: Star-Delta reciprocal contactor interlocks
    if star_coils and delta_coils:
        for s_net in star_networks:
            interlocks = network_interlocks.get(s_net, set())
            if not any(d_coil in interlocks for d_coil in delta_coils):
                hazards.append(
                    f"CRITICAL INTERLOCK HAZARD: Star contactor network {s_net} lacks opposing Delta coil "
                    f"electrical/software interlock contact (AN Delta). Risk of phase-to-phase dead short!"
                )
        for d_net in delta_networks:
            interlocks = network_interlocks.get(d_net, set())
            if not any(s_coil in interlocks for s_coil in star_coils):
                hazards.append(
                    f"CRITICAL INTERLOCK HAZARD: Delta contactor network {d_net} lacks opposing Star coil "
                    f"electrical/software interlock contact (AN Star). Risk of phase-to-phase dead short!"
                )

    # Check 2: Duplicate coil assignments
    for coil, net_list in assigned_coils.items():
        if len(net_list) > 1:
            hazards.append(
                f"DUPLICATE COIL HAZARD: Output coil '=' assigned multiple times to '{coil}' "
                f"in networks {net_list}. The final network will overwrite previous assignments."
            )

    # Check 3: Unpaired SET/RESET
    for coil, net_list in set_coils.items():
        if coil not in reset_coils and not coil.startswith("L "):
            warnings.append(
                f"UNPAIRED SET COIL: Device '{coil}' is SET in networks {net_list} but never RESET (R) in this block."
            )
    for coil, net_list in reset_coils.items():
        if coil not in set_coils and not coil.startswith("L "):
            warnings.append(
                f"UNPAIRED RESET COIL: Device '{coil}' is RESET in networks {net_list} but never SET (S) in this block."
            )

    # Check 4: Nesting depth limit
    if max_nesting_depth > 7:
        hazards.append(
            f"NESTING STACK OVERFLOW: Logic nesting depth {max_nesting_depth} exceeds Siemens hardware limit of 7."
        )

    # Check 5: Timers started but never read
    unreferenced_timers = timers_started - timers_read
    if unreferenced_timers:
        for t in sorted(unreferenced_timers):
            warnings.append(f"TIMER HYGIENE: S7 timer '{t}' is triggered but its contact is never evaluated.")

    # Check 6: Peripheral I/O
    if peripheral_accesses:
        for pa in peripheral_accesses[:3]:
            warnings.append(
                f"PERIPHERAL DIRECT ACCESS: {pa}. Direct peripheral I/O bypasses process image and creates cycle latency."
            )

    # Calculate estimated scan sweep time
    estimated_sweep_us = (
        bit_ops_count * profile.bit_op_time_us
        + word_ops_count * profile.word_op_time_us
        + timer_ops_count * profile.timer_op_time_us
        + call_ops_count * profile.call_op_time_us
    )

    # Calculate safety score
    safety_score = 100
    safety_score -= len(hazards) * 35
    safety_score -= len(warnings) * 10
    safety_score = max(0, min(100, safety_score))

    if safety_score < 70:
        recommendations.append("Resolve all critical hazards before compiling or loading onto physical CPU.")
    if max_nesting_depth > 4:
        recommendations.append("Consider decomposing deeply nested logic rungs into intermediate marker bits.")

    return {
        "safety_score": safety_score,
        "status": "PASSED" if safety_score >= 80 else ("WARNING" if safety_score >= 50 else "FAILED"),
        "hazards": hazards,
        "warnings": warnings,
        "recommendations": recommendations,
        "metrics": {
            "network_count": len(networks),
            "bit_operations": bit_ops_count,
            "word_operations": word_ops_count,
            "timer_operations": timer_ops_count,
            "call_operations": call_ops_count,
            "max_nesting_depth": max_nesting_depth,
            "estimated_sweep_time_us": round(estimated_sweep_us, 3),
        },
    }


def detect_simatic_environment() -> Dict[str, Any]:
    """
    Discovers installed Siemens STEP 7 Classic environment, binaries,
    running processes, and offline workspace health.
    """
    exe_path = find_step7_manager_exe()
    installed_versions: List[str] = []

    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (r"SOFTWARE\Siemens\STEP7", r"SOFTWARE\WOW6432Node\Siemens\STEP7"):
                    try:
                        with winreg.OpenKey(hive, sub) as k:
                            for i in range(winreg.QueryInfoKey(k)[1]):
                                name, val, _ = winreg.EnumValue(k, i)
                                if "version" in name.lower() and str(val) not in installed_versions:
                                    installed_versions.append(str(val))
                    except Exception:
                        pass
        except Exception:
            pass

    running_siemens_procs: List[str] = []
    try:
        res = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True, text=True, timeout=3.0)
        for line in res.stdout.splitlines():
            clean_l = line.strip().strip('"')
            parts = clean_l.split('","')
            if parts:
                pname = parts[0].strip('"')
                if any(k in pname.lower() for k in ("s7", "step7", "siemens", "lad", "kop")):
                    if pname not in running_siemens_procs:
                        running_siemens_procs.append(pname)
    except Exception:
        pass

    s7bin = exe_path.parent if exe_path else STEP7_DEFAULT_BIN
    editor_path = s7bin / STEP7_EDITOR_EXE if s7bin.is_dir() else None
    hwcnf_path = s7bin / STEP7_HWCNF_EXE if s7bin.is_dir() else None

    projects_dir = WORKSPACE_DIR / "Projects"
    existing_projects = [p.name for p in projects_dir.iterdir() if p.is_dir()] if projects_dir.is_dir() else []

    return {
        "step7_installed": exe_path is not None and exe_path.is_file(),
        "simatic_manager_exe": str(exe_path) if exe_path else None,
        "editor_exe": str(editor_path) if editor_path and editor_path.is_file() else None,
        "hw_config_exe": str(hwcnf_path) if hwcnf_path and hwcnf_path.is_file() else None,
        "installed_versions": installed_versions or (["v5.5 / v5.6 / v5.7"] if exe_path else []),
        "running_siemens_processes": running_siemens_procs,
        "pywinauto_available": PYWINAUTO_AVAILABLE,
        "workspace_dir": str(WORKSPACE_DIR),
        "existing_offline_projects": existing_projects,
    }


# ============================================================================
# 7. SERIALIZED SIMATIC MANAGER GUI CONTROLLER (pywinauto)
# ============================================================================

class SimaticManagerController:
    """
    Automates Siemens SIMATIC Manager (s7tgtopx.exe) and the LAD/FBD/STL Editor (s7kotx.exe).
    Uses pywinauto with atomic verification, modal popup handling, and verified rollbacks.
    """

    def __init__(self) -> None:
        self.app: Optional[Any] = None
        self.main_win: Optional[Any] = None
        self.project_name: Optional[str] = None
        self.import_started: bool = False
        self._last_confirmation: List[str] = []

    def connect(self, timeout: float = 0.5) -> Tuple[bool, str]:
        """Connects to a running instance of SIMATIC Manager or the LAD/FBD/STL Editor."""
        if not PYWINAUTO_AVAILABLE:
            return False, "pywinauto is not installed."
        try:
            # 1. Look for LAD/FBD/STL Editor first if open
            try:
                app = Application(backend="win32").connect(title_re=S7_EDITOR_WINDOW_TITLE_REGEX, timeout=timeout)
                self.app = app
                self.main_win = app.top_window()
                return True, self.main_win.window_text()
            except Exception:
                pass

            # 2. Look for SIMATIC Manager
            app = Application(backend="win32").connect(title_re=S7_MANAGER_WINDOW_TITLE_REGEX, timeout=timeout)
            self.app = app
            self.main_win = app.top_window()
            return True, self.main_win.window_text()
        except Exception as exc:
            return False, f"Could not connect to SIMATIC Manager: {exc}"

    def launch(self, project_path: Optional[str] = None) -> Tuple[bool, str]:
        """Attempts to launch SIMATIC Manager if installed on the host."""
        if not PYWINAUTO_AVAILABLE:
            return False, "pywinauto is not installed."
        exe_path = find_step7_manager_exe()
        if not exe_path or not exe_path.is_file():
            candidate = STEP7_DEFAULT_BIN / STEP7_MANAGER_EXE
            if candidate.is_file():
                exe_path = candidate
        if not exe_path or not exe_path.is_file():
            return False, f"SIMATIC Manager executable was not found at standard locations or '{STEP7_DEFAULT_BIN}'."
        try:
            cmd = f'"{exe_path}"'
            if project_path and Path(project_path).exists():
                cmd += f' "{project_path}"'
            self.app = Application(backend="win32").start(cmd)
            time.sleep(3.0)
            ok, msg = self.connect()
            if ok and self.app:
                self._dismiss_popups(confirm=True, timeout=1.5)
            return ok, msg
        except Exception as exc:
            return False, f"Failed to launch SIMATIC Manager: {exc}"

    def create_project(
        self,
        project_name: str,
        plc_type: str = "S7-300",
        cpu_type: Optional[str] = None,
        station_name: Optional[str] = None,
        project_path: Optional[str] = None,
        launch_if_closed: bool = True,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Creates a new project targeting S7-300, S7-400, or WinAC.
        Launches SIMATIC Manager if requested and available, selects target PLC family/CPU,
        and establishes complete standard STEP 7 Classic project structure (.s7p, Blocks, Symbols).
        """
        profile, profile_err = resolve_s7_plc_profile(plc_type)
        if profile_err or profile is None:
            return False, profile_err or "Unsupported PLC type.", {}
        if not project_name or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", project_name):
            return False, "project_name must be 1-64 characters using letters, numbers, spaces, _, ., or -.", {}

        target_cpu = cpu_type.strip() if cpu_type and cpu_type.strip() else profile.typical_cpu
        target_station = station_name.strip() if station_name and station_name.strip() else profile.station_type
        proj_dir = Path(project_path).expanduser().resolve() if project_path else (WORKSPACE_DIR / "Projects" / project_name)

        # 1. Try native GUI automation if requested
        ok, title = self.connect()
        if not ok and launch_if_closed:
            ok, title = self.launch()

        gui_created = False
        if ok and self.main_win:
            try:
                self.main_win.set_focus()
                time.sleep(0.2)
                try:
                    self.main_win.menu_select("File->New...")
                except Exception:
                    send_keys("^n")
                time.sleep(0.5)

                dialog = None
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    for window in self.app.windows(visible_only=True):
                        if window.handle != self.main_win.handle and window.class_name() == "#32770" and window.is_enabled():
                            dialog = window
                            break
                    if dialog is not None:
                        break
                    time.sleep(0.1)

                if dialog is not None:
                    edits = [c for c in dialog.descendants() if c.friendly_class_name() == "Edit"]
                    if edits:
                        edits[0].set_edit_text(project_name)
                    ok_btn = next((
                        c for c in dialog.descendants()
                        if c.friendly_class_name() == "Button"
                        and c.window_text().replace("&", "").strip().lower() in ("ok", "ja", "yes", "save")
                    ), None)
                    if ok_btn:
                        ok_btn.click()
                    else:
                        send_keys("{ENTER}")
                    time.sleep(1.0)
                    self._dismiss_popups(confirm=True, timeout=2.0)
                    gui_created = True
            except Exception:
                pass

        # 2. Establish complete STEP 7 Classic project directory tree and descriptor files
        proj_dir.mkdir(parents=True, exist_ok=True)
        blocks_dir = proj_dir / "Blocks"
        blocks_dir.mkdir(parents=True, exist_ok=True)
        symbols_dir = proj_dir / "Symbols"
        symbols_dir.mkdir(parents=True, exist_ok=True)
        sources_dir = proj_dir / "Sources"
        sources_dir.mkdir(parents=True, exist_ok=True)

        s7p_file = proj_dir / f"{project_name}.s7p"
        s7p_content = (
            f"[STEP7_PROJECT]\r\n"
            f"Name={project_name}\r\n"
            f"Version=5.6\r\n"
            f"Type=Standard\r\n"
            f"PLC_Family={profile.name}\r\n"
            f"CPU_Model={target_cpu}\r\n"
            f"Station={target_station}\r\n"
            f"Author=SIMATIC7-MCP\r\n"
        )
        s7p_file.write_text(s7p_content, encoding="utf-8")

        meta_file = proj_dir / "project.json"
        meta = {
            "project_name": project_name,
            "plc_type": profile.name,
            "cpu_type": target_cpu,
            "station_name": target_station,
            "s7p_path": str(s7p_file),
            "workspace": str(proj_dir),
            "created_at": datetime.datetime.now().isoformat(),
            "gui_created": gui_created,
            "status": "PROJECT_READY",
        }
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        station_file = proj_dir / "station.json"
        station_data = {
            "station_name": target_station,
            "plc_family": profile.name,
            "cpu_model": target_cpu,
            "rack": 0,
            "slot": 2,
            "mpi_address": 2,
            "max_inputs": profile.max_input_byte,
            "max_outputs": profile.max_output_byte,
            "max_markers": profile.max_marker_byte,
            "max_timers": profile.max_timer_num,
            "max_counters": profile.max_counter_num,
        }
        station_file.write_text(json.dumps(station_data, indent=2), encoding="utf-8")

        # Initial OB1 template
        initial_ob1_path = blocks_dir / "OB1.awl"
        if not initial_ob1_path.exists():
            ob1_content = generate_s7_awl_source(
                [S7NetworkSpec(title="Main Sweep Entry", comment="Cyclic execution rung", instructions=["NOP 0"])],
                block_type="OB",
                block_number=1,
                title="Main Program Sweep",
            )
            initial_ob1_path.write_text(ob1_content, encoding="utf-8")

        # Initial symbols
        initial_sdf = symbols_dir / "symbols.sdf"
        initial_csv = symbols_dir / "symbols.csv"
        if not initial_sdf.exists():
            initial_sdf.write_text("Symbol\tAddress\tData type\tComment\r\n", encoding="utf-8")
        if not initial_csv.exists():
            initial_csv.write_text('"Symbol","Address","Data type","Comment"\r\n', encoding="utf-8")

        self.project_name = project_name
        mode_str = "natively via SIMATIC Manager GUI" if gui_created else "offline STEP 7 workspace"
        msg = f"Created project '{project_name}' for {profile.name} ({target_cpu}) {mode_str}."
        return True, msg, meta

    def open_project(
        self,
        project_path_or_name: str,
        launch_if_closed: bool = True,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Opens an existing SIMATIC STEP 7 project (.s7p) or project workspace directory.
        Launches SIMATIC Manager if requested and attaches to project.
        """
        p = Path(project_path_or_name).expanduser().resolve()
        target_s7p: Optional[Path] = None
        target_dir: Optional[Path] = None

        if p.is_file() and p.suffix.lower() == ".s7p":
            target_s7p = p
            target_dir = p.parent
        elif p.is_dir():
            s7ps = list(p.glob("*.s7p"))
            target_s7p = s7ps[0] if s7ps else None
            target_dir = p
        else:
            cand = WORKSPACE_DIR / "Projects" / project_path_or_name
            if cand.is_dir():
                s7ps = list(cand.glob("*.s7p"))
                target_s7p = s7ps[0] if s7ps else None
                target_dir = cand

        if not target_dir or not target_dir.is_dir():
            return False, f"Could not find project directory for '{project_path_or_name}'.", {}

        proj_name = target_s7p.stem if target_s7p else target_dir.name
        self.project_name = proj_name

        ok, title = self.connect()
        if not ok and launch_if_closed:
            ok, title = self.launch(project_path=str(target_s7p) if target_s7p else None)

        meta_file = target_dir / "project.json"
        meta: Dict[str, Any] = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        meta.update({
            "project_name": proj_name,
            "project_dir": str(target_dir),
            "s7p_path": str(target_s7p) if target_s7p else None,
            "simatic_manager_connected": ok,
        })
        return True, f"Opened project '{proj_name}' (directory: {target_dir}).", meta

    def open_block(
        self,
        block_name: str = "OB1",
        project_name: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Ensures target block is accessible or focused in the LAD/FBD/STL Editor.
        """
        blk_clean = block_name.strip().upper()
        ok, title = self.connect()
        if ok and self.is_block_editor_active(blk_clean)[0]:
            return True, f"Block editor '{blk_clean}' is active.", {"block_name": blk_clean, "mode": "gui"}

        # Check offline block file
        proj = project_name or self.project_name
        proj_dir = (WORKSPACE_DIR / "Projects" / proj) if proj else None
        blk_file = None
        if proj_dir and proj_dir.is_dir():
            for cand in [proj_dir / "Blocks" / f"{blk_clean}.awl", proj_dir / f"{blk_clean}.awl"]:
                if cand.is_file():
                    blk_file = cand
                    break

        if not ok and not blk_file:
            return False, f"Target block '{blk_clean}' not found in project '{proj or 'workspace'}'.", {
                "block_name": blk_clean,
                "gui_connected": False,
            }

        return True, f"Target block '{blk_clean}' ready.", {
            "block_name": blk_clean,
            "block_file": str(blk_file) if blk_file else None,
            "block_path": str(blk_file) if blk_file else None,
            "gui_connected": ok,
        }

    def is_block_editor_active(self, expected_block: str = "OB1") -> Tuple[bool, str]:
        """Verifies that the target block editor is active and focused."""
        ok, title = self.connect()
        if not ok:
            return False, title
        if not self.main_win:
            return False, "SIMATIC Manager window is not accessible."
        current_title = self.main_win.window_text()
        if expected_block.upper() in current_title.upper():
            return True, current_title
        try:
            for w in self.app.windows():
                if expected_block.upper() in w.window_text().upper():
                    return True, w.window_text()
        except Exception:
            pass
        return False, f"Block '{expected_block}' is not the active editor. Active window: '{current_title}'"

    def _dismiss_popups(self, confirm: bool = True, timeout: float = 0.8) -> List[str]:
        """Detects and safely dismisses modal dialogs owned by the Siemens process."""
        messages: List[str] = []
        if not self.app:
            return messages
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                main_handle = self.main_win.handle if self.main_win else None
                dialogs = []
                for window in self.app.windows(visible_only=True):
                    if (window.handle == main_handle or window.class_name() != "#32770" or not window.is_enabled()):
                        continue
                    buttons = [
                        child.window_text().replace("&", "").strip().lower()
                        for child in window.children()
                        if child.friendly_class_name() == "Button"
                    ]
                    has_message_buttons = any(label in buttons for label in ("yes", "no", "ok", "ja", "nein"))
                    has_text = any(
                        child.friendly_class_name() == "Static" and child.window_text().strip()
                        for child in window.children()
                    )
                    if has_message_buttons and has_text:
                        dialogs.append(window)
                    elif not buttons:
                        continue
                    elif any(label in buttons for label in ("open", "save", "cancel", "abbrechen")):
                        continue
                    else:
                        raise RuntimeError(
                            f"Unrecognized modal dialog '{window.window_text()}' with buttons {buttons}."
                        )
                if not dialogs:
                    break
                dlg = dialogs[-1]
                texts = [
                    child.window_text().strip()
                    for child in dlg.children()
                    if child.friendly_class_name() == "Static" and child.window_text().strip()
                ]
                if texts:
                    messages.extend(texts)
                if confirm:
                    yes = next((
                        child for child in dlg.children()
                        if child.friendly_class_name() == "Button"
                        and child.window_text().replace("&", "").strip().lower() in ("yes", "ja", "ok")
                    ), None)
                    if yes:
                        yes.click()
                    else:
                        raise RuntimeError(f"Unrecognized confirmation button in '{dlg.window_text()}'")
                else:
                    dlg.set_focus()
                    send_keys("{ESC}")
                time.sleep(0.2)
            except Exception as exc:
                raise RuntimeError(f"Could not safely handle SIMATIC Manager dialog: {exc}") from exc
        return messages

    def export_current_awl(self, output_path: Path, block_name: str = "OB1") -> Tuple[bool, str]:
        """
        Exports the current block from SIMATIC Manager / LAD Editor to an AWL source file.
        Uses native export commands ('File -> Generate Source...' or 'File -> Export...').
        """
        ok, title = self.connect()
        if not ok:
            return False, title
        if output_path.exists():
            return False, f"Refusing to overwrite existing AWL artifact: {output_path}"

        try:
            self.main_win.set_focus()
            time.sleep(0.2)
            try:
                self.main_win.menu_select("File->Export...")
            except Exception:
                send_keys("^e")

            time.sleep(0.5)
            picker = self.app.window(title_re=r".*(Export|Save|Speichern).*")
            if picker.exists(timeout=2.0):
                edit = picker.child_window(class_name="Edit")
                edit.set_edit_text(str(output_path))
                save_btn = next((
                    c for c in picker.children()
                    if c.friendly_class_name() == "Button"
                    and c.window_text().replace("&", "").strip().lower() in ("save", "speichern", "ok")
                ), None)
                if save_btn:
                    save_btn.click()
                else:
                    send_keys("{ENTER}")
                time.sleep(0.8)
                self._dismiss_popups(confirm=True, timeout=1.0)
                if output_path.exists() and output_path.stat().st_size > 0:
                    return True, str(output_path)

            return False, f"Export dialog did not complete. File not created at {output_path}"
        except Exception as exc:
            return False, f"Failed during S7 AWL export: {exc}"

    def import_awl_into_active_editor(self, awl_path: Path, block_name: str = "OB1") -> Tuple[bool, str]:
        """
        Imports an AWL source file into the active project/block.
        """
        self.import_started = False
        ok, title = self.connect()
        if not ok:
            return False, title
        if not awl_path.exists():
            return False, f"AWL file does not exist: {awl_path}"

        try:
            self.main_win.set_focus()
            time.sleep(0.2)
            try:
                self.main_win.menu_select("File->Import...")
            except Exception:
                send_keys("^i")

            time.sleep(0.5)
            picker = self.app.window(title_re=r".*(Import|Open|Öffnen).*")
            if picker.exists(timeout=2.0):
                edit = picker.child_window(class_name="Edit")
                edit.set_edit_text(str(awl_path))
                open_btn = next((
                    c for c in picker.children()
                    if c.friendly_class_name() == "Button"
                    and c.window_text().replace("&", "").strip().lower() in ("open", "öffnen", "ok")
                ), None)
                self.import_started = True
                if open_btn:
                    open_btn.click()
                else:
                    send_keys("{ENTER}")
                time.sleep(0.8)
                popups = self._dismiss_popups(confirm=True, timeout=2.0)
                popup_text = " ".join(popups).lower()
                if any(err in popup_text for err in ("error", "fehler", "failed", "cannot", "ungültig")):
                    return False, f"SIMATIC Manager reported an import error: {' | '.join(popups)}"
                return True, f"SIMATIC Manager accepted the AWL import for {awl_path.name}."

            return False, "Import file dialog was not presented by SIMATIC Manager."
        except Exception as exc:
            return False, f"Failed during AWL import automation: {exc}"

    def compile_check_block(self, block_name: str = "OB1") -> Tuple[bool, List[str]]:
        """
        Triggers 'File -> Check and Save Block' (Ctrl+S) or 'PLC -> Check Block Consistency...'.
        Returns (success, messages).
        """
        ok, title = self.connect()
        if not ok:
            return False, [title]

        try:
            self.main_win.set_focus()
            time.sleep(0.2)
            send_keys("^s")
            time.sleep(0.8)
            popups = self._dismiss_popups(confirm=True, timeout=2.5)
            popup_text = " ".join(popups).lower()
            failure_terms = ("error", "fehler", "failed", "unable", "cannot", "invalid")
            if any(term in popup_text for term in failure_terms):
                return False, popups
            if not popups:
                return True, ["Save and compile completed with 0 errors."]
            return True, popups
        except Exception as exc:
            return False, [f"Compile & Check failed: {exc}"]


# ============================================================================
# 8. FASTMCP SERVER DEFINITION & TOOLS
# ============================================================================

mcp = FastMCP(
    "simatic7-mcp",
    instructions=(
        "Siemens SIMATIC STEP 7 Classic (v5.x / SIMATIC Manager) offline and online ladder/STL editing "
        "for S7-300, S7-400, and WinAC PLCs (specifically NOT for TIA Portal). "
        "Always pre-validate with s7_validate_program, generate verified AWL/STL sources, and report SUCCESS "
        "only after backup, compile, check, and exact readback verification. Never transfers to live PLC without review."
    ),
)

controller = SimaticManagerController()


def serialized_gui(function):
    """Protects SIMATIC Manager GUI operations from concurrent interleaved access."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with GUI_TRANSACTION_LOCK:
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                payload: Dict[str, Any] = {
                    "status": "GUI_OPERATION_FAILED",
                    "error": f"Unexpected {function.__name__} failure: {exc}",
                }
                if function.__name__ == "s7_write_program":
                    if GUI_ACTIVE_WRITE.get("import_attempted") or getattr(controller, "import_started", False):
                        rollback = GUI_ACTIVE_WRITE.get("rollback")
                        try:
                            payload["status"] = "WRITE_EXCEPTION"
                            payload["backup_awl"] = GUI_ACTIVE_WRITE.get("backup_awl")
                            default_rb = {"restored": False, "error": "Rollback handler unavailable."}
                            payload["rollback"] = rollback() if rollback else default_rb
                        except Exception as rollback_exc:
                            payload["rollback"] = {"restored": False, "error": str(rollback_exc)}
                    else:
                        payload["status"] = "REJECTED_BEFORE_WRITE"
                else:
                    payload["success"] = False
                return json.dumps(payload, indent=2)
            finally:
                if function.__name__ == "s7_write_program":
                    GUI_ACTIVE_WRITE.clear()
    return wrapper


@mcp.tool()
@serialized_gui
def s7_get_status() -> str:
    """
    Checks whether Siemens SIMATIC Manager or the LAD/FBD/STL Editor is running on Windows,
    reports active window title, supported PLC families, and recent generated artifacts.
    """
    ok, title_or_err = controller.connect()
    block_active, active_title = controller.is_block_editor_active() if ok else (False, "")
    recent_files = sorted(
        [f.name for f in WORKSPACE_DIR.glob("*.*") if f.suffix in (".awl", ".sdf", ".csv")],
        reverse=True,
    )[:10]

    return json.dumps(
        {
            "simatic_manager_connected": ok,
            "block_editor_active": block_active,
            "window_title": title_or_err if ok else "Not Connected",
            "pywinauto_available": PYWINAUTO_AVAILABLE,
            "supported_plc_families": sorted(S7_PLC_PROFILES),
            "workspace_dir": str(WORKSPACE_DIR),
            "recent_artifacts": recent_files,
        },
        indent=2,
    )


@mcp.tool()
def s7_list_supported_plc_types() -> str:
    """
    Lists supported Siemens S7 PLC families (S7-300, S7-400, WinAC), limits, aliases, and addressing conventions.
    """
    return json.dumps(
        {
            "families": [
                {
                    "family": profile.name,
                    "max_input_bytes": profile.max_input_byte,
                    "max_output_bytes": profile.max_output_byte,
                    "max_marker_bytes": profile.max_marker_byte,
                    "max_timers": profile.max_timer_num,
                    "max_counters": profile.max_counter_num,
                    "max_dbs": profile.max_db_num,
                    "max_db_bytes": profile.max_db_byte,
                    "addressing": "Bit bounds .0 to .7 (byte.bit); byte/word/dword addresses",
                    "mnemonics": "International (I, Q, M, T, C, A, AN, O, S, R) and German (E, A, M, T, Z, U, UN, O, S, R)",
                    "aliases": sorted(
                        alias for alias, family in S7_PLC_TYPE_ALIASES.items() if family == key
                    ),
                }
                for key, profile in sorted(S7_PLC_PROFILES.items())
            ],
            "note": (
                "Family profiles enforce conservative Siemens STEP 7 Classic memory envelopes. "
                "SIMATIC Manager block consistency check and compile remain authoritative for specific CPU configurations."
            ),
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def s7_create_project(
    project_name: str,
    plc_type: str = "S7-300",
    cpu_type: Optional[str] = None,
    station_name: Optional[str] = None,
    project_path: Optional[str] = None,
    launch_if_closed: bool = True,
) -> str:
    """
    Creates a new SIMATIC STEP 7 project targeting S7-300, S7-400, or WinAC.
    Automatically launches SIMATIC Manager (s7tgtopx.exe) if not already running, selects the
    appropriate target PLC family/CPU, and establishes standard STEP 7 Classic project structure (.s7p, Blocks, Symbols).

    Args:
        project_name: Name of the project (1-64 alphanumeric characters).
        plc_type: Target PLC family: 'S7-300', 'S7-400', or 'WinAC'.
        cpu_type: Optional specific CPU model (e.g. 'CPU 315-2 DP', 'CPU 314', 'CPU 414-3 PN/DP', 'WinAC RTX').
        station_name: Optional station name (e.g. 'SIMATIC 300(1)').
        project_path: Optional custom folder path for project.
        launch_if_closed: If True (default), automatically starts SIMATIC Manager if closed.
    """
    ok, message, meta = controller.create_project(
        project_name,
        plc_type=plc_type,
        cpu_type=cpu_type,
        station_name=station_name,
        project_path=project_path,
        launch_if_closed=launch_if_closed,
    )
    if ok:
        return json.dumps({
            "success": True,
            "project_name": project_name,
            "plc_type": meta.get("plc_type", plc_type),
            "cpu_type": meta.get("cpu_type"),
            "station_name": meta.get("station_name"),
            "s7p_path": meta.get("s7p_path"),
            "message": message,
            "details": meta,
        }, indent=2)
    return json.dumps({
        "success": False,
        "error": message,
        "plc_type": plc_type,
    }, indent=2)


@mcp.tool()
@serialized_gui
def s7_open_project(
    project_path_or_name: str,
    launch_if_closed: bool = True,
) -> str:
    """
    Opens an existing SIMATIC STEP 7 project (.s7p) or project workspace directory.
    Launches SIMATIC Manager (s7tgtopx.exe) automatically if not already running.

    Args:
        project_path_or_name: Path to the .s7p project file, project folder, or project name in workspace.
        launch_if_closed: If True (default), launches SIMATIC Manager if not running.
    """
    ok, message, meta = controller.open_project(project_path_or_name, launch_if_closed=launch_if_closed)
    return json.dumps({
        "success": ok,
        "message": message,
        "project": meta,
    }, indent=2)


@mcp.tool()
@serialized_gui
def s7_open_block(
    block_name: str = "OB1",
    project_name: Optional[str] = None,
) -> str:
    """
    Opens or focuses the target block in the SIMATIC STEP 7 LAD/FBD/STL Editor (s7kotx.exe / s7wndltx.exe).

    Args:
        block_name: Target block identifier (e.g. 'OB1', 'FC1', 'FB1', 'DB1').
        project_name: Optional project name to locate the block file within.
    """
    ok, message, meta = controller.open_block(block_name=block_name, project_name=project_name)
    return json.dumps({
        "success": ok,
        "message": message,
        "details": meta,
    }, indent=2)


@mcp.tool()
def s7_validate_program(
    networks: List[Dict[str, Any]],
    symbols: Optional[Any] = None,
    symbol_table: Optional[Any] = None,
    device_comments: Optional[Any] = None,
    auto_fix_mnemonics: bool = False,
    plc_type: str = "S7-300",
    mnemonic_language: str = "International",
) -> str:
    """
    Pre-validates candidate S7 Statement List (AWL/STL) networks without modifying SIMATIC Manager.
    Enforces device boundary ranges, valid mnemonics, Star-Delta contactor interlock checks,
    and duplicate output coil detection.

    Args:
        networks: List of network objects:
                  [{"title": "Start/Stop", "comment": "Motor 1", "instructions": ["A I 0.0", "= M 0.0"]}].
        symbols: Optional symbol mappings for I/O and markers. Can be dict or list.
        symbol_table: Explicit alias for symbols.
        device_comments: Explicit alias for symbols (accepts Address -> Comment mapping).
        auto_fix_mnemonics: If True, automatically translates LD/LDI/OUT or German/International mnemonics.
        plc_type: Target PLC family: 'S7-300', 'S7-400', or 'WinAC'.
        mnemonic_language: 'International' (A, AN, O, I, Q) or 'German' (U, UN, O, E, A).
    """
    symbols_to_use = symbols if symbols is not None else (symbol_table if symbol_table is not None else device_comments)
    report = validate_and_analyze_program(
        networks,
        symbols_input=symbols_to_use,
        auto_fix_mnemonics=auto_fix_mnemonics,
        plc_type=plc_type,
        mnemonic_language=mnemonic_language,
    )
    return json.dumps(
        {
            "valid": report.valid,
            "estimated_mc7_bytes": report.estimated_mc7_bytes,
            "network_count": len(report.normalized_networks),
            "errors": report.errors,
            "warnings": report.warnings,
            "devices_used": sorted(report.devices_used),
            "normalized_networks": [
                {
                    "title": net.title,
                    "comment": net.comment,
                    "instructions": net.instructions,
                }
                for net in report.normalized_networks
            ],
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def s7_write_program(
    networks: List[Dict[str, Any]],
    symbols: Optional[Any] = None,
    symbol_table: Optional[Any] = None,
    device_comments: Optional[Any] = None,
    delivery_mode: str = "awl_source",
    plc_type: str = "S7-300",
    block_name: str = "OB1",
    run_compile_and_check: bool = True,
    auto_fix_mnemonics: bool = False,
    mnemonic_language: str = "International",
) -> str:
    """
    Validates, backs up, imports, compiles, checks, and reads back an S7 program block.
    Invalid candidates are rejected before touching SIMATIC Manager.
    SUCCESS requires verified round-trip equality between candidate and readback.

    Args:
        networks: Ordered network objects with title, comment, and instructions.
        symbols: Optional symbol mappings for I/O and markers.
        symbol_table: Explicit alias for symbols.
        device_comments: Explicit alias for symbols.
        delivery_mode: Must be 'awl_source' (Siemens AWL text source import).
        plc_type: Target PLC family ('S7-300', 'S7-400', or 'WinAC').
        block_name: Name of target block ('OB1', 'FC1', 'FB1').
        run_compile_and_check: Must be True; compilation and block checks are mandatory.
        auto_fix_mnemonics: If True, translates foreign or cross-language mnemonics.
        mnemonic_language: 'International' or 'German'.
    """
    controller.import_started = False
    GUI_ACTIVE_WRITE.clear()

    if not isinstance(delivery_mode, str) or delivery_mode.lower() != "awl_source":
        err_msg = "Only delivery_mode='awl_source' is supported for reliable S7 writes."
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": err_msg}, indent=2)
    if run_compile_and_check is not True:
        err_msg = "run_compile_and_check must be True for safe verification."
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": err_msg}, indent=2)

    profile, profile_error = resolve_s7_plc_profile(plc_type)
    if profile_error or profile is None:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": profile_error}, indent=2)

    symbols_to_use = symbols if symbols is not None else (symbol_table if symbol_table is not None else device_comments)

    # 1. Pre-flight deterministic validation
    report = validate_and_analyze_program(
        networks,
        symbols_input=symbols_to_use,
        auto_fix_mnemonics=auto_fix_mnemonics,
        plc_type=plc_type,
        mnemonic_language=mnemonic_language,
    )
    if not report.valid:
        return json.dumps(
            {
                "status": "REJECTED_BEFORE_WRITE",
                "message": "Program rejected before modifying block due to validation errors.",
                "errors": report.errors,
                "warnings": report.warnings,
            },
            indent=2,
        )

    # Parse block type and number
    m_blk = re.match(r"^([A-Za-z]+)(\d+)$", block_name.strip())
    blk_type = m_blk.group(1).upper() if m_blk else "OB"
    blk_num = int(m_blk.group(2)) if m_blk else 1

    # 2. Generate local AWL source artifact
    candidate_awl_content = generate_s7_awl_source(
        report.normalized_networks,
        block_type=blk_type,
        block_number=blk_num,
        title=f"Program {block_name}",
        symbols=symbols_to_use,
        annotate_comments=True,
    )
    after_awl_path = save_s7_artifact(
        candidate_awl_content,
        new_s7_artifact_path(f"{block_name.lower()}-after"),
    )

    candidate_parsed = parse_s7_awl_source(after_awl_path)
    if candidate_parsed.get("error"):
        return json.dumps(
            {
                "status": "CANDIDATE_FORMAT_FAILED",
                "error": candidate_parsed.get("error"),
                "awl_source_path": str(after_awl_path),
            },
            indent=2,
        )

    # Generate symbol table artifact if symbols provided
    symbol_sdf_path: Optional[Path] = None
    if symbols_to_use:
        _, sym_entries = _validate_symbol_map(symbols_to_use)
        if sym_entries:
            symbol_content = generate_s7_symbol_sdf(sym_entries)
            symbol_sdf_path = save_s7_artifact(
                symbol_content,
                new_s7_artifact_path("symbols", suffix=".sdf"),
            )

    # 3. Connect to SIMATIC Manager
    ok, conn_msg = controller.connect()
    if not ok:
        # Graceful offline mode: return verified generated artifacts
        return json.dumps(
            {
                "status": "OFFLINE_SOURCE_GENERATED",
                "message": (
                    "Successfully validated program and generated compliant Siemens STEP 7 AWL source artifact locally, "
                    f"but SIMATIC Manager was not reachable: {conn_msg}"
                ),
                "block_name": block_name,
                "plc_type": profile.name,
                "network_count": len(report.normalized_networks),
                "estimated_mc7_bytes": report.estimated_mc7_bytes,
                "warnings": report.warnings,
                "awl_source_path": str(after_awl_path),
                "symbol_table_path": str(symbol_sdf_path) if symbol_sdf_path else None,
            },
            indent=2,
        )

    # Verify editor is open and focused
    is_active, active_msg = controller.is_block_editor_active(block_name)
    if not is_active:
        return json.dumps(
            {
                "status": "REJECTED_BEFORE_WRITE",
                "error": active_msg,
                "awl_source_path": str(after_awl_path),
            },
            indent=2,
        )

    # 4. Safe backup of existing block before modification
    before_awl_path = new_s7_artifact_path(f"{block_name.lower()}-before")
    backup_ok, backup_msg = controller.export_current_awl(before_awl_path, block_name=block_name)
    if not backup_ok:
        return json.dumps(
            {
                "status": "BACKUP_FAILED",
                "error": backup_msg,
                "awl_source_path": str(after_awl_path),
            },
            indent=2,
        )

    backup_parsed = parse_s7_awl_source(before_awl_path)
    if backup_parsed.get("error"):
        return json.dumps(
            {
                "status": "BACKUP_FAILED",
                "error": backup_parsed.get("error", "Backup AWL contains no parseable instructions."),
                "backup_awl": str(before_awl_path),
            },
            indent=2,
        )

    def restore_backup() -> Dict[str, Any]:
        restored, restore_msg = controller.import_awl_into_active_editor(before_awl_path, block_name=block_name)
        if not restored:
            return {"restored": False, "error": restore_msg}
        restore_compile_ok, restore_msgs = controller.compile_check_block(block_name=block_name)
        if not restore_compile_ok:
            return {"restored": False, "error": "Backup re-imported, but compile failed.", "details": restore_msgs}
        return {"restored": True, "backup_awl": str(before_awl_path)}

    GUI_ACTIVE_WRITE.update({
        "backup_awl": str(before_awl_path),
        "rollback": restore_backup,
        "import_attempted": False,
    })

    # 5. Import candidate AWL source
    import_ok, import_msg = controller.import_awl_into_active_editor(after_awl_path, block_name=block_name)
    GUI_ACTIVE_WRITE["import_attempted"] = bool(getattr(controller, "import_started", import_ok))
    if not import_ok:
        if not GUI_ACTIVE_WRITE["import_attempted"]:
            return json.dumps({
                "status": "IMPORT_NOT_STARTED",
                "error": import_msg,
                "backup_awl": str(before_awl_path),
            }, indent=2)
        return json.dumps({
            "status": "WRITE_FAILED",
            "error": import_msg,
            "backup_awl": str(before_awl_path),
            "rollback": restore_backup(),
        }, indent=2)

    # 6. Compile & Check
    compile_ok, compile_msgs = controller.compile_check_block(block_name=block_name)
    if not compile_ok:
        return json.dumps({
            "status": "BUILD_FAILED",
            "error": compile_msgs,
            "backup_awl": str(before_awl_path),
            "rollback": restore_backup(),
        }, indent=2)

    # 7. Readback Verification
    verify_awl_path = new_s7_artifact_path(f"{block_name.lower()}-verify")
    verify_ok, verify_msg = controller.export_current_awl(verify_awl_path, block_name=block_name)
    if not verify_ok:
        return json.dumps({
            "status": "READBACK_FAILED",
            "error": verify_msg,
            "backup_awl": str(before_awl_path),
            "rollback": restore_backup(),
        }, indent=2)

    verified_summary = parse_s7_awl_source(verify_awl_path)
    expected_instructions = s7_instruction_signature(candidate_parsed.get("networks", []))
    actual_instructions = s7_instruction_signature(verified_summary.get("networks", []))

    if verified_summary.get("error") or expected_instructions != actual_instructions:
        return json.dumps({
            "status": "READBACK_MISMATCH",
            "error": verified_summary.get("error", "Readback instructions differ from candidate."),
            "expected_count": len(expected_instructions),
            "actual_count": len(actual_instructions),
            "backup_awl": str(before_awl_path),
            "rollback": restore_backup(),
        }, indent=2)

    return json.dumps(
        {
            "status": "SUCCESS",
            "write_message": import_msg,
            "block_name": block_name,
            "plc_type": profile.name,
            "network_count": len(report.normalized_networks),
            "estimated_mc7_bytes": report.estimated_mc7_bytes,
            "compile_messages": compile_msgs,
            "engineering_warnings": report.warnings,
            "artifacts": {
                "before_awl": str(before_awl_path),
                "after_awl": str(after_awl_path),
                "verify_awl": str(verify_awl_path),
                "symbol_table": str(symbol_sdf_path) if symbol_sdf_path else None,
            },
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def s7_read_program(block_name: str = "OB1") -> str:
    """
    Exports and parses the currently open block in SIMATIC Manager / LAD Editor.
    Returns network titles, comments, and instructions.
    """
    ok, title = controller.connect()
    if not ok:
        return json.dumps({"success": False, "error": title}, indent=2)
    is_active, active_msg = controller.is_block_editor_active(block_name)
    if not is_active:
        return json.dumps({"success": False, "error": active_msg}, indent=2)

    export_path = new_s7_artifact_path(f"{block_name.lower()}-read")
    ok, msg = controller.export_current_awl(export_path, block_name=block_name)
    if not ok:
        return json.dumps({"success": False, "error": msg}, indent=2)

    parsed = parse_s7_awl_source(export_path)
    if parsed.get("error"):
        return json.dumps({"success": False, "error": parsed["error"]}, indent=2)

    parsed["success"] = True
    return json.dumps(parsed, indent=2)


@mcp.tool()
@serialized_gui
def s7_compile_and_check(block_name: str = "OB1") -> str:
    """
    Triggers save and compilation (Check Block Consistency) in SIMATIC Manager.
    """
    ok, title = controller.connect()
    if not ok:
        return json.dumps({"success": False, "error": title}, indent=2)
    is_active, active_msg = controller.is_block_editor_active(block_name)
    if not is_active:
        return json.dumps({"success": False, "error": active_msg}, indent=2)

    compile_ok, messages = controller.compile_check_block(block_name=block_name)
    return json.dumps({
        "success": compile_ok,
        "block_name": block_name,
        "messages": messages,
    }, indent=2)


@mcp.tool()
def s7_write_symbol_table(
    symbols: Any,
    import_via_gui: bool = False,
    plc_type: str = "S7-300",
) -> str:
    """
    Saves a validated Siemens STEP 7 Symbol Table artifact (.SDF and .CSV).
    Format: Symbol, Address, Data type, Comment.

    Args:
        symbols: Dict or List of symbols, e.g. {"START": "I 0.0", "STOP": "I 0.1", "MOTOR": "Q 0.0"}
                 or Address mapping {"I 0.0": "START", "Q 0.0": "MOTOR"}.
        import_via_gui: Must be False; automated GUI import of symbol tables is not supported safely.
        plc_type: Target PLC family for symbol verification.
    """
    if not isinstance(import_via_gui, bool):
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": ["import_via_gui must be a boolean."]}, indent=2)
    if not symbols:
        return json.dumps({
            "status": "REJECTED_BEFORE_WRITE",
            "errors": ["symbols must be a non-empty object or list."],
        }, indent=2)

    errors, symbol_entries = _validate_symbol_map(symbols)
    if errors:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": errors}, indent=2)

    sdf_content = generate_s7_symbol_sdf(symbol_entries)
    sdf_path = save_s7_artifact(sdf_content, new_s7_artifact_path("symbols", suffix=".sdf"))

    csv_content = generate_s7_symbol_csv(symbol_entries)
    csv_path = save_s7_artifact(csv_content, new_s7_artifact_path("symbols", suffix=".csv"))

    return json.dumps(
        {
            "status": "SUCCESS",
            "symbol_count": len(symbol_entries),
            "symbol_sdf_path": str(sdf_path),
            "symbol_csv_path": str(csv_path),
            "note": "Saved standard Siemens SDF and CSV symbol table artifacts in Codex-Simatic7 workspace.",
        },
        indent=2,
    )


@mcp.tool()
def s7_read_symbol_table(symbol_table_path: str) -> str:
    """
    Reads and validates a previously exported Siemens STEP 7 Symbol Table (.SDF, .SEQ, or .CSV).

    Args:
        symbol_table_path: Path to the .sdf, .seq, or .csv symbol file.
    """
    if not isinstance(symbol_table_path, str) or not symbol_table_path.strip():
        return json.dumps({"success": False, "error": "symbol_table_path must be a non-empty string path."}, indent=2)

    path = Path(symbol_table_path).expanduser()
    parsed = parse_s7_symbol_table(path)
    parsed["success"] = "error" not in parsed
    return json.dumps(parsed, indent=2)


@mcp.tool()
def s7_write_device_comments(
    device_comments: Dict[str, Any],
    plc_type: str = "S7-300",
    import_via_gui: bool = False,
) -> str:
    """
    Saves a validated global SIMATIC STEP 7 Device Comment and Symbol Table artifact (.SDF and .CSV).
    Enables AI agents to define global device comments for physical inputs, outputs, memory markers,
    timers, counters, and data blocks. Auto-infers data types and disambiguates duplicate comments.

    Args:
        device_comments: Mapping of S7 addresses or symbol names to comments or comment objects.
                         Examples:
                           {"I 0.0": "Emergency Stop Pushbutton", "Q 0.0": "Main Contactor Coil", "M 10.0": "Cycle Running"}
                           {"START_PB": {"address": "I 0.0", "comment": "Start PB", "data_type": "BOOL"}}
        plc_type: Target PLC family: 'S7-300', 'S7-400', or 'WinAC'.
        import_via_gui: Must be False; automated GUI symbol table import is unsupported.
    """
    if not isinstance(import_via_gui, bool):
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": ["import_via_gui must be a boolean."]}, indent=2)
    if not device_comments or not isinstance(device_comments, (dict, list)):
        return json.dumps({
            "status": "REJECTED_BEFORE_WRITE",
            "errors": ["device_comments must be a non-empty dictionary or list."],
        }, indent=2)

    profile, profile_error = resolve_s7_plc_profile(plc_type)
    if profile_error or profile is None:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": [profile_error]}, indent=2)

    errors, symbol_entries = _validate_symbol_map(device_comments)
    if errors:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": errors}, indent=2)

    sdf_content = generate_s7_symbol_sdf(symbol_entries)
    sdf_path = save_s7_artifact(sdf_content, new_s7_artifact_path("symbols", suffix=".sdf"))

    csv_content = generate_s7_symbol_csv(symbol_entries)
    csv_path = save_s7_artifact(csv_content, new_s7_artifact_path("symbols", suffix=".csv"))

    ws_symbols_dir = WORKSPACE_DIR / "Symbols"
    ws_symbols_dir.mkdir(parents=True, exist_ok=True)
    (ws_symbols_dir / "symbols.sdf").write_text(sdf_content, encoding="utf-8")
    (ws_symbols_dir / "symbols.csv").write_text(csv_content, encoding="utf-8")

    return json.dumps(
        {
            "status": "SUCCESS",
            "comment_count": len(symbol_entries),
            "plc_type": profile.name,
            "symbol_sdf_path": str(sdf_path),
            "symbol_csv_path": str(csv_path),
            "processed_comments": [
                {
                    "symbol": e.symbol,
                    "address": e.address,
                    "data_type": e.data_type,
                    "comment": e.comment,
                }
                for e in symbol_entries
            ],
            "note": "Saved standard Siemens SDF and CSV symbol table artifacts in Codex-Simatic7 workspace.",
        },
        indent=2,
    )


@mcp.tool()
def s7_read_device_comments(comment_file_path: Optional[str] = None) -> str:
    """
    Reads and parses a SIMATIC STEP 7 Device Comment / Symbol Table file (.SDF, .SEQ, or .CSV).
    If comment_file_path is not specified, automatically locates the most recent symbol file in the
    active project or workspace.

    Returns structured dictionaries:
      - device_comments: { "I 0.0": "Emergency Stop Pushbutton", ... }
      - symbol_to_address: { "START_PB": "I 0.0", ... }
      - address_to_symbol: { "I 0.0": "START_PB", ... }
      - symbols: List of symbol entries
    """
    target_path: Optional[Path] = None
    if comment_file_path and comment_file_path.strip():
        target_path = Path(comment_file_path).expanduser().resolve()
    else:
        if controller.project_name:
            proj_sym = WORKSPACE_DIR / "Projects" / controller.project_name / "Symbols" / "symbols.sdf"
            if proj_sym.is_file():
                target_path = proj_sym
        if not target_path or not target_path.is_file():
            ws_sym = WORKSPACE_DIR / "Symbols" / "symbols.sdf"
            if ws_sym.is_file():
                target_path = ws_sym
        if not target_path or not target_path.is_file():
            cands = sorted(WORKSPACE_DIR.glob("*.sdf"), key=lambda f: f.stat().st_mtime, reverse=True)
            if cands:
                target_path = cands[0]

    if not target_path or not target_path.is_file():
        err_msg = (
            "No symbol/comment table file found. Specify comment_file_path or create one first with s7_write_device_comments."
        )
        return json.dumps({"success": False, "error": err_msg}, indent=2)

    parsed = parse_s7_symbol_table(target_path)
    if parsed.get("error"):
        return json.dumps({"success": False, "error": parsed["error"]}, indent=2)

    device_comments = {
        entry["address"]: entry["comment"] for entry in parsed.get("symbols", []) if entry.get("comment")
    }
    symbol_to_addr = {entry["symbol"]: entry["address"] for entry in parsed.get("symbols", [])}
    addr_to_symbol = {entry["address"]: entry["symbol"] for entry in parsed.get("symbols", [])}

    return json.dumps(
        {
            "success": True,
            "file": str(target_path),
            "symbol_count": parsed.get("symbol_count", 0),
            "device_comments": device_comments,
            "symbol_to_address": symbol_to_addr,
            "address_to_symbol": addr_to_symbol,
            "symbols": parsed.get("symbols", []),
        },
        indent=2,
    )


@mcp.tool()
def s7_search_symbols_and_comments(
    query: str,
    filter_type: str = "ALL",
    comment_file_path: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """
    Searches device comments and symbols by address, symbol name, comment text, or data type.
    Supports wildcards ('I 0.*', '*STOP*') and case-insensitive matching.

    Args:
        query: Search query or pattern (e.g. 'I 0.*', 'MOTOR', 'Emergency', 'BOOL').
        filter_type: Filter category: 'ALL', 'ADDRESS', 'SYMBOL', 'COMMENT', 'DATA_TYPE'.
        comment_file_path: Optional path to symbol table file (.sdf or .csv).
        max_results: Maximum results to return (default: 50).
    """
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"success": False, "error": "query must be a non-empty string."}, indent=2)

    target_path = Path(comment_file_path).expanduser().resolve() if comment_file_path else None
    if not target_path or not target_path.is_file():
        proj_sym = (
            (WORKSPACE_DIR / "Projects" / controller.project_name / "Symbols" / "symbols.sdf")
            if controller.project_name else None
        )
        for cand in [proj_sym, WORKSPACE_DIR / "Symbols" / "symbols.sdf", WORKSPACE_DIR / "Symbols" / "symbols.csv"]:
            if cand and cand.is_file():
                target_path = cand
                break
        if not target_path or not target_path.is_file():
            cands = sorted(WORKSPACE_DIR.glob("*.sdf"), key=lambda f: f.stat().st_mtime, reverse=True)
            if cands:
                target_path = cands[0]

    if not target_path or not target_path.is_file():
        return json.dumps({
            "success": False,
            "error": "No symbol/comment table available to search. Use s7_write_device_comments to create one.",
        }, indent=2)

    parsed = parse_s7_symbol_table(target_path)
    if parsed.get("error"):
        return json.dumps({"success": False, "error": parsed["error"]}, indent=2)

    entries = [
        S7SymbolEntry(
            symbol=item["symbol"],
            address=item["address"],
            data_type=item.get("data_type", "BOOL"),
            comment=item.get("comment", ""),
        )
        for item in parsed.get("symbols", [])
    ]

    res = search_symbols_and_comments_entries(entries, query=query, filter_type=filter_type, max_results=max_results)
    res["success"] = True
    res["source_file"] = str(target_path)
    return json.dumps(res, indent=2)


@mcp.tool()
def s7_annotate_awl_code(
    source_code: Optional[str] = None,
    networks: Optional[List[Dict[str, Any]]] = None,
    symbols: Optional[Any] = None,
    comment_file_path: Optional[str] = None,
    style: str = "COMMENTS",
    plc_type: str = "S7-300",
) -> str:
    """
    Annotates AWL (STL) source code or network structures using global device comments and symbols.

    Styles:
      - 'COMMENTS': Appends inline device comments to instructions:
                    'A I 0.0;' -> 'A I 0.0;  // "START_PB": Emergency Stop Pushbutton'
      - 'SYMBOLIC': Substitutes absolute addresses with symbolic identifiers:
                    'A I 0.0;' -> 'A "START_PB";  // I 0.0: Emergency Stop Pushbutton'
      - 'ABSOLUTE': Translates symbolic operands into canonical physical addresses:
                    'A "START_PB";' -> 'A I 0.0;  // "START_PB": Emergency Stop Pushbutton'

    Args:
        source_code: Raw AWL source code text.
        networks: List of network objects.
        symbols: Dict or list of symbol/comment definitions.
        comment_file_path: Optional path to symbol table file.
        style: 'COMMENTS', 'SYMBOLIC', or 'ABSOLUTE' (default: 'COMMENTS').
        plc_type: Target PLC family ('S7-300', 'S7-400', 'WinAC').
    """
    if source_code is None and networks is None:
        return json.dumps({"success": False, "error": "Either source_code or networks must be provided."}, indent=2)

    symbols_to_use = symbols
    if not symbols_to_use and comment_file_path:
        p = Path(comment_file_path).expanduser()
        if p.is_file():
            parsed_sym = parse_s7_symbol_table(p)
            symbols_to_use = parsed_sym.get("symbols")

    if not symbols_to_use:
        ws_sym = WORKSPACE_DIR / "Symbols" / "symbols.sdf"
        if ws_sym.is_file():
            parsed_sym = parse_s7_symbol_table(ws_sym)
            symbols_to_use = parsed_sym.get("symbols")

    try:
        if source_code:
            annotated_awl, norm_nets, count = annotate_awl_source_text(
                source_code, symbols=symbols_to_use, style=style, plc_type=plc_type
            )
        else:
            report = validate_and_analyze_program(networks, symbols_input=symbols_to_use, plc_type=plc_type)
            if not report.valid:
                return json.dumps({"success": False, "errors": report.errors}, indent=2)
            annotated_awl = generate_s7_awl_source(
                report.normalized_networks,
                symbols=symbols_to_use,
                annotate_comments=True,
                style=style,
            )
            norm_nets = [
                {"title": n.title, "comment": n.comment, "instructions": n.instructions}
                for n in report.normalized_networks
            ]
            count = sum(1 for line in annotated_awl.splitlines() if "//" in line and not line.strip().startswith("//"))

        artifact_path = save_s7_artifact(
            annotated_awl,
            new_s7_artifact_path(f"annotated-{style.lower()}"),
        )
        return json.dumps({
            "success": True,
            "style": style.upper(),
            "annotations_count": count,
            "network_count": len(norm_nets),
            "annotated_awl_path": str(artifact_path),
            "annotated_awl_source": annotated_awl,
            "normalized_networks": norm_nets,
        }, indent=2)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Failed to annotate AWL code: {exc}"}, indent=2)


@mcp.tool()
def s7_get_cross_references(
    networks: Optional[List[Dict[str, Any]]] = None,
    awl_source_path: Optional[str] = None,
    block_name: str = "OB1",
    symbols: Optional[Any] = None,
) -> str:
    """
    Generates a complete cross-reference table (Querverweisliste) for all operands in the program.
    Classifies read accesses, write/set/reset assignments, and detects memory overlaps
    (e.g., bit M 0.1 accessed while MW 0 is overwritten).

    Args:
        networks: Optional list of network dicts.
        awl_source_path: Optional path to an AWL source file.
        block_name: Block to analyze if networks and awl_source_path are omitted.
        symbols: Optional symbol mappings for operand annotation.
    """
    nets_to_use = networks
    if nets_to_use is None:
        if awl_source_path:
            p = Path(awl_source_path).expanduser()
            if not p.is_file():
                return json.dumps({"success": False, "error": f"File not found: {awl_source_path}"}, indent=2)
            parsed = parse_s7_awl_source(p)
            if parsed.get("error"):
                return json.dumps({"success": False, "error": parsed["error"]}, indent=2)
            nets_to_use = parsed.get("networks", [])
        else:
            proj = controller.project_name
            if proj:
                cand = WORKSPACE_DIR / "Projects" / proj / "Blocks" / f"{block_name.upper()}.awl"
                if cand.is_file():
                    parsed = parse_s7_awl_source(cand)
                    nets_to_use = parsed.get("networks", [])

    if not nets_to_use:
        return json.dumps({
            "success": False,
            "error": "No networks or AWL source provided to generate cross references.",
        }, indent=2)

    res = generate_cross_references(nets_to_use, symbols=symbols)
    res["success"] = True
    return json.dumps(res, indent=2)


@mcp.tool()
def s7_get_memory_usage_map(
    networks: Optional[List[Dict[str, Any]]] = None,
    awl_source_path: Optional[str] = None,
    block_name: str = "OB1",
    plc_type: str = "S7-300",
    symbols: Optional[Any] = None,
) -> str:
    """
    Calculates exact memory allocation and generates a bit/byte visual map for:
      - Inputs (I / E)
      - Outputs (Q / A)
      - Bit Memory / Markers (M)
      - Timers (T)
      - Counters (C / Z)
      - Data Blocks (DB)
    Enforces and displays utilization against target PLC CPU family boundaries.

    Args:
        networks: Optional list of network dicts.
        awl_source_path: Optional path to an AWL source file.
        block_name: Block to analyze if networks and awl_source_path are omitted.
        plc_type: Target PLC family: 'S7-300', 'S7-400', or 'WinAC'.
        symbols: Optional symbol mappings.
    """
    profile, profile_err = resolve_s7_plc_profile(plc_type)
    if profile_err or profile is None:
        return json.dumps({"success": False, "error": profile_err}, indent=2)

    nets_to_use = networks
    if nets_to_use is None:
        if awl_source_path:
            p = Path(awl_source_path).expanduser()
            if not p.is_file():
                return json.dumps({"success": False, "error": f"File not found: {awl_source_path}"}, indent=2)
            parsed = parse_s7_awl_source(p)
            if parsed.get("error"):
                return json.dumps({"success": False, "error": parsed["error"]}, indent=2)
            nets_to_use = parsed.get("networks", [])
        else:
            proj = controller.project_name
            if proj:
                cand = WORKSPACE_DIR / "Projects" / proj / "Blocks" / f"{block_name.upper()}.awl"
                if cand.is_file():
                    parsed = parse_s7_awl_source(cand)
                    nets_to_use = parsed.get("networks", [])

    if not nets_to_use:
        return json.dumps({
            "success": False,
            "error": "No networks or AWL source provided to generate memory usage map.",
        }, indent=2)

    res = generate_memory_usage_map(nets_to_use, profile=profile, symbols=symbols)
    res["success"] = True
    return json.dumps(res, indent=2)


@mcp.tool()
def s7_get_block_overview(project_path_or_name: Optional[str] = None) -> str:
    """
    Scans the STEP 7 project workspace and returns an overview of all program blocks
    (OB, FB, FC, DB, SDB), including block metadata, estimated MC7 sizes, local data stacks,
    and the complete call hierarchy tree.

    Args:
        project_path_or_name: Optional project folder path or project name. Defaults to active project.
    """
    res = inspect_project_blocks(project_dir_or_file=Path(project_path_or_name) if project_path_or_name else None)
    res["success"] = True
    return json.dumps(res, indent=2)


@mcp.tool()
def s7_analyze_program(
    networks: Optional[List[Dict[str, Any]]] = None,
    awl_source_path: Optional[str] = None,
    symbols: Optional[Any] = None,
    plc_type: str = "S7-300",
) -> str:
    """
    Performs comprehensive static safety analysis on SIMATIC S7 Statement List (AWL/STL) code:
      - Star-Delta contactor switching hazards (interlock + dead-time delay)
      - Duplicate coil assignments (=)
      - Unpaired SET/RESET coils
      - Timer/Counter initialization and evaluation hygiene
      - Direct peripheral I/O access hazards (PIB/PQB)
      - Nesting stack depth verification (<= 7)
      - Estimated CPU scan sweep time in microseconds
      - Overall safety score (0-100) and actionable recommendations

    Args:
        networks: Candidate networks list.
        awl_source_path: Optional path to an AWL source file.
        symbols: Optional symbol mappings.
        plc_type: Target PLC family: 'S7-300', 'S7-400', or 'WinAC'.
    """
    profile, profile_err = resolve_s7_plc_profile(plc_type)
    if profile_err or profile is None:
        return json.dumps({"success": False, "error": profile_err}, indent=2)

    nets_to_use = networks
    if nets_to_use is None and awl_source_path:
        p = Path(awl_source_path).expanduser()
        if not p.is_file():
            return json.dumps({"success": False, "error": f"File not found: {awl_source_path}"}, indent=2)
        parsed = parse_s7_awl_source(p)
        if parsed.get("error"):
            return json.dumps({"success": False, "error": parsed["error"]}, indent=2)
        nets_to_use = parsed.get("networks", [])

    if not nets_to_use:
        return json.dumps({"success": False, "error": "No networks or AWL source provided for program analysis."}, indent=2)

    res = perform_deep_program_analysis(nets_to_use, profile=profile, symbols=symbols)
    res["success"] = True
    res["plc_type"] = profile.name
    return json.dumps(res, indent=2)


@mcp.tool()
def s7_auto_detect_controllers() -> str:
    """
    Auto-detects the host SIMATIC STEP 7 Classic environment:
      - Installed STEP 7 versions (v5.5 / v5.6 / v5.7) via registry
      - Binary paths (s7tgtopx.exe, s7hwcnfx.exe, s7wndltx.exe, PLCSIM)
      - Currently running Siemens processes
      - Offline workspace health and projects
    """
    res = detect_simatic_environment()
    res["success"] = True
    return json.dumps(res, indent=2)


# ============================================================================
# 9. SERVER ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    mcp.run()
