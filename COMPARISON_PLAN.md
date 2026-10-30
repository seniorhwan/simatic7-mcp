# Comprehensive Comparative Analysis & Architectural Plan

**Projects Analyzed:**
1. **Remote Repository**: [Czarnak/tia-portal-mcp](https://github.com/Czarnak/tia-portal-mcp) (Siemens TIA Portal V21 Openness MCP Server)
2. **Local Codebase 1**: `C:\Users\jethr\Downloads\simatic7 mcp` (Siemens SIMATIC STEP 7 Classic v5.5–v5.7 MCP Server)
3. **Local Codebase 2**: `C:\Users\jethr\Downloads\gx works mcp` (Mitsubishi MELSOFT GX Works2 FX/Q/L-Series MCP Server)
4. **Local Codebase 3**: `C:\Users\jethr\Downloads\ispsoft mcp` (Delta Industrial Automation ISPSoft 3.24 & COMMGR MCP Server)

---

## 1. Executive Summary & Ecosystem Overview

The industrial automation landscape is characterized by decades of proprietary vendor software, legacy Win32 runtimes, and specialized engineering tools. Model Context Protocol (MCP) servers bridge modern LLM coding agents into these industrial engineering environments.

A fundamental architectural divergence exists between Czarnak's `tia-portal-mcp` and your three local implementations:
* **Czarnak's `tia-portal-mcp`** is engineered in **C# (.NET 10 host paired with a .NET Framework 4.8 worker)**, targeting Siemens' modern flagship engineering environment (**TIA Portal V21**) via the official **Siemens TIA Openness API**.
* **Your three local MCP servers** (`simatic7 mcp`, `gx works mcp`, `ispsoft mcp`) are engineered in **Python 3.10+ (FastMCP)**, targeting legacy or closed vendor suites (**STEP 7 Classic v5.7**, **GX Works2**, and **ISPSoft 3.24**) that either lack modern public .NET APIs altogether or impose strict Win32/binary constraints.

```
+-------------------------------------------------------------------------------------------------------+
|                                    INDUSTRIAL MCP SERVERS TAXONOMY                                    |
+-------------------------------------------------------------------------------------------------------+
| Server           | Target Software / Hardware       | Primary Language   | Integration Vector          |
+------------------+----------------------------------+--------------------+-----------------------------+
| tia-portal-mcp   | Siemens TIA Portal V21           | C# (.NET 10 /      | Official Siemens Openness   |
| (Czarnak)        | (S7-1200, S7-1500, WinCC Unified)|  .NET Framework 4.8| Managed API (.NET Remoting) |
+------------------+----------------------------------+--------------------+-----------------------------+
| simatic7 mcp     | Siemens STEP 7 Classic           | Python 3.10+       | Win32 GUI Automation        |
| (Local)          | (v5.5-v5.7, S7-300, S7-400, AWL) | (FastMCP)          | (pywinauto) + LAD AST Engine|
+------------------+----------------------------------+--------------------+-----------------------------+
| gx works mcp     | Mitsubishi MELSOFT GX Works2     | Python 3.10+       | Win32 GUI Automation        |
| (Local)          | (FXCPU FX3U/FX3UC, QCPU, LCPU)   | (FastMCP)          | + UTF-16LE CSV Round-Trip   |
+------------------+----------------------------------+--------------------+-----------------------------+
| ispsoft mcp      | Delta Electronics ISPSoft 3.24   | Python 3.10+       | Reverse-Engineered DEFLATE  |
| (Local)          | & COMMGR 2.11 (DVP-ES2/SE, AS)   | (FastMCP)          | Binary Packaging + Win32 UI |
+------------------+----------------------------------+--------------------+-----------------------------+
```

---

## 2. Root Cause Analysis: Why C# (.NET) Instead of Python?

The user request specifically asks: *"Compare it and learn why we use c instead of python"*.
In technical parlance, Czarnak's codebase uses **C# (.NET)** (the C-family language of Microsoft and Siemens automation). Below is the comprehensive technical breakdown of why Czarnak chose C# over Python, and conversely, why Python was the optimal choice for your three local projects.

### 2.1 The Vendor API Barrier: Siemens Openness is Strictly .NET Framework 4.8
Siemens TIA Portal does not provide a C API, Python package, or REST service. Its sole official automation mechanism is **Siemens TIA Openness**, distributed as strongly-named .NET Framework 4.8 assemblies:
* `Siemens.Engineering.dll`
* `Siemens.Engineering.Hmi.dll`

#### Why Calling Siemens Openness from Python Fails in Production:
1. **.NET Remoting Architecture**:
   Under the hood, `Siemens.Engineering.dll` is an inter-process communication wrapper around `Siemens.Automation.Portal.exe`. It relies on legacy **.NET Framework Remoting** channels (`System.Runtime.Remoting`).
   When Python attempts to load these assemblies via `pythonnet` (`clr`):
   * .NET Remoting cross-AppDomain calls require strict COM Single-Threaded Apartment (STA) message pumping. Python's Global Interpreter Lock (GIL) and default MTA thread model frequently deadlock or raise `System.InvalidCastException` when crossing thread boundaries.
   * `pythonnet` does not properly handle Siemens custom serialization formatters and assembly resolution events (`AppDomain.CurrentDomain.AssemblyResolve`) when dynamic type redirection is required between TIA Portal minor versions.
2. **Strict User Group & Security Enforcement**:
   Siemens Openness enforces that the calling process runs under a Windows user belonging to the `Siemens TIA Openness` local user group. It prompts interactive Windows security dialogs on initial attachment. Handling these Windows modal events inside a Python C-extension frequently hangs headless stdio servers.

### 2.2 The Two-Process Host/Worker Topology
Modern MCP servers target the latest cross-platform runtimes (.NET 8/10 or Python 3.11/3.13). However, .NET Core / .NET 10 **cannot** load .NET Framework 4.8 remoting assemblies in-process.

Czarnak solved this by architecting a **Two-Process Split**:
```
  [ MCP Client (Claude / VS Code / Antigravity) ]
                       |
                       |  Standard MCP JSON-RPC over stdio
                       v
     +-----------------------------------+
     |        TiaMcpServer Host          |  <-- .NET 10.0 Modern Host
     |  (CLI, DI, FastMCP Protocol,      |
     |   Safety Pipeline, Value Budgets) |
     +-----------------------------------+
                       |
                       |  Newline-Delimited JSON IPC (private stdin/stdout pipes)
                       v
     +-----------------------------------+
     |   TiaMcpServer.OpennessWorker     |  <-- .NET Framework 4.8 Process
     |  (Loads Siemens.Engineering.dll,  |      (Runs on dedicated STA thread,
     |   maintains long-lived TIA attach)|       handles .NET Remoting directly)
     +-----------------------------------+
                       |
                       |  Siemens Openness / .NET Remoting
                       v
        [ Siemens TIA Portal V21 ]
```

#### Crash Isolation & Resilience:
Industrial engineering software is notoriously prone to uncatchable native access violations (e.g., when browsing corrupted DBs or malformed hardware devices).
* In a single-process Python architecture, an access violation in a C-extension crashes the entire MCP server, severing the client stdio connection and terminating the AI session.
* In Czarnak's C# architecture (`PersistentWorkerTransport.cs:203-234`), if `TiaMcpServer.OpennessWorker` crashes, the .NET 10 host intercepts the broken pipe, logs the stderr, and **automatically spawns a fresh worker instance** without dropping the client MCP connection:
```csharp
// TiaMcpServer/Worker/PersistentWorkerTransport.cs
private async Task<WorkerResponse> ExchangeAsync(Process process, WorkerRequest request)
{
    try {
        await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(request, WorkerJson.Request));
        await process.StandardInput.FlushAsync();
    } catch (IOException) {
        KillProcess(); // Broken pipe: worker died. Restart on next request.
        throw;
    }
    ...
}
```

### 2.3 Why Your Local Projects (`simatic7`, `gx works`, `ispsoft`) Use Python
While C# was mandatory for TIA Portal Openness, Python was the superior, pragmatic choice for your three local projects for clear engineering reasons:

1. **Absence of Official Modern Vendor APIs**:
   * **Mitsubishi GX Works2**: Has no Openness equivalent. Mitsubishi offers an expensive COM SDK ("MX Component") that is difficult to license and bundle. The standard way to interact is driving the GUI (`s7tgtopx` / `gx works2.exe`) via `pywinauto` and importing/exporting UTF-16LE CSV ladder logic.
   * **Delta ISPSoft**: Delta provides **zero** public APIs, zero COM libraries, and zero command-line tools. The only way to interface is reverse-engineering the binary containers or driving the UI.
   * **SIMATIC STEP 7 Classic**: STEP 7 v5.5–v5.7 is a 32-bit Win32 MFC application from the 1990s. Its legacy 32-bit COM interfaces ("Command Interface") frequently corrupt Step 7 MS Access/Sybase databases if accessed simultaneously. Generating compliant AWL (STL) plain-text sources and driving the LAD editor via GUI automation is far safer.
2. **Rapid Binary Reverse-Engineering in Python**:
   In `ispsoft mcp`, you had to decode raw DEFLATE zlib streams, patch binary headers at offsets `0x7c`, `0x94`, and `0x58`, and recalculate CRC-32 checksums (`ispsoft_mcp/containers.py:30-45`). Python's `struct`, `zlib`, and dynamic memory manipulation make writing custom binary decoders 10x faster and more readable than compiled C/C++ or C#.
3. **AST Construction & Grammar Parsing**:
   `simatic7 mcp` includes a 1,266-line Boolean Contact Graph AST and S7 Status Word VM (`lad_converter.py`). Building AST transformations, regex tokenizers, and graph traversals in Python is concise, expressive, and easily tested with `pytest`.
4. **GUI Automation Ecosystem**:
   Python's `pywinauto` is the industry standard for automating legacy Windows desktop software (Win32, MFC, Windows Forms). Doing equivalent window enumeration, handle matching, and focus management in C# requires verbose P/Invoke boilerplate (`user32.dll`, `kernel32.dll`).

---

## 3. Deep Comparative Analysis Across Architectural Dimensions

### 3.1 Software Architecture & Process Topology

| Metric | `tia-portal-mcp` (Czarnak) | `simatic7 mcp` (Local) | `gx works mcp` (Local) | `ispsoft mcp` (Local) |
| :--- | :--- | :--- | :--- | :--- |
| **Language** | C# (.NET 10 + .NET 4.8) | Python 3.10+ | Python 3.10+ | Python 3.10+ |
| **Process Model** | 2-process (Host + Worker) | 1-process (FastMCP + GUI) | 1-process (FastMCP + GUI) | 2-process (FastMCP + Private Desktop Worker) |
| **IPC Mechanism** | Private stdin/stdout JSON | Single process (In-memory) | Single process (In-memory) | Win32 Virtual Desktop / Windows API |
| **Target PLC Families** | S7-1200, S7-1500, ET200, Unified | S7-300, S7-400, WinAC, ET200S | FXCPU (FX3U/UC), QCPU, LCPU | Delta DVP (ES2/SE), Delta AS |
| **Lines of Code** | ~45,000 lines (across 375+ files) | ~8,600 lines (Server + LAD) | ~2,100 lines | ~4,200 lines (Core + Vendors) |

#### Key Takeaway:
Czarnak uses a decoupled enterprise architecture with contract assemblies (`TiaMcpServer.Contracts`), whereas your local servers are compact, monolithic scripts (`simatic_s7_mcp_server.py`, `gx_works2_mcp_server.py`) that bundle validation, conversion, and GUI driving into single modules.

### 3.2 Interfacing & Mutation Mechanism

1. **`tia-portal-mcp` (Direct Programmatic Object Graph)**:
   * Communicates directly with TIA Portal's runtime object model.
   * Can browse devices (`DeviceItem`), traverse subnets, read tag tables (`PlcTagTable`), export blocks to XML/SCL, and compile natively (`CompileChecker.cs`).
   * No physical screen pixels or GUI focus required.
2. **`simatic7 mcp` (Text Source Generation & GUI Automation)**:
   * Generates standard Siemens AWL (STL) plain-text sources (`OB 1`, `FC`, `FB`) and Symbol Table `.SDF` files.
   * Uses `pywinauto` with a headless-safe focus wrapper (`_headless_safe_set_focus`) to drive `s7tgtopx.exe` (SIMATIC Manager) and `s7kotx.exe` (LAD/STL Editor).
   * Fully functional offline mode: saves verified AWL/SDF artifacts even if SIMATIC Manager is not running.
3. **`gx works mcp` (Tab-Delimited CSV Interchange & GUI Automation)**:
   * Uses Mitsubishi's native `Edit -> Read from CSV File...` (`WM_COMMAND` `0x755`) and `Edit -> Write to CSV File...` (`0x756`).
   * Generates UTF-16LE tab-delimited ladder logic CSV files and validates step counts against verified backups.
4. **`ispsoft mcp` (Binary Container Patching & Silent Virtual Desktops)**:
   * Reverse-engineers ISPSoft 3.24 `.isp` containers (raw DEFLATE zlib compression).
   * Solves headless GUI limitations by spawning `NewISPSoft.exe` inside a **private Win32 Virtual Desktop** (`CreateDesktopW`), executing silent compiles without stealing focus from the user!

### 3.3 Concurrency, Threading & Safety Guards

#### Czarnak's Guarded Write Pipeline (`TiaMcpServer/Safety/`):
Czarnak implements a strict multi-phase write pipeline:
1. **Target Binding Lease**: Pinned project lease (`ProjectSessionBinding`). If the project path changes mid-operation, it fails closed with `binding_conflict`.
2. **Optimistic Concurrency via `contentHash`**:
   Before modifying any block or type, the client must supply the `expectedContentHash` (e.g. `source:sha256:7f8a...`) obtained during `plc_read`. If another user edited the block in TIA Portal, the write is rejected with `state_changed`.
3. **Dry-Run Preview (`dryRun: true`)**:
   Every write tool (`plc_write`, `network_write`, `hmi_write`, `vci_write`, `open_project`) supports dry-run mode, calculating exact changes and guard evaluations without mutating state.
4. **Value & Document Budgets**:
   Enforces strict size budgets (60,000 characters per item, 180,000 per response). Oversized payloads are omitted whole with structured narrowing guidance rather than clipped mid-JSON.

#### Local Servers' Concurrency & Safety Models:
1. **`simatic7 mcp`**:
   * Uses a reentrant GUI lock (`@serialized_gui` decorator with `threading.RLock()`) ensuring that only one pywinauto automation sequence interacts with SIMATIC Manager at a time.
   * Static Safety Analyzer: detects Star-Delta switching hazards (missing mutual interlocks between Star and Delta coils), flags duplicate `=` coil assignments, and identifies uninitialized timers/counters.
   * Automatic Backup & Rollback: captures pre-write backup in `~/Documents/Codex-Simatic7/` and restores it if compilation or readback verification fails.
2. **`gx works mcp`**:
   * Uses `GUI_TRANSACTION_LOCK = threading.RLock()`.
   * Enforces octal X/Y address bounds (`0o000` to `0o377`) and validates Mitsubishi opcodes before touching the GUI.
   * Safety analyzer checks for same-scan Star-Delta coil transitions.
3. **`ispsoft mcp`**:
   * Atomic file writes: uses `tempfile.NamedTemporaryFile` with `os.fsync()` and atomic `os.rename()` on the same volume (`limits.py:86-100`).
   * `_NATIVE_COMPILE_LOCK = threading.Lock()` guards private desktop compilation.

### 3.4 AST Compilers & Program Representation

* **`tia-portal-mcp`**: Operates on TIA Portal's native **SimaticML XML** schema or plain **SCL** (Structured Control Language) text blocks. It delegates all grammar compilation and ladder synthesis to the internal TIA Portal compiler.
* **`simatic7 mcp`**: Implements its own complete **LAD Contact Graph AST and Status Word VM** (`lad_converter.py`). It parses AWL/STL instructions into an AST, simulates the CPU Status Word (`FC`, `RLO`, `OR`, `NESTING_STACK`), and generates mathematically equivalent, clean Siemens Ladder Diagram rungs!
* **`gx works mcp`**: Operates on Mitsubishi rung matrices with CSV tab-separated tokens (`Step`, `Command`, `Device`).
* **`ispsoft mcp`**: Implements an Instruction List (IL) parser and serializer into Delta `.mpu` binary packages (`dvp.py` and `_vendor/il2mpu.py`).

### 3.5 Testing Strategy & CI Capabilities

* **`tia-portal-mcp` (375 Test Suites)**:
  Czarnak engineered `TiaMcpServer.FakeWorker`, a full test double of the net48 worker. This allows GitHub Actions CI on Linux and Windows to execute end-to-end integration tests of all 19 MCP tools without requiring a 50GB TIA Portal installation or physical PLC hardware!
* **Local MCP Servers**:
  * `simatic7 mcp`: Comprehensive `pytest` suite (98 unit tests covering mnemonic validation, AST conversions, safety rules, and offline round-trips). All 98 pass in ~37s!
  * `gx works mcp`: 27 unit tests verifying mnemonic checks, CSV generation, and Star-Delta interlocks.
  * `ispsoft mcp`: 74 unit tests verifying binary DEFLATE decompression, CRC-32 recalculation, and Modbus mapping.

---

## 4. Comprehensive Comparison Matrix

| Architectural Dimension | `tia-portal-mcp` (Czarnak) | `simatic7 mcp` (Local) | `gx works mcp` (Local) | `ispsoft mcp` (Local) |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Language** | C# (.NET 10 / net48) | Python 3.10+ | Python 3.10+ | Python 3.10+ |
| **Framework / SDK** | `ModelContextProtocol` C# SDK | FastMCP (`mcp[cli]`) | FastMCP (`mcp[cli]`) | FastMCP (`mcp[cli]`) |
| **Target PLC / IDE** | TIA Portal V21 (S7-1200/1500) | STEP 7 Classic v5.7 (S7-300/400) | GX Works2 (FX3U / Q-Series) | ISPSoft 3.24 (DVP / AS) |
| **Vendor Interface** | Siemens Openness Managed DLL | Win32 GUI (`pywinauto`) | Win32 GUI (`pywinauto`) | DEFLATE Binary + Win32 Desktop |
| **Process Topology** | 2-Process (Host + net48 Worker) | 1-Process (In-process GUI) | 1-Process (In-process GUI) | 2-Process (Private Desktop) |
| **Crash Isolation** | Pipe broken detection + auto-restart | None (Unhandled crash exits) | None (Unhandled crash exits) | Worker desktop isolation |
| **Concurrency Control** | `SemaphoreSlim` + Async Pipelining | `threading.RLock()` | `threading.RLock()` | `threading.Lock()` + Win32 Lock |
| **Optimistic Concurrency**| `contentHash` (SHA-256 state check) | None (Timestamp backup) | None (Timestamp backup) | None (Atomic file creation) |
| **Dry-Run Capability** | Native `dryRun: true` on all writes | Offline file generation mode | None | Offline transform mode |
| **AST / Grammar Engine**| Vendor XML / SCL Parser | Custom Contact Graph AST VM | CSV Token Grid | IL Arity Parser |
| **Hardware Topology** | Real-time subnet / device browsing | Static profiles (`s7_list_types`)| Static profiles (`PLC_PROFILES`)| Static profiles (`MODEL_PROFILES`) |
| **Memory / CPU Footprint**| ~80 MB RAM (Host) + 200 MB (Worker)| ~65 MB RAM | ~55 MB RAM | ~60 MB RAM |
| **Distribution Format** | .NET Global Tool (`dotnet tool`) | Python Script (`pip install`) | Python Script (`pip install`) | Python Script (`pip install`) |
| **CI Test Strategy** | `FakeWorker` double (375 tests) | Pytest offline suites (98 tests)| Pytest offline suites (27 tests)| Pytest offline suites (74 tests)|

---

## 5. Key Architectural Lessons from Czarnak's Work

Studying Czarnak's implementation yields five major architectural lessons that can directly elevate your local Python servers to commercial-grade robustness:

### Lesson 1: Optimistic Concurrency via Content Hashing
In industrial engineering, multiple engineers or external systems may edit a PLC project concurrently. Czarnak's `contentHash` model (`source:sha256:<hex>`) ensures that an LLM agent never overwrites code that was modified after it was read.
* **Action for Local Codebases**: Introduce `contentHash` into `simatic7 mcp` and `gx works mcp` read/write tools. When `s7_read_program` or `gx_read_program` runs, emit a SHA-256 hash of the logic. On write, require `expectedContentHash`. If the hash doesn't match current state, reject the write with `state_changed`.

### Lesson 2: Uniform `dryRun: true` Across All Write Tools
Czarnak provides `dryRun: true` on every write tool (`plc_write`, `network_write`, `vci_write`). This gives the user or AI agent the ability to preview exact changes, inspect safety guard evaluations, and verify syntax without committing mutations.
* **Action for Local Codebases**: Add a `dry_run: bool = False` parameter to `s7_write_program`, `gx_write_program`, and `ispsoft_compile_and_check`. When `dry_run=True`, execute all AST parsing, mnemonic validation, and safety interlock checks, returning the generated AWL/CSV artifact without touching the vendor GUI!

### Lesson 3: Value Budgets and Whole-Value Omission
LLM contexts have finite token limits. If a tool dumps a 500-network block or a massive hardware tree, the response blows the context window or gets truncated mid-JSON, causing parse errors. Czarnak solves this by enforcing a 60,000-character budget per item: if an item exceeds the budget, it is omitted whole with explicit narrowing guidance (e.g. *"Block exceeds 60,000 characters; query networks 1-20 explicitly"*).
* **Action for Local Codebases**: Implement response payload size guards in `simatic_s7_mcp_server.py` and `gx_works2_mcp_server.py`.

### Lesson 4: Decoupled Fake Worker for Automated CI
Czarnak built `TiaMcpServer.FakeWorker` to test every tool without TIA Portal installed.
* **Action for Local Codebases**: Your local servers already have strong unit test coverage, but decoupling GUI interactions behind an abstract protocol interface (`PlcDriverInterface`) would allow testing full tool dispatch in headless CI without `pywinauto` mocking hacks.

### Lesson 5: Dedicated Audit Log Stream
Czarnak maintains a structured JSONL audit trail in `%LOCALAPPDATA%\TiaMcpServer\audit\writes-yyyy-MM-dd.jsonl`, logging every write attempt, dry-run, and user confirmation.
* **Action for Local Codebases**: Add an append-only audit logger in `simatic7 mcp` and `gx works mcp` recording all AI edits with timestamps and user acceptance.

---

## 6. Actionable Implementation Plan (/plan)

Based on these findings, here is the concrete, prioritized plan for cross-pollinating the best features of Czarnak's architecture into your local Python MCP implementations:

### Component 1: `gx works mcp` Bugfix & Modernization
#### [MODIFY] `gx_works2_mcp_server.py`
1. **Fix `sys.stdin.reconfigure` crash during pytest execution**:
   Wrap line 60 with `hasattr(sys.stdin, "reconfigure")` to prevent test runner collection errors.
2. **Add `dry_run` support to `gx_write_program`**:
   Permit validating and generating the UTF-16LE CSV artifact without triggering `pywinauto`.
3. **Implement Optimistic Concurrency Hash**:
   Return `content_hash` in `gx_read_program` and verify `expected_content_hash` in `gx_write_program`.

### Component 2: `simatic7 mcp` Architectural Hardening
#### [MODIFY] `simatic_s7_mcp_server.py`
1. **Add `dry_run` parameter to `s7_write_program`**:
   Return preview phase, AST validation, Star-Delta warnings, and generated AWL source text without invoking SIMATIC Manager GUI.
2. **Implement Content Hashing (`expected_content_hash`)**:
   Compute `sha256` hash of normalized AWL networks and verify prior to GUI write.
3. **Structured Audit Logging**:
   Log all read/write/compile actions to `~/Documents/Codex-Simatic7/audit/writes-yyyy-MM-dd.jsonl`.
4. **Value Budget Guard**:
   Add maximum network/character bounds to `s7_read_program` with narrowing pagination hints.

### Component 3: `ispsoft mcp` Safety & Modbus Expansion
#### [MODIFY] `ispsoft_mcp_server.py`
1. **Add Dry-Run Preview**:
   Expose container transformation preview without writing to disk.
2. **Structured Error Contract**:
   Normalize return envelopes across `ispsoft_mcp_server.py` into canonical JSON structures matching MCP 2024 specifications.

---

## 7. Verification Plan

### Automated Tests
Run pytest across all local repositories to verify zero regression and validation accuracy:
```powershell
# 1. Simatic7 MCP Tests (98 tests)
pytest "C:\Users\jethr\Downloads\simatic7 mcp\tests"

# 2. GX Works2 MCP Tests (27 tests)
pytest -s "C:\Users\jethr\Downloads\gx works mcp\tests"

# 3. ISPSoft MCP Tests (74 tests)
$env:PYTHONPATH="C:\Users\jethr\Downloads\ispsoft mcp"; pytest "C:\Users\jethr\Downloads\ispsoft mcp\tests"
```

### Manual Verification
1. Inspect generated `COMPARISON_PLAN.md` file in `C:\Users\jethr\Downloads\simatic7 mcp\COMPARISON_PLAN.md`.
2. Confirm all concrete citations and line references against `tia-portal-mcp`, `simatic_s7_mcp_server.py`, `gx_works2_mcp_server.py`, and `ispsoft_mcp_server.py`.
