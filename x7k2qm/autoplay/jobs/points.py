"""points：設定自動加點（取代暫存資料夾的 set_points.py）。

    python -X utf8 -m x7k2qm.autoplay.jobs points --profile 2 --discover                          # 只列目前加點模式與屬性
    python -X utf8 -m x7k2qm.autoplay.jobs points --profile 2 --discover --apply                  # 自訂（預設）
    python -X utf8 -m x7k2qm.autoplay.jobs points --profile 2 --discover --apply --mode recommend

- `custom`：`Party.set_trans_points`（轉生後規範第 3 點）：目標值看 `Party.point_goal`——四轉以前速度 100
  （**隊長 150**）、體力 100；**五轉以後全隊 200／200**（使用者 2026-10-28），速度永遠排第一，
  `WaiGua_AutoRolePointSet{autoPointDiyList:[{type 4 速度, value}, {type 1 體力, value}, {type 2 腕力, 0}, {type 3 耐力, 0}]}`
  再送 `{autoSetPointType:1}` 切自訂。為什麼要有這個指令：上一轉後半改成推薦（2）之後，轉生**不會自己變回自訂**
  （2026-10-25 第二組三轉，LESSONS #78），補做時不用另寫腳本。
- `recommend`：`Party.switch_to_recommend` → `{autoSetPointType:2}`（系統推薦，剩餘點數自動分配）。
  練功程式在敏／血到標後自己會切，這裡是手動補。
- 每隻被拒就停（規範第 2 點），結束碼 1；設好後列 `Role_RoleInfo` 推回的 autoSetPointType（伺服器有推才有）。
- 不需要停練功（不動戰鬥、不走路）。
"""

from typing import Tuple

from . import JobError
from .pets import names_of

NAME = "points"
HELP = "設定自動加點（--mode custom|recommend；custom＝四轉前 100／隊長 150，五轉後 200／200）；不加 --apply 只列"
PAUSE = False
MODES = ("custom", "recommend")


def add_args(ap) -> None:
    ap.add_argument("--mode", default="custom", choices=MODES, help="custom（預設）或 recommend")
    ap.add_argument("--who", default=None, help="只做這些角色（名字逗號分隔；預設全隊）")


def check(args) -> None:
    if (getattr(args, "mode", "custom") or "custom") not in MODES:
        raise JobError(f"--mode 只能是 {'、'.join(MODES)}：{args.mode}")


def status(p, names) -> dict:
    with p.lock:
        return {n: {"type": p.point_type.get(n), "level": p.attrs.get(n, {}).get("level"),
                    "speed": p.attrs.get(n, {}).get("speed"), "vitality": p.attrs.get(n, {}).get("vitality"),
                    "left": p.attrs.get(n, {}).get("potentialPoint")} for n in names}


def run(p, args) -> Tuple[str, dict]:
    mode = getattr(args, "mode", "custom") or "custom"
    names = names_of(p, args)
    if not getattr(args, "apply", False):
        for n, s in status(p, names).items():
            p.log(f"{n}｜加點模式 {s['type']}（1 自訂、2 推薦）｜{s['level']} 級 速度 {s['speed']} 體力 {s['vitality']}"
                  f" 剩餘點數 {s['left']}")
        return f"只列（沒加 --apply，沒有送任何動作）；--apply 會設成 {mode}", {"done": []}
    done = p.set_trans_points(names) if mode == "custom" else p.switch_to_recommend(names)
    failed = [n for n in names if n not in done]
    if failed:
        raise JobError(f"加點設成 {mode} 被拒：{failed}（已設好 {done}）")
    p.nap(2)
    with p.lock:
        pushed = {n: p.attrs.get(n, {}).get("autoSetPointType") for n in names}
    return f"加點設成 {mode}：{done}｜伺服器推回的 autoSetPointType {pushed}", {"done": done, "pushed": pushed}
