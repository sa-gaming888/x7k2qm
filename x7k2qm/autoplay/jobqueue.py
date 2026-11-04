"""工作佇列（檔案）：常駐練功程式在兩場戰鬥之間用自己的連線做固定指令，不另外登入（參考 參考專案 `tools/headless/jobs`）。

    <base>/<profile>/queue/<id>.json     排隊中 {id, name, argv, ts}（jobs add 寫；jobs cancel 刪）
    <base>/<profile>/running/<id>.json   做到一半（常駐取出時 os.replace 搬過來；行程死掉會留著，不會自動重做）
    <base>/<profile>/done/<id>.json      結果 {id, name, argv, status, detail, data, ts, started, ended}

結果狀態 `status`：
- `ok`：做完。
- `failed`：工作失敗（JobError），常駐照常回練功。
- `hold`：送過不可逆／花費封包後對不上（JobHold），或工作程式例外（狀態不明）→ 常駐**停在原地不回練功**，
  等人處理（`jobs add resume` 或重啟常駐）。
- `cancelled`：還沒開始就被 `jobs cancel` 取消。
- `expired`：排隊超過 `MAX_AGE` 秒還沒輪到（避免很久以前排的工作突然跑掉），沒做。

一次只做一件：`take()` 用 os.replace 搬檔，`cancel()` 用 os.remove 刪排隊檔，兩邊只會有一邊成功。
`base` 一律由呼叫端傳（預設目錄只在 CLI 的 `default_dir()`：環境變數 `BOT_JOBS_DIR`，沒有才用 `captures/jobs`）。
"""

import json
import os
import random
import time
from typing import Callable, List, Optional

MAX_AGE = 1800.0            # 排隊超過 30 分鐘還沒做 → expired
STATUSES = ("ok", "failed", "hold", "cancelled", "expired")


def default_dir() -> str:
    return os.environ.get("BOT_JOBS_DIR") or os.path.join("captures", "jobs")


def _write(p: str, d: dict) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, p)


def _read(p: str) -> Optional[dict]:
    try:
        with open(p, encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class Queue:
    def __init__(self, base: str, profile: str):
        self.root = os.path.join(base, str(profile))
        self.profile = str(profile)

    def _dir(self, kind: str) -> str:
        return os.path.join(self.root, kind)

    def _path(self, kind: str, jid: str) -> str:
        if not jid or any(c in jid for c in "/\\."):
            raise ValueError(f"工作編號不合法：{jid!r}")
        return os.path.join(self._dir(kind), f"{jid}.json")

    def _list(self, kind: str) -> List[dict]:
        d = self._dir(kind)
        if not os.path.isdir(d):
            return []
        out = [_read(os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith(".json")]
        return [j for j in out if j]

    # ---- 排隊端（jobs add／status／cancel）----
    def add(self, name: str, argv: List[str], now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        jid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{random.randrange(16 ** 4):04x}"
        job = {"id": jid, "name": name, "argv": list(argv), "ts": now}
        _write(self._path("queue", jid), job)
        return job

    def pending(self) -> List[dict]:
        return sorted(self._list("queue"), key=lambda j: (j.get("ts") or 0, j.get("id")))

    def running(self) -> List[dict]:
        return self._list("running")

    def result(self, jid: str) -> Optional[dict]:
        return _read(self._path("done", jid))

    def recent(self, n: int = 10) -> List[dict]:
        return sorted(self._list("done"), key=lambda j: (j.get("ended") or 0, j.get("id")))[-n:]

    def cancel(self, jid: str, now: Optional[float] = None) -> bool:
        """只能取消還沒開始的；已經被取出（running／done）回 False。"""
        p = self._path("queue", jid)
        job = _read(p)
        try:
            os.remove(p)
        except FileNotFoundError:
            return False
        self._done(job or {"id": jid}, "cancelled", "還沒開始就被取消（jobs cancel）", {}, None, now)
        return True

    def wait(self, jid: str, timeout: float, still_there: Callable[[], bool] = lambda: True,
             poll: float = 1.0, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> Optional[dict]:
        """等結果最多 timeout 秒；常駐不在了（still_there 回 False）也不再等。回結果或 None。"""
        end = clock() + timeout
        while True:
            r = self.result(jid)
            if r is not None:
                return r
            if not still_there() or clock() >= end:
                return self.result(jid)
            sleep(poll)

    # ---- 常駐端（headless_party 主迴圈）----
    def take(self, now: Optional[float] = None) -> Optional[dict]:
        """取最舊的一件搬到 running/；過期的直接寫 expired 跳過。沒有回 None。"""
        now = time.time() if now is None else now
        for job in self.pending():
            src = self._path("queue", job["id"])
            if now - (job.get("ts") or now) > MAX_AGE:
                try:
                    os.remove(src)
                except FileNotFoundError:
                    continue
                self._done(job, "expired", f"排隊超過 {MAX_AGE:.0f} 秒沒輪到，沒做", {}, None, now)
                continue
            dst = self._path("running", job["id"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.replace(src, dst)
            except FileNotFoundError:               # 剛好被取消
                continue
            return job
        return None

    def finish(self, job: dict, status: str, detail: str, data: Optional[dict] = None,
               started: Optional[float] = None, now: Optional[float] = None) -> dict:
        r = self._done(job, status, detail, data or {}, started, now)
        try:
            os.remove(self._path("running", job["id"]))
        except FileNotFoundError:
            pass
        return r

    def _done(self, job: dict, status: str, detail: str, data: dict, started: Optional[float],
              now: Optional[float]) -> dict:
        if status not in STATUSES:
            raise ValueError(f"不認得的結果狀態 {status}")
        r = dict(job)
        r.update(status=status, detail=detail, data=data, started=started,
                 ended=time.time() if now is None else now)
        _write(self._path("done", job["id"]), r)
        return r
