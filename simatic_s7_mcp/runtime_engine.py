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

import math
import re
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# S7 Address Parsing & Encoding
# ---------------------------------------------------------------------------

S7_AREA_CODES: Dict[str, int] = {
    "I": 0x81,
    "E": 0x81,
    "Q": 0x82,
    "A": 0x82,
    "M": 0x83,
    "DB": 0x84,
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
    area: str          # "I", "Q", "M", "DB", "T", "C"
    area_code: int     # 0x81, 0x82, 0x83, 0x84, 0x1D, 0x1C
    db_number: int     # 0 for non-DB
    byte_offset: int
    bit_offset: int
    data_type: str     # "BOOL", "BYTE", "WORD", "INT", "DWORD", "DINT", "REAL", "TIMER", "COUNTER"
    byte_length: int


_RE_BIT = re.compile(r"^([IEQAM])(\d+)\.([0-7])$", re.IGNORECASE)
_RE_SCALAR = re.compile(r"^([IEQAM])([BWD])(\d+)$", re.IGNORECASE)
_RE_DB_BIT = re.compile(r"^DB(\d+)\.DBX(\d+)\.([0-7])$", re.IGNORECASE)
_RE_DB_SCALAR = re.compile(r"^DB(\d+)\.DB([BWD])(\d+)$", re.IGNORECASE)
_RE_TC = re.compile(r"^([TCZ])(\d+)$", re.IGNORECASE)


def parse_s7_address(address: str, data_type: Optional[str] = None) -> ParsedS7Address:
    """Parse classic STEP 7 absolute addresses including I/Q/M/DB/T/C (with or without internal spaces)."""
    raw = re.sub(r"\s+", "", (address or "").strip().upper())
    if not raw:
        raise ValueError("Empty S7 address")

    m = _RE_BIT.match(raw)
    if m:
        area_ch = m.group(1)
        area = "I" if area_ch in ("I", "E") else ("Q" if area_ch in ("Q", "A") else "M")
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
        area = "I" if area_ch in ("I", "E") else ("Q" if area_ch in ("Q", "A") else "M")
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


def build_cotp_cr(rack: int, slot: int) -> bytes:
    tsap_dst = 0x0100 | ((rack & 0x07) << 5) | (slot & 0x1F)
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


class S7ConnectionPool:
    """Thread-safe RFC1006/S7comm connection pool eliminating per-read TCP handshakes."""

    def __init__(self, idle_ttl_s: float = 60.0, default_timeout_s: float = 3.0) -> None:
        self.idle_ttl_s = idle_ttl_s
        self.default_timeout_s = default_timeout_s
        self._lock = threading.Lock()
        self._conns: Dict[Tuple[str, int, int, int], _PooledS7Conn] = {}
        self.connect_count = 0
        self.reuse_count = 0

    def _open_conn(self, host: str, port: int, rack: int, slot: int, timeout_s: float) -> _PooledS7Conn:
        sock = socket.create_connection((host, port), timeout=timeout_s)
        sock.settimeout(timeout_s)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # 1. COTP Connection Request
        sock.sendall(build_cotp_cr(rack, slot))
        cc = recv_tpkt(sock)
        if len(cc) < 6 or cc[5] != 0xD0:
            sock.close()
            raise ConnectionError("PLC rejected COTP Connection Request")
        # 2. S7 Setup Communication
        sock.sendall(build_s7_setup_comm(pdu_ref=1, max_pdu=480))
        setup_resp = recv_tpkt(sock)
        _, err, param, _ = parse_s7_ack_data(setup_resp)
        if err != 0:
            sock.close()
            raise ConnectionError(f"S7 Setup Communication error: 0x{err:04X}")
        neg_pdu = struct.unpack(">H", param[6:8])[0] if len(param) >= 8 else 240
        self.connect_count += 1
        return _PooledS7Conn(
            host=host,
            port=port,
            rack=rack,
            slot=slot,
            sock=sock,
            negotiated_pdu=max(240, neg_pdu),
        )

    def acquire(
        self,
        host: str,
        port: int = 102,
        rack: int = 0,
        slot: int = 2,
        timeout_s: Optional[float] = None,
    ) -> _PooledS7Conn:
        key = (host, int(port), int(rack), int(slot))
        t_out = timeout_s if timeout_s is not None else self.default_timeout_s
        now = time.monotonic()
        with self._lock:
            existing = self._conns.get(key)
            if existing is not None:
                if (now - existing.last_used) <= self.idle_ttl_s and existing.sock.fileno() >= 0:
                    existing.sock.settimeout(t_out)
                    existing.last_used = now
                    self.reuse_count += 1
                    return existing
                existing.close()
                del self._conns[key]
        conn = self._open_conn(host, int(port), int(rack), int(slot), t_out)
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
                "keys": [f"{h}:{p}/R{r}S{s}" for (h, p, r, s) in self._conns.keys()],
            }

    def _exchange(
        self,
        host: str,
        port: int,
        rack: int,
        slot: int,
        pdu_builder: Callable[[int], bytes],
        timeout_s: float = 3.0,
    ) -> Tuple[int, int, bytes, bytes]:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            conn = self.acquire(host, port, rack, slot, timeout_s=timeout_s)
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
    ) -> Dict[str, Any]:
        """Read multiple S7 addresses in minimal S7comm Read Var PDUs."""
        t0 = time.perf_counter()
        parsed_specs: List[Tuple[Dict[str, Any], ParsedS7Address]] = []
        for item in items:
            addr_str = str(item.get("address") or "")
            dt = item.get("data_type")
            parsed_specs.append((item, parse_s7_address(addr_str, dt)))

        results: List[Dict[str, Any]] = []
        pdu_count = 0

        for start in range(0, len(parsed_specs), max(1, max_items_per_pdu)):
            chunk = parsed_specs[start:start + max_items_per_pdu]
            addrs = [a for _, a in chunk]
            pdu_count += 1
            _, err, _, data = self._exchange(
                host, port, rack, slot,
                lambda ref, _addrs=addrs: build_batch_read_pdu(_addrs, ref),
                timeout_s=timeout_s,
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
    ) -> Dict[str, Any]:
        """Write multiple S7 addresses in minimal S7comm Write Var PDUs."""
        t0 = time.perf_counter()
        encoded: List[Tuple[Dict[str, Any], ParsedS7Address, bytes]] = []
        for item in items:
            addr = parse_s7_address(str(item.get("address") or ""), item.get("data_type"))
            raw = encode_s7_value(addr, item.get("value"))
            encoded.append((item, addr, raw))

        results: List[Dict[str, Any]] = []
        pdu_count = 0

        for start in range(0, len(encoded), max(1, max_items_per_pdu)):
            chunk = encoded[start:start + max_items_per_pdu]
            pairs = [(a, raw) for _, a, raw in chunk]
            pdu_count += 1
            _, err, _, data = self._exchange(
                host, port, rack, slot,
                lambda ref, _pairs=pairs: build_batch_write_pdu(_pairs, ref),
                timeout_s=timeout_s,
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
    ) -> Dict[str, Any]:
        """Query CPU order number (SZL 0x0011), CPU state (SZL 0x0424), and diagnostic buffer (SZL 0x00A0)."""
        t0 = time.perf_counter()

        def _query(szl_id: int, szl_idx: int = 0) -> bytes:
            _, _, _, data = self._exchange(
                host, port, rack, slot,
                lambda ref: build_szl_read_pdu(szl_id, szl_idx, ref),
                timeout_s=timeout_s,
            )
            return data

        raw_id = _query(0x0011, 0x0001)
        raw_state = _query(0x0424, 0x0000)
        raw_diag = _query(0x00A0, 0x0000)

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

        return {
            "ok": True,
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "order_number": order_number or "6ES7 315-2EH14-0AB0",
            "cpu_mode": cpu_mode,
            "diagnostics_count": len(diag_entries),
            "diagnostics_buffer": diag_entries,
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
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        _, err, param, _ = self._exchange(
            host, port, rack, slot,
            lambda ref: build_cpu_control_pdu(action, ref),
            timeout_s=timeout_s,
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

        sample_count = max(1, min(int(samples), 20))
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
    - Process images I, Q, M (64 KB each) + dynamic Data Blocks DB1..DB65535
    - S7 Timers T0..T255 (SD, SE, SP, SS, SF with S5T# time literals & deterministic scan ticks)
    - S7 Counters C0..C255 (CU, CD, S, R, L, LC)
    - STL/LAD/FBD instruction subset: A, AN, O, ON, X, XN, A(, AN(, O(, ON(, ), NOT, SET, CLR,
      FP, FN, =, S, R, L, T, +I, -I, *I, /I, +D, -D, +R, -R, *R, /R,
      ==I, <>I, >I, >=I, <I, <=I, ==R, >R, <R, JU, JC, JCN, JNB, OPN, CALL, BEC, BEU, NOP
    - CPU mode (RUN/STOP) & structured diagnostic buffer entries.
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
        }
        self.dbs: Dict[int, bytearray] = {1: bytearray(4096)}
        self.timers: Dict[int, _S7TimerState] = {}
        self.counters: Dict[int, _S7CounterState] = {}
        self.blocks: Dict[str, List[str]] = {}
        self.diagnostics: List[Dict[str, Any]] = [
            {"event_id": 0x4302, "priority_class": 0x01, "ob_number": 100, "info1": 0x0001, "info2": 0x00000000},
            {"event_id": 0x4304, "priority_class": 0x01, "ob_number": 1, "info1": 0x0008, "info2": 0x00000000},
        ]
        self._open_db: int = 1

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
        name = {0x81: "I", 0x82: "Q", 0x83: "M"}.get(area_code)
        if name is None:
            raise ValueError(f"Unsupported S7 area code: 0x{area_code:02X}")
        buf = self.areas[name]
        if len(buf) < min_size:
            buf.extend(bytearray(min_size - len(buf)))
        return buf

    def read_address(self, address: str, data_type: Optional[str] = None) -> Any:
        addr = parse_s7_address(address, data_type)
        if addr.data_type == "BOOL":
            return self.read_bit(addr.area_code, addr.db_number, addr.byte_offset, addr.bit_offset)
        raw = self.read_bytes(addr.area_code, addr.db_number, addr.byte_offset, addr.byte_length)
        return decode_s7_value(addr, raw)

    def write_address(self, address: str, value: Any, data_type: Optional[str] = None) -> None:
        addr = parse_s7_address(address, data_type)
        if addr.data_type == "BOOL":
            self.write_bit(addr.area_code, addr.db_number, addr.byte_offset, addr.bit_offset, bool(value))
            return
        raw = encode_s7_value(addr, value)
        self.write_bytes(addr.area_code, addr.db_number, addr.byte_offset, raw)

    def set_mode(self, mode: str) -> str:
        with self._lock:
            m = mode.strip().upper()
            if m in ("RUN", "START", "WARM_RESTART", "RUN-P"):
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

    def load_block(self, block_name: str, awl_source: str) -> Dict[str, Any]:
        """Parse and load an AWL/STL/LAD block into virtual CPU memory."""
        with self._lock:
            instructions: List[str] = []
            in_body = False
            saw_begin = False
            for raw_line in awl_source.splitlines():
                line = raw_line.split("//", 1)[0].strip()
                if not line:
                    continue
                upper = line.upper()
                if upper in ("BEGIN", "NETWORK"):
                    in_body = True
                    saw_begin = True
                    continue
                if upper.startswith(("TITLE", "VERSION", "AUTHOR", "FAMILY", "NAME", "VAR", "END_VAR", "ORGANIZATION_BLOCK", "FUNCTION", "FUNCTION_BLOCK")):
                    continue
                if upper.startswith("END_"):
                    in_body = False
                    continue
                if in_body or not saw_begin:
                    # Strip trailing semicolons
                    stmt = line.rstrip(";").strip()
                    if stmt:
                        instructions.append(stmt)
            key = block_name.strip().upper()
            self.blocks[key] = instructions
            return {
                "ok": True,
                "block": key,
                "instruction_count": len(instructions),
                "loaded_blocks": sorted(self.blocks.keys()),
            }

    @staticmethod
    def _parse_s5time_ms(token: str) -> int:
        tok = token.strip().upper()
        if tok.startswith("S5T#"):
            tok = tok[4:]
        total_ms = 0
        for num_s, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(MS|S|M|H)", tok):
            num = float(num_s)
            mult = {"MS": 1, "S": 1000, "M": 60000, "H": 3600000}[unit]
            total_ms += int(round(num * mult))
        if total_ms == 0 and tok.isdigit():
            total_ms = int(tok)
        return total_ms

    def _parse_literal(self, operand: str) -> Tuple[Any, str]:
        op = operand.strip()
        up = op.upper()
        if up.startswith("S5T#"):
            return self._parse_s5time_ms(up), "S5TIME"
        if up.startswith("C#"):
            return int(up[2:]), "INT"
        if up.startswith("W#16#") or up.startswith("DW#16#") or up.startswith("B#16#"):
            return int(up.split("#")[-1], 16), "INT"
        if up.startswith("L#"):
            return int(up[2:]), "DINT"
        if re.fullmatch(r"[+-]?\d+\.\d+(?:[eE][+-]?\d+)?", up):
            return float(up), "REAL"
        if re.fullmatch(r"[+-]?\d+", up):
            return int(up), "INT"
        # Otherwise treat as address
        if up.startswith("DBX") or up.startswith("DBB") or up.startswith("DBW") or up.startswith("DBD"):
            full = f"DB{self._open_db}.{up}"
            return self.read_address(full), "ADDR"
        return self.read_address(up), "ADDR"

    def _eval_bit_operand(self, operand: str) -> bool:
        op = re.sub(r"\s+", "", operand.strip().upper())
        if op.startswith("DBX"):
            op = f"DB{self._open_db}.{op}"
        if op.startswith("T") and op[1:].isdigit():
            t = self.timers.get(int(op[1:]))
            return bool(t.output) if t else False
        if op.startswith(("C", "Z")) and op[1:].isdigit():
            c = self.counters.get(int(op[1:]))
            return bool(c and c.value > 0)
        return bool(self.read_address(op))

    def _write_bit_operand(self, operand: str, val: bool) -> None:
        op = re.sub(r"\s+", "", operand.strip().upper())
        if op.startswith("DBX"):
            op = f"DB{self._open_db}.{op}"
        self.write_address(op, bool(val), data_type="BOOL")

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

    def _execute_block(self, block_name: str, depth: int = 0) -> None:
        if depth > 16:
            return
        instrs = self.blocks.get(block_name.upper(), [])
        if not instrs:
            return

        # Pre-index labels
        labels: Dict[str, int] = {}
        clean_instrs: List[str] = []
        for idx, raw in enumerate(instrs):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", raw)
            if m:
                labels[m.group(1).upper()] = idx
                clean_instrs.append(m.group(2).strip())
            else:
                clean_instrs.append(raw)

        rlo = False
        fc_bit = False  # First Check (/FC): False at start of logic chain
        accu1: Any = 0
        accu2: Any = 0
        nest_stack: List[Tuple[str, bool, bool]] = []

        pc = 0
        steps = 0
        max_steps = max(2000, len(clean_instrs) * 20)

        while pc < len(clean_instrs) and steps < max_steps:
            steps += 1
            stmt = clean_instrs[pc]
            pc += 1
            if not stmt:
                continue

            parts = stmt.split(None, 1)
            op = parts[0].upper()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if op == "NOP":
                continue
            if op == "BEU":
                break
            if op == "BEC":
                if rlo:
                    break
                continue
            if op == "SET":
                rlo = True
                fc_bit = False
                continue
            if op == "CLR":
                rlo = False
                fc_bit = False
                continue
            if op == "NOT":
                rlo = not rlo
                continue
            if op == "SAVE":
                continue
            if op == "OPN":
                m_db = re.match(r"^DB\s*(\d+)$", arg, re.IGNORECASE)
                if m_db:
                    self._open_db = int(m_db.group(1))
                continue
            if op == "CALL":
                target = arg.split(",")[0].strip().upper()
                self._execute_block(target, depth=depth + 1)
                fc_bit = False
                continue

            # Parentheses nesting
            if op in ("A(", "AN(", "O(", "ON(", "X(", "XN("):
                nest_stack.append((op, rlo, fc_bit))
                rlo = False
                fc_bit = False
                continue
            if op == ")":
                if nest_stack:
                    n_op, prev_rlo, prev_fc = nest_stack.pop()
                    val = not rlo if n_op in ("AN(", "ON(", "XN(") else rlo
                    if not prev_fc:
                        rlo = val
                    elif n_op.startswith("A"):
                        rlo = prev_rlo and val
                    elif n_op.startswith("O"):
                        rlo = prev_rlo or val
                    else:
                        rlo = prev_rlo ^ val
                    fc_bit = True
                continue

            # Bare OR combiner (O with no argument in STL = OR of two AND branches)
            if op == "O" and not arg:
                # Peek: save current branch via implicit OR-combine
                nest_stack.append(("O(", rlo, True))
                rlo = False
                fc_bit = False
                continue

            # Bit logic instructions
            if op in ("A", "AN", "O", "ON", "X", "XN"):
                val = self._eval_bit_operand(arg)
                if op in ("AN", "ON", "XN"):
                    val = not val
                if not fc_bit:
                    rlo = val
                elif op in ("A", "AN"):
                    rlo = rlo and val
                elif op in ("O", "ON"):
                    rlo = rlo or val
                else:
                    rlo = rlo ^ val
                fc_bit = True
                continue

            # Edge detection
            if op in ("FP", "FN"):
                prev = self._eval_bit_operand(arg)
                self._write_bit_operand(arg, rlo)
                rlo = (rlo and not prev) if op == "FP" else ((not rlo) and prev)
                fc_bit = True
                continue

            # Output assignments terminate logic chain (/FC = 0)
            if op == "=":
                # Close any implicit bare-O branches
                while nest_stack and nest_stack[-1][0] == "O(":
                    _, prev_rlo, _ = nest_stack.pop()
                    rlo = prev_rlo or rlo
                self._write_bit_operand(arg, rlo)
                fc_bit = False
                continue
            if op == "S":
                arg_c = re.sub(r"\s+", "", arg.upper())
                if arg_c.startswith(("C", "Z")) and arg_c[1:].isdigit():
                    c = self.counters.setdefault(int(arg_c[1:]), _S7CounterState())
                    if rlo:
                        c.value = max(0, min(999, int(accu1)))
                elif rlo:
                    self._write_bit_operand(arg, True)
                fc_bit = False
                continue
            if op == "R":
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
                    self._write_bit_operand(arg, False)
                fc_bit = False
                continue

            # S7 Timers (SD, SS, SP, SE, SF)
            if op in ("SD", "SS", "SP", "SE", "SF"):
                t_num = int(re.sub(r"[^0-9]", "", arg))
                t = self.timers.setdefault(t_num, _S7TimerState())
                t.mode = op
                rising = rlo and not t.prev_rlo
                falling = (not rlo) and t.prev_rlo
                t.prev_rlo = rlo
                preset = int(accu1) if isinstance(accu1, (int, float)) else 0
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
                c_num = int(re.sub(r"[^0-9]", "", arg))
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

            # Load & Transfer
            if op in ("L", "LC"):
                accu2 = accu1
                val, _ = self._parse_literal(arg)
                accu1 = val
                continue
            if op == "T":
                dest = arg.upper()
                if dest.startswith(("DBB", "DBW", "DBD")):
                    dest = f"DB{self._open_db}.{dest}"
                dt = "REAL" if isinstance(accu1, float) else None
                self.write_address(dest, accu1, data_type=dt)
                continue

            # Arithmetic
            if op in ("+I", "+D", "+R"):
                accu1 = (float(accu2) + float(accu1)) if op == "+R" else (int(accu2) + int(accu1))
                continue
            if op in ("-I", "-D", "-R"):
                accu1 = (float(accu2) - float(accu1)) if op == "-R" else (int(accu2) - int(accu1))
                continue
            if op in ("*I", "*D", "*R"):
                accu1 = (float(accu2) * float(accu1)) if op == "*R" else (int(accu2) * int(accu1))
                continue
            if op in ("/I", "/D", "/R"):
                if accu1 != 0:
                    accu1 = (float(accu2) / float(accu1)) if op == "/R" else int(int(accu2) / int(accu1))
                continue

            # Comparators
            if op in ("==I", "<>I", ">I", ">=I", "<I", "<=I", "==D", "<>D", ">D", ">=D", "<D", "<=D", "==R", "<>R", ">R", ">=R", "<R", "<=R"):
                a, b = (float(accu2), float(accu1)) if op.endswith("R") else (int(accu2), int(accu1))
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
                continue

            # Jumps
            if op == "JU":
                lbl = arg.upper()
                if lbl in labels:
                    pc = labels[lbl]
                fc_bit = False
                continue
            if op == "JC":
                if rlo and arg.upper() in labels:
                    pc = labels[arg.upper()]
                fc_bit = False
                continue
            if op in ("JCN", "JNB"):
                if (not rlo) and arg.upper() in labels:
                    pc = labels[arg.upper()]
                fc_bit = False
                continue

    def step(self, cycles: int = 1, tick_ms: int = 100, entry_block: str = "OB1") -> Dict[str, Any]:
        """Execute ``cycles`` deterministic OB1 scan cycles advancing S7 timers by ``tick_ms`` per cycle."""
        with self._lock:
            executed = 0
            if self.cpu_mode != "RUN":
                return {
                    "ok": True,
                    "cpu_mode": self.cpu_mode,
                    "executed_cycles": 0,
                    "scan_count": self.scan_count,
                    "sim_time_ms": self.sim_time_ms,
                    "warning": "CPU is in STOP mode; no scan cycles executed.",
                }
            for _ in range(max(1, int(cycles))):
                self.sim_time_ms += max(0, int(tick_ms))
                self._advance_timers(max(0, int(tick_ms)))
                self._execute_block(entry_block)
                self.scan_count += 1
                executed += 1
            return {
                "ok": True,
                "cpu_mode": self.cpu_mode,
                "executed_cycles": executed,
                "scan_count": self.scan_count,
                "sim_time_ms": self.sim_time_ms,
                "entry_block": entry_block.upper(),
            }


# ---------------------------------------------------------------------------
# Enhanced S7comm Loopback TCP Server (Multi-Item Batch + SZL + CPU Control)
# ---------------------------------------------------------------------------

class EnhancedS7LoopbackServer:
    """Real RFC1006/S7comm TCP server backed by ``VirtualS7PLC`` for offline & CI verification."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, plc: Optional[VirtualS7PLC] = None) -> None:
        self.host = host
        self.requested_port = port
        self.plc = plc or VirtualS7PLC()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = port

    def start(self) -> int:
        plc_ref = self.plc

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
                    if cotp_type == 0xE0:  # COTP CR -> CC
                        cc = bytes([0x03, 0x00, 0x00, 0x16, 0x11, 0xD0, 0x00, 0x01, 0x00, 0x01, 0x00,
                                    0xC0, 0x01, 0x0A, 0xC1, 0x02, 0x01, 0x00, 0xC2, 0x02, 0x01, 0x02])
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
                            r_param = struct.pack(">BBHHH", 0xF0, 0x00, 0x0001, 0x0001, 480)
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), 0, 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param))
                        elif fn == 0x04:  # Multi-Item Read Var
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
                            hdr = struct.pack(">BBHHHHBB", 0x32, 0x03, 0x0000, pdu_ref, len(r_param), len(resp_data), 0, 0)
                            sock.sendall(wrap_tpkt_cotp_dt(hdr + r_param + bytes(resp_data)))
                        elif fn == 0x05:  # Multi-Item Write Var
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
                    elif rosctr == 0x07 and len(data) >= 8:  # Userdata SZL Read
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
    "ParsedS7Address",
    "parse_s7_address",
    "encode_s7_value",
    "decode_s7_value",
    "S7ConnectionPool",
    "WatchTableMonitor",
    "VirtualS7PLC",
    "EnhancedS7LoopbackServer",
]
