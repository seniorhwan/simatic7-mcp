# Siemens SIMATIC STEP 7 Classic MCP Server (`v2.0`)

A production-grade **Model Context Protocol (MCP)** stdio server giving AI coding agents inside **VS Code, Google Antigravity, OpenAI Codex, and Claude Code** fast, safe, end-to-end engineering control over **Siemens SIMATIC Manager / STEP 7 Classic (V5.5 / V5.6 / V5.7)** for **S7-300, S7-400, ET200M, and WinAC** controllers.

> **Note:** Built specifically for **SIMATIC STEP 7 Classic** (not TIA Portal).

---

## Key Production Capabilities

- **Sub-15 ms Persistent 32-bit COM & PLCSIM Bridge (`simatic_s7_mcp/com_worker.py`)**
  - Maintains a single persistent 32-bit COM worker (optional native x86 C++ `native/s7_com_bridge.exe` or persistent SysWOW64 PowerShell daemon) holding live `Simatic.Simatic` (`UnattendedServerMode`) and `S7wspsmx.S7ProSim.1` COM objects — eliminating the ~1.9 s per-call startup cost.
  - Zero hardcoded project name biases; non-destructive transactional staging with automatic backup, rollback, and crash/timeout recovery.
- **Full Hardware Configuration & DIN-Rail Assembly (`simatic_s7_mcp/hw_config.py`)**
  - Curated catalog of 35+ authentic Siemens MLFB modules (S7-300/400 rails, PS 307/407 power supplies, CPUs, IMs, DI/DO/AI/AO signal modules, CPs, FMs).
  - Enforces S7-300/400 DIN-rail slot rules, multi-slot width collisions, 5 V backplane mA power budgets, automatic non-overlapping I/O byte addressing, IPv4/MPI/PROFIBUS validation, and round-trip STEP 7 ASCII `.cfg` export/import.
- **Complete IEC 61131-3 Programming Suite (`simatic_s7_mcp/iec_languages.py` + `lad_converter.py`)**
  - **STL / AWL** (`BLKLANG=00001`): Full International & German mnemonic validation, Star-Delta interlock & duplicate-coil safety analysis.
  - **LAD / KOP** (`BLKLANG=00002`): AST Contact Graph compiler with truth-table equivalence verification and strict `BLD 100/101/102/103` + `NOP 0` compliance.
  - **FBD / FUP** (`BLKLANG=00003`): Pure FBD converter & compliance validator.
  - **SCL / ST** (`BLKLANG=00004`): Syntax/static validator (catches C-style `==`/`!=`/`&&`, unbalanced blocks, infinite loops) + `S7SC.exe` / COM compilation.
  - **S7-GRAPH / SFC** (`BLKLANG=00005`): BFS reachability & dead-end sink analyzer + `.gr7` source emission.
  - **Global/Instance DB & UDT** (`BLKLANG=00006`): Exact S7 bit-packing and even-byte word alignment layout generator.
- **Full Simulation (Live S7-PLCSIM + Cross-Platform Virtual S7 PLC)**
  - S7-PLCSIM lifecycle (`connect`, `disconnect`, `set_state`, `set_scan_mode`, `step_scan`, `save_plc`, `load_plc`), exact IEEE-754 `REAL` float preservation, and `I`-area reads.
  - Built-in deterministic **Virtual S7-300/400 PLC** (`VirtualS7PLC`) executing STL/LAD logic, latches, edges, math, comparators, S7 timers (`SD`/`SS`/`SP`/`SE`/`SF`), and S7 counters (`CU`/`CD`) offline and in CI.
- **High-Speed Pooled Monitoring & Diagnostics (`simatic_s7_mcp/runtime_engine.py`)**
  - Persistent RFC1006 / ISO-on-TCP `S7ConnectionPool`, multi-item batch Read/Write Var PDUs, Timer/Counter area access, Watch Table (VAT) snapshots with symbolic resolution & delta tracking, and live CPU SZL diagnostics (`0x0011` MLFB, `0x0424` mode, `0x00A0` diagnostics buffer).
- **Industrial Safety & AI-Agent Ergonomics (`simatic_s7_mcp/contracts.py`)**
  - Strict dual-guard (`confirm_live_write=True` **AND** `S7_WRITES_ENABLED=true`) for all physical/remote PLC writes and CPU RUN/STOP control.
  - Access modes (`--mode read-only | read-write | full`), SHA-256 `content_hash` optimistic concurrency (`failure_category="state_changed"`), closed machine-readable failure taxonomy, MCP `ToolAnnotations`, MCP Resources & Prompts, and append-only JSONL audit logging (`~/Documents/Codex-Simatic7/audit.jsonl`).

---

## Quick Start & AI Client Setup

### 1. Install Dependencies

```bash
pip install -e .
# On Windows with STEP 7 Classic installed (optional GUI fallback):
pip install -e ".[windows]"
```

### 2. Run Environment Diagnostics (`doctor`)

```bash
python simatic_s7_mcp_server.py doctor
```

### 3. Generate Drop-In Configs for Your AI Coding Agent

```bash
python simatic_s7_mcp_server.py init-client --client all
# Or target a single client: vscode | antigravity | codex | claude
python simatic_s7_mcp_server.py --mode read-only init-client --client vscode
```

#### VS Code (`.vscode/mcp.json`)
```json
{
  "servers": {
    "simatic-s7-classic": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/simatic_s7_mcp_server.py", "--mode", "read-write"],
      "env": {
        "S7_ACCESS_MODE": "read-write",
        "S7_WRITES_ENABLED": "false"
      }
    }
  }
}
```

#### Google Antigravity (`~/.gemini/antigravity/mcp_config.json`) & Claude Code (`.mcp.json`)
```json
{
  "mcpServers": {
    "simatic-s7-classic": {
      "command": "python",
      "args": ["/path/to/simatic_s7_mcp_server.py", "--mode", "read-write"],
      "env": {
        "S7_ACCESS_MODE": "read-write",
        "S7_WRITES_ENABLED": "false"
      }
    }
  }
}
```

#### OpenAI Codex (`~/.codex/config.toml`)
```toml
[mcp_servers.simatic-s7-classic]
command = "python"
args = ["/path/to/simatic_s7_mcp_server.py", "--mode", "read-write"]

[mcp_servers.simatic-s7-classic.env]
S7_ACCESS_MODE = "read-write"
S7_WRITES_ENABLED = "false"
```

---

## Optional <5 ms Native 32-bit C++ COM Bridge (Windows)

On Windows with MSVC Build Tools installed, build the zero-dependency 32-bit x86 COM bridge (`ole32.lib` + `oleaut32.lib`):

```bat
native\build_bridge.bat
```

When `native/s7_com_bridge.exe` is present, `PersistentComWorker` uses it automatically.

---

## Running the Test Suite

```bash
python3 -m unittest discover -s tests -v
```
