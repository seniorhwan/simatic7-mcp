"""Authentic Siemens S7-300 System/Standard Library Blocks & Closed-Loop Plant Simulator.

Provides deterministic implementations of the standard Siemens STEP 7 Classic (S7-300/400)
system and library blocks that real industrial PLC programs depend on:
  - IEC Timers:       SFB 3 (TP), SFB 4 (TON), SFB 5 (TOF)
  - IEC Counters:     SFB 0 (CTU), SFB 1 (CTD), SFB 2 (CTUD)
  - TI-S7 Analog:     FC 105 (SCALE), FC 106 (UNSCALE)
  - Continuous PID:   FB 41 (CONT_C)
  - Memory / System:  SFC 20 (BLKMOV), SFC 21 (FILL), SFC 64 (TIME_TCK)

Also provides a deterministic closed-loop physical plant simulator (`run_closed_loop_plant_sim`)
so AI agents can verify PID tuning, tank levels, thermal ovens, conveyors, interlocks, and
disturbance rejection across hundreds of scan cycles with quantitative control metrics.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Time Literal & ANY-Pointer Helpers
# ---------------------------------------------------------------------------

def parse_iec_time_ms(val: Any) -> int:
    """Parse IEC TIME / S5TIME literal (e.g. 'T#5S', 'T#1S500MS', 'S5T#2M', 2500) into integer ms."""
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(round(val))
    s = str(val or "").strip().upper()
    if not s:
        return 0
    for prefix in ("S5TIME#", "S5T#", "TIME#", "T#"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    sign = -1 if s.startswith("-") else 1
    if s.startswith(("+", "-")):
        s = s[1:]
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return sign * int(round(float(s)))
    total_ms = 0.0
    matched = False
    for num_s, unit in re.findall(r"(\d+(?:\.\d+)?)(MS|D|H|M|S)", s):
        matched = True
        v = float(num_s)
        if unit == "MS":
            total_ms += v
        elif unit == "S":
            total_ms += v * 1000.0
        elif unit == "M":
            total_ms += v * 60_000.0
        elif unit == "H":
            total_ms += v * 3_600_000.0
        elif unit == "D":
            total_ms += v * 86_400_000.0
    return sign * int(round(total_ms)) if matched else 0


def parse_any_pointer(ptr_str: str) -> Optional[Dict[str, Any]]:
    """Parse a Siemens ANY pointer: 'P#DB10.DBX0.0 BYTE 16', 'P#M 10.0 WORD 4', 'P#I0.0 BYTE 2'."""
    s = str(ptr_str or "").strip()
    m = re.match(
        r"^P#\s*(?:DB\s*(\d+)\.\s*DBX\s*(\d+)\.([0-7])|([IEQM])\s*(\d+)\.([0-7]))\s+"
        r"(BOOL|BYTE|CHAR|WORD|INT|DWORD|DINT|REAL)\s+(\d+)$",
        s,
        re.IGNORECASE,
    )
    if not m:
        return None
    type_sizes = {
        "BOOL": 1, "BYTE": 1, "CHAR": 1,
        "WORD": 2, "INT": 2,
        "DWORD": 4, "DINT": 4, "REAL": 4,
    }
    dtype = m.group(7).upper()
    count = int(m.group(8))
    byte_len = count if dtype == "BOOL" else count * type_sizes[dtype]
    if m.group(1) is not None:
        return {
            "area": "DB",
            "area_code": 0x84,
            "db_number": int(m.group(1)),
            "byte_offset": int(m.group(2)),
            "bit_offset": int(m.group(3)),
            "data_type": dtype,
            "count": count,
            "byte_length": byte_len,
        }
    area_raw = m.group(4).upper()
    area = "I" if area_raw == "E" else area_raw
    area_codes = {"I": 0x81, "Q": 0x82, "M": 0x83}
    return {
        "area": area,
        "area_code": area_codes[area],
        "db_number": 0,
        "byte_offset": int(m.group(5)),
        "bit_offset": int(m.group(6)),
        "data_type": dtype,
        "count": count,
        "byte_length": byte_len,
    }


# ---------------------------------------------------------------------------
# Standard Library Parameter Schemas & Symbolic Aliases
# ---------------------------------------------------------------------------

S7_STDLIB_ALIASES: Dict[str, str] = {
    "CTU": "SFB0",
    "S_CU_IEC": "SFB0",
    "CTD": "SFB1",
    "CTUD": "SFB2",
    "TP": "SFB3",
    "TON": "SFB4",
    "TOF": "SFB5",
    "SCALE": "FC105",
    "UNSCALE": "FC106",
    "CONT_C": "FB41",
    "PID": "FB41",
    "BLKMOV": "SFC20",
    "FILL": "SFC21",
    "TIME_TCK": "SFC64",
}

# Mapping: CANONICAL_BLOCK -> {PARAM_UPPER: (section, s7_data_type)}
S7_STDLIB_SCHEMAS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "SFB0": {
        "CU": ("VAR_INPUT", "BOOL"),
        "R": ("VAR_INPUT", "BOOL"),
        "PV": ("VAR_INPUT", "INT"),
        "Q": ("VAR_OUTPUT", "BOOL"),
        "CV": ("VAR_OUTPUT", "INT"),
    },
    "SFB1": {
        "CD": ("VAR_INPUT", "BOOL"),
        "LD": ("VAR_INPUT", "BOOL"),
        "PV": ("VAR_INPUT", "INT"),
        "Q": ("VAR_OUTPUT", "BOOL"),
        "CV": ("VAR_OUTPUT", "INT"),
    },
    "SFB2": {
        "CU": ("VAR_INPUT", "BOOL"),
        "CD": ("VAR_INPUT", "BOOL"),
        "R": ("VAR_INPUT", "BOOL"),
        "LD": ("VAR_INPUT", "BOOL"),
        "PV": ("VAR_INPUT", "INT"),
        "QU": ("VAR_OUTPUT", "BOOL"),
        "QD": ("VAR_OUTPUT", "BOOL"),
        "CV": ("VAR_OUTPUT", "INT"),
    },
    "SFB3": {
        "IN": ("VAR_INPUT", "BOOL"),
        "PT": ("VAR_INPUT", "TIME"),
        "Q": ("VAR_OUTPUT", "BOOL"),
        "ET": ("VAR_OUTPUT", "TIME"),
    },
    "SFB4": {
        "IN": ("VAR_INPUT", "BOOL"),
        "PT": ("VAR_INPUT", "TIME"),
        "Q": ("VAR_OUTPUT", "BOOL"),
        "ET": ("VAR_OUTPUT", "TIME"),
    },
    "SFB5": {
        "IN": ("VAR_INPUT", "BOOL"),
        "PT": ("VAR_INPUT", "TIME"),
        "Q": ("VAR_OUTPUT", "BOOL"),
        "ET": ("VAR_OUTPUT", "TIME"),
    },
    "FC105": {
        "IN": ("VAR_INPUT", "INT"),
        "HI_LIM": ("VAR_INPUT", "REAL"),
        "LO_LIM": ("VAR_INPUT", "REAL"),
        "BIPOLAR": ("VAR_INPUT", "BOOL"),
        "RET_VAL": ("VAR_OUTPUT", "WORD"),
        "OUT": ("VAR_OUTPUT", "REAL"),
    },
    "FC106": {
        "IN": ("VAR_INPUT", "REAL"),
        "HI_LIM": ("VAR_INPUT", "REAL"),
        "LO_LIM": ("VAR_INPUT", "REAL"),
        "BIPOLAR": ("VAR_INPUT", "BOOL"),
        "RET_VAL": ("VAR_OUTPUT", "WORD"),
        "OUT": ("VAR_OUTPUT", "INT"),
    },
    "FB41": {
        "COM_RST": ("VAR_INPUT", "BOOL"),
        "MAN_ON": ("VAR_INPUT", "BOOL"),
        "PVPER_ON": ("VAR_INPUT", "BOOL"),
        "P_SEL": ("VAR_INPUT", "BOOL"),
        "I_SEL": ("VAR_INPUT", "BOOL"),
        "INT_HOLD": ("VAR_INPUT", "BOOL"),
        "I_ITL_ON": ("VAR_INPUT", "BOOL"),
        "D_SEL": ("VAR_INPUT", "BOOL"),
        "CYCLE": ("VAR_INPUT", "TIME"),
        "SP_INT": ("VAR_INPUT", "REAL"),
        "PV_IN": ("VAR_INPUT", "REAL"),
        "PV_PER": ("VAR_INPUT", "INT"),
        "MAN": ("VAR_INPUT", "REAL"),
        "GAIN": ("VAR_INPUT", "REAL"),
        "TI": ("VAR_INPUT", "TIME"),
        "TD": ("VAR_INPUT", "TIME"),
        "TM_LAG": ("VAR_INPUT", "TIME"),
        "DEADB_W": ("VAR_INPUT", "REAL"),
        "LMN_HLM": ("VAR_INPUT", "REAL"),
        "LMN_LLM": ("VAR_INPUT", "REAL"),
        "PV_FAC": ("VAR_INPUT", "REAL"),
        "PV_OFF": ("VAR_INPUT", "REAL"),
        "LMN_FAC": ("VAR_INPUT", "REAL"),
        "LMN_OFF": ("VAR_INPUT", "REAL"),
        "I_ITLVAL": ("VAR_INPUT", "REAL"),
        "DISV": ("VAR_INPUT", "REAL"),
        "LMN": ("VAR_OUTPUT", "REAL"),
        "LMN_PER": ("VAR_OUTPUT", "INT"),
        "QLMN_HLM": ("VAR_OUTPUT", "BOOL"),
        "QLMN_LLM": ("VAR_OUTPUT", "BOOL"),
        "LMN_P": ("VAR_OUTPUT", "REAL"),
        "LMN_I": ("VAR_OUTPUT", "REAL"),
        "LMN_D": ("VAR_OUTPUT", "REAL"),
        "PV": ("VAR_OUTPUT", "REAL"),
        "ER": ("VAR_OUTPUT", "REAL"),
    },
    "SFC20": {
        "SRCBLK": ("VAR_INPUT", "ANY"),
        "RET_VAL": ("VAR_OUTPUT", "INT"),
        "DSTBLK": ("VAR_OUTPUT", "ANY"),
    },
    "SFC21": {
        "BVAL": ("VAR_INPUT", "ANY"),
        "RET_VAL": ("VAR_OUTPUT", "INT"),
        "BLK": ("VAR_OUTPUT", "ANY"),
    },
    "SFC64": {
        "RET_VAL": ("VAR_OUTPUT", "TIME"),
    },
}


def resolve_stdlib_block_name(name: str) -> Optional[str]:
    """Resolve 'SFB 4', 'SFB4', 'TON', 'FC 105', 'SCALE', 'FB 41', 'CONT_C', etc. to canonical stdlib key."""
    clean = re.sub(r"\s+", "", str(name or "").strip().strip('"').upper())
    if clean in S7_STDLIB_SCHEMAS:
        return clean
    return S7_STDLIB_ALIASES.get(clean)


def get_stdlib_schema(name: str) -> Optional[Dict[str, Tuple[str, str]]]:
    canon = resolve_stdlib_block_name(name)
    return S7_STDLIB_SCHEMAS.get(canon) if canon else None


def _ensure_instance_db_layout(plc: Any, canon_blk: str, instance_db: Optional[int]) -> None:
    """Register authentic S7-300 field layout on the instance DB so DB100.Q / DB41.LMN work seamlessly."""
    if not instance_db or instance_db <= 0:
        return
    if instance_db in plc.db_layouts and plc.db_layouts[instance_db]:
        return
    from .iec_languages import compute_s7_struct_layout

    schema = S7_STDLIB_SCHEMAS.get(canon_blk, {})
    fields = [
        {"name": pname, "type": ("DINT" if ptype == "TIME" else ("WORD" if ptype == "ANY" else ptype))}
        for pname, (_, ptype) in schema.items()
    ]
    layout_res = compute_s7_struct_layout(fields, db_number=instance_db)
    plc._ensure_db(instance_db, max(4096, int(layout_res.get("total_bytes", 64))))
    fmap: Dict[str, Tuple[str, str]] = {}
    for item in layout_res.get("layout", []):
        fname = item["name"].upper()
        orig_type = schema.get(fname, ("VAR_INPUT", item["type"]))[1]
        ftype = "DINT" if orig_type == "TIME" else item["type"].upper()
        b_off = item["byte_offset"]
        bit_off = item["bit_offset"]
        if ftype == "BOOL":
            addr = f"DB{instance_db}.DBX{b_off}.{bit_off}"
        elif item["size_bytes"] == 1:
            addr = f"DB{instance_db}.DBB{b_off}"
        elif item["size_bytes"] == 2:
            addr = f"DB{instance_db}.DBW{b_off}"
        else:
            addr = f"DB{instance_db}.DBD{b_off}"
        fmap[fname] = (addr, ftype)
    plc.db_layouts[instance_db] = fmap


def _sync_instance_db(plc: Any, instance_db: Optional[int], values: Dict[str, Any]) -> None:
    if not instance_db or instance_db not in plc.db_layouts:
        return
    fmap = plc.db_layouts[instance_db]
    for k, v in values.items():
        ku = k.upper()
        if ku in fmap:
            addr, ftype = fmap[ku]
            try:
                plc.write_address(addr, v, data_type=ftype)
            except Exception:
                pass


def execute_stdlib_block(
    plc: Any,
    block_name: str,
    params: Dict[str, Any],
    *,
    raw_param_exprs: Optional[Dict[str, str]] = None,
    instance_db: Optional[int] = None,
    tick_ms: int = 100,
) -> Dict[str, Any]:
    """Execute a built-in Siemens S7-300 system/standard library block deterministically."""
    canon = resolve_stdlib_block_name(block_name)
    if not canon:
        return {}

    p_up = {k.upper(): v for k, v in (params or {}).items()}
    raw_up = {k.upper(): v for k, v in (raw_param_exprs or {}).items()}
    inst_key = f"{canon}:DB{instance_db}" if instance_db else canon
    state = plc.fb_statics.setdefault(inst_key, {})

    if instance_db:
        _ensure_instance_db_layout(plc, canon, instance_db)

    # --- IEC TIMERS: SFB3 (TP), SFB4 (TON), SFB5 (TOF) ---
    if canon == "SFB4":  # TON (On-Delay Timer)
        inp = bool(p_up.get("IN", False))
        pt = max(0, parse_iec_time_ms(p_up.get("PT", state.get("PT", 0))))
        et = int(state.get("ET", 0))
        q = bool(state.get("Q", False))
        prev_in = bool(state.get("prev_in", False))
        if not inp:
            et = 0
            q = False
        else:
            if not prev_in:
                et = 0
            et = min(pt, et + max(0, int(tick_ms)))
            q = et >= pt
        state.update({"IN": inp, "PT": pt, "ET": et, "Q": q, "prev_in": inp})
        out = {"IN": inp, "PT": pt, "Q": q, "ET": et}
        _sync_instance_db(plc, instance_db, out)
        return out

    if canon == "SFB5":  # TOF (Off-Delay Timer)
        inp = bool(p_up.get("IN", False))
        pt = max(0, parse_iec_time_ms(p_up.get("PT", state.get("PT", 0))))
        et = int(state.get("ET", 0))
        q = bool(state.get("Q", False))
        timing = bool(state.get("timing", False))
        prev_in = bool(state.get("prev_in", False))
        if inp:
            q = True
            et = 0
            timing = False
        else:
            if prev_in:
                timing = pt > 0
                q = pt > 0
                et = 0
            if timing:
                et = min(pt, et + max(0, int(tick_ms)))
                if et >= pt:
                    q = False
                    timing = False
                else:
                    q = True
            else:
                q = False
        state.update({"IN": inp, "PT": pt, "ET": et, "Q": q, "timing": timing, "prev_in": inp})
        out = {"IN": inp, "PT": pt, "Q": q, "ET": et}
        _sync_instance_db(plc, instance_db, out)
        return out

    if canon == "SFB3":  # TP (Pulse Timer)
        inp = bool(p_up.get("IN", False))
        pt = max(0, parse_iec_time_ms(p_up.get("PT", state.get("PT", 0))))
        et = int(state.get("ET", 0))
        q = bool(state.get("Q", False))
        running = bool(state.get("running", False))
        prev_in = bool(state.get("prev_in", False))
        if (inp and not prev_in) and not running:
            if pt > 0:
                running = True
                q = True
                et = 0
        if running:
            et = min(pt, et + max(0, int(tick_ms)))
            if et >= pt:
                running = False
                q = False
            else:
                q = True
        elif not inp:
            et = 0
            q = False
        state.update({"IN": inp, "PT": pt, "ET": et, "Q": q, "running": running, "prev_in": inp})
        out = {"IN": inp, "PT": pt, "Q": q, "ET": et}
        _sync_instance_db(plc, instance_db, out)
        return out

    # --- IEC COUNTERS: SFB0 (CTU), SFB1 (CTD), SFB2 (CTUD) ---
    if canon == "SFB0":  # CTU
        cu = bool(p_up.get("CU", False))
        rst = bool(p_up.get("R", False))
        pv = int(p_up.get("PV", state.get("PV", 0)))
        cv = int(state.get("CV", 0))
        prev_cu = bool(state.get("prev_cu", False))
        if rst:
            cv = 0
        elif cu and not prev_cu and cv < 32767:
            cv += 1
        q = cv >= pv
        state.update({"CU": cu, "R": rst, "PV": pv, "CV": cv, "Q": q, "prev_cu": cu})
        out = {"CU": cu, "R": rst, "PV": pv, "Q": q, "CV": cv}
        _sync_instance_db(plc, instance_db, out)
        return out

    if canon == "SFB1":  # CTD
        cd = bool(p_up.get("CD", False))
        ld = bool(p_up.get("LD", False))
        pv = int(p_up.get("PV", state.get("PV", 0)))
        cv = int(state.get("CV", 0))
        prev_cd = bool(state.get("prev_cd", False))
        if ld:
            cv = pv
        elif cd and not prev_cd and cv > -32768:
            cv -= 1
        q = cv <= 0
        state.update({"CD": cd, "LD": ld, "PV": pv, "CV": cv, "Q": q, "prev_cd": cd})
        out = {"CD": cd, "LD": ld, "PV": pv, "Q": q, "CV": cv}
        _sync_instance_db(plc, instance_db, out)
        return out

    if canon == "SFB2":  # CTUD
        cu = bool(p_up.get("CU", False))
        cd = bool(p_up.get("CD", False))
        rst = bool(p_up.get("R", False))
        ld = bool(p_up.get("LD", False))
        pv = int(p_up.get("PV", state.get("PV", 0)))
        cv = int(state.get("CV", 0))
        prev_cu = bool(state.get("prev_cu", False))
        prev_cd = bool(state.get("prev_cd", False))
        if rst:
            cv = 0
        elif ld:
            cv = pv
        else:
            if cu and not prev_cu and cv < 32767:
                cv += 1
            if cd and not prev_cd and cv > -32768:
                cv -= 1
        qu = cv >= pv
        qd = cv <= 0
        state.update({"CV": cv, "PV": pv, "QU": qu, "QD": qd, "prev_cu": cu, "prev_cd": cd})
        out = {"CU": cu, "CD": cd, "R": rst, "LD": ld, "PV": pv, "QU": qu, "QD": qd, "CV": cv}
        _sync_instance_db(plc, instance_db, out)
        return out

    # --- TI-S7 ANALOG SCALING: FC105 (SCALE) & FC106 (UNSCALE) ---
    if canon == "FC105":  # SCALE
        raw_in = int(p_up.get("IN", 0))
        if raw_in >= 32768:
            raw_in -= 65536
        hi_lim = float(p_up.get("HI_LIM", 100.0))
        lo_lim = float(p_up.get("LO_LIM", 0.0))
        bipolar = bool(p_up.get("BIPOLAR", False))
        k1 = -27648.0 if bipolar else 0.0
        k2 = 27648.0
        if raw_in > k2:
            ret_val = 0x0008
            out_val = hi_lim
        elif raw_in < k1:
            ret_val = 0x0008
            out_val = lo_lim
        else:
            ret_val = 0x0000
            out_val = ((float(raw_in) - k1) / (k2 - k1)) * (hi_lim - lo_lim) + lo_lim
        return {"RET_VAL": ret_val, "OUT": out_val, "__RETURN__": ret_val}

    if canon == "FC106":  # UNSCALE
        eng_in = float(p_up.get("IN", 0.0))
        hi_lim = float(p_up.get("HI_LIM", 100.0))
        lo_lim = float(p_up.get("LO_LIM", 0.0))
        bipolar = bool(p_up.get("BIPOLAR", False))
        k1 = -27648.0 if bipolar else 0.0
        k2 = 27648.0
        if hi_lim == lo_lim:
            return {"RET_VAL": 0x0008, "OUT": 0, "__RETURN__": 0x0008}
        low_b, high_b = (lo_lim, hi_lim) if lo_lim <= hi_lim else (hi_lim, lo_lim)
        if eng_in > high_b:
            ret_val = 0x0008
            out_int = int(k2 if hi_lim >= lo_lim else k1)
        elif eng_in < low_b:
            ret_val = 0x0008
            out_int = int(k1 if hi_lim >= lo_lim else k2)
        else:
            ret_val = 0x0000
            out_int = int(round(((eng_in - lo_lim) / (hi_lim - lo_lim)) * (k2 - k1) + k1))
        return {"RET_VAL": ret_val, "OUT": out_int, "__RETURN__": ret_val}

    # --- SIEMENS STANDARD CONTINUOUS PID CONTROLLER: FB41 (CONT_C) ---
    if canon == "FB41":
        com_rst = bool(p_up.get("COM_RST", False))
        man_on = bool(p_up.get("MAN_ON", state.get("MAN_ON", False)))
        pvper_on = bool(p_up.get("PVPER_ON", state.get("PVPER_ON", False)))
        p_sel = bool(p_up.get("P_SEL", state.get("P_SEL", True)))
        i_sel = bool(p_up.get("I_SEL", state.get("I_SEL", True)))
        int_hold = bool(p_up.get("INT_HOLD", False))
        i_itl_on = bool(p_up.get("I_ITL_ON", False))
        d_sel = bool(p_up.get("D_SEL", state.get("D_SEL", False)))

        cycle_ms = parse_iec_time_ms(p_up.get("CYCLE", state.get("CYCLE", tick_ms or 100)))
        dt_s = max(0.001, (cycle_ms if cycle_ms > 0 else max(1, tick_ms)) / 1000.0)

        sp_int = float(p_up.get("SP_INT", state.get("SP_INT", 0.0)))
        pv_in = float(p_up.get("PV_IN", state.get("PV_IN", 0.0)))
        pv_per_raw = int(p_up.get("PV_PER", state.get("PV_PER", 0)))
        if pv_per_raw >= 32768:
            pv_per_raw -= 65536
        man = float(p_up.get("MAN", state.get("MAN", 0.0)))
        gain = float(p_up.get("GAIN", state.get("GAIN", 2.0)))
        ti_ms = parse_iec_time_ms(p_up.get("TI", state.get("TI", 20000)))
        td_ms = parse_iec_time_ms(p_up.get("TD", state.get("TD", 10000)))
        tm_lag_ms = parse_iec_time_ms(p_up.get("TM_LAG", state.get("TM_LAG", 2000)))
        deadb_w = max(0.0, float(p_up.get("DEADB_W", state.get("DEADB_W", 0.0))))
        lmn_hlm = float(p_up.get("LMN_HLM", state.get("LMN_HLM", 100.0)))
        lmn_llm = float(p_up.get("LMN_LLM", state.get("LMN_LLM", 0.0)))
        pv_fac = float(p_up.get("PV_FAC", state.get("PV_FAC", 1.0)))
        pv_off = float(p_up.get("PV_OFF", state.get("PV_OFF", 0.0)))
        lmn_fac = float(p_up.get("LMN_FAC", state.get("LMN_FAC", 1.0)))
        lmn_off = float(p_up.get("LMN_OFF", state.get("LMN_OFF", 0.0)))
        i_itlval = float(p_up.get("I_ITLVAL", 0.0))
        disv = float(p_up.get("DISV", 0.0))

        # Process value conditioning (PV_PER scales 0..27648 -> 0..100.0%)
        pv_base = (pv_per_raw / 276.48) if pvper_on else pv_in
        pv = pv_base * pv_fac + pv_off

        if com_rst:
            lmn_p = 0.0
            lmn_i = i_itlval if i_itl_on else 0.0
            lmn_d = 0.0
            er = 0.0
            lmn_raw = lmn_i
            state["prev_er"] = 0.0
            state["lmn_i"] = lmn_i
            state["lmn_d"] = 0.0
        else:
            raw_err = sp_int - pv
            if abs(raw_err) <= deadb_w:
                er = 0.0
            elif raw_err > 0:
                er = raw_err - deadb_w
            else:
                er = raw_err + deadb_w

            prev_er = float(state.get("prev_er", er))
            lmn_i = float(state.get("lmn_i", 0.0))
            prev_d = float(state.get("lmn_d", 0.0))

            # P-term
            lmn_p = (gain * er) if p_sel else 0.0

            # D-term (filtered derivative with TM_LAG)
            if d_sel and td_ms > 0:
                td_s = td_ms / 1000.0
                tlag_s = max(dt_s * 0.5, tm_lag_ms / 1000.0)
                alpha = tlag_s / (tlag_s + dt_s)
                lmn_d = alpha * prev_d + (1.0 - alpha) * gain * td_s * ((er - prev_er) / dt_s)
            else:
                lmn_d = 0.0

            # I-term with bumpless manual tracking & anti-windup
            if man_on:
                lmn_raw = man
                if i_sel:
                    lmn_i = man - lmn_p - disv
            else:
                if i_itl_on:
                    lmn_i = i_itlval
                elif i_sel and not int_hold and ti_ms > 0:
                    ti_s = ti_ms / 1000.0
                    # Anti-windup: integrate unless already saturated in same direction
                    proposed_i = lmn_i + (gain * er * dt_s) / ti_s
                    proposed_sum = lmn_p + proposed_i + lmn_d + disv
                    if proposed_sum > lmn_hlm and er > 0:
                        lmn_i = max(lmn_i, lmn_hlm - lmn_p - lmn_d - disv)
                    elif proposed_sum < lmn_llm and er < 0:
                        lmn_i = min(lmn_i, lmn_llm - lmn_p - lmn_d - disv)
                    else:
                        lmn_i = proposed_i
                elif not i_sel:
                    lmn_i = 0.0
                lmn_raw = lmn_p + lmn_i + lmn_d + disv

            state["prev_er"] = er
            state["lmn_i"] = lmn_i
            state["lmn_d"] = lmn_d

        # Output clamping
        qlmn_hlm = lmn_raw >= lmn_hlm
        qlmn_llm = lmn_raw <= lmn_llm
        lmn_clamped = max(lmn_llm, min(lmn_hlm, lmn_raw))
        lmn = lmn_clamped * lmn_fac + lmn_off
        lmn_per = int(round(max(-32768.0, min(32767.0, lmn * 276.48))))

        state.update({
            "MAN_ON": man_on, "PVPER_ON": pvper_on, "P_SEL": p_sel, "I_SEL": i_sel, "D_SEL": d_sel,
            "CYCLE": cycle_ms, "SP_INT": sp_int, "PV_IN": pv_in, "PV_PER": pv_per_raw,
            "MAN": man, "GAIN": gain, "TI": ti_ms, "TD": td_ms, "TM_LAG": tm_lag_ms,
            "DEADB_W": deadb_w, "LMN_HLM": lmn_hlm, "LMN_LLM": lmn_llm,
            "PV_FAC": pv_fac, "PV_OFF": pv_off, "LMN_FAC": lmn_fac, "LMN_OFF": lmn_off,
        })
        out = {
            "SP_INT": sp_int,
            "PV_IN": pv_in,
            "PV": pv,
            "ER": er,
            "LMN": lmn,
            "LMN_PER": lmn_per,
            "QLMN_HLM": qlmn_hlm,
            "QLMN_LLM": qlmn_llm,
            "LMN_P": lmn_p,
            "LMN_I": lmn_i,
            "LMN_D": lmn_d,
        }
        _sync_instance_db(plc, instance_db, out)
        return out

    # --- SFC20 (BLKMOV) & SFC21 (FILL) ---
    if canon == "SFC20":
        src_p = parse_any_pointer(raw_up.get("SRCBLK") or str(p_up.get("SRCBLK", "")))
        dst_p = parse_any_pointer(raw_up.get("DSTBLK") or str(p_up.get("DSTBLK", "")))
        if not src_p or not dst_p:
            return {"RET_VAL": -32477, "__RETURN__": -32477}
        nbytes = min(src_p["byte_length"], dst_p["byte_length"])
        data = plc.read_bytes(src_p["area_code"], src_p["db_number"], src_p["byte_offset"], nbytes)
        plc.write_bytes(dst_p["area_code"], dst_p["db_number"], dst_p["byte_offset"], data)
        return {"RET_VAL": 0, "__RETURN__": 0}

    if canon == "SFC21":
        src_p = parse_any_pointer(raw_up.get("BVAL") or str(p_up.get("BVAL", "")))
        dst_p = parse_any_pointer(raw_up.get("BLK") or str(p_up.get("BLK", "")))
        if not dst_p:
            return {"RET_VAL": -32477, "__RETURN__": -32477}
        if src_p:
            pattern = plc.read_bytes(src_p["area_code"], src_p["db_number"], src_p["byte_offset"], max(1, src_p["byte_length"]))
        else:
            b = int(p_up.get("BVAL", 0)) & 0xFF
            pattern = bytes([b])
        reps = (dst_p["byte_length"] + len(pattern) - 1) // len(pattern)
        payload = (pattern * reps)[: dst_p["byte_length"]]
        plc.write_bytes(dst_p["area_code"], dst_p["db_number"], dst_p["byte_offset"], payload)
        return {"RET_VAL": 0, "__RETURN__": 0}

    # --- SFC64 (TIME_TCK) ---
    if canon == "SFC64":
        t_ms = int(plc.sim_time_ms) & 0x7FFFFFFF
        return {"RET_VAL": t_ms, "__RETURN__": t_ms}

    return {}


# ---------------------------------------------------------------------------
# Closed-Loop Physical Plant / Process Simulator for VirtualS7PLC
# ---------------------------------------------------------------------------

def _clamp(val: float, lo: Optional[float], hi: Optional[float]) -> float:
    if lo is not None and val < lo:
        val = lo
    if hi is not None and val > hi:
        val = hi
    return val


def run_closed_loop_plant_sim(
    plc: Any,
    *,
    duration_ms: int = 10_000,
    tick_ms: int = 100,
    entry_block: str = "OB1",
    initial_plc_inputs: Optional[Dict[str, Any]] = None,
    plant_states: Optional[List[Dict[str, Any]]] = None,
    sensor_links: Optional[List[Dict[str, Any]]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    watch: Optional[List[str]] = None,
    assertions: Optional[List[Dict[str, Any]]] = None,
    sample_every_scans: int = 5,
    reset_memory: bool = False,
) -> Dict[str, Any]:
    """Run a deterministic closed-loop PLC + physical plant simulation.

    Each `plant_states` entry models one physical variable (e.g. tank level, oven temp, motor speed):
      {
        "name": "level_pct",
        "initial": 10.0,
        "min": 0.0,
        "max": 100.0,
        "type": "integrator" | "first_order",   # default "integrator"
        "ambient": 0.0,                         # equilibrium for first_order (e.g. 20.0 C)
        "tau_s": 10.0,                          # time constant for first_order
        "bias_rate": -2.0,                      # constant drain/loss per second
        "actuators": [
          {"address": "PQW256", "data_type": "INT", "in_min": 0.0, "in_max": 27648.0, "gain": 12.0},
          {"address": "Q0.0", "gain": 5.0},     # digital actuator: +5.0 units/s when True
          {"address": "Q0.1", "gain": -8.0}     # drain valve: -8.0 units/s when True
        ]
      }

    Each `sensor_links` entry maps a physical state back into a PLC input every scan:
      - Analog:  {"state": "level_pct", "address": "PIW256", "kind": "analog",
                  "eng_min": 0.0, "eng_max": 100.0, "raw_min": 0, "raw_max": 27648}
      - Real:    {"state": "level_pct", "address": "MD100", "kind": "real"}
      - Digital: {"state": "level_pct", "address": "I0.2", "kind": "high_switch", "threshold": 85.0}
                 {"state": "level_pct", "address": "I0.3", "kind": "low_switch", "threshold": 15.0}

    Each `events` entry injects timed operator actions or plant disturbances:
      {"at_ms": 5000, "inputs": {"I0.0": True}, "disturbances": {"level_pct": -15.0}, "state_bias": {"level_pct": -4.0}}

    Each `assertions` entry checks quantitative control-engineering properties:
      {"state": "level_pct", "final_near": 50.0, "tolerance": 2.0}
      {"state": "level_pct", "max_le": 65.0, "min_ge": 5.0}
      {"state": "level_pct", "settle_target": 50.0, "settle_band": 2.5, "settle_within_ms": 8000}
      {"address": "Q0.7", "never_true": True}
      {"address": "Q0.0", "final_equals": True}
    """
    if reset_memory:
        plc.reset(clear_blocks=False, clear_symbols=False)

    tick_ms = max(1, int(tick_ms))
    total_scans = max(1, int(math.ceil(max(1, int(duration_ms)) / tick_ms)))
    dt_s = tick_ms / 1000.0

    # Initialize physical state variables
    p_defs: Dict[str, Dict[str, Any]] = {}
    p_vals: Dict[str, float] = {}
    p_bias_override: Dict[str, float] = {}
    p_hist: Dict[str, List[Tuple[int, float]]] = {}
    for st in plant_states or []:
        sname = str(st["name"])
        init_v = float(st.get("initial", 0.0))
        p_defs[sname] = dict(st)
        p_vals[sname] = init_v
        p_bias_override[sname] = float(st.get("bias_rate", 0.0))
        p_hist[sname] = [(0, init_v)]

    # Apply initial PLC inputs before scan 0
    for addr, val in (initial_plc_inputs or {}).items():
        plc.write_address(addr, val)

    def _apply_sensors() -> None:
        for link in sensor_links or []:
            sname = str(link["state"])
            if sname not in p_vals:
                continue
            v = p_vals[sname]
            addr = str(link["address"])
            kind = str(link.get("kind", "analog")).lower()
            if kind == "analog":
                eng_min = float(link.get("eng_min", 0.0))
                eng_max = float(link.get("eng_max", 100.0))
                raw_min = float(link.get("raw_min", 0.0))
                raw_max = float(link.get("raw_max", 27648.0))
                frac = (v - eng_min) / (eng_max - eng_min) if eng_max != eng_min else 0.0
                raw_val = int(round(_clamp(raw_min + frac * (raw_max - raw_min), -32768.0, 32767.0)))
                plc.write_address(addr, raw_val, data_type="INT")
            elif kind == "real":
                plc.write_address(addr, float(v), data_type="REAL")
            elif kind == "high_switch":
                thr = float(link.get("threshold", 80.0))
                invert = bool(link.get("normally_closed", False))
                tripped = v >= thr
                plc.write_address(addr, (not tripped) if invert else tripped, data_type="BOOL")
            elif kind == "low_switch":
                thr = float(link.get("threshold", 20.0))
                invert = bool(link.get("normally_closed", False))
                tripped = v <= thr
                plc.write_address(addr, (not tripped) if invert else tripped, data_type="BOOL")

    # Track watched PLC signals & assertions
    watch_addrs = list(watch or [])
    assert_addrs: set = set()
    for a in assertions or []:
        if "address" in a:
            assert_addrs.add(str(a["address"]))
    addr_ever_true: Dict[str, bool] = {a: False for a in assert_addrs}
    addr_last_val: Dict[str, Any] = {}

    pending_events = sorted(list(events or []), key=lambda e: int(e.get("at_ms", 0)))
    ev_idx = 0
    trace: List[Dict[str, Any]] = []
    start_time_ms = plc.sim_time_ms

    # Write initial sensors so scan 1 sees initial physical state
    _apply_sensors()

    for scan_i in range(1, total_scans + 1):
        elapsed_ms = scan_i * tick_ms

        # Trigger scheduled events at elapsed_ms
        while ev_idx < len(pending_events) and int(pending_events[ev_idx].get("at_ms", 0)) <= elapsed_ms:
            ev = pending_events[ev_idx]
            ev_idx += 1
            for addr, val in (ev.get("inputs") or {}).items():
                plc.write_address(addr, val)
            for sname, delta in (ev.get("disturbances") or {}).items():
                if sname in p_vals:
                    sd = p_defs[sname]
                    p_vals[sname] = _clamp(p_vals[sname] + float(delta), sd.get("min"), sd.get("max"))
            for sname, new_bias in (ev.get("state_bias") or {}).items():
                if sname in p_bias_override:
                    p_bias_override[sname] = float(new_bias)

        # Update sensors -> run 1 PLC scan -> advance plant physics
        _apply_sensors()
        plc.step(cycles=1, tick_ms=tick_ms, entry_block=entry_block)

        # Integrate physical plant states based on PLC actuator outputs
        for sname, sd in p_defs.items():
            drive = p_bias_override[sname]
            for act in sd.get("actuators") or []:
                addr = str(act["address"])
                dt_hint = act.get("data_type")
                raw_out = plc.read_address(addr, data_type=dt_hint)
                gain = float(act.get("gain", 1.0))
                if isinstance(raw_out, bool):
                    u = 1.0 if raw_out else 0.0
                else:
                    in_min = float(act.get("in_min", 0.0))
                    in_max = float(act.get("in_max", 1.0))
                    u = (float(raw_out) - in_min) / (in_max - in_min) if in_max != in_min else float(raw_out)
                drive += u * gain

            model_type = str(sd.get("type", "integrator")).lower()
            cur = p_vals[sname]
            if model_type == "first_order":
                ambient = float(sd.get("ambient", 0.0))
                tau_s = max(0.01, float(sd.get("tau_s", 5.0)))
                # dx/dt = (ambient + drive - x) / tau_s
                nxt = cur + ((ambient + drive - cur) / tau_s) * dt_s
            else:
                nxt = cur + drive * dt_s
            nxt = _clamp(nxt, sd.get("min"), sd.get("max"))
            p_vals[sname] = nxt
            p_hist[sname].append((elapsed_ms, nxt))

        # Track assertion addresses
        for a in assert_addrs:
            val = plc.read_address(a)
            addr_last_val[a] = val
            if bool(val):
                addr_ever_true[a] = True

        # Record downsampled trace
        if scan_i == 1 or scan_i == total_scans or (sample_every_scans > 0 and scan_i % sample_every_scans == 0):
            sample: Dict[str, Any] = {
                "t_ms": elapsed_ms,
                "states": {k: round(v, 4) for k, v in p_vals.items()},
            }
            if watch_addrs:
                w_snap: Dict[str, Any] = {}
                for w in watch_addrs:
                    if ":" in w:
                        wa, wt = w.rsplit(":", 1)
                        w_snap[w] = plc.read_address(wa.strip(), data_type=wt.strip().upper())
                    else:
                        w_snap[w] = plc.read_address(w)
                sample["watch"] = w_snap
            trace.append(sample)

    # Compute engineering summary per state
    state_metrics: Dict[str, Dict[str, Any]] = {}
    for sname, pts in p_hist.items():
        vals = [v for _, v in pts]
        state_metrics[sname] = {
            "initial": round(vals[0], 4),
            "final": round(vals[-1], 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
        }

    # Evaluate quantitative control assertions
    assertion_results: List[Dict[str, Any]] = []
    all_passed = True
    for idx, rule in enumerate(assertions or [], start=1):
        passed = True
        failures: List[str] = []
        if "state" in rule:
            sname = str(rule["state"])
            pts = p_hist.get(sname, [])
            if not pts:
                passed = False
                failures.append(f"Unknown plant state '{sname}'")
            else:
                vals = [v for _, v in pts]
                final_v = vals[-1]
                if "final_near" in rule:
                    target = float(rule["final_near"])
                    tol = float(rule.get("tolerance", 2.0))
                    if abs(final_v - target) > tol:
                        passed = False
                        failures.append(f"final={final_v:.3f} outside {target} +/- {tol}")
                if "max_le" in rule and max(vals) > float(rule["max_le"]):
                    passed = False
                    failures.append(f"max={max(vals):.3f} > {rule['max_le']}")
                if "min_ge" in rule and min(vals) < float(rule["min_ge"]):
                    passed = False
                    failures.append(f"min={min(vals):.3f} < {rule['min_ge']}")
                if "settle_target" in rule:
                    target = float(rule["settle_target"])
                    band = float(rule.get("settle_band", 2.5))
                    max_settle_ms = int(rule.get("settle_within_ms", duration_ms))
                    # Last timestamp outside the settling band
                    last_out_ms = 0
                    for t_ms, v in pts:
                        if abs(v - target) > band:
                            last_out_ms = t_ms
                    state_metrics[sname]["settling_time_ms"] = last_out_ms
                    state_metrics[sname]["overshoot"] = round(max(0.0, max(vals) - target), 4)
                    if last_out_ms > max_settle_ms:
                        passed = False
                        failures.append(f"settling_time={last_out_ms}ms > {max_settle_ms}ms (band +/-{band})")
                if "overshoot_le" in rule and "settle_target" in rule:
                    ov = max(0.0, max(vals) - float(rule["settle_target"]))
                    if ov > float(rule["overshoot_le"]):
                        passed = False
                        failures.append(f"overshoot={ov:.3f} > {rule['overshoot_le']}")
        elif "address" in rule:
            addr = str(rule["address"])
            if rule.get("never_true") and addr_ever_true.get(addr, False):
                passed = False
                failures.append(f"{addr} became True during simulation")
            if "final_equals" in rule:
                actual = addr_last_val.get(addr)
                expected = rule["final_equals"]
                if isinstance(expected, float):
                    if actual is None or abs(float(actual) - expected) > float(rule.get("tolerance", 1e-2)):
                        passed = False
                        failures.append(f"final {addr}={actual} != {expected}")
                elif actual != expected:
                    passed = False
                    failures.append(f"final {addr}={actual} != {expected}")
        if not passed:
            all_passed = False
        assertion_results.append({
            "index": idx,
            "rule": rule,
            "passed": passed,
            "failures": failures,
        })

    return {
        "ok": all_passed,
        "success": all_passed,
        "duration_ms": total_scans * tick_ms,
        "executed_scans": total_scans,
        "sim_time_ms": plc.sim_time_ms,
        "elapsed_sim_ms": plc.sim_time_ms - start_time_ms,
        "ob35_executions": getattr(plc, "ob35_count", 0),
        "state_metrics": state_metrics,
        "assertions": assertion_results,
        "unsupported_instructions": list(getattr(plc, "last_unsupported_instructions", [])),
        "trace": trace,
    }


__all__ = [
    "S7_STDLIB_ALIASES",
    "S7_STDLIB_SCHEMAS",
    "parse_iec_time_ms",
    "parse_any_pointer",
    "resolve_stdlib_block_name",
    "get_stdlib_schema",
    "execute_stdlib_block",
    "run_closed_loop_plant_sim",
]
