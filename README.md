# SIMATIC STEP 7 MCP: Instructions for AI Agents & Engineers

A Model Context Protocol (MCP) server for **Siemens SIMATIC STEP 7 Classic** (v5.5 / v5.6 / v5.7 / SIMATIC Manager) supporting **S7-300**, **S7-400**, and **WinAC** PLCs.

> **Target Environment**: This server automates and edits offline/online blocks in **SIMATIC Manager** (`s7tgtopx.exe`) and the **LAD/FBD/STL Editor** (`s7kotx.exe`). It is designed specifically for **STEP 7 Classic (not TIA Portal)**.

---

## Safe Call Sequence

1. **Check Status**: Call `s7_get_status`. Proceed only when SIMATIC Manager is connected and the block editor is active (or when offline AWL artifact generation is desired). Confirm that the active project and block match the intended target.
2. **List Target Families**: Call `s7_list_supported_plc_types` to inspect memory boundaries, data types, and supported CPU model aliases.
3. **Pre-Validate Candidate**: Call `s7_validate_program` with the complete candidate networks. Resolve every error and inspect all safety warnings (such as Star-Delta interlocks or duplicate coils).
4. **Execute Verified Write**: Call `s7_write_program` with the validated candidate. The server creates an atomic backup before writing, imports the AWL source, triggers compilation/block check, and reads back the block.
5. **Verify Success**: Report success only when the returned `status` is exactly `SUCCESS`. That status strictly requires:
   - Verified pre-modification block backup.
   - Successful AWL source import.
   - Successful compilation with zero errors.
   - Readback signature equality between candidate and compiled block.
   Any other status indicates failure or uncertainty; inspect the returned `rollback` and `backup_awl` fields. Never retry a write automatically after an error without checking the rollback state.

---

## Network (Ladder / STL) Input Format

Pass `networks` as an ordered array of network objects. Each network corresponds to an S7 network / ladder rung and contains an optional `title`, optional `comment`, and an ordered list of `instructions`.

```json
{
  "networks": [
    {
      "title": "Motor start/stop seal-in",
      "comment": "Main control rung for hydraulic pump motor",
      "instructions": [
        "A I 0.0",
        "O M 0.0",
        "AN I 0.1",
        "= M 0.0"
      ]
    },
    {
      "title": "Drive main contactor",
      "instructions": [
        "A M 0.0",
        "= Q 0.0"
      ]
    }
  ]
}
```

Branching jumps with labels are fully supported:
```json
{
  "title": "Conditional Jump",
  "instructions": [
    "A I 0.0",
    "JC M001",
    "L 0",
    "T MW 10",
    "JU M002",
    "M001: L 100",
    "T MW 10",
    "M002: NOP 0"
  ]
}
```

---

## Siemens S7 Mnemonics & Syntax Reference

### 1. Bit Logic & Addressing

| Operation | International (English) | German (SIMATIC) | Description |
| :--- | :--- | :--- | :--- |
| **Inputs** | `I <byte>.<bit>` (e.g. `I 0.0` - `I 0.7`) | `E <byte>.<bit>` (e.g. `E 0.0` - `E 0.7`) | Process image input bit |
| **Outputs** | `Q <byte>.<bit>` (e.g. `Q 0.0` - `Q 0.7`) | `A <byte>.<bit>` (e.g. `A 0.0` - `A 0.7`) | Process image output bit |
| **Bit Memory** | `M <byte>.<bit>` (e.g. `M 0.0` - `M 0.7`) | `M <byte>.<bit>` | Internal marker bit |
| **Local Temporary** | `L <byte>.<bit>`, `LB`, `LW`, `LD` | `L <byte>.<bit>`, `LB`, `LW`, `LD` | Local stack temp data |
| **AND / AND NOT** | `A <bit>` / `AN <bit>` | `U <bit>` / `UN <bit>` | Contact check NO / NC |
| **OR / OR NOT** | `O <bit>` / `ON <bit>` | `O <bit>` / `ON <bit>` | Parallel contact NO / NC |
| **XOR / XOR NOT** | `X <bit>` / `XN <bit>` | `X <bit>` / `XN <bit>` | Exclusive OR |
| **Nesting** | `A(`, `AN(`, `O(`, `ON(`, `)` | `U(`, `UN(`, `O(`, `ON(`, `)` | Parentheses evaluation |
| **Assign** | `= <bit>` | `= <bit>` | Standard output coil |
| **Latch / Unlatch**| `S <bit>` / `R <bit>` | `S <bit>` / `R <bit>` | Set / Reset coil |
| **Edge Detect** | `FP <bit>` / `FN <bit>` | `FP <bit>` / `FN <bit>` | Positive / Negative transition pulse |
| **Jumps** | `JU`, `JC`, `JCN`, `JCB`, `JL` | `SPA`, `SPB`, `SPBN`, `SPBB`, `SPL` | Program jump instructions |
| **Block End** | `BE`, `BEC`, `BEU` | `BEA`, `BEB` | Block termination |

> **Addressing Rule**: Bit indices within bytes **must be 0 to 7** (`.0` to `.7`). Addresses like `I 0.8` or `Q 1.9` are invalid.
> **Cross-Platform Mappings**: The validator automatically rejects foreign non-Siemens opcodes (`LD`, `OUT`, `TON`, `XIC`, `OTE`) and provides clear instructions or auto-correction (`auto_fix_mnemonics=True`).

### 2. Timers, Counters & Data Blocks

- **S7 Timers (English)**: `SD T 1` (On-Delay), `SF T 1` (Off-Delay), `SP T 1` (Pulse), `SE T 1` (Extended Pulse), `SS T 1` (Retentive On-Delay).
- **S7 Timers (German)**: `SE T 1` (Einschaltverzögerung), `SA T 1` (Ausschaltverzögerung), `SI T 1` (Impuls), `SV T 1` (Verlängerter Impuls).
  - Time value must be loaded before triggering: `L S5T#5S` followed by `SD T 1`. Underscores between units are supported (e.g. `S5T#1M_30S`, `S5TIME#10S`).
- **S7 Counters**: `CU C 1` (Count Up / German `ZV C 1`), `CD C 1` (Count Down / German `ZR C 1`), `S C 1` (Set), `R C 1` (Reset).
- **Block Calls**: `CALL FC 1`, `CALL FB 1, DB 1`, `CC FC 10` (Conditional Call), `UC FC 20` (Unconditional Call).
- **Data Blocks**: `OPN DB 1`, `DB1.DBX 0.0` (Bit), `DB1.DBB 2` (Byte), `DB1.DBW 4` (Word), `DB1.DBD 8` (Double Word) or open block elements (`DBX 0.0`, `DBW 2`, `DBD 4`).
- **Status Word Queries**: `A ==0`, `A <>0`, `A >0`, `A <0`, `A >=0`, `A <=0`, `A OS`, `A OV`, `A BR`.
- **Address Registers & Stack**: `LAR1 P#0.0`, `LAR2`, `TAR1 MD 10`, `CAR`, `PUSH`, `POP`.
- **Peripherals**: `PIW 256` (Peripheral Input Word), `PQW 256` (Peripheral Output Word).

---

## Device Comments & Global Symbol Table Integration

The server provides bidirectional integration for Siemens STEP 7 Symbol Tables (`.SDF`, `.SEQ`, and `.CSV`), supporting device comments and symbolic programming:

### 1. Defining Device Comments and Symbols
Pass either a symbol-to-address map or an address-to-comment dictionary:

```json
{
  "device_comments": {
    "I 0.0": "Main Motor Start Pushbutton",
    "I 0.1": "Main Motor Stop Pushbutton",
    "Q 0.0": "Main Motor Contactor Coil",
    "M 10.0": "Cycle Running Marker",
    "T 1": "Motor Acceleration Timer"
  }
}
```

- **Auto-Type Inference**: The server automatically infers canonical S7 data types (`BOOL`, `BYTE`, `WORD`, `DWORD`, `TIMER`, `COUNTER`, etc.).
- **Disambiguation**: If multiple addresses share the identical comment text (e.g., spare inputs), symbol identifiers are automatically disambiguated with address slugs (`Spare_Input_I_0_0`, `Spare_Input_I_0_1`) to prevent duplicate symbol collisions.
- **SDF & CSV Artifacts**: Writes both tab-delimited Siemens `.SDF` and comma-separated `.CSV` tables.

### 2. Querying and Searching Comments & Symbols
Use `s7_search_symbols_and_comments` to query symbols by wildcard pattern or substring across addresses, symbol identifiers, comments, or data types:
```json
{
  "query": "*Motor*",
  "filter_type": "COMMENT",
  "max_results": 20
}
```

### 3. Annotating AWL / STL Source Code
Use `s7_annotate_awl_code` to translate or document AWL source text using three distinct formatting styles:
- **`COMMENTS`**: Appends inline comments: `A I 0.0;  // "START_PB": Main Motor Start Pushbutton`
- **`SYMBOLIC`**: Substitutes physical operands with symbolic names: `A "START_PB";  // I 0.0: Main Motor Start Pushbutton`
- **`ABSOLUTE`**: Canonical physical addressing: `A I 0.0;  // "START_PB"`

---

## Project & Station Management

The server automates SIMATIC Manager (`s7tgtopx.exe`) launching, project creation, and block editing:

- **`s7_create_project`**: Creates a new STEP 7 project targeting `S7-300`, `S7-400`, or `WinAC`. Automatically starts SIMATIC Manager if closed, selects the target CPU model (e.g. `CPU 315-2 DP`), and establishes standard directory structures (`.s7p`, `station.json`, `Blocks/OB1.awl`, `Symbols/symbols.sdf`).
- **`s7_open_project`**: Opens an existing project folder or `.s7p` file, automatically launching SIMATIC Manager if closed.
- **`s7_open_block`**: Opens or focuses the target block in the LAD/FBD/STL Editor (`s7kotx.exe`).

---

## Deep Program Analysis & Quality of Life (QoL) Tools

### 1. Cross-Reference Table (`s7_get_cross_references`)
Builds a complete Siemens Querverweisliste tracking every operand read, assignment (`=`), SET (`S`), RESET (`R`), and transfer (`T`).
- **Memory Overlap Detection**: Detects multi-byte hazards where a bit address (e.g. `M 0.1` or `Q 0.0`) overlaps with a byte, word, or double-word write (e.g. `MB 0`, `MW 0`, `QB 0`).

### 2. Memory Usage Allocation Map (`s7_get_memory_usage_map`)
Computes exact memory allocation and visual ASCII bit grids (`[ 0:X 1:. 2:. 3:. 4:. 5:. 6:. 7:X ]`) across Inputs (`I`), Outputs (`Q`), Markers (`M`), Timers (`T`), Counters (`C`), and Data Blocks (`DB`). Enforces family-specific hardware limits.

### 3. Block Inspection & Call Hierarchy (`s7_get_block_overview`)
Scans workspace projects for all compiled and source blocks (`OB`, `FB`, `FC`, `DB`), extracts estimated MC7 byte sizes, network counts, and generates the complete call dependency graph (`OB1 -> [FC 10, FB 1]`).

### 4. Comprehensive Program Safety Analysis (`s7_analyze_program`)
Performs static engineering validation:
- **Star-Delta Switching Hazards**: Enforces reciprocal interlocks (`AN Delta` in Star network, `AN Star` in Delta network) to prevent phase-to-phase dead shorts. Supports English and German naming.
- **Duplicate Output Coils**: Flags destructive duplicate `=` assignments to identical bit addresses.
- **Unpaired SET / RESET Coils**: Detects set bits never reset within the block.
- **Nesting Stack Limit**: Enforces the Siemens hardware parenthetical nesting depth limit (`depth <= 7`).
- **Direct Peripheral Access**: Warns on direct peripheral access (`PIW`, `PQW`) bypassing the process image.
- **Cycle Time & Safety Score**: Estimates CPU scan sweep time in microseconds and calculates an overall safety score (0-100).

### 5. Controller Environment Auto-Detection (`s7_auto_detect_controllers`)
Auto-detects installed STEP 7 Classic versions via Windows registry, identifies executable paths (`s7tgtopx.exe`, `s7kotx.exe`, `s7hwcnfx.exe`), lists running Siemens background services, and inspects local workspace health.

---

## Complete MCP Tools Directory (20 Tools)

| Tool Name | Type | Description |
| :--- | :--- | :--- |
| `s7_get_status` | Inspection | Checks SIMATIC Manager / Editor connection status, active windows, and recent artifacts. |
| `s7_list_supported_plc_types` | Inspection | Lists supported PLC families (S7-300, S7-400, WinAC), limits, and CPU aliases. |
| `s7_auto_detect_controllers` | Inspection | Auto-detects installed STEP 7 Classic environment, binaries, and active Siemens processes. |
| `s7_create_project` | Project | Creates a STEP 7 project with CPU family selection and offline workspace structure. |
| `s7_open_project` | Project | Opens an existing `.s7p` project or workspace folder in SIMATIC Manager. |
| `s7_open_block` | Project | Opens or focuses a target block (`OB1`, `FC`, `FB`) in the LAD/FBD/STL editor. |
| `s7_validate_program` | Safety | Pre-validates AWL networks, syntax, and interlock rules without modifying SIMATIC Manager. |
| `s7_write_program` | Execution | Verified atomic write: backups, candidate import, compilation, and readback verification. |
| `s7_read_program` | Execution | Exports and parses the active block in SIMATIC Manager. |
| `s7_compile_and_check` | Execution | Triggers save and Check Block Consistency in SIMATIC Manager. |
| `s7_write_symbol_table` | Symbols | Exports validated `.SDF` and `.CSV` Symbol Table artifacts. |
| `s7_read_symbol_table` | Symbols | Parses exported `.SDF`, `.SEQ`, or `.CSV` Symbol Tables. |
| `s7_write_device_comments` | Comments | Saves global device comments with automatic type inference and symbol disambiguation. |
| `s7_read_device_comments` | Comments | Reads device comments and symbol dictionaries from active project or workspace. |
| `s7_search_symbols_and_comments` | Search | Searches symbols/comments with wildcards and category filters. |
| `s7_annotate_awl_code` | Code Gen | Annotates AWL code in `COMMENTS`, `SYMBOLIC`, or `ABSOLUTE` styles. |
| `s7_get_cross_references` | Analysis | Generates operand cross-references and flags memory overlap hazards. |
| `s7_get_memory_usage_map` | Analysis | Visual bit matrices and utilization statistics across all memory areas. |
| `s7_get_block_overview` | Analysis | Scans workspace blocks, estimated MC7 sizes, and call dependency trees. |
| `s7_analyze_program` | Analysis | Deep safety analysis: Star-Delta, duplicate coils, nesting limits, cycle times, safety score. |

---

## Generated Artifacts

All backups, candidate programs, and verification files are written to:
`~/Documents/Codex-Simatic7/`

- `ob1-before-OB1-<timestamp>-<hash>.awl` (Verified pre-edit backup)
- `ob1-after-OB1-<timestamp>-<hash>.awl` (Validated candidate source)
- `ob1-verify-OB1-<timestamp>-<hash>.awl` (Compiled readback export)
- `symbols-<timestamp>-<hash>.sdf` (Standard Siemens SDF Symbol table)
- `symbols-<timestamp>-<hash>.csv` (Standard Siemens CSV Symbol table)

Existing artifacts are never overwritten.
