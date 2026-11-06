"""quests：依序無畫面解任務，每個任務完成後照處置規則表清背包（取代一次性的 quest_seq.py）。

    python -X utf8 -m x7k2qm.autoplay.jobs quests --profile 2 --discover                    # 只列每個任務的完成狀態
    python -X utf8 -m x7k2qm.autoplay.jobs quests --profile 2 --discover --apply            # 照預設順序跑
    ... --apply --groups 2012,2028,2024,2010 --start 2028 --free 3

- 一次登入、依序跑：組隊 → 關遇敵 → 前置 hook → `quest.headless` 執行器 → 後置 hook → clean（只做道具：
  刪除／存倉／穿上；寵物放生、存倉要另外跑 `clean`）。
- **任何一步失敗就停，後面的任務不跑**（規範第 2 點）；失敗時後置 hook 也不做，日誌會寫要手動補什麼。
- 已經全隊完成的任務，執行器自己會略過（日誌「全隊都已完成」）。
- 每個任務的前／後置動作寫在 `HOOKS`：四寶玉 2010 打地守護獸前關隊長外掛首次動作（大地的精靈打地沒用），
  打完開回（LESSONS #68）。
"""

from typing import Callable, Dict, List, Optional, Tuple

from . import JobError, JobHold
from . import clean

RECORD_DIR = "captures/quests/battles"          # 戰鬥型步驟錄檔（使用者 2026-10-31「你錄製」）
NAME = "quests"
HELP = "依序無畫面解任務（--groups、--start、--free）；每個任務後清背包；不加 --apply 只列狀態"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

# 預設順序（2026-10-23 第二組 1 轉後重解 1.82 的實際順序；runbook §6）。要寵物的 3003／2016／2019 不在預設裡。
ORDER = [2999, 2017, 2015, 2023, 2027, 2018, 2021, 2025, 2014, 2013, 2020, 2011, 2012, 2028, 2024, 2010]
FREE = {2999: 15}               # 開始前每隻背包要空幾格（成人儀式 15 格全空），其他預設 DEFAULT_FREE
# 前置任務：設定表的 condition 沒寫，但伺服器會擋（提示 40000000069「隊中[X]條件不滿足，請檢查郵件，背包，寵物欄」）。
# 2026-11-03 第七、八組（11-02 新開、直接練上 131）新手任務 1000 初入沒做，成人儀式接不起來，劇本停在 1.82；
# 第六組做過 1000 就能接。有人沒做就先自動補做（兩步都是跟薩村村長對話，不用寵物、不打怪）。
PREREQ = {2999: [1000]}


def with_prereq(p, groups: List[int]) -> List[int]:
    """在需要前置任務的群組前面補上「還有人沒做」的前置群組（已經全隊做完就不補，也不重複補）。"""
    out: List[int] = []
    for g in groups:
        for pre in PREREQ.get(g, []):
            if pre in out or pre in groups:
                continue
            # 使用者 11-05「0 轉解完美人任務時需要先完成新生任務，只有 0 轉需要」：轉過的不補（轉數不知道當 0 轉，照舊補）
            todo = [n for _, n in p.party if pre not in (p.finished.get(n) or [])
                    and not (p.trans.get(n) or 0)]
            if todo:
                p.log(f"任務 {g} 的前置任務 {pre} 有人沒做（{'、'.join(todo)}），先補做 {pre}")
                out.append(pre)
        out.append(g)
    return out
DEFAULT_FREE = 3
FIRST_CMD_OFF = {"firstRoleCmdFlg": False}
# 首次動作要開回什麼**不寫死**：照隊長身上那枚精靈金幣（Party.ensure_first_cmd）。
# 原本寫死 "10185"（大地）是第二組地隊的值，水隊跑完 2010 會被設成放不出來的大地精靈。
FIRST_CMD_KEYS = ("firstRoleCmdFlg", "firstRoleCmd", "firstRoleCmdArg", "firstRoleCmdArgEx",
                  "firstRoleCmdArgTargetRule")


def add_args(ap) -> None:
    ap.add_argument("--groups", default=None, help="任務群組，逗號分隔（預設 ORDER）")
    ap.add_argument("--leader", default=None,
                    help="指定誰帶隊（預設隊伍第一位）。執行器從**隊長**目前進行到的步驟開跑，"
                         "所以某個隊員落後時（別人在第 2 步、他還在第 1 步）要指定他當隊長才補得到")
    ap.add_argument("--start", type=int, default=None, help="從清單裡哪個群組開始（之前的不跑）")
    ap.add_argument("--free", type=int, default=None, help="每個任務開始前每隻背包至少空幾格（預設 2999 要 15、其他 3）")
    ap.add_argument("--stop-after", type=int, default=None,
                    help="做完這個步驟（taskId，例 201900001）就停；只套用在它所屬的群組，停了後面的群組也不跑")
    ap.add_argument("--give-up", default=None,
                    help="放棄這些任務群組（逗號分隔），讓全隊回到同一個狀態重接；要加 --apply。"
                         "有人中途陣亡後任務狀態會分岔，再跑就卡「隊員任務狀態不一致」")
    # 戰鬥型步驟（type 4「通過守護者」「挑戰機暴」…）打很久：2026-10-25 第三組劇本跑 1.82，
    # 2012 的 201200007 等 180 秒沒翻成 2 就判失敗停手，和轉生 10 樓／機暴同一個病（LESSONS）。
    ap.add_argument("--battle-timeout", type=float, default=None,
                    help="戰鬥型步驟等任務狀態翻面的秒數（預設 180；守護者、機暴這類建議 420）")
    ap.add_argument("--retry", type=int, default=0,
                    help="有人被打死時回薩村醫院補血、直接重打同一關幾次（0～20，不回練功；預設 0＝照舊停）")
    ap.add_argument("--water", type=int, default=0,
                    help="每次開打（含 --retry 重打）前每隻阿布的水補到幾瓶（商城石幣 1 萬／瓶，0～13；預設 0＝不補）")
    ap.add_argument("--record", default=RECORD_DIR,
                    help=f"戰鬥型步驟每次互動錄一個 jsonl（隊長收到的封包），放這個目錄（預設 {RECORD_DIR}）")
    ap.add_argument("--no-record", dest="record", action="store_const", const=None, help="不錄")
    ap.add_argument("--no-preclean", action="store_true",
                    help="每個任務開始前不先整理背包（預設會照規則表五隻同時整理一次，只動道具）")


def groups_of(args) -> List[int]:
    if getattr(args, "groups", None):
        try:
            gs = [int(x) for x in str(args.groups).replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise JobError(f"--groups 要是逗號分隔的數字：{args.groups}")
    else:
        gs = list(ORDER)
    start = getattr(args, "start", None)
    if start is not None:
        if start not in gs:
            raise JobError(f"--start {start} 不在任務清單 {gs} 裡")
        gs = gs[gs.index(start):]
    if not gs:
        raise JobError("任務清單是空的")
    return gs


def stop_group(args):
    """--stop-after 的 taskId 屬於哪個群組（taskId＝群組×100000＋步驟）；沒給回 None。"""
    sa = getattr(args, "stop_after", None)
    return None if sa is None else int(sa) // 100000


WATER = 2400               # 阿布的水
WATER_MALL = 44            # 商城石幣分頁：阿布的水 10,000 石幣


def refill_water(p, want: int) -> None:
    """每次開打（含 --retry 重打）前每隻背包的阿布的水補到 want 瓶（商城石幣，jobs/mall 同一套核對）。
    使用者 2026-10-31：BOSS 長期戰一場要喝約 60 瓶，重打不補水第二次就沒 MP（LESSONS #122）。want 0＝不補。"""
    if want <= 0:
        return
    from types import SimpleNamespace
    from . import mall
    for _, n in p.party:
        have = sum(it.get("overlay", 1) for it in p.bag(n) if it.get("itemId") == WATER)
        need = want - have
        while need > 0:
            k = min(need, mall.MAX_COUNT)
            mall.run(p, SimpleNamespace(apply=True, id=WATER_MALL, count=k, who=n))
            need -= k
        p.log(f"補水：{n} 阿布的水 {want} 瓶")


def check(args) -> None:
    gs = groups_of(args)
    g = stop_group(args)
    if g is not None and g not in gs:
        raise JobError(f"--stop-after {args.stop_after} 屬於群組 {g}，不在任務清單 {gs} 裡")
    if not 0 <= int(getattr(args, "retry", 0) or 0) <= 20:
        raise JobError("--retry 要在 0～20")
    if not 0 <= int(getattr(args, "water", 0) or 0) <= 13:
        raise JobError("--water 要在 0～13（背包 15 格）")


# ---- 前／後置 hook：fn(party) → None，失敗丟 JobError ----
def _first_cmd(p, setting: dict, what: str) -> None:
    lead = p.party[0][1]
    r = p.clients[lead].request("WaiGua_FastBattleSettingChange", dict(setting)) or {}
    if not r.get("isOK"):
        raise JobError(f"{lead} {what}隊長外掛首次動作沒確認：{r}")
    # 送成功就同步快取：不更新的話，關掉之後 `p.battle` 仍停在「開著而且正確」，
    # 後置 ensure_first_cmd 比對快取會以為不用送 → 首次動作一直關著
    # （primaljason-home 2026-10-25 審出；伺服器會不會自己推新的 battleSetting 沒驗證過，不能賭）。
    with p.lock:
        p.battle.setdefault(lead, {}).update(setting)
    p.log(f"{lead} {what}隊長外掛首次動作：{setting}")


def first_cmd_off(p) -> None:
    """關掉前先記住原本的設定，沒戴精靈金幣的隊長才有東西可以還原。"""
    lead = p.party[0][1]
    with p.lock:
        p.saved_first_cmd = {k: p.battle.get(lead, {}).get(k) for k in FIRST_CMD_KEYS}
    _first_cmd(p, FIRST_CMD_OFF, "關掉")


def first_cmd_on(p) -> None:
    """照隊長身上那枚精靈金幣開回（地隊→大地、水隊→水）；沒戴金幣就還原關掉前記住的設定。"""
    lead = p.party[0][1]
    if p.spirit_coin(lead) is not None:
        if not p.ensure_first_cmd(lead):
            raise JobError(f"{lead} 首次動作沒照金幣設回")
        return
    saved = getattr(p, "saved_first_cmd", None) or {}
    if not saved.get("firstRoleCmdFlg"):
        p.log(f"{lead} 沒戴精靈金幣、原本也沒開首次動作 → 不設回")
        return
    _first_cmd(p, {k: v for k, v in saved.items() if v is not None}, "照原本設定開回")


SPIRIT_ARMOR = 19646                 # 靈力鎧：天空之島第 12 步交四色寶石給的鎧甲（防+48 敏−12、附恩惠 Lv4）


def wear_spirit_armor(p) -> None:
    """天空之島 2034 第 13 步「打敗黑暗精靈王」前，背包有靈力鎧的人都穿上（不脫回）。
    2026-10-31 第二組：靈力鎧只放背包、穿新手鎧甲去打，連輸 8 場（抗性練到 70 也一樣）；
    參考專案 攻略寫「製作靈力鎧，並擊敗黑暗精靈王」（LESSONS #118）。穿不上就停，不硬打。"""
    for _, n in p.party:
        with p.lock:
            worn = any(it.get("itemId") == SPIRIT_ARMOR and it.get("itemStorge", 0) != 0
                       for it in p.items[n].values() if it.get("overlay", 1) > 0)
        if worn:
            continue
        it = next((x for x in p.bag(n) if x.get("itemId") == SPIRIT_ARMOR), None)
        if it is None:
            continue                                  # 還沒拿到（前面步驟還沒做）或已經交掉：不管
        r = p.clients[n].request("ItemHandle_itemEquipOn", {"itemIndex": it["itemIndex"]}) or {}
        if not r.get("isOK"):
            raise JobError(f"{n} 穿不上靈力鎧：{r}")
        p.log(f"{n} 穿上靈力鎧（打黑暗精靈王用）")


HOOKS: Dict[int, Tuple[List[Callable], List[Callable]]] = {
    2010: ([first_cmd_off], [first_cmd_on]),         # 四寶玉：守護獸有地屬性，隊長首次動作大地的精靈沒用（LESSONS #68）
    2034: ([wear_spirit_armor], []),                 # 天空之島：打黑暗精靈王要穿靈力鎧（LESSONS #118）
}


def _grid_loader():
    from ..pathing import load_grid
    cache: Dict[int, object] = {}

    def grid(m):
        if m not in cache:
            cache[m] = load_grid(m)
        return cache[m]
    return grid


def build_quest(p, group: int, log, grid_fn, need_free: int, stop_after=None, battle_timeout=None, only=None):
    """另外包一層，測試可以換成假的執行器。"""
    from ..quest.headless import build_quest as real
    kw = {} if battle_timeout is None else {"battle_timeout": battle_timeout}
    if only is not None:
        kw["only"] = only
    rec = getattr(p, "quest_record_dir", None)       # run() 照 --record 設定；None＝不錄
    if rec:
        kw["record_dir"] = rec
    return real(p, group, log, grid_fn, need_free=need_free, stop_after=stop_after,
                before_step=lambda s: top_up(p, group, s), **kw)


QUEST_HEAL_BELOW = 70            # 任務每一步開始前，隊伍最低血量低於這個 % 就先回村補血


def top_up(p, group: int, s) -> None:
    """任務步驟開始前血量太低就先回村補血（之後執行器照目前地圖重新跳回去）。
    11-05 2010 四寶玉連打三場不補血，第 4 步風的寶玉（風剋地）開打時有人只剩 148/650，四回合死三隻，重打三次都一樣。
    補血失敗照規則停（不硬打）。"""
    from ..quest.headless import QuestStop
    hp = p.hp_percent()
    if hp is None or hp >= QUEST_HEAL_BELOW:
        return
    p.log(f"任務 {group}：步驟 {s.task_id} 開始前隊伍最低血量 {hp}%，先回村補血")
    if not p.heal_at_samugil():
        raise QuestStop(f"步驟 {s.task_id} 開始前回村補血失敗")


PET_MAX = 5


def pet_slots_needed(steps) -> int:
    """這個群組跑完的途中，寵物欄最多會多出幾隻（任務給的寵物 − 交出去的寵物，取過程中的最大值）。
    交寵物的算法跟執行器 check_pets 一致：該步條件裡的寵物視為交出、空出格子。"""
    cur = need = 0
    for s in steps:
        cur -= len(s.pets)
        cur += s.reward_pets
        need = max(need, cur)
    return need


def preflight_pets(p, group: int, steps=None) -> None:
    """使用者 2026-10-25「我就是不希望人物跑到一半空間不夠、寵物不夠」：任務**開始前**先算寵物欄要空幾格，
    不夠就把可以收的寵物存個人寵物倉（不是戰寵／騎寵、不是這個任務條件要的那種，等級低的先存）；
    存完還是不夠就在**開始前**停（JobError），不要跑到給獎勵那一步才停。"""
    from ..quest.spec import load_steps
    steps = steps if steps is not None else load_steps(group)
    need = pet_slots_needed(steps)
    if need <= 0:
        return
    wanted = {pid for s in steps for pid, _ in s.pets}
    short = {}
    for _, name in p.party:
        fight, ride = p.locks(name)
        with p.lock:
            pets = {k: dict(v) for k, v in p.petinfo.get(name, {}).items() if (v.get("petId") or 0) > 0}
        free = PET_MAX - len(pets)
        movable = sorted(((k, v) for k, v in pets.items()
                          if v.get("status") not in (2, 3) and v.get("uniqueIndex") not in (fight, ride)
                          and v.get("petId") not in wanted), key=lambda kv: kv[1].get("level") or 0)
        while free < need and movable:
            k, v = movable.pop(0)
            clean.do_store_pet(p, name, k, v["petId"])
            free += 1
        if free < need:
            short[name] = free
    if short:
        raise JobError(f"{group}：任務途中寵物欄要空 {need} 格，存倉後還是不夠（目前空格 {short}），開始前停下")


def _tidy_after_reward(p, group: int, s) -> None:
    """給寵物的步驟一做完就照規則表整理寵物欄（五隻同時；藍暴、金飛存倉，修寶、布克布克放生…）。
    失敗丟 JobError → 執行器停（不帶著滿的寵物欄往下跑）。"""
    from ..quest.headless import QuestStop
    p.nap(1)                                           # 讓獎勵的 Pet_PetInfo 推完
    p.log(f"[{group}] 步驟 {s.task_id} 給了寵物：馬上照規則表整理寵物欄")
    try:
        clean.clean_all(p, apply=True, confirm_release=True, parallel=True)
    except JobError as e:
        raise QuestStop(f"步驟 {s.task_id} 給寵物後整理沒完成：{e}")


def _forget_team_if_dismissed(p, q) -> None:
    """交寵物型（type 7）的步驟，伺服器會**自己解散隊伍**（提示 33109「系統幫你解散團隊單獨操作」）。
    解散後的 Team_TeamInfo 被 Party 當局部更新略過 → 隊伍快取還是 5 人 → 回練功只有隊長傳送、失敗、常駐停
    （2026-10-25 第二組劇本恐龍博士後，LESSONS #77 同一類，#83）。有這種步驟就清快取，之後 form_team 真的重組。"""
    steps = getattr(q, "steps", None) or []
    if any(getattr(s, "type", None) == 7 for s in steps) and hasattr(p, "s"):
        with p.lock:
            p.s.team = []
        p.log("這個任務有交寵物步驟（伺服器會解散隊伍）：清掉隊伍快取，之後真的重組")


def lead_actor(p, group: int):
    """隊長已經完成這個群組（已完成的任務放棄指令清不掉）→ 換第一個沒完成的人帶隊，原隊長跟隊幫忙打。
    接任務、跳點、NPC 互動都是隊長送的，已完成的人送會被伺服器拒（第四組 2010，使用者 2026-10-28 同意
    「有人已完成就自動續跑」）。回傳原本的順序（要還原）；不用換就回 None。"""
    with p.lock:
        done = [n for _, n in p.party if group in (p.finished.get(n) or [])]
    if not done or len(done) == len(p.party) or p.party[0][1] not in done:
        return None
    old = list(p.party)
    p.party = sorted(old, key=lambda rn: rn[1] in done)      # 沒完成的排前面（穩定排序，其餘順序不變）
    try:
        p.dismiss_team()                                     # 換隊長一定要重組（組隊協定由隊長發起）
    except Exception:                                        # noqa: BLE001  解散失敗要先換回來再往上丟：
        p.party = old                                        # old 還沒回傳給 run_one，p.party 會停在
        raise                                                # 換過的順序，之後回練功／下一個群組用錯隊長
    p.log(f"隊長 {old[0][1]} 已經完成 {group}，改由 {p.party[0][1]} 帶隊（原隊長跟隊幫忙打，不接任務）")
    return old


def _back_to_leader(p, old_party) -> None:
    """把換掉的隊長換回來（換過才做，重複呼叫沒事）。**一定要在 `_restore(p, post)` 之前**：
    後置動作 `first_cmd_on` 讀的是 `p.party[0]`，還原之前跑會把原隊長的設定開到臨時隊長身上，
    原隊長的首次動作就一直關著（2026-10-24 那個「打到一半有人死、精靈沒開回」的坑）。"""
    if not old_party or p.party == old_party:
        return
    p.party = list(old_party)
    p.dismiss_team()


def set_leader(p, name: Optional[str]):
    """指定誰帶隊（`--leader`）：把他排到隊伍第一位、解散重組。回傳原本的順序（要還原）；不用換回 None。

    執行器是從**隊長**目前進行到的步驟開跑的，落後的隊員補不到前面的步驟
    （2026-10-30 第五組 2016：四隻交完第 1 步，角色18的狗當時在出戰沒交成，
    隊長停在第 2 步 → 每次都卡「隊員任務狀態不一致」）。讓落後那位當隊長就會從他那一步重跑。"""
    if not name:
        return None
    everyone = [n for _, n in p.party]
    if name not in everyone:
        raise JobError(f"--leader 不在這組隊伍裡：{name}（有 {everyone}）")
    if p.party[0][1] == name:
        return None
    old = list(p.party)
    p.party = sorted(old, key=lambda rn: rn[1] != name)      # 指定的排第一（穩定排序，其餘順序不變）
    try:
        p.dismiss_team()                                     # 換隊長一定要重組（組隊協定由隊長發起）
    except Exception:                                        # noqa: BLE001  解散失敗先換回來再往上丟
        p.party = old
        raise
    p.log(f"改由 {name} 帶隊（原隊長 {old[0][1]} 跟隊）")
    return old


def run_one(p, group: int, free: int, grid_fn, stop_after=None, battle_timeout=None, leader=None,
            only=None) -> bool:
    """跑一個任務群組；回傳是不是照 --stop-after 提早停。"""
    from ..quest.headless import QuestStop
    pre, post = HOOKS.get(group, ([], []))
    if not p.form_team():
        raise JobError(f"{group}：組隊失敗")
    # 關遇敵、前置動作都要在**換隊長之前**用原隊長做：2010 的 first_cmd_off／first_cmd_on 讀的是
    # `p.party[0]`，換了之後關到的會是臨時隊長、原隊長的大地的精靈還開著跟著打（LESSONS #68 那個坑），
    # 收尾還原隊長後 post 又開回原隊長 → 臨時隊長的設定沒人補。原隊長的原地遇敵也在這裡先關掉。
    if not p.set_loop(False):
        raise JobError(f"{group}：關不了原地遇敵")
    for h in pre:
        h(p)
    before = {n: list(getattr(c, "listeners", [])) for n, c in p.clients.items()}
    old_party = None
    try:
        if only and not leader and p.party[0][1] not in only:
            leader = only[0]                                        # 補進度：由落後的人帶隊（接任務、互動都是隊長送的）
        old_party = set_leader(p, leader) or lead_actor(p, group)   # --leader 指定、或隊長已完成就換人帶隊
        if old_party and not p.form_team():
            _back_to_leader(p, old_party)
            raise JobError(f"{group}：換隊長後組隊失敗{_restore(p, post)}")
        kw = {} if only is None else {"only": only}
        q = build_quest(p, group, lambda m: p.log(f"[{group}] {m}"), grid_fn, free, stop_after=stop_after,
                        battle_timeout=battle_timeout, **kw)
        q.after_reward = lambda s: _tidy_after_reward(p, group, s)
        q.run()
        early = bool(getattr(q, "stopped_early", False))
        _forget_team_if_dismissed(p, q)
    except QuestStop as e:
        _forget_team_if_dismissed(p, q if "q" in locals() else None)
        # 後置動作是「把前置動作改掉的設定還原」，**失敗也一定要補回去**：
        # 2026-10-24 四寶玉打到一半有人被打死就停手，隊長首次動作（大地的精靈）一直關著，
        # 回去練功每 5 分鐘從 48～49 勝掉到 43 勝，過了 20 分鐘才被發現。
        _back_to_leader(p, old_party)
        with p.lock:
            fighting = any(p.fighting.values())
        if fighting:
            # 戰鬥型步驟等滿 battle_timeout 時還在打：回練功要傳送，戰鬥中一定被拒 → 常駐「回練功失敗」停止並登出＝放棄這場
            # （2026-10-31 第二組柯黑穆肯打到第 54 回合、BOSS 剩一半，就這樣被中斷）。使用者：「斷線不要重回練功區」
            # → 還在戰鬥中就 hold 停在原地，打完由使用者決定（LESSONS #121）
            raise JobHold(f"{group}：{e}（還在戰鬥中，停在原地不回練功）{_restore(p, post)}")
        raise JobError(f"{group}：{e}{_restore(p, post)}")
    finally:
        for n, c in p.clients.items():           # 拿掉這個任務掛上的監聽器（下一個任務重新掛）
            if hasattr(c, "listeners"):
                c.listeners[:] = [f for f in c.listeners if f in before[n]]
        _back_to_leader(p, old_party)            # 換過隊長就還原，下一個群組／回練功照原本的隊長
    for h in post:
        h(p)
    return early


def _restore(p, post: List[Callable]) -> str:
    """任務失敗後還原設定；還原本身再失敗只記在訊息裡，不蓋掉原本的失敗原因。"""
    if not post:
        return ""
    done, bad = [], []
    for h in post:
        try:
            h(p)
            done.append(h.__name__)
        except Exception as e:                   # noqa: BLE001  還原失敗要講，但不能蓋掉原因
            bad.append(f"{h.__name__}({type(e).__name__})")
    out = "；已還原：" + "、".join(done) if done else ""
    return out + ("；還原失敗（要手動補）：" + "、".join(bad) if bad else "")


# ---- 放棄任務（使用者 2026-10-26 第四組實機：四寶玉有人陣亡後狀態分岔，只能全隊放棄重接）----
IN_PROGRESS = (1, 4)             # Task_TaskInfo.taskStatus：1 進行中、4 可交付


def doing(p, name: str, group: int) -> Dict[int, int]:
    """這隻角色在這個群組裡還在進行中的步驟。"""
    with p.lock:
        st = dict(p.task_status.get(name) or {})
    return {tid: v for tid, v in st.items() if tid // 100000 == group and v in IN_PROGRESS}


def split_members(p, group: int) -> bool:
    """還沒做完這個群組的隊員，進行中的步驟不一樣（有人陣亡後狀態分岔：別人在第 N 步、他沒有進行中的步驟）。
    已經做完的人不算（本來就沒有進行中的步驟，LESSONS #101 由執行器只帶還沒做完的人）。"""
    with p.lock:
        todo = [n for _, n in p.party if group not in (p.finished.get(n) or [])]
    return len({tuple(sorted(doing(p, n, group))) for n in todo}) > 1


def laggards(p, group: int) -> Optional[Tuple[List[str], int]]:
    """分岔時落後的人能不能補做追上：還沒做完的人**都有**進行中的步驟、只是停在不同步 →
    回傳（停在最前面那一步的人, 補到哪一步就停＝領先的人那一步的前一步）；有人整個沒有進行中的步驟就回 None（只能放棄重接）。
    11-05 2010 風的寶玉陣亡：三隻已經在第 5 步（交寶玉），陣亡那隻還在第 4 步；放棄第 5 步伺服器不清，整組停住。"""
    with p.lock:
        todo = [n for _, n in p.party if group not in (p.finished.get(n) or [])]
    cur = {n: min(doing(p, n, group) or [0]) for n in todo}
    if not cur or 0 in cur.values() or len(set(cur.values())) < 2:
        return None
    low = min(cur.values())
    ahead = min(v for v in cur.values() if v != low)
    before = [t for t in sorted(t["ID"] for t in task_table().values() if t.get("group") == group) if t < ahead]
    if not before or before[-1] < low:
        return None
    return [n for n in todo if cur[n] == low], before[-1]


REGROUP_TRIES = 3                # 重打前組隊／回村補血最多試幾次
REGROUP_WAIT = 60.0              # 每次失敗後等幾秒再試（戰鬥中、剛過 00:00 傳送會被拒）


def regroup_and_heal(p, what: str) -> None:
    """有人被打死後重打前：清隊伍快取、真的重組、回村補血。傳送被拒（00:00 重置、還在戰鬥中）就等一下再試，
    最多 REGROUP_TRIES 次才丟 JobError（11-06 review：舊版補血一失敗就整個任務失敗、劇本停住）。"""
    for k in range(REGROUP_TRIES):
        if k:
            p.log(f"{what}：重打前組隊／補血沒成功，{int(REGROUP_WAIT)} 秒後再試（第 {k + 1}/{REGROUP_TRIES} 次）")
            p.nap(REGROUP_WAIT)
        with p.lock:
            p.s.team = []                        # 陣亡的人被送回記錄點、隊伍散了，快取還是 5 人
        if p.form_team() and p.heal_at_samugil():
            return
    raise JobError(f"{what}：重打前重新組隊或回村補血失敗（試了 {REGROUP_TRIES} 次）")


GIVE_UP_SENDS = 2                # 放棄任務最多送幾次（沒看到清掉才重送）
GIVE_UP_WAIT = 10.0              # 每次送完等狀態清掉幾秒


def give_up(p, group: int) -> Dict[str, str]:
    """全隊放棄一個任務群組。`Task_NewTaskGiveUp{taskGroup}` **沒有 response**（用 request 會逾時），
    只能送出去再看 `Task_TaskInfo` 推回的狀態（2026-10-26 實機確認）。
    五隻都沒有進行中的步驟才算成功；有人沒清掉就丟 JobError（規範第 2 點）。
    沒看到清掉會**再送一次**：放棄同一個群組重送無害（已經放棄的再送也只是沒有進行中的步驟），
    11-05 任務陣亡後重打時一隻 8 秒內沒推回狀態就整個劇本停住，前面三隻都正常清掉。
    """
    out = {}
    for _, name in p.party:
        before = doing(p, name, group)
        if not before:
            out[name] = "本來就沒有進行中的步驟"
            continue
        ok = False
        for attempt in range(GIVE_UP_SENDS):
            if attempt:
                p.log(f"{name} 放棄任務 {group} 沒看到清掉，再送一次")
            p.clients[name].send("Task_NewTaskGiveUp", {"taskGroup": group})
            ok = p.wait(lambda: not {t: v for t, v in (p.task_status.get(name) or {}).items()
                                     if t // 100000 == group and v in IN_PROGRESS}, GIVE_UP_WAIT)
            if ok:
                break
        after = doing(p, name, group)
        out[name] = f"放棄 {sorted(before)}" if ok else f"沒清掉：{after}"
        if not ok:
            raise JobError(f"{name} 放棄任務 {group} 沒確認（還在進行中：{after}）")
        p.log(f"{name} 放棄任務 {group}：原本進行中 {sorted(before)}")
    left = {n: doing(p, n, group) for _, n in p.party}
    bad = {n: v for n, v in left.items() if v}
    if bad:
        raise JobError(f"放棄任務 {group} 後還有人卡著：{bad}")
    return out


def status_lines(p, groups: List[int]) -> List[str]:
    out = []
    for g in groups:
        with p.lock:
            done = [n for _, n in p.party if g in (p.finished.get(n) or [])]
        mark = "全隊完成" if len(done) == len(p.party) else (f"部分完成 {done}" if done else "未完成")
        out.append(f"{g}：{mark}" + ("（有前／後置動作）" if g in HOOKS else ""))
    return out


def stash_shells(p, free: int) -> None:
    """有人背包空格不到 free、背包裡又有千年龜的殼 → 全隊的殼先存共倉（規則表 11879：平常留背包、存共倉換石幣）。
    2026-10-26 第二組四轉後跑 1.82：clean 不動殼（規則是 sell），成人儀式 2999 要 15 格全空 → 停在
    「背包空格不足 15」。存不進去就停（JobError），不開始任務。"""
    from ..shell_exchange import SHELL
    sh = getattr(p, "shells", None)
    if sh is None:
        return
    with p.lock:
        short = [n for _, n in p.party if sh.bag_free(n) < free and sh._count(n, (SHELL,)) > 0]
    if not short:
        return
    p.log(f"背包空格不到 {free} 而且有龜殼（{'、'.join(short)}）：全隊的殼先存共倉")
    if not sh.store_all():
        raise JobError("任務開始前龜殼存不進共倉（可能滿了），不開始任務")


_TASKS: Optional[Dict[int, dict]] = None


def task_table() -> Dict[int, dict]:
    """Task.json：taskId → 那一步（只讀一次）。"""
    global _TASKS
    if _TASKS is None:
        import json
        import os
        from ...features.gamedb.tables import CONFIGS
        with open(os.path.join(CONFIGS, "Task.json"), encoding="utf-8") as fp:
            _TASKS = {t["ID"]: t for t in json.load(fp) if isinstance(t, dict) and t.get("ID")}
    return _TASKS


def restore_task_items(p, group: int) -> Dict[str, Dict[int, int]]:
    """還沒做完的人：從他進行中的那一步起，之後要交的道具背包沒有、個倉第 1 頁有，就領回來。
    11-05 第一次跑完整劇本：2999 要背包全空，把 2012 做到一半拿到的答案用紙（2509～2515）存進個倉，
    重跑 2012 停在第 6 步「獲取智慧結晶」交不出、等不到翻面。個倉也沒有就照舊交給執行器（不買、不停）。
    回傳 {角色: {itemId: 取出幾個}}；取出沒確認就丟 JobError（規範第 2 點）。"""
    from .carry import depot, in_bag
    steps = sorted((t for t in task_table().values() if t.get("group") == group), key=lambda t: t.get("step", 0))
    took: Dict[str, Dict[int, int]] = {}
    for _, n in p.party:
        with p.lock:
            if group in (p.finished.get(n) or []):
                continue
        cur = min(doing(p, n, group) or [0])
        need: Dict[int, int] = {}
        for t in steps:
            if cur and t["ID"] < cur:
                continue                                   # 已經交過的步驟
            if not cur and t is not steps[0]:
                break                                      # 還沒接（或被重置）：只領第 1 步要帶的，舊進度的道具不領回
            for c in t.get("condition") or []:
                if isinstance(c, dict) and c.get("itemId"):
                    need[c["itemId"]] = max(need.get(c["itemId"], 0), int(c.get("itemNum") or 1))
        missing = {i: k - in_bag(p, n, i) for i, k in need.items() if in_bag(p, n, i) < k}
        if not missing:
            continue
        rows = [r for r in depot(p, n) if r.get("itemId") in missing and r.get("uniqueIndex")]
        for r in rows:
            iid = r["itemId"]
            k = min(missing[iid] - took.get(n, {}).get(iid, 0), int(r.get("overlay") or 1))
            if k <= 0:
                continue
            before = in_bag(p, n, iid)
            g = p.clients[n].request("ItemHandle_GetItemToNewDepot",
                                     {"page": 1, "isPublic": False, "uniqueIndex": r["uniqueIndex"], "count": k}) or {}
            if not (g.get("isOK") and p.wait(lambda: in_bag(p, n, iid) >= before + k, 5)):
                raise JobError(f"{group}：{n} 從個倉取回任務道具 {iid} 沒確認：{g}")
            took.setdefault(n, {})[iid] = took.get(n, {}).get(iid, 0) + k
            p.nap(0.4)
        if took.get(n):
            p.log(f"任務 {group}：{n} 從個倉取回之後要交的道具 {took[n]}")
    return took


def _group_items(group: int) -> Dict[int, int]:
    """這個群組的步驟用到的道具（要交的、獎勵發的）→ 最多要留幾個；別的群組也用到的不算（不能亂刪）。"""
    own: Dict[int, int] = {}
    other = set()
    for t in task_table().values():
        ids = [(c["itemId"], int(c.get("itemNum") or 1)) for c in t.get("condition") or []
               if isinstance(c, dict) and c.get("itemId")]
        ids += [(r["itemId"], 0) for r in t.get("reward") or [] if isinstance(r, dict) and r.get("itemId")]
        for iid, k in ids:
            if t.get("group") == group:
                own[iid] = max(own.get(iid, 0), k)
            else:
                other.add(iid)
    return {i: max(k, 1) for i, k in own.items() if i not in other}


def _group_tokens(group: int) -> set:
    """這個群組要**交出去**的道具（步驟條件），而且別的群組都用不到——做完這個群組就只是佔格子的廢物。
    只看條件、不看獎勵：最後一步的獎勵（例：2010 的 2765）可能是有用的東西，不能刪。"""
    mine, other = set(), set()
    for t in task_table().values():
        ids = {c["itemId"] for c in t.get("condition") or [] if isinstance(c, dict) and c.get("itemId")}
        ids |= {r["itemId"] for r in t.get("reward") or [] if isinstance(r, dict) and r.get("itemId")}
        if t.get("group") == group:
            mine |= {c["itemId"] for c in t.get("condition") or [] if isinstance(c, dict) and c.get("itemId")}
        else:
            other |= ids
    return mine - other


def drop_finished_tokens(p) -> Dict[str, List[int]]:
    """每隻身上「已經做完的群組」專用的交件道具全刪。
    11-06 第三組：2010 早就做完，舊版重打留下兩套四寶玉＋結晶石（10 格）、2031 的勾玉 5 格 → 背包 15 格全滿，
    英雄島買耐久力回複藥（1501）一直「沒確認」、劇本停住。clean 的規則表沒寫這些道具就不動。
    回傳 {角色: [刪掉的 itemId]}；刪除沒確認由 clean.do_delete 丟 JobError。"""
    out: Dict[str, List[int]] = {}
    for _, n in p.party:
        with p.lock:
            done = list(p.finished.get(n) or [])
        junk = set()
        for g in done:
            junk |= _group_tokens(g)
        if not junk:
            continue
        cells = sorted((it["itemIndex"], it["itemId"]) for it in p.bag(n) if it.get("itemId") in junk)
        if cells:
            p.log(f"{n} 身上已完成任務的交件道具 {[i for _, i in cells]} 刪掉（任務做完就用不到）")
            clean.do_delete(p, n, cells)
            out[n] = [i for _, i in cells]
        gone = _drop_from_depot(p, n, junk)
        if gone:
            out.setdefault(n, []).extend(gone)
    return out


def _drop_from_depot(p, name: str, junk: set) -> List[int]:
    """個人倉庫第 1 頁裡的已完成任務交件道具：一筆一筆領到背包再刪（倉庫沒有直接刪的協定）。
    使用者 11-06 同意刪：2010 早就做完，舊的四寶玉、結晶石被清背包存進個倉，一組佔 5～10 格。
    背包沒空格就先不動（下次整理再刪）；領出沒確認丟 JobError（規範第 2 點）。"""
    from .carry import depot, in_bag
    from ..shell_exchange import BAG_SIZE
    try:
        rows = [r for r in depot(p, name) if r.get("itemId") in junk and r.get("uniqueIndex")]
    except Exception as e:                         # noqa: BLE001  查不到倉庫就這次不動
        p.log(f"{name} 查個人倉庫失敗（{e}），已完成任務的道具這次先不刪")
        return []
    gone: List[int] = []
    for r in rows:
        if len(p.bag(name)) >= BAG_SIZE:
            p.log(f"{name} 背包滿了，倉庫裡已完成任務的道具先不刪")
            break
        iid, k = r["itemId"], int(r.get("overlay") or 1)
        before = in_bag(p, name, iid)
        g = p.clients[name].request("ItemHandle_GetItemToNewDepot",
                                    {"page": 1, "isPublic": False, "uniqueIndex": r["uniqueIndex"], "count": k}) or {}
        if not (g.get("isOK") and p.wait(lambda: in_bag(p, name, iid) >= before + k, 5)):
            raise JobError(f"{name} 從個倉領出已完成任務的道具 {iid} 沒確認：{g}")
        cells = sorted((it["itemIndex"], it["itemId"]) for it in p.bag(name) if it.get("itemId") == iid)
        clean.do_delete(p, name, cells)
        gone.extend([iid] * len(cells))
        p.nap(0.4)
    if gone:
        p.log(f"{name} 個人倉庫裡已完成任務的交件道具 {gone} 領出來刪掉")
    return gone


def _first_step_items(group: int) -> set:
    """第 1 步要帶的道具（例：2031 先買好的耐久力回複藥 1501）：還沒接任務前就該在身上，不能當舊道具丟。"""
    steps = sorted((t for t in task_table().values() if t.get("group") == group), key=lambda t: t.get("step", 0))
    return {c["itemId"] for c in (steps[0].get("condition") if steps else None) or []
            if isinstance(c, dict) and c.get("itemId")}


def drop_stale_tokens(p, group: int) -> Dict[str, List[int]]:
    """還沒做完、也沒有進行中步驟的人（上一次沒做完被重置／放棄）：這個群組的交件道具全丟，重接會重新發。
    使用者 11-06：「每次解任務前都應該先清空道具欄，如果是上一個任務沒完成重置，道具要丟掉」。
    第 1 步要帶的道具不丟。回傳 {角色: [刪掉的 itemId]}。"""
    junk = _group_tokens(group) - _first_step_items(group)
    out: Dict[str, List[int]] = {}
    if not junk:
        return out
    for _, n in p.party:
        with p.lock:
            if group in (p.finished.get(n) or []):
                continue
        if doing(p, n, group):
            continue
        cells = sorted((it["itemIndex"], it["itemId"]) for it in p.bag(n) if it.get("itemId") in junk)
        if cells:
            p.log(f"任務 {group}：{n} 沒有進行中的步驟（上次沒做完被重置），身上舊的交件道具 {[i for _, i in cells]} 丟掉")
            clean.do_delete(p, n, cells)
            out[n] = [i for _, i in cells]
    return out


def trim_task_items(p, group: int) -> Dict[str, List[int]]:
    """這個群組專用的道具，背包裡多過要交的數量就刪掉多的。
    11-05 2010 陣亡重打 3 次：每次放棄重接又發一套四寶玉（第 1～4 步的獎勵），舊的留在背包，
    隊長身上三套結晶石＋四寶玉塞滿 15 格 → 停在「背包空格不足 3」；clean 把它們當任務道具不動。
    回傳 {角色: [刪掉的 itemId]}；刪除沒確認由 clean.do_delete 丟 JobError。"""
    keep = _group_items(group)
    if not keep:
        return {}
    out: Dict[str, List[int]] = {}
    for _, n in p.party:
        cells = sorted((it["itemIndex"], it["itemId"], int(it.get("overlay") or 1))
                       for it in p.bag(n) if it.get("itemId") in keep)
        seen: Dict[int, int] = {}
        extra = []
        for idx, iid, k in cells:
            seen[iid] = seen.get(iid, 0) + k
            if seen[iid] - k >= keep[iid]:            # 前面的格子已經夠交了，這格整格多餘
                extra.append((idx, iid))
        if extra:
            p.log(f"任務 {group}：{n} 身上重複的任務道具 {[i for _, i in extra]} 刪掉（只留要交的）")
            clean.do_delete(p, n, extra)
            out[n] = [i for _, i in extra]
    return out


def stash_bag(p, free: int) -> None:
    """要整個背包全空的任務（成人儀式 2999，15 格）：整理和存殼之後背包還有東西的角色，剩下的道具全存個倉。
    11-04 一天三次停在「背包空格不足 15」：角色96的 2D人龍進化石、角色100的 2D老虎自選蛋、角色77的鑽石道具——
    規則表沒寫的道具 clean 不動，只能手動 `carry --store` 再 resume（LESSONS #161）。
    只在要全空時才做：一般任務只要幾格，身上可能有任務道具（例：四寶玉），不亂存。"""
    from ..shell_exchange import BAG_SIZE
    from .clean import do_store
    if free < BAG_SIZE:
        return
    table = p.item_table()
    for _, n in p.party:
        cells = sorted(((it["itemIndex"], it["itemId"]) for it in p.bag(n)), key=lambda x: x[0])
        if not cells:
            continue
        p.log(f"任務要背包 {free} 格全空：{n} 背包剩的 {[(table.get(i, {}) or {}).get('nameTn') or i for _, i in cells]} 存個倉")
        for idx, iid in cells:
            do_store(p, n, idx, iid, False)
            p.nap(0.4)


def run(p, args) -> Tuple[str, dict]:
    p.quest_record_dir =getattr(args, "record", None)      # 測試的 Args 沒這欄＝不錄（不寫真實路徑，規範 13）
    if getattr(args, "give_up", None):
        gs = [int(x) for x in str(args.give_up).replace("，", ",").split(",") if x.strip()]
        if not getattr(args, "apply", False):
            for g in gs:
                p.log(f"{g}：進行中的步驟 " + str({n: sorted(doing(p, n, g)) for _, n in p.party}))
            return "只列狀態（沒加 --apply，沒有放棄任何任務）", {"give_up": gs}
        done = {g: give_up(p, g) for g in gs}
        return f"放棄任務完成：{gs}", {"give_up": gs, "detail": done}
    groups = with_prereq(p, groups_of(args))
    if not getattr(args, "apply", False):
        for line in status_lines(p, groups):
            p.log(line)
        return "只列狀態（沒加 --apply，沒有送任何動作）", {"groups": groups}
    grid_fn = getattr(args, "grid_fn", None) or _grid_loader()
    done = []
    sg = stop_group(args)
    for g in groups:
        with p.lock:
            all_done = all(g in (p.finished.get(n) or []) for _, n in p.party)
        if all_done:
            # 全隊早就做完：不整理、不存背包（11-05 2999 已完成還照樣「要背包 15 格全空」把 2012 做到一半的答案用紙存進個倉）
            p.log(f"任務 {g}：全隊都已完成，跳過")
            done.append(g)
            continue
        free = args.free if getattr(args, "free", None) is not None else FREE.get(g, DEFAULT_FREE)
        sa = args.stop_after if g == sg else None
        # 使用者 2026-10-25「1.82 四大 英雄島 所有任務執行前 先平行快速整理一次」：
        # 背包被練功掉落塞滿會讓執行器停在「背包至少空 N 格」，先照規則表整理（只動道具、五隻同時）
        # 「寵物不夠」：寵物也照規則表處理（存倉、放生——放生清單是使用者核准過的規則表），再算這個任務要空幾格
        if not getattr(args, "no_preclean", False):
            p.log(f"任務 {g} 開始前：五隻同時整理背包與寵物欄")
            clean.clean_all(p, apply=True, confirm_release=True, parallel=True)
            p.nap(1)
        preflight_pets(p, g, getattr(args, "steps_of", lambda g: None)(g))
        stash_shells(p, free)
        stash_bag(p, free)
        drop_finished_tokens(p)
        drop_stale_tokens(p, g)
        restore_task_items(p, g)
        trim_task_items(p, g)
        p.log(f"開始任務 {g}（背包至少空 {free} 格）" + (f"，做完 {sa} 就停" if sa else ""))
        retry, tries = int(getattr(args, "retry", 0) or 0), 0
        while True:
            refill_water(p, int(getattr(args, "water", 0) or 0))
            try:
                early = run_one(p, g, free, grid_fn, stop_after=sa,
                                battle_timeout=getattr(args, "battle_timeout", None),
                                leader=getattr(args, "leader", None))
                break
            except JobError as e:
                # 使用者 2026-10-31：「為什麼一直回去練功，浪費時間」——打輸（有人被打死）就回薩村醫院補血、
                # 直接回來重打同一關，不回練功地圖；只有 --retry 次數用完才照舊失敗（LESSONS #122）
                msg = str(e)
                dead, split = "被打死" in msg, "隊員任務狀態不一致" in msg
                if isinstance(e, JobHold) or not (dead or split) or tries >= retry:
                    raise
                tries += 1
                if dead:
                    p.log(f"任務 {g}：有人被打死，重新集合組隊、回村補血後直接重打（第 {tries}/{retry} 次，不回練功）")
                    # 陣亡的人被送回記錄點、隊伍散了，但隊伍快取還是 5 人 → form_team 不重組、補血只傳走一部分
                    # （11-05 第一次跑完整劇本：2010 風的寶玉陣亡後「傳到薩姆吉爾村失敗 isOK True」、回練功也傳不回去）
                    regroup_and_heal(p, f"{g}（第 {tries} 次）")
                lag = laggards(p, g) if split_members(p, g) else None
                if lag:
                    # 大家都還在做、只是停在不同步：落後的人補到領先的人那一步之前，其他人跟隊（不放棄已經拿到的進度）
                    behind, upto = lag
                    p.log(f"任務 {g}：隊員進度分岔，{behind} 先補做到 {upto}，其他人跟隊（第 {tries}/{retry} 次）")
                    try:
                        run_one(p, g, free, grid_fn, stop_after=upto,
                                battle_timeout=getattr(args, "battle_timeout", None), only=behind)
                    except JobHold:
                        raise
                    except JobError as e2:
                        p.log(f"任務 {g}：補進度沒成功（{e2}），下一輪再看")
                elif split_members(p, g):
                    # 陣亡的人任務狀態跟其他人分岔（別人在第 N 步、他是 None），再打只會卡「隊員任務狀態不一致」：
                    # 全隊放棄重接（LESSONS #94；11-05 兩組 2010 都這樣停住）
                    p.log(f"任務 {g}：隊員進度分岔，全隊放棄後重接（第 {tries}/{retry} 次）")
                    give_up(p, g)
                    drop_stale_tokens(p, g)
                    trim_task_items(p, g)
                p.nap(2)
        p.log(f"任務 {g} {'停在 ' + str(sa) + '（--stop-after）' if early else '完成'}，清背包")
        p.nap(getattr(args, "settle", 3.0))          # 讓獎勵的 Item_ItemInfo 推完
        clean.clean_all(p, apply=True, items_only=True)
        if early:
            return (f"任務 {g} 停在 {sa}（--stop-after），後面不跑；之前完成 {done}",
                    {"done": done, "stopped_at": sa})
        done.append(g)
    return f"完成 {len(done)} 個任務：{done}", {"done": done}
