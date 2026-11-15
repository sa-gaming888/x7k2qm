"""容器狀態保存：Pod 刪掉重建（換映像、搬節點）後接回劇本進度與還沒做的工作。

使用者 11-15「我這個是要紀錄狀態讓他重開可以接回來用」「可以做 r2 用中性名 未來可能多個共用」。

- 遠端：一個帳號一個物件，固定位址整份覆蓋（`GET`／`PUT <STATE_URL>/<id>`，標頭 `X-State-Key`）；
  實際放在物件儲存（R2），由前面的 Worker 驗密鑰、限定每個 ns 只能碰自己的物件。
- 存什麼：`scenario/*.json`（劇本進度）、`jobs/*/queue/*.json`（排了還沒做的工作）。不存 running／done：
  做到一半被砍的工作在新 Pod 裡看起來「不見了」，劇本 60 秒後自動重排（scenario.AUTO_RETRY_LOST）。
- 什麼時候讀：容器啟動、練功程式登入前一次。本機已經有劇本進度（同一個 Pod 裡重開）就不蓋。
- 什麼時候寫：每 INTERVAL 秒算一次內容雜湊，跟上次成功送出的不一樣才送；結束時再送一次。
- 出錯：讀不到（網路、5xx）→ 這次不還原、**也不上傳**（免得空的蓋掉遠端好的那份）；
  內容壞掉 → 丟 StateError，容器停下不登入（規範 2）。上傳失敗只記日誌，下一輪內容有變再送。
"""

import fnmatch
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

VERSION = 1
PATTERNS = ("scenario/*.json", "jobs/*/queue/*.json")
MAX_BYTES = 900_000                  # 遠端一個物件上限 1 MB
INTERVAL = 60.0
TIMEOUT = 15.0
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
USER_AGENT = "statesync/1"           # 不帶的話 Cloudflare 擋 Python-urllib（403 error code 1010，11-15 實測）


class StateError(ValueError):
    """遠端存的狀態不能用（版本不對、格式壞掉、路徑不在允許範圍）。"""


def allowed(rel: str) -> bool:
    if rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        return False
    return any(fnmatch.fnmatchcase(rel, p) and rel.count("/") == p.count("/") for p in PATTERNS)


def collect(data: str) -> Dict[str, str]:
    """資料目錄裡要保存的檔 → {相對路徑（/ 分隔）: 內容}。"""
    out: Dict[str, str] = {}
    for root, _, files in os.walk(data):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, data).replace(os.sep, "/")
            if not allowed(rel):
                continue
            try:
                with open(full, encoding="utf-8") as fp:
                    out[rel] = fp.read()
            except OSError:
                continue                      # 剛好被搬走（工作被取出）：下一輪再算
    return out


def pack(files: Dict[str, str]) -> bytes:
    return json.dumps({"v": VERSION, "files": dict(sorted(files.items()))}, ensure_ascii=False,
                      sort_keys=True).encode("utf-8")


def unpack(blob: bytes) -> Dict[str, str]:
    try:
        d = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise StateError(f"不是 JSON：{e}") from None
    if not isinstance(d, dict) or d.get("v") != VERSION or not isinstance(d.get("files"), dict):
        raise StateError(f"版本或格式不對（v={d.get('v') if isinstance(d, dict) else '?'}）")
    files = d["files"]
    for rel, text in files.items():
        if not isinstance(rel, str) or not isinstance(text, str) or not allowed(rel):
            raise StateError(f"不允許的檔案：{rel!r}")
    return files


def has_local(data: str) -> bool:
    return any(rel.startswith("scenario/") for rel in collect(data))


def restore(data: str, files: Dict[str, str]) -> int:
    for rel, text in files.items():
        full = os.path.join(data, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".restore"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, full)
    return len(files)


class Remote:
    """`<url>/<id>` 讀寫（GET 沒有回 None；其他錯誤丟例外）。"""

    def __init__(self, url: str, key: str, ident: str, opener=urllib.request.urlopen):
        if not ID_RE.match(ident or ""):
            raise StateError(f"物件名稱只能是英數、_、-（1～64 字）：{ident!r}")
        self.url, self.key, self.opener = url.rstrip("/") + "/" + ident, key, opener

    def get(self) -> Optional[bytes]:
        req = urllib.request.Request(self.url, headers={"X-State-Key": self.key, "User-Agent": USER_AGENT}, method="GET")
        try:
            with self.opener(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def put(self, blob: bytes) -> None:
        req = urllib.request.Request(self.url, data=blob, method="PUT",
                                     headers={"X-State-Key": self.key, "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        with self.opener(req, timeout=TIMEOUT) as r:
            r.read()


class Syncer:
    """啟動時還原、之後有變才上傳。`start()` 開背景執行緒，`stop()` 停下並最後送一次。"""

    def __init__(self, data: str, remote: Remote, log: Callable[[str], None], interval: float = INTERVAL):
        self.data, self.remote, self.log, self.interval = data, remote, log, interval
        self.enabled = False                  # 還原那步沒確認遠端狀態前不上傳
        self.last: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def boot(self) -> None:
        """登入前呼叫。遠端內容壞掉丟 StateError（容器停）；讀不到就這次不還原也不上傳。"""
        if has_local(self.data):
            self.log("狀態保存：本機已經有劇本進度（同一個 Pod 重開），不從遠端還原")
            self.enabled = True
            return
        try:
            blob = self.remote.get()
        except Exception as e:  # noqa: BLE001  網路、5xx、驗證失敗：不知道遠端有什麼，這次不碰
            self.log(f"狀態保存：讀不到遠端（{type(e).__name__}: {e}），這次不還原、也不上傳")
            return
        self.enabled = True
        if blob is None:
            self.log("狀態保存：遠端還沒有存檔，從頭開始")
            return
        files = unpack(blob)
        n = restore(self.data, files)
        self.last = hashlib.sha256(pack(collect(self.data))).hexdigest()
        self.log(f"狀態保存：從遠端還原 {n} 個檔（劇本進度、還沒做的工作）")

    def sync(self) -> bool:
        """有變才送；回 True＝這次送了。"""
        if not self.enabled:
            return False
        files = collect(self.data)
        blob = pack(files)
        h = hashlib.sha256(blob).hexdigest()
        if h == self.last:
            return False
        if len(blob) > MAX_BYTES:
            self.log(f"狀態保存：內容 {len(blob)} bytes 超過 {MAX_BYTES}，不上傳")
            return False
        try:
            self.remote.put(blob)
        except Exception as e:  # noqa: BLE001  上傳失敗只記日誌；下一輪有變再送（不另外重送）
            self.log(f"狀態保存：上傳失敗（{type(e).__name__}: {e}）")
            return False
        self.last = h
        self.log(f"狀態保存：已上傳 {len(blob)} bytes（{len(files)} 個檔）")   # 有變才送，一天幾次（11-15 k8s 要查得到）
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.sync()

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="statesync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT + 5)
        if self.sync():
            self.log("狀態保存：結束前已上傳最後狀態")
