"""revive：設定外掛戰鬥中的「復活」（技能復活倒下的寵物／人）。使用者 2026-10-31：「寵物死了復活」。

    python -X utf8 -m x7k2qm.autoplay.jobs revive --profile 2 --discover                                   # 只列每隻目前的復活設定
    python -X utf8 -m x7k2qm.autoplay.jobs add revive --profile 2 --discover --apply --skill 10215 --level 1 --target 2
    python -X utf8 -m x7k2qm.autoplay.jobs add revive --profile 2 --discover --apply --off                 # 關掉

協定 `WaiGua_FastBattleSettingReviveSkillChange{reviveRoleFlg, reviveRoleCmdArg（技能 ID 字串）, reviveRoleCmdArgEx（等級字串）,
reviveTargetRule（1 自己、2 自己的寵物、3 隨機敵人、4 隨機隊友）}` → `{isOK}`（`doc/autoplay/game/auto-battle.md` §3.1）。
技能要是身上裝備附的（例：月光的手鐲 2765 附「複活守精靈」10215 Lv1）；沒有那個技能就停、不送。
送成功就同步 `p.battle` 快取（伺服器會不會推新的 battleSetting 沒驗證過，同 jobs/quests 首次動作的做法）。
**我方無樣本**：第一次實機看 isOK 與戰鬥中有沒有真的復活。
"""

from typing import Dict, List, Tuple

from . import JobError

NAME = "revive"
HELP = "外掛戰鬥中的技能復活（--skill --level --target，--off 關）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

KEYS = ("reviveRoleFlg", "reviveRoleCmdArg", "reviveRoleCmdArgEx", "reviveTargetRule")
TARGETS = {1: "自己", 2: "自己的寵物", 3: "隨機敵人", 4: "隨機隊友"}


def add_args(ap) -> None:
    ap.add_argument("--skill", type=int, default=10215, help="復活技能 ID（預設 10215 複活守精靈，月光的手鐲附）")
    ap.add_argument("--level", type=int, default=1, help="技能等級（預設 1）")
    ap.add_argument("--target", type=int, default=2, help="目標：1 自己、2 自己的寵物（預設）、3 隨機敵人、4 隨機隊友")
    ap.add_argument("--off", action="store_true", help="關掉技能復活")
    ap.add_argument("--who", default=None, help="只設這些角色（逗號分隔）；預設全隊")


def names_of(p, args) -> List[str]:
    names = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if not who:
        return names
    pick = {x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()}
    bad = pick - set(names)
    if bad:
        raise JobError(f"--who 有不在這組的角色：{sorted(bad)}")
    return [n for n in names if n in pick]


def has_skill(p, name: str, skill: int, level: int) -> bool:
    """穿著的裝備有沒有附這個技能（同 ID、等級 ≥ level）。"""
    table = p.item_table()
    with p.lock:
        worn = [it for it in p.items[name].values() if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) != 0]
    for it in worn:
        for s in (table.get(it.get("itemId"), {}) or {}).get("skill") or []:
            if s.get("skillId") == skill and int(s.get("skillLevel") or 0) >= level:
                return True
    return False


def current(p, name: str) -> Dict[str, object]:
    with p.lock:
        b = dict(p.battle.get(name, {}) or {})
    return {k: b.get(k) for k in KEYS}


def run(p, args) -> Tuple[str, dict]:
    names = names_of(p, args)
    off = bool(getattr(args, "off", False))
    skill, level, target = int(args.skill), int(args.level), int(args.target)
    if target not in TARGETS:
        raise JobError(f"--target 要是 {sorted(TARGETS)}")
    want = ({"reviveRoleFlg": False} if off else
            {"reviveRoleFlg": True, "reviveRoleCmdArg": str(skill), "reviveRoleCmdArgEx": str(level),
             "reviveTargetRule": target})
    now = {n: current(p, n) for n in names}
    for n in names:
        p.log(f"復活：{n} 目前 {now[n]}")
    if not getattr(args, "apply", False):
        return "只列復活設定（沒加 --apply，沒有送任何動作）", {"now": now}
    if not off:
        miss = [n for n in names if not has_skill(p, n, skill, level)]
        if miss:
            raise JobError(f"{miss} 身上裝備沒有技能 {skill} Lv{level}，不設（伺服器會回 isOK 卻不套用）")
    done = []
    for n in names:
        r = p.clients[n].request("WaiGua_FastBattleSettingReviveSkillChange", dict(want)) or {}
        if not r.get("isOK"):
            raise JobError(f"{n} 復活設定沒確認：{r}（已設好的：{done}）")
        with p.lock:
            p.battle.setdefault(n, {}).update(want)
        done.append(n)
        p.log(f"復活：{n} → {'關掉' if off else f'技能 {skill} Lv{level}、目標{TARGETS[target]}'}")
        p.nap(0.5)
    return (f"復活設定{'關掉' if off else f'技能 {skill} Lv{level}（{TARGETS[target]}）'}：{done}", {"done": done, "want": want})
