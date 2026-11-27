# Siemens SIMATIC STEP 7 Classic MCP Server (`v2.0`)

A production-grade **Model Context Protocol (MCP)** stdio server giving AI coding agents inside **VS Code, Google Antigravity, OpenAI Codex, and Claude Code** fast, safe, end-to-end engineering control over **Siemens SIMATIC Manager / STEP 7 Classic (V5.5 / V5.6 / V5.7)** for **S7-300, S7-400, ET200M, and WinAC** controllers.

> **Note:** Built specifically for **SIMATIC STEP 7 Classic** (not TIA Portal).

---

## Key Production Capabilities

- **Sub-15 ms Persistent 32-bit COM & PLCSIM Bridge (`simatic_s7_mcp/com_worker.py`)**
  - Maintains a single persistent 32-bit COM worker (optional native x86 C++ `native/s7_com_bridge.exe` or persistent SysWOW64 PowerShell `-File` daemon) holding live `Simatic.Simatic` (`UnattendedServerMode`) and `S7wspsmx.S7ProSim.1` COM objects — eliminating the ~1.9 s per-call startup cost and avoiding Windows 8 KB command-line truncation.
  - Non-modal `copy_block`, duplicate-project pre-checks (`already_exists`), `S7StationType` station creation (`30/31/32`), COM stdout suppression, and transactional backup/rollback recovery.
- **Full Hardware Configuration & DIN-Rail Assembly (`simatic_s7_mcp/hw_config.py`)**
  - Curated catalog of 40+ authentic Siemens MLFB modules (S7-300/400 rails, PS 307/407 power supplies, classic V1.x/V2.x and PN/DP CPUs including `6ES7 314-1AE04-0AB0`, IMs, DI/DO/AI/AO signal modules, CPs, FMs).
  - Enforces S7-300/400 DIN-rail slot rules, multi-slot width collisions, 5 V backplane mA power budgets, automatic non-overlapping I/O byte addressing, IPv4/MPI/PROFIBUS validation, and STEP 7 V5.x flat-grammar `.cfg` export/import.
- **Complete IEC 61131-3 Programming Suite (`simatic_s7_mcp/iec_languages.py` + `lad_converter.py`)**
  - **STL / AWL** (`BLKLANG=00001`): International & German mnemonic validation, single-block and cross-block multi-block safety analysis (`s7_analyze_program`).
  - **LAD / KOP** (`BLKLANG=00002`): AST Contact Graph compiler with truth-table equivalence verification and strict `BLD 100/101/102/103` + `NOP 0` compliance.
  - **FBD / FUP** (`BLKLANG=00003`): Pure FBD converter & compliance validator.
  - **SCL / ST** (`BLKLANG=00004`): Syntax/static validator + `S7SC.exe` / opt-in COM compilation (`compile_via_com`).
  - **S7-GRAPH / SFC** (`BLKLANG=00005`): BFS reachability & dead-end sink analyzer + `.gr7` source emission with opt-in COM compilation (`compile_via_com`).
  - **Global/Instance DB & UDT** (`BLKLANG=00006`): Exact S7 bit-packing and even-byte word alignment layout generator.
- **Full Simulation (Live S7-PLCSIM + Virtual S7-300 PLC + Siemens Standard Library)**
  - S7-PLCSIM 32-bit COM automation (`s7_plcsim_start_stop` for verified `RUN_P`/`RUN`/`STOP` simulation start/stop with optional scan-mode selection, auto-launch & disconnect, plus `connect`, `disconnect`, `set_state`, `set_scan_mode`, `step_scan`, `save_plc`, `load_plc`), exact IEEE-754 `REAL` float preservation, and `I`-area reads.
  - Deterministic **Virtual S7-300/400 PLC** (`VirtualS7PLC`) executing STL/LAD, SCL, S7-GRAPH, DBs, periodic `OB35`, and authentic Siemens S7-300 standard/system library blocks (`SFB0` CTU, `SFB1` CTD, `SFB2` CTUD, `SFB3` TP, `SFB4` TON, `SFB5` TOF, `FC105` SCALE, `FC106` UNSCALE, `FB41` CONT_C PID, `SFC20` BLKMOV, `SFC21` FILL, `SFC64` TIME_TCK).
  - Deterministic closed-loop physical plant simulator (`s7_run_closed_loop_sim`) for verifying PID tuning, tank levels, thermal systems, and disturbance rejection with quantitative control assertions.
- **High-Speed Physical PLC Monitoring, Direct Program Deployment & AI Data Historian (`simatic_s7_mcp/runtime_engine.py` + `simatic_s7_mcp/s7_mc7.py`)**
  - **Direct S7comm ISO-on-TCP Program Transfer (port 102, cross-platform)**: Deploy compiled MC7 blocks (`OB`/`FC`/`FB`/`DB`/`SDB`) directly to physical S7-300/S7-400 PLCs (`0x1A` → `0x1B` → `0x1C` → `0x28 _INSE`), upload & back up blocks from physical PLCs (`0x1D` → `0x1E` → `0x1F`), delete blocks (`0x28 _DELE`), and persist RAM to MMC/EPROM (`0x28 _MODU`). Extracts authentic Siemens-compiled MC7 blocks directly from STEP 7 Classic `.s7p` projects (`ombstx/offline/*/SUBBLK.DBF` + `SUBBLK.DBT`) and compiles Global Data Blocks (DBs) directly in pure Python.
  - Persistent RFC1006 / ISO-on-TCP `S7ConnectionPool` with automatic **PG -> OP -> S7_BASIC TSAP fallback** (critical when SIMATIC Manager already occupies the single PG resource on an S7-300), **dynamic negotiated-PDU chunking** (respects classic S7-300 240-byte PDUs), Timer/Counter area access, online block inventory (`s7_runtime_list_online_blocks`), 7-stage pre-upload network & S7comm connectivity diagnostics with automatic rack/slot discovery (`s7_plc_diagnose_connectivity` + built-in `preflight_check` in `s7_plc_deploy_program`), quick pre-flight check (`s7_plc_connection_check`), Watch Table (VAT) snapshots, CPU SZL diagnostics, and a dedicated **AI time-series historian** (`s7_collect_plc_data`) with per-tag analytics, threshold alerting, and CSV/JSONL export.
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

## Complete MCP Tool Catalog (78 Tools)

### Environment, Diagnostics & Project Lifecycle (12 Tools)
- `s7_get_status` — Inspect controller connection, workspace, and active project state.
- `s7_doctor` — Run comprehensive environment diagnostics (STEP 7, COM, S7ProSim, Python, ports).
- `s7_auto_detect_controllers` — Detect installed STEP 7 versions, binaries, and running Siemens processes.
- `s7_list_supported_plc_types` — List supported S7-300, S7-400, and WinAC profiles and limits.
- `s7_generate_client_config` — Generate MCP config snippets for VS Code, Antigravity, Codex, and Claude Code.
- `s7_create_project` — Create a new STEP 7 Classic project workspace (offline or GUI).
- `s7_open_project` — Open an existing `.s7p` file or project workspace directory.
- `s7_open_block` — Open/focus a program block (`OB1`, `FC10`, `FB1`, `DB1`) in offline workspace or LAD/STL editor.
- `s7_get_block_overview` — Scan project blocks, estimated MC7 sizes, and call hierarchy.
- `s7_com_list_projects` — Enumerate registered STEP 7 projects via native 32-bit COM.
- `s7_com_create_project` — Create a STEP 7 project (and optional station) directly via native 32-bit COM.
- `s7_com_browse_project_tree` — Browse stations, programs, blocks, sources, and symbol tables via COM.

### Native COM Block, Symbol & Station Operations (9 Tools)
- `s7_com_list_blocks` — List compiled blocks in a STEP 7 project with accurate STL/LAD/FBD/SCL/GRAPH/DB/SDB/UDT language metadata.
- `s7_com_read_block` — Export and parse a block via `IS7Block.GenerateSource` (`awl_source` & `raw_awl`).
- `s7_com_write_block` — Compile and write a block (`OB`/`FC`/`FB`/`DB`/`UDT`) via COM with backup, rollback, and `BLKLANG` tagging.
- `s7_com_copy_block` — Non-modal copy of a block within or across STEP 7 projects via COM.
- `s7_com_delete_block` — Delete a compiled block via COM with automatic backup.
- `s7_com_export_symbols` — Export a project's live Symbol Table via COM (`.sdf` / `.asc`).
- `s7_com_import_symbols` — Import a Symbol Table into a STEP 7 project via COM.
- `s7_com_download_blocks` — Download blocks to a connected PLC or S7-PLCSIM via COM (dual-guarded).
- `s7_com_upload_station` — Upload station blocks from a connected PLC via COM.

### Hardware Configuration & DIN-Rail Assembly (8 Tools)
- `s7_hw_catalog_search` — Search 40+ Siemens MLFB modules (PS, CPU, IM, DI, DO, AI, AO, CP, FM).
- `s7_hw_configure_station` — Create/configure an S7-300/400 station with DIN-rail rack & slots.
- `s7_hw_add_module` — Insert a Siemens MLFB module with automatic slot, width, power-budget, and I/O address checks.
- `s7_hw_remove_module` — Remove a module from a configured rack slot.
- `s7_hw_validate_station` — Validate backplane 5 V mA power budget, slot rules, addressing, and subnets.
- `s7_hw_export_cfg` — Export STEP 7 V5.x flat-grammar ASCII `.cfg` station configuration.
- `s7_hw_import_cfg` — Parse and import an existing STEP 7 `.cfg` file.
- `s7_hw_compile_station` — Compile/import a hardware `.cfg` into a STEP 7 project via COM.

### IEC 61131-3 Programming, Conversion & Validation (15 Tools)
- `s7_read_program` — Read and parse an AWL/STL/LAD program from active editor, COM, or offline project.
- `s7_write_program` — Validate and write AWL/STL/LAD networks with automatic LAD normalization.
- `s7_validate_program` — Validate S7 AWL/STL instructions, operands, addresses, and CPU limits.
- `s7_compile_and_check` — Compile/verify an AWL source artifact and report diagnostics.
- `s7_convert_to_ladder` — Convert STL/AWL into strict Siemens LAD (`BLD 100/101/102/103` + `NOP 0`) with truth-table equivalence check.
- `s7_validate_lad_compliance` — Audit AWL source for strict SIMATIC LAD display compliance.
- `s7_convert_to_fbd` — Convert STL/AWL into pure Siemens FBD/FUP compliant source.
- `s7_validate_fbd_compliance` — Audit AWL source for strict SIMATIC FBD/FUP compliance.
- `s7_validate_scl` — Validate Siemens SCL (Structured Text) syntax and static rules.
- `s7_write_scl_block` — Validate and write `.scl` artifact (with opt-in `compile_via_com` or `S7SC.exe` CLI compilation).
- `s7_validate_graph_sfc` — Validate S7-GRAPH SFC reachability, initial steps, dead-ends, and IEC qualifiers.
- `s7_write_graph_block` — Validate and emit `.gr7` source (with opt-in `compile_via_com` compilation).
- `s7_create_data_block` — Generate Global or Instance DB AWL source with exact S7 byte/bit layout.
- `s7_create_udt` — Generate User-Defined Type (`TYPE UDT`) AWL source with exact S7 layout.
- `s7_annotate_awl_code` — Annotate AWL instructions with symbol names and comments.

### Static Safety Analysis, Diffing & Symbols (9 Tools)
- `s7_analyze_program` — Deep static safety analysis across single or multiple blocks (`networks`, `blocks`, `awl_source_path`, `awl_source_paths`).
- `s7_get_cross_references` — Generate address/symbol read/write cross-reference tables.
- `s7_get_memory_usage_map` — Map I/Q/M/T/C/DB memory usage against PLC family limits.
- `s7_compare_blocks` — Unified diff and structural comparison between two blocks/sources.
- `s7_write_symbol_table` — Validate and write `.asc`, `.sdf`, and `.csv` symbol table artifacts.
- `s7_read_symbol_table` — Parse and validate `.sdf`, `.seq`, `.asc`, or `.csv` symbol tables.
- `s7_read_device_comments` — Read address-to-comment mappings from symbol tables.
- `s7_write_device_comments` — Update/write device comments into symbol table artifacts.
- `s7_search_symbols_and_comments` — Search symbols, addresses, and comments by pattern.

### Simulation — Live S7-PLCSIM & Virtual S7-300 PLC (9 Tools)
- `s7_virtual_plc_load` — Load STL/LAD, SCL, S7-GRAPH, or Data Blocks into the in-memory `VirtualS7PLC`.
- `s7_virtual_plc_step` — Execute deterministic scan cycles (advancing S7 timers, IEC timers/counters, and periodic `OB35`).
- `s7_virtual_plc_inspect` — Inspect Virtual PLC I/Q/M/PI/PQ/DB memory, timers, counters, and active SFC steps.
- `s7_plcsim_start_stop` — Trigger S7-PLCSIM simulation `start` (`RUN_P`/`RUN`, optional `CONTINUOUS`/`SINGLE` scan mode & auto-launch) and `stop` (`STOP`, optional COM disconnect) commands via the 32-bit `S7wspsmx.S7ProSim.1` COM interface with post-command state verification and audit logging.
- `s7_plcsim_control` — Control S7-PLCSIM via S7ProSim (`start`, `stop`, `connect`, `disconnect`, `set_state`, `set_scan_mode`, `save_plc`, `load_plc`).
- `s7_plcsim_step_scan` — Execute deterministic single/multi-cycle scans in live S7-PLCSIM.
- `s7_plcsim_snapshot` — Capture a multi-address state snapshot from S7-PLCSIM or Virtual PLC.
- `s7_plcsim_run_scenario` — Run multi-step input/expectation test scenarios on Virtual PLC or S7-PLCSIM.
- `s7_run_closed_loop_sim` — Run closed-loop PLC + physical plant simulation with `FB41` PID, `FC105`/`FC106`, `SFB0`-`SFB5`, `SFC20`/`SFC21`/`SFC64`, disturbances, and control assertions.

### Live S7comm Runtime, Physical PLC Deployment & AI Data Historian (16 Tools)
- `s7_plc_diagnose_connectivity` — Comprehensive 7-stage pre-upload diagnostic verifying DNS, multi-probe TCP RTT/jitter/packet loss, COTP PG/OP/S7_BASIC TSAP availability + automatic rack/slot discovery, negotiated PDU chunk budget, CPU SZL identity/mode/diagnostic buffer, block management & target overwrite impact + read-only `0x1D..0x1F` upload channel probe, and write guardrails (`READY` / `DEGRADED` / `BLOCKED` verdict).
- `s7_plc_connection_check` — Quick non-destructive pre-flight check for physical PLCs (TCP port 102, PG/OP/S7-Basic TSAP, negotiated PDU, CPU MLFB/mode, online blocks, Windows `S7ONLINE` PG/PC interface, and write/download guardrails).
- `s7_plc_deploy_program` — Directly deploy/download compiled MC7 blocks (`OB`/`FC`/`FB`/`DB`/`SDB`) to a physical S7-300/S7-400 PLC over S7comm ISO-on-TCP port 102 (`0x1A`→`0x1B`→`0x1C`→`_INSE`→optional `_MODU`) with built-in pre-upload connectivity diagnostics (`preflight_check=True`), automatic pre-overwrite online backup & online verification (dual-guarded).
- `s7_plc_upload_block` — Upload (retrieve & back up) any block (`OB1`, `FC10`, `FB5`, `DB1`, `SDB0`) directly from a physical PLC over S7comm ISO-on-TCP (`0x1D`→`0x1E`→`0x1F`) into `~/Documents/Codex-Simatic7/Backups/*.mc7` (read-only safe).
- `s7_plc_delete_block` — Safely back up and delete a block on a physical PLC via S7comm PI-Service `_DELE` (`0x28`, dual-guarded).
- `s7_plc_copy_ram_to_rom` — Persist active RAM blocks to MMC / Flash EPROM via S7comm PI-Service `_MODU` (`0x28`, dual-guarded).
- `s7_runtime_ping` — Test TCP/RFC1006 reachability to a PLC or loopback server.
- `s7_runtime_read` — Read an S7 address/symbol via RFC1006/S7comm or S7ProSim.
- `s7_runtime_write` — Dual-guarded write to an S7 address/symbol.
- `s7_runtime_batch_read` — High-speed pooled multi-item Read Var PDU snapshot (auto-chunked to the CPU's negotiated PDU).
- `s7_runtime_batch_write` — Dual-guarded pooled multi-item Write Var PDU (auto-chunked to the CPU's negotiated PDU).
- `s7_runtime_cpu_diagnostics` — Read CPU SZL identity (`0x0011`), operating mode (`0x0424`), and diagnostic buffer (`0x00A0`).
- `s7_runtime_list_online_blocks` — Query live block counts and block numbers (`OB`/`FB`/`FC`/`DB`/`SDB`/`SFB`/`SFC`) loaded on a physical PLC over S7comm TCP.
- `s7_runtime_cpu_control` — Dual-guarded CPU RUN / STOP control.
- `s7_monitor_watch_table` — Multi-sample symbolic Watch Table (VAT) polling with delta detection (up to 100 samples, auto-resolves project symbol table).
- `s7_collect_plc_data` — AI-agent time-series historian (up to 500 samples / 60 s) with per-tag statistics (`min`/`max`/`mean`/`stddev`/`delta`, BOOL duty cycle & edge counts), threshold alarms, and optional CSV/JSONL dataset export.

### MCP Resources & Prompts
- **Resources (3)**: `s7://catalog/modules`, `s7://reference/iec-61131-3`, `s7://workspace/status`
- **Prompts (4)**: `s7_new_project_wizard`, `s7_safe_commissioning_checklist`, `s7_convert_and_verify_lad`, `s7_plcsim_automated_test`

---

## Connecting to a Physical S7-300 / S7-400 PLC

### 1. Live Monitoring, Block Upload & AI Data Gathering (Windows, Linux, or macOS — No STEP 7 GUI Required)
Any physical S7-300/400 PLC with onboard PROFINET (`CPU 315-2 PN/DP`, `CPU 317-2 PN/DP`, etc.) or Ethernet CP (`CP 343-1`, `CP 443-1`) listens on **ISO-on-TCP port 102**:
1. Run `s7_plc_diagnose_connectivity(host="192.168.0.10", rack=0, slot=2)` (or `s7_plc_connection_check`) to verify TCP latency, packet loss, COTP TSAP availability, CPU rack/slot, PDU chunk size, CPU mode, and deployment guardrails.
2. Read tags, back up live blocks, or gather datasets with `s7_runtime_batch_read`, `s7_monitor_watch_table`, `s7_runtime_list_online_blocks`, `s7_plc_upload_block`, `s7_runtime_cpu_diagnostics`, and `s7_collect_plc_data(..., export_format="both")`.
3. Monitoring, connectivity diagnostics, and block upload/backup are **read-only** and work in every access mode (`read-only`, `read-write`, `full`).

### 2. Deploying / Downloading Programs Directly to a Physical PLC (`s7_plc_deploy_program` & `s7_com_download_blocks`)
- **Direct S7comm ISO-on-TCP Deployment (`s7_plc_deploy_program` — Works on Windows, Linux, or macOS, NO `S7ONLINE` Required!)**:
  1. Verify connectivity first with `s7_plc_diagnose_connectivity(host="192.168.0.10", rack=0, slot=2, target_blocks=["DB1", "FC10", "OB1"])` (and keep `preflight_check=True` enabled on `s7_plc_deploy_program`).
  2. Start the server with `--mode full` AND `S7_WRITES_ENABLED=true`, pass `confirm_live_write=True`, and ensure the CPU keyswitch is in `RUN-P` or `STOP`.
  3. Supply a compiled STEP 7 `.s7p` project (`project_path="C:/Projects/MyPlant"`), a `.mc7` binary file (`mc7_file_path`), a Global Data Block (`db_number` + `db_fields`), or raw AWL/LAD/SCL source on Windows (`compile_via_step7=True`).
  4. The tool verifies PLC communication via preflight diagnostics, automatically backs up existing online blocks via `0x1D..0x1F`, transfers blocks in exact dependency order (`SDB`→`DB`→`FC`→`FB`→`OB`) over TCP port 102 (`0x1A`→`0x1B`→`0x1C`→`_INSE`), optionally persists to MMC/EPROM (`copy_ram_to_rom=True`), and verifies online via `list_online_blocks` + SZL diagnostics.
- **STEP 7 COM Download (`s7_com_download_blocks` — Windows with STEP 7 Classic)**:
  - Uses `Simatic.Simatic` COM over `S7ONLINE` (MPI/PROFIBUS/TCP) and can automatically fall back to direct S7comm TCP (`fallback_to_tcp=True`) when `host` + compiled project are supplied.

---

## Optional <5 ms Native 32-bit C++ COM Bridge (Windows)

On Windows with MSVC Build Tools installed, build the zero-dependency 32-bit x86 COM bridge (`ole32.lib` + `oleaut32.lib`):

```bat
native\build_bridge.bat
```

When `native/s7_com_bridge.exe` is present, `PersistentComWorker` uses it automatically for ultra-low-latency S7ProSim calls and delegates extended SIMATIC Manager commands to the persistent 32-bit PowerShell COM daemon.

---

## Running Tests & Linting

```bash
# Unit test suite (unittest or pytest)
python3 -m unittest discover -s tests -v
pytest -v

# Static lint check
flake8 simatic_s7_mcp_server.py lad_converter.py simatic_s7_mcp tests --select=E9,F401,F63,F7,F82
```

## License

MIT — see [LICENSE](LICENSE).
