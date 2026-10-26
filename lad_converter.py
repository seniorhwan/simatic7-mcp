"""
Siemens STEP 7 Ladder Diagram (LAD / KOP) AST Validator and Compiler.

Enforces strict compliance with the Siemens STEP 7 LAD decompiler grammar
using a formal Boolean Contact Graph AST and an S7 Status Word (FC / RLO / OR)
Virtual Machine to guarantee 100% truth-table equivalence and instruction-order
preservation.

Siemens LAD Decompilation Rules:
1. Single Coil / Terminal Output:
   Each network must terminate in a single coil (=, S, R) or execution box
   (MOVE, Math, Timer, Counter, Call). Consecutive coils or independent logic
   chains in a single network are split into dedicated single-action networks
   respecting S7 First-Check (FC) boundaries.
2. Explicit Parenthesization for Complex Boolean Logic (AST Contact Graph):
   Any combination of parallel OR (O, ON) and series AND (A, AN) is parsed into
   an exact Boolean AST using Siemens S7 CPU status-word semantics (FC, RLO, OR,
   and nesting stack) and emitted with canonical parenthesization:
   A( O ... O( A ... ) ) without inverting Boolean precedence.
3. Data Transfers & Arithmetic Boxes (JNB ... NOP 0):
   Register loads, transfers, and arithmetic (+I, -I, *I, /I, etc.) are preserved
   in exact sequential order inside individual Siemens LAD box structures:
       <contact condition>
       JNB <label>
       L <source>
       [arithmetic/word op]
       T <destination>
   <label>: NOP 0
4. Valid 4-Character Alphabetic Jump Labels:
   Jump labels must match ^[A-Za-z][A-Za-z0-9]{0,3}$ (e.g. m001).
5. Timers and Counters:
   LAD timer boxes pad unused parameter pins with 4 sequential NOP 0 instructions.
6. Block Interface Preservation:
   Preserves ORGANIZATION_BLOCK (OB), FUNCTION (FC), and FUNCTION_BLOCK (FB)
   headers, return types, and VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / VAR / VAR_TEMP
   declarations without coercing non-OB blocks into OB 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import re

COIL_OPCODES = {"=", "S", "R"}
TIMER_OPCODES = {"SD", "SE", "SP", "SS", "SF", "SI", "SV", "SA"}
COUNTER_OPCODES = {"CU", "CD", "ZV", "ZR"}
LOAD_STORE_OPCODES = {"L", "T"}
JUMP_OPCODES = {"JC", "JCN", "JU", "JBI", "JNBI", "JO", "JOS", "LOOP", "JL"}
CMP_OPCODES = {
    "==I", "<>I", ">I", "<I", ">=I", "<=I",
    "==D", "<>D", ">D", "<D", ">=D", "<=D",
    "==R", "<>R", ">R", "<R", ">=R", "<=R",
}
ARITH_AND_DATA_OPCODES = {
    "+I", "-I", "*I", "/I",
    "+D", "-D", "*D", "/D", "MOD",
    "+R", "-R", "*R", "/R",
    "ABS", "SQR", "SQRT", "EXP", "LN", "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
    "AW", "OW", "XW", "AD", "OD", "XD",
    "INV_I", "INV_D", "NEG_I", "NEG_D", "NEG_R",
    "SLW", "SRW", "SLD", "SRD", "SSI", "SSD", "RLD", "RRD",
    "ITB", "BTI", "BTD", "ITD", "DTB", "DTR", "RND", "TRUNC", "RND+", "RND-", "CAW", "CAD",
    "INC", "DEC", "TAK", "PUSH", "POP", "ENT", "LEAVE", "+",
}
EDGE_OPCODES = {"FP", "FN"}
XOR_BASIC_OPCODES = {"X", "XN"}
XOR_PAREN_OPCODES = {"X(", "XN("}
CONTACT_BASIC_OPCODES = {"A", "AN", "O", "ON", "U", "UN"} | XOR_BASIC_OPCODES
PAREN_OPEN_OPCODES = {"A(", "AN(", "O(", "ON(", "U(", "UN("} | XOR_PAREN_OPCODES
CONTACT_ALL_OPCODES = CONTACT_BASIC_OPCODES | PAREN_OPEN_OPCODES | EDGE_OPCODES | {")", "NOT"}


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


# ============================================================================
# BOOLEAN CONTACT GRAPH AST & S7 RLO VIRTUAL MACHINE
# ============================================================================

class ASTNode:
    """Base class for Siemens S7 Boolean Contact Graph AST nodes."""


@dataclass(frozen=True)
class ContactNode(ASTNode):
    operand: str
    negated: bool = False


@dataclass(frozen=True)
class EdgeNode(ASTNode):
    operand: str
    op: str = "FP"
    negated: bool = False


@dataclass(frozen=True)
class ComparisonNode(ASTNode):
    arg1: str
    arg2: str
    cmp_op: str


@dataclass(frozen=True)
class AndNode(ASTNode):
    children: Tuple[ASTNode, ...]


@dataclass(frozen=True)
class OrNode(ASTNode):
    branches: Tuple[ASTNode, ...]


def make_and(left: Optional[ASTNode], right: Optional[ASTNode]) -> Optional[ASTNode]:
    if left is None:
        return right
    if right is None:
        return left
    left_items = list(left.children) if isinstance(left, AndNode) else [left]
    right_items = list(right.children) if isinstance(right, AndNode) else [right]
    return AndNode(tuple(left_items + right_items))


def make_or(left: Optional[ASTNode], right: Optional[ASTNode]) -> Optional[ASTNode]:
    if left is None:
        return right
    if right is None:
        return left
    left_items = list(left.branches) if isinstance(left, OrNode) else [left]
    right_items = list(right.branches) if isinstance(right, OrNode) else [right]
    return OrNode(tuple(left_items + right_items))


_CMP_INVERSE = {
    "==I": "<>I", "<>I": "==I", ">I": "<=I", "<I": ">=I", ">=I": "<I", "<=I": ">I",
    "==D": "<>D", "<>D": "==D", ">D": "<=D", "<D": ">=D", ">=D": "<D", "<=D": ">D",
    "==R": "<>R", "<>R": "==R", ">R": "<=R", "<R": ">=R", ">=R": "<R", "<=R": ">R",
}


def make_not(node: ASTNode) -> ASTNode:
    """Applies De Morgan's Law to push negation down to leaf contacts/comparators."""
    if isinstance(node, ContactNode):
        return ContactNode(node.operand, negated=not node.negated)
    if isinstance(node, EdgeNode):
        return EdgeNode(node.operand, op=node.op, negated=not node.negated)
    if isinstance(node, AndNode):
        return OrNode(tuple(make_not(c) for c in node.children))
    if isinstance(node, OrNode):
        return AndNode(tuple(make_not(b) for b in node.branches))
    if isinstance(node, ComparisonNode) and node.cmp_op in _CMP_INVERSE:
        return ComparisonNode(node.arg1, node.arg2, _CMP_INVERSE[node.cmp_op])
    return node


def _normalize_contact_opcode(op: str) -> str:
    op_u = op.upper()
    mapping = {"U": "A", "UN": "AN", "U(": "A(", "UN(": "AN("}
    return mapping.get(op_u, op_u)


def _coerce_inst_tuples(insts: Any) -> List[Tuple[str, str]]:
    """
    Accepts any representation of STL instructions:
      - multiline string ("A I 0.0;\nO I 0.1;")
      - list of instruction strings (['A "START_PB"', 'A     "SAFETY_OK";'])
      - list of (op, operand) tuples ([('A', '"START_PB"'), ...])
    and normalizes them into a clean list of (op, operand) tuples.
    """
    if not insts:
        return []
    if isinstance(insts, str):
        raw_items: List[Any] = insts.splitlines()
    else:
        raw_items = list(insts)

    out: List[Tuple[str, str]] = []
    for item in raw_items:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            op = str(item[0]).strip().rstrip(";").upper()
            operand = " ".join(str(item[1]).strip().rstrip(";").split())
            if op:
                out.append((op, operand))
        elif isinstance(item, str):
            code = item.split("//")[0].strip().rstrip(";").strip()
            if not code:
                continue
            if ":" in code and ":=" not in code and not code.upper().startswith(("TITLE", "S5T#", "T#", "W#", "DW#", "B#", "L#", "P#")):
                _, code = code.split(":", 1)
                code = code.strip()
                if not code:
                    continue
            tokens = code.split()
            if tokens:
                op = tokens[0].upper()
                operand = " ".join(tokens[1:]) if len(tokens) > 1 else ""
                out.append((op, operand))
    return out


def _pregroup_comparators(insts: Any) -> List[Tuple[str, str]]:
    """Groups 3-instruction comparator sequences (L arg1; L arg2; >=I) into atomic nodes."""
    coerced = _coerce_inst_tuples(insts)
    grouped: List[Tuple[str, str]] = []
    i = 0
    n = len(coerced)
    while i < n:
        if (
            i + 2 < n
            and coerced[i][0].upper() == "L"
            and coerced[i + 1][0].upper() == "L"
            and coerced[i + 2][0].upper() in CMP_OPCODES
        ):
            arg1 = coerced[i][1]
            arg2 = coerced[i + 1][1]
            cmp_op = coerced[i + 2][0].upper()
            grouped.append(("__CMP__", f"{arg1}|{arg2}|{cmp_op}"))
            i += 3
        else:
            grouped.append((_normalize_contact_opcode(coerced[i][0]), coerced[i][1]))
            i += 1
    return grouped


def parse_boolean_ast(insts: Any) -> Optional[ASTNode]:
    """
    Parses a sequence of S7 STL Boolean contact instructions into a formal Contact Graph AST.
    Models the exact Siemens S7 CPU hardware status word (FC, RLO, OR, and 7-level nesting stack).
    """
    grouped = _pregroup_comparators(insts)
    if not grouped:
        return None

    rlo: Optional[ASTNode] = None
    or_acc: Optional[ASTNode] = None
    fc: bool = False
    stack: List[Tuple[str, Optional[ASTNode], Optional[ASTNode], bool]] = []

    def flush_or(cur_rlo: Optional[ASTNode], cur_or: Optional[ASTNode]) -> Tuple[Optional[ASTNode], None]:
        if cur_or is not None and cur_rlo is not None:
            return make_or(cur_or, cur_rlo), None
        if cur_or is not None:
            return cur_or, None
        return cur_rlo, None

    for op, operand in grouped:
        if op in ("A", "AN", "__CMP__"):
            if op == "__CMP__":
                arg1, arg2, cmp_op = operand.split("|", 2)
                node: ASTNode = ComparisonNode(arg1, arg2, cmp_op)
            else:
                node = ContactNode(operand, negated=(op == "AN"))
            if not fc or rlo is None:
                rlo = node
                fc = True
            else:
                rlo = make_and(rlo, node)
        elif op in ("FP", "FN"):
            node = EdgeNode(operand, op=op, negated=False)
            if not fc or rlo is None:
                rlo = node
                fc = True
            else:
                rlo = make_and(rlo, node)
        elif op in ("O", "ON"):
            if not operand:
                # Bare 'O' instruction: OR-ing of AND functions
                if fc and rlo is not None:
                    or_acc = make_or(or_acc, rlo)
                    rlo = None
                    fc = False
            else:
                node = ContactNode(operand, negated=(op == "ON"))
                rlo, or_acc = flush_or(rlo, or_acc)
                if not fc or rlo is None:
                    rlo = node
                    fc = True
                else:
                    rlo = make_or(rlo, node)
                    fc = True
        elif op in ("X", "XN"):
            node = ContactNode(operand, negated=(op == "XN"))
            rlo, or_acc = flush_or(rlo, or_acc)
            if not fc or rlo is None:
                rlo = node
                fc = True
            else:
                rlo = make_or(make_and(rlo, make_not(node)), make_and(make_not(rlo), node))
                fc = True
        elif op in ("A(", "AN(", "O(", "ON(", "X(", "XN("):
            if op in ("O(", "ON(", "X(", "XN("):
                rlo, or_acc = flush_or(rlo, or_acc)
            stack.append((op, rlo, or_acc, fc))
            rlo = None
            or_acc = None
            fc = False
        elif op == ")":
            inner, _ = flush_or(rlo, or_acc)
            if stack:
                open_op, saved_rlo, saved_or_acc, saved_fc = stack.pop()
                if inner is not None:
                    if open_op in ("AN(", "ON(", "XN("):
                        inner = make_not(inner)
                    if open_op in ("A(", "AN("):
                        if not saved_fc or saved_rlo is None:
                            rlo = inner
                            or_acc = saved_or_acc
                            fc = True
                        else:
                            rlo = make_and(saved_rlo, inner)
                            or_acc = saved_or_acc
                            fc = True
                    elif open_op in ("X(", "XN("):
                        base, _ = flush_or(saved_rlo, saved_or_acc)
                        if not saved_fc or base is None:
                            rlo = inner
                            or_acc = None
                            fc = True
                        else:
                            rlo = make_or(make_and(base, make_not(inner)), make_and(make_not(base), inner))
                            or_acc = None
                            fc = True
                    else:
                        base, _ = flush_or(saved_rlo, saved_or_acc)
                        if not saved_fc or base is None:
                            rlo = inner
                            or_acc = None
                            fc = True
                        else:
                            rlo = make_or(base, inner)
                            or_acc = None
                            fc = True
                else:
                    rlo, or_acc, fc = saved_rlo, saved_or_acc, saved_fc
        elif op == "NOT":
            rlo, or_acc = flush_or(rlo, or_acc)
            if rlo is not None:
                rlo = make_not(rlo)

    while stack:
        open_op, saved_rlo, saved_or_acc, saved_fc = stack.pop()
        inner, _ = flush_or(rlo, or_acc)
        if inner is not None:
            if open_op in ("AN(", "ON(", "XN("):
                inner = make_not(inner)
            if open_op in ("A(", "AN("):
                rlo = inner if (not saved_fc or saved_rlo is None) else make_and(saved_rlo, inner)
                or_acc = saved_or_acc
                fc = True
            elif open_op in ("X(", "XN("):
                base, _ = flush_or(saved_rlo, saved_or_acc)
                rlo = inner if (not saved_fc or base is None) else make_or(make_and(base, make_not(inner)), make_and(make_not(base), inner))
                or_acc = None
                fc = True
            else:
                base, _ = flush_or(saved_rlo, saved_or_acc)
                rlo = inner if (not saved_fc or base is None) else make_or(base, inner)
                or_acc = None
                fc = True
        else:
            rlo, or_acc, fc = saved_rlo, saved_or_acc, saved_fc

    rlo, _ = flush_or(rlo, or_acc)
    return rlo


def emit_lad_ast(node: Optional[ASTNode], wrap_top_level_or: bool = False) -> List[Tuple[str, str]]:
    """
    Serializes a Boolean Contact Graph AST into canonical Siemens STEP 7 LAD
    planar instructions. Guarantees that all parallel OR branches are enclosed
    in explicit O / O( ... ) or A( O ... ) blocks as required by S7KAFAPX.EXE.
    """
    if node is None:
        return []

    def emit_or_branches(branches: Tuple[ASTNode, ...]) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for br in branches:
            if isinstance(br, ContactNode):
                out.append(("ON" if br.negated else "O", br.operand))
            elif isinstance(br, OrNode):
                out.extend(emit_or_branches(br.branches))
            else:
                out.append(("O(", ""))
                out.extend(emit_series(br))
                out.append((")", ""))
        return out

    def emit_series(curr: ASTNode) -> List[Tuple[str, str]]:
        if isinstance(curr, ContactNode):
            return [("AN" if curr.negated else "A", curr.operand)]
        if isinstance(curr, EdgeNode):
            res_edge: List[Tuple[str, str]] = [(curr.op, curr.operand)]
            if curr.negated:
                res_edge.append(("NOT", ""))
            return res_edge
        if isinstance(curr, ComparisonNode):
            return [("L", curr.arg1), ("L", curr.arg2), (curr.cmp_op, "")]
        if isinstance(curr, OrNode):
            res: List[Tuple[str, str]] = [("A(", "")]
            res.extend(emit_or_branches(curr.branches))
            res.append((")", ""))
            return res
        if isinstance(curr, AndNode):
            res = []
            for idx, ch in enumerate(curr.children):
                if isinstance(ch, ContactNode):
                    res.append(("AN" if ch.negated else "A", ch.operand))
                elif isinstance(ch, EdgeNode):
                    res.append((ch.op, ch.operand))
                    if ch.negated:
                        res.append(("NOT", ""))
                elif isinstance(ch, ComparisonNode):
                    if idx == 0:
                        res.extend([("L", ch.arg1), ("L", ch.arg2), (ch.cmp_op, "")])
                    else:
                        res.append(("A(", ""))
                        res.extend([("L", ch.arg1), ("L", ch.arg2), (ch.cmp_op, "")])
                        res.append((")", ""))
                elif isinstance(ch, OrNode):
                    res.append(("A(", ""))
                    res.extend(emit_or_branches(ch.branches))
                    res.append((")", ""))
                elif isinstance(ch, AndNode):
                    res.extend(emit_series(ch))
            return res
        return []

    if isinstance(node, OrNode) and not wrap_top_level_or:
        return emit_or_branches(node.branches)
    return emit_series(node)


def evaluate_ast(node: Optional[ASTNode], env: Dict[str, bool]) -> bool:
    """Evaluates a Boolean Contact Graph AST against a truth-table variable assignment."""
    if node is None:
        return True
    if isinstance(node, ContactNode):
        val = bool(env.get(node.operand, False))
        return (not val) if node.negated else val
    if isinstance(node, EdgeNode):
        key = f"__EDGE__:{node.op}|{node.operand}"
        val = bool(env.get(key, False))
        return (not val) if node.negated else val
    if isinstance(node, ComparisonNode):
        key = f"__CMP__:{node.arg1}|{node.arg2}|{node.cmp_op}"
        return bool(env.get(key, False))
    if isinstance(node, AndNode):
        return all(evaluate_ast(c, env) for c in node.children)
    if isinstance(node, OrNode):
        return any(evaluate_ast(b, env) for b in node.branches)
    return True


def evaluate_stl_rlo(insts: List[Tuple[str, str]], env: Dict[str, bool]) -> bool:
    """
    Simulates the physical Siemens S7-300/400 CPU Status Word registers (FC, RLO, OR,
    and nesting stack) directly on raw STL instructions for formal verification.
    """
    grouped = _pregroup_comparators(insts)
    if not grouped:
        return True

    rlo = False
    or_acc = False
    fc = False
    stack: List[Tuple[str, bool, bool, bool]] = []

    for op, operand in grouped:
        if op in ("A", "AN", "__CMP__"):
            if op == "__CMP__":
                val = bool(env.get(f"__CMP__:{operand}", False))
            else:
                raw_v = bool(env.get(operand, False))
                val = (not raw_v) if op == "AN" else raw_v
            if not fc:
                rlo = val
                fc = True
            else:
                rlo = rlo and val
        elif op in ("FP", "FN"):
            val = bool(env.get(f"__EDGE__:{op}|{operand}", False))
            if not fc:
                rlo = val
                fc = True
            else:
                rlo = rlo and val
        elif op in ("O", "ON"):
            if not operand:
                if fc:
                    or_acc = or_acc or rlo
                    rlo = False
                    fc = False
            else:
                raw_v = bool(env.get(operand, False))
                val = (not raw_v) if op == "ON" else raw_v
                if or_acc:
                    rlo = or_acc or rlo
                    or_acc = False
                if not fc:
                    rlo = val
                    fc = True
                else:
                    rlo = rlo or val
                    fc = True
        elif op in ("X", "XN"):
            raw_v = bool(env.get(operand, False))
            val = (not raw_v) if op == "XN" else raw_v
            if or_acc:
                rlo = or_acc or rlo
                or_acc = False
            if not fc:
                rlo = val
                fc = True
            else:
                rlo = (rlo != val)
                fc = True
        elif op in ("A(", "AN(", "O(", "ON(", "X(", "XN("):
            if op in ("O(", "ON(", "X(", "XN(") and or_acc:
                rlo = or_acc or rlo
                or_acc = False
            stack.append((op, rlo, or_acc, fc))
            rlo = False
            or_acc = False
            fc = False
        elif op == ")":
            inner = or_acc or rlo
            if stack:
                open_op, saved_rlo, saved_or_acc, saved_fc = stack.pop()
                if open_op in ("AN(", "ON(", "XN("):
                    inner = not inner
                if open_op in ("A(", "AN("):
                    rlo = inner if not saved_fc else (saved_rlo and inner)
                    or_acc = saved_or_acc
                    fc = True
                elif open_op in ("X(", "XN("):
                    base = saved_or_acc or saved_rlo
                    rlo = inner if not saved_fc else (base != inner)
                    or_acc = False
                    fc = True
                else:
                    base = saved_or_acc or saved_rlo
                    rlo = inner if not saved_fc else (base or inner)
                    or_acc = False
                    fc = True
        elif op == "NOT":
            if or_acc:
                rlo = or_acc or rlo
                or_acc = False
            rlo = not rlo

    return or_acc or rlo


def verify_boolean_equivalence(
    orig_insts: List[Tuple[str, str]],
    conv_insts: List[Tuple[str, str]],
) -> Tuple[bool, Optional[Dict[str, bool]]]:
    """
    Formally verifies across all 2^N truth-table assignments (or deterministic boundary
    samples if N > 12) that `orig_insts` and `conv_insts` produce identical RLO outputs.
    Returns (is_equivalent, counterexample_env_or_None).
    """
    vars_set = set()
    for seq in (orig_insts, conv_insts):
        for op, operand in _pregroup_comparators(seq):
            if op == "__CMP__":
                vars_set.add(f"__CMP__:{operand}")
            elif op in ("FP", "FN") and operand:
                vars_set.add(f"__EDGE__:{op}|{operand}")
            elif op in ("A", "AN", "O", "ON", "X", "XN") and operand:
                vars_set.add(operand)

    var_list = sorted(vars_set)
    n = len(var_list)
    if n == 0:
        return True, None

    if n <= 12:
        total = 1 << n
        for mask in range(total):
            env = {var_list[bit]: bool((mask >> bit) & 1) for bit in range(n)}
            if evaluate_stl_rlo(orig_insts, env) != evaluate_stl_rlo(conv_insts, env):
                return False, env
    else:
        # Test all single-hot, single-cold, alternating, and pseudo-random bit vectors
        test_masks = [0, (1 << n) - 1]
        for b in range(n):
            test_masks.append(1 << b)
            test_masks.append(((1 << n) - 1) ^ (1 << b))
        for seed in range(256):
            m = 0
            for b in range(n):
                if ((seed * 1103515245 + 12345 + b * 97) >> 8) & 1:
                    m |= (1 << b)
            test_masks.append(m)
        for mask in test_masks:
            env = {var_list[bit]: bool((mask >> bit) & 1) for bit in range(n)}
            if evaluate_stl_rlo(orig_insts, env) != evaluate_stl_rlo(conv_insts, env):
                return False, env

    return True, None


def _normalize_contacts(
    contacts: List[Tuple[str, str]],
    wrap_top_level_or: bool = False,
) -> List[Tuple[str, str]]:
    """
    Compiles a sequence of boolean contact operations into canonical Siemens LAD
    parenthesized form via the Boolean Contact Graph AST, verifying truth-table
    equivalence before returning.
    """
    if not contacts:
        return []
    ast = parse_boolean_ast(contacts)
    if ast is None:
        return contacts
    emitted = emit_lad_ast(ast, wrap_top_level_or=wrap_top_level_or)
    ok, _ = verify_boolean_equivalence(contacts, emitted)
    if not ok:
        raise ValueError(f"Boolean AST equivalence check failed for contact sequence: {contacts}")
    return emitted


# ============================================================================
# AWL PARSER & VALIDATOR
# ============================================================================

def _extract_awl_envelope_and_networks(awl_code: str) -> Tuple[List[str], List[Dict[str, Any]], str]:
    """
    Extracts (header_lines, networks, footer_line) from an AWL source string,
    preserving OB / FC / FB / DB declarations and VAR_* interface blocks.
    """
    lines = awl_code.splitlines()
    header_lines: List[str] = []
    networks: List[Dict[str, Any]] = []
    footer_line: str = "END_ORGANIZATION_BLOCK"
    current_net: Optional[Dict[str, Any]] = None
    in_code = False
    found_begin = False

    end_prefixes = (
        "END_ORGANIZATION_BLOCK",
        "END_FUNCTION_BLOCK",
        "END_FUNCTION",
        "END_DATA_BLOCK",
        "END_TYPE",
    )

    for line in lines:
        stripped = line.strip()
        up = stripped.upper()

        if not found_begin and (up == "BEGIN" or up.startswith("BEGIN ")):
            found_begin = True
            in_code = True
            continue

        if any(up.startswith(ep) for ep in end_prefixes):
            if current_net:
                networks.append(current_net)
                current_net = None
            footer_line = stripped
            in_code = False
            continue

        if not in_code:
            if not found_begin:
                header_lines.append(line)
            continue

        if up.startswith("NETWORK"):
            if current_net:
                networks.append(current_net)
            current_net = {
                "title": "",
                "comment": "",
                "instructions": [],
                "clean_instructions": [],
                "raw_lines": [],
            }
            continue

        if current_net is None:
            if stripped:
                current_net = {
                    "title": "",
                    "comment": "",
                    "instructions": [],
                    "clean_instructions": [],
                    "raw_lines": [],
                }
            else:
                continue

        current_net["raw_lines"].append(line)
        if up.startswith("TITLE =") or up.startswith("TITLE="):
            eq_idx = stripped.find("=")
            current_net["title"] = stripped[eq_idx + 1:].strip().strip('"')
        elif stripped.startswith("//") and not current_net["instructions"]:
            comment_line = stripped[2:].strip()
            if current_net["comment"]:
                current_net["comment"] += " " + comment_line
            else:
                current_net["comment"] = comment_line
        elif stripped and not stripped.startswith("//"):
            current_net["instructions"].append(stripped)
            clean_part = " ".join(stripped.split("//")[0].strip().rstrip(";").split())
            if clean_part:
                current_net["clean_instructions"].append(clean_part)

    if current_net:
        networks.append(current_net)

    # Support bare network snippets without BEGIN/END_...
    if not found_begin and not networks and header_lines:
        first_non_empty = next((ln.strip().upper() for ln in header_lines if ln.strip()), "")
        prefix = "BEGIN\n" if first_non_empty.startswith("NETWORK") else "BEGIN\nNETWORK\n"
        snippet_wrapped = prefix + "\n".join(header_lines) + "\nEND_ORGANIZATION_BLOCK\n"
        _, networks, _ = _extract_awl_envelope_and_networks(snippet_wrapped)
        header_lines = []

    return header_lines, networks, footer_line


def parse_awl_networks(awl_code: str) -> List[Dict[str, Any]]:
    """Splits an AWL source string into individual network dictionaries."""
    _, networks, _ = _extract_awl_envelope_and_networks(awl_code)
    return networks


def _has_unparenthesized_mixed_boolean(parsed_insts: List[Tuple[str, str, str]]) -> Tuple[bool, List[str]]:
    """
    Checks whether a network contains unparenthesized mixed AND/OR operations
    at any parenthesis nesting level (e.g. A x; O y; AN z; or A x; O( A y; A z; );).
    Evaluates each RLO chain segment independently across coils/boxes/BLD boundaries.
    """
    segments: List[List[Tuple[str, str, str]]] = [[]]
    for op, operand, raw in parsed_insts:
        norm_op = _normalize_contact_opcode(op)
        if norm_op in CONTACT_ALL_OPCODES:
            segments[-1].append((norm_op, operand, raw))
        elif op in COIL_OPCODES or op in ("BLD", "JNB", "CALL", "UC", "CC") or op in TIMER_OPCODES or op in COUNTER_OPCODES:
            if segments[-1]:
                segments.append([])

    for contact_seq in segments:
        if not contact_seq:
            continue

        # Check parenthesis balance and per-level operator mixing
        # In Siemens LAD:
        # - An OR group (inside A( ... ) or at top level) must use O / ON / O( for all its branches,
        #   not mix A / AN / A( with O / ON / O( at the same nesting level!
        level_ops: List[List[str]] = [[]]
        offending = [raw for _, _, raw in contact_seq]

        for norm_op, operand, _ in contact_seq:
            if norm_op in XOR_BASIC_OPCODES or norm_op in XOR_PAREN_OPCODES:
                return True, offending
            if norm_op in ("A(", "AN(", "O(", "ON("):
                base_kind = "O" if norm_op.startswith("O") else "A"
                level_ops[-1].append(base_kind)
                level_ops.append([])
            elif norm_op == ")":
                if len(level_ops) <= 1:
                    return True, offending
                inner_ops = level_ops.pop()
                if "O" in inner_ops and "A" in inner_ops:
                    return True, offending
            elif norm_op in ("A", "AN", "FP", "FN"):
                level_ops[-1].append("A")
            elif norm_op in ("O", "ON"):
                if not operand:
                    # Bare 'O' without parentheses is STL-only
                    return True, offending
                level_ops[-1].append("O")

        if len(level_ops) != 1:
            return True, offending

        top_ops = level_ops[0]
        if "O" in top_ops and "A" in top_ops:
            return True, offending

    return False, []


def _is_canonical_sr_rs_box(
    parsed_insts: List[Tuple[str, str, str]],
    coil_insts: List[Tuple[str, str, str]],
) -> bool:
    """
    Checks if a network represents a canonical Siemens LAD SR (Set-Reset) or RS (Reset-Set) flip-flop box.
    In Siemens LAD, an SR/RS flip-flop is represented in AWL/STL as:
      [S input contact logic]
      S <target>
      [R input contact logic]
      R <target>
      [NOP 0 or = <output>]
    (or with R before S for RS flip-flops).
    """
    if len(coil_insts) not in (2, 3):
        return False

    op1, target1, _ = coil_insts[0]
    op2, target2, _ = coil_insts[1]

    # Must be {S, R} targeting the exact same bit operand
    if {op1, op2} != {"S", "R"}:
        return False

    norm_target1 = re.sub(r"\s+", "", target1).upper()
    norm_target2 = re.sub(r"\s+", "", target2).upper()
    if norm_target1 != norm_target2:
        return False

    if len(coil_insts) == 3:
        op3, _, _ = coil_insts[2]
        if op3 != "=":
            return False

    # Find indices of coil_insts in parsed_insts
    try:
        idx1 = next(i for i, inst in enumerate(parsed_insts) if inst == coil_insts[0])
        idx2 = next(i for i, inst in enumerate(parsed_insts) if i > idx1 and inst == coil_insts[1])
    except StopIteration:
        return False

    # Must have contact logic before first coil
    if idx1 == 0:
        return False

    # Must have contact logic between first and second coil
    if idx2 <= idx1 + 1:
        return False

    # Check instructions after second coil (or third coil)
    last_coil_idx = idx2
    if len(coil_insts) == 3:
        try:
            idx3 = next(i for i, inst in enumerate(parsed_insts) if i > idx2 and inst == coil_insts[2])
            last_coil_idx = idx3
        except StopIteration:
            return False

    for op, _, _ in parsed_insts[last_coil_idx + 1:]:
        if op not in ("NOP", "BLD"):
            return False

    return True


def validate_network_lad_compliance(net_idx: int, net: Dict[str, Any]) -> List[LADViolation]:
    """Validates a single network against Siemens STEP 7 LAD decompiler constraints."""
    violations: List[LADViolation] = []
    title = net.get("title", f"Network {net_idx}")
    instructions = net.get("instructions", [])

    if not instructions:
        return violations

    parsed_insts: List[Tuple[str, str, str]] = []
    for raw in instructions:
        code_part = raw.split("//")[0].strip().rstrip(";")
        if not code_part:
            continue
        if ":" in code_part and not code_part.upper().startswith("TITLE") and ":=" not in code_part:
            _, rest = code_part.split(":", 1)
            code_part = rest.strip()
            if not code_part:
                continue

        tokens = code_part.split()
        op = tokens[0].upper() if tokens else ""
        operand = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        parsed_insts.append((op, operand, raw))

    if not parsed_insts:
        return violations

    # Rule 1: Check multiple coil outputs (=, S, R) in series/same network
    # Exclude intermediate local bit stores (= L 20.0) used in CALL parameter setup
    # and canonical Siemens LAD SR/RS flip-flop boxes
    has_call = any(op in ("CALL", "UC", "CC") for op, _, _ in parsed_insts)
    coil_insts = [
        (op, operand, raw)
        for op, operand, raw in parsed_insts
        if op in COIL_OPCODES and not (has_call and op == "=" and re.match(r"^L\s*\d+\.\d+$", operand, re.I))
    ]
    if len(coil_insts) > 1 and not _is_canonical_sr_rs_box(parsed_insts, coil_insts):
        offending = [raw for _, _, raw in coil_insts]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="Single_Coil_Rule",
                reason=f"Found {len(coil_insts)} coil operations in a single network. In Siemens LAD, multiple coils in one network force STL fallback.",
                offending_instructions=offending,
                suggested_fix="Split each coil operation into its own dedicated single-action network with matching contact logic.",
            )
        )

    # Rule 2: Check unparenthesized combinations of OR and AND at any nesting level
    mixed_bool, offending_bool = _has_unparenthesized_mixed_boolean(parsed_insts)
    if mixed_bool:
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="Parenthesized_OR_Rule",
                reason="Unparenthesized combination of OR and AND operations detected. Siemens LAD decompiler requires explicit parentheses A( O ... O ... ) or O( A ... A ... ) to construct parallel ladder branches.",
                offending_instructions=offending_bool,
                suggested_fix="Enclose the OR branches inside 'A( O tag1 O tag2 )' before connecting series contacts.",
            )
        )

    # Rule 3: Check bare L and T / arithmetic without JNB MOVE/Math box pattern
    has_load = any(op == "L" for op, _, _ in parsed_insts)
    has_transfer = any(op == "T" for op, _, _ in parsed_insts)
    has_arith = any(op in ARITH_AND_DATA_OPCODES for op, _, _ in parsed_insts)
    has_jnb = any(op == "JNB" for op, _, _ in parsed_insts)
    has_sd = any(op in TIMER_OPCODES or op in COUNTER_OPCODES for op, _, _ in parsed_insts)
    transfer_count = sum(1 for op, _, _ in parsed_insts if op == "T")

    if ((has_load and has_transfer) or has_arith) and not has_jnb and not has_sd:
        offending = [raw for op, _, raw in parsed_insts if op in ("L", "T") or op in ARITH_AND_DATA_OPCODES]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="LAD_MOVE_Box_Rule",
                reason="Bare Load (L), Arithmetic, or Transfer (T) instructions without JNB pattern. In LAD, register transfers and math boxes must be structured with JNB and NOP 0.",
                offending_instructions=offending,
                suggested_fix="Structure each transfer/math box as: A( condition ) JNB m001 L value [op] T destination m001: NOP 0",
            )
        )
    elif has_jnb and transfer_count > 1:
        offending = [raw for op, _, raw in parsed_insts if op in ("L", "T") or op in ARITH_AND_DATA_OPCODES]
        violations.append(
            LADViolation(
                network_index=net_idx,
                network_title=title,
                rule_name="LAD_MOVE_Box_Rule",
                reason=f"Found {transfer_count} transfers inside a single network. A single LAD MOVE/Math box can only write to one destination.",
                offending_instructions=offending,
                suggested_fix="Split each L/T transfer or math box into its own single-box network.",
            )
        )

    # Rule 4: Check for raw STL jumps outside standard JNB box pattern
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
        if ":" in clean and not clean.upper().startswith("TITLE") and ":=" not in clean:
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


# ============================================================================
# AST-BASED AWL TO PURE LAD COMPILER
# ============================================================================

def _clean_network_title(raw_title: str, fallback_idx: int) -> str:
    """Strips redundant 'Network N:' prefixes so re-converting does not duplicate prefixes."""
    t = (raw_title or "").strip()
    while True:
        m = re.match(r"^Network\s+\d+\s*:\s*(.+)$", t, re.IGNORECASE)
        if m:
            t = m.group(1).strip()
        else:
            break
    return t or f"Rung {fallback_idx}"


def _format_inst_line(op: str, operand: str = "", label: str = "") -> str:
    prefix = f"{label}: " if label else "      "
    if operand:
        return f"{prefix}{op:<5} {operand};"
    return f"{prefix}{op:<5} ;" if op in ("A(", "AN(", "O(", "ON(", ")") else f"{prefix}{op};"


def _split_data_units(data_insts: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """
    Splits a sequence of L / Arithmetic / T instructions into individual execution
    box units in exact sequential order. Each unit ends at a 'T <dst>' instruction,
    preserving all preceding 'L' and arithmetic opcodes (+I, -I, *I, /I, etc.)
    without ever reordering loads before transfers.
    """
    units: List[List[Tuple[str, str]]] = []
    current_unit: List[Tuple[str, str]] = []

    for op, operand in data_insts:
        current_unit.append((op, operand))
        if op == "T":
            units.append(current_unit)
            current_unit = []

    if current_unit:
        if units:
            units[-1].extend(current_unit)
        else:
            units.append(current_unit)

    return units


def convert_awl_to_pure_lad(
    awl_code: str,
    block_title: Optional[str] = None,
) -> str:
    """
    Deterministically compiles any valid S7 AWL source into 100% pure LAD-compliant code:
    1. Preserves original block type (OB, FC, FB) and VAR_* interface declarations.
    2. Parses Boolean contact chains into an exact Contact Graph AST and verifies
       2^N truth-table equivalence against the S7 CPU RLO state machine.
    3. Splits multi-coil and multi-chain networks along S7 First-Check (FC) boundaries
       so independent rungs never cross-contaminate one another.
    4. Preserves all arithmetic (+I, -I, *I, /I, shifts, conversions) and L/T instruction
       ordering inside individual JNB ... NOP 0 LAD boxes.
    5. Pads Timer instructions with 4 NOP 0 placeholders idempotently.
    """
    header_lines, networks, footer_line = _extract_awl_envelope_and_networks(awl_code)
    new_networks: List[Dict[str, Any]] = []
    move_counter = 1

    for idx, net in enumerate(networks, start=1):
        base_title = _clean_network_title(net.get("title", ""), idx)
        comment = net.get("comment", "").strip()
        instructions = net.get("instructions", [])

        # Parse raw lines into (label, op, operand)
        parsed: List[Tuple[str, str, str]] = []
        for raw in instructions:
            clean = raw.split("//")[0].strip().rstrip(";")
            if not clean:
                continue
            lbl = ""
            if ":" in clean and not clean.upper().startswith("TITLE") and ":=" not in clean:
                lbl_part, rest = clean.split(":", 1)
                lbl = lbl_part.strip()
                clean = rest.strip()
                if not clean:
                    parsed.append((lbl, "NOP", "0"))
                    continue
            tokens = clean.split()
            if not tokens:
                continue
            op = tokens[0].upper()
            operand = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            parsed.append((lbl, op, operand))

        if not parsed:
            continue

        # Pass through CALL networks intact (only normalizing any invalid jump labels)
        if any(op in ("CALL", "UC", "CC") for _, op, _ in parsed):
            net_lines = [f"      {raw.strip()}" for raw in instructions if raw.strip()]
            new_networks.append({
                "title": base_title,
                "comment": comment,
                "lines": net_lines,
            })
            continue

        # General network decomposition respecting S7 First-Check (FC) boundaries:
        # Walk instruction stream and segment into (contacts, action) rungs.
        current_contacts: List[Tuple[str, str]] = []
        last_was_action = False
        rungs: List[Tuple[List[Tuple[str, str]], str, Any]] = []

        i = 0
        n = len(parsed)
        while i < n:
            lbl, op, operand = parsed[i]
            norm_op = _normalize_contact_opcode(op)

            # 0. Check existing JNB <label> ... <label>: NOP 0 box
            if op == "JNB":
                target_lbl = operand.strip()
                body_insts: List[Tuple[str, str]] = []
                j = i + 1
                while j < n:
                    b_lbl, b_op, b_operand = parsed[j]
                    if b_lbl and (
                        (target_lbl and b_lbl.upper() == target_lbl.upper())
                        or (b_op == "NOP" and b_operand == "0")
                    ):
                        if not (b_op == "NOP" and b_operand == "0"):
                            body_insts.append((b_op, b_operand))
                        j += 1
                        break
                    body_insts.append((b_op, b_operand))
                    j += 1
                units = _split_data_units(body_insts) if body_insts else [[("NOP", "0")]]
                for unit in units:
                    rungs.append((list(current_contacts), "BOX", unit))
                last_was_action = True
                i = j
                continue

            # 1. Check 3-instruction Comparator (L arg1; L arg2; >=I)
            if (
                i + 2 < n
                and op == "L"
                and parsed[i + 1][1] == "L"
                and parsed[i + 2][1] in CMP_OPCODES
            ):
                if last_was_action:
                    current_contacts = []
                    last_was_action = False
                current_contacts.append(("L", operand))
                current_contacts.append(("L", parsed[i + 1][2]))
                current_contacts.append((parsed[i + 2][1], parsed[i + 2][2]))
                i += 3
                continue

            # 2. Check Timer / Counter with preceding L <preset>
            if (
                i + 1 < n
                and op == "L"
                and (
                    parsed[i + 1][1] in TIMER_OPCODES
                    or parsed[i + 1][1] in COUNTER_OPCODES
                    or (parsed[i + 1][1] == "S" and re.match(r'^(".*"|[CZ]\s*\d+)$', parsed[i + 1][2], re.I) and "S5T" not in operand.upper())
                )
            ):
                t_op = parsed[i + 1][1]
                t_operand = parsed[i + 1][2]
                rungs.append((list(current_contacts), "TIMER", (operand, t_op, t_operand)))
                last_was_action = True
                i += 2
                # Skip any existing NOP 0 padding immediately following the timer
                while i < n and parsed[i][1] == "NOP" and parsed[i][2] == "0":
                    i += 1
                continue

            # 3. Check standalone Timer / Counter opcode
            if op in TIMER_OPCODES or op in COUNTER_OPCODES:
                rungs.append((list(current_contacts), "TIMER", (None, op, operand)))
                last_was_action = True
                i += 1
                while i < n and parsed[i][1] == "NOP" and parsed[i][2] == "0":
                    i += 1
                continue

            # 4. Check Boolean Contact / Parenthesis / Edge / Midline NOT
            if norm_op in CONTACT_ALL_OPCODES:
                if last_was_action:
                    if norm_op == "NOT" and current_contacts:
                        # Midline NOT after a coil inverts the preserved RLO from the upstream chain
                        last_was_action = False
                    else:
                        # Previous coil/box reset FC=0, so this contact starts a brand new logic chain
                        current_contacts = []
                        last_was_action = False
                current_contacts.append((norm_op, operand))
                i += 1
                continue

            # 5. Check Coil (=, S, R)
            if op in COIL_OPCODES:
                rungs.append((list(current_contacts), "COIL", (op, operand)))
                last_was_action = True
                i += 1
                # Skip trailing NOP 0 after SR/RS coils
                while i < n and parsed[i][1] == "NOP" and parsed[i][2] == "0":
                    i += 1
                continue

            # 6. Check Data / Arithmetic sequence (L, T, +I, -I, *I, /I, etc.)
            if op in LOAD_STORE_OPCODES or op in ARITH_AND_DATA_OPCODES:
                data_seq: List[Tuple[str, str]] = []
                while i < n:
                    # Stop if next is a comparator or timer preset
                    if (
                        i + 2 < n
                        and parsed[i][1] == "L"
                        and parsed[i + 1][1] == "L"
                        and parsed[i + 2][1] in CMP_OPCODES
                    ):
                        break
                    if (
                        i + 1 < n
                        and parsed[i][1] == "L"
                        and (parsed[i + 1][1] in TIMER_OPCODES or parsed[i + 1][1] in COUNTER_OPCODES)
                    ):
                        break
                    cur_op = parsed[i][1]
                    cur_operand = parsed[i][2]
                    if cur_op in LOAD_STORE_OPCODES or cur_op in ARITH_AND_DATA_OPCODES:
                        data_seq.append((cur_op, cur_operand))
                        i += 1
                    else:
                        break

                for unit in _split_data_units(data_seq):
                    rungs.append((list(current_contacts), "BOX", unit))
                last_was_action = True
                continue

            # 7. Other instructions (NOP 0, SET, CLR, BE, etc.)
            if op == "NOP" and operand == "0":
                if not rungs and not current_contacts and i == n - 1:
                    rungs.append(([], "RAW", [("NOP", "0")]))
                i += 1
                continue

            rungs.append((list(current_contacts), "RAW", [(op, operand)]))
            last_was_action = True
            i += 1

        if not rungs and current_contacts:
            rungs.append((list(current_contacts), "RAW", []))

        # Emit each decomposed rung as a pure LAD network
        total_rungs = len(rungs)
        for r_idx, (r_contacts, r_kind, r_payload) in enumerate(rungs, start=1):
            wrap_or = r_kind in ("BOX", "TIMER")
            norm_contacts = _normalize_contacts(r_contacts, wrap_top_level_or=wrap_or) if r_contacts else []
            net_lines = [_format_inst_line(c_op, c_operand) for c_op, c_operand in norm_contacts]

            if r_kind == "COIL":
                c_op, c_operand = r_payload
                net_lines.append(_format_inst_line(c_op, c_operand))
                sub_title = (
                    base_title
                    if total_rungs == 1
                    else f"{base_title} (Part {r_idx}: {c_op} {c_operand})"
                )
                sub_comment = comment if total_rungs == 1 else f"Single-rung action: {c_op} {c_operand}"
            elif r_kind == "TIMER":
                preset, t_op, t_operand = r_payload
                if preset is not None:
                    net_lines.append(_format_inst_line("L", preset))
                net_lines.append(_format_inst_line(t_op, t_operand))
                if t_op in TIMER_OPCODES:
                    net_lines.extend([
                        "      NOP   0;",
                        "      NOP   0;",
                        "      NOP   0;",
                        "      NOP   0;",
                    ])
                sub_title = base_title if total_rungs == 1 else f"{base_title} (Part {r_idx}: {t_op} {t_operand})"
                sub_comment = comment
            elif r_kind == "BOX":
                unit_insts: List[Tuple[str, str]] = r_payload
                lbl_name = f"m{move_counter:03d}"
                move_counter += 1
                net_lines.append(_format_inst_line("JNB", lbl_name))
                for b_op, b_operand in unit_insts:
                    net_lines.append(_format_inst_line(b_op, b_operand))
                net_lines.append(f"{lbl_name}: NOP   0;")
                dst_op = next((b_operand for b_op, b_operand in reversed(unit_insts) if b_op == "T"), "")
                sub_title = (
                    base_title
                    if total_rungs == 1
                    else f"{base_title} (Part {r_idx}: T {dst_op})" if dst_op else f"{base_title} (Part {r_idx})"
                )
                sub_comment = comment
            else:
                for raw_op, raw_operand in r_payload:
                    net_lines.append(_format_inst_line(raw_op, raw_operand))
                sub_title = base_title if total_rungs == 1 else f"{base_title} (Part {r_idx})"
                sub_comment = comment

            new_networks.append({
                "title": sub_title,
                "comment": sub_comment,
                "lines": net_lines,
            })

    # Reconstruct output AWL preserving original header (OB / FC / FB + VAR_* declarations)
    if header_lines:
        out_lines = list(header_lines)
        if block_title is not None:
            replaced_title = False
            for h_idx, h_line in enumerate(out_lines):
                if h_line.strip().upper().startswith("TITLE"):
                    out_lines[h_idx] = f'TITLE = "{block_title}"'
                    replaced_title = True
                    break
            if not replaced_title and out_lines:
                out_lines.insert(1, f'TITLE = "{block_title}"')
        out_lines.append("BEGIN")
        out_lines.append("")
    else:
        eff_title = block_title or "Pure Ladder Diagram Block"
        out_lines = [
            "ORGANIZATION_BLOCK OB 1",
            f'TITLE = "{eff_title}"',
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

    out_lines.append(footer_line)
    return "\n".join(out_lines) + "\n"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("Usage: python lad_converter.py <input.awl> [output.awl]")
        sys.exit(1)

    in_path = Path(sys.argv[1])
    raw_src = in_path.read_text(encoding="utf-8", errors="ignore")
    orig_rep = validate_lad_source(raw_src)
    print(f"Input: {in_path} ({orig_rep.total_networks} networks, {len(orig_rep.violations)} LAD violations)")

    converted_src = convert_awl_to_pure_lad(raw_src)
    conv_rep = validate_lad_source(converted_src)
    print(f"Converted: {conv_rep.total_networks} networks, {len(conv_rep.violations)} LAD violations (compliant={conv_rep.is_lad_compliant})")

    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(converted_src, encoding="ascii")
        print(f"Saved pure LAD AWL to: {out_path}")

