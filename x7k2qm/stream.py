"""stream.py - 連線層：剝握手 → 切訊框 → DES 解密 → Sproto 分發。

依連線 (conn) + 方向 (c2s/s2c) 各自維護緩衝區；同一連線雙方向共用一把 DES 金鑰
與一張 session 表（讓 S→C response 對到 C→S request 的協定型別）。

訊框：[2B BE 長度 N][N-4 byte DES-ECB 密文][4B 小端序號]
（序號觀察：雙方向共用遞增計數，如 a7 00 00 00 → a8 00 00 00）
"""

import re
from typing import Any, Callable, Dict, List, Optional

from .des_cipher import DESCipher, trim_iso9797_padding
from .handshake import extract_handshake_key
from .sproto import Schema, SessionTable, SprotoError, dispatch, sproto_unpack

MAX_BUFFER_BYTES = 1 << 20


# S→C 握手行：`base64(uid|subId|key)\n`、`base64("200 ok")\n`。
# 只接受純 base64 字元，避免把長度 >= 8192（高位元組落在可見 ASCII）的大訊框誤判成明文行。
# 至少 4 字元：長度高位元組為 0x0A 的訊框（2560~2815 bytes）不可被當成空行吃掉。
_HANDSHAKE_LINE = re.compile(rb"[A-Za-z0-9+/=]{4,128}\n")
_B64_PREFIX = re.compile(rb"[A-Za-z0-9+/=]{2,128}\Z")


def _handshake_line_len(buf) -> Optional[int]:
    m = _HANDSHAKE_LINE.match(bytes(buf[:130]))
    return m.end() if m else None


def _is_printable(b) -> bool:
    return len(b) > 0 and all(0x20 <= x < 0x7F or x == 0x0A for x in b)


class Connection:
    def __init__(self) -> None:
        self.cipher: Optional[DESCipher] = None
        self.uid: Optional[str] = None
        self.sessions = SessionTable()
        self.buffers: Dict[str, bytearray] = {"c2s": bytearray(), "s2c": bytearray()}


class PacketStream:
    def __init__(self, schema: Optional[Schema] = None) -> None:
        self.schema = schema or Schema.load()
        self.conns: Dict[str, Connection] = {}
        # 選用：on_frame(unpacked_bytes, events) 在每個成功解碼的訊框後呼叫（匯出 fixture 用）
        self.on_frame: Optional[Callable[[bytes, List[Dict[str, Any]]], None]] = None

    def set_key(self, conn: str, key: bytes) -> None:
        self.conns.setdefault(conn, Connection()).cipher = DESCipher(bytes(key)[:8])

    def feed(self, conn: str, direction: str, data: bytes, ts: float = 0.0) -> List[Dict[str, Any]]:
        c = self.conns.setdefault(conn, Connection())
        buf = c.buffers[direction]
        buf += data
        events: List[Dict[str, Any]] = []
        base = {"ts": ts, "conn": conn, "dir": direction}

        while len(buf) >= 2:
            line_len = _handshake_line_len(buf)
            if line_len is not None:
                line = bytes(buf[:line_len])
                del buf[:line_len]
                hs = extract_handshake_key(line)
                if hs:
                    c.cipher = DESCipher(hs["key"])
                    c.uid = hs["uid"]
                    c.sessions = SessionTable()
                    events.append({**base, "kind": "handshake", "name": "Handshake",
                                   "data": {"uid": hs["uid"], "sub_id": hs["sub_id"]}})
                else:
                    events.append({**base, "kind": "plaintext", "name": None,
                                   "data": {"text": line.decode("latin1").strip()[:40]}})
                continue
            if c.cipher is None and _B64_PREFIX.match(bytes(buf[:130])):
                break  # 握手行被 TCP 切段、換行還沒到：等下一段
            n = int.from_bytes(buf[:2], "big")
            if 4 < n and len(buf) >= n + 2 and _is_printable(buf[2:n - 2]):
                # C→S 握手 #1/#3：與加密訊框同格式 [N][N-4 明文 base64][4B 序號]，
                # 但長度不一定 8 對齊（含帳號，不輸出內容）
                del buf[:n + 2]
                events.append({**base, "kind": "plaintext", "name": "HandshakeRequest",
                               "data": {"len": n}})
                continue
            if not (12 <= n and (n - 4) % 8 == 0):
                # 不是合法訊框（可能是 C→S 帶長度前綴的明文握手 #1/#3）：丟掉 1 byte 重新對齊
                del buf[:1]
                continue
            if len(buf) < n + 2:
                break
            frame = bytes(buf[:n + 2])
            del buf[:n + 2]
            events.extend({**base, **ev} for ev in self._decode_frame(c, frame))

        if len(buf) > MAX_BUFFER_BYTES:
            buf.clear()
        return events

    def _decode_frame(self, c: Connection, frame: bytes) -> List[Dict[str, Any]]:
        n = int.from_bytes(frame[:2], "big")
        seq = int.from_bytes(frame[-4:], "little")
        if c.cipher is None:
            return [{"kind": "undecrypted", "name": None, "seq": seq, "data": {"len": n}}]
        try:
            plain = trim_iso9797_padding(c.cipher.decrypt_ecb(frame[2:n - 2]), strict=True)
            unpacked = sproto_unpack(plain)
        except (ValueError, SprotoError) as exc:
            return [{"kind": "decrypt_error", "name": None, "seq": seq,
                     "data": {"len": n, "error": str(exc)}}]
        try:
            evs = dispatch(self.schema, unpacked, c.sessions)
        except (SprotoError, KeyError, IndexError) as exc:
            return [{"kind": "decode_error", "name": None, "seq": seq,
                     "data": {"len": n, "error": str(exc)}}]
        if self.on_frame is not None:
            self.on_frame(unpacked, evs)
        return [{**ev, "seq": seq} for ev in evs]
