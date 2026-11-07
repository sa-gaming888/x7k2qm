"""讓長跑的工作在崩潰或被強殺後自動重來（使用者 2026-11-01）。

    python -X utf8 -m x7k2qm.autoplay.keepalive --log captures/lingshi_keep.log \\
        --stop-file captures/lingshi_2.stop -- lingshi --profile 2 --discover --apply --element 水

常駐練功（使用者 11-05「以後停了自動重開」）加 `--party`，`--` 後面接 headless_party 的參數，停止檔用 `<log>.stop`；
Windows 用 Start-Process 開成獨立行程，Claude Code 記憶體不足時才不會連它一起殺（LESSONS #165）。

看這次執行寫進日誌的內容判斷怎麼結束（jobs 的 JobError、崩潰、Windows 強殺結束碼都可能是 1，分不出來）：
- 結束碼 0、有停止檔、工作自己回報「停止：…」→ 不重開。
- 明講被擠下線（「被擠下線」「被踢下線」「KickConnect」）→ 不重開（規範 2）。
- 斷線（「連線中斷」「連線被關閉」「連線已關閉」，使用者 11-07「接下來這些帳號斷線 預設重登10次」）→
  重登，最多 `--relogin` 次（預設 10）；間隔 5、10、20 分鐘，之後每次 30 分鐘。
  「連線被關閉」分不出是不是有人登入擠掉（LESSONS #136），照使用者這次的指示一律當斷線重登；
  要自己進遊戲前先放停止檔。
- 「停止：例外 …」（崩潰）或完全沒有停止紀錄（被強殺）→ 重開，間隔 5、10、20 分鐘，最多 3 次。
- 跑滿 STABLE_SECONDS 才算恢復，兩種次數都歸零；用完次數就放棄，推響鈴 TG。

這支程式自己被殺、電腦關機時不會重來（沒有誰看著它）。
"""

import argparse
import os
import re
import subprocess
import sys
import time
from typing import Callable, List, Optional

BACKOFF = (300.0, 600.0, 1200.0)      # 規範 2：5 分鐘起、每次加倍、最多 3 次
STABLE_SECONDS = 1800.0               # 跑滿這麼久才算恢復，重開次數歸零
RELOGIN_MAX = 10                      # 使用者 11-07「接下來這些帳號斷線 預設重登10次」
RELOGIN_CAP = 1800.0                  # 斷線重登的間隔加倍到 30 分鐘就不再加

PLANNED, CRASHED, KILLED, DONE, DISCONNECTED = "planned", "crashed", "killed", "done", "disconnected"
_STOP_LINE = re.compile(r"^\S+ \S+ (?:\[\w+\] )?停止：(.*)$", re.M)   # headless_party 的日誌沒有 [標籤]（容器用）
_KICKED = re.compile(r"被擠下線|被踢下線|KickConnect")
_DISCONNECT = re.compile(r"連線中斷|連線被關閉|連線已關閉")


def classify(rc: int, text: str) -> str:
    """text 是這次執行新寫進日誌的內容。被擠下線、斷線看這次所有的停止紀錄（petgoal 斷線時最後一行是
    「寵物目標第 1 輪沒練完（練功停止）」，斷線那行在前面）；其他看最後一行。"""
    if rc == 0:
        return DONE
    stops = _STOP_LINE.findall(text)
    if not stops:
        return KILLED
    if any(_KICKED.search(s) for s in stops):
        return PLANNED
    if any(_DISCONNECT.search(s) for s in stops):
        return DISCONNECTED
    return CRASHED if stops[-1].startswith("例外") else PLANNED


def relogin_wait(n: int) -> float:
    """第 n 次（從 0 起）斷線重登前等多久：5、10、20 分鐘，之後 30 分鐘。"""
    return min(BACKOFF[0] * 2 ** n, RELOGIN_CAP)


def supervise(run_once: Callable[[], tuple], stop_exists: Callable[[], bool], notify: Callable[[str], None],
              log: Callable[[str], None], sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, relogin: int = RELOGIN_MAX) -> str:
    """run_once() 回傳 (結束碼, 這次新增的日誌)。回傳最後結束的原因。
    崩潰／強殺最多重開 3 次；斷線另外算，最多重登 relogin 次（0＝斷線不重登）。"""
    fails = drops = 0
    while True:
        t0 = clock()
        rc, text = run_once()
        kind = classify(rc, text)
        log(f"工作結束：結束碼 {rc}，判定 {kind}，跑了 {clock() - t0:.0f} 秒")
        if kind in (DONE, PLANNED):
            return kind
        if stop_exists():
            log("有停止檔，不重開")
            return "stopped"
        if clock() - t0 >= STABLE_SECONDS:
            fails = drops = 0
        if kind == DISCONNECTED:
            if drops >= relogin:
                notify(f"⛔ 斷線重登 {drops} 次都沒撐住，放棄，請查日誌" if drops else "⛔ 斷線，沒設定重登，停")
                return "gave_up"
            wait = relogin_wait(drops)
            drops += 1
            notify(f"⚠️ 斷線，{wait / 60:.0f} 分鐘後重登（第 {drops}/{relogin} 次）")
        else:
            if fails >= len(BACKOFF):
                notify(f"⛔ 自動重開 {fails} 次都失敗，放棄（最後判定 {kind}），請查日誌")
                return "gave_up"
            wait = BACKOFF[fails]
            fails += 1
            notify(f"⚠️ 工作異常結束（{kind}），{wait / 60:.0f} 分鐘後自動重開（第 {fails}/{len(BACKOFF)} 次）")
        sleep(wait)
        if stop_exists():
            log("等待重開時看到停止檔，不重開")
            return "stopped"


def make_notify(log: Callable[[str], None], label: str) -> Callable[[str], None]:
    from ..notify.telegram import Telegram, TelegramConfigError
    try:
        tg: Optional[Telegram] = Telegram.from_env()
    except TelegramConfigError as e:
        log(f"不推 TG（{e}）")
        tg = None

    def notify(msg: str) -> None:
        log(msg)
        if tg is not None:
            err = tg.send(text=f"{label}｜{msg}", silent=False)
            if err:
                log(f"TG 推送失敗（{err}）")
    return notify


JOBS, PARTY = "x7k2qm.autoplay.jobs", "x7k2qm.autoplay.headless_party"


def run_child(job_argv: List[str], path: str, module: str = JOBS) -> tuple:
    """跑一次子程式，回傳 (結束碼, 這次新寫進日誌的內容)。headless_party 自己寫 --log（同一個檔），
    stdout 再寫進去會重複，所以只收 stderr（崩潰的 Traceback）。"""
    start = os.path.getsize(path) if os.path.exists(path) else 0
    with open(path, "ab") as fp:
        out = subprocess.DEVNULL if module == PARTY else fp
        rc = subprocess.call([sys.executable, "-X", "utf8", "-m", module, *job_argv],
                             stdout=out, stderr=fp if module == PARTY else subprocess.STDOUT)
    with open(path, "rb") as fp:
        fp.seek(start)
        return rc, fp.read().decode("utf-8", "replace").replace("\r\n", "\n")


def claim(path: str) -> bool:
    """單一實例：同帳號兩支程式會互相擠掉，排程或手動多跑一次時不能真的跑起來。"""
    from .owner import alive
    try:
        with open(path, encoding="utf-8") as fp:
            if alive(fp.read().strip()):
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(str(os.getpid()))
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.keepalive")
    ap.add_argument("--log", required=True, help="工作的日誌")
    ap.add_argument("--stop-file", required=True, help="工作的停止檔；有這個檔就不重開")
    ap.add_argument("--label", default="自動重開")
    ap.add_argument("--party", action="store_true",
                    help="-- 之後是 headless_party 的參數（常駐練功；使用者 11-05「以後停了自動重開」）")
    ap.add_argument("--relogin", type=int, default=RELOGIN_MAX,
                    help=f"斷線最多重登幾次（預設 {RELOGIN_MAX}，使用者 11-07；0＝斷線就停）")
    ap.add_argument("job", nargs=argparse.REMAINDER, help="-- 之後是 jobs 的參數")
    args = ap.parse_args(argv)
    job = args.job[1:] if args.job[:1] == ["--"] else args.job
    if not job:
        ap.error("要給工作參數，例：-- lingshi --profile 2 ...")

    def log(msg: str) -> None:                  # 排程用 pythonw 跑沒有 stdout，一律寫進工作日誌
        with open(args.log, "a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%m-%d %H:%M:%S')} [keepalive] {msg}\n")
    if os.path.exists(args.stop_file):          # 使用者停掉的工作，開機也不自己跑回來
        log(f"停止檔 {args.stop_file} 還在，不啟動（要跑先刪掉）")
        return 1
    pid_path = args.stop_file + ".pid"
    if not claim(pid_path):
        log(f"已經有 keepalive 在跑（{pid_path}），不重複啟動")
        return 1
    try:
        module = PARTY if args.party else JOBS
        why = supervise(lambda: run_child(job, args.log, module), lambda: os.path.exists(args.stop_file),
                        make_notify(log, args.label), log, relogin=max(0, args.relogin))
    finally:
        os.remove(pid_path)
    log(f"結束：{why}")
    return 0 if why in (DONE, PLANNED, "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
