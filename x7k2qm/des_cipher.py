"""des_cipher.py - 網路傳輸層 DES-ECB 解密核心。

移植自 參考專案/參考專案/core/des_cipher.py（另一個版本）。
本遊戲的 Crypt / SocketDriver IL 與另一個版本逐指令相同（見 doc/REVERSE-ENGINEERING.md §3），故原樣沿用：
1. 傳輸層訊框: [2-Byte Big-Endian 長度] + [DES-ECB 密文 (8-Byte 對齊)] + [4-Byte 序號尾碼]
2. 加密模式: DES-ECB，8-Byte 密鑰 (des_key)，Padding 格式為 0x80 (ISO/IEC 9797)
"""

from typing import Optional, Tuple

# 嘗試引用 cryptography 進行硬體加速，若無安裝則使用純 Python 內建 DES 實作
HAS_CRYPTOGRAPHY = False
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    try:  # cryptography >= 43 把 TripleDES 移到 decrepit
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


# ==============================================================================
# 純 Python 標準庫 DES 實作 (Zero Dependency Fallback)
# ==============================================================================

# Initial Permutation (IP)
_IP = [
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7
]

# Final Permutation (FP)
_FP = [
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25
]

# Permuted Choice 1 (PC-1)
_PC1 = [
    57, 49, 41, 33, 25, 17,  9,  1, 58, 50, 42, 34, 26, 18,
    10,  2, 59, 51, 43, 35, 27, 19, 11,  3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15,  7, 62, 54, 46, 38, 30, 22,
    14,  6, 61, 53, 45, 37, 29, 21, 13,  5, 28, 20, 12,  4
]

# Permuted Choice 2 (PC-2)
_PC2 = [
    14, 17, 11, 24,  1,  5,  3, 28, 15,  6, 21, 10,
    23, 19, 12,  4, 26,  8, 16,  7, 27, 20, 13,  2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32
]

# Expansion Table (E)
_E = [
    32,  1,  2,  3,  4,  5,  4,  5,  6,  7,  8,  9,
     8,  9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32,  1
]

# P-Box Permutation (P)
_P = [
    16,  7, 20, 21, 29, 12, 28, 17,  1, 15, 23, 26,  5, 18, 31, 10,
     2,  8, 24, 14, 32, 27,  3,  9, 19, 13, 30,  6, 22, 11,  4, 25
]

# S-Boxes (S1 ~ S8)
_SBOX = [
    # S1
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
      0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
      4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    # S2
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
      3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
      0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    # S3
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
      1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    # S4
    [ 7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
      3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    # S5
    [ 2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
      4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    # S6
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
      9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
      4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    # S7
    [ 4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
      1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
      6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    # S8
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
      1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
      7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
      2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]
]

# Shifts in subkey generation
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]


def _permute(block: int, table: list, in_len: int) -> int:
    out = 0
    for pos in table:
        out = (out << 1) | ((block >> (in_len - pos)) & 1)
    return out


class PurePythonDES:
    """純 Python 實現的標準 64-bit DES 區塊密碼演算法"""

    def __init__(self, key_bytes: bytes):
        if len(key_bytes) < 8:
            key_bytes = key_bytes.ljust(8, b"\0")
        elif len(key_bytes) > 8:
            key_bytes = key_bytes[:8]

        key_int = int.from_bytes(key_bytes, "big")
        self.subkeys = self._generate_subkeys(key_int)

    def _generate_subkeys(self, key_int: int) -> list:
        # PC-1: 64-bit key -> 56-bit
        c_d = _permute(key_int, _PC1, 64)
        c = (c_d >> 28) & 0x0FFFFFFF
        d = c_d & 0x0FFFFFFF

        subkeys = []
        for shift in _SHIFTS:
            c = ((c << shift) & 0x0FFFFFFF) | (c >> (28 - shift))
            d = ((d << shift) & 0x0FFFFFFF) | (d >> (28 - shift))
            cd = (c << 28) | d
            # PC-2: 56-bit -> 48-bit subkey
            subkeys.append(_permute(cd, _PC2, 56))
        return subkeys

    def _round(self, r: int, subkey: int) -> int:
        # Expansion: 32-bit -> 48-bit
        e_r = _permute(r, _E, 32)
        xor_res = e_r ^ subkey

        # S-Box Substitution: 48-bit -> 32-bit
        s_out = 0
        for i in range(8):
            chunk = (xor_res >> (42 - i * 6)) & 0x3F
            row = ((chunk & 0x20) >> 4) | (chunk & 1)
            col = (chunk >> 1) & 0x0F
            s_val = _SBOX[i][row * 16 + col]
            s_out = (s_out << 4) | s_val

        # P-Box Permutation
        return _permute(s_out, _P, 32)

    def decrypt_block(self, block_8bytes: bytes) -> bytes:
        return self._crypt(block_8bytes, range(15, -1, -1))     # 解密：子金鑰倒序

    def encrypt_block(self, block_8bytes: bytes) -> bytes:
        return self._crypt(block_8bytes, range(16))             # 加密：子金鑰正序（容器不裝 cryptography 時用）

    def _crypt(self, block_8bytes: bytes, order) -> bytes:
        block_int = int.from_bytes(block_8bytes, "big")
        # IP
        ip_block = _permute(block_int, _IP, 64)
        l = (ip_block >> 32) & 0xFFFFFFFF
        r = ip_block & 0xFFFFFFFF

        for i in order:
            next_l = r
            next_r = l ^ self._round(r, self.subkeys[i])
            l = next_l
            r = next_r

        # Swap L and R, then FP
        pre_output = (r << 32) | l
        fp_block = _permute(pre_output, _FP, 64)
        return fp_block.to_bytes(8, "big")


# ==============================================================================
# DES-ECB 解密管理器 (自動適應硬體加速或純 Python 引擎)
# ==============================================================================

class DESCipher:
    """DES-ECB 解密封裝器"""

    def __init__(self, key_bytes: bytes):
        if len(key_bytes) < 8:
            key_bytes = key_bytes.ljust(8, b"\0")
        elif len(key_bytes) > 8:
            key_bytes = key_bytes[:8]

        self.key = key_bytes
        self.use_cryptography = HAS_CRYPTOGRAPHY

        if not self.use_cryptography:
            self._pure_des = PurePythonDES(self.key)

    def decrypt_ecb(self, ciphertext: bytes) -> bytes:
        """解密 8-Byte 整數倍長度的密文串流"""
        if not ciphertext or len(ciphertext) % 8 != 0:
            return b""

        if self.use_cryptography:
            try:
                # 3DES 3-key 相同時等價於標準單重 DES
                cipher = Cipher(TripleDES(self.key * 3), modes.ECB())
                decryptor = cipher.decryptor()
                return decryptor.update(ciphertext) + decryptor.finalize()
            except Exception:
                pass

        # Fallback 至純 Python 實作
        out = bytearray()
        for i in range(0, len(ciphertext), 8):
            out.extend(self._pure().decrypt_block(ciphertext[i:i + 8]))
        return bytes(out)

    def encrypt_ecb(self, plaintext: bytes) -> bytes:
        """加密 8-Byte 整數倍長度的明文（呼叫端自己補齊）；長度不對直接丟錯，不默默送出壞封包。"""
        if len(plaintext) % 8 != 0:
            raise ValueError("DES-ECB 明文長度要是 8 的倍數")
        if self.use_cryptography:
            enc = Cipher(TripleDES(self.key * 3), modes.ECB()).encryptor()
            return enc.update(plaintext) + enc.finalize()
        out = bytearray()
        for i in range(0, len(plaintext), 8):
            out.extend(self._pure().encrypt_block(plaintext[i:i + 8]))
        return bytes(out)

    def _pure(self) -> "PurePythonDES":
        if getattr(self, "_pure_des", None) is None:
            self._pure_des = PurePythonDES(self.key)
        return self._pure_des


def trim_iso9797_padding(data: bytes, strict: bool = False) -> bytes:
    """
    還原 Crypt.cs 中採用的 0x80 Padding 格式:
    從末尾往前回溯找到第一個 0x80，若是填充字元則截斷。
    若 strict=True 且未發現 0x80 填充標記，則拋出 ValueError 防止誤認偽金鑰。
    """
    if not data:
        if strict:
            raise ValueError("Empty data in ISO9797 padding check")
        return b""
    idx = len(data) - 1
    # 略過結尾可能出現的 0x00
    while idx >= 0 and data[idx] == 0x00:
        idx -= 1
    if idx >= 0 and data[idx] == 0x80:
        return data[:idx]
    if strict:
        raise ValueError("Invalid ISO9797 padding: no 0x80 marker")
    return data


def unpack_legacy_packet(raw_tcp_payload: bytes, des_key: bytes) -> Optional[Tuple[bytes, int]]:
    """
    依據 SocketDriver.CheckPacketComplte 結構拆解並解密封包：
    格式: [2-Byte Big-Endian 長度 N] + [N-4 Bytes 密文] + [4-Byte 序號/校驗]
    回傳: (解密後明文 bytes, 封包標稱長度)
    """
    if len(raw_tcp_payload) < 6:
        return None

    # 前 2 Bytes: big-endian dataSize
    data_size = int.from_bytes(raw_tcp_payload[:2], "big")
    if len(raw_tcp_payload) < data_size + 2:
        return None

    # 取出密文: 長度為 data_size - 4
    cipher_len = data_size - 4
    if cipher_len <= 0 or (cipher_len % 8 != 0):
        # 若非加密封包或長度不合，直接嘗試取完整 payload
        return (raw_tcp_payload[2:2 + data_size], data_size)

    cipher_bytes = raw_tcp_payload[2:2 + cipher_len]
    # trailer_bytes = raw_tcp_payload[2 + cipher_len:2 + data_size]  # 4 bytes 序號拋棄

    # 執行 DES-ECB 解密
    cipher = DESCipher(des_key)
    decrypted = cipher.decrypt_ecb(cipher_bytes)

    # 去除結尾 0x80 Padding (ISO/IEC 9797 填充: 0x80 後接可選的 0x00)
    plain = trim_iso9797_padding(decrypted)
    return (plain, data_size)
