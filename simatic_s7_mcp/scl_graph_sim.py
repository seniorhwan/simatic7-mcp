"""Deterministic S7-300/400 SCL (Structured Text) and S7-GRAPH (SFC) execution engines for VirtualS7PLC.

Allows AI agents to write realistic Siemens STEP 7 Classic blocks in ANY IEC 61131-3 language
(STL/AWL, LAD/KOP, FBD/FUP, SCL/ST, S7-GRAPH/SFC, DB/UDT) and simulate them deterministically
scan-by-scan inside ``VirtualS7PLC`` without requiring Windows S7-PLCSIM.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# SCL (IEC 61131-3 Structured Text) Safe Recursive-Descent Interpreter
# ---------------------------------------------------------------------------

_SCL_TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
    | (?P<COMMENT>//[^\n]*|\(\*[\s\S]*?\*\))
    | (?P<STR>'[^']*')
    | (?P<QUOTED>"[^"]+")
    | (?P<TIME>S5T\#[0-9A-Za-z_.]+|T\#[0-9A-Za-z_.]+)
    | (?P<HEX>(?:B|W|DW)?\#16\#[0-9A-Fa-f_]+|16\#[0-9A-Fa-f_]+)
    | (?P<FLOAT>\d+\.\d+(?:[eE][+-]?\d+)?)
    | (?P<INT>\d+)
    | (?P<OP>:=|<>|<=|>=|\.\.|[+\-*/=<>(),;:\[\].])
    | (?P<IDENT>[\#A-Za-z_][A-Za-z0-9_]*(?:\.\d+)?)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_SCL_KEYWORDS = {
    "IF", "THEN", "ELSIF", "ELSE", "END_IF",
    "CASE", "OF", "END_CASE",
    "FOR", "TO", "BY", "DO", "END_FOR",
    "WHILE", "END_WHILE",
    "REPEAT", "UNTIL", "END_REPEAT",
    "EXIT", "RETURN",
    "AND", "OR", "XOR", "NOT", "MOD",
    "TRUE", "FALSE",
}


@dataclass
class _Tok:
    kind: str
    value: str


def _tokenize_scl(code: str) -> List[_Tok]:
    tokens: List[_Tok] = []
    pos = 0
    n = len(code)
    while pos < n:
        m = _SCL_TOKEN_RE.match(code, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup or ""
        val = m.group(0)
        if kind in ("WS", "COMMENT"):
            continue
        if kind == "IDENT":
            up = val.upper()
            if up in _SCL_KEYWORDS:
                tokens.append(_Tok("KW", up))
            else:
                tokens.append(_Tok("IDENT", val))
        else:
            tokens.append(_Tok(kind, val))
    tokens.append(_Tok("EOF", ""))
    return tokens


@dataclass
class ParsedSclBlock:
    block_name: str
    block_type: str  # OB, FC, FB
    var_decls: Dict[str, Tuple[str, str, Any]] = field(default_factory=dict)  # UPPER -> (section, dtype, init)
    body_stmts: List[Any] = field(default_factory=list)
    parse_errors: List[str] = field(default_factory=list)


def _parse_init_literal(raw: str, dtype: str) -> Any:
    s = raw.strip().rstrip(";").strip()
    up = s.upper()
    if up == "TRUE":
        return True
    if up == "FALSE":
        return False
    if up.startswith("16#"):
        return int(up[3:].replace("_", ""), 16)
    if re.fullmatch(r"[+-]?\d+\.\d+(?:[eE][+-]?\d+)?", s):
        return float(s)
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if dtype.upper() == "BOOL":
        return False
    if dtype.upper() == "REAL":
        return 0.0
    return 0


def _default_for_type(dtype: str) -> Any:
    dt = dtype.strip().upper()
    if dt == "BOOL":
        return False
    if dt == "REAL":
        return 0.0
    return 0


class _SclParser:
    def __init__(self, tokens: List[_Tok]) -> None:
        self.toks = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> _Tok:
        idx = self.pos + offset
        if idx < len(self.toks):
            return self.toks[idx]
        return _Tok("EOF", "")

    def advance(self) -> _Tok:
        t = self.peek()
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def match(self, kind: str, value: Optional[str] = None) -> bool:
        t = self.peek()
        if t.kind == kind and (value is None or t.value.upper() == value.upper()):
            self.advance()
            return True
        return False

    def parse_stmts(self, stop_kws: Tuple[str, ...]) -> List[Any]:
        stmts: List[Any] = []
        while self.peek().kind != "EOF":
            t = self.peek()
            if t.kind == "KW" and t.value in stop_kws:
                break
            if t.kind == "OP" and t.value == ";":
                self.advance()
                continue
            stmts.append(self.parse_stmt())
        return stmts

    def parse_stmt(self) -> Any:
        t = self.peek()
        if t.kind == "KW":
            if t.value == "IF":
                return self.parse_if()
            if t.value == "CASE":
                return self.parse_case()
            if t.value == "FOR":
                return self.parse_for()
            if t.value == "WHILE":
                return self.parse_while()
            if t.value == "REPEAT":
                return self.parse_repeat()
            if t.value == "RETURN":
                self.advance()
                self.match("OP", ";")
                return ("RETURN",)
            if t.value == "EXIT":
                self.advance()
                self.match("OP", ";")
                return ("EXIT",)

        # Assignment or block/function call
        lhs = self.parse_lvalue()
        if self.match("OP", ":="):
            rhs = self.parse_expr()
            self.match("OP", ";")
            return ("ASSIGN", lhs, rhs)
        if self.match("OP", "("):
            args = self._parse_call_args()
            self.match("OP", ";")
            return ("CALL_STMT", lhs, args)
        # Skip stray token to avoid infinite loop
        self.advance()
        return ("NOP",)

    def _parse_call_args(self) -> List[Tuple[Optional[str], Any]]:
        args: List[Tuple[Optional[str], Any]] = []
        if self.match("OP", ")"):
            return args
        while self.peek().kind != "EOF":
            # Named arg: Param := Expr
            if self.peek().kind == "IDENT" and self.peek(1).kind == "OP" and self.peek(1).value == ":=":
                pname = self.advance().value
                self.advance()  # :=
                expr = self.parse_expr()
                args.append((pname, expr))
            else:
                expr = self.parse_expr()
                args.append((None, expr))
            if not self.match("OP", ","):
                break
        self.match("OP", ")")
        return args

    def parse_lvalue(self) -> str:
        t = self.advance()
        parts = [t.value]
        # Handle spaced S7 addresses like I 0.0, Q 0.0, MW 10, PIW 256, DB1.DBW 2
        if t.kind == "IDENT" and t.value.upper() in (
            "I", "E", "Q", "A", "M", "L",
            "IB", "IW", "ID", "EB", "EW", "ED",
            "QB", "QW", "QD", "AB", "AW", "AD",
            "MB", "MW", "MD", "LB", "LW", "LD",
            "PIB", "PIW", "PID", "PEB", "PEW", "PED",
            "PQB", "PQW", "PQD", "PAB", "PAW", "PAD",
            "T", "C", "Z", "DB",
        ):
            nxt = self.peek()
            if nxt.kind in ("INT", "FLOAT"):
                parts.append(self.advance().value)
        # Handle dot-chained members: DB1.DBX0.0, "MotorDB".Speed, FC1.DB1
        while self.match("OP", "."):
            nxt = self.advance()
            parts.append("." + nxt.value)
            if nxt.value.upper() in ("DBX", "DBB", "DBW", "DBD") and self.peek().kind in ("INT", "FLOAT"):
                parts.append(self.advance().value)
        return "".join(parts)

    def parse_if(self) -> Any:
        self.advance()  # IF
        cond = self.parse_expr()
        self.match("KW", "THEN")
        then_body = self.parse_stmts(("ELSIF", "ELSE", "END_IF"))
        elifs: List[Tuple[Any, List[Any]]] = []
        while self.match("KW", "ELSIF"):
            econd = self.parse_expr()
            self.match("KW", "THEN")
            ebody = self.parse_stmts(("ELSIF", "ELSE", "END_IF"))
            elifs.append((econd, ebody))
        else_body: List[Any] = []
        if self.match("KW", "ELSE"):
            else_body = self.parse_stmts(("END_IF",))
        self.match("KW", "END_IF")
        self.match("OP", ";")
        return ("IF", cond, then_body, elifs, else_body)

    def parse_case(self) -> Any:
        self.advance()  # CASE
        selector = self.parse_expr()
        self.match("KW", "OF")
        branches: List[Tuple[List[Tuple[int, int]], List[Any]]] = []
        else_body: List[Any] = []
        while self.peek().kind != "EOF":
            if self.peek().kind == "KW" and self.peek().value == "END_CASE":
                break
            if self.match("KW", "ELSE"):
                else_body = self.parse_stmts(("END_CASE",))
                break
            # Parse case labels: 1, 2, 3..5 :
            ranges: List[Tuple[int, int]] = []
            while self.peek().kind != "EOF":
                sign = -1 if self.match("OP", "-") else 1
                lo_tok = self.advance()
                lo = sign * int(lo_tok.value) if lo_tok.value.isdigit() else 0
                hi = lo
                if self.match("OP", ".."):
                    sign2 = -1 if self.match("OP", "-") else 1
                    hi_tok = self.advance()
                    hi = sign2 * int(hi_tok.value) if hi_tok.value.isdigit() else lo
                ranges.append((lo, hi))
                if not self.match("OP", ","):
                    break
            self.match("OP", ":")
            # Parse statements until next case label (<int>[,|..|:]) or ELSE or END_CASE
            body: List[Any] = []
            while self.peek().kind != "EOF":
                t0 = self.peek()
                if t0.kind == "KW" and t0.value in ("ELSE", "END_CASE"):
                    break
                if self._looks_like_case_label():
                    break
                if t0.kind == "OP" and t0.value == ";":
                    self.advance()
                    continue
                body.append(self.parse_stmt())
            branches.append((ranges, body))
        self.match("KW", "END_CASE")
        self.match("OP", ";")
        return ("CASE", selector, branches, else_body)

    def _looks_like_case_label(self) -> bool:
        i = 0
        if self.peek(i).kind == "OP" and self.peek(i).value == "-":
            i += 1
        if self.peek(i).kind != "INT":
            return False
        i += 1
        nxt = self.peek(i)
        return nxt.kind == "OP" and nxt.value in (":", ",", "..")

    def parse_for(self) -> Any:
        self.advance()  # FOR
        var_name = self.advance().value
        self.match("OP", ":=")
        start_expr = self.parse_expr()
        self.match("KW", "TO")
        end_expr = self.parse_expr()
        step_expr: Any = ("LIT", 1)
        if self.match("KW", "BY"):
            step_expr = self.parse_expr()
        self.match("KW", "DO")
        body = self.parse_stmts(("END_FOR",))
        self.match("KW", "END_FOR")
        self.match("OP", ";")
        return ("FOR", var_name, start_expr, end_expr, step_expr, body)

    def parse_while(self) -> Any:
        self.advance()  # WHILE
        cond = self.parse_expr()
        self.match("KW", "DO")
        body = self.parse_stmts(("END_WHILE",))
        self.match("KW", "END_WHILE")
        self.match("OP", ";")
        return ("WHILE", cond, body)

    def parse_repeat(self) -> Any:
        self.advance()  # REPEAT
        body = self.parse_stmts(("UNTIL",))
        self.match("KW", "UNTIL")
        cond = self.parse_expr()
        self.match("KW", "END_REPEAT")
        self.match("OP", ";")
        return ("REPEAT", body, cond)

    # Expression precedence: OR -> XOR -> AND -> CMP -> ADD -> MUL -> UNARY -> PRIMARY
    def parse_expr(self) -> Any:
        return self.parse_or()

    def parse_or(self) -> Any:
        node = self.parse_xor()
        while self.match("KW", "OR"):
            node = ("BIN", "OR", node, self.parse_xor())
        return node

    def parse_xor(self) -> Any:
        node = self.parse_and()
        while self.match("KW", "XOR"):
            node = ("BIN", "XOR", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_cmp()
        while self.match("KW", "AND"):
            node = ("BIN", "AND", node, self.parse_cmp())
        return node

    def parse_cmp(self) -> Any:
        node = self.parse_add()
        while self.peek().kind == "OP" and self.peek().value in ("=", "<>", "<", "<=", ">", ">="):
            op = self.advance().value
            node = ("BIN", op, node, self.parse_add())
        return node

    def parse_add(self) -> Any:
        node = self.parse_mul()
        while self.peek().kind == "OP" and self.peek().value in ("+", "-"):
            op = self.advance().value
            node = ("BIN", op, node, self.parse_mul())
        return node

    def parse_mul(self) -> Any:
        node = self.parse_unary()
        while (self.peek().kind == "OP" and self.peek().value in ("*", "/")) or (
            self.peek().kind == "KW" and self.peek().value == "MOD"
        ):
            op = self.advance().value.upper()
            node = ("BIN", op, node, self.parse_unary())
        return node

    def parse_unary(self) -> Any:
        if self.match("KW", "NOT"):
            return ("UNARY", "NOT", self.parse_unary())
        if self.match("OP", "-"):
            return ("UNARY", "-", self.parse_unary())
        if self.match("OP", "+"):
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Any:
        t = self.peek()
        if self.match("OP", "("):
            expr = self.parse_expr()
            self.match("OP", ")")
            return expr
        if t.kind == "KW" and t.value == "TRUE":
            self.advance()
            return ("LIT", True)
        if t.kind == "KW" and t.value == "FALSE":
            self.advance()
            return ("LIT", False)
        if t.kind == "INT":
            self.advance()
            return ("LIT", int(t.value))
        if t.kind == "FLOAT":
            self.advance()
            return ("LIT", float(t.value))
        if t.kind == "HEX":
            self.advance()
            return ("LIT", int(t.value.split("#")[-1].replace("_", ""), 16))
        if t.kind == "TIME":
            self.advance()
            return ("TIME_LIT", t.value)
        if t.kind == "STR":
            self.advance()
            return ("LIT", t.value[1:-1])
        if t.kind in ("IDENT", "QUOTED"):
            ref = self.parse_lvalue()
            if self.match("OP", "("):
                args = self._parse_call_args()
                return ("CALL_EXPR", ref, args)
            return ("VAR", ref)
        self.advance()
        return ("LIT", 0)


def parse_scl_block(block_name: str, scl_source: str) -> ParsedSclBlock:
    """Parse a Siemens STEP 7 Classic SCL block into declarations + AST statements."""
    block_type = "FC"
    m_hdr = re.search(r"^\s*(ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION)\s+", scl_source, re.IGNORECASE | re.MULTILINE)
    if m_hdr:
        ht = m_hdr.group(1).upper()
        block_type = "OB" if ht == "ORGANIZATION_BLOCK" else ("FB" if ht == "FUNCTION_BLOCK" else "FC")

    # Strip comments before splitting VAR / BEGIN / END_
    no_blk_comments = re.sub(r"\(\*[\s\S]*?\*\)", "", scl_source)
    lines = [ln.split("//", 1)[0] for ln in no_blk_comments.splitlines()]

    var_decls: Dict[str, Tuple[str, str, Any]] = {}
    body_lines: List[str] = []
    cur_var_sec: Optional[str] = None
    in_body = False
    saw_begin = False

    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        up = s.upper()
        if up in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR", "VAR_STAT"):
            cur_var_sec = up
            continue
        if up == "END_VAR":
            cur_var_sec = None
            continue
        if up == "BEGIN":
            in_body = True
            saw_begin = True
            continue
        if up.startswith(("END_ORGANIZATION_BLOCK", "END_FUNCTION_BLOCK", "END_FUNCTION")):
            in_body = False
            continue
        if cur_var_sec and not in_body:
            # e.g. Start : BOOL := FALSE;
            m_decl = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z0-9_]+)(?:\s*:=\s*(.+?))?\s*;?\s*$", s)
            if m_decl:
                vname = m_decl.group(1).upper()
                vtype = m_decl.group(2).upper()
                vinit = _parse_init_literal(m_decl.group(3), vtype) if m_decl.group(3) else _default_for_type(vtype)
                var_decls[vname] = (cur_var_sec, vtype, vinit)
            continue
        if in_body or (not saw_begin and not up.startswith(("ORGANIZATION_BLOCK", "FUNCTION", "TITLE", "VERSION", "AUTHOR", "FAMILY", "NAME", "KNOW_HOW_PROTECT"))):
            body_lines.append(s)

    tokens = _tokenize_scl("\n".join(body_lines))
    parser = _SclParser(tokens)
    stmts = parser.parse_stmts(())
    return ParsedSclBlock(
        block_name=block_name.strip().upper(),
        block_type=block_type,
        var_decls=var_decls,
        body_stmts=stmts,
    )


def _scl_builtin_call(fn_name: str, eval_args: List[Any]) -> Tuple[bool, Any]:
    fn = fn_name.strip().upper()
    if fn in ("INT_TO_REAL", "DINT_TO_REAL", "WORD_TO_REAL", "DWORD_TO_REAL", "REAL"):
        return True, float(eval_args[0]) if eval_args else 0.0
    if fn in ("REAL_TO_INT", "DINT_TO_INT", "WORD_TO_INT", "BOOL_TO_INT", "INT", "ROUND", "RND"):
        return True, int(round(float(eval_args[0]))) if eval_args else 0
    if fn in ("REAL_TO_DINT", "INT_TO_DINT", "DWORD_TO_DINT", "DINT"):
        return True, int(round(float(eval_args[0]))) if eval_args else 0
    if fn == "TRUNC":
        return True, int(math.trunc(float(eval_args[0]))) if eval_args else 0
    if fn == "ABS":
        return True, abs(eval_args[0]) if eval_args else 0
    if fn == "SQRT":
        v = float(eval_args[0]) if eval_args else 0.0
        return True, math.sqrt(v) if v >= 0.0 else 0.0
    if fn == "SQR":
        v = float(eval_args[0]) if eval_args else 0.0
        return True, v * v
    if fn == "MIN":
        return True, min(eval_args) if eval_args else 0
    if fn == "MAX":
        return True, max(eval_args) if eval_args else 0
    if fn == "LIMIT":
        if len(eval_args) >= 3:
            lo, val, hi = eval_args[0], eval_args[1], eval_args[2]
            return True, max(lo, min(hi, val))
        return True, 0
    if fn == "SEL":
        if len(eval_args) >= 3:
            return True, eval_args[2] if bool(eval_args[0]) else eval_args[1]
        return True, 0
    if fn == "BOOL_TO_WORD":
        return True, 1 if (eval_args and bool(eval_args[0])) else 0
    if fn == "INT_TO_BOOL":
        return True, bool(eval_args[0]) if eval_args else False
    return False, None


def execute_scl_block(
    parsed: ParsedSclBlock,
    read_ref: Callable[[str], Any],
    write_ref: Callable[[str, Any], None],
    static_store: Dict[str, Any],
    call_block_cb: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    param_bindings: Optional[Dict[str, Any]] = None,
    parse_time_ms: Optional[Callable[[str], int]] = None,
) -> Dict[str, Any]:
    """Execute a parsed SCL block against VirtualS7PLC memory + local/static variables."""
    local_env: Dict[str, Any] = {}
    out_names: List[str] = []
    inout_names: List[str] = []

    for vname, (sec, _dtype, init_val) in parsed.var_decls.items():
        if sec in ("VAR", "VAR_STAT"):
            if vname not in static_store:
                static_store[vname] = init_val
            local_env[vname] = static_store[vname]
        else:
            local_env[vname] = init_val
        if sec == "VAR_OUTPUT":
            out_names.append(vname)
        elif sec == "VAR_IN_OUT":
            inout_names.append(vname)

    # Apply incoming CALL parameter values
    for k, v in (param_bindings or {}).items():
        local_env[k.lstrip("#").upper()] = v

    # Allow assigning to block name as FC return value (e.g. FC1 := result;)
    ret_key = parsed.block_name.upper()
    if ret_key not in local_env:
        local_env[ret_key] = 0

    step_budget = [10000]

    def _get(ref: str) -> Any:
        key = ref.lstrip("#").upper()
        if key in local_env:
            return local_env[key]
        return read_ref(ref)

    def _set(ref: str, val: Any) -> None:
        key = ref.lstrip("#").upper()
        if key in local_env:
            local_env[key] = val
            if key in parsed.var_decls and parsed.var_decls[key][0] in ("VAR", "VAR_STAT"):
                static_store[key] = val
            return
        write_ref(ref, val)

    def _eval(node: Any) -> Any:
        kind = node[0]
        if kind == "LIT":
            return node[1]
        if kind == "TIME_LIT":
            return parse_time_ms(node[1]) if parse_time_ms else 0
        if kind == "VAR":
            return _get(node[1])
        if kind == "UNARY":
            op, sub = node[1], _eval(node[2])
            if op == "NOT":
                return not bool(sub)
            if op == "-":
                return -sub
            return sub
        if kind == "BIN":
            op = node[1]
            if op == "AND":
                return bool(_eval(node[2])) and bool(_eval(node[3]))
            if op == "OR":
                return bool(_eval(node[2])) or bool(_eval(node[3]))
            if op == "XOR":
                return bool(_eval(node[2])) ^ bool(_eval(node[3]))
            a, b = _eval(node[2]), _eval(node[3])
            if op == "=":
                return a == b
            if op == "<>":
                return a != b
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            if op == ">=":
                return a >= b
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                if b == 0:
                    return 0.0 if (isinstance(a, float) or isinstance(b, float)) else 0
                return (a / b) if (isinstance(a, float) or isinstance(b, float)) else int(a / b)
            if op == "MOD":
                return 0 if b == 0 else int(a) % int(b)
        if kind == "CALL_EXPR":
            fn_name, raw_args = node[1], node[2]
            eval_pos = [_eval(ex) for _, ex in raw_args]
            ok_bi, bi_val = _scl_builtin_call(fn_name, eval_pos)
            if ok_bi:
                return bi_val
            if call_block_cb:
                kw_args = {(k or f"ARG{i}").upper(): _eval(ex) for i, (k, ex) in enumerate(raw_args)}
                return call_block_cb(fn_name, kw_args)
        return 0

    def _exec_list(stmts: List[Any]) -> Optional[str]:
        for st in stmts:
            step_budget[0] -= 1
            if step_budget[0] <= 0:
                return "RETURN"
            sk = st[0]
            if sk == "NOP":
                continue
            if sk == "RETURN":
                return "RETURN"
            if sk == "EXIT":
                return "EXIT"
            if sk == "ASSIGN":
                _set(st[1], _eval(st[2]))
            elif sk == "CALL_STMT":
                fn_name, raw_args = st[1], st[2]
                if call_block_cb:
                    kw_args = {(k or f"ARG{i}").upper(): _eval(ex) for i, (k, ex) in enumerate(raw_args)}
                    call_block_cb(fn_name, kw_args)
            elif sk == "IF":
                _, cond, then_b, elifs, else_b = st
                if bool(_eval(cond)):
                    sig = _exec_list(then_b)
                    if sig:
                        return sig
                else:
                    matched = False
                    for ec, eb in elifs:
                        if bool(_eval(ec)):
                            matched = True
                            sig = _exec_list(eb)
                            if sig:
                                return sig
                            break
                    if not matched and else_b:
                        sig = _exec_list(else_b)
                        if sig:
                            return sig
            elif sk == "CASE":
                _, sel_expr, branches, else_b = st
                sel_val = int(_eval(sel_expr))
                matched = False
                for ranges, b_stmts in branches:
                    if any(lo <= sel_val <= hi for lo, hi in ranges):
                        matched = True
                        sig = _exec_list(b_stmts)
                        if sig:
                            return sig
                        break
                if not matched and else_b:
                    sig = _exec_list(else_b)
                    if sig:
                        return sig
            elif sk == "FOR":
                _, vname, start_e, end_e, step_e, body = st
                cur = int(_eval(start_e))
                end_v = int(_eval(end_e))
                step_v = int(_eval(step_e)) or 1
                iters = 0
                while (cur <= end_v if step_v > 0 else cur >= end_v) and iters < 2000:
                    iters += 1
                    _set(vname, cur)
                    sig = _exec_list(body)
                    if sig == "RETURN":
                        return "RETURN"
                    if sig == "EXIT":
                        break
                    cur += step_v
            elif sk == "WHILE":
                _, cond, body = st
                iters = 0
                while bool(_eval(cond)) and iters < 2000:
                    iters += 1
                    sig = _exec_list(body)
                    if sig == "RETURN":
                        return "RETURN"
                    if sig == "EXIT":
                        break
            elif sk == "REPEAT":
                _, body, cond = st
                iters = 0
                while iters < 2000:
                    iters += 1
                    sig = _exec_list(body)
                    if sig == "RETURN":
                        return "RETURN"
                    if sig == "EXIT":
                        break
                    if bool(_eval(cond)):
                        break
        return None

    _exec_list(parsed.body_stmts)

    # Persist any VAR/VAR_STAT back into static_store
    for vname, (sec, _, _) in parsed.var_decls.items():
        if sec in ("VAR", "VAR_STAT"):
            static_store[vname] = local_env[vname]

    outputs = {k: local_env[k] for k in out_names + inout_names if k in local_env}
    outputs["__RETURN__"] = local_env.get(ret_key, 0)
    return outputs


# ---------------------------------------------------------------------------
# S7-GRAPH (IEC 61131-3 SFC / .gr7) Deterministic State Machine Executor
# ---------------------------------------------------------------------------

@dataclass
class GraphStepDef:
    name: str
    initial: bool = False
    actions: List[Tuple[str, str]] = field(default_factory=list)  # (qualifier, operand)


@dataclass
class GraphTransitionDef:
    name: str
    from_steps: List[str]
    to_steps: List[str]
    condition: str


@dataclass
class ParsedGraphBlock:
    block_name: str
    steps: Dict[str, GraphStepDef] = field(default_factory=dict)
    transitions: List[GraphTransitionDef] = field(default_factory=list)
    initial_steps: List[str] = field(default_factory=list)


def parse_graph_block(block_name: str, gr7_source: str) -> ParsedGraphBlock:
    """Parse Siemens STEP 7 S7-GRAPH (`.gr7`) source into steps, actions, and transitions."""
    parsed = ParsedGraphBlock(block_name=block_name.strip().upper())

    # Parse INITIAL_STEP / STEP blocks
    step_re = re.compile(
        r"\b(INITIAL_STEP|STEP)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:([\s\S]*?)\bEND_STEP\b",
        re.IGNORECASE,
    )
    for m in step_re.finditer(gr7_source):
        is_init = m.group(1).upper() == "INITIAL_STEP"
        sname = m.group(2).strip()
        body = m.group(3)
        actions: List[Tuple[str, str]] = []
        for raw_ln in body.splitlines():
            ln = raw_ln.split("//", 1)[0].strip().rstrip(";")
            if not ln:
                continue
            # Formats: "N Q 0.0" or "N: Q 0.0" or "S Q0.1"
            m_act = re.match(r"^([A-Za-z]{1,2})\s*:?\s+(.+)$", ln)
            if m_act:
                q = m_act.group(1).upper()
                op = m_act.group(2).strip()
                if q in ("N", "S", "R", "D", "L", "P", "SD", "DS", "SL"):
                    actions.append((q, op))
        parsed.steps[sname] = GraphStepDef(name=sname, initial=is_init, actions=actions)
        if is_init:
            parsed.initial_steps.append(sname)

    # Parse TRANSITION blocks (supports both Siemens ':=' and ':' headers)
    tr_re = re.compile(
        r"\bTRANSITION\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*"
        r"FROM\s+\(?([^):=]+?)\)?\s*TO\s+\(?([^):=]+?)\)?\s*:=?([\s\S]*?)\bEND_TRANSITION\b",
        re.IGNORECASE,
    )
    for m in tr_re.finditer(gr7_source):
        tname = m.group(1).strip()
        froms = [s.strip() for s in m.group(2).split(",") if s.strip()]
        tos = [s.strip() for s in m.group(3).split(",") if s.strip()]
        cond_body = m.group(4)
        cond_lines: List[str] = []
        for raw_ln in cond_body.splitlines():
            ln = raw_ln.split("//", 1)[0].strip()
            if not ln or ln.upper() in ("CONDITION", "CONDITION:", "END_CONDITION"):
                continue
            ln = re.sub(r"^CONDITION\s*:\s*", "", ln, flags=re.IGNORECASE)
            ln = ln.rstrip(";").strip()
            if ln:
                cond_lines.append(ln)
        cond_str = " ".join(cond_lines).strip() or "TRUE"
        parsed.transitions.append(GraphTransitionDef(name=tname, from_steps=froms, to_steps=tos, condition=cond_str))

    return parsed


def _normalize_spaced_s7_addrs_in_expr(expr: str) -> str:
    """Compacts spaced S7 addresses inside Boolean expressions: 'I 0.0 AND NOT Q 0.1' -> 'I0.0 AND NOT Q0.1'."""
    out = re.sub(r"\b(DB\s*\d+\.(?:DBX|DBB|DBW|DBD))\s+(\d+(?:\.[0-7])?)\b", r"\1\2", expr, flags=re.IGNORECASE)
    out = re.sub(r"\bDB\s+(\d+)\.", r"DB\1.", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(P?[IEQAM][BWD]?)\s+(\d+(?:\.[0-7])?)\b", r"\1\2", out, flags=re.IGNORECASE)
    out = re.sub(r"\b([TCZ])\s+(\d+(?:\.Q)?)\b", r"\1\2", out, flags=re.IGNORECASE)
    return out


def _eval_graph_condition(cond: str, read_ref: Callable[[str], Any]) -> bool:
    """Evaluate a GRAPH transition condition (supports SCL Boolean syntax or AWL A/AN/O lines)."""
    c = cond.strip()
    if not c or c.upper() == "TRUE":
        return True
    if c.upper() == "FALSE":
        return False
    # If condition looks like AWL single-bit checks (e.g. "A I 0.0" or "AN I 0.1")
    m_awl = re.match(r"^(A|AN|U|UN)\s+(.+)$", c, re.IGNORECASE)
    if m_awl:
        op = m_awl.group(1).upper()
        val = bool(read_ref(m_awl.group(2).strip()))
        return (not val) if op in ("AN", "UN") else val
    # Normalize spaced S7 addresses (e.g. "I 0.0 AND NOT I 0.1" -> "I0.0 AND NOT I0.1")
    norm_c = _normalize_spaced_s7_addrs_in_expr(c)
    tokens = _tokenize_scl(norm_c)
    parser = _SclParser(tokens)
    expr = parser.parse_expr()
    out = execute_scl_block(
        ParsedSclBlock(block_name="__COND__", block_type="FC", body_stmts=[("ASSIGN", "__COND__", expr)]),
        read_ref=read_ref,
        write_ref=lambda _k, _v: None,
        static_store={},
    )
    return bool(out.get("__RETURN__", False))


def execute_graph_block(
    parsed: ParsedGraphBlock,
    sfc_state: Dict[str, Any],
    read_ref: Callable[[str], Any],
    write_ref: Callable[[str, Any], None],
    tick_ms: int = 100,
) -> Dict[str, Any]:
    """Execute one deterministic PLC scan of an S7-GRAPH SFC block."""
    if not sfc_state.get("initialized"):
        sfc_state["active_steps"] = list(parsed.initial_steps)
        sfc_state["prev_active_steps"] = []
        sfc_state["step_times_ms"] = {s: 0 for s in parsed.initial_steps}
        sfc_state["initialized"] = True

    active: List[str] = list(sfc_state["active_steps"])
    prev_active: List[str] = list(sfc_state["prev_active_steps"])
    step_times: Dict[str, int] = dict(sfc_state.get("step_times_ms", {}))

    # 1. Reset non-stored ('N') operands from steps that were active previously before re-evaluating active steps
    n_operands: set = set()
    for sdef in parsed.steps.values():
        for q, op in sdef.actions:
            if q == "N":
                n_operands.add(op)
    for op in n_operands:
        write_ref(op, False)

    # 2. Execute actions of currently active steps
    for sname in active:
        sdef = parsed.steps.get(sname)
        if not sdef:
            continue
        newly_entered = sname not in prev_active
        step_times[sname] = step_times.get(sname, 0) + max(0, int(tick_ms))
        for q, op in sdef.actions:
            if q == "N":
                write_ref(op, True)
            elif q == "S":
                write_ref(op, True)
            elif q == "R":
                write_ref(op, False)
            elif q == "P" and newly_entered:
                write_ref(op, True)

    # 3. Evaluate transitions from active steps (at most one transition firing per active path per scan)
    next_active = list(active)
    fired: List[str] = []
    for tr in parsed.transitions:
        if all(fs in active for fs in tr.from_steps):
            if _eval_graph_condition(tr.condition, read_ref):
                for fs in tr.from_steps:
                    if fs in next_active:
                        next_active.remove(fs)
                    step_times.pop(fs, None)
                for ts in tr.to_steps:
                    if ts not in next_active:
                        next_active.append(ts)
                        step_times[ts] = 0
                fired.append(tr.name)
                break

    sfc_state["prev_active_steps"] = active
    sfc_state["active_steps"] = next_active
    sfc_state["step_times_ms"] = step_times
    return {
        "active_steps": next_active,
        "fired_transitions": fired,
    }


__all__ = [
    "ParsedSclBlock",
    "parse_scl_block",
    "execute_scl_block",
    "GraphStepDef",
    "GraphTransitionDef",
    "ParsedGraphBlock",
    "parse_graph_block",
    "execute_graph_block",
]
