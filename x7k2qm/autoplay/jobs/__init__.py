"""固定指令（工作）：一個工作一個模組，取代每次寫一次性腳本。

    python -X utf8 -m x7k2qm.autoplay.jobs <工作> --profile N [--discover] [--apply] [工作自己的參數]

每個工作模組定義（參考 參考專案 `tools/headless/jobs/`）：
- `NAME`：指令名稱；`HELP`：一行說明。
- `PAUSE`：要不要先停練功（關原地遇敵、等這場打完）；可以是 bool 或 `callable(args) -> bool`
  （例：`clean` 只列清單時不停、不送任何封包）。
- `add_args(ap)`：（可選）加這個工作自己的命令列參數。
- `check(args)`：（可選）登入前檢查參數，不合法丟 `JobError`（什麼都還沒送）。
- `SOLO`：（可選）True = 單人活動，沒有常駐、自己登入時只登帳號第一個人物（`__main__.default_party`）；
  工作參數有 `--role N`／`--roles 3,4,5` 時改登那一隻／那幾隻（同一個 pcId）。
- `run(party, args) -> (說明, data)`：在已登入的 `headless_party.Party` 上做事；每步等封包確認，
  失敗丟 `JobError`（規範第 2 點：出錯就停，不重試、不跳過）。

`run_job()` 只負責「檢查參數 → 登入 → 需要的話停練功 → run」。常駐練功（headless_party）有工作佇列
（`x7k2qm/autoplay/jobqueue.py`）：主迴圈拿自己的 Party 呼叫 `run_job(mod, args, party, login=False)`，
不另外登入（見 doc/autoplay/flows/jobs.md「工作佇列」）。
失敗丟 `JobError`（常駐照常回練功）；送過不可逆／花費封包後對不上丟 `JobHold`（常駐停在原地等人）。
"""

from typing import Callable, Dict, Tuple


class JobError(RuntimeError):
    """工作失敗（理由給人看）。常駐練功收到它：結果寫 failed、照常回練功。"""


class JobHold(JobError):
    """送過不可逆／花費封包後對不上（例：花了石幣卻沒拿到東西）。

    常駐練功收到它：結果寫 hold、**停在原地不回練功**，等人處理（`jobs add resume` 或重啟）。
    單獨執行時跟 JobError 一樣：停止、結束碼非 0。"""


def pauses(mod, args) -> bool:
    p = getattr(mod, "PAUSE", True)
    return bool(p(args)) if callable(p) else bool(p)


def run_job(mod, args, party, login: bool = True) -> Tuple[str, dict]:
    """執行一個工作。失敗丟 JobError；登出（party.close）由呼叫端負責。"""
    chk = getattr(mod, "check", None)
    if chk:
        chk(args)
    if login:
        party.login_all()
        party.nap(3)
    if pauses(mod, args) and not party.pause():
        raise JobError("停不了原地遇敵（Role_UnLoopBattle 沒確認）")
    return mod.run(party, args)


def registry() -> Dict[str, object]:
    from . import catch, clean, quests
    from . import resume
    from . import train
    from . import petgoal
    from . import buy, pets, points, rebirth
    from . import after_trans
    from . import fruit
    from . import mall
    from . import auction
    from . import marketbuy
    from . import signin
    from . import trade_give, trade_take
    from . import spirit
    return {m.NAME: m for m in (after_trans, auction, buy, catch, clean, fruit, mall, marketbuy, petgoal, pets, points, quests, rebirth, resume, signin, spirit, trade_give, trade_take, train)}


Log = Callable[[str], None]
