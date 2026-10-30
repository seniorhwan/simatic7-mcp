# Comprehensive Architectural Plan (/plan): Industrial Automation MCP Server Evolution

**Document ID**: `PLAN-M3-ARCHITECTURAL-PLAN`  
**Author**: Worker Report Author (`worker_report_1`)  
**Scope**: Concrete architectural roadmap, cross-pollination design specifications, and prioritized implementation plan across all local MCP servers.  
**Date**: October 30, 2026  
**Status**: Authoritative Deliverable (Milestone M3)  

---

## 1. Strategic Vision & Architectural Principles

The comparative analysis across `Czarnak/tia-portal-mcp`, `simatic7 mcp`, `gx works mcp`, and `ispsoft mcp` reveals a fundamental truth: **Industrial automation MCP servers must bridge two opposing worlds**. On one side is the dynamic, asynchronous, LLM-driven Model Context Protocol; on the other is the deterministic, synchronous, single-threaded, and legacy Windows-locked world of PLC engineering suites.

### Core Architectural Principles:

```
+--------------------------------------------------------------------------------------------------------------------+
|                                         CORE ARCHITECTURAL PRINCIPLES                                              |
+--------------------------------------------------------------------------------------------------------------------+

1. FAIL-CLOSED DEFENSE-IN-DEPTH
   - Hardware write protections, optimistic concurrency tokens, and pre-flight static safety analyzers must prevent
     unsafe PLC mutations. If any guard fails, execution halts immediately without partial side effects.

2. ASYNCHRONOUS DECOUPLING OF LEGACY TOOLS
   - Modern MCP host processes (stdio JSON-RPC) must never directly block on slow or fragile legacy desktop UIs.
     Legacy COM and GUI engines must run in isolated out-of-process workers or private Windows desktops.

3. OPTIMISTIC CONCURRENCY VIA CONTENT HASHING
   - Every read must return a cryptographically signed content digest (SHA-256); every mutation must provide the
     expected hash to prevent overwriting concurrent human modifications made in vendor IDEs.

4. SEAMLESS AIR-GAPPED IPC DEPLOYABILITY
   - Solutions must eliminate heavy runtime dependencies, complex offline wheel compilations, and fragile PowerShell
     surrogates, moving toward self-contained, lightweight native binaries for factory floor deployments.
```

---

## 2. Prioritized Recommendations for Local MCP Implementations (P0 to P3)

The following matrix categorizes and prioritizes the architectural improvements for `simatic7 mcp`, `gx works mcp`, and `ispsoft mcp`:

```
+--------------------------------------------------------------------------------------------------------------------+
|                                    PRIORITIZED ARCHITECTURAL RECOMMENDATIONS                                       |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| Priority | Recommendation                                     | Target Codebase  | Technical Impact | Complexity   |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P0**   | Adopt `contentHash` Optimistic Concurrency Control  | All Local MCPs   | Eliminates silent| Medium       |
| (Urgent) | across all POU, Block, and Network mutation tools  |                  | write overwrites |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P0**   | Port `CreateDesktopW` Private Desktop Automation   | `gx works mcp`   | Eliminates focus | Medium       |
| (Urgent) | to GX Works2 to eliminate interactive focus theft  |                  | stealing & deadlocks             |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P1**   | Replace SysWOW64 PowerShell Bridge with Native     | `simatic7 mcp`   | Reduces COM call | High         |
| (High)   | 32-bit C/C++ In-Process Worker (`s7_com_bridge`)   |                  | latency 99%      |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P1**   | Port Boolean AST Contact Graph Engine to Support   | `gx works mcp`   | Enables complex  | High         |
| (High)   | Branching Ladder Networks (ORB, ANB, MPS, MPP)     |                  | Mitsubishi rungs |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P1**   | Test Runner & Python Compatibility Hardening       | `simatic7 mcp` & | Prevents Pytest  | Low          |
| (High)   | (`hasattr` stdin guard; PEP 701 regex unnesting)   | `gx works mcp`   | collection crash |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P2**   | Pure-Python MELSEC MC Protocol Client (1E/3E/4E)   | `gx works mcp`   | Adds live device | High         |
| (Medium) | for direct FX and Q/L PLC telemetry over Ethernet  |                  | telemetry & test |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P2**   | Unified `mcp doctor` Diagnostic CLI Subsystem      | All Local MCPs   | Standardizes OS, | Low          |
| (Medium) | patterned after `tia-portal-mcp`                   |                  | COM, & driver checks             |
+----------+----------------------------------------------------+------------------+------------------+--------------+
| **P3**   | Standalone Compiled Edge Daemon (C/Rust)           | Edge Deployment  | Zero-dependency  | Very High    |
| (Low)    | bundling ISO-on-TCP, MC Protocol, and Modbus TCP   |                  | air-gapped IPCs  |              |
+----------+----------------------------------------------------+------------------+------------------+--------------+
```

---

## 3. Cross-Pollination Specifications

### 3.1 Specification 1: Optimistic Concurrency Control via `contentHash`
**Inspiration**: `Czarnak/tia-portal-mcp` (`PlcReadTools.cs` and `PlcWriteTools.cs`).  
**Targets**: `simatic7 mcp`, `gx works mcp`, `ispsoft mcp`.

#### Architectural Design:
1. **Hash Generation during Reads**:
   Whenever a tool returns project code (e.g., `s7_read_program`, `gx_read_program`, `ispsoft_read_pou`), it computes the canonical SHA-256 digest of the raw source payload:
   ```python
   def compute_content_hash(format_prefix: str, content: str | bytes) -> str:
       raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
       digest = hashlib.sha256(raw_bytes).hexdigest()
       return f"{format_prefix}:sha256:{digest}"
   ```
2. **Hash Validation during Writes**:
   All mutation tools (`s7_write_program`, `gx_write_program`, `ispsoft_write_ld_program`) add a required parameter: `expected_content_hash: str`.
3. **Execution Guard**:
   Prior to mutating the underlying file, container, or IDE block, the server re-reads the active state, calculates its current hash, and compares it:
   ```python
   current_hash = compute_content_hash("awl", current_awl_source)
   if current_hash != expected_content_hash:
       return {
           "status": "REJECTED_BEFORE_WRITE",
           "failure_category": "state_changed",
           "error": f"Target was modified concurrently. Current: {current_hash}, Expected: {expected_content_hash}",
           "current_content_hash": current_hash
       }
   ```
4. **Benefit**: Prevents race conditions where an automation engineer edits a block in the GUI while an AI agent prepares a mutation based on stale state.

---

### 3.2 Specification 2: Isolated Private Windows Desktop for GX Works2
**Inspiration**: `ispsoft mcp` (`CreateDesktopW` + `CreateProcessW` pipeline).  
**Target**: `gx works mcp`.

#### Architectural Design:
Currently, `gx works mcp` automates `GXW2.exe` on the interactive user desktop (`winsta0\default`). If the operator moves the mouse or types during an automated compile, the keystrokes collide, causing modal dialog deadlocks or corrupted CSV imports.

```
+----------------------------------------------------------------------------------------------------+
|                         PRIVATE DESKTOP ISOLATION PIPELINE FOR GX WORKS2                           |
+----------------------------------------------------------------------------------------------------+

MCP Client (VS Code)
   │
   │ 1. gx_write_program(rungs, ...)
   ▼
FastMCP Server (gx_works2_mcp_server.py)
   │
   │ 2. CreateDesktopW("codex-gx-<uuid>", ...)
   │ 3. CreateProcessW("GXW2.exe", lpDesktop="codex-gx-<uuid>")
   ▼
+----------------------------------------------------------------------------------------------------+
| Isolated Windows Desktop ("codex-gx-<uuid>")                                                       |
|   ├── GXW2.exe runs completely invisible to user (No window flashing, no mouse hijacking)         |
|   ├── PostMessage(WM_COMMAND, 0x755, 0) triggers CSV Read on hidden window                         |
|   ├── SendKeys F4 (Build) and Alt+T (Check Program) execute in private message pump                |
|   └── Inspects SysListView32 results without display hardware dependencies                         |
+----------------------------------------------------------------------------------------------------+
   │
   │ 4. TerminateProcess / CloseDesktop handle
   ▼
Return clean JSON result to MCP Client
```

#### Implementation Details:
- Wrap Win32 `CreateDesktopW`, `SetThreadDesktop`, and `CreateProcessW` inside a reusable Python context manager: `with private_desktop("codex-gx") as desktop:`.
- Ensure `atexit` hooks destroy orphaned desktops on unexpected Python termination.

---

### 3.3 Specification 3: AST Boolean Contact Graph Compiler for Mitsubishi GX Works2
**Inspiration**: `simatic7 mcp` (`lad_converter.py`).  
**Target**: `gx works mcp`.

#### Architectural Design:
Currently, `gx works mcp` explicitly rejects branching ladder logic (`ORB`, `ANB`, `MPS`, `MRD`, `MPP`), restricting generated programs to strictly linear rungs (series contacts followed by one coil). This severely limits AI code generation for Mitsubishi PLCs.

`simatic7 mcp` solved this for Siemens using an AST Boolean Contact Graph engine:
1. **Boolean Expression Parsing**: Parses arbitrary Boolean logic into an expression tree (`AndNode`, `OrNode`, `NotNode`, `ContactNode`).
2. **Canonical Disjunctive Normal Form (DNF) / Binary Decision Diagram (BDD) Reduction**: Simplifies redundant contacts while preserving exact truth tables.
3. **Mitsubishi Instruction Emitter**:
   - Compiles parallel branches using `ORB` (Block OR).
   - Compiles series branches using `ANB` (Block AND).
   - Compiles multi-coil rungs using Mitsubishi branch stack instructions (`MPS` = Push, `MRD` = Read, `MPP` = Pop).
4. **Verification**: Simulates the emitted instruction list against an in-memory virtual machine across all $2^N$ input states to mathematically prove truth-table equivalence before saving to CSV.

---

### 3.4 Specification 4: Native 32-Bit C COM Worker (`s7_com_bridge.exe`)
**Inspiration**: `Czarnak/tia-portal-mcp` (Persistent worker process architecture).  
**Target**: `simatic7 mcp`.

#### Architectural Design:
Eliminates the 1,500 ms SysWOW64 PowerShell bridge by deploying a dedicated, lightweight 32-bit C/C++ console executable.

```
+----------------------------------------------------------------------------------------------------+
|                         NATIVE 32-BIT C COM WORKER REPLACEMENT ARCHITECTURE                        |
+----------------------------------------------------------------------------------------------------+

64-bit Python MCP Server (FastMCP)
   │
   │ stdio Anonymous Pipes (Newline-Delimited JSON-RPC)
   ▼
s7_com_bridge.exe (Statically Compiled 32-bit x86 C/C++ Binary, ~120 KB)
   │
   ├── Runs continuously in background (Zero process spawn overhead per tool call)
   ├── Links directly to OLE32.DLL and Siemens S7ABATCX.DLL (32-bit address space)
   ├── Initializes COM Apartment: CoInitializeEx(NULL, COINIT_APARTMENTTHREADED)
   ├── Holds persistent ISimatic / IS7Project COM interfaces in memory
   └── Dispatches commands:
         {"cmd": "list_blocks", "project": "C:\\Projects\\Plant1"}
         {"cmd": "export_block", "block": "OB1", "format": "awl"}
         {"cmd": "compile_source", "source": "C:\\Temp\\candidate.awl"}
   │
   TOTAL EXECUTION LATENCY PER CALL: < 15 milliseconds (vs 1,500 ms in PowerShell)
```

#### C Implementation Contract:
- Target Compiler: Visual Studio MSVC `x86` with `/MT` (Static C Runtime).
- No external dependencies beyond Windows native `ole32.lib` and `oleaut32.lib`.
- Protocol: Reads JSON commands from stdin; writes JSON responses to stdout.

---

### 3.5 Specification 5: Pure-Python MELSEC MC Protocol Client
**Inspiration**: `simatic7 mcp` (`S7RuntimeClient` pure-Python socket engine).  
**Target**: `gx works mcp`.

#### Architectural Design:
Currently, `gx works mcp` is purely an offline file-based editor. To provide runtime diagnostics and live tag inspection, implement a native pure-Python client for the Mitsubishi MELSEC Communication (MC) Protocol:
1. **Frame Formats**:
   - **1E Frames**: For FX3U / FX3G / FX3S controllers communicating over ENET modules or Ethernet ports (Sub-header `0x00` = Batch Read, `0x01` = Batch Write).
   - **3E / 4E Frames (QnA-compatible)**: For Q Series and L Series CPUs over TCP/UDP Port 5000/5001.
2. **Device Address Encoding**:
   - Bit Devices: `X*`, `Y*`, `M*`, `S*`, `B*`, `F*`.
   - Word Devices: `D*`, `R*`, `W*`, `TN*`, `CN*`.
3. **Safety Guardrails**:
   - Port `confirm_live_write` and `MELSEC_WRITES_ENABLED` environment variable guards directly from `simatic7 mcp` to guarantee that live PLC writes cannot occur unintentionally.

---

## 4. Migration & Implementation Roadmap

The implementation plan is structured into four distinct, phased milestones:

```
+----------------------------------------------------------------------------------------------------+
|                                    IMPLEMENTATION ROADMAP (PHASES 1 - 4)                           |
+----------------------------------------------------------------------------------------------------+

[PHASE 1: Concurrency & Safety Hardening] (Weeks 1 - 2)
  ├── Deliverable 1.1: Implement contentHash across simatic7, gx works, and ispsoft.
  ├── Deliverable 1.2: Standardize failure categories across all local servers to match tia-portal-mcp.
  ├── Deliverable 1.3: Add unified `mcp doctor` diagnostic CLI checks to all local servers.
  └── Deliverable 1.4: Add defensive `hasattr(sys.stdin, "reconfigure")` guards and unnest line 7053 f-string regex.

[PHASE 2: Desktop Isolation & Reliability] (Weeks 3 - 4)
  ├── Deliverable 2.1: Port CreateDesktopW private desktop manager to gx works mcp.
  ├── Deliverable 2.2: Migrate GX Works2 CSV import and compile automation to private desktop.
  └── Deliverable 2.3: Implement Session 0 detection and automated fallback modes.

[PHASE 3: High-Performance Native Acceleration] (Weeks 5 - 6)
  ├── Deliverable 3.1: Build and compile native 32-bit C helper: s7_com_bridge.exe.
  ├── Deliverable 3.2: Replace SysWOW64 PowerShell bridge in simatic7 mcp with s7_com_bridge.
  └── Deliverable 3.3: Implement persistent worker keep-alive and health monitoring.

[PHASE 4: Ladder AST & Live Telemetry Expansion] (Weeks 7 - 8)
  ├── Deliverable 4.1: Adapt Boolean Contact Graph AST compiler to generate Mitsubishi ORB/ANB/MPS.
  ├── Deliverable 4.2: Implement pure-Python MELSEC MC Protocol (1E and 3E frames) in gx works mcp.
  └── Deliverable 4.3: Deploy automated end-to-end regression test suite across all 4 servers.
```

---

## 5. Risk Matrix & Mitigation Strategies

```
+----------------------------------------------------------------------------------------------------+
|                                      RISK MATRIX & MITIGATION                                      |
+----+--------------------------------+----------+--------+------------------------------------------+
| #  | Identified Technical Risk      | Severity | Likeli | Concrete Mitigation Strategy             |
+----+--------------------------------+----------+--------+------------------------------------------+
| R1 | Accidental Live PLC Overwrite  | Critical | Low    | Mandatory dual-guard: explicit tool      |
|    | by Autonomous AI Agent         |          |        | parameter `confirm_live_write=True` AND  |
|    |                                |          |        | environment variable `*_WRITES_ENABLED=1`|
+----+--------------------------------+----------+--------+------------------------------------------+
| R2 | Concurrent Human Edit Overwrite| High     | Medium | Enforce `expected_content_hash` check    |
|    | in Vendor Engineering Suite    |          |        | on every write. Abort if hash mismatches.|
+----+--------------------------------+----------+--------+------------------------------------------+
| R3 | Antivirus / WDAC Blocks 32-bit | High     | Low    | Digitally sign `s7_com_bridge.exe` with  |
|    | C Helper Binary on Factory IPC |          |        | local corporate PKI certificate; retain  |
|    |                                |          |        | PowerShell bridge as fallback.           |
+----+--------------------------------+----------+--------+------------------------------------------+
| R4 | Private Desktop GUI Deadlock   | Medium   | Low    | Enforce strict execution timeouts (15s)  |
|    | on Hidden Error Dialogs        |          |        | and automated child process termination. |
+----+--------------------------------+----------+--------+------------------------------------------+
| R5 | Air-Gapped Factory IPC Python  | High     | High   | Bundle all offline wheels into a pinned  |
|    | Environment Incompatibility    |          |        | `vendor_wheels/` cache with an automated |
|    |                                |          |        | offline PowerShell installation script.  |
+----+--------------------------------+----------+--------+------------------------------------------+
| R6 | Pytest Stdin Capture Collision | Medium   | High   | Wrap `sys.stdin.reconfigure()` with      |
|    | ('DontReadFromInput' crash)    |          |        | `hasattr` guard; invoke CI with `-s`.    |
+----+--------------------------------+----------+--------+------------------------------------------+
```

---

## 6. Conclusion

This architectural plan delivers a realistic, actionable path to elevate local industrial MCP servers to enterprise production quality. By cross-pollinating the best features of each project—the rigorous optimistic concurrency of `Czarnak/tia-portal-mcp`, the multi-tier hybrid telemetry of `simatic7 mcp`, the safe non-invasive file handling of `gx works mcp`, and the desktop isolation techniques of `ispsoft mcp`—the industrial automation ecosystem can achieve rock-solid, high-performance integration with next-generation AI tooling.
