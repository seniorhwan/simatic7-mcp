"""High-speed S7comm runtime engine, connection pool, watch-table monitor, and deterministic Virtual S7 PLC.

Provides:
- ``S7ConnectionPool``: persistent RFC1006 / ISO-on-TCP socket pooling with keepalive & auto-reconnect.
- ``S7BatchProtocol``: multi-item S7comm Read Var (0x04) & Write Var (0x05) PDU encoding/decoding,
  Timer (0x1D) & Counter (0x1C) area support, SZL CPU diagnostics (0x07 Userdata), and CPU RUN/STOP control.
- ``WatchTableMonitor``: symbolic tag snapshot reading with delta detection and multi-sample polling.
- ``VirtualS7PLC``: deterministic in-memory S7-300/400 CPU executing STL/LAD networks (bit logic,
  parentheses stack, FP/FN edge detection, =/S/R, L/T, math, comparators, jumps, S7 Timers, S7 Counters, DBs).
- ``EnhancedS7LoopbackServer``: real TCP RFC1006/S7comm server backed by ``VirtualS7PLC`` supporting
  multi-item batch Read/Write Var PDUs, Timers/Counters, SZL diagnostics, and CPU RUN/STOP control.
"""

from __future__ import annotations

import csv
import json
import math
import re
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# S7 Address Parsing & Encoding
# ---------------------------------------------------------------------------

S7_AREA_CODES: Dict[str, int] = {
    "PI": 0x80,
    "PE": 0x80,
    "I": 0x81,
    "E": 0x81,
    "Q": 0x82,
    "A": 0x82,
    "M": 0x83,
    "DB": 0x84,
    "PQ": 0x85,
    "PA": 0x85,
    "L": 0x86,
    "C": 0x1C,
    "Z": 0x1C,
    "T": 0x1D,
}

S7_TRANSPORT_SIZE_BIT = 0x01
S7_TRANSPORT_SIZE_BYTE = 0x02
S7_TRANSPORT_SIZE_WORD = 0x04
S7_TRANSPORT_SIZE_DWORD = 0x06
S7_TRANSPORT_SIZE_COUNTER = 0x1C
S7_TRANSPORT_SIZE_TIMER = 0x1D


@dataclass(frozen=True)
class ParsedS7Address:
    raw: str
    area: str          # "I", "Q", "M", "DB", "T", "C", "PI", "PQ", "L"
    area_code: int     # 0x81, 0x82, 0x83, 0x84, 0x1D, 0x1C, 0x80, 0x85, 0x86
    db_number: int     # 0 for non-DB
    byte_offset: int
    bit_offset: int
    data_type: str     # "BOOL", "BYTE", "WORD", "INT", "DWORD", "DINT", "REAL", "TIMER", "COUNTER"
    byte_length: int


_RE_BIT = re.compile(r"^([IEQAML])(\d+)\.([0-7])$", re.IGNORECASE)
_RE_SCALAR = re.compile(r"^([IEQAML])([BWD])(\d+)$", re.IGNORECASE)
_RE_PERIPH = re.compile(r"^(PI|PE|PQ|PA)([BWD])(\d+)$", re.IGNORECASE)
_RE_DB_BIT = re.compile(r"^DB(\d+)\.DBX(\d+)\.([0-7])$", re.IGNORECASE)
_RE_DB_SCALAR = re.compile(r"^DB(\d+)\.DB([BWD])(\d+)$", re.IGNORECASE)
_RE_TC_Q = re.compile(r"^([TCZ])(\d+)\.Q$", re.IGNORECASE)
_RE_TC = re.compile(r"^([TCZ])(\d+)$", re.IGNORECASE)


def parse_s7_address(address: str, data_type: Optional[str] = None) -> ParsedS7Address:
    """Parse classic STEP 7 absolute addresses including I/Q/M/DB/T/C/PI/PQ/L (with or without internal spaces)."""
    raw = re.sub(r"\s+", "", (address or "").strip().upper())
    if not raw:
        raise ValueError("Empty S7 address")

    m = _RE_PERIPH.match(raw)
    if m:
        p_area, width, offset_s = m.group(1), m.group(2), m.group(3)
        area = "PI" if p_area in ("PI", "PE") else "PQ"
        default_dt = {"B": "BYTE", "W": "INT", "D": "DWORD"}[width]
        dt = (data_type or default_dt).upper()
        length = {"B": 1, "W": 2, "D": 4}[width]
        return ParsedS7Address(
            raw=raw,
            area=area,
            area_code=S7_AREA_CODES[area],
            db_number=0,
            byte_offset=int(offset_s),
            bit_offset=0,
            data_type=dt,
            byte_length=length,
        )

    m = _RE_BIT.match(raw)
    if m:
        area_ch = m.group(1)
        area = "I" if area_ch in ("I", "E") else ("Q" if area_ch in ("Q", "A") else area_ch)
        return ParsedS7Address(
            raw=raw,
            area=area,
            area_code=S7_AREA_CODES[area],
            db_number=0,
            byte_offset=int(m.group(2)),
            bit_offset=int(m.group(3)),
            data_type="BOOL",
            byte_length=1,
        )

    m = _RE_SCALAR.match(raw)
    if m:
        area_ch, width, offset_s = m.group(1), m.group(2), m.group(3)
        area = "I" if area_ch in ("I", "E") else ("Q" if area_ch in ("Q", "A") else area_ch)
        default_dt = {"B": "BYTE", "W": "WORD", "D": "DWORD"}[width]
        dt = (data_type or default_dt).upper()
        length = {"B": 1, "W": 2, "D": 4}[width]
        return ParsedS7Address(
            raw=raw,
            area=area,
            area_code=S7_AREA_CODES[area],
            db_number=0,
            byte_offset=int(offset_s),
            bit_offset=0,
            data_type=dt,
            byte_length=length,
        )

    m = _RE_DB_BIT.match(raw)
    if m:
        return ParsedS7Address(
            raw=raw,
            area="DB",
            area_code=0x84,
            db_number=int(m.group(1)),
            byte_offset=int(m.group(2)),
            bit_offset=int(m.group(3)),
            data_type="BOOL",
            byte_length=1,
        )

    m = _RE_DB_SCALAR.match(raw)
    if m:
        db_num, width, offset_s = int(m.group(1)), m.group(2), m.group(3)
        default_dt = {"B": "BYTE", "W": "WORD", "D": "DWORD"}[width]
        dt = (data_type or default_dt).upper()
        length = {"B": 1, "W": 2, "D": 4}[width]
        return ParsedS7Address(
            raw=raw,
            area="DB",
            area_code=0x84,
            db_number=db_num,
            byte_offset=int(offset_s),
            bit_offset=0,
            data_type=dt,
            byte_length=length,
        )

    m = _RE_TC_Q.match(raw)
    if m:
        ch, num = m.group(1), int(m.group(2))
        area = "T" if ch == "T" else "C"
        return ParsedS7Address(
            raw=raw,
            area=area,
            area_code=0x1D if area == "T" else 0x1C,
            db_number=0,
            byte_offset=num,
            bit_offset=0,
            data_type="BOOL",
            byte_length=1,
        )

    m = _RE_TC.match(raw)
    if m:
        ch, num = m.group(1), int(m.group(2))
        if ch == "T":
            return ParsedS7Address(
                raw=raw,
                area="T",
                area_code=0x1D,
                db_number=0,
                byte_offset=num,
                bit_offset=0,
                data_type=(data_type or "TIMER").upper(),
                byte_length=2,
            )
        return ParsedS7Address(
            raw=raw,
            area="C",
            area_code=0x1C,
            db_number=0,
            byte_offset=num,
            bit_offset=0,
            data_type=(data_type or "COUNTER").upper(),
            byte_length=2,
        )

    raise ValueError(f"Unsupported S7 address format: {address!r}")


def encode_s7_value(addr: ParsedS7Address, value: Any) -> bytes:
    """Encode a Python value into big-endian S7 bytes."""
    dt = addr.data_type.upper()
    if dt == "BOOL":
        return bytes([1 if bool(value) else 0])
    if dt in ("BYTE", "USINT", "SINT"):
        return struct.pack(">B", int(value) & 0xFF)
    if dt in ("WORD", "UINT", "TIMER", "COUNTER"):
        return struct.pack(">H", int(value) & 0xFFFF)
    if dt == "INT":
        return struct.pack(">h", max(-32768, min(32767, int(value))))
    if dt in ("DWORD", "UDINT"):
        return struct.pack(">I", int(value) & 0xFFFFFFFF)
    if dt == "DINT":
        return struct.pack(">i", int(value))
    if dt == "REAL":
        return struct.pack(">f", float(value))
    if addr.byte_length == 1:
        return struct.pack(">B", int(value) & 0xFF)
    if addr.byte_length == 2:
        return struct.pack(">H", int(value) & 0xFFFF)
    return struct.pack(">I", int(value) & 0xFFFFFFFF)


def decode_s7_value(addr: ParsedS7Address, raw: bytes) -> Any:
    """Decode big-endian S7 bytes into a Python scalar."""
    dt = addr.data_type.upper()
    if dt == "BOOL":
        return bool(raw[0] & 0x01) if raw else False
    if dt in ("BYTE", "USINT"):
        return raw[0] if raw else 0
    if dt == "SINT":
        return struct.unpack(">b", raw[:1])[0]
    if dt in ("WORD", "UINT", "TIMER", "COUNTER"):
        return struct.unpack(">H", raw[:2])[0]
    if dt == "INT":
        return struct.unpack(">h", raw[:2])[0]
    if dt in ("DWORD", "UDINT"):
        return struct.unpack(">I", raw[:4])[0]
    if dt == "DINT":
        return struct.unpack(">i", raw[:4])[0]
    if dt == "REAL":
        val = struct.unpack(">f", raw[:4])[0]
        return round(val, 6) if math.isfinite(val) else val
    if len(raw) == 1:
        return raw[0]
    if len(raw) == 2:
        return struct.unpack(">H", raw[:2])[0]
    if len(raw) >= 4:
        return struct.unpack(">I", raw[:4])[0]
    return int.from_bytes(raw, "big")


# ---------------------------------------------------------------------------
# RFC1006 / TPKT / COTP / S7comm Wire Protocol Helpers
# ---------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("S7 TCP socket closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


def recv_tpkt(sock: socket.socket) -> bytes:
    hdr = _recv_exact(sock, 4)
    if hdr[0] != 0x03:
        raise ValueError(f"Invalid TPKT version byte: 0x{hdr[0]:02X}")
    length = struct.unpack(">H", hdr[2:4])[0]
    if length < 4:
        raise ValueError(f"Invalid TPKT frame length: {length}")
    body = _recv_exact(sock, length - 4)
    return hdr + body


def wrap_tpkt_cotp_dt(s7_pdu: bytes) -> bytes:
    cotp = b"\x02\xf0\x80"
    total = 4 + len(cotp) + len(s7_pdu)
    return struct.pack(">BBH", 0x03, 0x00, total) + cotp + s7_pdu


S7_CONN_TYPES: Dict[str, int] = {
    "PG": 0x01,
    "OP": 0x02,
    "S7_BASIC": 0x03,
}

S7_BLOCK_TYPE_CODES: Dict[str, int] = {
    "OB": 0x38,
    "DB": 0x41,
    "SDB": 0x42,
    "FC": 0x43,
    "SFC": 0x44,
    "FB": 0x45,
    "SFB": 0x46,
}

S7_BLOCK_TYPE_NAMES: Dict[int, str] = {v: k for k, v in S7_BLOCK_TYPE_CODES.items()}


def build_cotp_cr(rack: int, slot: int, connection_type: str = "PG") -> bytes:
    conn_code = S7_CONN_TYPES.get((connection_type or "PG").strip().upper(), 0x01)
    tsap_dst = (conn_code << 8) | ((rack & 0x07) << 5) | (slot & 0x1F)
    cotp = (
        bytes([0x11, 0xE0, 0x00, 0x00, 0x00, 0x01, 0x00])
        + bytes([0xC0, 0x01, 0x0A])
        + bytes([0xC1, 0x02, 0x01, 0x00])
        + bytes([0xC2, 0x02, (tsap_dst >> 8) & 0xFF, tsap_dst & 0xFF])
    )
    return struct.pack(">BBH", 0x03, 0x00, 4 + len(cotp)) + cotp


def build_s7_setup_comm(pdu_ref: int = 1, max_pdu: int = 480) -> bytes:
    param = struct.pack(">BBHHH", 0xF0, 0x00, 0x0001, 0x0001, max_pdu)
    header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000, pdu_ref, len(param), 0)
    return wrap_tpkt_cotp_dt(header + param)


def _encode_s7_item_spec(addr: ParsedS7Address) -> bytes:
    if addr.area == "T":
        t_size = S7_TRANSPORT_SIZE_TIMER
        count = 1
        bit_addr = addr.byte_offset
    elif addr.area == "C":
        t_size = S7_TRANSPORT_SIZE_COUNTER
        count = 1
        bit_addr = addr.byte_offset
    elif addr.data_type == "BOOL":
        t_size = S7_TRANSPORT_SIZE_BIT
        count = 1
        bit_addr = addr.byte_offset * 8 + addr.bit_offset
    else:
        t_size = S7_TRANSPORT_SIZE_BYTE
        count = addr.byte_length
        bit_addr = addr.byte_offset * 8
    return struct.pack(
        ">BBBBHHBBBB",
        0x12,
        0x0A,
        0x10,
        t_size,
        count,
        addr.db_number,
        addr.area_code,
        (bit_addr >> 16) & 0xFF,
        (bit_addr >> 8) & 0xFF,
        bit_addr & 0xFF,
    )


def build_batch_read_pdu(addrs: Sequence[ParsedS7Address], pdu_ref: int) -> bytes:
    items = b"".join(_encode_s7_item_spec(a) for a in addrs)
    param = bytes([0x04, len(addrs)]) + items
    header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000, pdu_ref & 0xFFFF, len(param), 0)
    return wrap_tpkt_cotp_dt(header + param)


def build_batch_write_pdu(pairs: Sequence[Tuple[ParsedS7Address, bytes]], pdu_ref: int) -> bytes:
    param_items = bytearray()
    data_items = bytearray()
    for idx, (addr, raw_bytes) in enumerate(pairs):
        param_items.extend(_encode_s7_item_spec(addr))
        if addr.data_type == "BOOL":
            ts = 0x03  # BIT data transport
            bit_len = 1
            payload = bytes([1 if raw_bytes[0] else 0])
        else:
            ts = 0x04  # BYTE/WORD/DWORD data transport (length in bits)
            bit_len = len(raw_bytes) * 8
            payload = raw_bytes
        data_items.extend(struct.pack(">BBH", 0x00, ts, bit_len))
        data_items.extend(payload)
        if len(payload) % 2 == 1 and idx < len(pairs) - 1:
            data_items.append(0x00)
    param = bytes([0x05, len(pairs)]) + bytes(param_items)
    header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000, pdu_ref & 0xFFFF, len(param), len(data_items))
    return wrap_tpkt_cotp_dt(header + param + bytes(data_items))


def build_szl_read_pdu(szl_id: int, szl_index: int = 0, pdu_ref: int = 1) -> bytes:
    # S7comm Userdata (0x07), CPU functions (group 4), Read SZL (subfunction 1)
    param = bytes([0x00, 0x01, 0x12, 0x04, 0x11, 0x44, 0x01, 0x00])
    data = struct.pack(">BBHHH", 0xFF, 0x09, 0x0004, szl_id & 0xFFFF, szl_index & 0xFFFF)
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref & 0xFFFF, len(param), len(data))
    return wrap_tpkt_cotp_dt(header + param + data)


def build_list_blocks_count_pdu(pdu_ref: int = 1) -> bytes:
    # S7comm Userdata (0x07), Block functions (group 3 -> 0x43), List blocks count (subfunction 1)
    param = bytes([0x00, 0x01, 0x12, 0x04, 0x11, 0x43, 0x01, 0x00])
    data = bytes([0x0A, 0x00, 0x00, 0x00])
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref & 0xFFFF, len(param), len(data))
    return wrap_tpkt_cotp_dt(header + param + data)


def build_list_blocks_of_type_pdu(block_type_code: int, pdu_ref: int = 1) -> bytes:
    # S7comm Userdata (0x07), Block functions (group 3 -> 0x43), List blocks of type (subfunction 2)
    param = bytes([0x00, 0x01, 0x12, 0x04, 0x11, 0x43, 0x02, 0x00])
    data = bytes([0xFF, 0x09, 0x00, 0x02, 0x30, block_type_code & 0xFF])
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref & 0xFFFF, len(param), len(data))
    return wrap_tpkt_cotp_dt(header + param + data)


def build_cpu_control_pdu(action: str, pdu_ref: int = 1) -> bytes:
    act = action.strip().upper()
    if act == "STOP":
        # S7comm PLC Stop (0x29)
        param = b"\x29\x00\x00\x00\x00\x00\x09P_PROGRAM"
    elif act in ("RUN", "WARM_RESTART", "START"):
        # S7comm PI-Service _INSE / P_PROGRAM Warm Start (0x28)
        param = b"\x28\x00\x00\x00\x00\x00\x00\xfd\x00\x00\x09P_PROGRAM"
    else:
        raise ValueError(f"Unsupported CPU control action: {action!r}")
    header = struct.pack(">BBHHHH", 0x32, 0x01, 0x0000, pdu_ref & 0xFFFF, len(param), 0)
    return wrap_tpkt_cotp_dt(header + param)


def parse_s7_ack_data(tpkt: bytes) -> Tuple[int, int, bytes, bytes]:
    """Return (rosctr, error_code, param_bytes, data_bytes) from a TPKT S7 response."""
    if len(tpkt) < 17:
        raise ValueError("Truncated S7 response frame")
    s7 = tpkt[7:]  # skip 4-byte TPKT + 3-byte COTP DT
    if s7[0] != 0x32:
        raise ValueError(f"Unexpected S7 protocol ID: 0x{s7[0]:02X}")
    rosctr = s7[1]
    if rosctr == 0x03:  # Ack_Data
        param_len, data_len, err_class, err_code = struct.unpack(">HHBB", s7[6:12])
        offset = 12
        err = (err_class << 8) | err_code
    elif rosctr == 0x07:  # Userdata
        param_len, data_len = struct.unpack(">HH", s7[6:10])
        offset = 10
        err = 0
    else:
        param_len, data_len = struct.unpack(">HH", s7[6:10])
        offset = 10
        err = 0
    param = s7[offset:offset + param_len]
    data = s7[offset + param_len:offset + param_len + data_len]
    return rosctr, err, param, data


# ---------------------------------------------------------------------------
# Thread-Safe Persistent S7 Connection Pool
# ---------------------------------------------------------------------------

@dataclass
class _PooledS7Conn:
    host: str
    port: int
    rack: int
    slot: int
    sock: socket.socket
    negotiated_pdu: int = 240
    connection_type: str = "PG"
    pdu_ref: int = 1
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_ref(self) -> int:
        self.pdu_ref = (self.pdu_ref % 65535) + 1
        return self.pdu_ref

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def _chunk_read_specs(
    parsed_specs: Sequence[Tuple[Dict[str, Any], ParsedS7Address]],
    negotiated_pdu: int,
    max_items_per_pdu: int = 16,
) -> List[List[Tuple[Dict[str, Any], ParsedS7Address]]]:
    """Split Read Var items so neither request nor response exceeds the CPU's negotiated PDU (e.g. 240B on S7-300)."""
    pdu_budget = max(64, int(negotiated_pdu) - 4)
    chunks: List[List[Tuple[Dict[str, Any], ParsedS7Address]]] = []
    cur: List[Tuple[Dict[str, Any], ParsedS7Address]] = []
    req_bytes = 12   # 10B S7 job header + 2B Read Var param header
    resp_bytes = 14  # 12B S7 Ack_Data header + 2B Read Var param header
    cap = max(1, int(max_items_per_pdu))

    for spec in parsed_specs:
        addr = spec[1]
        item_req = 12
        payload_len = 1 if addr.data_type == "BOOL" else addr.byte_length
        padded_len = ((payload_len + 1) // 2) * 2
        item_resp = 4 + padded_len
        if cur and (
            len(cur) >= cap
            or (req_bytes + item_req) > pdu_budget
            or (resp_bytes + item_resp) > pdu_budget
        ):
            chunks.append(cur)
            cur = []
            req_bytes = 12
            resp_bytes = 14
        cur.append(spec)
        req_bytes += item_req
        resp_bytes += item_resp

    if cur:
        chunks.append(cur)
    return chunks


def _chunk_write_specs(
    encoded_specs: Sequence[Tuple[Dict[str, Any], ParsedS7Address, bytes]],
    negotiated_pdu: int,
    max_items_per_pdu: int = 12,
) -> List[List[Tuple[Dict[str, Any], ParsedS7Address, bytes]]]:
    """Split Write Var items so the request stays strictly inside the CPU's negotiated PDU (e.g. 240B on S7-300)."""
    pdu_budget = max(64, int(negotiated_pdu) - 4)
    chunks: List[List[Tuple[Dict[str, Any], ParsedS7Address, bytes]]] = []
    cur: List[Tuple[Dict[str, Any], ParsedS7Address, bytes]] = []
    req_bytes = 12   # 10B S7 job header + 2B Write Var param header
    resp_bytes = 14  # 12B S7 Ack_Data header + 2B Write Var param header
    cap = max(1, int(max_items_per_pdu))

    for spec in encoded_specs:
        _, addr, raw = spec
        payload_len = 1 if addr.data_type == "BOOL" else len(raw)
        padded_len = ((payload_len + 1) // 2) * 2
        item_req = 12 + 4 + padded_len
        item_resp = 1
        if cur and (
            len(cur) >= cap
            or (req_bytes + item_req) > pdu_budget
            or (resp_bytes + item_resp) > pdu_budget
        ):
            chunks.append(cur)
            cur = []
            req_bytes = 12
            resp_bytes = 14
        cur.append(spec)
        req_bytes += item_req
        resp_bytes += item_resp

    if cur:
        chunks.append(cur)
    return chunks


class S7ConnectionPool:
    """Thread-safe RFC1006/S7comm connection pool eliminating per-read TCP handshakes."""

    def __init__(self, idle_ttl_s: float = 60.0, default_timeout_s: float = 3.0) -> None:
        self.idle_ttl_s = idle_ttl_s
        self.default_timeout_s = default_timeout_s
        self._lock = threading.Lock()
        self._conns: Dict[Tuple[str, int, int, int], _PooledS7Conn] = {}
        self.connect_count = 0
        self.reuse_count = 0

    def _open_conn(
        self,
        host: str,
        port: int,
        rack: int,
        slot: int,
        timeout_s: float,
        connection_type: str = "AUTO",
    ) -> _PooledS7Conn:
        ct_up = (connection_type or "AUTO").strip().upper()
        candidates = ["PG", "OP", "S7_BASIC"] if ct_up == "AUTO" else [ct_up if ct_up in S7_CONN_TYPES else "PG"]
        last_cotp_err: Optional[Exception] = None

        for idx, cand_type in enumerate(candidates):
            try:
                sock = socket.create_connection((host, port), timeout=timeout_s)
            except OSError:
                # Physical TCP endpoint unreachable — fail immediately without retrying other TSAPs
                raise
            sock.settimeout(timeout_s)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            try:
                # 1. COTP Connection Request
                sock.sendall(build_cotp_cr(rack, slot, connection_type=cand_type))
                cc = recv_tpkt(sock)
                if len(cc) < 6 or cc[5] != 0xD0:
                    raise ConnectionError(
                        f"PLC rejected COTP Connection Request (TSAP type={cand_type}, rack={rack}, slot={slot})"
                    )
                # 2. S7 Setup Communication
                sock.sendall(build_s7_setup_comm(pdu_ref=1, max_pdu=480))
                setup_resp = recv_tpkt(sock)
                _, err, param, _ = parse_s7_ack_data(setup_resp)
                if err != 0:
                    raise ConnectionError(f"S7 Setup Communication error: 0x{err:04X}")
                neg_pdu = struct.unpack(">H", param[6:8])[0] if len(param) >= 8 else 240
                self.connect_count += 1
                return _PooledS7Conn(
                    host=host,
                    port=port,
                    rack=rack,
                    slot=slot,
                    sock=sock,
                    negotiated_pdu=max(96, neg_pdu),
                    connection_type=cand_type,
                )
            except Exception as exc:
                try:
                    sock.close()
                except Exception:
                    pass
                last_cotp_err = exc
                if idx == len(candidates) - 1:
                    raise

        raise last_cotp_err or ConnectionError("Failed to establish S7 COTP session")

    def acquire(
        self,
        host: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: Optional[float] = None,
        connection_type: str = "AUTO",
    ) -> _PooledS7Conn:
        key = (host, int(port), int(rack), int(slot))
        t_out = timeout_s if timeout_s is not None else self.default_timeout_s
        ct_up = (connection_type or "AUTO").strip().upper()
        now = time.monotonic()
        with self._lock:
            existing = self._conns.get(key)
            if existing is not None:
                type_ok = (ct_up == "AUTO") or (existing.connection_type == ct_up)
                if type_ok and (now - existing.last_used) <= self.idle_ttl_s and existing.sock.fileno() >= 0:
                    existing.sock.settimeout(t_out)
                    existing.last_used = now
                    self.reuse_count += 1
                    return existing
                existing.close()
                del self._conns[key]
        conn = self._open_conn(host, int(port), int(rack), int(slot), t_out, connection_type=ct_up)
        with self._lock:
            self._conns[key] = conn
        return conn

    def invalidate(self, host: str, port: int, rack: int, slot: int) -> None:
        key = (host, int(port), int(rack), int(slot))
        with self._lock:
            conn = self._conns.pop(key, None)
            if conn:
                conn.close()

    def close_all(self) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.close()
            self._conns.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_connections": len(self._conns),
                "total_handshakes": self.connect_count,
                "pool_reuses": self.reuse_count,
                "keys": [
                    f"{h}:{p}/R{r}S{s}({c.connection_type},PDU={c.negotiated_pdu})"
                    for (h, p, r, s), c in self._conns.items()
                ],
            }

    def _exchange(
        self,
        host: str,
        port: int,
        rack: int,
        slot: int,
        pdu_builder: Callable[[int], bytes],
        timeout_s: float = 3.0,
        connection_type: str = "AUTO",
    ) -> Tuple[int, int, bytes, bytes]:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            conn = self.acquire(host, port, rack, slot, timeout_s=timeout_s, connection_type=connection_type)
            with conn.lock:
                try:
                    ref = conn.next_ref()
                    conn.sock.sendall(pdu_builder(ref))
                    resp = recv_tpkt(conn.sock)
                    conn.last_used = time.monotonic()
                    return parse_s7_ack_data(resp)
                except Exception as exc:
                    last_exc = exc
                    self.invalidate(host, port, rack, slot)
                    if attempt == 1:
                        raise
        raise last_exc or ConnectionError("S7 exchange failed")

    def batch_read(
        self,
        host: str,
        items: Sequence[Dict[str, Any]],
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: float = 3.0,
        max_items_per_pdu: int = 16,
        connection_type: str = "AUTO",
    ) -> Dict[str, Any]:
        """Read multiple S7 addresses in minimal S7comm Read Var PDUs sized to the CPU's negotiated PDU."""
        t0 = time.perf_counter()
        parsed_specs: List[Tuple[Dict[str, Any], ParsedS7Address]] = []
        for item in items:
            addr_str = str(item.get("address") or "")
            dt = item.get("data_type")
            parsed_specs.append((item, parse_s7_address(addr_str, dt)))

        conn = self.acquire(host, port, rack, slot, timeout_s=timeout_s, connection_type=connection_type)
        neg_pdu = conn.negotiated_pdu
        conn_type = conn.connection_type
        chunks = _chunk_read_specs(parsed_specs, negotiated_pdu=neg_pdu, max_items_per_pdu=max_items_per_pdu)

        results: List[Dict[str, Any]] = []
        pdu_count = 0

        for chunk in chunks:
            addrs = [a for _, a in chunk]
            pdu_count += 1
            _, err, _, data = self._exchange(
                host, port, rack, slot,
                lambda ref, _addrs=addrs: build_batch_read_pdu(_addrs, ref),
                timeout_s=timeout_s,
                connection_type=connection_type,
            )
            if err != 0:
                raise RuntimeError(f"S7 Read Var failed with error 0x{err:04X}")

            cursor = 0
            for item, addr in chunk:
                if cursor + 4 > len(data):
                    results.append({
                        "address": addr.raw,
                        "symbol": item.get("symbol"),
                        "data_type": addr.data_type,
                        "ok": False,
                        "error": "Truncated item response",
                    })
                    continue
                ret_code, t_size, raw_len = struct.unpack(">BBH", data[cursor:cursor + 4])
                cursor += 4
                # Length is in bits for transport sizes 0x03 (BIT) and 0x04 (BYTE/WORD/DWORD), bytes for 0x09 (OCTET)
                if t_size in (0x03, 0x04):
                    byte_len = (raw_len + 7) // 8
                else:
                    byte_len = raw_len
                payload = data[cursor:cursor + byte_len]
                cursor += byte_len
                if byte_len % 2 == 1 and cursor < len(data):
                    cursor += 1
                if ret_code != 0xFF:
                    results.append({
                        "address": addr.raw,
                        "symbol": item.get("symbol"),
                        "data_type": addr.data_type,
                        "ok": False,
                        "return_code": f"0x{ret_code:02X}",
                    })
                else:
                    results.append({
                        "address": addr.raw,
                        "symbol": item.get("symbol"),
                        "data_type": addr.data_type,
                        "value": decode_s7_value(addr, payload),
                        "raw_hex": payload.hex(),
                        "ok": True,
                    })

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "ok": all(r.get("ok") for r in results),
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "connection_type": conn_type,
            "negotiated_pdu": neg_pdu,
            "item_count": len(results),
            "pdu_count": pdu_count,
            "latency_ms": elapsed_ms,
            "pool": self.stats(),
            "items": results,
        }

    def batch_write(
        self,
        host: str,
        items: Sequence[Dict[str, Any]],
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: float = 3.0,
        max_items_per_pdu: int = 12,
        connection_type: str = "AUTO",
    ) -> Dict[str, Any]:
        """Write multiple S7 addresses in minimal S7comm Write Var PDUs sized to the CPU's negotiated PDU."""
        t0 = time.perf_counter()
        encoded: List[Tuple[Dict[str, Any], ParsedS7Address, bytes]] = []
        for item in items:
            addr = parse_s7_address(str(item.get("address") or ""), item.get("data_type"))
            raw = encode_s7_value(addr, item.get("value"))
            encoded.append((item, addr, raw))

        conn = self.acquire(host, port, rack, slot, timeout_s=timeout_s, connection_type=connection_type)
        neg_pdu = conn.negotiated_pdu
        conn_type = conn.connection_type
        chunks = _chunk_write_specs(encoded, negotiated_pdu=neg_pdu, max_items_per_pdu=max_items_per_pdu)

        results: List[Dict[str, Any]] = []
        pdu_count = 0

        for chunk in chunks:
            pairs = [(a, raw) for _, a, raw in chunk]
            pdu_count += 1
            _, err, _, data = self._exchange(
                host, port, rack, slot,
                lambda ref, _pairs=pairs: build_batch_write_pdu(_pairs, ref),
                timeout_s=timeout_s,
                connection_type=connection_type,
            )
            if err != 0:
                raise RuntimeError(f"S7 Write Var failed with error 0x{err:04X}")

            for idx, (item, addr, _) in enumerate(chunk):
                rc = data[idx] if idx < len(data) else 0x00
                results.append({
                    "address": addr.raw,
                    "symbol": item.get("symbol"),
                    "data_type": addr.data_type,
                    "written_value": item.get("value"),
                    "ok": rc == 0xFF,
                    "return_code": f"0x{rc:02X}",
                })

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {
            "ok": all(r.get("ok") for r in results),
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "connection_type": conn_type,
            "negotiated_pdu": neg_pdu,
            "item_count": len(results),
            "pdu_count": pdu_count,
            "latency_ms": elapsed_ms,
            "items": results,
        }

    def read_szl_diagnostics(
        self,
        host: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: float = 3.0,
        connection_type: str = "AUTO",
    ) -> Dict[str, Any]:
        """Query CPU order number (SZL 0x0011), CPU state (SZL 0x0424), and diagnostic buffer (SZL 0x00A0)."""
        t0 = time.perf_counter()
        szl_errors: Dict[str, str] = {}

        def _query(label: str, szl_id: int, szl_idx: int = 0) -> bytes:
            try:
                _, err, _, data = self._exchange(
                    host, port, rack, slot,
                    lambda ref: build_szl_read_pdu(szl_id, szl_idx, ref),
                    timeout_s=timeout_s,
                    connection_type=connection_type,
                )
                if err != 0:
                    szl_errors[label] = f"S7 error 0x{err:04X}"
                    return b""
                if len(data) >= 1 and data[0] != 0xFF:
                    szl_errors[label] = f"SZL return code 0x{data[0]:02X}"
                    return b""
                return data
            except Exception as exc:
                szl_errors[label] = str(exc)
                return b""

        raw_id = _query("szl_0011_module_id", 0x0011, 0x0001)
        raw_state = _query("szl_0424_cpu_mode", 0x0424, 0x0000)
        raw_diag = _query("szl_00a0_diag_buffer", 0x00A0, 0x0000)

        # Parse module identification (SZL 0x0011)
        order_number = ""
        if len(raw_id) >= 34:
            # Skip 4-byte data header (FF 09 len) + 8-byte SZL header (id, idx, rec_len, n_rec) + 2-byte record index
            rec_start = 12
            order_number = raw_id[rec_start + 2:rec_start + 22].decode("ascii", errors="ignore").strip("\x00 ")

        # Parse CPU operating mode (SZL 0x0424)
        cpu_mode = "UNKNOWN"
        if len(raw_state) >= 16:
            mode_byte = raw_state[15]
            mode_map = {
                0x00: "UNKNOWN",
                0x04: "STOP",
                0x08: "RUN",
                0x0C: "STARTUP",
                0x03: "HOLD",
            }
            cpu_mode = mode_map.get(mode_byte, f"MODE_0x{mode_byte:02X}")

        # Parse diagnostics buffer entries (SZL 0x00A0, 20 bytes per entry)
        diag_entries: List[Dict[str, Any]] = []
        if len(raw_diag) >= 12:
            rec_len, n_rec = struct.unpack(">HH", raw_diag[8:12])
            pos = 12
            for _ in range(min(n_rec, 50)):
                if pos + rec_len > len(raw_diag) or rec_len < 12:
                    break
                rec = raw_diag[pos:pos + rec_len]
                pos += rec_len
                event_id = struct.unpack(">H", rec[0:2])[0]
                priority = rec[2]
                ob_num = rec[3]
                info1 = struct.unpack(">H", rec[4:6])[0]
                info2 = struct.unpack(">I", rec[6:10])[0]
                diag_entries.append({
                    "event_id": f"0x{event_id:04X}",
                    "priority_class": priority,
                    "ob_number": ob_num,
                    "info1": f"0x{info1:04X}",
                    "info2": f"0x{info2:08X}",
                })

        has_any_data = bool(order_number or cpu_mode != "UNKNOWN" or diag_entries or not szl_errors)
        out: Dict[str, Any] = {
            "ok": has_any_data,
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "order_number": order_number or "UNKNOWN",
            "cpu_mode": cpu_mode,
            "diagnostics_count": len(diag_entries),
            "diagnostics_buffer": diag_entries,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
        if szl_errors:
            out["szl_errors"] = szl_errors
        return out

    def list_online_blocks(
        self,
        host: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        include_block_numbers: bool = True,
        timeout_s: float = 3.0,
        connection_type: str = "AUTO",
    ) -> Dict[str, Any]:
        """List blocks currently loaded in a live S7-300/400 PLC over S7comm Userdata Block Functions (group 3)."""
        t0 = time.perf_counter()
        _, err, _, data = self._exchange(
            host, port, rack, slot,
            lambda ref: build_list_blocks_count_pdu(ref),
            timeout_s=timeout_s,
            connection_type=connection_type,
        )
        if err != 0:
            raise RuntimeError(f"S7 List Blocks Count failed with error 0x{err:04X}")

        counts: Dict[str, int] = {name: 0 for name in S7_BLOCK_TYPE_CODES}
        if len(data) >= 4 and data[0] == 0xFF:
            payload_len = struct.unpack(">H", data[2:4])[0]
            body = data[4:4 + payload_len]
            for pos in range(0, len(body) - 3, 4):
                if body[pos] == 0x30:
                    b_type = S7_BLOCK_TYPE_NAMES.get(body[pos + 1])
                    b_cnt = struct.unpack(">H", body[pos + 2:pos + 4])[0]
                    if b_type:
                        counts[b_type] = b_cnt

        blocks: Dict[str, List[int]] = {}
        block_names: List[str] = []
        if include_block_numbers:
            for b_type, b_code in S7_BLOCK_TYPE_CODES.items():
                if counts.get(b_type, 0) <= 0:
                    blocks[b_type] = []
                    continue
                try:
                    _, b_err, _, b_data = self._exchange(
                        host, port, rack, slot,
                        lambda ref, _c=b_code: build_list_blocks_of_type_pdu(_c, ref),
                        timeout_s=timeout_s,
                        connection_type=connection_type,
                    )
                    nums: List[int] = []
                    if b_err == 0 and len(b_data) >= 4 and b_data[0] == 0xFF:
                        blen = struct.unpack(">H", b_data[2:4])[0]
                        bbody = b_data[4:4 + blen]
                        for pos in range(0, len(bbody) - 3, 4):
                            nums.append(struct.unpack(">H", bbody[pos:pos + 2])[0])
                    nums = sorted(set(nums))
                    blocks[b_type] = nums
                    for n in nums:
                        block_names.append(f"{b_type}{n}")
                except Exception:
                    blocks[b_type] = []

        return {
            "ok": True,
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "counts": counts,
            "total_blocks": sum(counts.values()),
            "blocks": blocks,
            "block_names": block_names,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    def cpu_control(
        self,
        host: str,
        action: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: float = 3.0,
        connection_type: str = "AUTO",
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        _, err, param, _ = self._exchange(
            host, port, rack, slot,
            lambda ref: build_cpu_control_pdu(action, ref),
            timeout_s=timeout_s,
            connection_type=connection_type,
        )
        return {
            "ok": err == 0,
            "host": host,
            "port": port,
            "action": action.upper(),
            "error_code": f"0x{err:04X}",
            "response_hex": param.hex(),
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }


# ---------------------------------------------------------------------------
# Watch Table (VAT) Monitor with Symbolic Resolution & Delta Tracking
# ---------------------------------------------------------------------------

class WatchTableMonitor:
    """Monitors named watch tables with optional symbol-table resolution and delta tracking."""

    def __init__(self, pool: S7ConnectionPool) -> None:
        self.pool = pool
        self._lock = threading.Lock()
        self._last_snapshots: Dict[str, Dict[str, Any]] = {}

    def snapshot(
        self,
        host: str,
        tags: Sequence[Dict[str, Any]],
        table_id: str = "default",
        symbol_table: Optional[Dict[str, str]] = None,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        samples: int = 1,
        interval_ms: int = 100,
    ) -> Dict[str, Any]:
        resolved_tags: List[Dict[str, Any]] = []
        sym_lookup = {k.strip().upper(): v.strip() for k, v in (symbol_table or {}).items()}
        for t in tags:
            if isinstance(t, str):
                addr_or_sym = t.strip()
                dt = None
                sym = None
            else:
                addr_or_sym = str(t.get("address") or t.get("symbol") or "").strip()
                dt = t.get("data_type")
                sym = t.get("symbol")
            if addr_or_sym.upper() in sym_lookup:
                sym = sym or addr_or_sym
                addr = sym_lookup[addr_or_sym.upper()]
            else:
                addr = addr_or_sym
            resolved_tags.append({"address": addr, "data_type": dt, "symbol": sym})

        sample_count = max(1, min(int(samples), 100))
        all_samples: List[Dict[str, Any]] = []
        key = f"{table_id}@{host}:{port}"

        for idx in range(sample_count):
            res = self.pool.batch_read(host, resolved_tags, port=port, rack=rack, slot=slot)
            with self._lock:
                prev = self._last_snapshots.get(key, {})
                deltas: List[Dict[str, Any]] = []
                curr_map: Dict[str, Any] = {}
                for it in res["items"]:
                    addr = it["address"]
                    val = it.get("value")
                    curr_map[addr] = val
                    if addr in prev and prev[addr] != val:
                        deltas.append({
                            "address": addr,
                            "symbol": it.get("symbol"),
                            "previous": prev[addr],
                            "current": val,
                        })
                self._last_snapshots[key] = curr_map

            all_samples.append({
                "sample_index": idx,
                "timestamp": time.time(),
                "latency_ms": res["latency_ms"],
                "pdu_count": res["pdu_count"],
                "items": res["items"],
                "changed": deltas,
            })
            if idx < sample_count - 1 and interval_ms > 0:
                time.sleep(min(interval_ms, 5000) / 1000.0)

        return {
            "ok": True,
            "table_id": table_id,
            "host": host,
            "port": port,
            "tag_count": len(resolved_tags),
            "sample_count": len(all_samples),
            "latest": all_samples[-1] if all_samples else None,
            "samples": all_samples,
        }


# ---------------------------------------------------------------------------
# AI-Agent PLC Data Collector & Time-Series Historian
# ---------------------------------------------------------------------------

class PLCDataCollector:
    """High-throughput time-series data logger, statistical summarizer, and CSV/JSONL exporter for AI agents."""

    @staticmethod
    def _sanitize_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "plc_dataset").strip()).strip("_")
        return cleaned[:64] or "plc_dataset"

    @classmethod
    def collect(
        cls,
        read_fn: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
        tags: Sequence[Dict[str, Any]],
        samples: int = 20,
        interval_ms: int = 200,
        duration_s: Optional[float] = None,
        thresholds: Optional[Dict[str, Dict[str, Any]]] = None,
        export_format: Optional[str] = None,
        export_dir: Optional[Path] = None,
        dataset_name: Optional[str] = None,
        include_raw_samples: bool = True,
    ) -> Dict[str, Any]:
        """Collect time-series tag data via ``read_fn``, compute per-tag analytics, and optionally export CSV/JSONL."""
        if not tags:
            raise ValueError("At least one tag must be provided to PLCDataCollector.collect().")

        max_samples = max(1, min(int(samples), 500))
        sleep_s = max(0.0, min(int(interval_ms), 10000) / 1000.0)
        max_duration_s = min(float(duration_s), 60.0) if duration_s is not None else None
        if max_duration_s is not None and max_duration_s <= 0:
            max_duration_s = 0.0

        tag_list = [dict(t) for t in tags]
        tag_keys: List[str] = []
        for idx, t in enumerate(tag_list):
            k = str(t.get("symbol") or t.get("address") or f"tag_{idx}")
            if k in tag_keys:
                k = f"{k}_{idx}"
            t["_key"] = k
            tag_keys.append(k)

        norm_thresholds: Dict[str, Dict[str, Any]] = {}
        for tk, rule in (thresholds or {}).items():
            if isinstance(rule, dict):
                norm_thresholds[tk.strip().upper()] = rule

        rows: List[Dict[str, Any]] = []
        alerts: List[Dict[str, Any]] = []
        t_start = time.perf_counter()
        wall_start = time.time()

        per_tag_values: Dict[str, List[Any]] = {k: [] for k in tag_keys}
        per_tag_errors: Dict[str, int] = {k: 0 for k in tag_keys}
        per_tag_meta: Dict[str, Dict[str, Any]] = {
            t["_key"]: {"address": t.get("address"), "symbol": t.get("symbol"), "data_type": t.get("data_type")}
            for t in tag_list
        }

        idx = 0
        while idx < max_samples:
            if max_duration_s is not None and idx > 0 and (time.perf_counter() - t_start) >= max_duration_s:
                break
            sample_ts = time.time()
            elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            br = read_fn(tag_list)
            items = br.get("items") or []

            row_vals: Dict[str, Any] = {}
            row_errs: List[str] = []
            for j, tag_spec in enumerate(tag_list):
                k = tag_spec["_key"]
                item = items[j] if j < len(items) else None
                if item and item.get("ok"):
                    val = item.get("value")
                    row_vals[k] = val
                    per_tag_values[k].append(val)
                    if item.get("data_type") and not per_tag_meta[k].get("data_type"):
                        per_tag_meta[k]["data_type"] = item.get("data_type")
                    if item.get("address"):
                        per_tag_meta[k]["address"] = item.get("address")

                    # Threshold/alarm evaluation
                    rule = (
                        norm_thresholds.get(k.upper())
                        or norm_thresholds.get(str(tag_spec.get("address") or "").strip().upper())
                        or norm_thresholds.get(str(tag_spec.get("symbol") or "").strip().upper())
                    )
                    if rule:
                        if "min" in rule and isinstance(val, (int, float)) and not isinstance(val, bool):
                            if val < float(rule["min"]):
                                alerts.append({
                                    "sample_index": idx,
                                    "timestamp": sample_ts,
                                    "tag": k,
                                    "rule": f"min<{rule['min']}",
                                    "value": val,
                                })
                        if "max" in rule and isinstance(val, (int, float)) and not isinstance(val, bool):
                            if val > float(rule["max"]):
                                alerts.append({
                                    "sample_index": idx,
                                    "timestamp": sample_ts,
                                    "tag": k,
                                    "rule": f"max>{rule['max']}",
                                    "value": val,
                                })
                        if "equals" in rule and val == rule["equals"]:
                            alerts.append({
                                "sample_index": idx,
                                "timestamp": sample_ts,
                                "tag": k,
                                "rule": f"equals=={rule['equals']}",
                                "value": val,
                            })
                else:
                    row_vals[k] = None
                    per_tag_errors[k] += 1
                    err_txt = (item or {}).get("error") or (item or {}).get("return_code") or "read_failed"
                    row_errs.append(f"{k}:{err_txt}")

            rows.append({
                "sample_index": idx,
                "timestamp": sample_ts,
                "elapsed_ms": elapsed_ms,
                "latency_ms": br.get("latency_ms", 0.0),
                "values": row_vals,
                "errors": row_errs,
            })
            idx += 1

            if idx < max_samples and sleep_s > 0.0:
                if max_duration_s is not None:
                    remaining = max_duration_s - (time.perf_counter() - t_start)
                    if remaining <= 0.0:
                        break
                    time.sleep(min(sleep_s, remaining))
                else:
                    time.sleep(sleep_s)

        total_elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

        # Compute per-tag statistics
        per_tag_stats: Dict[str, Dict[str, Any]] = {}
        for k in tag_keys:
            vals = per_tag_values[k]
            err_cnt = per_tag_errors[k]
            changes = 0
            for prev_v, cur_v in zip(vals, vals[1:]):
                if cur_v != prev_v:
                    changes += 1

            stat: Dict[str, Any] = {
                "address": per_tag_meta[k].get("address"),
                "symbol": per_tag_meta[k].get("symbol"),
                "data_type": per_tag_meta[k].get("data_type"),
                "samples_ok": len(vals),
                "samples_error": err_cnt,
                "first": vals[0] if vals else None,
                "last": vals[-1] if vals else None,
                "change_count": changes,
            }
            if vals and all(isinstance(v, bool) for v in vals):
                t_cnt = sum(1 for v in vals if v)
                rising = sum(1 for p, c in zip(vals, vals[1:]) if (not p) and c)
                falling = sum(1 for p, c in zip(vals, vals[1:]) if p and (not c))
                stat.update({
                    "true_count": t_cnt,
                    "false_count": len(vals) - t_cnt,
                    "true_ratio": round(t_cnt / len(vals), 4),
                    "rising_edges": rising,
                    "falling_edges": falling,
                })
            else:
                nums = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if nums:
                    mean_v = sum(nums) / len(nums)
                    var_v = sum((x - mean_v) ** 2 for x in nums) / len(nums)
                    stat.update({
                        "min": round(min(nums), 6),
                        "max": round(max(nums), 6),
                        "mean": round(mean_v, 6),
                        "stddev": round(math.sqrt(var_v), 6),
                        "delta": round(nums[-1] - nums[0], 6),
                    })
            per_tag_stats[k] = stat

        # Optional CSV / JSONL export
        exported_files: Dict[str, str] = {}
        fmt = (export_format or "").strip().lower()
        if fmt in ("csv", "jsonl", "both") and export_dir is not None:
            out_dir = Path(export_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            ts_tag = time.strftime("%Y%m%d_%H%M%S", time.localtime(wall_start))
            stem = f"{cls._sanitize_name(dataset_name or 'plc_data')}_{ts_tag}"
            if fmt in ("csv", "both"):
                csv_path = out_dir / f"{stem}.csv"
                with csv_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["sample_index", "timestamp", "elapsed_ms", "latency_ms", *tag_keys])
                    for r in rows:
                        writer.writerow([
                            r["sample_index"],
                            f"{r['timestamp']:.6f}",
                            r["elapsed_ms"],
                            r["latency_ms"],
                            *[r["values"].get(k) for k in tag_keys],
                        ])
                exported_files["csv"] = str(csv_path)
            if fmt in ("jsonl", "both"):
                jsonl_path = out_dir / f"{stem}.jsonl"
                with jsonl_path.open("w", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r) + "\n")
                exported_files["jsonl"] = str(jsonl_path)

        latencies = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
        avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0.0

        result: Dict[str, Any] = {
            "ok": len(rows) > 0 and any(s["samples_ok"] > 0 for s in per_tag_stats.values()),
            "tag_count": len(tag_keys),
            "sample_count": len(rows),
            "duration_ms": total_elapsed_ms,
            "avg_poll_latency_ms": avg_lat,
            "alert_count": len(alerts),
            "alerts": alerts[:100],
            "per_tag_stats": per_tag_stats,
            "exported_files": exported_files,
        }
        if include_raw_samples:
            result["samples"] = rows[:100]
            if len(rows) > 100:
                result["samples_truncated_note"] = (
                    f"Showing first 100 of {len(rows)} samples inline; full series saved to exported_files."
                )
        return result


# ---------------------------------------------------------------------------
# Deterministic Virtual S7-300/400 PLC Simulator (Offline & CI-Ready)
# ---------------------------------------------------------------------------

@dataclass
class _S7TimerState:
    preset_ms: int = 0
    remaining_ms: int = 0
    running: bool = False
    output: bool = False
    mode: str = "SD"  # SD (On-Delay), SE, SP, SS, SF
    prev_rlo: bool = False


@dataclass
class _S7CounterState:
    value: int = 0
    prev_cu: bool = False
    prev_cd: bool = False


class VirtualS7PLC:
    """Deterministic in-memory Siemens S7-300/400 CPU simulator.

    Supports:
    - Process images I, Q, M + Peripheral Analog/Digital PI/PQ (PIW/PQW) + Local Temp Stack (L) + DB1..DB65535
    - S7 Timers T0..T255 (SD, SE, SP, SS, SF + German SI/SV/SE/SS/SA, FR, R, S5T# literals)
    - S7 Counters C0..C255 / Z0..Z255 (CU/ZV, CD/ZR, S, R, FR, L, LC)
    - Authentic 32-bit ACCU1/ACCU2 bit-pattern semantics for INT, DINT, WORD, DWORD, REAL,
      ITD, DTR, RND, RND+, RND-, TRUNC, MOD, ABS, NEGI/D/R, SQR, SQRT, AW/OW/XOW/AD/OD/XOD,
      INVI/INVD, SLW/SRW/SLD/SRD/SSI/SSD, INC/DEC, TAK/PUSH/POP
    - Authentic S7 Status Word (RLO, /FC, OR, BR): SAVE, A BR, AN BR, JNB, JB, JBI, JNBI, BLD, NETWORK reset
    - International AND German S7-300 mnemonics (A/AN/U/UN, JU/JC/JCN/JNB/SPA/SPB/SPBN/SPBNB, E/A/Z)
    - Symbolic tags ("START_PB"), DATA_BLOCK initializers & symbolic DB fields (DB10.SpeedRpm),
      modular CALL FC/FB with parameter bindings & write-back
    - Direct execution of ALL IEC 61131-3 block types: STL/AWL, LAD/KOP, FBD/FUP, SCL (Structured Text),
      S7-GRAPH (SFC), and DATA_BLOCK!
    """

    def __init__(self, order_number: str = "6ES7 315-2EH14-0AB0") -> None:
        self._lock = threading.RLock()
        self.order_number = order_number
        self.cpu_mode = "RUN"
        self.scan_count = 0
        self.sim_time_ms = 0
        self.areas: Dict[str, bytearray] = {
            "I": bytearray(4096),
            "Q": bytearray(4096),
            "M": bytearray(4096),
            "PI": bytearray(4096),
            "PQ": bytearray(4096),
        }
        self._local_stack: List[bytearray] = [bytearray(512)]
        self.dbs: Dict[int, bytearray] = {1: bytearray(4096)}
        self.db_layouts: Dict[int, Dict[str, Tuple[str, str]]] = {}  # db_num -> {FIELD_UP: (s7_addr, dtype)}
        self.db_names: Dict[str, int] = {}  # SYMBOL_UP -> db_num
        self.symbols: Dict[str, Tuple[str, Optional[str]]] = {}  # SYMBOL_UP -> (addr, dtype)
        self.timers: Dict[int, _S7TimerState] = {}
        self.counters: Dict[int, _S7CounterState] = {}
        self.blocks: Dict[str, List[str]] = {}
        self.block_meta: Dict[str, Dict[str, Any]] = {}
        self.fb_statics: Dict[str, Dict[str, Any]] = {}
        self.sfc_states: Dict[str, Dict[str, Any]] = {}
        self.last_unsupported_instructions: List[str] = []
        self.diagnostics: List[Dict[str, Any]] = [
            {"event_id": 0x4302, "priority_class": 0x01, "ob_number": 100, "info1": 0x0001, "info2": 0x00000000},
            {"event_id": 0x4304, "priority_class": 0x01, "ob_number": 1, "info1": 0x0008, "info2": 0x00000000},
        ]
        self._open_db: int = 1
        self._current_tick_ms: int = 100
        self.ob35_count: int = 0
        self.ob35_interval_ms: int = 100
        self._ob35_accum_ms: int = 0

    def reset(self, clear_blocks: bool = False, clear_symbols: bool = False) -> None:
        with self._lock:
            self.areas = {
                "I": bytearray(4096),
                "Q": bytearray(4096),
                "M": bytearray(4096),
                "PI": bytearray(4096),
                "PQ": bytearray(4096),
            }
            self._local_stack = [bytearray(512)]
            self.dbs = {1: bytearray(4096)}
            self.timers.clear()
            self.counters.clear()
            self.fb_statics.clear()
            self.sfc_states.clear()
            self.scan_count = 0
            self.sim_time_ms = 0
            self.ob35_count = 0
            self._ob35_accum_ms = 0
            self.cpu_mode = "RUN"
            self.last_unsupported_instructions = []
            if clear_blocks:
                self.blocks.clear()
                self.block_meta.clear()
                self.db_layouts.clear()
                self.db_names.clear()
            if clear_symbols:
                self.symbols.clear()

    def set_symbols(self, symbols: Any) -> int:
        """Register symbol -> (address, data_type) mappings for symbolic program & scenario execution."""
        with self._lock:
            count = 0
            if isinstance(symbols, dict):
                for k, v in symbols.items():
                    ks = str(k).strip().strip('"')
                    if isinstance(v, str):
                        vs = v.strip()
                        # Support either {"START_PB": "I 0.0"} or {"I 0.0": "START_PB"}
                        try:
                            parse_s7_address(ks)
                            # key is address, value is symbol/comment
                            sym_tok = re.sub(r"[^A-Za-z0-9_]+", "_", vs).strip("_").upper()
                            if sym_tok:
                                self.symbols[sym_tok] = (ks, None)
                                count += 1
                        except Exception:
                            self.symbols[ks.upper()] = (vs, None)
                            m_db = re.fullmatch(r"DB\s*(\d+)", vs, re.IGNORECASE)
                            if m_db:
                                self.db_names[ks.upper()] = int(m_db.group(1))
                            count += 1
                    elif isinstance(v, dict) and "address" in v:
                        addr_s = str(v["address"]).strip()
                        dt_s = str(v.get("data_type") or "").strip().upper() or None
                        self.symbols[ks.upper()] = (addr_s, dt_s)
                        m_db = re.fullmatch(r"DB\s*(\d+)", addr_s, re.IGNORECASE)
                        if m_db:
                            self.db_names[ks.upper()] = int(m_db.group(1))
                        count += 1
            elif isinstance(symbols, list):
                for item in symbols:
                    if isinstance(item, dict) and item.get("symbol") and item.get("address"):
                        ks = str(item["symbol"]).strip().strip('"').upper()
                        addr_s = str(item["address"]).strip()
                        dt_s = str(item.get("data_type") or "").strip().upper() or None
                        self.symbols[ks] = (addr_s, dt_s)
                        m_db = re.fullmatch(r"DB\s*(\d+)", addr_s, re.IGNORECASE)
                        if m_db:
                            self.db_names[ks] = int(m_db.group(1))
                        count += 1
            return count

    def resolve_operand(self, ref: str, data_type: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Resolve symbolic names, DB symbolic members ("MotorDB".Speed / DB10.Speed), or raw S7 addresses."""
        s = (ref or "").strip()
        if not s:
            return s, data_type
        unq = s.strip('"').strip()
        up = unq.upper()

        # 1. Exact symbol match (e.g. "START_PB" or START_PB)
        if up in self.symbols:
            addr_s, sym_dt = self.symbols[up]
            return addr_s, data_type or sym_dt

        # 2. Symbolic DB field: "MotorDB".SpeedRpm or DB10.SpeedRpm
        if "." in s and not re.match(r"^(DB\d+\.DB[XBWD]|[IEQAML]\d+\.[0-7]|[TCZ]\d+\.Q)$", re.sub(r"\s+", "", up)):
            left, right = s.split(".", 1)
            left_clean = left.strip().strip('"').upper()
            right_clean = right.strip().strip('"').upper()
            db_num: Optional[int] = None
            m_db = re.fullmatch(r"DB\s*(\d+)", left_clean)
            if m_db:
                db_num = int(m_db.group(1))
            elif left_clean in self.db_names:
                db_num = self.db_names[left_clean]
            elif left_clean in self.symbols:
                m_s = re.fullmatch(r"DB\s*(\d+)", self.symbols[left_clean][0], re.IGNORECASE)
                if m_s:
                    db_num = int(m_s.group(1))
            if db_num is not None and db_num in self.db_layouts and right_clean in self.db_layouts[db_num]:
                f_addr, f_dt = self.db_layouts[db_num][right_clean]
                return f_addr, data_type or f_dt

        # 3. Opened DB short syntax: DBX 0.0 / DBW 2 / DBD 4
        compact = re.sub(r"\s+", "", up)
        if compact.startswith(("DBX", "DBB", "DBW", "DBD")):
            return f"DB{self._open_db}.{compact}", data_type

        return unq, data_type

    def _ensure_db(self, db_num: int, min_size: int = 4096) -> bytearray:
        buf = self.dbs.get(db_num)
        if buf is None:
            buf = bytearray(max(min_size, 4096))
            self.dbs[db_num] = buf
        elif len(buf) < min_size:
            buf.extend(bytearray(min_size - len(buf)))
        return buf

    def read_bytes(self, area_code: int, db_number: int, byte_offset: int, length: int) -> bytes:
        with self._lock:
            if area_code == 0x1D:  # Timer
                t = self.timers.get(byte_offset, _S7TimerState())
                return struct.pack(">H", max(0, min(65535, int(t.remaining_ms // 10))))
            if area_code == 0x1C:  # Counter
                c = self.counters.get(byte_offset, _S7CounterState())
                return struct.pack(">H", max(0, min(999, int(c.value))))
            buf = self._get_area_buffer(area_code, db_number, byte_offset + length)
            return bytes(buf[byte_offset:byte_offset + length])

    def write_bytes(self, area_code: int, db_number: int, byte_offset: int, payload: bytes) -> None:
        with self._lock:
            if area_code == 0x1D:
                t = self.timers.setdefault(byte_offset, _S7TimerState())
                val = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 0
                t.remaining_ms = val * 10
                return
            if area_code == 0x1C:
                c = self.counters.setdefault(byte_offset, _S7CounterState())
                c.value = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 0
                return
            buf = self._get_area_buffer(area_code, db_number, byte_offset + len(payload))
            buf[byte_offset:byte_offset + len(payload)] = payload

    def read_bit(self, area_code: int, db_number: int, byte_offset: int, bit_offset: int) -> bool:
        with self._lock:
            if area_code == 0x1D:
                t = self.timers.get(byte_offset)
                return bool(t.output) if t else False
            if area_code == 0x1C:
                c = self.counters.get(byte_offset)
                return bool(c and c.value > 0)
            buf = self._get_area_buffer(area_code, db_number, byte_offset + 1)
            return bool((buf[byte_offset] >> bit_offset) & 1)

    def write_bit(self, area_code: int, db_number: int, byte_offset: int, bit_offset: int, val: bool) -> None:
        with self._lock:
            buf = self._get_area_buffer(area_code, db_number, byte_offset + 1)
            if val:
                buf[byte_offset] |= (1 << bit_offset)
            else:
                buf[byte_offset] &= ~(1 << bit_offset)

    def _get_area_buffer(self, area_code: int, db_number: int, min_size: int) -> bytearray:
        if area_code == 0x84:
            return self._ensure_db(db_number, min_size)
        if area_code == 0x86:
            buf = self._local_stack[-1] if self._local_stack else bytearray(512)
            if len(buf) < min_size:
                buf.extend(bytearray(min_size - len(buf)))
            return buf
        name = {0x80: "PI", 0x81: "I", 0x82: "Q", 0x83: "M", 0x85: "PQ"}.get(area_code)
        if name is None:
            raise ValueError(f"Unsupported S7 area code: 0x{area_code:02X}")
        buf = self.areas[name]
        if len(buf) < min_size:
            buf.extend(bytearray(min_size - len(buf)))
        return buf

    def read_address(self, address: str, data_type: Optional[str] = None) -> Any:
        resolved, eff_dt = self.resolve_operand(address, data_type)
        addr = parse_s7_address(resolved, eff_dt)
        if addr.data_type == "BOOL":
            return self.read_bit(addr.area_code, addr.db_number, addr.byte_offset, addr.bit_offset)
        raw = self.read_bytes(addr.area_code, addr.db_number, addr.byte_offset, addr.byte_length)
        return decode_s7_value(addr, raw)

    def write_address(self, address: str, value: Any, data_type: Optional[str] = None) -> None:
        resolved, eff_dt = self.resolve_operand(address, data_type)
        # Auto-infer signed/float encoding when caller passes Python float or negative int without explicit data_type
        if eff_dt is None:
            if isinstance(value, float):
                eff_dt = "REAL"
            elif isinstance(value, int) and not isinstance(value, bool) and value < 0:
                tmp = parse_s7_address(resolved)
                if tmp.byte_length == 2:
                    eff_dt = "INT"
                elif tmp.byte_length == 4:
                    eff_dt = "DINT"
        addr = parse_s7_address(resolved, eff_dt)
        if addr.data_type == "BOOL":
            self.write_bit(addr.area_code, addr.db_number, addr.byte_offset, addr.bit_offset, bool(value))
            return
        raw = encode_s7_value(addr, value)
        self.write_bytes(addr.area_code, addr.db_number, addr.byte_offset, raw)

    def set_mode(self, mode: str) -> str:
        with self._lock:
            m = mode.strip().upper()
            if m in ("RUN", "START", "WARM_RESTART", "RUN-P", "RUN_P"):
                self.cpu_mode = "RUN"
                self.diagnostics.insert(0, {
                    "event_id": 0x4304, "priority_class": 0x01, "ob_number": 1, "info1": 0x0008, "info2": self.scan_count
                })
            elif m == "STOP":
                self.cpu_mode = "STOP"
                self.diagnostics.insert(0, {
                    "event_id": 0x430E, "priority_class": 0x01, "ob_number": 0, "info1": 0x0004, "info2": self.scan_count
                })
            self.diagnostics = self.diagnostics[:32]
            return self.cpu_mode

    @staticmethod
    def _detect_source_language(source: str, explicit: Optional[str] = None) -> str:
        if explicit and explicit.strip().upper() not in ("", "AUTO"):
            up = explicit.strip().upper()
            if up in ("LAD", "KOP", "FBD", "FUP", "STL", "AWL"):
                return "AWL"
            return up
        upper = source.upper()
        if re.search(r"^\s*DATA_BLOCK\s+DB\s*\d+", upper, re.MULTILINE):
            return "DB"
        if re.search(r"\bINITIAL_STEP\b", upper) and re.search(r"\bTRANSITION\b", upper):
            return "GRAPH"
        # SCL indicators in body (after BEGIN, or standalone statements)
        body_part = upper.split("BEGIN", 1)[1] if "BEGIN" in upper else upper
        if re.search(r"\b(END_IF|END_CASE|END_FOR|END_WHILE|END_REPEAT)\b", body_part):
            return "SCL"
        if ":=" in body_part and not re.search(r"\bCALL\s+(?:FC|FB|SFC|SFB)\b", body_part):
            return "SCL"
        return "AWL"

    def _load_data_block(self, block_name: str, source: str) -> Dict[str, Any]:
        from .iec_languages import compute_s7_struct_layout

        m_db = re.search(r"DATA_BLOCK\s+DB\s*(\d+)", source, re.IGNORECASE)
        if m_db:
            db_num = int(m_db.group(1))
        else:
            m_bn = re.search(r"(\d+)", block_name)
            db_num = int(m_bn.group(1)) if m_bn else 1

        # Parse optional TITLE = Name
        m_title = re.search(r"^\s*TITLE\s*=\s*(.+)$", source, re.IGNORECASE | re.MULTILINE)
        if m_title:
            tname = m_title.group(1).strip().strip('"').upper()
            if tname and re.fullmatch(r"[A-Z_][A-Z0-9_]*", tname):
                self.db_names[tname] = db_num

        # Parse STRUCT ... END_STRUCT fields
        fields: List[Dict[str, Any]] = []
        m_struct = re.search(r"\bSTRUCT\b([\s\S]*?)\bEND_STRUCT\b", source, re.IGNORECASE)
        if m_struct:
            for raw in m_struct.group(1).splitlines():
                ln = raw.split("//", 1)[0].strip()
                if not ln:
                    continue
                m_f = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z0-9_]+)(?:\s*:=\s*(.+?))?\s*;?\s*$", ln)
                if m_f:
                    fields.append({
                        "name": m_f.group(1),
                        "type": m_f.group(2).upper(),
                        "initial": (m_f.group(3) or "").strip(),
                    })

        layout_res = compute_s7_struct_layout(fields, db_number=db_num) if fields else {"layout": [], "total_bytes": 0}
        layout = layout_res.get("layout", [])
        total_bytes = int(layout_res.get("total_bytes", 0))
        self._ensure_db(db_num, max(4096, total_bytes))
        fmap: Dict[str, Tuple[str, str]] = {}
        init_overrides: Dict[str, str] = {}

        # Parse BEGIN ... END_DATA_BLOCK initial value assignments
        m_begin = re.search(r"\bBEGIN\b([\s\S]*?)\bEND_DATA_BLOCK\b", source, re.IGNORECASE)
        if m_begin:
            for raw in m_begin.group(1).splitlines():
                ln = raw.split("//", 1)[0].strip().rstrip(";")
                if ":=" in ln:
                    k, v = ln.split(":=", 1)
                    init_overrides[k.strip().upper()] = v.strip()

        for item in layout:
            fname = item["name"]
            ftype = item["type"].upper()
            b_off = item["byte_offset"]
            bit_off = item["bit_offset"]
            if ftype == "BOOL":
                s7_addr = f"DB{db_num}.DBX{b_off}.{bit_off}"
            elif item["size_bytes"] == 1:
                s7_addr = f"DB{db_num}.DBB{b_off}"
            elif item["size_bytes"] == 2:
                s7_addr = f"DB{db_num}.DBW{b_off}"
            else:
                s7_addr = f"DB{db_num}.DBD{b_off}"
            fmap[fname.upper()] = (s7_addr, ftype)

            raw_init = init_overrides.get(fname.upper(), item.get("initial") or "")
            if raw_init:
                val: Any = raw_init
                up_i = raw_init.upper()
                if up_i == "TRUE":
                    val = True
                elif up_i == "FALSE":
                    val = False
                elif re.fullmatch(r"[+-]?\d+\.\d+(?:[eE][+-]?\d+)?", raw_init):
                    val = float(raw_init)
                elif re.fullmatch(r"[+-]?\d+", raw_init):
                    val = int(raw_init)
                try:
                    self.write_address(s7_addr, val, data_type=ftype)
                except Exception:
                    pass

        self.db_layouts[db_num] = fmap
        key = f"DB{db_num}"
        self.block_meta[key] = {"language": "DB", "db_number": db_num, "fields": list(fmap.keys()), "total_bytes": total_bytes}
        return {
            "ok": True,
            "block": key,
            "language": "DB",
            "detected_language": "DB",
            "db_number": db_num,
            "field_count": len(fmap),
            "fields": {k: {"address": v[0], "type": v[1]} for k, v in fmap.items()},
            "instruction_count": 0,
            "unsupported_instructions": [],
            "loaded_blocks": sorted(set(list(self.blocks.keys()) + list(self.block_meta.keys()))),
        }

    def load_block(
        self,
        block_name: str,
        awl_source: str,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parse and load an AWL/STL/LAD/FBD, SCL, S7-GRAPH, or DATA_BLOCK into virtual CPU memory."""
        from .scl_graph_sim import parse_graph_block, parse_scl_block

        with self._lock:
            key = re.sub(r"\s+", "", block_name.strip().upper())
            lang = self._detect_source_language(awl_source, language)

            if lang == "DB":
                return self._load_data_block(key, awl_source)

            if lang == "SCL":
                parsed_scl = parse_scl_block(key, awl_source)
                self.blocks[key] = ["__SCL__"]
                self.block_meta[key] = {
                    "language": "SCL",
                    "parsed_scl": parsed_scl,
                    "var_decls": parsed_scl.var_decls,
                }
                return {
                    "ok": True,
                    "block": key,
                    "language": "SCL",
                    "detected_language": "SCL",
                    "statement_count": len(parsed_scl.body_stmts),
                    "instruction_count": len(parsed_scl.body_stmts),
                    "unsupported_instructions": [],
                    "loaded_blocks": sorted(self.blocks.keys()),
                }

            if lang == "GRAPH":
                parsed_gr7 = parse_graph_block(key, awl_source)
                self.blocks[key] = ["__GRAPH__"]
                self.block_meta[key] = {
                    "language": "GRAPH",
                    "parsed_graph": parsed_gr7,
                }
                for old_k in [k for k in list(self.sfc_states.keys()) if k == key or k.startswith(f"{key}:")]:
                    self.sfc_states.pop(old_k, None)
                return {
                    "ok": True,
                    "block": key,
                    "language": "GRAPH",
                    "detected_language": "GRAPH",
                    "step_count": len(parsed_gr7.steps),
                    "transition_count": len(parsed_gr7.transitions),
                    "initial_steps": parsed_gr7.initial_steps,
                    "instruction_count": len(parsed_gr7.steps) + len(parsed_gr7.transitions),
                    "unsupported_instructions": [],
                    "loaded_blocks": sorted(self.blocks.keys()),
                }

            # Standard AWL / STL / LAD / FBD parsing (with VAR declarations & multiline CALL merging)
            instructions: List[str] = []
            var_decls: Dict[str, Tuple[str, str]] = {}  # VAR_UPPER -> (section, dtype)
            cur_var_sec: Optional[str] = None
            in_body = False
            saw_begin = False
            pending_call: Optional[List[str]] = None

            for raw_line in awl_source.splitlines():
                line = raw_line.split("//", 1)[0].strip()
                if not line:
                    continue
                upper = line.upper()

                if pending_call is not None:
                    pending_call.append(line)
                    if ")" in line:
                        merged = " ".join(pending_call).rstrip(";").strip()
                        instructions.append(merged)
                        pending_call = None
                    continue

                if upper in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR", "VAR_STAT"):
                    cur_var_sec = upper
                    continue
                if upper == "END_VAR":
                    cur_var_sec = None
                    continue
                if cur_var_sec and not in_body:
                    m_decl = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z0-9_]+)", line)
                    if m_decl:
                        var_decls[m_decl.group(1).upper()] = (cur_var_sec, m_decl.group(2).upper())
                    continue

                if upper == "BEGIN":
                    in_body = True
                    saw_begin = True
                    continue
                if upper == "NETWORK":
                    in_body = True
                    saw_begin = True
                    instructions.append("__NETWORK__")
                    continue
                if upper.startswith(("TITLE", "VERSION", "AUTHOR", "FAMILY", "NAME", "ORGANIZATION_BLOCK", "FUNCTION", "FUNCTION_BLOCK", "KNOW_HOW_PROTECT")):
                    continue
                if upper.startswith("END_"):
                    in_body = False
                    continue
                if in_body or not saw_begin:
                    # Multiline CALL FC 1 ( ... )
                    if upper.startswith("CALL ") and "(" in line and ")" not in line:
                        pending_call = [line]
                        continue
                    stmt = line.rstrip(";").strip()
                    if stmt:
                        instructions.append(stmt)

            if pending_call:
                instructions.append(" ".join(pending_call).rstrip(";").strip())

            # Detect whether this AWL block uses German mnemonics
            german_markers = 0
            for ins in instructions:
                if ins == "__NETWORK__":
                    continue
                tok0 = ins.split(None, 1)[0].upper()
                if tok0 in ("U", "UN", "U(", "UN(", "SPA", "SPB", "SPBN", "SPBNB", "ZV", "ZR", "BEA", "BEB", "SI", "SV", "SA"):
                    german_markers += 1
            german_mode = german_markers > 0

            # Pre-check unsupported opcodes so AI agents get immediate feedback
            unsupported = self._find_unsupported_opcodes(instructions)

            self.blocks[key] = instructions
            self.block_meta[key] = {
                "language": "AWL",
                "german_mode": german_mode,
                "var_decls": var_decls,
                "unsupported_instructions": unsupported,
            }
            return {
                "ok": True,
                "block": key,
                "language": "AWL",
                "detected_language": "AWL",
                "german_mnemonics": german_mode,
                "instruction_count": sum(1 for x in instructions if x != "__NETWORK__"),
                "unsupported_instructions": unsupported,
                "loaded_blocks": sorted(self.blocks.keys()),
            }

    @staticmethod
    def _known_opcodes() -> set:
        return {
            "__NETWORK__", "NOP", "BLD", "BEU", "BEC", "BEA", "BEB", "BE",
            "SET", "CLR", "NOT", "SAVE", "OPN", "AUF", "CALL", "UC", "CC",
            "A(", "AN(", "O(", "ON(", "X(", "XN(", "U(", "UN(", ")",
            "A", "AN", "O", "ON", "X", "XN", "U", "UN",
            "FP", "FN", "=", "S", "R", "FR",
            "SD", "SS", "SP", "SE", "SF", "SI", "SV", "SA",
            "CU", "CD", "ZV", "ZR",
            "L", "LC", "T", "TAK", "PUSH", "POP",
            "+I", "-I", "*I", "/I", "+D", "-D", "*D", "/D", "MOD",
            "+R", "-R", "*R", "/R",
            "ITD", "DTR", "RND", "RND+", "RND-", "TRUNC", "BTI", "ITB", "BTD", "DTB",
            "NEGI", "NEGD", "NEGR", "INVI", "INVD", "ABS", "SQR", "SQRT",
            "AW", "OW", "XOW", "AD", "OD", "XOD",
            "SLW", "SRW", "SLD", "SRD", "SSI", "SSD", "INC", "DEC",
            "==I", "<>I", ">I", ">=I", "<I", "<=I",
            "==D", "<>D", ">D", ">=D", "<D", "<=D",
            "==R", "<>R", ">R", ">=R", "<R", "<=R",
            "JU", "JC", "JCN", "JNB", "JB", "JBI", "JNBI",
            "SPA", "SPB", "SPBN", "SPBNB", "SPBI", "SPNBI",
        }

    def _find_unsupported_opcodes(self, instructions: Sequence[str]) -> List[str]:
        known = self._known_opcodes()
        unknown: List[str] = []
        for raw in instructions:
            if raw == "__NETWORK__":
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", raw)
            stmt = m.group(2).strip() if m else raw.strip()
            if not stmt:
                continue
            op = stmt.split(None, 1)[0].upper()
            if op not in known and op not in unknown:
                unknown.append(op)
        return unknown

    # --- Authentic 32-bit S7 Accumulator Bit-Pattern Helpers ---
    @staticmethod
    def _i32_to_u32(val: int) -> int:
        return int(val) & 0xFFFFFFFF

    @staticmethod
    def _u32_to_i16(u: int) -> int:
        v = u & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    @staticmethod
    def _u32_to_i32(u: int) -> int:
        v = u & 0xFFFFFFFF
        return v - 0x100000000 if v >= 0x80000000 else v

    @staticmethod
    def _f32_to_u32(f: float) -> int:
        return struct.unpack(">I", struct.pack(">f", float(f)))[0]

    @staticmethod
    def _u32_to_f32(u: int) -> float:
        return struct.unpack(">f", struct.pack(">I", u & 0xFFFFFFFF))[0]

    @staticmethod
    def _parse_s5time_ms(token: str) -> int:
        tok = token.strip().upper()
        if tok.startswith("S5TIME#"):
            tok = tok[7:]
        elif tok.startswith("S5T#"):
            tok = tok[4:]
        elif tok.startswith("TIME#"):
            tok = tok[5:]
        elif tok.startswith("T#"):
            tok = tok[2:]
        total_ms = 0
        for num_s, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(MS|S|M|H)", tok):
            num = float(num_s)
            mult = {"MS": 1, "S": 1000, "M": 60000, "H": 3600000}[unit]
            total_ms += int(round(num * mult))
        if total_ms == 0 and tok.isdigit():
            total_ms = int(tok)
        return total_ms

    def _load_operand_u32(
        self,
        operand: str,
        param_env: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, str]:
        """Load literal or memory operand into an authentic 32-bit S7 ACCU1 uint32 bit-pattern."""
        op = operand.strip()
        up = op.upper()
        if up.startswith(("S5T#", "S5TIME#", "T#", "TIME#")):
            return self._i32_to_u32(self._parse_s5time_ms(up)), "S5TIME"
        if up.startswith("C#"):
            return self._i32_to_u32(int(up[2:])), "INT"
        if up.startswith(("W#16#", "DW#16#", "B#16#", "16#")):
            return self._i32_to_u32(int(up.split("#")[-1].replace("_", ""), 16)), "HEX"
        if up.startswith("2#"):
            return self._i32_to_u32(int(up[2:].replace("_", ""), 2)), "BIN"
        if up.startswith("L#"):
            return self._i32_to_u32(int(up[2:])), "DINT"
        if re.fullmatch(r"[+-]?\d+\.\d+(?:[eE][+-]?\d+)?", up):
            return self._f32_to_u32(float(up)), "REAL"
        if re.fullmatch(r"[+-]?\d+", up):
            return self._i32_to_u32(int(up)), "INT"

        # Local parameter (#Param)
        if param_env is not None:
            pk = op.lstrip("#").upper()
            if pk in param_env:
                pv = param_env[pk]
                if isinstance(pv, float):
                    return self._f32_to_u32(pv), "REAL"
                return self._i32_to_u32(int(pv)), "PARAM"

        resolved, _ = self.resolve_operand(op)
        addr = parse_s7_address(resolved)
        if addr.area in ("T", "C"):
            raw = self.read_bytes(addr.area_code, 0, addr.byte_offset, 2)
            return struct.unpack(">H", raw)[0], addr.area
        raw = self.read_bytes(addr.area_code, addr.db_number, addr.byte_offset, addr.byte_length)
        if addr.byte_length == 1:
            return raw[0], "BYTE"
        if addr.byte_length == 2:
            return struct.unpack(">H", raw)[0], "WORD"
        return struct.unpack(">I", raw[:4])[0], "DWORD"

    def _store_u32_to_operand(
        self,
        operand: str,
        accu1_u32: int,
        param_env: Optional[Dict[str, Any]] = None,
        param_types: Optional[Dict[str, str]] = None,
    ) -> None:
        op = operand.strip()
        if param_env is not None:
            pk = op.lstrip("#").upper()
            if pk in param_env:
                pt = (param_types or {}).get(pk, "INT").upper()
                if pt == "REAL":
                    param_env[pk] = self._u32_to_f32(accu1_u32)
                elif pt == "INT":
                    param_env[pk] = self._u32_to_i16(accu1_u32)
                elif pt == "DINT":
                    param_env[pk] = self._u32_to_i32(accu1_u32)
                elif pt == "BOOL":
                    param_env[pk] = bool(accu1_u32 & 1)
                else:
                    param_env[pk] = accu1_u32 & 0xFFFF
                return

        resolved, _ = self.resolve_operand(op)
        addr = parse_s7_address(resolved)
        if addr.byte_length == 1:
            self.write_bytes(addr.area_code, addr.db_number, addr.byte_offset, bytes([accu1_u32 & 0xFF]))
        elif addr.byte_length == 2:
            self.write_bytes(addr.area_code, addr.db_number, addr.byte_offset, struct.pack(">H", accu1_u32 & 0xFFFF))
        else:
            self.write_bytes(addr.area_code, addr.db_number, addr.byte_offset, struct.pack(">I", accu1_u32 & 0xFFFFFFFF))

    def _eval_bit_operand(self, operand: str, param_env: Optional[Dict[str, Any]] = None, br_bit: bool = False) -> bool:
        raw = operand.strip()
        if raw.upper() == "BR":
            return bool(br_bit)
        if param_env is not None:
            pk = raw.lstrip("#").upper()
            if pk in param_env:
                return bool(param_env[pk])
        resolved, _ = self.resolve_operand(raw)
        compact = re.sub(r"\s+", "", resolved.upper())
        if compact.startswith("T") and compact[1:].isdigit():
            t = self.timers.get(int(compact[1:]))
            return bool(t.output) if t else False
        if compact.startswith(("C", "Z")) and compact[1:].isdigit():
            c = self.counters.get(int(compact[1:]))
            return bool(c and c.value > 0)
        return bool(self.read_address(resolved, data_type="BOOL"))

    def _write_bit_operand(self, operand: str, val: bool, param_env: Optional[Dict[str, Any]] = None) -> None:
        raw = operand.strip()
        if param_env is not None:
            pk = raw.lstrip("#").upper()
            if pk in param_env:
                param_env[pk] = bool(val)
                return
        resolved, _ = self.resolve_operand(raw)
        self.write_address(resolved, bool(val), data_type="BOOL")

    def _advance_timers(self, tick_ms: int) -> None:
        for t in self.timers.values():
            if not t.running:
                continue
            t.remaining_ms = max(0, t.remaining_ms - tick_ms)
            if t.mode in ("SD", "SS"):
                if t.remaining_ms == 0:
                    t.output = True
                    t.running = False
            elif t.mode in ("SP", "SE", "SF"):
                if t.remaining_ms == 0:
                    t.output = False
                    t.running = False

    def _parse_call_statement(self, arg: str) -> Tuple[str, Optional[int], Dict[str, str]]:
        """Parse 'FC 1 (Start := I 0.0, Motor := Q 0.0)' or 'FB 1, DB 1 (...)'."""
        params: Dict[str, str] = {}
        head = arg
        if "(" in arg:
            idx = arg.index("(")
            head = arg[:idx].strip()
            body = arg[idx + 1:].rsplit(")", 1)[0]
            for piece in body.split(","):
                if ":=" in piece:
                    k, v = piece.split(":=", 1)
                    params[k.strip().lstrip("#").upper()] = v.strip()
        parts = [p.strip() for p in head.split(",") if p.strip()]
        target = re.sub(r"\s+", "", parts[0].strip('"').upper()) if parts else ""
        # Resolve symbolic block name if present
        if target in self.symbols:
            target = re.sub(r"\s+", "", self.symbols[target][0].upper())
        inst_db: Optional[int] = None
        if len(parts) > 1:
            m_db = re.search(r"DB\s*(\d+)", parts[1], re.IGNORECASE)
            if m_db:
                inst_db = int(m_db.group(1))
        return target, inst_db, params

    def _execute_block(
        self,
        block_name: str,
        depth: int = 0,
        call_params: Optional[Dict[str, Any]] = None,
        instance_db: Optional[int] = None,
        raw_call_params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        from .s7_stdlib import execute_stdlib_block, get_stdlib_schema, resolve_stdlib_block_name
        from .scl_graph_sim import execute_graph_block, execute_scl_block

        if depth > 16:
            return {}
        key = re.sub(r"\s+", "", block_name.strip().upper())
        meta = self.block_meta.get(key, {})
        lang = meta.get("language", "AWL")

        # --- SCL Execution ---
        if lang == "SCL" and meta.get("parsed_scl") is not None:
            stat_key = f"{key}:DB{instance_db}" if instance_db else key
            stat_store = self.fb_statics.setdefault(stat_key, {})

            def _call_cb(callee: str, kw: Dict[str, Any]) -> Any:
                c_blk = callee
                c_db: Optional[int] = None
                if "." in callee:
                    left_c, right_c = callee.rsplit(".", 1)
                    right_clean = right_c.strip().strip('"')
                    m_cdb = re.fullmatch(r"DB\s*(\d+)", right_clean, re.IGNORECASE)
                    if m_cdb:
                        c_blk = left_c
                        c_db = int(m_cdb.group(1))
                    elif right_clean.upper() in self.db_names:
                        c_blk = left_c
                        c_db = self.db_names[right_clean.upper()]
                out = self._execute_block(c_blk, depth=depth + 1, call_params=kw, instance_db=c_db)
                c_key = re.sub(r"\s+", "", c_blk.strip().upper())
                c_decls = (self.block_meta.get(c_key) or {}).get("var_decls") or get_stdlib_schema(c_key) or {}
                out_only = {
                    k: v
                    for k, v in out.items()
                    if k != "__RETURN__" and (not c_decls or (c_decls.get(k, ("", ""))[0] in ("VAR_OUTPUT", "VAR_IN_OUT")))
                }
                return {"__RETURN__": out.get("__RETURN__", 0), "__OUTPUTS__": out_only}

            return execute_scl_block(
                meta["parsed_scl"],
                read_ref=lambda r: self.read_address(r),
                write_ref=lambda r, v: self.write_address(r, v),
                static_store=stat_store,
                call_block_cb=_call_cb,
                param_bindings=call_params,
                parse_time_ms=self._parse_s5time_ms,
            )

        # --- S7-GRAPH (SFC) Execution ---
        if lang == "GRAPH" and meta.get("parsed_graph") is not None:
            st_key = f"{key}:DB{instance_db}" if instance_db else key
            sfc_state = self.sfc_states.setdefault(st_key, {})
            return execute_graph_block(
                meta["parsed_graph"],
                sfc_state=sfc_state,
                read_ref=lambda r: self.read_address(r),
                write_ref=lambda r, v: self.write_address(r, v),
                tick_ms=self._current_tick_ms,
            )

        instrs = self.blocks.get(key, [])
        if not instrs:
            # Dispatch built-in Siemens S7-300 standard/system library blocks (SFB0-5, FC105-106, FB41, SFC20/21/64)
            if resolve_stdlib_block_name(key):
                return execute_stdlib_block(
                    self,
                    key,
                    call_params or {},
                    raw_param_exprs=raw_call_params,
                    instance_db=instance_db,
                    tick_ms=self._current_tick_ms,
                )
            return {}

        # Prepare local temp frame + parameter bindings
        self._local_stack.append(bytearray(512))
        var_decls: Dict[str, Tuple[str, str]] = meta.get("var_decls") or {}
        param_env: Dict[str, Any] = {}
        param_types: Dict[str, str] = {}
        stat_key = f"{key}:DB{instance_db}" if instance_db else key
        stat_store = self.fb_statics.setdefault(stat_key, {})

        for vname, (sec, vtype) in var_decls.items():
            param_types[vname] = vtype
            if sec in ("VAR", "VAR_STAT"):
                if vname not in stat_store:
                    stat_store[vname] = False if vtype == "BOOL" else (0.0 if vtype == "REAL" else 0)
                param_env[vname] = stat_store[vname]
            else:
                param_env[vname] = False if vtype == "BOOL" else (0.0 if vtype == "REAL" else 0)
        for pk, pv in (call_params or {}).items():
            param_env[pk.lstrip("#").upper()] = pv

        german_mode = bool(meta.get("german_mode", False))

        # Pre-index labels
        labels: Dict[str, int] = {}
        clean_instrs: List[str] = []
        for idx, raw in enumerate(instrs):
            if raw == "__NETWORK__":
                clean_instrs.append("__NETWORK__")
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", raw)
            if m:
                labels[m.group(1).upper()] = idx
                clean_instrs.append(m.group(2).strip())
            else:
                clean_instrs.append(raw)

        rlo = False
        fc_bit = False  # /FC: False at start of logic chain
        or_bit = False  # S7 OR status bit for bare 'O' combiner
        br_bit = False  # Binary Result (BR) status bit
        accu1: int = 0  # Authentic 32-bit unsigned bit pattern
        accu2: int = 0
        accu3: int = 0
        accu4: int = 0
        nest_stack: List[Tuple[str, bool, bool, bool]] = []

        def _flush_bare_or() -> None:
            nonlocal rlo, or_bit
            if or_bit:
                rlo = rlo or or_bit
                or_bit = False

        pc = 0
        steps = 0
        max_steps = max(4000, len(clean_instrs) * 25)

        try:
            while pc < len(clean_instrs) and steps < max_steps:
                steps += 1
                stmt = clean_instrs[pc]
                pc += 1
                if not stmt:
                    continue
                if stmt == "__NETWORK__":
                    rlo = False
                    fc_bit = False
                    or_bit = False
                    nest_stack.clear()
                    continue

                parts = stmt.split(None, 1)
                op = parts[0].upper()
                arg = parts[1].strip() if len(parts) > 1 else ""

                # Normalize German mnemonics -> canonical International S7 opcodes
                if op == "U":
                    op = "A"
                elif op == "UN":
                    op = "AN"
                elif op == "U(":
                    op = "A("
                elif op == "UN(":
                    op = "AN("
                elif op == "SPA":
                    op = "JU"
                elif op == "SPB":
                    op = "JC"
                elif op == "SPBN":
                    op = "JCN"
                elif op == "SPBNB":
                    op = "JNB"
                elif op == "SPBI":
                    op = "JBI"
                elif op == "SPNBI":
                    op = "JNBI"
                elif op == "ZV":
                    op = "CU"
                elif op == "ZR":
                    op = "CD"
                elif op == "BEA":
                    op = "BEU"
                elif op == "BEB":
                    op = "BEC"
                elif op == "AUF":
                    op = "OPN"
                elif op == "SI":
                    op = "SP"
                elif op == "SV":
                    op = "SE"
                elif op == "SA":
                    op = "SF"
                elif op == "SE" and german_mode:
                    op = "SD"

                if op in ("NOP", "BLD", "FR"):
                    continue
                if op in ("BEU", "BE"):
                    break
                if op == "BEC":
                    _flush_bare_or()
                    if rlo:
                        break
                    fc_bit = False
                    continue
                if op == "SET":
                    rlo = True
                    fc_bit = False
                    or_bit = False
                    continue
                if op == "CLR":
                    rlo = False
                    fc_bit = False
                    or_bit = False
                    continue
                if op == "NOT":
                    rlo = not rlo
                    continue
                if op == "SAVE":
                    _flush_bare_or()
                    br_bit = rlo
                    continue
                if op == "OPN":
                    m_db = re.match(r"^DB\s*(\d+)$", arg, re.IGNORECASE)
                    if m_db:
                        self._open_db = int(m_db.group(1))
                    continue
                if op in ("CALL", "UC", "CC"):
                    if op == "CC":
                        _flush_bare_or()
                        if not rlo:
                            fc_bit = False
                            continue
                    from .s7_stdlib import get_stdlib_schema, parse_iec_time_ms
                    target_blk, inst_db, param_map = self._parse_call_statement(arg)
                    bound_in: Dict[str, Any] = {}
                    out_targets: Dict[str, str] = {}
                    callee_decls = (self.block_meta.get(target_blk) or {}).get("var_decls") or get_stdlib_schema(target_blk) or {}
                    for pname, pexpr in param_map.items():
                        decl = callee_decls.get(pname)
                        sec = decl[0] if decl else "VAR_INPUT"
                        if sec in ("VAR_OUTPUT", "VAR_IN_OUT"):
                            out_targets[pname] = pexpr
                        if sec != "VAR_OUTPUT":
                            try:
                                if re.match(r"^P#", pexpr, re.IGNORECASE):
                                    bound_in[pname] = pexpr
                                elif re.match(r"^(?:S5TIME|S5T|TIME|T)#", pexpr, re.IGNORECASE):
                                    bound_in[pname] = parse_iec_time_ms(pexpr)
                                elif re.fullmatch(r"TRUE|FALSE", pexpr, re.IGNORECASE):
                                    bound_in[pname] = pexpr.upper() == "TRUE"
                                elif re.fullmatch(r"[+-]?\d+\.\d+(?:[eE][+-]?\d+)?", pexpr):
                                    bound_in[pname] = float(pexpr)
                                elif re.fullmatch(r"[+-]?\d+", pexpr):
                                    bound_in[pname] = int(pexpr)
                                elif pexpr.lstrip("#").upper() in param_env:
                                    bound_in[pname] = param_env[pexpr.lstrip("#").upper()]
                                else:
                                    dt_hint = decl[1] if decl else None
                                    bound_in[pname] = self.read_address(pexpr, data_type=dt_hint)
                            except Exception:
                                bound_in[pname] = 0
                    callee_out = self._execute_block(
                        target_blk,
                        depth=depth + 1,
                        call_params=bound_in,
                        instance_db=inst_db,
                        raw_call_params=param_map,
                    )
                    for pname, dest_expr in out_targets.items():
                        if pname in callee_out:
                            val_out = callee_out[pname]
                            if dest_expr.lstrip("#").upper() in param_env:
                                param_env[dest_expr.lstrip("#").upper()] = val_out
                            else:
                                decl = callee_decls.get(pname)
                                dt_hint = decl[1] if decl else None
                                self.write_address(dest_expr, val_out, data_type=dt_hint)
                    fc_bit = False
                    or_bit = False
                    continue

                # Parentheses nesting
                if op in ("A(", "AN(", "O(", "ON(", "X(", "XN("):
                    nest_stack.append((op, rlo, fc_bit, or_bit))
                    rlo = False
                    fc_bit = False
                    or_bit = False
                    continue
                if op == ")":
                    _flush_bare_or()
                    if nest_stack:
                        n_op, prev_rlo, prev_fc, prev_or = nest_stack.pop()
                        val = not rlo if n_op in ("AN(", "ON(", "XN(") else rlo
                        if not prev_fc:
                            rlo = val
                        elif n_op.startswith("A"):
                            rlo = prev_rlo and val
                        elif n_op.startswith("O"):
                            rlo = prev_rlo or val
                        else:
                            rlo = prev_rlo ^ val
                        or_bit = prev_or
                        fc_bit = True
                    continue

                # Bare OR combiner (O with no argument in S7-300 STL: OR_bit = OR_bit | RLO, /FC = 0)
                if op == "O" and not arg:
                    or_bit = or_bit or rlo
                    fc_bit = False
                    continue

                # Bit logic instructions
                if op in ("A", "AN", "O", "ON", "X", "XN"):
                    val = self._eval_bit_operand(arg, param_env=param_env, br_bit=br_bit)
                    if op in ("AN", "ON", "XN"):
                        val = not val
                    if not fc_bit:
                        rlo = val
                    elif op in ("A", "AN"):
                        rlo = rlo and val
                    elif op in ("O", "ON"):
                        _flush_bare_or()
                        rlo = rlo or val
                    else:
                        _flush_bare_or()
                        rlo = rlo ^ val
                    fc_bit = True
                    continue

                # Edge detection
                if op in ("FP", "FN"):
                    _flush_bare_or()
                    prev = self._eval_bit_operand(arg, param_env=param_env, br_bit=br_bit)
                    self._write_bit_operand(arg, rlo, param_env=param_env)
                    rlo = (rlo and not prev) if op == "FP" else ((not rlo) and prev)
                    fc_bit = True
                    continue

                # Output assignments terminate logic chain (/FC = 0)
                if op == "=":
                    _flush_bare_or()
                    self._write_bit_operand(arg, rlo, param_env=param_env)
                    fc_bit = False
                    continue
                if op == "S":
                    _flush_bare_or()
                    arg_c = re.sub(r"\s+", "", arg.upper())
                    if arg_c.startswith(("C", "Z")) and arg_c[1:].isdigit():
                        c = self.counters.setdefault(int(arg_c[1:]), _S7CounterState())
                        if rlo:
                            c.value = max(0, min(999, self._u32_to_i16(accu1)))
                    elif rlo:
                        self._write_bit_operand(arg, True, param_env=param_env)
                    fc_bit = False
                    continue
                if op == "R":
                    _flush_bare_or()
                    arg_c = re.sub(r"\s+", "", arg.upper())
                    if arg_c.startswith("T") and arg_c[1:].isdigit():
                        t = self.timers.setdefault(int(arg_c[1:]), _S7TimerState())
                        if rlo:
                            t.running = False
                            t.output = False
                            t.remaining_ms = 0
                    elif arg_c.startswith(("C", "Z")) and arg_c[1:].isdigit():
                        c = self.counters.setdefault(int(arg_c[1:]), _S7CounterState())
                        if rlo:
                            c.value = 0
                    elif rlo:
                        self._write_bit_operand(arg, False, param_env=param_env)
                    fc_bit = False
                    continue

                # S7 Timers (SD, SS, SP, SE, SF)
                if op in ("SD", "SS", "SP", "SE", "SF"):
                    _flush_bare_or()
                    resolved_t, _ = self.resolve_operand(arg)
                    t_num = int(re.sub(r"[^0-9]", "", resolved_t))
                    t = self.timers.setdefault(t_num, _S7TimerState())
                    t.mode = op
                    rising = rlo and not t.prev_rlo
                    falling = (not rlo) and t.prev_rlo
                    t.prev_rlo = rlo
                    preset = max(0, self._u32_to_i32(accu1))
                    if op == "SD":
                        if rising:
                            t.preset_ms = preset
                            t.remaining_ms = preset
                            t.running = True
                            t.output = preset == 0
                        elif not rlo:
                            t.running = False
                            t.output = False
                            t.remaining_ms = 0
                    elif op == "SS":
                        if rising:
                            t.preset_ms = preset
                            t.remaining_ms = preset
                            t.running = True
                            if preset == 0:
                                t.output = True
                    elif op == "SP":
                        if rising:
                            t.preset_ms = preset
                            t.remaining_ms = preset
                            t.running = preset > 0
                            t.output = preset > 0
                        elif not rlo:
                            t.running = False
                            t.output = False
                    elif op == "SE":
                        if rising:
                            t.preset_ms = preset
                            t.remaining_ms = preset
                            t.running = preset > 0
                            t.output = preset > 0
                    elif op == "SF":
                        if rlo:
                            t.output = True
                            t.running = False
                            t.remaining_ms = preset
                        elif falling:
                            t.preset_ms = preset
                            t.remaining_ms = preset
                            t.running = preset > 0
                            t.output = preset > 0
                    fc_bit = False
                    continue

                # S7 Counters
                if op in ("CU", "CD"):
                    _flush_bare_or()
                    resolved_c, _ = self.resolve_operand(arg)
                    c_num = int(re.sub(r"[^0-9]", "", resolved_c))
                    c = self.counters.setdefault(c_num, _S7CounterState())
                    if op == "CU":
                        if rlo and not c.prev_cu:
                            c.value = min(999, c.value + 1)
                        c.prev_cu = rlo
                    else:
                        if rlo and not c.prev_cd:
                            c.value = max(0, c.value - 1)
                        c.prev_cd = rlo
                    fc_bit = False
                    continue

                # Load, Transfer & Accumulator Stack
                if op in ("L", "LC"):
                    accu4, accu3, accu2 = accu3, accu2, accu1
                    val_u32, _ = self._load_operand_u32(arg, param_env=param_env)
                    accu1 = val_u32
                    continue
                if op == "T":
                    self._store_u32_to_operand(arg, accu1, param_env=param_env, param_types=param_types)
                    continue
                if op == "TAK":
                    accu1, accu2 = accu2, accu1
                    continue
                if op == "PUSH":
                    accu4, accu3, accu2 = accu3, accu2, accu1
                    continue
                if op == "POP":
                    accu1, accu2, accu3 = accu2, accu3, accu4
                    continue
                if op == "INC":
                    delta = int(arg) if arg.isdigit() else 1
                    accu1 = (accu1 & 0xFFFFFF00) | ((accu1 + delta) & 0xFF)
                    continue
                if op == "DEC":
                    delta = int(arg) if arg.isdigit() else 1
                    accu1 = (accu1 & 0xFFFFFF00) | ((accu1 - delta) & 0xFF)
                    continue

                # Type Conversions & Unary Math
                if op == "ITD":
                    accu1 = self._i32_to_u32(self._u32_to_i16(accu1))
                    continue
                if op == "DTR":
                    accu1 = self._f32_to_u32(float(self._u32_to_i32(accu1)))
                    continue
                if op == "RND":
                    accu1 = self._i32_to_u32(int(round(self._u32_to_f32(accu1))))
                    continue
                if op == "RND+":
                    accu1 = self._i32_to_u32(int(math.ceil(self._u32_to_f32(accu1))))
                    continue
                if op == "RND-":
                    accu1 = self._i32_to_u32(int(math.floor(self._u32_to_f32(accu1))))
                    continue
                if op == "TRUNC":
                    accu1 = self._i32_to_u32(int(math.trunc(self._u32_to_f32(accu1))))
                    continue
                if op == "NEGI":
                    accu1 = (accu1 & 0xFFFF0000) | ((-self._u32_to_i16(accu1)) & 0xFFFF)
                    continue
                if op == "NEGD":
                    accu1 = self._i32_to_u32(-self._u32_to_i32(accu1))
                    continue
                if op == "NEGR":
                    accu1 = self._f32_to_u32(-self._u32_to_f32(accu1))
                    continue
                if op == "INVI":
                    accu1 = (accu1 & 0xFFFF0000) | ((~accu1) & 0xFFFF)
                    continue
                if op == "INVD":
                    accu1 = (~accu1) & 0xFFFFFFFF
                    continue
                if op == "ABS":
                    accu1 = self._f32_to_u32(abs(self._u32_to_f32(accu1)))
                    continue
                if op == "SQR":
                    f = self._u32_to_f32(accu1)
                    accu1 = self._f32_to_u32(f * f)
                    continue
                if op == "SQRT":
                    f = self._u32_to_f32(accu1)
                    accu1 = self._f32_to_u32(math.sqrt(f) if f >= 0.0 else 0.0)
                    continue

                # Word / Double-Word Logic & Shifts
                if op in ("AW", "OW", "XOW"):
                    rhs = self._load_operand_u32(arg, param_env=param_env)[0] if arg else accu2
                    lhs = accu1
                    res_w = (lhs & rhs) if op == "AW" else ((lhs | rhs) if op == "OW" else (lhs ^ rhs))
                    accu1 = (accu1 & 0xFFFF0000) | (res_w & 0xFFFF)
                    continue
                if op in ("AD", "OD", "XOD"):
                    rhs = self._load_operand_u32(arg, param_env=param_env)[0] if arg else accu2
                    lhs = accu1
                    accu1 = ((lhs & rhs) if op == "AD" else ((lhs | rhs) if op == "OD" else (lhs ^ rhs))) & 0xFFFFFFFF
                    continue
                if op in ("SLW", "SRW", "SLD", "SRD", "SSI", "SSD"):
                    n_bits = int(arg) if arg.isdigit() else (accu2 & 0xFF)
                    if op == "SLW":
                        accu1 = (accu1 & 0xFFFF0000) | (((accu1 & 0xFFFF) << n_bits) & 0xFFFF)
                    elif op == "SRW":
                        accu1 = (accu1 & 0xFFFF0000) | (((accu1 & 0xFFFF) >> n_bits) & 0xFFFF)
                    elif op == "SLD":
                        accu1 = (accu1 << n_bits) & 0xFFFFFFFF
                    elif op == "SRD":
                        accu1 = (accu1 >> n_bits) & 0xFFFFFFFF
                    elif op == "SSI":
                        accu1 = (accu1 & 0xFFFF0000) | ((self._u32_to_i16(accu1) >> n_bits) & 0xFFFF)
                    elif op == "SSD":
                        accu1 = self._i32_to_u32(self._u32_to_i32(accu1) >> n_bits)
                    continue

                # Arithmetic (16-bit INT, 32-bit DINT, 32-bit IEEE-754 REAL)
                if op in ("+I", "-I", "*I", "/I"):
                    a, b = self._u32_to_i16(accu2), self._u32_to_i16(accu1)
                    if op == "+I":
                        r16 = (a + b) & 0xFFFF
                    elif op == "-I":
                        r16 = (a - b) & 0xFFFF
                    elif op == "*I":
                        r16 = (a * b) & 0xFFFF
                    else:
                        r16 = (int(a / b) & 0xFFFF) if b != 0 else 0
                    accu1 = (accu1 & 0xFFFF0000) | r16
                    continue
                if op in ("+D", "-D", "*D", "/D", "MOD"):
                    a, b = self._u32_to_i32(accu2), self._u32_to_i32(accu1)
                    if op == "+D":
                        accu1 = self._i32_to_u32(a + b)
                    elif op == "-D":
                        accu1 = self._i32_to_u32(a - b)
                    elif op == "*D":
                        accu1 = self._i32_to_u32(a * b)
                    elif op == "/D":
                        accu1 = self._i32_to_u32(int(a / b)) if b != 0 else 0
                    else:
                        accu1 = self._i32_to_u32(a - int(a / b) * b) if b != 0 else 0
                    continue
                if op in ("+R", "-R", "*R", "/R"):
                    a, b = self._u32_to_f32(accu2), self._u32_to_f32(accu1)
                    if op == "+R":
                        accu1 = self._f32_to_u32(a + b)
                    elif op == "-R":
                        accu1 = self._f32_to_u32(a - b)
                    elif op == "*R":
                        accu1 = self._f32_to_u32(a * b)
                    else:
                        accu1 = self._f32_to_u32(a / b) if b != 0.0 else 0
                    continue

                # Comparators (INT, DINT, REAL)
                if op in ("==I", "<>I", ">I", ">=I", "<I", "<=I", "==D", "<>D", ">D", ">=D", "<D", "<=D", "==R", "<>R", ">R", ">=R", "<R", "<=R"):
                    if op.endswith("I"):
                        a, b = self._u32_to_i16(accu2), self._u32_to_i16(accu1)
                    elif op.endswith("D"):
                        a, b = self._u32_to_i32(accu2), self._u32_to_i32(accu1)
                    else:
                        a, b = self._u32_to_f32(accu2), self._u32_to_f32(accu1)
                    cmp_op = op[:-1]
                    rlo = {
                        "==": a == b,
                        "<>": a != b,
                        ">": a > b,
                        ">=": a >= b,
                        "<": a < b,
                        "<=": a <= b,
                    }[cmp_op]
                    fc_bit = True
                    or_bit = False
                    continue

                # Jumps (with authentic S7 BR handling for JNB / JB / JBI / JNBI)
                if op == "JU":
                    lbl = arg.upper()
                    if lbl in labels:
                        pc = labels[lbl]
                    fc_bit = False
                    or_bit = False
                    continue
                if op == "JC":
                    _flush_bare_or()
                    if rlo and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    continue
                if op == "JB":
                    _flush_bare_or()
                    br_bit = rlo
                    if rlo and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    continue
                if op == "JCN":
                    _flush_bare_or()
                    if (not rlo) and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    continue
                if op == "JNB":
                    _flush_bare_or()
                    br_bit = rlo
                    if (not rlo) and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    continue
                if op == "JBI":
                    if br_bit and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    or_bit = False
                    continue
                if op == "JNBI":
                    if (not br_bit) and arg.upper() in labels:
                        pc = labels[arg.upper()]
                    fc_bit = False
                    or_bit = False
                    continue

                # Record any unrecognized opcode instead of failing silently
                if op not in self.last_unsupported_instructions:
                    self.last_unsupported_instructions.append(op)
        finally:
            if len(self._local_stack) > 1:
                self._local_stack.pop()
            for vname, (sec, _) in var_decls.items():
                if sec in ("VAR", "VAR_STAT") and vname in param_env:
                    stat_store[vname] = param_env[vname]

        return {
            vname: param_env[vname]
            for vname, (sec, _) in var_decls.items()
            if sec in ("VAR_OUTPUT", "VAR_IN_OUT") and vname in param_env
        }

    def step(self, cycles: int = 1, tick_ms: int = 100, entry_block: str = "OB1") -> Dict[str, Any]:
        """Execute ``cycles`` deterministic scan cycles advancing S7 timers by ``tick_ms`` per cycle."""
        with self._lock:
            executed = 0
            self.last_unsupported_instructions = []
            self._current_tick_ms = max(0, int(tick_ms))
            if self.cpu_mode != "RUN":
                return {
                    "ok": True,
                    "cpu_mode": self.cpu_mode,
                    "executed_cycles": 0,
                    "scan_count": self.scan_count,
                    "sim_time_ms": self.sim_time_ms,
                    "warning": "CPU is in STOP mode; no scan cycles executed.",
                }
            entry_up = re.sub(r"\s+", "", entry_block.strip().upper())
            for _ in range(max(1, int(cycles))):
                self.sim_time_ms += self._current_tick_ms
                self._advance_timers(self._current_tick_ms)
                self._execute_block(entry_block)
                if entry_up == "OB35":
                    self.ob35_count += 1
                elif "OB35" in self.blocks:
                    self._ob35_accum_ms += self._current_tick_ms
                    interval = max(1, int(self.ob35_interval_ms))
                    while self._ob35_accum_ms >= interval:
                        self._ob35_accum_ms -= interval
                        self._execute_block("OB35")
                        self.ob35_count += 1
                self.scan_count += 1
                executed += 1
            res: Dict[str, Any] = {
                "ok": True,
                "cpu_mode": self.cpu_mode,
                "executed_cycles": executed,
                "scan_count": self.scan_count,
                "sim_time_ms": self.sim_time_ms,
                "entry_block": entry_block.upper(),
            }
            if self.ob35_count:
                res["ob35_executions"] = self.ob35_count
            if self.last_unsupported_instructions:
                res["unsupported_instructions"] = list(self.last_unsupported_instructions)
            if self.sfc_states:
                res["sfc_active_steps"] = {k: list(v.get("active_steps", [])) for k, v in self.sfc_states.items()}
            return res

    def inspect_state(self, max_bytes_scan: int = 256) -> Dict[str, Any]:
        """Return a compact, AI-friendly snapshot of VirtualS7PLC memory, timers, counters, and loaded blocks."""
        with self._lock:
            active_io: Dict[str, Dict[str, Any]] = {}
            for area_name in ("I", "Q", "M", "PI", "PQ"):
                buf = self.areas[area_name]
                limit = min(len(buf), max_bytes_scan)
                non_zero: Dict[str, Any] = {}
                if area_name in ("PI", "PQ"):
                    for b in range(0, min(len(buf), 512), 2):
                        w = struct.unpack(">h", buf[b:b + 2])[0]
                        if w != 0:
                            non_zero[f"{area_name}W{b}"] = w
                else:
                    for b in range(limit):
                        val = buf[b]
                        if val != 0:
                            bits = [f"{area_name}{b}.{bit}" for bit in range(8) if (val >> bit) & 1]
                            non_zero[f"{area_name}B{b}"] = {"byte": val, "active_bits": bits}
                active_io[area_name] = non_zero

            timers_view = {
                f"T{num}": {
                    "mode": t.mode,
                    "preset_ms": t.preset_ms,
                    "remaining_ms": t.remaining_ms,
                    "running": t.running,
                    "output": t.output,
                }
                for num, t in sorted(self.timers.items())
            }
            counters_view = {f"C{num}": c.value for num, c in sorted(self.counters.items())}
            db_view: Dict[str, Any] = {}
            for db_num, fields in sorted(self.db_layouts.items()):
                db_view[f"DB{db_num}"] = {
                    fname: self.read_address(addr, data_type=ftype)
                    for fname, (addr, ftype) in fields.items()
                }
            return {
                "ok": True,
                "order_number": self.order_number,
                "cpu_mode": self.cpu_mode,
                "scan_count": self.scan_count,
                "sim_time_ms": self.sim_time_ms,
                "loaded_blocks": {
                    k: (self.block_meta.get(k) or {}).get("language", "AWL")
                    for k in sorted(set(list(self.blocks.keys()) + list(self.block_meta.keys())))
                },
                "symbol_count": len(self.symbols),
                "timers": timers_view,
                "counters": counters_view,
                "sfc_active_steps": {k: list(v.get("active_steps", [])) for k, v in self.sfc_states.items()},
                "active_memory": active_io,
                "data_blocks": db_view,
                "unsupported_instructions": list(self.last_unsupported_instructions),
            }


# ---------------------------------------------------------------------------
# Enhanced S7comm Loopback TCP Server (Multi-Item Batch + SZL + CPU Control)
# ---------------------------------------------------------------------------

class EnhancedS7LoopbackServer:
    """Real RFC1006/S7comm TCP server backed by ``VirtualS7PLC`` for offline & CI verification."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        plc: Optional[VirtualS7PLC] = None,
        max_pdu: int = 480,
        allowed_connection_types: Optional[Sequence[str]] = None,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.plc = plc or VirtualS7PLC()
        self.max_pdu = max(96, int(max_pdu))
        self.allowed_connection_types = (
            {S7_CONN_TYPES[c.strip().upper()] for c in allowed_connection_types if c.strip().upper() in S7_CONN_TYPES}
            if allowed_connection_types is not None
            else None
        )
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = port

    @staticmethod
    def _collect_plc_blocks(plc_ref: VirtualS7PLC) -> Dict[str, List[int]]:
        by_type: Dict[str, set] = {name: set() for name in S7_BLOCK_TYPE_CODES}
        with plc_ref._lock:
            for bname in set(list(plc_ref.blocks.keys()) + list(plc_ref.block_meta.keys())):
                m = re.match(r"^(OB|FB|FC|DB|SDB|SFB|SFC)(\d+)$", bname.strip().upper())
                if m:
                    by_type[m.group(1)].add(int(m.group(2)))
            for db_num in plc_ref.dbs.keys():
                by_type["DB"].add(int(db_num))
        return {k: sorted(v) for k, v in by_type.items()}

    def start(self) -> int:
        plc_ref = self.plc
        server_max_pdu = self.max_pdu
        allowed_conn_codes = self.allowed_connection_types
        collect_blocks = self._collect_plc_blocks

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                sock: socket.socket = self.request
                sock.settimeout(10.0)
                while True:
                    try:
                        frame = recv_tpkt(sock)
                    except Exception:
                        break
                    if len(frame) < 6:
                        break
                    cotp_type = frame[5]
                    if cotp_type == 0xE0:  # COTP CR -> CC (or Disconnect Request if TSAP rejected)
                        dst_tsap_hi = 0x01
                        dst_tsap_lo = 0x02
                        # Locate 0xC2 parameter (Dst TSAP) inside COTP CR
                        pos = 11
                        while pos + 2 <= len(frame):
                            p_code = frame[pos]
                            p_len = frame[pos + 1]
                            if p_code == 0xC2 and p_len >= 2 and pos + 2 + p_len <= len(frame):
                                dst_tsap_hi = frame[pos + 2]
                                dst_tsap_lo = frame[pos + 3]
                                break
                            pos += 2 + p_len
                        if allowed_conn_codes is not None and dst_tsap_hi not in allowed_conn_codes:
                            # Send COTP Disconnect Request (0x80) to simulate occupied PG resource
                            dr = bytes([0x03, 0x00, 0x00, 0x0B, 0x06, 0x80, 0x00, 0x01, 0x00, 0x01, 0x00])
                            sock.sendall(dr)
                            break
                        cc = bytes([0x03, 0x00, 0x00, 0x16, 0x11, 0xD0, 0x00, 0x01, 0x00, 0x01, 0x00,
                                    0xC0, 0x01, 0x0A, 0xC1, 0x02, 0x01, 0x00, 0xC2, 0x02, dst_tsap_hi, dst_tsap_lo])
                        sock.sendall(cc)
                        continue
                    if len(frame) < 17 or frame[7] != 0x32:
                        continue
                    s7 = frame[7:]
                    rosctr = s7[1]
                    pdu_ref = struct.unpack(">H", s7[4:6])[0]
                    param_len, data_len = struct.unpack(">HH", s7[6:10])
                    param = s7[10:10 + param_len]
                    data = s7[10 + param_len:10 + param_len + data_len]

                    if rosctr == 0x01 and param:
                        fn = param[0]
                        if fn == 0xF0:  # Setup Communication
                            req_pdu = struct.unpack(">H", param[6:8])[0] if len(param) >= 8 else 480
                            neg = min(req_pdu, server_max_pdu)
                            r_param = struct.pack(">BBHHH", 0xF0, 0x00, 0x0001, 0x0001, neg)
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), 0, 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param))
                        elif fn == 0x04:  # Multi-Item Read Var
                            if len(s7) > server_max_pdu:
                                hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, 0, 0, 0x85, 0x00)
                                sock.sendall(wrap_tpkt_cotp_dt(hdr))
                                continue
                            item_cnt = param[1]
                            resp_data = bytearray()
                            cursor = 2
                            for idx in range(item_cnt):
                                spec = param[cursor:cursor + 12]
                                cursor += 12
                                t_size = spec[3]
                                count, db_num = struct.unpack(">HH", spec[4:8])
                                area_code = spec[8]
                                bit_addr = (spec[9] << 16) | (spec[10] << 8) | spec[11]
                                if area_code in (0x1D, 0x1C):
                                    payload = plc_ref.read_bytes(area_code, 0, bit_addr, 2)
                                    resp_data.extend(struct.pack(">BBH", 0xFF, 0x04, len(payload) * 8))
                                    resp_data.extend(payload)
                                elif t_size == S7_TRANSPORT_SIZE_BIT:
                                    bit_val = plc_ref.read_bit(area_code, db_num, bit_addr // 8, bit_addr % 8)
                                    payload = bytes([1 if bit_val else 0])
                                    resp_data.extend(struct.pack(">BBH", 0xFF, 0x03, 1))
                                    resp_data.extend(payload)
                                    if idx < item_cnt - 1:
                                        resp_data.append(0x00)
                                else:
                                    payload = plc_ref.read_bytes(area_code, db_num, bit_addr // 8, count)
                                    resp_data.extend(struct.pack(">BBH", 0xFF, 0x04, len(payload) * 8))
                                    resp_data.extend(payload)
                                    if len(payload) % 2 == 1 and idx < item_cnt - 1:
                                        resp_data.append(0x00)
                            r_param = bytes([0x04, item_cnt])
                            if 12 + len(r_param) + len(resp_data) > server_max_pdu:
                                hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, 0, 0, 0x85, 0x00)
                                sock.sendall(wrap_tpkt_cotp_dt(hdr))
                                continue
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), len(resp_data), 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + bytes(resp_data)))
                        elif fn == 0x05:  # Multi-Item Write Var
                            if len(s7) > server_max_pdu:
                                hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, 0, 0, 0x85, 0x00)
                                sock.sendall(wrap_tpkt_cotp_dt(hdr))
                                continue
                            item_cnt = param[1]
                            p_cur = 2
                            d_cur = 0
                            rcs = bytearray()
                            for idx in range(item_cnt):
                                spec = param[p_cur:p_cur + 12]
                                p_cur += 12
                                t_size = spec[3]
                                _, db_num = struct.unpack(">HH", spec[4:8])
                                area_code = spec[8]
                                bit_addr = (spec[9] << 16) | (spec[10] << 8) | spec[11]
                                _, d_ts, raw_len = struct.unpack(">BBH", data[d_cur:d_cur + 4])
                                d_cur += 4
                                byte_len = (raw_len + 7) // 8 if d_ts in (0x03, 0x04) else raw_len
                                payload = data[d_cur:d_cur + byte_len]
                                d_cur += byte_len
                                if byte_len % 2 == 1 and idx < item_cnt - 1:
                                    d_cur += 1
                                if area_code in (0x1D, 0x1C):
                                    plc_ref.write_bytes(area_code, 0, bit_addr, payload)
                                elif t_size == S7_TRANSPORT_SIZE_BIT or d_ts == 0x03:
                                    plc_ref.write_bit(area_code, db_num, bit_addr // 8, bit_addr % 8, bool(payload[0]))
                                else:
                                    plc_ref.write_bytes(area_code, db_num, bit_addr // 8, payload)
                                rcs.append(0xFF)
                            r_param = bytes([0x05, item_cnt])
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), len(rcs), 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + bytes(rcs)))
                        elif fn in (0x28, 0x29):  # PLC Start / Stop
                            plc_ref.set_mode("RUN" if fn == 0x28 else "STOP")
                            r_param = bytes([fn, 0x00])
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), 0, 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param))
                    elif rosctr == 0x07 and len(param) >= 7:
                        group = param[5] & 0x0F
                        subfunc = param[6]
                        if group == 0x03 and subfunc == 0x01:  # List Blocks Count
                            inv = collect_blocks(plc_ref)
                            body = bytearray()
                            for b_type, b_code in S7_BLOCK_TYPE_CODES.items():
                                body.extend(struct.pack(">BBH", 0x30, b_code, len(inv.get(b_type, []))))
                            r_param = bytes([0x00, 0x01, 0x12, 0x08, 0x12, 0x83, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])
                            r_data = struct.pack(">BBH", 0xFF, 0x09, len(body)) + bytes(body)
                            hdr = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref, len(r_param), len(r_data))
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + r_data))
                        elif group == 0x03 and subfunc == 0x02 and len(data) >= 6:  # List Blocks of Type
                            b_code = data[5]
                            b_type = S7_BLOCK_TYPE_NAMES.get(b_code, "")
                            inv = collect_blocks(plc_ref)
                            nums = inv.get(b_type, [])
                            body = bytearray()
                            for n in nums:
                                body.extend(struct.pack(">HBB", n, 0x00, 0x01))
                            r_param = bytes([0x00, 0x01, 0x12, 0x08, 0x12, 0x83, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00])
                            r_data = struct.pack(">BBH", 0xFF, 0x09, len(body)) + bytes(body)
                            hdr = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref, len(r_param), len(r_data))
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + r_data))
                        elif group == 0x04 and len(data) >= 8:  # Userdata SZL Read
                            szl_id, szl_idx = struct.unpack(">HH", data[4:8])
                            r_param = bytes([0x00, 0x01, 0x12, 0x08, 0x12, 0x84, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])
                            if szl_id == 0x0011:
                                ord_b = plc_ref.order_number.encode("ascii").ljust(20, b" ")[:20]
                                rec = struct.pack(">H", 0x0001) + ord_b + bytes(6)
                                body = struct.pack(">HHHH", 0x0011, szl_idx, len(rec), 1) + rec
                            elif szl_id == 0x0424:
                                mode_b = 0x08 if plc_ref.cpu_mode == "RUN" else 0x04
                                rec = bytes([0x00, 0x00, 0x00, mode_b, 0x00, 0x00])
                                body = struct.pack(">HHHH", 0x0424, 0x0000, len(rec), 1) + rec
                            elif szl_id == 0x00A0:
                                recs = bytearray()
                                for d in plc_ref.diagnostics[:10]:
                                    recs.extend(struct.pack(
                                        ">HBBHI",
                                        d["event_id"], d["priority_class"], d["ob_number"], d["info1"], d["info2"]
                                    ) + bytes(10))
                                body = struct.pack(">HHHH", 0x00A0, 0x0000, 20, len(plc_ref.diagnostics[:10])) + bytes(recs)
                            else:
                                body = struct.pack(">HHHH", szl_id, szl_idx, 0, 0)
                            r_data = struct.pack(">BBH", 0xFF, 0x09, len(body)) + body
                            hdr = struct.pack(">BBHHHH", 0x32, 0x07, 0x0000, pdu_ref, len(r_param), len(r_data))
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + r_data))

        class _ReuseServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _ReuseServer((self.host, self.requested_port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="s7-loopback")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


__all__ = [
    "S7_CONN_TYPES",
    "S7_BLOCK_TYPE_CODES",
    "S7_BLOCK_TYPE_NAMES",
    "ParsedS7Address",
    "parse_s7_address",
    "encode_s7_value",
    "decode_s7_value",
    "build_cotp_cr",
    "build_list_blocks_count_pdu",
    "build_list_blocks_of_type_pdu",
    "S7ConnectionPool",
    "WatchTableMonitor",
    "PLCDataCollector",
    "VirtualS7PLC",
    "EnhancedS7LoopbackServer",
]
