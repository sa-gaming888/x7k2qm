"""pcap.py - 讀 pcap/pcapng，依「連線 + 方向」重組 TCP payload（純 Python，不需 tshark）。

參考專案 的舊版以 tcp.stream 為單位把雙向資料塞進同一個緩衝區，靠「解不開就前進
1 byte」掩蓋錯位；這裡改成每個方向各自依 TCP 序號重組。
"""

import socket
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

try:                     # 只有讀擷取檔才需要；無畫面客戶端／容器版不裝（live.py 只用 Reassembler、Segment）
    import dpkt
except ImportError:      # pragma: no cover - 容器版
    dpkt = None

GAME_PORT = 11000


@dataclass
class Segment:
    ts: float
    conn: str        # "<client_ip>:<client_port>"，雙方向共用
    direction: str   # "c2s" | "s2c"
    seq: int
    payload: bytes


def _open_reader(fp):
    magic = fp.read(4)
    fp.seek(0)
    if magic == b"\x0a\x0d\x0d\x0a":
        return dpkt.pcapng.Reader(fp)
    return dpkt.pcap.Reader(fp)


def _ip_of(buf: bytes, datalink: int):
    if datalink == dpkt.pcap.DLT_EN10MB:
        eth = dpkt.ethernet.Ethernet(buf)
        return eth.data
    if datalink in (dpkt.pcap.DLT_NULL, dpkt.pcap.DLT_LOOP):
        return dpkt.ip.IP(buf[4:])
    if datalink == dpkt.pcap.DLT_RAW or datalink == 101:
        return dpkt.ip.IP(buf)
    return dpkt.ethernet.Ethernet(buf).data


def iter_segments(path: str, port: int = GAME_PORT) -> Iterator[Segment]:
    if dpkt is None:
        raise RuntimeError("讀擷取檔要先安裝 dpkt（pip install dpkt）")
    with open(path, "rb") as fp:
        reader = _open_reader(fp)
        datalink = reader.datalink()
        for ts, buf in reader:
            try:
                ip = _ip_of(buf, datalink)
            except Exception:
                continue
            if not isinstance(ip, (dpkt.ip.IP, dpkt.ip6.IP6)):
                continue
            tcp = ip.data
            if not isinstance(tcp, dpkt.tcp.TCP) or not tcp.data:
                continue
            fam = socket.AF_INET6 if isinstance(ip, dpkt.ip6.IP6) else socket.AF_INET
            if tcp.dport == port:
                direction = "c2s"
                conn = f"{socket.inet_ntop(fam, ip.src)}:{tcp.sport}"
            elif tcp.sport == port:
                direction = "s2c"
                conn = f"{socket.inet_ntop(fam, ip.dst)}:{tcp.dport}"
            else:
                continue
            yield Segment(float(ts), conn, direction, tcp.seq, bytes(tcp.data))


class Reassembler:
    """單一方向的 TCP 重組：依序號輸出新資料，丟掉重傳、暫存亂序片段。"""

    MAX_PENDING = 256

    def __init__(self) -> None:
        self.next_seq: Optional[int] = None
        self.pending: Dict[int, bytes] = {}

    def push(self, seq: int, data: bytes) -> bytes:
        if self.next_seq is None:
            self.next_seq = seq
        out = bytearray()
        self.pending[seq] = data
        progressed = True
        while progressed:
            progressed = False
            for s in sorted(self.pending):
                d = self.pending[s]
                rel = (s - self.next_seq) & 0xFFFFFFFF
                if rel >= 0x80000000:  # 片段起點在 next_seq 之前（重傳/重疊）
                    overlap = (self.next_seq - s) & 0xFFFFFFFF
                    del self.pending[s]
                    if overlap < len(d):
                        out += d[overlap:]
                        self.next_seq = (self.next_seq + len(d) - overlap) & 0xFFFFFFFF
                    progressed = True
                    break
                if rel == 0:
                    del self.pending[s]
                    out += d
                    self.next_seq = (self.next_seq + len(d)) & 0xFFFFFFFF
                    progressed = True
                    break
        if len(self.pending) > self.MAX_PENDING:
            # 缺口一直補不上（擷取漏包）：跳過缺口，從最小的暫存片段繼續
            self.next_seq = min(self.pending, key=lambda s: (s - self.next_seq) & 0xFFFFFFFF)
            out += self.push(self.next_seq, self.pending.pop(self.next_seq))
        return bytes(out)


def iter_streams(path: str, port: int = GAME_PORT) -> Iterator[Tuple[float, str, str, bytes]]:
    """產生 (ts, conn, direction, 依序重組後的新位元組)。"""
    asm: Dict[Tuple[str, str], Reassembler] = {}
    for seg in iter_segments(path, port):
        r = asm.setdefault((seg.conn, seg.direction), Reassembler())
        data = r.push(seg.seq, seg.payload)
        if data:
            yield seg.ts, seg.conn, seg.direction, data


def split_frames(buf: bytes) -> Tuple[List[bytes], int]:
    """從緩衝區切出完整加密訊框 [2B BE N][N-4 密文][4B 序號]，回傳 (訊框清單, 用掉的位元組數)。

    只做長度切割，不處理明文握手行（那由 stream.py 負責）。
    """
    frames = []
    pos = 0
    while pos + 2 <= len(buf):
        n = int.from_bytes(buf[pos:pos + 2], "big")
        if pos + 2 + n > len(buf):
            break
        frames.append(buf[pos:pos + 2 + n])
        pos += 2 + n
    return frames, pos
