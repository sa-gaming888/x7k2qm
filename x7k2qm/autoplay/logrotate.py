"""日誌輪替：超過大小就換新檔，舊檔壓成 .gz，只保留最近幾份（使用者 2026-10-21：rotate、compress、delete）。

由寫檔的程式自己輪替（Windows 上檔案開著時無法從外面改名），所以 watch.py、supervisor.py 都用這個類別寫檔。
"""

import glob
import gzip
import os
import shutil
import time
from typing import List


RETRY_BYTES = 1024 * 1024


class RotatingFile:
    def __init__(self, path: str, max_bytes: int = 50 * 1024 * 1024, keep: int = 10) -> None:
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        # 全新 clone 沒有 captures/ → 先建資料夾（2026-10-22 Mac 第一次跑時 FileNotFoundError）
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.fp = open(path, "a", encoding="utf-8")
        self.retry_at = 0                   # 改名失敗後，寫到這個大小才再試

    def write(self, text: str) -> None:
        self.fp.write(text)
        self.fp.flush()
        if self.fp.tell() >= max(self.max_bytes, self.retry_at):
            self.rotate()

    def rotate(self) -> str:
        """關檔 → 改名加時間 → 壓縮 → 刪多餘的 → 開新檔。回傳壓縮檔路徑；這次沒輪替回空字串。"""
        self.fp.close()
        # 檔名＝日期時間＋同一秒內的奈秒（固定 9 位）：只會遞增，刪掉舊檔也不會重用，排序就是新舊順序
        ns = time.time_ns()
        rotated = f"{self.path}.{time.strftime('%Y%m%d-%H%M%S', time.localtime(ns / 1e9))}-{ns % 10**9:09d}"
        try:
            os.replace(self.path, rotated)
        except PermissionError:
            # Windows：別的行程也開著這個檔就改不了名（2026-11-02 第三組練功佔著 jobs.log，第二組查寵物寫滿 5MB
            # 就崩潰、四隻沒登出，LESSONS #137）。寫日誌不能弄崩工作：繼續寫原檔，再寫 1MB 才試下一次。
            self.fp = open(self.path, "a", encoding="utf-8")
            self.retry_at = self.fp.tell() + RETRY_BYTES
            return ""
        self.retry_at = 0
        with open(rotated, "rb") as src, gzip.open(rotated + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(rotated)
        self.prune()
        self.fp = open(self.path, "a", encoding="utf-8")
        return rotated + ".gz"

    def archives(self) -> List[str]:
        return sorted(glob.glob(glob.escape(self.path) + ".*.gz"))

    def prune(self) -> None:
        old = self.archives()
        for p in old[:-self.keep] if self.keep else old:
            os.remove(p)

    def flush(self) -> None:
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()
