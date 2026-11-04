"""handshake.py - 登入握手 #2 明文金鑰萃取。

移植自 參考專案/參考專案/core/handshake.py（另一個版本）；Login/LoginAuth IL 與另一個版本相同。

登入握手 #2 為 S->C 明文封包，內容是 base64(`<uid>|<subId>|<8-byte des_key>`)。
每連線一把 DES 金鑰，錄製/解析都要涵蓋該連線的登入握手才解得開。
"""

import base64
import re
from typing import Optional, TypedDict


class HandshakeInfo(TypedDict):
    uid: str
    sub_id: str
    key: bytes


_BASE64_CANDIDATE = re.compile(rb"[A-Za-z0-9+/=]{16,80}")


def extract_handshake_key(raw_bytes: bytes) -> Optional[HandshakeInfo]:
    """偵測 raw_bytes 中是否含登入握手 #2 (base64 `<uid>|<subId>|<8-byte key>`)。

    找到即回傳 {"uid": str, "sub_id": str, "key": bytes}（key 為 8-byte DES 金鑰）；
    找不到回傳 None。呼叫端負責把 key 綁到對應的 stream_id、並確保任何要落地/匯出
    的資料都不含這裡回傳的 key 原始值。
    """
    if not raw_bytes:
        return None

    for m in _BASE64_CANDIDATE.findall(raw_bytes):
        try:
            decoded = base64.b64decode(m, validate=False).decode("latin1", errors="replace")
        except Exception:
            continue
        if "|" not in decoded:
            continue
        parts = decoded.split("|")
        # 登入握手 #2 格式為: <uid>|<subId>|<8-byte des_key>
        if len(parts) >= 3 and len(parts[2]) == 8:
            uid_str, sub_id, des_key_str = parts[0], parts[1], parts[2]
            return {
                "uid": uid_str,
                "sub_id": sub_id,
                "key": des_key_str.encode("latin1"),
            }
    return None


def leading_plaintext_line_len(buf: bytes) -> Optional[int]:
    """若 buf 開頭是一行『\\n 結尾的可見 ASCII 明文行』(登入/熱重登握手前導，
    例如 base64 的 `<uid>|<subId>|<des_key>`、或 `200 ok`)，回傳該行長度
    (含結尾 \\n)，否則回傳 None。

    二進位訊框開頭為 2-Byte BE 長度，實務上觀察到的訊框長度多半遠小於
    8192 bytes，故長度高位位元組通常 < 0x20 (非可見字元)；明文握手行則全為可見
    ASCII 且以 \\n 收尾。以此區分兩者，避免把明文握手行誤讀成訊框長度標頭而導致
    整條連線錯位。

    S3(code review 記錄，2026-09-24，刻意不改邏輯只留註記)：這裡假設的 8192 上限
    跟 `core/stream.py` 實際驗證訊框合法性用的 `12 <= data_size <= 65535` 並不
    完全一致——理論上若真的出現 data_size 落在 8192~32512 之間(高位元組落在可見
    ASCII 範圍 0x20~0x7E)，這個判斷式可能誤把二進位訊框開頭當成明文行。實務上尚未
    觀察過這麼大的訊框，暫不處理，之後若要處理請同時檢視這兩處的假設是否一致。
    """
    limit = min(len(buf), 96)
    for i in range(limit):
        c = buf[i]
        if c == 0x0A:  # \n 收尾 -> 確認是一整行明文
            return i + 1
        if not (0x20 <= c <= 0x7E):  # 出現非可見字元 -> 是二進位訊框，不是明文行
            return None
    return None  # 96 bytes 內沒 \n -> 當二進位/尚未收齊處理
