"""live.py - 即時模式：tshark 逐封包輸出欄位 → TCP 重組 → PacketStream 解碼 → 事件佇列。

讓自動化操作（x7k2qm/game、x7k2qm/features/*）可以「點一下 → 等到對應的回應封包 → 馬上做下一步」，
不必固定空等。tshark 同時用 -w 留一份 pcapng（-P 讓它邊寫檔邊印欄位）。
"""

import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .pcap import GAME_PORT, Reassembler, Segment
from .stream import PacketStream

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
FIELDS = ["frame.time_epoch", "ip.src", "tcp.srcport", "ip.dst", "tcp.dstport",
          "tcp.seq_raw", "tcp.payload"]

Event = Dict[str, Any]


def parse_field_line(line: str, port: int = GAME_PORT) -> Optional[Segment]:
    """tshark -T fields 的一行 → Segment；沒有 payload 或不是遊戲埠就回傳 None。"""
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != len(FIELDS) or not parts[6]:
        return None
    ts, src, sport, dst, dport, seq, payload = parts
    try:
        sport_i, dport_i = int(sport), int(dport)
        data = bytes.fromhex(payload.replace(":", ""))
    except ValueError:
        return None
    if dport_i == port:
        return Segment(float(ts), f"{src}:{sport_i}", "c2s", int(seq), data)
    if sport_i == port:
        return Segment(float(ts), f"{dst}:{dport_i}", "s2c", int(seq), data)
    return None


class EventQueue:
    """事件佇列：wait_for 等特定事件、drain 清空；LiveDecoder 與無畫面 GameClient 共用這個介面。"""

    def __init__(self) -> None:
        self.events: "queue.Queue[Event]" = queue.Queue()

    def wait_for(self, pred: Callable[[Event], bool], timeout: float,
                 on_event: Optional[Callable[[Event], None]] = None) -> Optional[Event]:
        """取出事件直到 pred 成立（回傳該事件）或逾時（回傳 None）。途中每個事件都交給 on_event。"""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                e = self.events.get(timeout=left)
            except queue.Empty:
                return None
            if on_event:
                on_event(e)
            if pred(e):
                return e

    def drain(self, on_event: Optional[Callable[[Event], None]] = None) -> None:
        while True:
            try:
                e = self.events.get_nowait()
            except queue.Empty:
                return
            if on_event:
                on_event(e)


class LiveDecoder(EventQueue):
    """把 tshark 欄位行餵進重組器與 PacketStream，事件放進佇列。可脫離 tshark 單獨測試。"""

    def __init__(self, stream: Optional[PacketStream] = None, port: int = GAME_PORT) -> None:
        super().__init__()
        self.stream = stream or PacketStream()
        self.port = port
        self.asm: Dict[Tuple[str, str], Reassembler] = {}

    def feed_line(self, line: str) -> List[Event]:
        seg = parse_field_line(line, self.port)
        if seg is None:
            return []
        r = self.asm.setdefault((seg.conn, seg.direction), Reassembler())
        data = r.push(seg.seq, seg.payload)
        if not data:
            return []
        evs = self.stream.feed(seg.conn, seg.direction, data, seg.ts)
        for e in evs:
            self.events.put(e)
        return evs

    def feed_lines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.feed_line(line)


class LiveCapture(LiveDecoder):
    """啟動 tshark 並在背景執行緒解碼。用 with 包起來，結束時會停掉 tshark。"""

    def __init__(self, iface: str = "乙太網路", pcap_out: Optional[str] = None,
                 tshark: str = TSHARK, port: int = GAME_PORT) -> None:
        super().__init__(port=port)
        cmd = [tshark, "-i", iface, "-f", f"tcp port {port}", "-l", "-n", "-T", "fields",
               "-E", "separator=/t"]
        for f in FIELDS:
            cmd += ["-e", f]
        if pcap_out:
            cmd += ["-P", "-w", pcap_out]
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None

    def __enter__(self) -> "LiveCapture":
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="ascii", errors="replace", bufsize=1)
        self.thread = threading.Thread(target=self.feed_lines, args=(self.proc.stdout,), daemon=True)
        self.thread.start()
        time.sleep(1.5)  # 等 tshark 開始擷取
        return self

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
