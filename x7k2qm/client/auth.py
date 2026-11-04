"""登入握手 #1~#4（AccountNetManager.GetTokenInfo / ContrastToken、GameScoket.TcpSend）。

  #1 C→S  [2B BE 長度] b64("1a")|b64(帳號)|b64(密碼)|b64(節點)|b64(pcId)          （TcpSend 不附序號）
  #2 S→C  b64("uid|subId|des_key")\\n
  #3 C→S  [2B BE 長度] b64("2a")|<s>:b64(hmac64(hashkey(<s>), des_key))|rid|[4B LE 序號]
          <s> = b64(uid)@b64(節點)#b64(subId):index                                （skynet msgserver 格式）
  #4 S→C  b64("200 ok")\\n

#1 消耗序號 1，所以 #3 的序號是 2，之後每個加密訊框 +1。
"""

import base64
import struct
from dataclasses import dataclass
from typing import Optional

from .crypto import hashkey, hmac64

TOKEN1 = b"1a"  # AccountNetManager.token1
TOKEN2 = b"2a"  # AccountNetManager.token2


def _b64(b) -> bytes:
    return base64.b64encode(b if isinstance(b, bytes) else str(b).encode("utf-8"))


def frame_plain(payload: bytes, seq: Optional[int] = None) -> bytes:
    """TcpSend：[2B BE 長度][payload]（+[4B LE 序號]，長度含序號）。"""
    tail = b"" if seq is None else struct.pack("<I", seq)
    return struct.pack(">H", len(payload) + len(tail)) + payload + tail


def token_request(account: str, password: str, node: str, pc_id: str) -> bytes:
    """握手 #1 的完整訊框。"""
    payload = b"|".join([_b64(TOKEN1), _b64(account), _b64(password), _b64(node), _b64(pc_id)])
    return frame_plain(payload)


@dataclass
class TokenInfo:
    uid: int
    sub_id: int
    key: bytes

    def __repr__(self) -> str:  # 不把金鑰印出來
        return f"TokenInfo(uid={self.uid}, sub_id={self.sub_id}, key=<{len(self.key)} bytes>)"


class AuthError(RuntimeError):
    pass


def parse_token_response(line: bytes) -> TokenInfo:
    """握手 #2：base64 一行 → uid、subId、8 bytes DES 金鑰。"""
    try:
        text = base64.b64decode(line.strip()).decode("latin1")
    except Exception as e:
        raise AuthError(f"握手 #2 不是 base64：{e}") from None
    parts = text.split("|")
    if len(parts) < 3 or len(parts[2]) != 8:
        raise AuthError(f"握手 #2 格式不符（{len(parts)} 段）：帳密錯誤或伺服器拒絕")
    return TokenInfo(int(parts[0]), int(parts[1]), parts[2].encode("latin1"))


def contrast_string(uid: int, node: str, sub_id: int, index: int, key: bytes) -> bytes:
    """skynet 握手字串 <s>:b64(hmac)。"""
    s = _b64(str(uid)) + b"@" + _b64(node) + b"#" + _b64(str(sub_id)) + b":" + str(index).encode()
    return s + b":" + _b64(hmac64(hashkey(s), key))


def contrast_request(info: TokenInfo, node: str, rid: int, index: int = 1, seq: int = 2) -> bytes:
    """握手 #3 的完整訊框。"""
    payload = _b64(TOKEN2) + b"|" + contrast_string(info.uid, node, info.sub_id, index, info.key) \
        + b"|" + str(rid).encode() + b"|"
    return frame_plain(payload, seq)


def check_contrast_response(line: bytes) -> None:
    """握手 #4：要是 base64("200 ...")。"""
    try:
        text = base64.b64decode(line.strip()).decode("latin1")
    except Exception:
        text = line.decode("latin1", "replace")
    if not text.startswith("200"):
        raise AuthError(f"握手 #4 失敗：{text.strip()[:40]}")
