"""
Siemens S7-300/S7-400 MC7 Block Binary Engine & STEP 7 Classic Offline Extractor.

Provides pure-Python, zero-dependency tools for real physical S7-300/S7-400 PLCs:
  - Authentic Siemens S7 MC7 block binary container parser & builder (36-byte header
    starting with 0x70 0x70 + MC7/DB body + SSB/ADD sections + 36-byte footer with 0x65).
  - Pure-Python Global Data Block (DB) compiler producing 100% authentic, directly
    downloadable S7-300/S7-400 MC7 DB images from field declarations or DATA_BLOCK source.
  - Pure-Python dBase III+ reader for Siemens STEP 7 Classic V5.x offline databases
    (``ombstx/offline/*/SUBBLK.DBF`` + ``SUBBLK.DBT``), extracting exact Siemens-compiled
    MC7 binary blocks (OB, FC, FB, DB, SDB) ready for direct S7comm TCP port 102 download.
"""

from __future__ import annotations

import datetime
import re
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .iec_languages import compute_s7_struct_layout


# ---------------------------------------------------------------------------
# S7 Block Type & Language Mappings
# ---------------------------------------------------------------------------

# MC7 header subblk_type byte (offset 5) & SUBBLK.DBF SUBBLKTYP values
S7_MC7_TYPE_TO_BYTE: Dict[str, int] = {
    "OB": 0x08,
    "DB": 0x0A,
    "SDB": 0x0B,
    "FC": 0x0C,
    "SFC": 0x0D,
    "FB": 0x0E,
    "SFB": 0x0F,
}
S7_MC7_BYTE_TO_TYPE: Dict[int, str] = {v: k for k, v in S7_MC7_TYPE_TO_BYTE.items()}

# 2-char ASCII hex block type used in S7comm filenames (_0800001P, _0A00010A) and PI-services
S7_COMM_BLOCK_TYPE_HEX: Dict[str, str] = {
    "OB": "08",
    "DB": "0A",
    "SDB": "0B",
    "FC": "0C",
    "SFC": "0D",
    "FB": "0E",
    "SFB": "0F",
}
S7_COMM_HEX_TO_BLOCK_TYPE: Dict[str, str] = {v: k for k, v in S7_COMM_BLOCK_TYPE_HEX.items()}

# S7 MC7 header block language codes (offset 4)
S7_MC7_LANG_TO_BYTE: Dict[str, int] = {
    "AWL": 0x01,
    "STL": 0x01,
    "KOP": 0x02,
    "LAD": 0x02,
    "FUP": 0x03,
    "FBD": 0x03,
    "SCL": 0x04,
    "DB": 0x05,
    "GRAPH": 0x06,
    "S7-GRAPH": 0x06,
    "SDB": 0x07,
    "CPU-DB": 0x0A,
    "INSTANCE_DB": 0x0A,
}
S7_MC7_BYTE_TO_LANG: Dict[int, str] = {
    0x01: "STL",
    0x02: "LAD",
    0x03: "FBD",
    0x04: "SCL",
    0x05: "DB",
    0x06: "GRAPH",
    0x07: "SDB",
    0x0A: "INSTANCE_DB",
}

S7_EPOCH = datetime.date(1984, 1, 1)
MC7_HEADER_SIZE = 36
MC7_FOOTER_SIZE = 36

# Universally valid S7-300/400 Block End (BE) MC7 instruction
MC7_OP_BE = b"\x65\x00"


# ---------------------------------------------------------------------------
# Block Identifier & Timestamp Helpers
# ---------------------------------------------------------------------------

def parse_block_identifier(block_id: str) -> Tuple[str, int]:
    """Parse a Siemens block identifier like 'OB1', 'FC 10', 'DB100', 'SDB0' -> ('OB', 1)."""
    text = (block_id or "").strip().upper()
    m = re.match(r"^(OB|DB|SDB|FC|SFC|FB|SFB)\s*(\d+)$", text)
    if not m:
        raise ValueError(
            f"Invalid Siemens S7 block identifier {block_id!r}. "
            "Expected OB/FC/FB/DB/SDB/SFC/SFB followed by a number (e.g. 'OB1', 'FC10', 'DB1')."
        )
    b_type = m.group(1)
    b_num = int(m.group(2))
    if b_num < 0 or b_num > 65535:
        raise ValueError(f"Block number out of range (0..65535): {b_num}")
    return b_type, b_num


def encode_s7_timestamp(dt: Optional[datetime.datetime] = None) -> Tuple[int, int]:
    """Encode a datetime into Siemens S7 (ms_since_midnight, days_since_1984_01_01)."""
    now = dt or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    days = max(0, (now.date() - S7_EPOCH).days) & 0xFFFF
    ms = (
        now.hour * 3_600_000
        + now.minute * 60_000
        + now.second * 1_000
        + now.microsecond // 1_000
    )
    return ms, days


def decode_s7_timestamp(ms: int, days: int) -> str:
    """Decode Siemens S7 (ms_since_midnight, days_since_1984_01_01) to ISO-8601 string."""
    try:
        base = datetime.datetime(1984, 1, 1) + datetime.timedelta(days=int(days), milliseconds=int(ms))
        return base.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return "1984-01-01T00:00:00"


def _encode_bcd_version(version: str) -> int:
    """Encode 'major.minor' (e.g. '0.1', '1.2') into S7 single-byte version nibble."""
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", str(version or "0.1"))
    if not m:
        return 0x01
    maj = min(15, int(m.group(1)))
    mn = min(15, int(m.group(2)))
    return ((maj & 0x0F) << 4) | (mn & 0x0F)


def _decode_bcd_version(v_byte: int) -> str:
    return f"{(v_byte >> 4) & 0x0F}.{v_byte & 0x0F}"


def compute_mc7_checksum(payload: bytes) -> int:
    """Compute CRC-16/XMODEM checksum over MC7 body bytes (standard 16-bit integrity value)."""
    crc = 0x0000
    for b in payload:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Authentic S7-300/400 MC7 Block Container Parser & Builder
# ---------------------------------------------------------------------------

def parse_mc7_block(raw: bytes) -> Dict[str, Any]:
    """
    Parse an authentic Siemens S7-300/S7-400 network MC7 block binary image
    (36-byte header starting with 0x70 0x70 + MC7/DB body + SSB + ADD + 36-byte footer).
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("MC7 block image must be bytes")
    buf = bytes(raw)
    if len(buf) < MC7_HEADER_SIZE + MC7_FOOTER_SIZE:
        raise ValueError(
            f"MC7 block image too short ({len(buf)} bytes); minimum is {MC7_HEADER_SIZE + MC7_FOOTER_SIZE} bytes."
        )
    if buf[0:2] != b"\x70\x70":
        raise ValueError(
            f"Invalid Siemens S7 MC7 header signature: expected 0x7070, got 0x{buf[0:2].hex()}."
        )

    fmt_version = buf[2]
    blk_flags = buf[3]
    blk_lang_byte = buf[4]
    subblk_byte = buf[5]
    block_number = struct.unpack(">H", buf[6:8])[0]
    load_size = struct.unpack(">I", buf[8:12])[0]
    blk_security = struct.unpack(">I", buf[12:16])[0]
    code_ms, code_days = struct.unpack(">IH", buf[16:22])
    intf_ms, intf_days = struct.unpack(">IH", buf[22:28])
    ssb_len, add_len, local_data_len, mc7_size = struct.unpack(">HHHH", buf[28:36])

    block_type = S7_MC7_BYTE_TO_TYPE.get(subblk_byte)
    if block_type is None:
        raise ValueError(f"Unsupported S7 MC7 subblock type byte: 0x{subblk_byte:02X}")

    expected_min_end = MC7_HEADER_SIZE + mc7_size + ssb_len + add_len + MC7_FOOTER_SIZE
    if len(buf) < expected_min_end:
        raise ValueError(
            f"Truncated MC7 block {block_type}{block_number}: buffer has {len(buf)} bytes, "
            f"header declares {expected_min_end} bytes (mc7={mc7_size}, ssb={ssb_len}, add={add_len})."
        )

    body_start = MC7_HEADER_SIZE
    mc7_body = buf[body_start:body_start + mc7_size]
    ssb_start = body_start + mc7_size
    ssb_body = buf[ssb_start:ssb_start + ssb_len]
    add_start = ssb_start + ssb_len
    add_body = buf[add_start:add_start + add_len]
    footer = buf[len(buf) - MC7_FOOTER_SIZE:]

    author = footer[2:10].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    family = footer[10:18].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    name = footer[18:26].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    version_str = _decode_bcd_version(footer[26])
    checksum = struct.unpack(">H", footer[28:30])[0]

    return {
        "valid": True,
        "block_type": block_type,
        "block_number": block_number,
        "block_name": f"{block_type}{block_number}",
        "format_version": fmt_version,
        "flags": blk_flags,
        "language_code": blk_lang_byte,
        "language": S7_MC7_BYTE_TO_LANG.get(blk_lang_byte, f"LANG_0x{blk_lang_byte:02X}"),
        "load_size": load_size,
        "actual_size": len(buf),
        "mc7_size": mc7_size,
        "ssb_length": ssb_len,
        "add_length": add_len,
        "local_data_length": local_data_len,
        "security": blk_security,
        "code_timestamp": decode_s7_timestamp(code_ms, code_days),
        "interface_timestamp": decode_s7_timestamp(intf_ms, intf_days),
        "author": author,
        "family": family,
        "symbolic_name": name,
        "version": version_str,
        "checksum": f"0x{checksum:04X}",
        "mc7_body_hex": mc7_body.hex(),
        "has_ssb": bool(ssb_body),
        "has_add": bool(add_body),
    }


def build_mc7_block(
    block_type: str,
    block_number: int,
    mc7_body: bytes,
    language: Optional[str] = None,
    local_data_len: Optional[int] = None,
    ssb_bytes: bytes = b"",
    add_bytes: bytes = b"",
    author: str = "S7MCP",
    family: str = "STEP7",
    symbolic_name: str = "",
    version: str = "0.1",
    flags: int = 0x01,
    security: int = 0x00000000,
    checksum: Optional[int] = None,
    code_ts: Optional[Tuple[int, int]] = None,
    intf_ts: Optional[Tuple[int, int]] = None,
) -> bytes:
    """
    Build an authentic Siemens S7-300/S7-400 downloadable MC7 block container
    (36-byte header + MC7/DB body + SSB + ADD + 36-byte footer).
    """
    bt = block_type.strip().upper()
    if bt not in S7_MC7_TYPE_TO_BYTE:
        raise ValueError(f"Unsupported S7 block type: {block_type!r}")
    bnum = int(block_number)
    if bnum < 0 or bnum > 65535:
        raise ValueError(f"Block number out of range (0..65535): {bnum}")

    body = bytes(mc7_body)
    ssb = bytes(ssb_bytes)
    add = bytes(add_bytes)

    # S7-300/400 requires 2-byte aligned MC7/DB payloads
    if len(body) % 2 == 1:
        body += b"\x00"

    if language is None:
        if bt == "DB":
            lang_byte = 0x05
        elif bt == "SDB":
            lang_byte = 0x07
        else:
            lang_byte = 0x01  # STL/AWL
    else:
        lang_byte = S7_MC7_LANG_TO_BYTE.get(language.strip().upper(), 0x01)

    if local_data_len is None:
        # OB1 / cyclic OBs on S7-300/400 require 20 bytes of system TEMP data
        loc_len = 20 if bt == "OB" else 0
    else:
        loc_len = max(20, int(local_data_len)) if bt == "OB" else max(0, int(local_data_len))

    mc7_size = len(body)
    ssb_len = len(ssb)
    add_len = len(add)
    load_size = MC7_HEADER_SIZE + mc7_size + ssb_len + add_len + MC7_FOOTER_SIZE

    c_ms, c_days = code_ts if code_ts is not None else encode_s7_timestamp()
    i_ms, i_days = intf_ts if intf_ts is not None else (c_ms, c_days)

    # Build 36-byte header with exact Siemens S7-300/400 big-endian offsets:
    # 0..1: 70 70 | 2: ver | 3: flags | 4: lang | 5: subblk | 6..7: num
    # 8..11: load_size | 12..15: security
    # 16..19: code_ms | 20..21: code_days | 22..25: intf_ms | 26..27: intf_days
    # 28..29: ssb_len | 30..31: add_len | 32..33: local_data_len | 34..35: mc7_size
    header = struct.pack(
        ">2sBBBBHII I H I H HHHH",
        b"\x70\x70",
        0x01,
        flags & 0xFF,
        lang_byte & 0xFF,
        S7_MC7_TYPE_TO_BYTE[bt],
        bnum & 0xFFFF,
        load_size & 0xFFFFFFFF,
        security & 0xFFFFFFFF,
        c_ms & 0xFFFFFFFF,
        c_days & 0xFFFF,
        i_ms & 0xFFFFFFFF,
        i_days & 0xFFFF,
        ssb_len & 0xFFFF,
        add_len & 0xFFFF,
        loc_len & 0xFFFF,
        mc7_size & 0xFFFF,
    )
    assert len(header) == MC7_HEADER_SIZE

    def _pad8(val: str) -> bytes:
        return (val or "").encode("ascii", errors="ignore")[:8].ljust(8, b"\x00")

    csum = compute_mc7_checksum(body) if checksum is None else (int(checksum) & 0xFFFF)
    ver_byte = _encode_bcd_version(version)
    footer = (
        b"\x65\x00"
        + _pad8(author)
        + _pad8(family)
        + _pad8(symbolic_name or f"{bt}{bnum}")
        + bytes([ver_byte, 0x00])
        + struct.pack(">H", csum)
        + bytes(6)
    )
    assert len(footer) == MC7_FOOTER_SIZE
    return header + body + ssb + add + footer


# ---------------------------------------------------------------------------
# Pure-Python Global Data Block (DB) MC7 Compiler
# ---------------------------------------------------------------------------

def _encode_db_field_value(data_type: str, value: Any, byte_size: int) -> bytes:
    """Encode an initial value for a Global DB field in Siemens S7 big-endian layout."""
    dt = data_type.strip().upper()
    if value is None:
        return bytes(byte_size)
    if dt == "BOOL":
        return bytes([1 if bool(value) else 0])
    if dt in ("BYTE", "CHAR"):
        if isinstance(value, str) and len(value) == 1:
            return bytes([ord(value) & 0xFF])
        return bytes([int(value) & 0xFF])
    if dt == "INT":
        return struct.pack(">h", int(value))
    if dt in ("WORD", "S5TIME", "DATE"):
        if isinstance(value, str):
            s = value.strip().upper()
            if s.startswith("W#16#"):
                return struct.pack(">H", int(s[5:], 16) & 0xFFFF)
            if s.startswith("16#"):
                return struct.pack(">H", int(s[3:], 16) & 0xFFFF)
        return struct.pack(">H", int(value) & 0xFFFF)
    if dt in ("DINT", "TIME"):
        if isinstance(value, str) and value.strip().upper().startswith("T#"):
            from .s7_stdlib import parse_iec_time_ms
            return struct.pack(">i", int(parse_iec_time_ms(value)))
        return struct.pack(">i", int(value))
    if dt in ("DWORD", "TIME_OF_DAY", "TOD"):
        if isinstance(value, str):
            s = value.strip().upper()
            if s.startswith("DW#16#"):
                return struct.pack(">I", int(s[6:], 16) & 0xFFFFFFFF)
            if s.startswith("16#"):
                return struct.pack(">I", int(s[3:], 16) & 0xFFFFFFFF)
        return struct.pack(">I", int(value) & 0xFFFFFFFF)
    if dt == "REAL":
        return struct.pack(">f", float(value))
    if dt.startswith("STRING"):
        m = re.match(r"^STRING(?:\[(\d+)\])?$", dt)
        max_len = min(254, int(m.group(1))) if (m and m.group(1)) else 254
        raw_s = str(value).strip("'").encode("latin-1", errors="replace")[:max_len]
        buf = bytearray(max_len + 2)
        buf[0] = max_len
        buf[1] = len(raw_s)
        buf[2:2 + len(raw_s)] = raw_s
        return bytes(buf[:byte_size])
    return bytes(byte_size)


def _parse_data_block_source(source: str) -> Tuple[int, List[Dict[str, Any]], Dict[str, Any]]:
    """Parse STEP 7 Classic DATA_BLOCK AWL/STL source into (db_number, fields, initial_assignments)."""
    m_db = re.search(r"DATA_BLOCK\s+DB\s*(\d+)", source, re.IGNORECASE)
    if not m_db:
        raise ValueError("Source does not contain a 'DATA_BLOCK DB <number>' header.")
    db_num = int(m_db.group(1))

    m_struct = re.search(r"\bSTRUCT\b(.*?)\bEND_STRUCT\b", source, re.IGNORECASE | re.DOTALL)
    fields: List[Dict[str, Any]] = []
    if m_struct:
        for raw_line in m_struct.group(1).splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if not line:
                continue
            fm = re.match(
                r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z0-9_\[\]\.\s]+?)(?:\s*:=\s*(.+?))?\s*;\s*$",
                line,
            )
            if fm:
                fname = fm.group(1).strip()
                ftype = fm.group(2).strip().upper()
                finit = fm.group(3).strip() if fm.group(3) else None
                fields.append({"name": fname, "type": ftype, "initial": finit})

    assignments: Dict[str, Any] = {}
    m_begin = re.search(r"\bBEGIN\b(.*?)\bEND_DATA_BLOCK\b", source, re.IGNORECASE | re.DOTALL)
    if m_begin:
        for raw_line in m_begin.group(1).splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if not line:
                continue
            am = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(.+?)\s*;\s*$", line)
            if am:
                assignments[am.group(1).strip()] = am.group(2).strip()

    return db_num, fields, assignments


def _coerce_literal(raw_val: Any, data_type: str) -> Any:
    if raw_val is None or not isinstance(raw_val, str):
        return raw_val
    s = raw_val.strip()
    dt = data_type.strip().upper()
    if dt == "BOOL":
        return s.upper() in ("TRUE", "1", "ON")
    if dt == "REAL":
        return float(s)
    if dt in ("INT", "DINT", "BYTE", "WORD", "DWORD"):
        if s.upper().startswith(("W#16#", "DW#16#", "B#16#", "16#")):
            return s
        return int(float(s))
    return s


def build_db_mc7_block(
    db_number: int,
    fields: Optional[Sequence[Dict[str, Any]]] = None,
    raw_data: Optional[bytes] = None,
    byte_size: Optional[int] = None,
    db_source: Optional[str] = None,
    author: str = "S7MCP",
    family: str = "STEP7",
    symbolic_name: str = "",
    version: str = "0.1",
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Compile a Siemens S7-300/S7-400 Global Data Block (DB) directly into a 100% authentic,
    immediately downloadable MC7 binary image.

    Accepts any of:
      - ``db_source`` (STEP 7 DATA_BLOCK ... STRUCT ... BEGIN ... END_DATA_BLOCK text)
      - ``fields`` (list of ``{name, type, initial}`` dicts using authentic STEP 7 alignment)
      - ``raw_data`` (raw big-endian bytes) or ``byte_size`` (zero-initialized DB of N bytes)
    """
    parsed_fields: List[Dict[str, Any]] = list(fields or [])
    assignments: Dict[str, Any] = {}
    if db_source:
        parsed_db_num, src_fields, assignments = _parse_data_block_source(db_source)
        if db_number <= 0:
            db_number = parsed_db_num
        if not parsed_fields:
            parsed_fields = src_fields

    layout_info: Optional[Dict[str, Any]] = None
    if raw_data is not None:
        buf = bytearray(raw_data)
        if len(buf) % 2 == 1:
            buf.append(0x00)
        if len(buf) == 0:
            buf.extend(b"\x00\x00")
    elif parsed_fields:
        layout_info = compute_s7_struct_layout(parsed_fields)
        total_bytes = max(2, int(layout_info["total_bytes"]))
        if total_bytes % 2 == 1:
            total_bytes += 1
        buf = bytearray(total_bytes)
        for item in layout_info["layout"]:
            fname = item["name"]
            ftype = item["type"]
            b_off = int(item["byte_offset"])
            bit_off = int(item["bit_offset"])
            fsize = int(item["size_bytes"])
            raw_init = assignments.get(fname, item.get("initial"))
            if raw_init is None:
                continue
            val = _coerce_literal(raw_init, ftype)
            if ftype.upper() == "BOOL":
                if bool(val):
                    buf[b_off] |= (1 << bit_off)
                else:
                    buf[b_off] &= ~(1 << bit_off)
            else:
                encoded = _encode_db_field_value(ftype, val, fsize)
                buf[b_off:b_off + len(encoded)] = encoded
    else:
        sz = max(2, int(byte_size or 64))
        if sz % 2 == 1:
            sz += 1
        buf = bytearray(sz)

    mc7_image = build_mc7_block(
        block_type="DB",
        block_number=db_number,
        mc7_body=bytes(buf),
        language="DB",
        local_data_len=0,
        author=author,
        family=family,
        symbolic_name=symbolic_name or f"DB{db_number}",
        version=version,
    )
    meta = parse_mc7_block(mc7_image)
    meta["data_bytes_length"] = len(buf)
    if layout_info is not None:
        meta["layout"] = layout_info
    return mc7_image, meta


# ---------------------------------------------------------------------------
# Pure-Python dBase III+ Reader for STEP 7 Classic Offline Projects
# (ombstx/offline/*/SUBBLK.DBF + SUBBLK.DBT)
# ---------------------------------------------------------------------------

def read_dbf_records(
    dbf_path: Path,
    dbt_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Read records from a dBase III+ ``.DBF`` table (with optional ``.DBT`` 512-byte
    memo file) in pure Python — zero external dependencies.

    Handles both ASCII text memos (terminated by 0x1A 0x1A) and Siemens STEP 7 binary
    memos (where ``SUBBLK.DBT`` stores raw MC7 bytecode/SSB/ADD blobs).
    """
    dbf_bytes = Path(dbf_path).read_bytes()
    if len(dbf_bytes) < 32:
        raise ValueError(f"DBF file too small ({len(dbf_bytes)} bytes): {dbf_path}")

    num_records = struct.unpack("<I", dbf_bytes[4:8])[0]
    header_len = struct.unpack("<H", dbf_bytes[8:10])[0]
    record_len = struct.unpack("<H", dbf_bytes[10:12])[0]

    fields: List[Tuple[str, str, int]] = []
    pos = 32
    while pos + 32 <= header_len and dbf_bytes[pos] != 0x0D:
        f_name = dbf_bytes[pos:pos + 11].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip().upper()
        f_type = chr(dbf_bytes[pos + 11]).upper()
        f_len = dbf_bytes[pos + 16]
        fields.append((f_name, f_type, f_len))
        pos += 32

    dbt_bytes: Optional[bytes] = None
    dbt_block_size = 512
    if dbt_path is None:
        cand_dbt = Path(dbf_path).with_suffix(".DBT")
        if not cand_dbt.exists():
            cand_dbt = Path(dbf_path).with_suffix(".dbt")
        if cand_dbt.exists():
            dbt_path = cand_dbt
    if dbt_path is not None and Path(dbt_path).exists():
        dbt_bytes = Path(dbt_path).read_bytes()
        if len(dbt_bytes) >= 22:
            # dBase IV stores block size at offset 20..21 (little-endian); dBase III+ uses 512
            blk_sz_cand = struct.unpack("<H", dbt_bytes[20:22])[0]
            if blk_sz_cand in (512, 1024, 2048, 4096):
                dbt_block_size = blk_sz_cand

    def _read_memo_raw(block_idx: int) -> bytes:
        if dbt_bytes is None or block_idx <= 0:
            return b""
        offset = block_idx * dbt_block_size
        if offset >= len(dbt_bytes):
            return b""
        # dBase IV memo header signature: 0xFF 0xFF 0x08 0x00 + uint32 total_len (includes 8B hdr)
        if offset + 8 <= len(dbt_bytes) and dbt_bytes[offset:offset + 4] == b"\xff\xff\x08\x00":
            total_len = struct.unpack("<I", dbt_bytes[offset + 4:offset + 8])[0]
            payload_len = max(0, total_len - 8)
            return dbt_bytes[offset + 8:offset + 8 + payload_len]
        # dBase III+ memo: starts directly at offset; raw bytes up to EOF/next-block boundary
        # Keep raw slice starting at offset; callers slice exact MC5LEN / SSBLEN / ADDLEN!
        return dbt_bytes[offset:]

    records: List[Dict[str, Any]] = []
    rec_pos = header_len
    for _ in range(num_records):
        if rec_pos + record_len > len(dbf_bytes):
            break
        rec_raw = dbf_bytes[rec_pos:rec_pos + record_len]
        rec_pos += record_len
        if not rec_raw or rec_raw[0] == 0x2A:  # '*' deleted record
            continue
        row: Dict[str, Any] = {}
        f_off = 1
        for f_name, f_type, f_len in fields:
            raw_val = rec_raw[f_off:f_off + f_len]
            f_off += f_len
            if f_type == "M":
                # Memo pointer: either ASCII decimal block number (10B) or 4-byte LE int
                if f_len == 4:
                    blk_idx = struct.unpack("<I", raw_val)[0]
                else:
                    s_ptr = raw_val.decode("ascii", errors="ignore").strip()
                    blk_idx = int(s_ptr) if s_ptr.isdigit() else 0
                row[f_name] = _read_memo_raw(blk_idx)
                row[f"{f_name}_BLOCK"] = blk_idx
            elif f_type in ("N", "F"):
                s_num = raw_val.decode("ascii", errors="ignore").strip()
                if not s_num:
                    row[f_name] = 0
                else:
                    try:
                        row[f_name] = float(s_num) if "." in s_num else int(s_num)
                    except ValueError:
                        row[f_name] = 0
            elif f_type == "I":
                row[f_name] = struct.unpack("<i", raw_val[:4])[0] if len(raw_val) >= 4 else 0
            elif f_type == "L":
                row[f_name] = raw_val[:1].upper() in (b"T", b"Y")
            else:
                row[f_name] = raw_val.decode("latin-1", errors="ignore").strip("\x00 ")
        records.append(row)

    return records


def _find_subblk_databases(project_path: Path) -> List[Path]:
    """Locate ``SUBBLK.DBF`` files inside a STEP 7 Classic project folder or ``.s7p`` path."""
    p = Path(project_path).expanduser().resolve()
    if p.is_file():
        if p.name.upper() == "SUBBLK.DBF":
            return [p]
        # .s7p file sits in the root of the STEP 7 project directory
        p = p.parent
    if not p.is_dir():
        return []

    found: List[Path] = []
    # Preferred canonical STEP 7 Classic path: ombstx/offline/*/SUBBLK.DBF
    omb = p / "ombstx" / "offline"
    if omb.is_dir():
        for child in sorted(omb.iterdir()):
            if child.is_dir():
                for fname in ("SUBBLK.DBF", "subblk.dbf"):
                    cand = child / fname
                    if cand.is_file():
                        found.append(cand)
                        break
    if not found:
        # Fallback recursive search (handles direct offline container folder or case variations)
        for cand in sorted(p.rglob("*")):
            if cand.is_file() and cand.name.upper() == "SUBBLK.DBF":
                found.append(cand)
    return found


def extract_mc7_blocks_from_step7_project(
    project_path: str | Path,
    block_names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Extract authentic Siemens-compiled MC7 binary blocks (OB/FC/FB/DB/SDB) directly
    from a STEP 7 Classic V5.x project folder (``ombstx/offline/*/SUBBLK.DBF`` + ``SUBBLK.DBT``).

    Returns a dictionary containing:
      - ``ok``: bool
      - ``project_dir``: str
      - ``databases_scanned``: list[str]
      - ``blocks``: dict[str, bytes] (e.g. ``{"OB1": b"\\x70\\x70...", "DB10": b"\\x70\\x70..."}``)
      - ``metadata``: dict[str, dict] (parsed MC7 headers per block)
      - ``missing_requested``: list[str]
    """
    p = Path(project_path).expanduser().resolve()
    dbf_files = _find_subblk_databases(p)
    if not dbf_files:
        return {
            "ok": False,
            "project_dir": str(p),
            "databases_scanned": [],
            "blocks": {},
            "metadata": {},
            "missing_requested": list(block_names or []),
            "error": (
                f"No STEP 7 Classic offline block database (SUBBLK.DBF) found under {p}. "
                "Ensure the STEP 7 project has compiled blocks in ombstx/offline/*/SUBBLK.DBF."
            ),
        }

    wanted: Optional[set] = None
    if block_names:
        wanted = set()
        for bn in block_names:
            bt, bnum = parse_block_identifier(bn)
            wanted.add(f"{bt}{bnum}")

    extracted: Dict[str, bytes] = {}
    metadata: Dict[str, Dict[str, Any]] = {}

    for dbf_path in dbf_files:
        records = read_dbf_records(dbf_path)
        for rec in records:
            subblk_typ = int(rec.get("SUBBLKTYP", 0) or 0)
            b_type = S7_MC7_BYTE_TO_TYPE.get(subblk_typ)
            if not b_type:
                continue  # skip interface/comment-only subblocks (types 1..7)
            b_num = int(rec.get("BLKNUMBER", -1) if rec.get("BLKNUMBER") is not None else -1)
            if b_num < 0:
                continue
            b_key = f"{b_type}{b_num}"
            if wanted is not None and b_key not in wanted:
                continue

            mc5_len = int(rec.get("MC5LEN", 0) or 0)
            ssb_len = int(rec.get("SSBLEN", 0) or 0)
            add_len = int(rec.get("ADDLEN", 0) or 0)
            loc_len = int(rec.get("LOCDATLEN", rec.get("LOCALDATA", 0)) or 0)
            blk_lang_code = int(rec.get("BLKLANG", 0) or 0)
            blk_flags = int(rec.get("BLKFLAGS", 1) or 1)
            checksum = int(rec.get("CHECKSUM", 0) or 0)
            version_raw = rec.get("VERSION", 1)
            if isinstance(version_raw, int):
                version_str = _decode_bcd_version(version_raw)
            else:
                version_str = str(version_raw or "0.1")

            raw_mc5 = rec.get("MC5CODE", b"")
            raw_ssb = rec.get("SSBPART", b"")
            raw_add = rec.get("ADDINFO", b"")
            if isinstance(raw_mc5, str):
                raw_mc5 = raw_mc5.encode("latin-1", errors="ignore")
            if isinstance(raw_ssb, str):
                raw_ssb = raw_ssb.encode("latin-1", errors="ignore")
            if isinstance(raw_add, str):
                raw_add = raw_add.encode("latin-1", errors="ignore")

            mc5_bytes = raw_mc5[:mc5_len] if mc5_len > 0 else raw_mc5
            ssb_bytes = raw_ssb[:ssb_len] if ssb_len > 0 else b""
            add_bytes = raw_add[:add_len] if add_len > 0 else b""

            if not mc5_bytes:
                continue

            # If the memo already contains a full 0x7070 network MC7 container, keep it verbatim!
            if len(mc5_bytes) >= MC7_HEADER_SIZE + MC7_FOOTER_SIZE and mc5_bytes[0:2] == b"\x70\x70":
                full_image = mc5_bytes
            else:
                lang_name = S7_MC7_BYTE_TO_LANG.get(blk_lang_code)
                full_image = build_mc7_block(
                    block_type=b_type,
                    block_number=b_num,
                    mc7_body=mc5_bytes,
                    language=lang_name,
                    local_data_len=loc_len,
                    ssb_bytes=ssb_bytes,
                    add_bytes=add_bytes,
                    author=str(rec.get("USERNAME", rec.get("AUTHOR", "STEP7")) or "STEP7"),
                    family=str(rec.get("FAMILY", "STEP7") or "STEP7"),
                    symbolic_name=str(rec.get("BLOCKNAME", rec.get("NAME", b_key)) or b_key),
                    version=version_str,
                    flags=blk_flags,
                    checksum=checksum if checksum != 0 else None,
                )

            extracted[b_key] = full_image
            parsed_meta = parse_mc7_block(full_image)
            parsed_meta["source_dbf"] = str(dbf_path)
            metadata[b_key] = parsed_meta

    missing = sorted(wanted - set(extracted.keys())) if wanted is not None else []
    return {
        "ok": bool(extracted) and not missing,
        "project_dir": str(p),
        "databases_scanned": [str(f) for f in dbf_files],
        "block_count": len(extracted),
        "block_names": sorted(extracted.keys()),
        "blocks": extracted,
        "metadata": metadata,
        "missing_requested": missing,
    }


__all__ = [
    "S7_MC7_TYPE_TO_BYTE",
    "S7_MC7_BYTE_TO_TYPE",
    "S7_COMM_BLOCK_TYPE_HEX",
    "S7_COMM_HEX_TO_BLOCK_TYPE",
    "S7_MC7_LANG_TO_BYTE",
    "S7_MC7_BYTE_TO_LANG",
    "MC7_HEADER_SIZE",
    "MC7_FOOTER_SIZE",
    "MC7_OP_BE",
    "parse_block_identifier",
    "encode_s7_timestamp",
    "decode_s7_timestamp",
    "compute_mc7_checksum",
    "parse_mc7_block",
    "build_mc7_block",
    "build_db_mc7_block",
    "read_dbf_records",
    "extract_mc7_blocks_from_step7_project",
]
