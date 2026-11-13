"""從本機遊戲檔自動抓客戶端版本（使用者 2026-10-23：「版本可以自動抓嗎」）。

版本號寫死在熱更新 DLL：`GameFrame::.cctor` 的 `ldstr '1.0.3'` → `stsfld IOS_NowVersion`。
維護改版後**遊戲主程式要關掉重開**才會熱更新（README §7），之後這裡讀到的就是新版本。

順序（`config.load`）：環境變數 `BOT_CLIENT_VER` → 本機遊戲檔 → `FALLBACK`（Mac 沒有遊戲檔時用）。
讀不到、讀到多個版本字串都回 None，不猜。
"""

import os
import re
import struct
from typing import Dict, Optional, Tuple

FALLBACK = "1.0.6"                      # 11-13 12:30 維護改版（使用者「先改版號」）；11-07 是 1.0.4 → 1.0.5
DEFAULT_BUNDLE = ""
BUNDLE_OFFSET = 331                     # bundle 去掉前 331 bytes 是標準 UnityFS（REVERSE-ENGINEERING §1）

FIELD = "IOS_NowVersion"
_VER = re.compile(r"^\d+\.\d+\.\d+$")
_cache: Dict[Tuple[str, float], Optional[str]] = {}


def version_from_il(dll: bytes, field_token: int, user_string) -> Optional[str]:
    """找 `ldstr <字串> ; stsfld <IOS_NowVersion>`（0x72 tttttttt 0x80 ffffffff）那一個字串。
    DLL 裡還有別的 x.y.z（例 1.0.0），所以一定要用欄位定位，不能只找版本樣式（2026-10-23 實測）。
    找不到、不像版本號、或同一欄位被指定成不同值都回 None，不猜。"""
    pat = b"\x80" + struct.pack("<I", field_token)
    found, i = set(), dll.find(pat)
    while i >= 5:
        if dll[i - 5] == 0x72:
            s = user_string(struct.unpack("<I", dll[i - 4:i])[0] & 0xFFFFFF)
            if isinstance(s, str) and _VER.match(s):
                found.add(s)
        i = dll.find(pat, i + 1)
    return found.pop() if len(found) == 1 else None


def version_from_dll(dll: bytes) -> Optional[str]:
    import dnfile                       # 只有讀本機遊戲檔才需要
    pe = dnfile.dnPE(data=dll)
    rows = pe.net.mdtables.Field.rows
    idx = [i + 1 for i, r in enumerate(rows) if str(r.Name) == FIELD]
    if len(idx) != 1:
        return None

    def us(offset):
        v = pe.net.user_strings.get(offset)
        return getattr(v, "value", v)
    return version_from_il(dll, 0x04000000 | idx[0], us)


def _load_dll(path: str) -> bytes:
    data = open(path, "rb").read()
    if data[:2] == b"MZ":
        return data
    import UnityPy                      # 只有讀 assetbundle 才需要（Windows 有遊戲檔的機器）
    env = UnityPy.load(data[BUNDLE_OFFSET:])
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        raw = obj.read().m_Script
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        if raw[:2] == b"MZ":
            return raw
    raise ValueError(f"{path} 內找不到 DLL")


def local_client_ver(path: Optional[str] = None) -> Optional[str]:
    """本機遊戲檔的版本；沒有遊戲檔或讀不出來回 None。依檔案修改時間快取（改版後自動重讀）。"""
    path = path or os.environ.get("BOT_GAME_BUNDLE") or DEFAULT_BUNDLE
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _cache:
        try:
            _cache[key] = version_from_dll(_load_dll(path))
        except Exception:               # noqa: BLE001 — 讀不到就退回預設，不讓登入因此失敗
            _cache[key] = None
    return _cache[key]
