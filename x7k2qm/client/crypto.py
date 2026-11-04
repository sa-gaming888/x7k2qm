"""登入用的加解密：與客戶端 HotUpdate.dll 的 Crypt、DataManager.Obfuscate 逐行對應。

Crypt.hashkey / Crypt.hmac64 是 skynet lua-crypt.c 的 C# 移植（IL 逐指令比對：DJB + JS hash、
以 MD5 輪函式為骨架的 64-bit HMAC；常數表 k、r 為標準 MD5 值）。已用真實登入擷取驗證簽名相符。
"""

import math
import struct

from ..des_cipher import DESCipher   # 有 cryptography 用它（3DES 三把同 key）；沒有用純 Python（容器版）

_M32 = 0xFFFFFFFF
_K = [int(abs(math.sin(i + 1)) * 2 ** 32) & _M32 for i in range(64)]
_R = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4

# DataManager.encryptionMap（.cctor 的 64 組 Add）：HTTP 登入的帳號/密碼逐字元替換
_OBF_SRC = "#0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
_OBF_DST = "_9876543210QXZNYUKOJMFVTWAPGLRISCBEHD#qxznyukojmfvtwapglriscbehd"
_OBF = dict(zip(_OBF_SRC, _OBF_DST))
_DEOBF = dict(zip(_OBF_DST, _OBF_SRC))


def hashkey(s: bytes) -> bytes:
    """Crypt.Hash：8 bytes = DJB hash（LE）+ JS hash（LE）。"""
    djb, js = 5381, 1315423911
    for c in s:
        djb = (djb + (djb << 5) + c) & _M32
        js = (js ^ (((js << 5) & _M32) + c + (js >> 2))) & _M32
    return struct.pack("<II", djb, js)


def _rol(x: int, c: int) -> int:
    return ((x << c) | (x >> (32 - c))) & _M32


def hmac64(x: bytes, y: bytes) -> bytes:
    """Crypt.hmac64(x, y)：x、y 各 8 bytes，回傳 8 bytes。"""
    if len(x) != 8 or len(y) != 8:
        raise ValueError("hmac64 需要兩個 8 bytes 參數")
    x0, x1 = struct.unpack("<II", x)
    y0, y1 = struct.unpack("<II", y)
    w = [x1, x0, y1, y0] * 4
    a, b, c, d = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    for i in range(64):
        if i < 16:
            f, g = (b & c) | (~b & d), i
        elif i < 32:
            f, g = (d & b) | (~d & c), (5 * i + 1) % 16
        elif i < 48:
            f, g = b ^ c ^ d, (3 * i + 5) % 16
        else:
            f, g = c ^ (b | (~d & _M32)), (7 * i) % 16
        f &= _M32
        d, c, b, a = c, b, (b + _rol((a + f + _K[i] + w[g]) & _M32, _R[i])) & _M32, d
    return struct.pack("<II", c ^ d, a ^ b)


def obfuscate(s: str) -> str:
    return "".join(_OBF.get(ch, ch) for ch in s)


def deobfuscate(s: str) -> str:
    return "".join(_DEOBF.get(ch, ch) for ch in s)


def des_encrypt(key: bytes, packed: bytes) -> bytes:
    """Crypt.desencode：補 0x80 再補 0 到 (len+8)&~7（一定會補），DES-ECB 加密。"""
    padded = packed + b"\x80"
    padded += b"\0" * (-len(padded) % 8)
    return DESCipher(bytes(key)[:8]).encrypt_ecb(padded)
