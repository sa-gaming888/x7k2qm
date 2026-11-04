"""sproto.py - schema 驅動的通用 CloudWu Sproto 解碼器。

與 參考專案（另一個版本）手寫逐型別 decoder 不同，這裡完全由
`data/sproto_schema.json`（tools/dump_sproto_schema.py 從 HotUpdate.dll 的
decode() IL 匯出）驅動：631 個協定、1295 個型別全部可解，不必逐一手寫。

線路格式（雲風 sproto）：
- 0-Packing 壓縮（sproto_unpack）
- struct = fn(uint16) + fn 個 slot(uint16) + data 區
  slot v：0 → 值在 data 區；奇數 → 跳過 v//2 個 tag；偶數 → 內聯整數 v//2-1
- data 區每筆 = [uint32 長度][內容]；integer 4/8 bytes；integer 陣列首 byte 為元素寬度
- 一個 RPC 封包 = Package{type, session} 後面緊接 payload struct
- 每個 RPC 外層包 GateMessage(type=0)，content[].networkMessage 是另一個 0-packed 封包
"""

import json
import os
import struct
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "sproto_schema.json")

GATE_MESSAGE_TAG = 0
MAX_GATE_DEPTH = 4  # 正常只會巢一層；設上限避免惡意/損毀資料無限遞迴


class SprotoError(ValueError):
    pass


def sproto_unpack(src: bytes) -> bytes:
    """雲風 Sproto 0-Packing 解壓。"""
    dst = bytearray()
    idx = 0
    n_src = len(src)
    while idx < n_src:
        header = src[idx]
        idx += 1
        if header == 0xFF:
            if idx >= n_src:
                raise SprotoError("0xFF 段缺長度")
            n = (src[idx] + 1) * 8
            idx += 1
            if idx + n > n_src:
                raise SprotoError("0xFF 段超出資料")
            dst += src[idx: idx + n]
            idx += n
        else:
            for bit in range(8):
                if header & (1 << bit):
                    if idx >= n_src:
                        raise SprotoError("0-pack 段超出資料")
                    dst.append(src[idx])
                    idx += 1
                else:
                    dst.append(0)
    return bytes(dst)


# ---------------------------------------------------------------------------
# 原始 struct 讀取
# ---------------------------------------------------------------------------


def read_struct(buf: bytes, pos: int = 0) -> Tuple[Dict[int, Any], int]:
    """讀一個 struct，回傳 ({tag: int 或 bytes}, 結束位置)。

    內聯整數以 int 表示，data 區欄位以 bytes（未解讀）表示，由 schema 決定如何解讀。
    """
    if pos + 2 > len(buf):
        raise SprotoError("struct 標頭不足")
    fn = struct.unpack_from("<H", buf, pos)[0]
    pos += 2
    if pos + 2 * fn > len(buf):
        raise SprotoError("slot 區不足")
    slots = struct.unpack_from(f"<{fn}H", buf, pos)
    pos += 2 * fn
    out: Dict[int, Any] = {}
    tag = -1
    for v in slots:
        tag += 1
        if v & 1:
            tag += v // 2
            continue
        if v == 0:
            if pos + 4 > len(buf):
                raise SprotoError(f"tag {tag} data 長度不足")
            sz = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
            if pos + sz > len(buf):
                raise SprotoError(f"tag {tag} data 超出 ({sz})")
            out[tag] = bytes(buf[pos: pos + sz])
            pos += sz
        else:
            out[tag] = v // 2 - 1
    return out, pos


def _decode_int_bytes(b: bytes) -> int:
    if len(b) == 4:
        return struct.unpack("<i", b)[0]
    if len(b) == 8:
        return struct.unpack("<q", b)[0]
    if not b:
        return 0
    raise SprotoError(f"integer 長度不合法 {len(b)}")


def _encode_int(v: int) -> bytes:
    if -2**31 <= v < 2**31:
        return struct.pack("<i", v)
    return struct.pack("<q", v)


def _chunk(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def sproto_pack(src: bytes) -> bytes:
    """雲風 Sproto 0-Packing 壓縮（逐位元組照 sproto.c 的 sproto_pack，含 0xFF 連續段規則）。

    每 8 bytes 一段：一般段 = header bitmap + 非零 bytes；全非零（或在 0xFF 連續段中、6~7 個非零）
    的段併入 0xFF 段：0xFF、段數-1、原始 bytes（最多 256 段）。
    """
    segs = [src[i:i + 8].ljust(8, b"\0") for i in range(0, len(src), 8)]
    out = bytearray()
    ff: List[bytes] = []

    def flush_ff() -> None:
        if ff:
            out.append(0xFF)
            out.append(len(ff) - 1)
            for s in ff:
                out.extend(s)
            ff.clear()

    for seg in segs:
        notzero = sum(1 for b in seg if b)
        if notzero == 8 or (notzero in (6, 7) and ff):
            ff.append(seg)
            if len(ff) == 256:
                flush_ff()
            continue
        flush_ff()
        header = 0
        body = bytearray()
        for bit, b in enumerate(seg):
            if b:
                header |= 1 << bit
                body.append(b)
        out.append(header)
        out.extend(body)
    flush_ff()
    return bytes(out)


def _split_chunks(b: bytes) -> List[bytes]:
    out = []
    pos = 0
    while pos < len(b):
        if pos + 4 > len(b):
            raise SprotoError("陣列元素長度不足")
        sz = struct.unpack_from("<I", b, pos)[0]
        pos += 4
        if pos + sz > len(b):
            raise SprotoError("陣列元素超出")
        out.append(b[pos: pos + sz])
        pos += sz
    return out


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class Schema:
    def __init__(self, data: Dict[str, Any]):
        self.types: Dict[str, Dict[int, Dict[str, Any]]] = {
            name: {f["tag"]: f for f in t["fields"]} for name, t in data["types"].items()
        }
        self.protocols: Dict[str, Dict[str, Any]] = data["protocols"]
        self.by_tag: Dict[int, str] = {p["tag"]: name for name, p in self.protocols.items()}

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Schema":
        with open(path or DEFAULT_SCHEMA_PATH, encoding="utf-8") as fp:
            return cls(json.load(fp))

    def protocol_name(self, tag: int) -> Optional[str]:
        return self.by_tag.get(tag)

    # ── 值解碼 ──────────────────────────────────────────────────────────
    def decode_type(self, type_name: str, buf: bytes) -> Dict[str, Any]:
        raw, _ = read_struct(buf)
        return self.decode_fields(type_name, raw)

    def decode_fields(self, type_name: str, raw: Dict[int, Any]) -> Dict[str, Any]:
        fields = self.types.get(type_name)
        if fields is None:
            raise SprotoError(f"未知型別 {type_name}")
        out: Dict[str, Any] = {}
        for tag, val in raw.items():
            f = fields.get(tag)
            if f is None:
                out[f"_tag{tag}"] = val.hex() if isinstance(val, bytes) else val
                continue
            out[f["name"]] = self._decode_value(f, val)
        return out

    # ── 值編碼（decode 的反向；無畫面客戶端送 request 用）────────────────
    def encode_type(self, type_name: str, data: Dict[str, Any]) -> bytes:
        fields = self.types.get(type_name)
        if fields is None:
            raise SprotoError(f"未知型別 {type_name}")
        by_name = {f["name"]: f for f in fields.values()}
        unknown = set(data) - set(by_name)
        if unknown:
            raise SprotoError(f"{type_name} 沒有欄位 {sorted(unknown)}")
        slots: List[int] = []
        body = bytearray()
        last = -1
        for tag in sorted(fields):
            f = fields[tag]
            val = data.get(f["name"])
            if val is None:
                continue
            # 官方 C# SprotoTypeSerialize.write_*(List／Dictionary) 遇到空的直接 return、不寫欄位；
            # bool false、整數 0、空字串 "" 仍照寫（參考專案-learnings §1.7，Item_ItemUse 的 data: []）
            if (f["type"].startswith("*") or f["type"] == "map") and len(val) == 0:
                continue
            inline, blob = self._encode_value(f, val)
            gap = tag - last - 1
            if gap > 0:
                slots.append(2 * gap - 1)
            if inline is not None:
                slots.append((inline + 1) * 2)
            else:
                slots.append(0)
                body += struct.pack("<I", len(blob)) + blob
            last = tag
        return struct.pack("<H", len(slots)) + struct.pack(f"<{len(slots)}H", *slots) + bytes(body)

    def _encode_value(self, f: Dict[str, Any], val: Any) -> Tuple[Optional[int], bytes]:
        """回傳 (內聯整數, None) 或 (None, data 區內容)。"""
        t = f["type"]
        if t == "boolean":
            return (1 if val else 0), b""
        if t == "integer":
            v = int(val)
            if 0 <= v < 0x7FFF:
                return v, b""
            return None, _encode_int(v)
        if t == "string":
            return None, str(val).encode("utf-8")
        if t == "binary":
            return None, bytes.fromhex(val) if isinstance(val, str) else bytes(val)
        if t == "obj":
            return None, self.encode_type(f["elem"], val)
        if t in ("*obj", "map"):
            return None, b"".join(_chunk(self.encode_type(f["elem"], x)) for x in val)
        if t == "*integer":
            vals = [int(x) for x in val]
            if not vals:
                return None, b""
            if all(-2**31 <= x < 2**31 for x in vals):
                return None, b"\x04" + struct.pack(f"<{len(vals)}i", *vals)
            return None, b"\x08" + struct.pack(f"<{len(vals)}q", *vals)
        if t == "*boolean":
            return None, bytes(1 if x else 0 for x in val)
        if t == "*string":
            return None, b"".join(_chunk(str(x).encode("utf-8")) for x in val)
        if t == "*binary":
            return None, b"".join(_chunk(bytes.fromhex(x) if isinstance(x, str) else bytes(x)) for x in val)
        raise SprotoError(f"不支援編碼的型別 {t}")

    def _decode_value(self, f: Dict[str, Any], val: Any) -> Any:
        t = f["type"]
        if isinstance(val, int):  # 內聯
            if t == "boolean":
                return bool(val)
            return val
        if t == "integer":
            return _decode_int_bytes(val)
        if t == "boolean":
            return _decode_int_bytes(val) != 0
        if t == "string":
            return val.decode("utf-8", errors="replace")
        if t == "binary":
            return val.hex()
        if t == "obj":
            return self.decode_type(f["elem"], val)
        if t in ("*obj", "map"):
            return [self.decode_type(f["elem"], c) for c in _split_chunks(val)]
        if t == "*integer":
            if not val:
                return []
            width = val[0]
            body = val[1:]
            if width not in (4, 8) or len(body) % width:
                raise SprotoError(f"integer 陣列寬度不合法 {width}")
            fmt = "<i" if width == 4 else "<q"
            return [struct.unpack_from(fmt, body, i)[0] for i in range(0, len(body), width)]
        if t == "*boolean":
            return [b != 0 for b in val]
        if t == "*string":
            return [c.decode("utf-8", errors="replace") for c in _split_chunks(val)]
        if t == "*binary":
            return [c.hex() for c in _split_chunks(val)]
        return val.hex()


# ---------------------------------------------------------------------------
# RPC 分發
# ---------------------------------------------------------------------------


class SessionTable:
    """記住 request 的 session → 協定名，讓沒有 type 的 response 能找到正確型別。

    每條連線一個（雙方向共用）。"""

    def __init__(self) -> None:
        self._pending: Dict[int, str] = {}

    def remember(self, session: int, proto: str) -> None:
        self._pending[session] = proto
        if len(self._pending) > 4096:  # 防無限成長
            for k in list(self._pending)[:2048]:
                del self._pending[k]

    def pop(self, session: int) -> Optional[str]:
        return self._pending.pop(session, None)


def dispatch(schema: Schema, unpacked: bytes, sessions: Optional[SessionTable] = None,
             depth: int = 0) -> List[Dict[str, Any]]:
    """解一個 0-unpack 後的 RPC 封包，遞迴展開 GateMessage，回傳事件清單。

    事件格式：{"kind": "request"|"response"|"unknown", "tag", "name", "session", "data"}
    """
    if depth > MAX_GATE_DEPTH:
        raise SprotoError("GateMessage 巢狀過深")
    pkg, pos = read_struct(unpacked)
    ptype = pkg.get(0)
    session = pkg.get(1)
    if isinstance(ptype, bytes):
        ptype = _decode_int_bytes(ptype)
    if isinstance(session, bytes):
        session = _decode_int_bytes(session)
    payload = unpacked[pos:]

    if ptype is not None:
        name = schema.protocol_name(ptype)
        if name is None:
            return [{"kind": "unknown", "tag": ptype, "name": None, "session": session,
                     "data": {"raw": payload.hex()}}]
        if session is not None and sessions is not None and schema.protocols[name]["response"]:
            sessions.remember(session, name)
        type_name = schema.protocols[name]["request"]
        data = schema.decode_type(type_name, payload) if (type_name and payload) else {}
        kind = "request"
    else:
        name = sessions.pop(session) if (sessions is not None and session is not None) else None
        if name is None:
            return [{"kind": "unknown", "tag": None, "name": None, "session": session,
                     "data": {"raw": payload.hex()}}]
        ptype = schema.protocols[name]["tag"]
        type_name = schema.protocols[name]["response"]
        data = schema.decode_type(type_name, payload) if (type_name and payload) else {}
        kind = "response"

    if ptype == GATE_MESSAGE_TAG:
        events: List[Dict[str, Any]] = []
        for mc in data.get("content", []):
            inner = mc.get("networkMessage")
            if inner:
                events.extend(dispatch(schema, sproto_unpack(bytes.fromhex(inner)),
                                       sessions, depth + 1))
            elif mc.get("error"):
                events.append({"kind": "error", "tag": None, "name": "ErrorMessage",
                               "session": session, "data": mc["error"]})
        return events

    return [{"kind": kind, "tag": ptype, "name": name, "session": session, "data": data}]


# ---------------------------------------------------------------------------
# RPC 封裝（dispatch 的反向；無畫面客戶端用）
# ---------------------------------------------------------------------------


def encode_rpc(schema: Schema, name: str, data: Optional[Dict[str, Any]] = None,
               session: Optional[int] = None) -> bytes:
    """一則 request（0-unpack 後的形式）：Package{type, session} + payload。"""
    proto = schema.protocols[name]
    pkg: Dict[str, Any] = {"type": proto["tag"]}
    if session is not None:
        pkg["session"] = session
    out = schema.encode_type("Package", pkg)
    if proto["request"]:
        out += schema.encode_type(proto["request"], data or {})
    elif data:
        raise SprotoError(f"{name} 沒有 request 欄位")
    return out


def encode_gate(schema: Schema, inner_unpacked: bytes, rid: Optional[int] = None,
                session: Optional[int] = None, not_main: Optional[bool] = None) -> bytes:
    """包 GateMessage 信封（0-unpack 後的形式）；客戶端 SocketDriver.SprotoBufferSend 的做法。"""
    head: Dict[str, Any] = {}
    if rid is not None:
        head["rid"] = rid
    if not_main is not None:
        head["notMain"] = not_main
    gate: Dict[str, Any] = {"content": [{"networkMessage": sproto_pack(inner_unpacked)}]}
    if head:
        gate["head"] = head
    return encode_rpc(schema, "GateMessage", gate, session)


def encode_response(schema: Schema, name: str, data: Optional[Dict[str, Any]], session: int) -> bytes:
    """一則 response（0-unpack 後）：Package{session}（沒有 type）+ response payload。測試用假伺服器會用到。"""
    out = schema.encode_type("Package", {"session": session})
    rtype = schema.protocols[name]["response"]
    if rtype:
        out += schema.encode_type(rtype, data or {})
    return out
