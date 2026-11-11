"""rebirth：轉生（漆黑洞窟，任務群組 2004＋轉數）。取代暫存資料夾的 rebirth_prep.py＋手動三段指令。

    python -X utf8 -m x7k2qm.autoplay.jobs rebirth --profile 2 --discover                              # 只檢查（唯讀）
    python -X utf8 -m x7k2qm.autoplay.jobs rebirth --profile 2 --discover --apply --confirm-rebirth    # 真的轉生

**轉生不可逆**：使用者每次親口同意才轉。`--apply` 一定要再加 `--confirm-rebirth`，少一個就在登入前停（什麼都不送）。

檢查（不加 --apply 就只做這個；不通過列原因、結束碼 1）：每隻角色
- 1.82 任務（2010～2028 裡的 16 個＋3003）、四大洞窟 2000～2003、英雄島 2031／2032 都完成（`finishTasks`）；
- 剩餘點數 0（`potentialPoint`；沒配完就轉生等於白丟屬性點）；
- 寵物欄有空格（最後一步給守護獸，滿格拿不到）；
- 四大證明 2701 2770 2707 2735 在背包（16F 守門人收走換精靈王之弓）；不在背包但在個人倉庫算「待取出」，
  **只有 --apply 才取出**；
- 等級夠（2004～2007 要 131、2008 五轉要 140）；全隊轉數一樣。

--apply 的流程（2026-10-24 第二組二轉、10-25 三轉實機）：
1. 檢查全過 → 四大證明從個人倉庫取出、再確認一次都在背包。
2. 組隊、關遇敵 → 解任務執行器跑到**第 4 步「挑戰機暴」（200X00004）就停**
   （戰鬥型，`battle_timeout` 420：2026-10-24 第三組二轉，漆黑 10 樓與機暴 180 秒內沒翻面，其實早就打完了）。
3. **全部寵物存個人寵物倉庫**（`pets.store_all`：鎖定清 0 → 改休息 → 逐隻存 → 確認寵物欄全空）；
   使用者 2026-10-24：打完機暴後全存，守護獸才放得進寵物欄，轉生獎勵寵也固定在第 1 格。
4. 跑完剩下的步驟（通過守護人、覲見精靈王）→ 等 `Role_RoleInfo.trans` 變成 轉數＋1。
任何一步失敗就停（規範第 2 點）；重跑會從進行中的那一步接續（執行器自己略過做完的步驟）。

轉生後的規範（開禮盒、吃果、寵物重配、自訂配點、隱藏）不在這裡做：重開練功時
`Party.init_baseline` → `init_after_trans` 會對「1 級且轉數 ≥ 1」的角色自動套。
"""

from typing import Dict, List, Tuple

from . import JobError, quests
from . import pets as pets_job

NAME = "rebirth"
HELP = "轉生（不加 --apply 只檢查；--apply 要再加 --confirm-rebirth）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

PROOFS = (2701, 2770, 2707, 2735)                      # 四大證明（地水火風；item-rules.md：轉生後才可丟）
TASKS_182 = (2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2023, 2024, 2025, 2027,
             2028, 3003)
CAVES = (2000, 2001, 2002, 2003)                       # 四大洞窟
HERO = (2031, 2032)                                    # 英雄島前傳（每轉都要做）
NEED = frozenset(TASKS_182 + CAVES + HERO)
FIRST_GROUP, LAST_GROUP = 2004, 2008                   # 一轉～五轉
MIN_LEVEL = {2008: 140}                                # 其他 131（Task.json 條件 level）
DEFAULT_LEVEL = 131
BOSS_STEP = 4                                          # 第 4 步「挑戰機暴」：打完先停、全部寵物存倉
BATTLE_TIMEOUT = 420.0
SLOT_MAX = 5
TRANS_WAIT = 15.0                                      # 跑完後等轉數更新的秒數
PROOF_STEP = 3                                         # 第 3 步交四大證明（Task.json 200X00003 condition）
ACTIVE = (1, 2, 4)                                     # 任務狀態：進行中、完成、進度更新


def proofs_handed(p, name: str, group: int) -> bool:
    """這一轉的四大證明已經交掉了（第 3 步完成，或更後面的步驟已經在進行／完成）。
    2026-10-26 第二組四轉：第一次跑到機暴後存倉失敗（LESSONS #89），重跑時四大證明早被第 3 步收走，
    檢查卻還要求它們在背包或個倉 → 「四大證明背包和個倉都沒有」停（LESSONS #90）。
    登入時伺服器只推目前那一步（例：200700005 進行中），所以看「比第 3 步後面的步驟」有沒有狀態。"""
    with p.lock:
        st = dict(p.task_status.get(name) or {})
    base = group * 100000
    return st.get(base + PROOF_STEP) == 2 or any(
        base + PROOF_STEP < tid < base + 100 and v in ACTIVE for tid, v in st.items())


def add_args(ap) -> None:
    ap.add_argument("--confirm-rebirth", action="store_true",
                    help="真的轉生（不可逆；使用者每次親口同意才加）。要和 --apply 一起用")
    ap.add_argument("--free", type=int, default=1, help="開始前每隻背包至少空幾格（預設 1）")


def check(args) -> None:
    if getattr(args, "apply", False) and not getattr(args, "confirm_rebirth", False):
        raise JobError("轉生不可逆：--apply 要再加 --confirm-rebirth（使用者親口同意才加）；沒有送任何東西")


def group_for(trans: int) -> int:
    g = FIRST_GROUP + trans
    if not FIRST_GROUP <= g <= LAST_GROUP:
        raise JobError(f"轉數 {trans} 沒有下一轉的任務（只有 {FIRST_GROUP}～{LAST_GROUP}）")
    return g


def _depot_rows(p, name: str) -> List[dict]:
    """個人倉庫第 1 頁（唯讀查詢）。"""
    r = p.clients[name].request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": False}) or {}
    rows = r.get("itemInfoList") or []
    return list(rows.values()) if isinstance(rows, dict) else list(rows)


def inspect(p, name: str) -> dict:
    """一隻角色的轉生條件。problems 空＝通過；in_depot＝不在背包但在個人倉庫的四大證明（--apply 會取出）。"""
    with p.lock:
        a = dict(p.attrs.get(name, {}))
        fin = set(p.finished.get(name) or a.get("finishTasks") or [])
        level = p.s.levels.get(name, a.get("level"))
        trans = p.trans.get(name, a.get("trans"))
    pets = pets_job.slots(p, name)
    have = {b["itemId"] for b in p.bag(name)}
    handed = False
    if trans is not None:
        try:
            handed = proofs_handed(p, name, group_for(trans))
        except JobError:
            pass                                       # 沒有下一轉：下面的等級檢查會列原因
    missing = [] if handed else [x for x in PROOFS if x not in have]
    in_depot = []
    if missing:
        dep = {r.get("itemId") for r in _depot_rows(p, name)}
        in_depot = [x for x in missing if x in dep]
    problems = []
    miss = sorted(NEED - fin)
    if miss:
        problems.append(f"缺任務 {miss}")
    if a.get("potentialPoint") is None:
        problems.append("不知道剩餘點數")
    elif a.get("potentialPoint"):
        problems.append(f"剩餘點數 {a.get('potentialPoint')} 沒配完")
    if len(pets) >= SLOT_MAX:
        problems.append(f"寵物欄滿 {len(pets)}/{SLOT_MAX}")
    lost = [x for x in missing if x not in in_depot]
    if lost:
        problems.append(f"四大證明背包和個倉都沒有 {lost}")
    if trans is None:
        problems.append("不知道轉數")
    else:
        try:
            need_lv = MIN_LEVEL.get(group_for(trans), DEFAULT_LEVEL)
        except JobError as e:
            problems.append(str(e))
            need_lv = None
        if need_lv is not None and (level or 0) < need_lv:
            problems.append(f"等級 {level} 未達 {need_lv}")
    return {"name": name, "trans": trans, "level": level, "problems": problems, "in_depot": in_depot,
            "pets": len(pets), "points": a.get("potentialPoint"), "handed": handed}


def check_all(p) -> Tuple[List[dict], List[str]]:
    rows = [inspect(p, n) for _, n in p.party]
    bad = [r["name"] for r in rows if r["problems"]]
    if len({r["trans"] for r in rows}) > 1:
        bad = [r["name"] for r in rows]
        for r in rows:
            r["problems"].append(f"全隊轉數不一致 {[(x['name'], x['trans']) for x in rows]}")
    for r in rows:
        p.log(f"{r['name']}｜{r['trans']} 轉 {r['level']} 級｜剩餘點數 {r['points']}｜寵物 {r['pets']}/{SLOT_MAX}"
              + (f"｜四大證明在個倉（--apply 會取出）{r['in_depot']}" if r["in_depot"] else "")
              + ("｜四大證明這一轉已經交了（第 3 步做完，接續）" if r.get("handed") else "")
              + (f"｜不通過：{'；'.join(r['problems'])}" if r["problems"] else "｜通過"))
    return rows, bad


def take_proofs(p, name: str) -> None:
    """四大證明不在背包的從個人倉庫第 1 頁取出（一件一件取，每件回 isOK 且背包出現才算）。"""
    for iid in PROOFS:
        if any(b["itemId"] == iid for b in p.bag(name)):
            continue
        row = next((r for r in _depot_rows(p, name) if r.get("itemId") == iid and r.get("uniqueIndex")), None)
        if row is None:
            raise JobError(f"{name} 個人倉庫找不到四大證明 {iid}")
        g = p.clients[name].request("ItemHandle_GetItemToNewDepot",
                                    {"page": 1, "isPublic": False, "uniqueIndex": row["uniqueIndex"], "count": 1}) or {}
        ok = g.get("isOK") and p.wait(lambda: any(it.get("itemId") == iid and it.get("overlay", 1) > 0
                                                  and it.get("itemStorge", 0) == 0
                                                  for it in p.items.get(name, {}).values()), 5)
        if not ok:
            raise JobError(f"{name} 從個人倉庫取出四大證明 {iid} 沒確認：{g}")
        p.log(f"{name} 從個人倉庫取出四大證明 {iid}")


def _grid_loader():
    from ..pathing import load_grid
    cache: Dict[int, object] = {}

    def grid(m):
        if m not in cache:
            cache[m] = load_grid(m)
        return cache[m]
    return grid


def build_quest(p, group: int, log, grid_fn, need_free: int, stop_after, battle_timeout: float):
    """另外包一層，測試可以換成假的執行器。"""
    from ..quest.headless import build_quest as real
    return real(p, group, log, grid_fn, need_free=need_free, stop_after=stop_after, battle_timeout=battle_timeout)


DEATH_RETRY = 6                   # 轉生任務戰鬥有人被打死：重新組隊、回村補血後重打幾次
# 11-08 2→3 轉五組都在機暴陣亡，其中一組 3 次全用完、最後一次補打才過；用完就整個轉生失敗、劇本停住，
# 四大證明又已經交出去，多打幾次比停下來等人好


def run_quest(p, group: int, grid_fn, need_free: int, stop_after=None):
    """跑一段執行器；有人被打死就重新組隊、回村補血後重打（最多 DEATH_RETRY 次），其他錯誤照舊停。
    11-06 第五組轉生 2004 打到機暴前角色57被打死，劇本整個停住；1.82 的 quests 早就有 --retry，轉生沒有。"""
    for tries in range(DEATH_RETRY + 1):
        try:
            q = _run_quest_once(p, group, grid_fn, need_free, stop_after)
            if not quests.split_members(p, group):
                return q
            # 執行器只看帶隊的進度：補進度時陣亡、補血後重跑，帶隊早已過了 stop_after 就直接「停在機暴」回來，
            # 落後的人其實還沒打完（11-08 第五組 2→3 轉照樣存寵物、空手補打機暴連死）。分岔就照分岔處理、先補完
            raise JobError(f"轉生 {group}：執行器跑完但隊員任務狀態不一致（還有人落後）")
        except JobError as e:
            msg = str(e)
            dead, split = "被打死" in msg, "隊員任務狀態不一致" in msg
            if not (dead or split) or tries >= DEATH_RETRY:
                raise
            if dead:
                p.log(f"轉生 {group}：有人被打死，重新組隊、回村補血後重打（第 {tries + 1}/{DEATH_RETRY} 次）")
                quests.regroup_and_heal(p, f"轉生 {group}")
            lag = quests.laggards(p, group) if quests.split_members(p, group) else None
            if lag:
                # 陣亡那隻停在前一步（11-06 第五組角色57停在 200400002、其他人 200400003）：落後的人補到同一步，
                # 其他人跟隊。**不放棄**：轉生前幾步已經交出四大證明，放棄就拿不回來、再也轉不了
                behind, upto = lag
                p.log(f"轉生 {group}：隊員進度分岔，{behind} 先補做到 {upto}，其他人跟隊（第 {tries + 1}/{DEATH_RETRY} 次）")
                try:
                    quests.run_one(p, group, need_free, grid_fn, stop_after=upto, battle_timeout=BATTLE_TIMEOUT,
                                   only=behind)
                except JobError as e2:
                    # 補打機暴又有人被打死：算一次重打、補血後下一輪再補（11-06 0 轉組補打時陣亡，整個轉生直接失敗、
                    # 剩下的重打次數沒用到）
                    if "被打死" not in str(e2):
                        raise
                    p.log(f"轉生 {group}：補進度時有人被打死，重新組隊、回村補血後再補（第 {tries + 1}/{DEATH_RETRY} 次）")
                    quests.regroup_and_heal(p, f"轉生 {group}")
                    continue
            elif split:
                raise                              # 分岔又補不了（有人沒有進行中的步驟）：停下來給人看，不放棄
            # 重打前一定真的重組：補進度換過隊長、還原時會解散隊伍（11-06 第五組補完後沒重組，
            # 角色92一個人跳去交了 200400003，其他四隻留在 32011 → 又分岔）
            with p.lock:
                p.s.team = []
            if not p.form_team():
                raise JobError(f"轉生 {group}：重打前重新組隊失敗")
            if not p.set_loop(False):
                raise JobError(f"轉生 {group}：重打前關不了原地遇敵")


def _run_quest_once(p, group: int, grid_fn, need_free: int, stop_after=None):
    """跑一段執行器；QuestStop 轉成 JobError，跑完拿掉這段掛上的監聽器（下一段重新掛、重播 history）。"""
    from ..quest.headless import QuestStop
    before = {n: list(getattr(c, "listeners", [])) for n, c in p.clients.items()}
    try:
        q = build_quest(p, group, lambda m: p.log(f"[{group}] {m}"), grid_fn, need_free, stop_after, BATTLE_TIMEOUT)
        q.run()
        return q
    except QuestStop as e:
        raise JobError(f"轉生 {group}：{e}")
    finally:
        for n, c in p.clients.items():
            if hasattr(c, "listeners"):
                c.listeners[:] = [f for f in c.listeners if f in before[n]]


def run(p, args) -> Tuple[str, dict]:
    rows, bad = check_all(p)
    if not getattr(args, "apply", False):
        if bad:
            raise JobError(f"轉生前檢查不通過 {bad}（只檢查，沒有送任何動作）")
        return "轉生前檢查：全部通過（只檢查；要轉生加 --apply --confirm-rebirth）", {"rows": rows}
    if not getattr(args, "confirm_rebirth", False):       # check() 已擋過；這裡再擋一次，直接呼叫 run 也不會轉
        raise JobError("轉生不可逆：沒有 --confirm-rebirth，不送任何轉生封包")
    if bad:
        raise JobError(f"轉生前檢查不通過 {bad}，不開始轉生")
    trans = rows[0]["trans"]
    group = group_for(trans)
    for r in rows:
        if not r["handed"]:
            take_proofs(p, r["name"])
    rows, bad = check_all(p)                           # 取出後再確認一次（四大證明都在背包了）
    still = [r["name"] for r in rows if r["in_depot"]]
    if bad or still:
        raise JobError(f"取出四大證明後檢查還是不通過 {bad or still}，不開始轉生")
    if not p.form_team():
        raise JobError("組隊失敗")
    p.ensure_fight_pets()                              # 打機暴前補戰寵、騎寵（見 quests.regroup_and_heal）
    if not p.set_loop(False):
        raise JobError("關不了原地遇敵")
    grid_fn = getattr(args, "grid_fn", None) or _grid_loader()
    free = getattr(args, "free", 1)
    boss = group * 100000 + BOSS_STEP
    p.log(f"轉生 {trans}→{trans + 1}：任務 {group}，先跑到 {boss}（挑戰機暴）就停")
    q = run_quest(p, group, grid_fn, free, stop_after=boss)
    if not q.stopped_early:
        raise JobError(f"轉生 {group}：執行器沒有停在 {boss}（可能整個任務已經做完？），寵物沒動，先查狀態")
    p.log(f"打完機暴 {boss}，全部寵物存個人寵物倉庫")
    stored = {}
    for _, n in p.party:
        stored[n] = pets_job.store_all(p, n)
    p.log("寵物欄全空，跑剩下的步驟（通過守護人、覲見精靈王）")
    run_quest(p, group, grid_fn, free)
    if not p.wait(lambda: all(p.trans.get(n) == trans + 1 for _, n in p.party), TRANS_WAIT):
        with p.lock:
            now = {n: p.trans.get(n) for _, n in p.party}
        raise JobError(f"轉生 {group} 步驟跑完，但沒看到轉數變成 {trans + 1}：{now}（重登確認）")
    return (f"轉生完成：{trans}→{trans + 1} 轉（任務 {group}）；寵物已存個倉 {stored}。"
            f"重開練功會自動套轉生後規範（開禮盒、吃果、金虎＋戰寵、自訂加點、隱藏）",
            {"group": group, "trans": trans + 1, "stored": stored})
