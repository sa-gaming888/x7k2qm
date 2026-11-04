"""帳號擁有者鎖：同一組帳號（profile）同時只能有一支程式登入（參考 參考專案 `headless.owner`）。

同一角色只能在一處登入：常駐練功（headless_party）、固定指令（jobs）、無畫面解任務（quest/headless）
各自登入就會互相擠下線（System_KickConnect）、登入後轉移被拒。所以**登入前先 acquire**：

    captures/owner/<profile>.json   {pid, kind, profile, started, api_port, jobs_dir}

- 檔案已存在、裡面的 pid 還活著 → 丟 `OwnerBusy`（說誰在用），什麼都不登入。
- pid 已經死了（行程被砍、當機沒 release）→ 視為過期，記日誌後接手。
- 正常結束 `release()`（只刪自己 pid 的那份，不會刪到別人的）。
- 建檔用「先寫暫存檔再 os.link」：目標已存在就失敗，兩支同時搶只會有一支成功，也不會讀到寫一半的檔。

會寫檔的函式一律要傳 `base`（目錄）；預設目錄只由各程式的 `main()` 用 `default_dir()` 決定
（環境變數 `BOT_OWNER_DIR`，沒有才用 `captures/owner`；測試的 conftest 會把它指到暫存目錄，規範 13）。
"""

import json
import os
import sys
import time
from typing import Callable, Optional

KIND_PARTY = "headless_party"      # 常駐練功（會處理工作佇列）
KIND_JOBS = "jobs"                 # 固定指令單獨執行
KIND_QUEST = "quest"               # 無畫面解任務（quest/headless.py）


class OwnerBusy(RuntimeError):
    """這組帳號已經有別的程式在用（理由給人看）。"""

    def __init__(self, msg: str, record: Optional[dict] = None):
        super().__init__(msg)
        self.record = record or {}


def default_dir() -> str:
    return os.environ.get("BOT_OWNER_DIR") or os.path.join("captures", "owner")


def path(base: str, profile: str) -> str:
    return os.path.join(base, f"{profile}.json")


def alive(pid: int) -> bool:
    """行程還在不在。Windows 不能用 os.kill(pid, 0)（Python 在 Windows 會直接 TerminateProcess 砍掉它）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5            # 權限不足＝行程在（別的使用者的），其他（87 參數錯）＝不在
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                       # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read(base: str, profile: str) -> Optional[dict]:
    try:
        with open(path(base, profile), encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def describe(d: Optional[dict]) -> str:
    if not d:
        return "沒有人在用"
    t = d.get("started")
    since = time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if isinstance(t, (int, float)) else "?"
    api = f"、查詢 API 埠 {d['api_port']}" if d.get("api_port") is not None else ""
    return f"{d.get('kind', '?')}（pid {d.get('pid')}，{since} 開始{api}）"


def live(base: str, profile: str, is_alive: Optional[Callable[[int], bool]] = None) -> Optional[dict]:
    """目前活著的擁有者（沒有或已經死掉回 None；不刪檔）。"""
    is_alive = is_alive or alive
    d = read(base, profile)
    if d and is_alive(d.get("pid")):
        return d
    return None


def _link_new(target: str, data: dict) -> bool:
    """寫暫存檔再 link 到 target；target 已存在回 False。"""
    tmp = _tmp_write(target, data)
    try:
        os.link(tmp, target)
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def acquire(base: str, profile: str, kind: str, api_port: Optional[int] = None, jobs_dir: Optional[str] = None,
            log: Callable[[str], None] = lambda m: None, pid: Optional[int] = None,
            is_alive: Optional[Callable[[int], bool]] = None, now: Optional[float] = None) -> dict:
    """登入前呼叫。成功回寫進去的紀錄；有活著的別人在用丟 OwnerBusy。"""
    is_alive = is_alive or alive
    pid = os.getpid() if pid is None else pid
    rec = {"pid": pid, "kind": kind, "profile": str(profile), "started": time.time() if now is None else now,
           "api_port": api_port, "jobs_dir": os.path.abspath(jobs_dir) if jobs_dir else None}
    os.makedirs(base, exist_ok=True)
    target = path(base, profile)
    for _ in range(2):                                  # 第二次是清掉過期的之後再搶一次；再搶不到就是被別人搶走
        if _link_new(target, rec):
            return rec
        old = read(base, profile)
        if old and old.get("pid") == pid:               # 同一支程式重複 acquire：更新紀錄
            os.replace(_tmp_write(target, rec), target)
            return rec
        if old and is_alive(old.get("pid")):
            raise OwnerBusy(f"第 {profile} 組帳號正在被 {describe(old)} 使用，不登入", old)
        if old is None and os.path.exists(target):
            # 讀不出來：可能別人正好在寫（link 是一次到位，不會半份）或檔案壞了 → 不猜，交給人看
            raise OwnerBusy(f"第 {profile} 組帳號的擁有者檔 {target} 讀不出來，請人工確認後刪除", {})
        log(f"帳號擁有者：第 {profile} 組原本的 {describe(old)} 已經不在，視為過期，接手")
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
    raise OwnerBusy(f"第 {profile} 組帳號的擁有者檔剛被別的程式搶走，不登入", read(base, profile))


def _tmp_write(target: str, data: dict) -> str:
    tmp = f"{target}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
    return tmp


def release(base: str, profile: str, pid: Optional[int] = None) -> bool:
    """只刪自己 pid 的擁有者檔；回傳有沒有刪。"""
    pid = os.getpid() if pid is None else pid
    d = read(base, profile)
    if not d or d.get("pid") != pid:
        return False
    try:
        os.remove(path(base, profile))
        return True
    except FileNotFoundError:
        return False
