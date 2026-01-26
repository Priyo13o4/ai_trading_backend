"""MT5 binary wire protocol (v1)

This module defines a framed binary protocol used between:
- MT5 Expert Advisor <-> Python bridge (TCP)
- Python bridge <-> FastAPI backend (TCP)

Design goals:
- Strict framing (fixed header, bounded payload)
- Minimal allocations (readexactly + memoryview)
- Fail fast on protocol violations

Notes:
- Endianness: little-endian (<) for easier MQL5 byte packing.
- CRC32 is optional; when FLAG_CRC32 is not set, crc32 field is ignored.
"""

from __future__ import annotations

import asyncio
import binascii
import struct
from dataclasses import dataclass
from typing import Optional


MAGIC = b"MT5B"  # MT5 Binary
VERSION = 1
HEADER_LEN = 32

# Flags
FLAG_CRC32 = 1 << 0

# Message types (u8)
MSG_HELLO = 1
MSG_HEARTBEAT = 2
MSG_ERROR = 3

MSG_SUBSCRIBE = 10          # Backend/bridge -> EA
MSG_HISTORY_FETCH = 11      # Backend/bridge -> EA

MSG_LIVE_BAR = 20           # EA -> backend (via bridge)
MSG_FORMING_BAR = 24        # EA -> backend (forming M1 snapshot, ephemeral)
MSG_HIST_BEGIN = 21         # EA -> backend
MSG_HIST_CHUNK = 22         # EA -> backend
MSG_HIST_END = 23           # EA -> backend

# Timeframe codes (must match MT5 EA)
TF_M1 = 1
TF_M5 = 2
TF_M15 = 3
TF_M30 = 4
TF_H1 = 5
TF_H4 = 6
TF_D1 = 7   # Broker-provided only (DST-aware)
TF_W1 = 8   # Broker-provided only (session-aligned)
TF_MN1 = 9  # Broker-provided only (month-aligned)

# Map timeframe codes to database names
TF_TO_NAME = {
    TF_M1: "M1",
    TF_M5: "M5",
    TF_M15: "M15",
    TF_M30: "M30",
    TF_H1: "H1",
    TF_H4: "H4",
    TF_D1: "D1",
    TF_W1: "W1",
    TF_MN1: "MN1",
}

# Sources
SRC_MT5 = 1


# Header layout (little-endian)
# magic[4], version:u8, msg_type:u8, flags:u16, payload_len:u32, seq:u32, job_id:u64, crc32:u32, reserved:u32
_HEADER_STRUCT = struct.Struct("<4sBBHIIQII")


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Frame:
    msg_type: int
    flags: int
    seq: int
    job_id: int
    payload: bytes


def crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def pack_frame(
    msg_type: int,
    payload: bytes = b"",
    *,
    flags: int = 0,
    seq: int = 0,
    job_id: int = 0,
) -> bytes:
    if not (0 <= msg_type <= 255):
        raise ValueError("msg_type must fit in u8")
    if not (0 <= flags <= 0xFFFF):
        raise ValueError("flags must fit in u16")
    if not (0 <= seq <= 0xFFFFFFFF):
        raise ValueError("seq must fit in u32")
    if not (0 <= job_id <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError("job_id must fit in u64")

    if payload is None:
        payload = b""

    payload_len = len(payload)
    if payload_len > 8 * 1024 * 1024:
        raise ValueError("payload too large")

    csum = crc32(payload) if (flags & FLAG_CRC32) else 0
    header = _HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        msg_type,
        flags,
        payload_len,
        seq,
        job_id,
        csum,
        0,
    )
    return header + payload


def unpack_header(header: bytes) -> tuple[int, int, int, int, int, int]:
    if len(header) != HEADER_LEN:
        raise ProtocolError(f"invalid header length: {len(header)}")
    magic, version, msg_type, flags, payload_len, seq, job_id, csum, _reserved = _HEADER_STRUCT.unpack(header)

    if magic != MAGIC:
        raise ProtocolError(f"bad magic: {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported version: {version}")
    if payload_len > 8 * 1024 * 1024:
        raise ProtocolError(f"payload too large: {payload_len}")

    return msg_type, flags, payload_len, seq, job_id, csum


async def read_frame(reader: asyncio.StreamReader, *, timeout: Optional[float] = None) -> Frame:
    try:
        if timeout is None:
            header = await reader.readexactly(HEADER_LEN)
        else:
            header = await asyncio.wait_for(reader.readexactly(HEADER_LEN), timeout=timeout)
    except asyncio.IncompleteReadError as e:
        got = len(e.partial) if e.partial is not None else 0
        raise ProtocolError(f"EOF while reading header got={got} expected={HEADER_LEN}") from e

    msg_type, flags, payload_len, seq, job_id, csum = unpack_header(header)

    try:
        if payload_len:
            if timeout is None:
                payload = await reader.readexactly(payload_len)
            else:
                payload = await asyncio.wait_for(reader.readexactly(payload_len), timeout=timeout)
        else:
            payload = b""
    except asyncio.IncompleteReadError as e:
        got = len(e.partial) if e.partial is not None else 0
        raise ProtocolError(f"EOF while reading payload got={got} expected={payload_len}") from e

    if flags & FLAG_CRC32:
        if crc32(payload) != csum:
            raise ProtocolError("crc32 mismatch")

    return Frame(msg_type=msg_type, flags=flags, seq=seq, job_id=job_id, payload=payload)


# ================================
# Payload helpers (v1)
# ================================

_SYMBOL_LEN = 16


def pack_symbol(symbol: str) -> bytes:
    s = (symbol or "").upper().encode("ascii", errors="ignore")
    if len(s) > _SYMBOL_LEN:
        s = s[:_SYMBOL_LEN]
    return s.ljust(_SYMBOL_LEN, b"\x00")


def unpack_symbol(buf: bytes) -> str:
    raw = buf[:_SYMBOL_LEN]
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")


# Live candle payload: symbol[16], tf:u8, source:u8, rsv:u16, ts_open:i64, o,f64 h,f64 l,f64 c,f64 vol:i64
_LIVE_BAR_STRUCT = struct.Struct("<16sBBHqddddq")


def pack_live_bar(
    *,
    symbol: str,
    ts_open: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    tf: int = TF_M1,
    source: int = SRC_MT5,
) -> bytes:
    return _LIVE_BAR_STRUCT.pack(
        pack_symbol(symbol),
        tf,
        source,
        0,
        int(ts_open),
        float(open_),
        float(high),
        float(low),
        float(close),
        int(volume),
    )


def unpack_live_bar(payload: bytes) -> dict:
    if len(payload) != _LIVE_BAR_STRUCT.size:
        raise ProtocolError(f"bad LIVE_BAR payload size: {len(payload)}")
    sym_b, tf, source, _rsv, ts_open, o, h, l, c, vol = _LIVE_BAR_STRUCT.unpack(payload)
    return {
        "symbol": unpack_symbol(sym_b),
        "timeframe": tf,
        "source": source,
        "ts_open": ts_open,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
    }


def pack_forming_bar(
    *,
    symbol: str,
    ts_open: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    tf: int = TF_M1,
    source: int = SRC_MT5,
) -> bytes:
    # Same payload layout as LIVE_BAR.
    return pack_live_bar(
        symbol=symbol,
        ts_open=ts_open,
        open_=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tf=tf,
        source=source,
    )


def unpack_forming_bar(payload: bytes) -> dict:
    # Same payload layout as LIVE_BAR.
    return unpack_live_bar(payload)


# History chunk header + repeated candle rows
# header: symbol[16], tf:u8, rsv[3], chunk_index:u32, count:u16, rsv2:u16
_HIST_CHUNK_HDR = struct.Struct("<16sB3sIHH")
# row: ts_open:i64, o,f64 h,f64 l,f64 c,f64 vol:i64
_HIST_ROW = struct.Struct("<qddddq")


def iter_hist_chunk(payload: bytes) -> tuple[dict, list[dict]]:
    if len(payload) < _HIST_CHUNK_HDR.size:
        raise ProtocolError("HIST_CHUNK payload too small")

    sym_b, tf, _rsv3, chunk_index, count, _rsv2 = _HIST_CHUNK_HDR.unpack_from(payload, 0)
    expected = _HIST_CHUNK_HDR.size + count * _HIST_ROW.size
    if len(payload) != expected:
        raise ProtocolError(f"HIST_CHUNK bad size: got={len(payload)} expected={expected}")

    meta = {
        "symbol": unpack_symbol(sym_b),
        "timeframe": tf,
        "chunk_index": chunk_index,
        "count": count,
    }

    rows: list[dict] = []
    off = _HIST_CHUNK_HDR.size
    for _ in range(count):
        ts_open, o, h, l, c, vol = _HIST_ROW.unpack_from(payload, off)
        rows.append({
            "ts_open": ts_open,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": vol,
        })
        off += _HIST_ROW.size

    return meta, rows
