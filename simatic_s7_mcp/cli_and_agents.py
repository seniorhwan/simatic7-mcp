"""CLI doctor diagnostics, AI IDE configuration generators, MCP Resources, and MCP Prompts.

Supports drop-in configuration for:
- VS Code (`.vscode/mcp.json`)
- Google Antigravity (`antigravity` MCP JSON config)
- OpenAI Codex (`~/.codex/config.toml` + CLI command)
- Claude Code (`.mcp.json` + `claude mcp add` command)
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import AccessMode, resolve_access_mode
from .hw_config import S7_HARDWARE_CATALOG


def _probe_step7_binaries() -> Dict[str, Any]:
    candidates = {
        "S7TGTOPX.EXE": [
            r"C:\Program Files (x86)\Siemens\Step7\S7BIN\S7TGTOPX.EXE",
            r"C:\Program Files\Siemens\Step7\S7BIN\S7TGTOPX.EXE",
        ],
        "S7HWCNFX.EXE": [
            r"C:\Program Files (x86)\Siemens\Step7\S7BIN\S7HWCNFX.EXE",
            r"C:\Program Files\Siemens\Step7\S7BIN\S7HWCNFX.EXE",
        ],
        "S7SC.EXE": [
            r"C:\Program Files (x86)\Siemens\Step7\S7SCL\S7SC.EXE",
            r"C:\Program Files (x86)\Siemens\Step7\S7BIN\S7SC.EXE",
            r"C:\Program Files\Siemens\Step7\S7SCL\S7SC.EXE",
        ],
        "S7USSAPX.EXE": [
            r"C:\Program Files (x86)\Siemens\Step7\S7BIN\S7USSAPX.EXE",
            r"C:\Program Files\Siemens\Step7\S7BIN\S7USSAPX.EXE",
        ],
        "S7WSPX.EXE": [
            r"C:\Program Files (x86)\Siemens\PLCSIM\S7WSI\S7WSPX.EXE",
            r"C:\Program Files (x86)\Siemens\Step7\S7BIN\S7WSPX.EXE",
        ],
    }
    found: Dict[str, Optional[str]] = {}
    for name, paths in candidates.items():
        hit = None
        for p in paths:
            if os.path.isfile(p):
                hit = p
                break
        if hit is None:
            hit = shutil.which(name)
        found[name] = hit
    return found


def run_doctor(
    server_script: Optional[Path] = None,
    workspace_dir: Optional[Path] = None,
    audit_log_path: Optional[Path] = None,
    access_mode: Optional[AccessMode] = None,
) -> Dict[str, Any]:
    """Run comprehensive self-diagnostics for STEP 7 Classic MCP readiness."""
    script_path = Path(server_script) if server_script else Path(__file__).resolve().parent.parent / "simatic_s7_mcp_server.py"
    native_bridge = script_path.parent / "native" / "s7_com_bridge.exe"
    mode = access_mode or resolve_access_mode()

    has_mcp = importlib.util.find_spec("mcp") is not None
    has_pywinauto = importlib.util.find_spec("pywinauto") is not None
    has_win32com = importlib.util.find_spec("win32com") is not None
    syswow_ps = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
    has_syswow_ps = syswow_ps.is_file()
    binaries = _probe_step7_binaries()

    ws = Path(workspace_dir) if workspace_dir else Path.home() / "Documents" / "Codex-Simatic7"
    ws_writable = False
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe = ws / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        ws_writable = True
    except Exception:
        ws_writable = False

    is_windows = platform.system().lower() == "windows"
    checks: List[Dict[str, Any]] = [
        {
            "id": "python_version",
            "ok": sys.version_info >= (3, 10),
            "detail": f"Python {platform.python_version()} ({platform.architecture()[0]})",
        },
        {
            "id": "mcp_sdk",
            "ok": has_mcp,
            "detail": "installed" if has_mcp else "not installed (run: pip install 'mcp>=1.2.0')",
        },
        {
            "id": "workspace_writable",
            "ok": ws_writable,
            "detail": str(ws),
        },
        {
            "id": "com_bridge_transport",
            "ok": native_bridge.is_file() or has_syswow_ps or (not is_windows),
            "detail": (
                f"native 32-bit C++ worker ({native_bridge})"
                if native_bridge.is_file()
                else (
                    "persistent 32-bit SysWOW64 COM worker"
                    if has_syswow_ps
                    else "offline/virtual mode (non-Windows host; VirtualS7PLC & offline tools ready)"
                )
            ),
        },
        {
            "id": "step7_classic_install",
            "ok": any(binaries.values()) or (not is_windows),
            "detail": (
                f"found {sum(1 for v in binaries.values() if v)}/{len(binaries)} binaries"
                if is_windows
                else "non-Windows host (offline compilers, HW config, IEC validators, VirtualS7PLC active)"
            ),
            "binaries": binaries,
        },
        {
            "id": "safety_posture",
            "ok": True,
            "detail": (
                f"access_mode={mode.value}, S7_WRITES_ENABLED={os.environ.get('S7_WRITES_ENABLED', 'false')}, "
                f"dual_guard=STRICT_AND"
            ),
        },
    ]

    recommendations: List[str] = []
    if not has_mcp:
        recommendations.append("Install the official MCP Python SDK: pip install 'mcp>=1.2.0'")
    if is_windows and not native_bridge.is_file():
        recommendations.append("Optional <5 ms boost: build native/s7_com_bridge.exe via native/build_bridge.bat")
    if is_windows and not binaries.get("S7TGTOPX.EXE"):
        recommendations.append("Install Siemens SIMATIC STEP 7 Classic V5.x to enable live COM/HWConfig/PLCSIM automation")
    if is_windows and not has_pywinauto:
        recommendations.append("Optional UI fallback: pip install pywinauto")

    return {
        "ok": all(c["ok"] for c in checks if c["id"] in ("python_version", "workspace_writable")),
        "platform": platform.platform(),
        "server_script": str(script_path),
        "access_mode": mode.value,
        "audit_log_path": str(audit_log_path) if audit_log_path else str(ws / "audit.jsonl"),
        "capabilities": {
            "offline_iec_61131_3": True,
            "offline_hw_config_din_rail": True,
            "virtual_s7_plc_simulator": True,
            "s7comm_pooled_batch_runtime": True,
            "live_simatic_com_automation": is_windows and (native_bridge.is_file() or has_syswow_ps),
            "live_plcsim_s7prosim": is_windows and (native_bridge.is_file() or has_syswow_ps),
            "pywinauto_available": has_pywinauto,
            "win32com_available": has_win32com,
            "hardware_catalog_modules": len(S7_HARDWARE_CATALOG),
        },
        "checks": checks,
        "recommendations": recommendations,
    }


def generate_client_config(
    client: str,
    server_script: Optional[Path] = None,
    python_executable: Optional[str] = None,
    mode: str = "read-write",
    writes_enabled: bool = False,
) -> Dict[str, Any]:
    """Generate ready-to-paste MCP client configuration for VS Code, Antigravity, Codex, or Claude Code."""
    target = client.strip().lower()
    if target not in ("vscode", "antigravity", "codex", "claude", "all"):
        raise ValueError(f"Unsupported client '{client}'. Use one of: vscode, antigravity, codex, claude, all.")

    script = str(Path(server_script).resolve() if server_script else (Path(__file__).resolve().parent.parent / "simatic_s7_mcp_server.py").resolve())
    py_exe = python_executable or sys.executable or "python"
    env_map: Dict[str, str] = {
        "S7_ACCESS_MODE": mode,
        "S7_WRITES_ENABLED": "true" if writes_enabled else "false",
    }
    args = [script, "--mode", mode]

    if target == "all":
        return {
            "ok": True,
            "client": "all",
            "configs": {
                c: generate_client_config(c, Path(script), py_exe, mode, writes_enabled)
                for c in ("vscode", "antigravity", "codex", "claude")
            },
        }

    if target == "vscode":
        payload = {
            "servers": {
                "simatic-s7-classic": {
                    "type": "stdio",
                    "command": py_exe,
                    "args": args,
                    "env": env_map,
                }
            }
        }
        return {
            "ok": True,
            "client": "vscode",
            "target_file": ".vscode/mcp.json",
            "format": "json",
            "config": payload,
            "rendered": json.dumps(payload, indent=2),
        }

    if target == "antigravity":
        payload = {
            "mcpServers": {
                "simatic-s7-classic": {
                    "command": py_exe,
                    "args": args,
                    "env": env_map,
                }
            }
        }
        return {
            "ok": True,
            "client": "antigravity",
            "target_file": "~/.gemini/antigravity/mcp_config.json",
            "format": "json",
            "config": payload,
            "rendered": json.dumps(payload, indent=2),
        }

    if target == "codex":
        toml_lines = [
            "[mcp_servers.simatic-s7-classic]",
            f'command = "{py_exe.replace(chr(92), "/")}"',
            f'args = ["{script.replace(chr(92), "/")}", "--mode", "{mode}"]',
            "",
            "[mcp_servers.simatic-s7-classic.env]",
            f'S7_ACCESS_MODE = "{mode}"',
            f'S7_WRITES_ENABLED = "{"true" if writes_enabled else "false"}"',
        ]
        cli_cmd = f'codex mcp add simatic-s7-classic -- "{py_exe}" "{script}" --mode {mode}'
        return {
            "ok": True,
            "client": "codex",
            "target_file": "~/.codex/config.toml",
            "format": "toml",
            "cli_command": cli_cmd,
            "rendered": "\n".join(toml_lines),
        }

    # claude
    payload = {
        "mcpServers": {
            "simatic-s7-classic": {
                "command": py_exe,
                "args": args,
                "env": env_map,
            }
        }
    }
    cli_cmd = (
        f'claude mcp add --transport stdio simatic-s7-classic '
        f'-e S7_ACCESS_MODE={mode} -e S7_WRITES_ENABLED={"true" if writes_enabled else "false"} '
        f'-- "{py_exe}" "{script}" --mode {mode}'
    )
    return {
        "ok": True,
        "client": "claude",
        "target_file": ".mcp.json",
        "format": "json",
        "cli_command": cli_cmd,
        "config": payload,
        "rendered": json.dumps(payload, indent=2),
    }


# ---------------------------------------------------------------------------
# MCP Resource & Prompt Builders (Compact AI-Optimized Context)
# ---------------------------------------------------------------------------

def build_catalog_resource_text() -> str:
    """Compact JSON catalog of authentic Siemens S7-300/400 MLFB modules."""
    summary = [
        {
            "mlfb": m.mlfb,
            "name": m.short_name,
            "family": m.family,
            "category": m.category,
            "description": m.description,
            "input_bytes": m.input_bytes,
            "output_bytes": m.output_bytes,
            "backplane_ma": m.backplane_ma,
            "supply_ma": m.supply_ma,
            "slot_width": m.slot_width,
        }
        for m in S7_HARDWARE_CATALOG
    ]
    return json.dumps({"module_count": len(summary), "modules": summary}, indent=2)


def build_iec_reference_resource_text() -> str:
    """Concise IEC 61131-3 reference guide for STEP 7 Classic programming."""
    return "\n".join([
        "# Siemens STEP 7 Classic IEC 61131-3 Quick Reference",
        "",
        "## Block Language Codes (SUBBLK.DBF BLKLANG)",
        "- 00001 = STL / AWL (Instruction List)",
        "- 00002 = LAD / KOP (Ladder Logic — strict BLD 100/101/102/103 + NOP 0 rules)",
        "- 00003 = FBD / FUP (Function Block Diagram — pure FBD networks)",
        "- 00004 = SCL / ST  (Structured Control Language — S7SC.exe)",
        "- 00005 = S7-GRAPH  (Sequential Function Chart — .gr7)",
        "- 00006 = DB / UDT  (Global/Instance Data Blocks & User Defined Types)",
        "",
        "## Critical STEP 7 Classic Rules",
        "1. LAD/KOP: Every S7 Timer/Counter/Math/Move box needs exact BLD 101/102 and NOP 0 placeholders.",
        "   Always call s7_convert_to_lad or s7_validate_lad_compliance before writing LAD blocks.",
        "2. FBD/FUP: Use s7_convert_to_fbd + s7_validate_fbd_compliance; avoid STL-only jumps (JL, LOOP).",
        "3. SCL: Use := for assignment, = for equality, <> for inequality, AND/OR/NOT, END_IF/END_FOR.",
        "4. S7-GRAPH: Every step must be reachable from an initial step; avoid sink states without transitions.",
        "5. DB/UDT alignment: BOOL packs 8 bits/byte; WORD/INT/DWORD/DINT/REAL align to even byte offsets.",
        "6. Live PLC writes require BOTH confirm_live_write=True AND S7_WRITES_ENABLED=true.",
        "7. Concurrent edits: pass expected_content_hash from your last read to prevent silent overwrites.",
    ])


def build_prompt_new_project_wizard(project_name: str = "MyS7Project", cpu_mlfb: str = "6ES7 315-2EH14-0AB0") -> str:
    return (
        f"Create a new Siemens STEP 7 Classic project named '{project_name}' with CPU '{cpu_mlfb}':\n"
        f"1. Call s7_doctor to confirm whether live SIMATIC COM or offline/virtual mode is active.\n"
        f"2. Call s7_hw_configure_station to assemble Rack 0 (PS in Slot 1, CPU '{cpu_mlfb}' in Slot 2, "
        f"DI/DO/AI/AO modules in Slots 4+) and verify power budget & I/O addresses via s7_hw_validate_station.\n"
        f"3. Export the validated hardware configuration with s7_hw_export_cfg and, if live COM is active, "
        f"call s7_com_create_project and s7_hw_import_cfg.\n"
        f"4. Create Data Blocks / UDTs via s7_create_udt and s7_create_data_block, write control logic in "
        f"LAD/FBD/SCL/GRAPH, and verify execution in VirtualS7PLC or S7-PLCSIM before downloading."
    )


def build_prompt_safe_commissioning(host: str = "192.168.0.1") -> str:
    return (
        f"Run a safe read-only commissioning audit against PLC '{host}':\n"
        f"1. Call s7_runtime_cpu_diagnostics(host='{host}') to inspect CPU order number, mode, and diagnostic buffer.\n"
        f"2. Call s7_monitor_watch_table with critical interlocks, E-stop bits, and process analog inputs.\n"
        f"3. Export current offline/online blocks and run s7_compare_blocks to inspect differences before any write.\n"
        f"4. Never enable S7_WRITES_ENABLED or call s7_runtime_cpu_control without explicit operator authorization."
    )


def build_prompt_convert_and_verify_lad(block_name: str = "FC1") -> str:
    return (
        f"Convert and verify block '{block_name}' as 100% pure STEP 7 Classic LAD/KOP:\n"
        f"1. Read current source and record its content_hash.\n"
        f"2. Run s7_convert_to_lad to normalize STL into pure LAD with valid BLD/NOP 0 structure.\n"
        f"3. Verify with s7_validate_lad_compliance (must pass with 0 violations).\n"
        f"4. Write back passing expected_content_hash, then run a scan-cycle verification in VirtualS7PLC or PLCSIM."
    )


def build_prompt_plcsim_automated_test(block_name: str = "OB1") -> str:
    return (
        f"Run an automated closed-loop simulation test on '{block_name}':\n"
        f"1. Load logic into VirtualS7PLC (s7_virtual_plc_load) or connect S7-PLCSIM (s7_plcsim_control).\n"
        f"2. Drive deterministic input sequences (start/stop, fault injection, analog ramps), step scan cycles, "
        f"and assert expected outputs (Q bits, M markers, DB values, timers, counters).\n"
        f"3. Report pass/fail per scenario step with exact observed vs expected values."
    )


__all__ = [
    "run_doctor",
    "generate_client_config",
    "build_catalog_resource_text",
    "build_iec_reference_resource_text",
    "build_prompt_new_project_wizard",
    "build_prompt_safe_commissioning",
    "build_prompt_convert_and_verify_lad",
    "build_prompt_plcsim_automated_test",
]
