"""
Siemens STEP 7 Ladder Diagram (LAD / KOP) Validator and Converter.

Enforces strict compliance with the Siemens STEP 7 LAD decompiler grammar
to ensure that compiled AWL/STL blocks decompile 100% into graphical Ladder
Diagrams without falling back to Statement List (STL) text in S7KAFAPX.EXE.

Siemens LAD Decompilation Rules:
1. Single Coil / Terminal Output:
   Each network must terminate in exactly ONE coil (=, S, R) or one execution
   box (MOVE, Timer, Counter, Call). Multiple consecutive coils in series
   (e.g., S Q0.0; R Q0.1) violate LAD planar topology and force STL fallback.
   These must be split into dedicated single-action networks.
2. Explicit Parenthesization for Complex Boolean Logic:
   Any combination of parallel OR (O, ON) and series AND (A, AN) must be
   enclosed in explicit nesting parentheses (A( O ... O ... ) or O( A ... A ... )).
   Unparenthesized mixed operations (e.g., A tag1; O tag2; AN tag3;) fail LAD.
3. Data Transfers (L / T) as MOVE Boxes:
   Direct loads and transfers preceded by contact conditions must follow the
   standard Siemens LAD MOVE box template:
       A( ... )
       JNB <label>
       L <source>
       T <destination>
   <label>: NOP 0
4. Timers and Counters:
   LAD timer boxes require NOP 0 placeholders for unused parameter pins
   (Reset, BI, BCD, Q).
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import re

COIL_OPCODES = {"=", "S", "R"}
TIMER_OPCODES = {"SD", "SE", "SP", "SS", "SF"}
COUNTER_OPCODES = {"CU", "CD", "S"}
LOAD_STORE_OPCODES = {"L", "T"}
JUMP_OPCODES = {"JC", "JCN", "JU", "JBI", "JNBI", "JO", "JOS", "LOOP"}


@dataclass
class LADViolation:
    network_index: int
    network_title: str
    rule_name: str
    reason: str
    offending_instructions: List[str]
    suggested_fix: str


@dataclass
class LADValidationReport:
    is_lad_compliant: bool
    total_networks: int
    compliant_networks: int
    violations: List[LADViolation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_lad_compliant": self.is_lad_compliant,
            "total_networks": self.total_networks,
            "compliant_networks": self.compliant_networks,
            "non_compliant_count": len(self.violations),
            "violations": [
                {
                    "network_index": v.network_index,
                    "network_title": v.network_title,
                    "rule_name": v.rule_name,
                    "reason": v.reason,
                    "offending_instructions": v.offending_instructions,
                    "suggested_fix": v.suggested_fix,
                }
                for v in self.violations
            ],
        }


def parse_awl_networks(awl_code: str) -> List[Dict[str, Any]]:
    """Splits an AWL source string into individual network dictionaries."""
    lines = awl_code.splitlines()
    networks = []
    current_net: Optional[Dict[str, Any]] = None
    in_code = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("BEGIN"):
            in_code = True
            continue
        if stripped.startswith("END_ORGANIZATION_BLOCK") or stripped.startswith("END_FUNCTION"):
            if current_net:
                networks.append(current_net)
                current_net = None
            in_code = False
            continue

        if not in_code:
            continue

        if stripped.startswith("NETWORK"):
            if current_net:
                networks.append(current_net)
            current_net = {
                "title": "",
                "comment": "",
                "instructions": [],
                "raw_lines": [],
            }
            continue

        if current_net is not None:
            current_net["raw_lines"].append(line)
            if stripped.startswith("TITLE ="):
                current_net["title"] = stripped[7:].strip().strip('"')
            elif stripped.startswith("//") and not current_net["instructions"]:
                current_net["comment"] += stripped[2:].strip() + " "
            elif stripped and not stripped.startswith("//"):
                current_net["instructions"].append(stripped)

    if current_net:
        networks.append(current_net)

    return networks


def validate_network_lad_compliance(net_idx: int, net: Dict[str, Any]) -> List[LADViolation]:
    """Validates a single network against Siemens STEP 7 LAD decompiler constraints."""
    violations: List[LADViolation] = []
    title = net.get("title", f"Network {net_idx}")
    instructions = net.get("instructions", [])

    if not instructions:
        return violations

    # Extract clean opcodes and operands
    parsed_insts = []
    for raw in instructions:
        # Strip comments
        code_part = raw.split("//")[0].strip().rstrip(";")
        if not code_part:
            continue
        # Handle label
        if ":" in code_part and not code_part.startswith("TITLE"):
            label, rest = code_part.split(":", 1)
            code_part = rest.strip()
            if not code_part:
                continue

        tokens = code_part.split()
        op = tokens[0].upper() if tokens else ""
        operand = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        parsed_insts.append((op, operand, raw))

    if not parsed_insts:
        return violations

    # Rule 1: Check multiple coil outputs (=, S, R) in series
    coil_count = sum(1 for op, _, _ in parsed_insts if op in COIL_OPCODES)
    if coil_count > 1:
        offending = [raw for op, _, raw in parsed_insts if op in COIL_OPCODES]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="Single_Coil_Rule",
                reason=f"Found {coil_count} coil operations in a single network. In Siemens LAD, consecutive coils in series cannot be decompiled and force STL fallback.",
                offending_instructions=offending,
                suggested_fix="Split each coil operation into its own dedicated single-action network with matching contact logic.",
            )
        )

    # Rule 2: Check unparenthesized combinations of OR and AND
    # Pattern: 'O' or 'ON' without parentheses mixed with 'A' or 'AN'
    has_or = False
    has_and = False
    has_parens = False
    for op, _, _ in parsed_insts:
        if op in ("O", "ON"):
            has_or = True
        elif op in ("A", "AN"):
            has_and = True
        elif "(" in op or ")" in op:
            has_parens = True

    if has_or and has_and and not has_parens:
        offending = [raw for op, _, raw in parsed_insts if op in ("A", "AN", "O", "ON")]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="Parenthesized_OR_Rule",
                reason="Unparenthesized combination of OR and AND operations detected. Siemens LAD decompiler requires explicit parentheses A( O ... O ... ) or O( A ... A ... ) to construct parallel ladder branches.",
                offending_instructions=offending,
                suggested_fix="Enclose the OR branches inside 'A( O tag1 O tag2 )' before connecting series contacts.",
            )
        )

    # Rule 3: Check bare L and T without MOVE box pattern
    # In LAD, conditional assignments require JNB and NOP 0
    has_load = any(op == "L" for op, _, _ in parsed_insts)
    has_transfer = any(op == "T" for op, _, _ in parsed_insts)
    has_jnb = any(op == "JNB" for op, _, _ in parsed_insts)
    has_sd = any(op in TIMER_OPCODES for op, _, _ in parsed_insts)

    if has_load and has_transfer and not has_jnb and not has_sd:
        offending = [raw for op, _, raw in parsed_insts if op in ("L", "T")]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="LAD_MOVE_Box_Rule",
                reason="Bare Load (L) and Transfer (T) instructions without JNB pattern. In LAD, register data transfers must be structured as a Siemens MOVE box.",
                offending_instructions=offending,
                suggested_fix="Structure the data transfer as: A( condition ) JNB _label L value T destination _label: NOP 0",
            )
        )

    # Rule 4: Check for raw STL jumps outside standard patterns
    raw_jumps = [raw for op, _, raw in parsed_insts if op in JUMP_OPCODES and op != "JNB"]
    if raw_jumps:
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="No_Raw_Jumps_Rule",
                reason="Raw STL jump instruction detected. Arbitrary jumps cannot be represented in standard Ladder Diagram flow.",
                offending_instructions=raw_jumps,
                suggested_fix="Replace jumps with standard contact interlocks or structured block calls.",
            )
        )

    # Rule 5: Check jump label syntax compliance (max 4 chars, must start with letter)
    for raw in instructions:
        clean = raw.split("//")[0].strip()
        if ":" in clean:
            lbl = clean.split(":", 1)[0].strip()
            if not re.match(r"^[A-Za-z][A-Za-z0-9]{0,3}$", lbl):
                violations.append(
                    LADViolation(
                        network_index=net_idx,
                        network_title=title,
                        rule_name="Invalid_LAD_Label_Syntax",
                        reason=f"Jump label '{lbl}' is invalid in Siemens STEP 7. Labels must start with a letter (A-Z, a-z) and cannot exceed 4 alphanumeric characters.",
                        offending_instructions=[raw],
                        suggested_fix=f"Rename label '{lbl}' to a valid 4-character identifier such as 'm001'.",
                    )
                )

    return violations


def validate_lad_source(awl_code: str) -> LADValidationReport:
    """Validates an entire AWL source block for 100% LAD compliance."""
    networks = parse_awl_networks(awl_code)
    all_violations: List[LADViolation] = []

    for idx, net in enumerate(networks, start=1):
        v = validate_network_lad_compliance(idx, net)
        all_violations.extend(v)

    total = len(networks)
    compliant = total - len({v.network_index for v in all_violations})

    return LADValidationReport(
        is_lad_compliant=(len(all_violations) == 0),
        total_networks=total,
        compliant_networks=compliant,
        violations=all_violations,
    )


def convert_awl_to_pure_lad(awl_code: str, block_title: str = "Pure Ladder Diagram Block") -> str:
    """
    Deterministically transforms any valid S7 AWL source into 100% pure LAD compliant code.
    
    1. Splits multi-coil networks into separate single-action networks.
    2. Parenthesizes all parallel branches (A( O ... O ... )).
    3. Transforms conditional L/T transfers into Siemens LAD MOVE boxes.
    4. Pads Timer instructions with required NOP 0 placeholders.
    """
    networks = parse_awl_networks(awl_code)
    new_networks = []
    move_counter = 1

    for idx, net in enumerate(networks, start=1):
        title = net.get("title", f"Network {idx}")
        comment = net.get("comment", "").strip()
        instructions = net.get("instructions", [])

        # Parse instructions
        parsed = []
        for raw in instructions:
            clean = raw.split("//")[0].strip().rstrip(";")
            if not clean:
                continue
            if ":" in clean and not clean.startswith("TITLE"):
                _, clean = clean.split(":", 1)
                clean = clean.strip()
                if not clean:
                    continue
            tokens = clean.split()
            op = tokens[0].upper()
            operand = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            parsed.append((op, operand))

        if not parsed:
            continue

        # Case A: Conditional L/T -> MOVE Box
        has_l = any(op == "L" for op, _ in parsed)
        has_t = any(op == "T" for op, _ in parsed)
        is_timer = any(op in TIMER_OPCODES for op, _ in parsed)

        if has_l and has_t and not is_timer:
            # Separate contact condition from L/T instructions
            cond_ops = []
            load_ops = []
            transfer_ops = []
            for op, operand in parsed:
                if op in ("A", "AN", "O", "ON", "A(", "O(", ")"):
                    cond_ops.append((op, operand))
                elif op == "L":
                    load_ops.append(operand)
                elif op == "T":
                    transfer_ops.append(operand)

            lbl = f"m{move_counter:03d}"
            move_counter += 1

            net_lines = []
            if cond_ops:
                norm_cond = _normalize_contacts(cond_ops)
                for op, operand in norm_cond:
                    net_lines.append(f"      {op:<5} {operand};")

            net_lines.append(f"      JNB   {lbl};")
            for l_val in load_ops:
                net_lines.append(f"      L     {l_val};")
            for t_dst in transfer_ops:
                net_lines.append(f"      T     {t_dst};")
            net_lines.append(f"{lbl}: NOP   0;")

            new_networks.append({
                "title": title,
                "comment": comment,
                "lines": net_lines,
            })
            continue

        # Case B: Multi-coil splitting
        coils = [(i, op, operand) for i, (op, operand) in enumerate(parsed) if op in COIL_OPCODES]
        if len(coils) > 1:
            # Extract the contact conditions that precede the coils
            # For standard ladder rungs, contacts precede all coils
            contacts = [(op, operand) for op, operand in parsed if op not in COIL_OPCODES]

            # If contacts have unparenthesized OR, parenthesize them
            norm_contacts = _normalize_contacts(contacts)

            for c_idx, (_, c_op, c_operand) in enumerate(coils, start=1):
                net_lines = [f"      {op:<5} {operand};" for op, operand in norm_contacts]
                net_lines.append(f"      {c_op:<5} {c_operand};")

                sub_title = f"{title} (Part {c_idx}: {c_op} {c_operand})"
                new_networks.append({
                    "title": sub_title,
                    "comment": f"Single-rung action: {c_op} {c_operand}",
                    "lines": net_lines,
                })
            continue

        # Case C: Single coil / timer / other
        # Check and normalize contacts for unparenthesized OR
        contacts = [(op, operand) for op, operand in parsed if op not in COIL_OPCODES and op not in TIMER_OPCODES]
        action_ops = [(op, operand) for op, operand in parsed if op in COIL_OPCODES or op in TIMER_OPCODES]

        norm_contacts = _normalize_contacts(contacts)
        net_lines = [f"      {op:<5} {operand};" for op, operand in norm_contacts]

        for op, operand in action_ops:
            net_lines.append(f"      {op:<5} {operand};")
            if op in TIMER_OPCODES:
                # Add 4 NOP 0 placeholders for LAD timer box
                net_lines.extend([
                    "      NOP   0;",
                    "      NOP   0;",
                    "      NOP   0;",
                    "      NOP   0;",
                ])

        new_networks.append({
            "title": title,
            "comment": comment,
            "lines": net_lines,
        })

    # Build output AWL source
    out_lines = [
        "ORGANIZATION_BLOCK OB 1",
        f'TITLE = "{block_title}"',
        "VERSION : 0.1",
        "",
        "VAR_TEMP",
        "  OB1_EV_CLASS : BYTE ;",
        "  OB1_SCAN_1 : BYTE ;",
        "  OB1_PRIORITY : BYTE ;",
        "  OB1_OB_NUMBR : BYTE ;",
        "  OB1_RESERVED_1 : BYTE ;",
        "  OB1_RESERVED_2 : BYTE ;",
        "  OB1_PREV_CYCLE : INT ;",
        "  OB1_MIN_CYCLE : INT ;",
        "  OB1_MAX_CYCLE : INT ;",
        "  OB1_DATE_TIME : DATE_AND_TIME ;",
        "END_VAR",
        "",
        "BEGIN",
        "",
    ]

    for i, net in enumerate(new_networks, start=1):
        out_lines.append("NETWORK")
        out_lines.append(f"TITLE = Network {i}: {net['title']}")
        if net["comment"]:
            out_lines.append(f"// {net['comment']}")
        out_lines.extend(net["lines"])
        out_lines.append("")

    out_lines.append("END_ORGANIZATION_BLOCK")
    return "\n".join(out_lines) + "\n"


def _normalize_contacts(contacts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    Ensures that any OR operations mixed with AND operations are properly
    parenthesized as A( O ... O ... ).
    """
    has_or = any(op in ("O", "ON") for op, _ in contacts)
    has_and = any(op in ("A", "AN") for op, _ in contacts)
    has_parens = any("(" in op or ")" in op for op, _ in contacts)

    if has_or and has_and and not has_parens:
        # Group initial OR operations
        or_ops = []
        and_ops = []
        for op, operand in contacts:
            if op in ("O", "ON") or (not or_ops and op in ("A", "AN")):
                # Map leading A to O inside the OR group
                norm_op = "O" if op == "A" else ("ON" if op == "AN" else op)
                or_ops.append((norm_op, operand))
            else:
                and_ops.append((op, operand))

        result = [("A(", "")]
        result.extend(or_ops)
        result.append((")", ""))
        result.extend(and_ops)
        return result

    # Check for pure OR without AND: e.g. O tag1; O tag2; or A tag1; O tag2;
    if has_or and not has_and and not has_parens:
        norm = []
        for op, operand in contacts:
            norm_op = "O" if op == "A" else ("ON" if op == "AN" else op)
            norm.append((norm_op, operand))
        result = [("A(", "")]
        result.extend(norm)
        result.append((")", ""))
        return result

    return contacts
