# Comparative Technical Analysis: Industrial Automation MCP Server Implementations
## Siemens TIA Portal V21 (`Czarnak/tia-portal-mcp`) vs. Siemens STEP 7 Classic (`simatic7 mcp`), Mitsubishi MELSOFT GX Works2 (`gx works mcp`), and Delta ISPSoft (`ispsoft mcp`)

**Document ID**: `REPORT-M1-COMPARATIVE-ANALYSIS`  
**Author**: Worker Report Author (`worker_report_1`)  
**Scope**: Comprehensive, multi-dimensional technical comparison across all 4 industrial PLC MCP codebases.  
**Date**: October 30, 2026  
**Status**: Authoritative Deliverable (Milestone M1)  

---

## 1. Executive Overview & System Archetypes

Industrial automation platforms—traditionally closed, proprietary, Windows-centric, and real-time oriented—present substantial architectural impedance when integrated with modern AI tooling via the Model Context Protocol (MCP). Across the four investigated codebases, we identify **four distinct architectural archetypes** designed to bridge this divide:

```
+--------------------------------------------------------------------------------------------------------------------+
|                                         ARCHITECTURAL SYSTEM ARCHETYPES                                            |
+--------------------------------------------------------------------------------------------------------------------+

1. DUAL-RUNTIME ISOLATED OUT-OF-PROCESS ARCHETYPE (Czarnak/tia-portal-mcp)
   +------------------------------------+   Anonymous IPC Pipe   +------------------------------------+
   | Modern Host (.NET 10.0 / net10.0)  | <====================> | Legacy Worker (.NET Framework 4.8) |
   | - Official ModelContextProtocol SDK|   (JSON-RPC / stdio)   | - Siemens.Engineering.dll (Remoting|
   | - Fast async MCP stdio transport   |                        | - Single-threaded Openness gate    |
   +------------------------------------+                        +------------------------------------+

2. TRIPLE-ENGINE HYBRID PROTOCOL & AUTOMATION ARCHETYPE (simatic7 mcp)
   +--------------------------------------------------------------------------------------------------+
   | Python 3.12+ FastMCP Host (stdio)                                                                |
   |  ├── Tier 1: Direct ISO-on-TCP RFC 1006 / S7comm (Port 102) Socket Engine (Live PLC Telemetry)   |
   |  ├── Tier 2: 32-bit SysWOW64 PowerShell Out-of-Process COM Bridge (S7ABATCX.DLL / DBF Patcher)   |
   |  └── Tier 3: Serialized pywinauto GUI Engine with Win32 Remote VirtualAllocEx Memory Injection   |
   +--------------------------------------------------------------------------------------------------+

3. SERIALIZED GUI WIN32 MESSAGE & CSV ROUND-TRIP ARCHETYPE (gx works mcp)
   +--------------------------------------------------------------------------------------------------+
   | Python 3.10+ FastMCP Host (stdio)                                                                |
   |  ├── Win32 Asynchronous Message Dispatch (WM_COMMAND 0x755 / 0x756 via PostMessage)              |
   |  ├── Native UTF-16LE Tab-Delimited CSV Rung Serialization / Deserialization                      |
   |  └── Codejock XTP Owner-Drawn Menu Keyboard Macro Automator (Alt+T -> Down -> Enter Program Check|
   +--------------------------------------------------------------------------------------------------+

4. REVERSE-ENGINEERED CONTAINER & PRIVATE DESKTOP OCR ARCHETYPE (ispsoft mcp)
   +--------------------------------------------------------------------------------------------------+
   | Python 3.11+ FastMCP Host (stdio)                                                                |
   |  ├── Raw Binary .isp DEFLATE Container Codec (zlib wbits=-15 + Little-Endian Pointer Math)       |
   |  ├── PKZIP 2.0 ZipCrypto Encrypted .mpu Package Codec                                            |
   |  ├── Offline Delta DVP Octal-to-Modbus PDU Address Mapping Table                                 |
   |  └── Win32 Isolated Hidden Desktop (CreateDesktopW) + GDI BitBlt + WinRT Windows.Media.Ocr Engine |
   +--------------------------------------------------------------------------------------------------+
```

### 1.1 Dual-Runtime Isolated Architecture: `Czarnak/tia-portal-mcp`
- **Archetype Principle**: Total process and runtime decoupling between modern AI tooling protocols and legacy automation runtimes.
- **Problem Solved**: Siemens TIA Portal Openness (`Siemens.Engineering.dll`) is tied to the legacy Microsoft .NET Framework (specifically .NET Framework 4.8) and depends heavily on `System.Runtime.Remoting`, which Microsoft deprecated and removed in .NET Core/.NET 5–10. Modern MCP C# SDKs require .NET 8, 9, or 10.
- **Solution**: The host process runs on modern **.NET 10.0**, providing an asynchronous, strongly-typed MCP interface over standard I/O. When industrial engineering operations are required, it delegates to a dedicated, long-running child process running on **.NET Framework 4.8** via anonymous redirected pipes using single-line canonical JSON.

### 1.2 Triple-Engine Hybrid Architecture: `simatic7 mcp`
- **Archetype Principle**: Multi-tiered operational fallbacks combining low-level wire sockets, out-of-process legacy COM bridges, and GUI automation.
- **Problem Solved**: Siemens STEP 7 Classic (v5.5/v5.6/v5.7) is a 32-bit legacy MFC application dating back to Windows 95/NT. It provides an in-process 32-bit COM automation server (`S7ABATCX.DLL` / ProgID `Simatic.Simatic`), an interactive editor (`s7tgtopx.exe`), and Ethernet communication via proprietary S7comm over ISO-on-TCP (RFC 1006).
- **Solution**: A 64-bit Python FastMCP server implements three operational engines:
  1. *Tier 1 (Wire)*: A pure-Python RFC 1006 / S7comm socket engine communicating directly with S7-300/400 CPUs or CP343-1/CP443-1 communications processors over Port 102.
  2. *Tier 2 (Headless COM)*: An out-of-process bridge spawning 32-bit PowerShell (`SysWOW64\WindowsPowerShell\v1.0\powershell.exe`) with base64 encoded scripts to manipulate the 32-bit COM server without address space collisions, combined with binary dBASE III table patching (`ombstx/offline/*/SUBBLK.DBF`) to set block language flags (`BLKLANG='00002'`).
  3. *Tier 3 (GUI)*: A synchronized `pywinauto` controller capable of cross-process virtual memory allocation (`VirtualAllocEx` with 32-bit `_LVITEM32` structures) to read owner-drawn 32-bit MFC SysListView32 controls without access violations.

### 1.3 Serialized Win32 Message & CSV Round-Trip: `gx works mcp`
- **Archetype Principle**: Non-invasive, backup-verified file-based round-tripping orchestrated through native Windows messaging.
- **Problem Solved**: Mitsubishi MELSOFT GX Works2 (`GXW2.exe`) lacks public COM/OLE automation interfaces, has no public headless compiler CLI, and encrypts its native `.gwx` project files.
- **Solution**: Operates entirely through serialized GUI automation and native CSV file import/export:
  1. Win32 `PostMessage` (`WM_COMMAND = 0x0111`) triggers internal GX Works2 menu actions (`0x755` = Read from CSV, `0x756` = Write to CSV) asynchronously, preventing modal dialog thread deadlocks.
  2. Ladder logic is converted into Mitsubishi-compliant UTF-16LE tab-delimited CSV matrices.
  3. Triggers compilation via `F4` keystrokes and navigates owner-drawn Codejock XTP menus using keyboard macros (`Alt+T -> Down -> Enter`) to execute the internal program verification engine.

### 1.4 Reverse-Engineered Container & Private Desktop OCR: `ispsoft mcp`
- **Archetype Principle**: Direct binary file surgery combined with invisible OS-isolated GUI compilation and optical character recognition.
- **Problem Solved**: Delta Electronics ISPSoft 3.24 (`NewISPSoft.exe`) is an Embarcadero Delphi VCL application with no programmatic API, no command-line compiler, and an owner-drawn compiler diagnostic grid that completely conceals text from Windows UI Automation (UIA) and Microsoft Active Accessibility (MSAA).
- **Solution**:
  1. Direct binary container codecs (`containers.py` and `packages.py`) disassemble and rebuild Delta's raw DEFLATE `.isp` project files and ZipCrypto-encrypted `.mpu` archives, updating internal little-endian pointer tables and CRC32 checksums without touching the GUI.
  2. An isolated Windows desktop (`CreateDesktopW`) spawns a headless worker instance of `NewISPSoft.exe` in the background without stealing user focus or interfering with running engineering sessions.
  3. A Win32 GDI screen-capture (`BitBlt`/`PrintWindow`) grabs compiler window bitmaps, which are parsed via Windows Runtime OCR (`Windows.Media.Ocr`) to extract compilation error and warning counts.

---

## 2. Repository Topologies & Component Architectures

### 2.1 Comparative Repository Layout

```
Czarnak/tia-portal-mcp/                 simatic7 mcp/                      gx works mcp/                      ispsoft mcp/
├── TiaMcpServer/ (net10.0 Host)        ├── simatic_s7_mcp_server.py       ├── gx_works2_mcp_server.py        ├── ispsoft_mcp_server.py
│   (212 files, 23,342 lines)           │   (7,334 lines, main server)     │   (2,096 lines, main server)     │   (3,108 lines, FastMCP server)
├── TiaMcpServer.Contracts/             ├── lad_converter.py               ├── tests/                         ├── ispsoft_mcp/
│   (137 files, 7,429 lines DTOs)       │   (1,427 lines, AST compiler)    │   └── test_gx_works2_mcp_server  │   ├── containers.py (.isp codec)
├── TiaMcpServer.OpennessWorker/        ├── tests/                         │       (440 lines, 27 tests)      │   ├── packages.py (.mpu codec)
│   (147 files, 23,841 lines net48)     │   ├── test_simatic_s7_mcp_server ├── .vscode/                       │   ├── dvp_modbus.py (PDU map)
│   [Total Core: 496 files, 54,612 LOC] │   │   (1,802 lines, 82 tests)    │   └── mcp.json                   │   ├── dvp.py (IL validation)
├── TiaMcpServer.FakeWorker/            │   └── test_lad_converter_tool    └── README.md                      │   └── limits.py (Bounds & IO)
│   (6 files, 4,833 lines; Program=4.1k)│       (593 lines, 16 tests)                                         ├── ispsoft_native_ocr.ps1
└── TiaMcpServer.Tests/                 │   [Total: 2 files, 98 tests]                                        ├── tests/ (2 files, 74 tests)
    (405 files, 87,363 lines test C#)   ├── .vscode/                                                          └── README.md
    [Total Repo: 141,975 lines of C#]   └── README.md
```

### 2.2 Component Architecture Deep Dive

#### `Czarnak/tia-portal-mcp`
- **Separation of Concerns**: Strict multi-project solution structure. `TiaMcpServer.Contracts` has **zero** third-party or vendor dependencies, defining pure DTOs and failure category constants. `TiaMcpServer` handles MCP stdio protocol dispatch, CLI flags, continuation cursors, and security leasing. `TiaMcpServer.OpennessWorker` contains all proprietary Siemens Openness interactions.
- **Assembly Isolation & Packaging**: Siemens assemblies (`Siemens.Engineering.dll`) are dynamically resolved at runtime by `AssemblyResolver` via registry lookup (`HKLM\SOFTWARE\Siemens\Automation\InstalledApps\Totally Integrated Automation Portal V21`) and environment variables (`TiaPortalV21Dir`). Delay-signed reference stubs in `ref/` allow CI/CD compilation without distributing proprietary Siemens DLLs.

#### `simatic7 mcp`
- **Monolithic Multi-Tier Architecture**: Built around `simatic_s7_mcp_server.py` (7,334 lines) and `lad_converter.py` (1,427 lines).
- **Subsystem Breakdown**:
  1. *Validation & Guardrails*: Lines 1500–2800 parse S7 mnemonics (German vs. International), check bit addresses (`0..7`), enforce Star-Delta reciprocal interlocks (`KM1` vs. `KM2`), and detect duplicate coil assignments (`=`).
  2. *LAD AST Engine (`lad_converter.py`)*: Transforms arbitrary statement list (AWL) sequences into 100% LAD-compliant networks using an AST Boolean contact graph and S7 Status Word virtual machine.
  3. *COM Automation Bridge (`SimaticComBridge`)*: Lines 3422–3980 manage SysWOW64 PowerShell execution, unattended server mode (`$s7.UnattendedServerMode = $true`), and dBASE III table patching.
  4. *Socket Engine (`S7RuntimeClient`)*: Lines 4175–4550 handle pure-Python RFC 1006 TPKT, COTP connection requests, S7 Communication Setup jobs (`0xF0`), and Read/Write Var primitives (`0x04`/`0x05`).
  5. *GUI Controller (`SimaticManagerController`)*: Lines 4728–5760 wrap `pywinauto` and Win32 APIs to automate SIMATIC Manager.

#### `gx works mcp`
- **Compact File-Centric Architecture**: Centered on `gx_works2_mcp_server.py` (2,096 lines).
- **Subsystem Breakdown**:
  1. *Validation Subsystem*: Validates FX octal addressing (`X0..X7`, `X10..X17`) vs. Q/L hexadecimal addressing (`X0..XF`, `X10..X1F`), enforces linear-rung invariants, and detects Star-Delta same-scan switching hazards.
  2. *CSV Codec Subsystem*: Generates and parses tab-delimited UTF-16LE CSV files matching GX Works2's internal section format (`"PLC Information"`, `"Main Program"`, `"END"`).
  3. *GUI Automation Subsystem*: Manages GX Works2 window handles, dispatches `WM_COMMAND` `0x755`/`0x756`, handles modal confirmation popups, and drives owner-drawn XTP menus.

#### `ispsoft mcp`
- **Modular Package Architecture**: Clean split between the FastMCP server (`ispsoft_mcp_server.py`) and the `ispsoft_mcp/` core library.
- **Subsystem Breakdown**:
  1. *Binary Codecs (`containers.py`, `packages.py`)*: Direct little-endian binary parsing of Delta `.isp` containers, CRC32 recalculation, and ZipCrypto packaging.
  2. *Modbus Engine (`dvp_modbus.py`)*: Deterministic mapping of Delta PLC registers (X, Y, M, S, T, C, D) to Modbus Function Codes (FC 01, FC 02, FC 03) and 16-bit PDU offsets.
  3. *Defensive I/O Guard (`limits.py`)*: Implements path containment, 32 MB file ceilings, 512 member package limits, and atomic file replacement via temporary files prefixed with `.plc-`.
  4. *Desktop Isolation Subsystem*: Spawns `NewISPSoft.exe` inside a newly created Win32 desktop (`CreateDesktopW`), captures window bitmaps via GDI `BitBlt`, and invokes `Windows.Media.Ocr` via PowerShell.

---

## 3. MCP Protocol Implementation Patterns & Transport Layers

### 3.1 Frameworks & Transport Comparison

| Architectural Aspect | `Czarnak/tia-portal-mcp` | `simatic7 mcp` | `gx works mcp` | `ispsoft mcp` |
| :--- | :--- | :--- | :--- | :--- |
| **SDK / Framework** | Official `ModelContextProtocol` 2.3.0 (C#) | `mcp.server.fastmcp.FastMCP` (Python) | `mcp.server.fastmcp.FastMCP` (Python) | `mcp.server.fastmcp.FastMCP` (Python) |
| **Transport Protocol** | `stdio` via `WithStdioServerTransport()` | `stdio` via `mcp.run(transport="stdio")`| `stdio` via `mcp.run(transport="stdio")`| `stdio` via `mcp.run(transport="stdio")`|
| **Host Process Framework**| `Microsoft.Extensions.Hosting` (Generic Host)| CPython Event Loop / asyncio | CPython Event Loop / asyncio | CPython Event Loop / asyncio |
| **Output Schema Strategy** | Strongly typed via `UseStructuredContent=true` | Dynamic Pydantic validation via docstrings | Dynamic Pydantic validation via docstrings | Dynamic Pydantic validation via docstrings |
| **Windows stdio Defense** | Native .NET Core UTF-8 Console handles | `sys.stdin/stdout.reconfigure(encoding="utf-8")` | `sys.stdin/stdout.reconfigure(encoding="utf-8")` | `sys.stdin/stdout.reconfigure(encoding="utf-8")` |
| **Tool Execution Gating** | 3-Layer Access Modes (`ReadOnly`, `ReadWrite`, `Full`) | Advisory status strings / explicit safety flags | Explicit confirmation parameters | Dual guard: `confirm_live_write` + Env Var |

### 3.2 Canonical JSON & Deterministic Serialization (`tia-portal-mcp`)
A major architectural distinction in `Czarnak/tia-portal-mcp` is its deterministic JSON serializer (`TiaMcpServer.Json.CanonicalJson`):
- Enforces strict alphabetical property ordering.
- Preserves explicit null values.
- Enforces strict UTF-8 escaping.
- Dual-delivers results: The identical canonical JSON string is emitted as both the human-readable `TextContentBlock` and the structured `StructuredContent` JsonElement:
```csharp
// TiaMcpServer/Tools/StructuredToolResult.cs
public static CallToolResult CreateCanonical(string canonicalJson, bool isError = false)
{
    using var doc = JsonDocument.Parse(canonicalJson);
    return new CallToolResult
    {
        IsError = isError,
        Content = [new TextContentBlock { Text = canonicalJson }],
        StructuredContent = doc.RootElement.Clone()
    };
}
```

### 3.3 Optimistic Concurrency Control (`contentHash`)
To prevent concurrent editing hazards (e.g. an engineer modifying a block in the TIA Portal GUI while an LLM agent generates an edit), `tia-portal-mcp` incorporates SHA-256 content hashing across all read and write tools:
1. `plc_read` returns a cryptographically signed content hash:
   `contentHash: "xml:sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069"`.
2. `plc_write` requires callers to provide `expectedContentHash`.
3. The worker recalculates the hash of the live object in TIA Portal immediately prior to mutating; if the hashes differ, it aborts with error code `state_changed`.

In contrast, the three local Python servers rely on **pessimistic local backup snapshots** (e.g. `Codex-Simatic7/ob1-before-...awl`, `Codex-Gx/main-before-...csv`, `.plc-...` temp files). While this prevents unrecoverable loss, it lacks detection of concurrent human modifications during the agent's reasoning cycle.

### 3.4 Error Envelope & Failure Classification
- **`tia-portal-mcp`**: Defines an exhaustive closed vocabulary of 27 machine-readable failure constants (`WorkerFailureCategories`) including `validation_error`, `binding_conflict`, `portal_lost`, `state_changed`, `target_not_found`, and `engineering_rejected`. Native Siemens COM exceptions are caught, classified, and wrapped in structured envelopes containing inner exception messages and severity.
- **Local Servers (`simatic7`, `gx works`, `ispsoft`)**: Return top-level JSON objects containing structured failure codes (`SUCCESS`, `REJECTED_BEFORE_WRITE`, `BACKUP_FAILED`, `COMPILATION_FAILED`, `READBACK_MISMATCH`, `TRANSPORT_NOT_QUALIFIED`). Python exceptions are intercepted at the tool boundary to prevent unhandled JSON-RPC protocol crashes.

---

## 4. PLC Communication Mechanisms & Hardware Driver Layers

### 4.1 Comparative Communication Architecture

```
+---------------------------------------------------------------------------------------------------------------+
|                                      PLC COMMUNICATION MECHANISMS                                             |
+--------------------------+----------------------------+-----------------------------+-------------------------+
| Czarnak/tia-portal-mcp   | simatic7 mcp               | gx works mcp                | ispsoft mcp             |
+--------------------------+----------------------------+-----------------------------+-------------------------+
| Siemens TIA Openness API | Tier 1: ISO-on-TCP RFC 1006| No direct PLC comms.        | Offline Modbus mapping  |
| (.NET Remoting Proxies)  | Tier 2: 32-bit PS COM      | Operates strictly via       | SetupAPI serial scan    |
| Attaches to running      | Tier 3: pywinauto MFC GUI  | GX Works2 GUI & CSV files   | COMMGR XML driver parse |
| Siemens.Automation.Portal| S7comm Port 102 wire-level | (No MELSEC MC Protocol)     | Fail-closed live stubs  |
+--------------------------+----------------------------+-----------------------------+-------------------------+
```

### 4.2 Hardware Interfacing Deep Dive

#### 1. Siemens TIA Portal Openness (`tia-portal-mcp`)
- **API Model**: High-level managed object model (`Siemens.Engineering.TiaPortal`).
- **Connection Protocol**: Inter-process .NET Remoting channels to `Siemens.Automation.Portal.exe`.
- **Instance Binding**: Enumerates running instances via `TiaPortal.GetProcesses()`. Matches target projects by absolute path or unique PID. Rejects ambiguous multiple instances.
- **Security Confirmation**: Subscribes to `portal.Confirmation` events to answer Siemens security prompts automatically during active authorized sessions (`ConfirmationResult.Yes` in write modes; `ConfirmationResult.No` in read-only).

#### 2. Siemens STEP 7 Classic Hybrid Channels (`simatic7 mcp`)
- **Direct Wire-Level Socket Client (`S7RuntimeClient`)**:
  - Implements RFC 1006 TPKT (Header `0x03, 0x00, len_hi, len_lo`) and COTP Connection Request/Confirm over TCP Port 102.
  - Dynamically computes TSAP from rack and slot: `dst_tsap = (0x01 << 8) | ((rack & 0x07) * 32 + (slot & 0x1F))`.
  - Negotiates PDU sizes (480 bytes) and sends S7 Read Var (`0x04`) / Write Var (`0x05`) commands using S7ANY addressing (`0x12, 0x0A, 0x10, type, count, db_number, area, bit_address`).
- **Headless COM Automation (`SimaticComBridge`)**:
  - Connects to `Simatic.Simatic` (CLSID `{9A853ACF-BA8E-11D1-BAD8-0060086A411D}`).
  - Sets `$s7.UnattendedServerMode = $true` to suppress GUI dialogs.
  - Performs direct binary patching of `SUBBLK.DBF` dBASE III files to force the IDE to recognize compiled blocks as native LAD (`BLKLANG='00002'`).
- **GUI Engine (`SimaticManagerController`)**:
  - Manages SIMATIC Manager (`s7tgtopx.exe`) and LAD/FBD editor (`s7kotx.exe`) via `pywinauto`.

#### 3. Mitsubishi MELSOFT GX Works2 Interface (`gx works mcp`)
- **Complete Abstraction from Wire**: Contains zero socket, serial, or MC Protocol implementations.
- **Mechanism**: All changes are exported or imported as UTF-16LE CSV files via `WM_COMMAND` `0x755` (Read from CSV) and `0x756` (Write to CSV).
- **Compilation**: Triggers F4 (`compile_build`) and automates the owner-drawn "Check Program" menu dialog (`Alt+T -> Down -> Enter -> Execute`).

#### 4. Delta Electronics ISPSoft Interface (`ispsoft mcp`)
- **Hardware Abstraction Strategy**: Leverages Delta's **COMMGR** background service rather than directly manipulating physical serial or Ethernet hardware.
- **Driver Discovery**: Parses COMMGR's active driver registry (`DriverInfo.dri`), extracting serial COM parameters, baud rates, and Ethernet IP/station configurations, detecting duplicate endpoint bindings.
- **Zero-Dependency Win32 Serial Discovery**: Queries `setupapi.dll` (`SetupDiGetClassDevsW` with `GUID_DEVCLASS_PORTS`) via `ctypes` to list active COM ports without taking locks.
- **Offline Modbus Address Engine**: Maps Delta DVP device addresses (octal inputs/outputs `X0..X377`, `Y0..Y377`; auxiliary relays `M0..M4095`; data registers `D0..D9999`) to exact Modbus Function Codes (FC 01, 02, 03) and 16-bit PDU addresses.
- **Fail-Closed Hardware Policy**: All physical connection tools return `TRANSPORT_NOT_QUALIFIED` until hardware validation criteria are fully satisfied.

---

## 5. Tool Catalog & Schema Analysis

Across all four repositories, a total of **99 dedicated MCP tools** are implemented. The table below catalogs every tool, its parameter signatures, and underlying execution mechanics.

### 5.1 Remote: `Czarnak/tia-portal-mcp` (8 Read-Only / 19 Full Mode Tools)

| Tool Name | Parameters | Return Schema | Underlying Mechanics |
| :--- | :--- | :--- | :--- |
| `get_project_status` | *None* | `GetProjectStatusResponse` | Reads project path, modified flag, Multiuser status via `TiaPortalSession`. |
| `browse_project_tree`| `startSelector`, `depth`, `pageSize`, `cursor` | `BrowseProjectTreeResponse` | Paged hierarchical tree snapshot; HMAC-authenticated memory cache. |
| `bind_project` | `projectPath`, `forceRebind`, `action`, `portalProcessId`, `serverAlias`, `group`, `serverProjectName` | `BindProjectResponse` | Binds active project or inspects server projects/sessions. |
| `plc_read` | `operations: PlcOperationRequest[]` (max 50) | `OperationBatchReadResponse` | Batch reads: `get_block_content`, `get_type_content`, `list_tag_tables`. Returns `contentHash`. |
| `read_cross_references`| `target`, `filter`, `maxResults` | `ReadCrossReferencesResponse` | Cross-reference traversal across blocks, tags, folders, or units. |
| `network_read` | `operations: NetworkOperationRequest[]` (max 50) | `OperationBatchReadResponse` | Inspects hardware config, subnets, nodes, equipment catalog. |
| `hmi_read` | `operations: HmiOperationRequest[]` (max 50) | `OperationBatchReadResponse` | Inspects WinCC Unified screens, alarms, tags, devices (20 operations). |
| `vci_read` | `operations: VciOperationRequest[]` (max 50) | `OperationBatchReadResponse` | Inspects Version Control Interface workspaces, mappings, formats. |
| `compile_check` | `plcName`, `blockPath` | `CompileCheckReport` | Invokes `PlcSoftware.Compile()` without mutating project state. |
| `plc_write` | `operations: PlcOperationRequest[]` (max 50), `dryRun` | `PlcGuardedWriteResponse` | Sequential software writes; enforces `expectedContentHash` and OB singleton rules. |
| `network_write` | `operations: NetworkOperationRequest[]` (max 50), `dryRun` | `NetworkGuardedWriteResponse` | Hardware mutation: adds devices, subnets, interfaces. |
| `hmi_write` | `operations: HmiWriteOperationRequest[]` (max 50), `dryRun`| `HmiGuardedWriteResponse` | Mutates WinCC Unified tags, tag tables, discrete/analog alarms. |
| `vci_write` | `operations: VciOperationRequest[]` (max 50), `dryRun` | `VciGuardedWriteResponse` | Manages VCI workspace syncing, exports, and mapping deletions. |
| `open_project` | `projectPath`, `forceRebind`, `dryRun` | `LifecycleWriteResponse` | Opens `.ap21` project or `.als21` local session in TIA Portal. |
| `create_project` | `projectDirectory`, `projectName`, `author`, `comment`, `dryRun` | `LifecycleWriteResponse` | Creates new TIA Portal project in running instance. |
| `save_project` | `dryRun` | `LifecycleWriteResponse` | Calls `Project.Save()` or `LocalSession.Save()`. |
| `save_project_as`| `targetDirectory`, `targetName`, `rebind`, `dryRun` | `LifecycleWriteResponse` | Saves copy and rebinds active session to copy. |
| `archive_project`| `archiveDirectory`, `archiveName`, `mode`, `saveBeforeArchive`, `dryRun` | `LifecycleWriteResponse` | Compresses project into `.zap21` archive. |
| `close_project` | `saveBeforeClose`, `dryRun` | `LifecycleWriteResponse` | Closes active project and invalidates session binding. |

### 5.2 Local: `simatic7 mcp` (29 Tools)

| Tool Name | Parameters | Return Schema | Underlying Mechanics |
| :--- | :--- | :--- | :--- |
| `s7_get_status` | *None* | JSON string | Checks SIMATIC Manager and block editor window handles. |
| `s7_list_supported_plc_types` | *None* | JSON string | Returns CPU profiles (S7-300, S7-400, WinAC) and memory boundaries. |
| `s7_create_project` | `project_name`, `plc_type`, `cpu_type`, `station_name`, `project_path`, `launch_if_closed`, `target_cpu` | JSON string | Creates STEP 7 project directory or automates GUI New Project. |
| `s7_open_project` | `project_path_or_name`, `launch_if_closed` | JSON string | Automates SIMATIC Manager project open. |
| `s7_open_block` | `block_name`, `project_name` | JSON string | Opens block in LAD/FBD/STL editor via COM or GUI. |
| `s7_validate_program` | `networks`, `symbols`, `symbol_table`, `device_comments`, `auto_fix_mnemonics`, `plc_type`, `mnemonic_language` | JSON string | Validates syntax, bit bounds, duplicate coils, Star-Delta interlocks. |
| `s7_write_program` | `networks`, `symbols`, `symbol_table`, `device_comments`, `delivery_mode`, `plc_type`, `block_name`, `run_compile_and_check`, `auto_fix_mnemonics`, `mnemonic_language` | JSON string | Atomic transaction: backup -> AWL export -> COM/GUI compile -> check -> readback verify -> rollback. |
| `s7_read_program` | `block_name` | JSON string | Exports block to AWL via COM/GUI and parses networks. |
| `s7_compile_and_check`| `block_name` | JSON string | Executes Check Block Consistency via GUI or COM. |
| `s7_write_symbol_table`| `symbols`, `import_via_gui`, `plc_type` | JSON string | Generates SDF/CSV symbol tables; optionally imports via GUI. |
| `s7_read_symbol_table` | `symbol_table_path` | JSON string | Parses `.SDF`, `.SEQ`, or `.CSV` symbol files. |
| `s7_write_device_comments`| `device_comments`, `plc_type`, `import_via_gui` | JSON string | Disambiguates symbols, infers types, updates symbol tables. |
| `s7_read_device_comments`| `comment_file_path` | JSON string | Extracts device comments from active symbol table. |
| `s7_search_symbols_and_comments`| `query`, `filter_type`, `comment_file_path`, `max_results` | JSON string | Regex/wildcard search across symbols and comments. |
| `s7_annotate_awl_code` | `source_code`, `networks`, `symbols`, `comment_file_path`, `style`, `plc_type` | JSON string | Injects inline comments into AWL (`COMMENTS`, `SYMBOLIC`, `ABSOLUTE`). |
| `s7_get_cross_references`| `networks`, `awl_source_path`, `block_name`, `symbols` | JSON string | Generates cross-reference matrix and bit/word overlap list. |
| `s7_get_memory_usage_map`| `networks`, `awl_source_path`, `block_name`, `plc_type`, `symbols` | JSON string | ASCII bit usage matrices across I, Q, M, DB, T, C. |
| `s7_get_block_overview` | `project_path_or_name` | JSON string | Inspects block sizes (MC7 bytes) and call hierarchy tree. |
| `s7_analyze_program` | `networks`, `awl_source_path`, `symbols`, `plc_type` | JSON string | Static safety analysis: interlocks, nesting <= 7, cycle time. |
| `s7_auto_detect_controllers`| *None* | JSON string | Scans registry, executables, and background S7 services. |
| `s7_validate_lad_compliance`| `source_code`, `awl_source_path` | JSON string | Validates LAD decompiler rules (parenthesized OR, single coil). |
| `s7_convert_to_ladder` | `source_code`, `awl_source_path`, `target_awl_path`, `block_title` | JSON string | AST Boolean contact graph compiler translating STL to pure LAD. |
| `s7_com_list_projects` | *None* | JSON string | Enumerates projects via 32-bit PowerShell `Simatic.Simatic` COM. |
| `s7_com_list_blocks` | `project_name` | JSON string | Enumerates blocks in COM container `1138689`. |
| `s7_com_read_block` | `block_name`, `project_name` | JSON string | Exports block to AWL using `IS7Block.GenerateSource()`. |
| `s7_com_write_block` | `block_name`, `source_code`, `awl_code`, `networks`, `awl_source_path`, `project_name`, `convert_to_lad`, `plc_type` | JSON string | Headless COM compile via `IS7Source.Compile()` + DBF LAD patch. |
| `s7_runtime_ping` | `host`, `ip_address`, `port`, `rack`, `slot` | JSON string | RFC 1006 COTP ping over Port 102 or PLCSIM COM ping. |
| `s7_runtime_read` | `address`, `data_type`, `host`, `ip_address`, `port`, `rack`, `slot`, `project_name` | JSON string | Pure-Python ISO-on-TCP Read Var (`0x04`) or PLCSIM read. |
| `s7_runtime_write` | `address`, `value`, `data_type`, `host`, `ip_address`, `port`, `rack`, `slot`, `confirm_live_write`, `project_name` | JSON string | Pure-Python ISO-on-TCP Write Var (`0x05`) guarded by safety flags. |

### 5.3 Local: `gx works mcp` (9 Tools)

| Tool Name | Parameters | Return Schema | Underlying Mechanics |
| :--- | :--- | :--- | :--- |
| `gx_get_status` | *None* | JSON string | Checks `GXW2.exe` window, editor state, and lists backups. |
| `gx_list_supported_plc_types` | *None* | JSON string | Returns supported families (`FXCPU`, `QCPU`, `LCPU`). |
| `gx_create_project` | `project_name`, `plc_type` | JSON string | Launches GX Works2 and automates New Project dialog. |
| `gx_read_program` | *None* | JSON string | Triggers CSV export (`WM_COMMAND 0x756`) and parses rung matrix. |
| `gx_validate_program` | `rungs`, `device_comments`, `auto_fix_negatives`, `plc_type`, `global_device_comments` | JSON string | Checks FX octal vs Q hex, duplicate OUT coils, Star-Delta hazard. |
| `gx_write_program` | `rungs`, `device_comments`, `global_device_comments`, `delivery_mode`, `plc_type`, `run_compile_and_check` | JSON string | Atomic transaction: backup -> CSV import (`0x755`) -> F4 build -> check -> readback verify -> rollback. |
| `gx_write_device_comments`| `device_comments`, `import_via_gui`, `plc_type` | JSON string | Writes UTF-16LE comment CSV; blocks GUI import. |
| `gx_read_global_device_comments`| `comment_csv_path` | JSON string | Parses UTF-16LE tab-delimited comment CSV. |
| `gx_compile_and_check`| *None* | JSON string | Sends F4 (`compile_build`) and automates Check Program dialog. |

### 5.4 Local: `ispsoft mcp` (42 Tools)

| Tool Name | Parameters | Return Schema | Underlying Mechanics |
| :--- | :--- | :--- | :--- |
| `ispsoft_get_status` | *None* | JSON string | Registry check, process check via Toolhelp32, last project. |
| `ispsoft_list_serial_ports` | *None* | JSON string | Direct Win32 `setupapi.dll` COM port scan + COMMGR driver map. |
| `ispsoft_list_commgr_drivers`| *None* | JSON string | Parses COMMGR's `DriverInfo.dri` XML; detects endpoint conflicts. |
| `ispsoft_get_ide_session` | *None* | JSON string | Enumerates top-level ISPSoft windows (`EnumWindows`). |
| `ispsoft_get_capabilities` | *None* | JSON string | Returns verified offline capabilities and guarded live stubs. |
| `ispsoft_list_supported_plc_types`| *None* | JSON string | Parses ISPSoft device XML catalog for supported DVP/AS models. |
| `ispsoft_start_background_session`| `project_path`, `timeout_seconds` | JSON string | Spawns `NewISPSoft.exe` on private desktop (`CreateDesktopW`). |
| `ispsoft_get_background_sessions`| *None* | JSON string | Lists active private desktop worker sessions. |
| `ispsoft_stop_background_session`| `session_id` | JSON string | Terminates worker process and closes desktop handle. |
| `ispsoft_start_ide` | `project_path`, `wait_seconds` | JSON string | Interactive GUI launch (`os.startfile`). |
| `ispsoft_open_project` | `project_path`, `wait_seconds` | JSON string | Opens project via Windows shell association. |
| `ispsoft_list_projects` | `directory`, `recursive` | JSON string | Directory walk extracting `.isp`/`.bak` container metadata. |
| `ispsoft_inspect_project` | `project_path` | JSON string | Unpacks container header: layout version, offsets, CRC32. |
| `ispsoft_get_project_overview` | `project_path` | JSON string | Inflates DEFLATE container; counts POUs, tasks, symbols. |
| `ispsoft_read_project_source` | `project_path` | JSON string | Returns raw uncompressed source text from container. |
| `ispsoft_read_pou` | `project_path`, `pou_name` | JSON string | Extracts POU source text and `LOCAL_VAR` table. |
| `ispsoft_read_ld_network` | `project_path`, `pou_name`, `network_number` | JSON string | Parses native ladder rung text from container. |
| `ispsoft_get_ld_program_structure`| `project_path`, `pou_name` | JSON string | Parses `[LD_NODE]` records and connection matrices. |
| `ispsoft_read_device_comments`| `project_path` | JSON string | Extracts `<DEVICE_CMT_START>` dictionary. |
| `ispsoft_get_cross_references` | `project_path`, `pou_name` | JSON string | Scans device reads and writes across POU source. |
| `ispsoft_get_memory_usage_map` | `project_path` | JSON string | Parses `SYSTEM_VAR` memory configuration limits. |
| `ispsoft_create_project_from_template`| `template_path`, `destination_path`| JSON string | Copies template atomically with container validation. |
| `ispsoft_write_st_candidate` | `project_path`, `pou_name`, `st_source`, `candidate_path` | JSON string | Replaces ST body in container; updates CRC and offsets. |
| `ispsoft_write_local_variables_candidate`| `project_path`, `pou_name`, `local_var_source`, `candidate_path` | JSON string | Replaces `LOCAL_VAR` table in container. |
| `ispsoft_write_ld_network_candidate`| `project_path`, `pou_name`, `network_number`, `network_source`, `candidate_path` | JSON string | Replaces single ladder rung in `.isp` container. |
| `ispsoft_write_ld_program` | `project_path`, `pou_name`, `il_source`, `candidate_path` | JSON string | Translates IL -> LD, patches container, compiles via OCR. |
| `ispsoft_write_device_comment_candidate`| `project_path`, `comments`, `candidate_path` | JSON string | Merges comments into container comment table. |
| `ispsoft_validate_il` | `il_source` | JSON string | Validates Delta Instruction List syntax and arity. |
| `ispsoft_generate_mpu` | `il_source`, `pou_name`, `output_path` | JSON string | Encodes IL to MPU nodes and builds ZipCrypto package. |
| `ispsoft_read_mpu` | `mpu_path` | JSON string | Decrypts ZipCrypto package and emits IL text. |
| `ispsoft_analyze_source` | `source_text` | JSON string | Static regex scan of device read/write cross-references. |
| `ispsoft_validate_device_comments`| `comment_map` | JSON string | Validates octal bounds (`0o377`) and device syntax. |
| `ispsoft_map_dvp_modbus_addresses`| `addresses` | JSON string | Offline translation of Delta devices to Modbus PDU offsets. |
| `ispsoft_compile_and_check` | `project_path`, `action`, `timeout_seconds`, `idle_wait_seconds` | JSON string | Headless compile on private desktop; parses WinRT OCR text. |
| `ispsoft_online_status` | `model` | JSON string | Guarded stub: fails closed (`TRANSPORT_NOT_QUALIFIED`). |
| `ispsoft_connect` | `model`, `commgr_driver` | JSON string | Guarded stub: fails closed (`TRANSPORT_NOT_QUALIFIED`). |
| `ispsoft_upload_from_plc` | `model`, `commgr_driver`, `output_path` | JSON string | Guarded stub: fails closed (`TRANSPORT_NOT_QUALIFIED`). |
| `ispsoft_start_monitor` | `model`, `commgr_driver` | JSON string | Guarded stub: fails closed (`TRANSPORT_NOT_QUALIFIED`). |
| `ispsoft_stop_monitor` | *None* | JSON string | Guarded stub: returns `ALREADY_OFFLINE`. |
| `ispsoft_runtime_read` | `model`, `addresses` | JSON string | Validates device syntax; fails closed. |
| `ispsoft_runtime_write` | `model`, `values`, `confirm_live_write` | JSON string | Guarded by `ISPSOFT_WRITES_ENABLED`; fails closed. |
| `ispsoft_download` | `project_path`, `model`, `confirm_download` | JSON string | Guarded by `ISPSOFT_WRITES_ENABLED`; fails closed. |

---

## 6. Concurrency, Threading & Synchronization Models

### 6.1 Process & Thread Synchronization Comparison

```
+--------------------------------------------------------------------------------------------------------------------+
|                                    SYNCHRONIZATION & CONCURRENCY MECHANISMS                                        |
+--------------------------------------------------------------------------------------------------------------------+

1. Czarnak/tia-portal-mcp:
   Host Async Loop =====> SemaphoreSlim(1, 1) Gate =====> Anonymous Pipe =====> Single-Threaded Worker Loop
   (Continuation cursors served lock-free from in-memory cache; Openness calls strictly serialized)

2. simatic7 mcp:
   MCP stdio Task  =====> @serialized_gui (RLock) =====> Win32 / SysWOW64 PowerShell Subprocess
   (Monkeypatched pywinauto mouse moves; WM_ACTIVATE focus wrappers for Session 0 / locked screens)

3. gx works mcp:
   MCP stdio Task  =====> @serialized_gui (RLock) =====> Asynchronous PostMessage (WM_COMMAND 0x755/0x756)
   (Non-blocking message dispatch + modal popup dismissal polling loop)

4. ispsoft mcp:
   MCP stdio Task  =====> _NATIVE_COMPILE_LOCK (threading.Lock)
                   =====> kernel32.CreateMutexW(None, False, "Local\\ISPSoftMCP.NativeCompile.v1")
                   =====> CreateDesktopW (Isolated Win32 WindowStation Desktop)
```

### 6.2 Analysis of Concurrency Invariants

1. **Vendor Thread Safety Invariants**:
   - *Siemens TIA Openness*: Strictly single-threaded per TIA Portal instance. Concurrent calls corrupt .NET Remoting channels. `tia-portal-mcp` enforces this via `SemaphoreSlim(1, 1)` in `PersistentWorkerTransport.cs`.
   - *Siemens STEP 7 Classic & GX Works2*: 32-bit MFC message pumps are strictly thread-affine. Concurrent GUI message dispatch results in race conditions and modal popup deadlocks. Both `simatic7 mcp` and `gx works mcp` enforce this using Python's `threading.RLock()` decorated via `@serialized_gui`.
   - *Delta ISPSoft*: The native compiler writes sidecars to a shared directory (`%APPDATA%\Delta Industrial Automation\ISPSoft_New`). Multiple concurrent compilations scramble compiler outputs. `ispsoft mcp` enforces synchronization using an in-process `threading.Lock` and a cross-process Windows named mutex (`kernel32.CreateMutexW(None, False, "Local\\ISPSoftMCP.NativeCompile.v1")`). Crucially, this uses the Windows session-scoped `Local\` namespace, preventing cross-session lockups and avoiding the elevated `SeCreateGlobalPrivilege` requirement while strictly serializing compiler invocations within the user session.

2. **Headless & Session 0 Isolation**:
   - A critical failure mode in industrial automation occurs when an MCP server runs on a locked PC (`Win+L`), an unattended server, or inside a Windows Service (Session 0). Standard UI automation (`pywinauto`) crashes with:
     `RuntimeError: There is no active desktop required for moving mouse cursor!`.
   - `simatic7 mcp` neutralizes this by overriding `pywinauto.mouse.move = lambda *args, **kwargs: None` and replacing `SetFocus()` with asynchronous Win32 `WM_ACTIVATE` messages.
   - `ispsoft mcp` bypasses interactive desktop constraints completely by creating a dedicated, invisible desktop via `CreateDesktopW(name, None, None, 0, 0x01FF, None)` and spawning `NewISPSoft.exe` inside it.

---

## 7. Memory Management, Resource Lifecycles & Bounded Limits

### 7.1 Cross-Process 32-Bit Memory Allocation (`simatic7 mcp`)
When 64-bit Python communicates with 32-bit MFC controls (`SysListView32`) in `S7tgtopx.exe`, passing a 64-bit pointer or 64-bit `LVITEM` struct triggers a fatal **`0xC0000005 Access Violation`**.
`simatic7 mcp` solves this by explicitly declaring the 32-bit Win32 structure in `ctypes`:
```python
# simatic_s7_mcp_server.py:3213-3228
class _LVITEM32(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("iItem", ctypes.c_int32),
        ("iSubItem", ctypes.c_int32),
        ("state", ctypes.c_uint32),
        ("stateMask", ctypes.c_uint32),
        ("pszText", ctypes.c_uint32),    # Explicit 32-bit pointer
        ("cchTextMax", ctypes.c_int32),
        ("iImage", ctypes.c_int32),
        ("lParam", ctypes.c_uint32),
        ("iIndent", ctypes.c_int32),
    ]
```
The server calls `VirtualAllocEx(hproc, None, 1024, MEM_COMMIT, PAGE_READWRITE)` to commit memory inside the remote 32-bit process, copies the `_LVITEM32` struct via `WriteProcessMemory`, dispatches `SendMessageA(hwnd_lv, LVM_GETITEMTEXTA, ...)` with the remote 32-bit address, reads back the text via `ReadProcessMemory`, and frees the remote memory with `VirtualFreeEx`.

### 7.2 Bounded Memory & Defensive I/O Ceilings (`ispsoft mcp`)
`ispsoft mcp/limits.py` prevents buffer overflows, zip bombs, and filesystem corruption by enforcing mathematical limits:
- `MAX_FILE_BYTES = 32 * 1024 * 1024` (32 MB hard limit on project containers).
- `MAX_SOURCE_BYTES = 16 * 1024 * 1024` (16 MB limit on decompressed source text).
- `MAX_MEMBERS = 512` (Zip bomb defense for `.mpu` packages).
- `atomic_write()` writes all modified files to `.plc-` prefixed temporary files, executes `stream.flush()` and `os.fsync()`, and performs atomic renaming, preventing corrupted partial files on disk power cuts.

### 7.3 Snapshot Cache Management & Cursors (`tia-portal-mcp`)
To support fast pagination without repeatedly querying TIA Portal Openness over IPC, `ProjectTreeSnapshotStore` and `HmiSnapshotStore` cache paged snapshots in memory:
- Maximum of **4 snapshots** retained simultaneously (LRU eviction).
- Maximum **4,000,000 characters** per snapshot.
- Maximum **16,000,000 characters** aggregate.
- **10-minute sliding idle expiration**.
- Continuation cursors are encrypted and HMAC-SHA256 authenticated, preventing cursor tampering or unauthorized cache access.

---

## 8. Platform, OS, Security & Privilege Dependencies

### 8.1 OS & Runtime Requirements Matrix

| Technical Requirement | `Czarnak/tia-portal-mcp` | `simatic7 mcp` | `gx works mcp` | `ispsoft mcp` |
| :--- | :--- | :--- | :--- | :--- |
| **Operating System** | Windows 10/11, Windows Server (64-bit) | Windows 7, 10, 11 (32-bit or 64-bit) | Windows 7, 10, 11 (32-bit or 64-bit) | Windows 10, 11 (64-bit) |
| **Required Runtime** | .NET 10 SDK + .NET Framework 4.8 | Python 3.12+ (PEP 701 f-strings) | Python 3.10+ | Python 3.11+ (Union syntax `X \| Y`) |
| **Windows Security Groups**| User MUST be in `Siemens TIA Openness` | Standard Windows User / Administrator | Standard Windows User / Administrator | Standard Windows User / Administrator |
| **Windows Subsystems Used**| Win32 Registry, Named Pipes | Win32 GDI, User32, SysWOW64 PowerShell | Win32 User32, MFC Codejock Menus | Win32 User32 Desktop, WinRT OCR, SetupAPI |
| **Admin Rights Required**| No (except for initial TIA install) | Recommended for COM registration | Not required | Not required |
| **Diagnostic Subsystem** | Built-in CLI command: `tia-mcp doctor` | Diagnostic tool: `s7_auto_detect_controllers`| Status tool: `gx_get_status` | Status tool: `ispsoft_get_status` |

### 8.2 The `Siemens TIA Openness` Group Requirement
In `Czarnak/tia-portal-mcp`, Siemens enforces a strict Windows OS security boundary. The native Openness DLLs check Windows Access Control Lists (ACLs) to verify that the calling process belongs to the local Windows group **`Siemens TIA Openness`**.
If the user is not in this group, any attempt to attach to TIA Portal throws `Siemens.Engineering.Security.AccessDeniedException`. `TiaMcpServer` validates this during startup via `WindowsIdentity` in its `OpennessGroupCheck` diagnostic test.

### 8.3 Empirical Benchmark Data: Latency, Bridge Overhead & Memory Footprint
Independent empirical stress-testing (Windows 11 AMD64, high-resolution `[System.Diagnostics.Stopwatch]` timers across 7–10 trials) validated the runtime characteristics of all four platforms:

#### 1. Measured Cold-Start & Module Import Latency
- **Native OS & PE Baseline**: `cmd.exe /c exit` (15.2 ms median), `whoami.exe` (16.1 ms median), `ping.exe` (14.0 ms median).
- **CPython Base Interpreter**: CPython 3.11 bare (36.2 ms median), CPython 3.13 bare (48.5 ms median), CPython 3.13 stdlib (73.5 ms median).
- **Python Framework Overhead**: CPython + `pydantic` (126.3 ms median), CPython + `pywinauto` (417.2 ms median).
- **FastMCP Protocol Import Dead Window**: Importing `mcp.server.fastmcp` incurs an unyielding **1,069.4 ms median (mean 1,079.4 ms)** startup delay before processing any JSON-RPC frames.
- **Server Cold vs. Warm Start**:
  - `gx_works2_mcp_server.py`: **1,399.8 ms median (mean 1,413.3 ms)**.
  - `simatic_s7_mcp_server.py` (Cold import + AST compile): **4,655.65 ms**.
  - `simatic_s7_mcp_server.py` (Warm `.pyc` startup): **1,492.3 ms median (mean 1,556.5 ms)**.
- **32-Bit SysWOW64 PowerShell COM Bridge**: Spawning the 32-bit PowerShell process takes **191.0 ms median**, while executing an end-to-end `_run_ps32_json` COM call averages **1,920.34 ms (range: 1,449.15 ms – 3,370.02 ms)**.

#### 2. Measured Memory Footprint (Working Set RSS via K32GetProcessMemoryInfo)
- **Native Win32 C Binaries**: `smss.exe` (**1.12 MB** RSS), `csrss.exe` (**5.49 MB** RSS), `cmd.exe` (**7.43 MB** RSS).
- **CPython Bare & Stdlib**: Bare CPython 3.11 (**9.77 MB** RSS), Bare CPython 3.13 (**10.81 MB** RSS), Stdlib (**12.80 MB** RSS).
- **FastMCP & Full Servers**: FastMCP SDK (**59.61 MB** RSS / 47.62 MB commit), `gx_works2_mcp_server` (**82.19 MB** RSS / 61.15 MB commit), `simatic_s7_mcp_server` (**83.06 MB** RSS / 61.96 MB commit).
- **Memory Multiplier**: Full Python MCP servers require **15x to 30x more RAM** than native Win32 executables, consuming ~250 MB across the three local daemons on resource-constrained IPCs.

### 8.4 Test Suite Coverage, Passing Status & Test Runner Nuances
Across the three local codebases, an automated test suite comprises **199 tests**:
- **`ispsoft mcp`**: **74 test cases** (73 in `test_ispsoft_mcp.py` + 1 in `test_stdio_integration.py`, 1,375 LOC). All 74 tests pass in 2.31s.
- **`simatic7 mcp`**: **98 test cases** (82 in `test_simatic_s7_mcp_server.py` [1,802 LOC] + 16 in `test_lad_converter_tool.py` [593 LOC], total 2,395 LOC). All 98 tests pass cleanly under Python 3.13.
- **`gx works mcp`**: **27 test cases** (440 LOC in `test_gx_works2_mcp_server.py`). All 27 tests pass in 28.84s (including 25.5s in dialog retry timeout testing).

#### Test Runner Nuances & CI Traps:
1. **Unittest vs. Pytest Discovery**: Executing via `python -m unittest discover` runs cleanly and passes 100% across all suites.
2. **GX Works2 Pytest Stdin Capture Crash**: Running `pytest` against `gx works mcp` without `-s` crashes immediately during test collection:
   ```
   AttributeError: 'DontReadFromInput' object has no attribute 'reconfigure'
   ```
   This occurs because `gx_works2_mcp_server.py:60` executes `sys.stdin.reconfigure(encoding="utf-8")`, but Pytest replaces `sys.stdin` with `DontReadFromInput`, which lacks `.reconfigure()`. Running with `-s` (`pytest -s`) bypasses input capture, allowing 100% of tests to pass.
3. **Simatic7 Pytest Edge Cases**: Running `pytest` without repository root on `sys.path` or with full block envelopes (`ORGANIZATION_BLOCK ... BEGIN ... END_ORGANIZATION_BLOCK`) causes 3 edge case failures in `test_simatic_s7_mcp_server.py` (79/82 pass under unconfigured pytest), whereas standard discovery under `unittest` passes 100%.

### 8.5 The PEP 701 Constraint at Line 7053
In `simatic_s7_mcp_server.py:7053`:
```python
export_path = new_s7_artifact_path(f"{re.sub(r'\\s+', '', block_name.lower())}-com-read")
```
The embedded regex raw string `r'\\s+'` contains backslashes inside an f-string expression block. Under Python 3.11, this triggers compile-time `SyntaxError: f-string expression part cannot include a backslash`. Under PEP 701 (Python 3.12+), backslashes are legalized. AST parsing confirms that line 7053 is the **single, exclusive syntactic blocker** preventing Python 3.11 execution across all 7,334 lines; extracting `re.sub` prior to string formatting restores full backwards compatibility.

---

## 9. Side-by-Side Architectural Comparison Matrix

| Dimension | `Czarnak/tia-portal-mcp` | `simatic7 mcp` | `gx works mcp` | `ispsoft mcp` |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Language** | C# (C# 12 / 13) | Pure Python | Pure Python | Pure Python |
| **Runtime Platforms** | .NET 10.0 + .NET Framework 4.8 | Python 3.12+ (PEP 701) | Python 3.10+ | Python 3.11+ |
| **Lines of Code (Core / Test)**| **54,612 / 87,363 (405 test files; 141,975 LOC total)** | **8,761 / 2,395 (98 test cases)** | **2,096 / 440 (27 test cases)** | **4,200 / 1,375 (74 test cases)** |
| **Total MCP Tools** | **8 read-only / 19 full mode** | **29 tools** | **9 tools** | **42 tools** |
| **Official MCP SDK Used** | **Yes** (`ModelContextProtocol` 2.3.0) | FastMCP (`mcp` Python SDK) | FastMCP (`mcp` Python SDK) | FastMCP (`mcp` Python SDK) |
| **PLC Vendor & Ecosystem** | Siemens S7-1200 / S7-1500 / WinCC | Siemens S7-300 / S7-400 / WinAC | Mitsubishi FX / Q / L Series | Delta DVP / AS Series |
| **Vendor Engineering Tool**| TIA Portal V21 (`Siemens.Automation.Portal`)| STEP 7 Classic v5.5–v5.7 (`s7tgtopx.exe`) | MELSOFT GX Works2 (`GXW2.exe`) | ISPSoft 3.24 / COMMGR 2.11 |
| **Hardware Driver Approach**| TIA Openness .NET API | RFC 1006 S7comm Socket (Port 102) | None (Offline CSV exchange only) | COMMGR XML + SetupAPI Serial |
| **Offline Code Representation**| SimaticML XML & Structured Text | AWL / STL Statement List Text | UTF-16LE Tab-Delimited CSV | Binary `.isp` DEFLATE & `.mpu` ZipCrypto |
| **Ladder Compilation / AST** | Native TIA Portal Openness Compiler | Pure-Python AST Contact Graph (`lad_converter`) | Linear-rung parsing only (No branches) | Delta IL parser & MPU node generator |
| **Concurrency Gate** | `SemaphoreSlim(1, 1)` IPC Gate | `threading.RLock()` (`@serialized_gui`) | `threading.RLock()` (`@serialized_gui`) | In-memory Lock + `Local\ISPSoftMCP.NativeCompile.v1` Mutex |
| **Session & Concurrency Guard**| `contentHash` (SHA-256) & Session ID | Timestamped backup files | Timestamped backup files | Atomic `.plc-` writes & backups |
| **Desktop Isolation** | Headless API (No UI needed) | Monkeypatched cursor / `WM_ACTIVATE` | Interactive UI only (Focus required) | Win32 Private Desktop (`CreateDesktopW`) |
| **Compilation Diagnostic Extraction**| Structured `CompileCheckReport` DTO | Headless COM `Compile()` + Watchdog | SysListView32 error scraper | Windows Runtime OCR (`Windows.Media.Ocr`) |
| **Air-Gapped IPC Distribution**| NuGet Global Tool (`tia-mcp`) | Python venv + wheels (`pywinauto`) | Python venv + wheels (`pywinauto`) | Python venv + standard library `ctypes` |

---

## 10. Conclusion

Each of the four MCP implementations represents an engineering triumph over vendor-specific constraints:
- `Czarnak/tia-portal-mcp` is the gold standard for **modern, typed API integration**, demonstrating how modern .NET 10 and legacy .NET 4.8 can be cleanly bridged out-of-process.
- `simatic7 mcp` is the gold standard for **multi-modal industrial flexibility**, pairing pure-Python socket-level S7comm telemetry with out-of-process 32-bit COM compilation and AST ladder generation.
- `gx works mcp` is the gold standard for **safe, non-invasive file bridging**, driving legacy MFC engineering suites without vendor SDKs.
- `ispsoft mcp` is the gold standard for **defensive offline container surgery and isolated OS automation**, combining reverse-engineered DEFLATE container codecs with private desktop OCR compilation.
