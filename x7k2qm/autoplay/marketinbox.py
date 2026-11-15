"""交易所遠端指令信箱（使用者 2026-11-15「我希望未來買交易所商品，我可以 api 把完整資訊通知帳號，他檢查過沒問題，
會自動去交易所上下架跟領取所有販賣道具寵物」；入口選「走管理後台 Worker」、領取「只在指令要求時領」）。

放在狀態保存同一個地方（`STATE_URL`＝`https://<worker>/state/<ns>`、標頭 `X-State-Key`），一個帳號兩個物件：
- `<帳號>-mcmd`：指令（使用者寫，帳號只讀）
  `{"v":1, "account":"<帳號>", "cmds":[{"id":"20261115a", "ts":<送出時 Unix 秒>, "job":"auction", "item":2400, "price":1000000,
   "apply":true}, ...]}`
- `<帳號>-mres`：結果（帳號寫）`{"v":1, "account", "updated", "results": {"<id>": {"state", "jobId", "detail", "data", "ts"}}}`

可用的工作與欄位＝查詢 API `POST /jobs` 的白名單（`party_api.JOB_POSTS`：auction 道具上架、petauction 個倉寵物上架、
stall 攤位列出／領取／下架）。`apply` 不給只列（不動攤位）。

**檢查**（沒過就記 rejected、不排）：account 要等於這個帳號；id 英數 1～40 字、沒看過；ts 在 1 小時內（停機很久再開，
舊指令不做）；工作與欄位在白名單；工作自己的參數檢查（`check`）。排進佇列後，工作執行時還有自己的檢查
（道具在不在、綁定、攤位滿了、身上放不放得下…），任何一項不對就停、不重送（規範 2）。

**不重做**：新指令先把「accepted」寫回遠端，寫成功才排進佇列（工作編號固定 `m-<id>`）；寫失敗這輪不排。
重開後看到 accepted 卻找不到那件工作（Pod 重開時正在做、或結果沒寫回）→ 記 lost，不重排；要再做請送新的 id。
狀態：rejected、accepted、queued、running、ok、failed、hold、expired、cancelled、lost。

帳號端：容器設 `MARKET_INBOX=1`（要有 `STATE_URL`），容器裡開背景執行緒每 INTERVAL 秒讀一次（`Inbox`）。
送指令／看結果（使用者這台，密鑰讀 `~/.x7k2qm/state_keys.json`，不印出）：

    python -X utf8 -m x7k2qm.autoplay.marketinbox send --ns k8s-cgy --account <帳號> --job stall --set mode=claim --apply
    python -X utf8 -m x7k2qm.autoplay.marketinbox send --ns k8s-cgy --account <帳號> --job auction --set item=2400 --set price=1000000 --apply
    python -X utf8 -m x7k2qm.autoplay.marketinbox show --ns k8s-cgy --account <帳號>
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from . import jobqueue
from .statesync import ID_RE, Remote, StateError

VERSION = 1
CMD_SUFFIX, RES_SUFFIX = "-mcmd", "-mres"
INTERVAL = 60.0
MAX_CMDS = 50                    # 指令物件最多幾筆（多的不看）
MAX_RESULTS = 100                # 結果最多留幾筆（最舊的先丟）
MAX_AGE, MAX_AHEAD = 3600, 300   # 指令 ts 要在 1 小時內、不能比現在晚 5 分鐘以上
MAX_DATA = 20_000                # 一筆結果 data 超過就不放（只留 detail）
CMD_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FINAL = ("ok", "failed", "hold", "expired", "cancelled", "rejected", "lost")
JOB_PREFIX = "m-"


def job_id(cid: str) -> str:
    return JOB_PREFIX + cid


def where(q: jobqueue.Queue, jid: str) -> Optional[str]:
    """這件工作在哪：queue／running／done，找不到 None。"""
    for kind in ("done", "running", "queue"):
        if os.path.exists(q._path(kind, jid)):
            return kind
    return None


def validate(cmd, account: str, profile: str, now: float) -> List[str]:
    """一筆指令 → 佇列 argv；不合法丟 ValueError（訊息寫進結果）。"""
    from .party_api import JobRequestError, check_job, job_argv
    if not isinstance(cmd, dict):
        raise ValueError("每筆指令要是 JSON 物件")
    ts = cmd.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ValueError("缺 ts（送出時間 Unix 秒）")
    if ts < now - MAX_AGE:
        raise ValueError(f"指令是 {int(now - ts)} 秒前送的，超過 {MAX_AGE} 秒不做（要做請重送新的 id）")
    if ts > now + MAX_AHEAD:
        raise ValueError("ts 比現在晚太多（時鐘不對？）")
    body = {k: v for k, v in cmd.items() if k not in ("id", "ts")}
    try:
        argv = job_argv(body, profile)
        check_job(argv)
    except JobRequestError as e:
        raise ValueError(str(e))
    return argv


class Inbox:
    """`poll()` 讀一次指令、排新的、更新結果；`start()` 背景執行緒每 interval 秒一次。"""

    def __init__(self, cmd: Remote, res: Remote, account: str, queue: jobqueue.Queue, log: Callable[[str], None],
                 interval: float = INTERVAL, clock: Callable[[], float] = time.time):
        self.cmd, self.res, self.account, self.q, self.log = cmd, res, account, queue, log
        self.interval, self.clock = interval, clock
        self.results: Optional[Dict[str, dict]] = None      # None＝還沒讀到遠端結果（不處理指令）
        self.dirty = False
        self.bad = None                                     # 上次記過的壞指令內容（同一份不重複記）
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 遠端結果 ----
    def boot(self) -> bool:
        try:
            blob = self.res.get()
        except Exception as e:  # noqa: BLE001  讀不到就這輪不處理指令（不知道哪些做過）
            self.log(f"交易所指令：讀不到遠端結果（{type(e).__name__}: {e}），這輪不處理指令")
            return False
        results = {}
        if blob is not None:
            try:
                d = json.loads(blob.decode("utf-8"))
                results = d.get("results") if isinstance(d, dict) and isinstance(d.get("results"), dict) else None
            except (ValueError, UnicodeDecodeError):
                results = None
            if results is None:
                self.log("交易所指令：遠端結果壞掉，停用（刪掉那份結果物件再重開）")
                self._stop.set()
                return False
        for cid, r in results.items():           # 重開前寫了 accepted／queued 卻找不到工作 → lost（不重排）
            if r.get("state") not in FINAL:
                kind = where(self.q, job_id(cid))
                if kind is None:
                    r.update(state="lost", detail="重開時找不到這件工作（可能做到一半被中斷），沒有重做；要做請送新的 id")
                    self.dirty = True
        self.results = results
        self.log(f"交易所指令：已連上（結果 {len(results)} 筆）")
        return True

    def save(self) -> bool:
        items = sorted(self.results.items(), key=lambda kv: kv[1].get("ts") or 0)[-MAX_RESULTS:]
        self.results = dict(items)
        blob = json.dumps({"v": VERSION, "account": self.account, "updated": int(self.clock()), "results": self.results},
                          ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            self.res.put(blob)
        except Exception as e:  # noqa: BLE001  寫失敗：這輪不排新的，下輪再寫
            self.log(f"交易所指令：寫回結果失敗（{type(e).__name__}: {e}）")
            return False
        self.dirty = False
        return True

    # ---- 指令 ----
    def read_cmds(self) -> List[dict]:
        try:
            blob = self.cmd.get()
        except Exception as e:  # noqa: BLE001
            self.log(f"交易所指令：讀不到指令（{type(e).__name__}: {e}）")
            return []
        if blob is None:
            return []
        try:
            d = json.loads(blob.decode("utf-8"))
            if not isinstance(d, dict) or d.get("v") != VERSION or not isinstance(d.get("cmds"), list):
                raise ValueError(f"要是 {{\"v\":{VERSION},\"account\":…,\"cmds\":[…]}}")
            if d.get("account") != self.account:
                raise ValueError(f"account {d.get('account')!r} 不是這個帳號，整份不做")
        except (ValueError, UnicodeDecodeError) as e:
            if blob != self.bad:
                self.log(f"交易所指令：指令物件不合法（{e}），不做")
                self.bad = blob
            return []
        return d["cmds"][:MAX_CMDS]

    def poll(self) -> None:
        if self.results is None and not self.boot():
            return
        now = self.clock()
        new = []
        for cmd in self.read_cmds():
            cid = cmd.get("id") if isinstance(cmd, dict) else None
            if not isinstance(cid, str) or not CMD_ID.match(cid) or cid in self.results:
                continue
            try:
                argv = validate(cmd, self.account, self.q.profile, now)
            except ValueError as e:
                self.results[cid] = {"state": "rejected", "detail": str(e), "ts": int(now)}
                self.log(f"交易所指令：{cid} 不做：{e}")
                self.dirty = True
                continue
            self.results[cid] = {"state": "accepted", "jobId": job_id(cid), "detail": " ".join(argv), "ts": int(now)}
            new.append((cid, argv))
            self.dirty = True
        if new and not self.save():               # 先記下「收到了」才排；記不下來這輪不排（下輪 id 會再出現）
            for cid, _ in new:
                self.results.pop(cid, None)
            return
        for cid, argv in new:
            self.q.add(argv[0], argv, now=now, jid=job_id(cid))
            self.results[cid]["state"] = "queued"
            self.dirty = True
            self.log(f"交易所指令：{cid} 排進佇列：{' '.join(argv)}")
        self.refresh()
        if self.dirty:
            self.save()

    def refresh(self) -> None:
        for cid, r in self.results.items():
            if r.get("state") in FINAL or not r.get("jobId"):
                continue
            jid = r["jobId"]
            done = self.q.result(jid)
            if done is not None:
                data = done.get("data") or {}
                if len(json.dumps(data, ensure_ascii=False)) > MAX_DATA:
                    data = {"note": f"結果太大（>{MAX_DATA} 字），看帳號日誌"}
                r.update(state=done.get("status") or "failed", detail=done.get("detail") or "", data=data)
                self.log(f"交易所指令：{cid} {r['state']}：{r['detail']}")
                self.dirty = True
            elif where(self.q, jid) == "running" and r.get("state") != "running":
                r["state"] = "running"
                self.dirty = True
            elif where(self.q, jid) == "queue" and r.get("state") == "accepted":     # 重開後從狀態保存還原回來的
                r["state"] = "queued"
                self.dirty = True
            elif where(self.q, jid) is None:
                r.update(state="lost", detail="佇列裡找不到這件工作（可能被重開打斷），沒有重做")
                self.dirty = True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001  信箱出錯不影響練功
                self.log(f"交易所指令：例外 {type(e).__name__}: {e}")
            if self._stop.wait(self.interval):
                break

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="marketinbox", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)


def make(url: str, key: str, account: str, data: str, profile: str, log: Callable[[str], None]) -> Inbox:
    for suf in (CMD_SUFFIX, RES_SUFFIX):
        if not ID_RE.match(account + suf):
            raise StateError(f"帳號 {account!r} 加上 {suf} 不能當物件名稱（英數、_、-，最多 64 字）")
    return Inbox(Remote(url, key, account + CMD_SUFFIX), Remote(url, key, account + RES_SUFFIX), account,
                 jobqueue.Queue(os.path.join(data, "jobs"), profile), log)


# ---- 使用者這台：送指令、看結果 ----
def _keys(ns: str):
    path = os.path.join(os.path.expanduser("~"), ".x7k2qm", "state_keys.json")
    with open(path, encoding="utf-8") as fp:
        d = json.load(fp)
    if ns not in d:
        raise SystemExit(f"{path} 沒有 {ns}（有：{'、'.join(d)}）")
    e = d[ns]
    return e["url"].split("/<id>")[0], e["key"]


def _value(s: str):
    s = s.strip()
    return int(s) if re.fullmatch(r"-?\d+", s) and len(s) < 12 else s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="marketinbox", description="交易所遠端指令：送指令、看結果")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("send", "show"):
        sp = sub.add_parser(name)
        sp.add_argument("--ns", required=True, help="叢集（state_keys.json 的鍵，例 k8s-cgy）")
        sp.add_argument("--account", required=True, help="遊戲帳號（容器的 GAME_ACCOUNT）")
        if name == "send":
            sp.add_argument("--job", required=True, help="auction／petauction／stall")
            sp.add_argument("--set", action="append", default=[], metavar="欄位=值", help="工作欄位，可重複")
            sp.add_argument("--apply", action="store_true", help="真的做（不加只列）")
            sp.add_argument("--id", default=None, help="指令編號（預設用時間）")
    a = ap.parse_args(argv)
    url, key = _keys(a.ns)
    cmd_r, res_r = Remote(url, key, a.account + CMD_SUFFIX), Remote(url, key, a.account + RES_SUFFIX)
    if a.cmd == "show":
        blob = res_r.get()
        print(json.dumps(json.loads(blob) if blob else {}, ensure_ascii=False, indent=1))
        return 0
    now = time.time()
    c = {"id": a.id or time.strftime("%Y%m%d%H%M%S", time.localtime(now)), "ts": int(now), "job": a.job}
    for kv in a.set:
        k, _, v = kv.partition("=")
        c[k.strip()] = _value(v)
    if a.apply:
        c["apply"] = True
    blob = cmd_r.get()
    d = json.loads(blob) if blob else {"v": VERSION, "account": a.account, "cmds": []}
    res = res_r.get()
    seen = (json.loads(res).get("results") or {}) if res else {}
    # 已經有最後結果的舊指令拿掉，物件不會一直變大
    d["cmds"] = [x for x in d.get("cmds", []) if (seen.get(x.get("id")) or {}).get("state") not in FINAL][-(MAX_CMDS - 1):]
    if any(x.get("id") == c["id"] for x in d["cmds"]) or c["id"] in seen:
        raise SystemExit(f"指令編號 {c['id']} 已經用過，換一個")
    d["cmds"].append(c)
    cmd_r.put(json.dumps(d, ensure_ascii=False).encode("utf-8"))
    print(f"已送出 {c['id']}：{json.dumps(c, ensure_ascii=False)}（帳號約 {int(INTERVAL)} 秒內讀到，用 show 看結果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
