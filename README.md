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

## Global Symbol Tables & Comments

Symbol tables can be provided as a dictionary mapping symbol name to address, address to symbol name, or a detailed list of objects:

```json
{
  "symbols": {
    "START_PB": "I 0.0",
    "STOP_PB": "I 0.1",
    "MOTOR_CONTACTOR": "Q 0.0"
  }
}
```

Or address-first mapping (compatible with GX Works2 device comments):
```json
{
  "device_comments": {
    "I 0.0": "START BUTTON",
    "Q 0.0": "MAIN CONTACTOR"
  }
}
```

- `s7_write_symbol_table`: Exports validated `.SDF` (tab-delimited) and `.CSV` Symbol Table artifacts.
- `s7_read_symbol_table`: Parses exported `.SDF`, `.SEQ`, or `.CSV` Symbol Tables back into structured entries.

---

## Static Safety & Engineering Checks

The server performs deterministic pre-flight static analysis on every candidate:
1. **Star-Delta Switching Hazards**: Warns if Star contactor output and Delta contactor output are activated without mutual software interlocking (`AN` the opposing coil). Supports English ("STAR"/"DELTA") and German ("STERN"/"DREIECK") naming and resolves symbolic references.
2. **Duplicate Assignment Coils**: Detects multiple `=` coil writes to the identical bit address across networks (which causes the first assignment to be overwritten during PLC scan cycles), resolving symbolic coils to physical addresses.
3. **Timer Value Initializations**: Flags S7 timers triggered without prior `L S5T#...` time constants.
4. **Symbol Table Consistency**: Verifies declared symbols and warns about unreferenced device definitions.

---

## Available MCP Tools

- `s7_get_status`: Checks whether SIMATIC Manager or the LAD/FBD/STL Editor is running, lists active window and recent workspace artifacts.
- `s7_list_supported_plc_types`: Lists supported Siemens S7 PLC families, limits, aliases, and addressing conventions.
- `s7_create_project`: Connects to SIMATIC Manager to create a project via native New Project dialog or establishes offline project workspace artifacts.
- `s7_read_program`: Exports and parses the currently displayed block (`OB1`, `FC`, `FB`).
- `s7_validate_program`: Pre-validates candidate networks, opcodes, operand boundaries, and static safety rules without touching SIMATIC Manager.
- `s7_write_program`: Validates candidate, creates local AWL artifact, backs up existing block, imports candidate into SIMATIC Manager, compiles/checks block, and verifies readback equality.
- `s7_compile_and_check`: Triggers save and compilation (Check Block Consistency) in SIMATIC Manager.
- `s7_write_symbol_table`: Validates and saves standard Siemens `.SDF` (tab-delimited) and `.CSV` Symbol Table artifacts.
- `s7_read_symbol_table`: Reads and validates a previously exported Siemens STEP 7 Symbol Table (`.SDF`, `.SEQ`, or `.CSV`).

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
