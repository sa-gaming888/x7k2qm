"""clean：照處置規則表（`doc/autoplay/game/item-rules.md`）整理每隻角色的背包與寵物欄。

    python -X utf8 -m x7k2qm.autoplay.jobs clean --profile 2 --discover            # 只列清單（登入讀狀態，不送任何動作）
    python -X utf8 -m x7k2qm.autoplay.jobs clean --profile 2 --discover --apply    # 照清單執行
    ... --apply --confirm-release                                                     # 連放生也做（不可逆）

規則（出錯就停，規範第 2 點）：
- 不加 `--apply`：零動作封包、不停練功，只列每隻要做什麼。
- 刪除 `Item_batchDelItem{itemIndexList}`：送之前逐格核對 itemId，一次最多 5 格；送完等那幾格消失、
  其他格完全沒變（itemId／數量），沒確認就停（官方畫面版也是一次 4～5 格，參考專案 purge.py 錄檔）。
- 存倉 `ItemHandle_SaveItemToNewDepot{page:1, isPublic, itemIndex}`：回 isOK 且那格消失才算。
- 穿上 `ItemHandle_itemEquipOn{itemIndex}`：回 isOK 且身上出現那件才算。
- 寵物存倉 `NewPet_SavePetToNewDepot{page:1, isPublic:false, petIndex}`、放生 `Pet_DropPet{petIndex}`：
  等該格 petId ≤ 0；戰寵／騎寵（狀態 2／3 或外掛鎖定的唯一編號）絕不動；放生另要 `--confirm-release`。
- 賣（sell）：協定還沒查到，只列出、不送，留在背包。
- 吃（eat）：`Item_ItemUse{itemIndex, itemNum:1, itemScriptId}`（伺服器不回 response，看數量變少）；
  疊著的一次只消耗一個，吃到那格空掉為止（上限 MAX_EAT）。經驗果留在背包會佔格子，
  2026-10-24 就因此卡住任務（背包只有 15 格、解任務要 15 格全空）。
- 穿在身上的裝備不動；保護名單與任務條件要用的道具碰到刪除／賣就停（規則表有測試擋，這裡再擋一次）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..item_rules import Ctx, Rules, load
from . import JobError

NAME = "clean"
HELP = "照 item-rules 整理背包與寵物欄（不加 --apply 只列清單；放生要 --confirm-release）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731  只列清單時不停練功、不送封包
BATCH = 5                        # Item_batchDelItem 一次最多幾格
STEP_WAIT = 6.0                  # 每個動作等推送確認的秒數
MAX_EAT = 20                     # 同一格最多吃幾次（疊著的一次只消耗一個，上限保護）
RELEASE_MAX_LV = 30              # 放生規則只丟這個等級以下的（任務送的工具寵）；練過的要使用者指名
                                 # （2026-10-29 角色100 136 級戰寵被規則表放生，LESSONS #103）


def add_args(ap) -> None:
    ap.add_argument("--confirm-release", action="store_true", help="放生規則表裡 release 的寵物（不可逆，要明確加）")
    ap.add_argument("--parallel", action="store_true", help="五隻同時整理（每隻各自停，一隻失敗其他照做完）")
    ap.add_argument("--stash-all", action="store_true",
                    help="規則表沒有處置的道具也存共用倉庫（成人儀式 2999 要背包全空時用；任務條件道具不動）")
    ap.add_argument("--stash-private", action="store_true",
                    help="同 --stash-all，但存個人倉庫（共倉第 1 頁滿了、或綁定道具存不進共倉時用）")


@dataclass
class Plan:
    name: str
    trans: Optional[int] = None
    equip: List[Tuple[int, int]] = field(default_factory=list)          # (itemIndex, itemId)
    delete: List[Tuple[int, int]] = field(default_factory=list)
    store_private: List[Tuple[int, int]] = field(default_factory=list)
    store_public: List[Tuple[int, int]] = field(default_factory=list)
    sell: List[Tuple[int, int]] = field(default_factory=list)
    eat: List[Tuple[int, int]] = field(default_factory=list)
    keep: List[Tuple[int, int]] = field(default_factory=list)
    undecided: List[Tuple[int, int]] = field(default_factory=list)      # 有規則但條件不成立／不知道 → 不動
    release: List[Tuple[int, int]] = field(default_factory=list)        # (petIndex, petId)
    store_pet_private: List[Tuple[int, int]] = field(default_factory=list)
    pet_skipped: List[Tuple[int, int, str]] = field(default_factory=list)

    def item_work(self) -> int:
        return (len(self.equip) + len(self.delete) + len(self.store_private) + len(self.store_public)
                + len(self.eat))


def ctx_of(p, name: str, depot: Optional[Dict[int, int]] = None) -> Ctx:
    with p.lock:
        trans = p.trans.get(name)
        fin = p.finished.get(name)
    return Ctx(trans=trans, finished=frozenset(fin) if fin is not None else None,
               depot=None if depot is None else tuple(sorted(depot.items())))


def make_plan(p, name: str, rules: Rules, depot: Optional[Dict[int, int]] = None,
              stash_all: bool = False, stash_private: bool = False) -> Plan:
    ctx = ctx_of(p, name, depot)
    # 「個倉留 N 件」這種規則（`depot<N`／`depot>=N`）要邊規劃邊算：第一件存進個倉之後，
    # 同一批的第二件看到的個倉件數就是 1，才會掉到下一列（刪除）。
    counting = {r.id for r in rules.rules if r.kind == "item" and any("depot" in c for c in r.cond)}
    running = dict(depot or {})
    plan = Plan(name, trans=ctx.trans)
    with p.lock:
        worn = {it.get("itemId") for it in p.items.get(name, {}).values()
                if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) != 0}
    has_rule = {r.id for r in rules.rules if r.kind == "item"}
    equipping = set()
    for it in sorted(p.bag(name), key=lambda x: x["itemIndex"]):
        idx, iid = it["itemIndex"], it["itemId"]
        use = ctx if iid not in counting or depot is None else ctx_of(p, name, running)
        r = rules.item_rule(iid, use)
        if r is None:
            if iid in has_rule:
                plan.undecided.append((idx, iid))
                continue
            # --stash-all：規則表沒這個道具就整個存**共用倉庫**（使用者 2026-10-29「存共倉」：
            # 五隻共用，料理材料、升級石誰都能拿。成人儀式 2999 要背包 15 格全空，
            # 每日委託剩下的料理、材料、裝備升級石都沒有規則，clean 本來一件都不動）。
            # 存倉是可逆的，不會像刪除那樣弄丟東西；穿在身上的不算在背包裡，不會被碰到。
            # **任務條件道具不存**：2026-10-28 我把英雄島要交的氣力回複藥存進倉庫，任務當場
            # 交不出來（提示 40000000069）。寧可空格不夠停下來，也不要把交付道具收走。
            # --stash-private（2026-11-02 第六組成人儀式）：共倉第 1 頁存滿（isOK False）→ 改存個人倉。
            if (stash_all or stash_private) and iid not in rules.task_items:
                (plan.store_private if stash_private else plan.store_public).append((idx, iid))
            continue
        act = r.action
        if rules.blocks(r):
            raise JobError(f"{name} 第 {idx} 格 {iid} 在保護名單／任務條件裡，規則卻要 {act}（規則表有錯，先修表）")
        if act == "equip":
            if iid in worn or iid in equipping:
                act = "keep"                      # 已經穿著一件（或這次要穿的那件）→ 其餘留著
            else:
                equipping.add(iid)
        if act == "store_private" and iid in counting:
            running[iid] = running.get(iid, 0) + 1
        getattr(plan, act).append((idx, iid))
    fight, ride = p.locks(name)
    with p.lock:
        pets = {k: dict(v) for k, v in p.petinfo.get(name, {}).items()}
    for idx, pet in sorted(pets.items()):
        pid = pet.get("petId") or 0
        if pid <= 0:
            continue
        r = rules.pet_rule(pid, ctx)
        if r is None or r.action == "keep":
            continue
        uniq = pet.get("uniqueIndex")
        if pet.get("status") in (2, 3) or (uniq and uniq in (fight, ride)):
            plan.pet_skipped.append((idx, pid, "戰寵／騎寵不動"))
            continue
        if r.action == "release" and pid in rules.task_pets:
            raise JobError(f"{name} 寵物 {pid} 是任務條件要交的，規則卻要放生（規則表有錯，先修表）")
        if r.action == "release" and (pet.get("level") or 0) > RELEASE_MAX_LV:
            # 2026-10-29 第三組：規則表「烏力斯坦 11 → release」沒有等級條件，卡坦開跑前跑過
            # `pets --mode park`（寵物全存倉、鎖定清 0、狀態變休息）→ 上面那個「戰寵／騎寵不動」
            # 的保護失效，角色100 **136 級的戰寵**就被放生了（不可逆）。
            # 放生規則是為了丟任務送的 1 級工具寵，練過的一律不丟，要丟請使用者指名。
            plan.pet_skipped.append((idx, pid, f"{pet.get('level')} 級，超過放生上限 {RELEASE_MAX_LV}"))
            continue
        getattr(plan, r.action).append((idx, pid))
    return plan


def describe(p, plan: Plan, confirm_release: bool) -> List[str]:
    names = {k: v.get("nameTn") for k, v in p.item_table().items()}

    def items(xs):
        return "、".join(f"{names.get(i, i)}(格{idx})" for idx, i in xs) or "—"

    def pets(xs):
        return "、".join(f"{p.pet_name({'petId': pid})}(欄{idx})" for idx, pid in xs) or "—"
    out = [f"{plan.name}（{plan.trans if plan.trans is not None else '?'} 轉）"]
    for label, xs in (("穿上", plan.equip), ("刪除", plan.delete), ("存個人倉", plan.store_private),
                      ("存共倉", plan.store_public), ("要賣（協定未知，只列出）", plan.sell), ("吃掉", plan.eat), ("留著", plan.keep),
                      ("條件不成立不動", plan.undecided)):
        if xs:
            out.append(f"  {label}：{items(xs)}")
    if plan.store_pet_private:
        out.append(f"  寵物存個人倉：{pets(plan.store_pet_private)}")
    if plan.release:
        out.append(f"  放生：{pets(plan.release)}" + ("" if confirm_release else "（沒加 --confirm-release，不做）"))
    for idx, pid, why in plan.pet_skipped:
        out.append(f"  寵物 {p.pet_name({'petId': pid})}(欄{idx})：{why}")
    if len(out) == 1:
        out.append("  沒有要處理的")
    return out


# ---- 執行（每步等推送確認；傳給 p.wait 的條件只能直接讀欄位，不能呼叫會上鎖的方法，LESSONS 協作 #29）----
def _cell(p, name: str, idx: int) -> dict:
    return p.items.get(name, {}).get(idx) or {}


def _in_bag(it: dict, iid: int) -> bool:
    return it.get("itemId") == iid and it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0


def _verify(p, name: str, idx: int, iid: int, what: str) -> None:
    with p.lock:
        it = dict(_cell(p, name, idx))
    if not _in_bag(it, iid):
        raise JobError(f"{name} {what}前核對第 {idx} 格不符：要 {iid}，實際 {it.get('itemId')}"
                       f"（數量 {it.get('overlay')}、itemStorge {it.get('itemStorge')}），不送")


def _snapshot(p, name: str, skip) -> Dict[int, Tuple[int, int]]:
    with p.lock:
        return {k: (v.get("itemId"), v.get("overlay", 1)) for k, v in p.items.get(name, {}).items()
                if k not in skip and v.get("overlay", 1) > 0 and v.get("itemId")}


def do_equip(p, name: str, idx: int, iid: int) -> None:
    _verify(p, name, idx, iid, "穿上")
    r = p.clients[name].request("ItemHandle_itemEquipOn", {"itemIndex": idx}) or {}
    ok = bool(r.get("isOK")) and p.wait(lambda: any(
        v.get("itemId") == iid and v.get("overlay", 1) > 0 and v.get("itemStorge", 0) != 0
        for v in p.items[name].values()), STEP_WAIT)
    if not ok:
        raise JobError(f"{name} 穿上 {iid}（第 {idx} 格）沒確認：{r}")
    p.log(f"{name} 穿上 {iid}（第 {idx} 格）")


def do_delete(p, name: str, cells: List[Tuple[int, int]]) -> None:
    cells = sorted(cells)
    for i in range(0, len(cells), BATCH):
        chunk = cells[i:i + BATCH]
        for idx, iid in chunk:
            _verify(p, name, idx, iid, "刪除")
        idxs = [idx for idx, _ in chunk]
        before = _snapshot(p, name, set(idxs))
        r = p.clients[name].request("Item_batchDelItem", {"itemIndexList": idxs}) or {}
        gone = p.wait(lambda: all(not _in_bag(_cell(p, name, idx), iid) for idx, iid in chunk), STEP_WAIT)
        after = _snapshot(p, name, set(idxs))
        if not r.get("isOK") or not gone:
            raise JobError(f"{name} 刪除第 {idxs} 格沒確認（回應 {r}、格子消失 {gone}），停止")
        if after != before:
            raise JobError(f"{name} 刪除第 {idxs} 格後其他格子變了：{before} → {after}，停止")
        p.log(f"{name} 刪除 {[iid for _, iid in chunk]}（第 {idxs} 格），已確認")


def do_eat(p, name: str, idx: int, iid: int) -> None:
    """吃掉（經驗果這種「拿到就該用掉」的道具）。`Item_ItemUse` 伺服器不回 response（只能 send），
    結果看 `Item_ItemInfo` 數量變少。一次只消耗一個，所以疊著的要一顆一顆吃到那格空掉。"""
    _verify(p, name, idx, iid, "吃掉")
    script = (p.item_table().get(iid, {}).get("scripts") or [0])[0]
    for _ in range(MAX_EAT):
        with p.lock:
            before = dict(_cell(p, name, idx)).get("overlay", 1)
        if before <= 0:
            break
        p.clients[name].send("Item_ItemUse", {"itemIndex": idx, "itemNum": 1, "itemScriptId": script,
                                              "data": [], "useMuch": False})
        if not p.wait(lambda: p.items[name].get(idx, {}).get("overlay", 1) < before, STEP_WAIT):
            raise JobError(f"{name} 吃 {iid}（第 {idx} 格）沒看到數量變少（剩 {before} 個），停止")
    with p.lock:
        left = dict(_cell(p, name, idx)).get("overlay", 0)
    if left > 0:
        raise JobError(f"{name} 吃 {iid}（第 {idx} 格）吃了 {MAX_EAT} 次還剩 {left} 個，停止")
    p.log(f"{name} 吃掉 {iid}（第 {idx} 格）")


def do_store(p, name: str, idx: int, iid: int, public: bool) -> None:
    where = "公共" if public else "個人"
    _verify(p, name, idx, iid, f"存{where}倉庫")
    r = p.clients[name].request("ItemHandle_SaveItemToNewDepot",
                                {"page": 1, "isPublic": public, "itemIndex": idx}) or {}
    ok = bool(r.get("isOK")) and p.wait(lambda: not _in_bag(_cell(p, name, idx), iid), STEP_WAIT)
    if not ok:
        raise JobError(f"{name} {iid}（第 {idx} 格）存{where}倉庫沒確認：{r}")
    p.log(f"{name} {iid}（第 {idx} 格）存進{where}倉庫")


def stash_public(p, name: str, ids) -> int:
    """背包裡這些道具全存共倉（使用者 2026-11-02「積分、每日獎勵存共倉」）。存不進去只記日誌，不影響主流程。"""
    n = 0
    for it in sorted(p.bag(name), key=lambda x: x["itemIndex"]):
        if it.get("itemId") not in ids:
            continue
        try:
            do_store(p, name, it["itemIndex"], it["itemId"], public=True)
            n += 1
        except JobError as e:
            p.log(f"{name} 存共倉失敗（不影響主流程）：{e}")
    return n


def _pet_check(p, name: str, idx: int, pid: int, what: str) -> None:
    fight, ride = p.locks(name)
    with p.lock:
        pet = dict(p.petinfo.get(name, {}).get(idx) or {})
    if pet.get("petId") != pid:
        raise JobError(f"{name} {what}前核對第 {idx} 欄不符：要寵物 {pid}，實際 {pet.get('petId')}，不送")
    if pet.get("status") in (2, 3) or (pet.get("uniqueIndex") and pet.get("uniqueIndex") in (fight, ride)):
        raise JobError(f"{name} 第 {idx} 欄寵物 {pid} 是戰寵／騎寵，不{what}")


def _pet_gone(p, name: str, idx: int) -> bool:
    return ((p.petinfo.get(name, {}).get(idx) or {}).get("petId") or -1) <= 0


def do_store_pet(p, name: str, idx: int, pid: int) -> None:
    """存寵物倉庫第 1 頁：個倉滿了改存共倉、兩邊都滿就停（同 pets.store_pet，LESSONS #89／#93）。
    2026-10-26 卡坦 2019 第 2 步給金飛後整理：角色72個倉 20/20，這裡寫死存個倉 → isOK false 停住。"""
    _pet_check(p, name, idx, pid, "存倉")
    public = p.pet_depot_target(name)
    if public is None:
        raise JobError(f"{name} 寵物 {pid}（第 {idx} 欄）存不進去：個倉、共倉第 1 頁都滿了（第 2 頁要鑽石解鎖，不碰）")
    where = "共倉" if public else "個人寵物倉庫"
    r = p.clients[name].request("NewPet_SavePetToNewDepot", {"page": 1, "isPublic": public, "petIndex": idx}) or {}
    if not r.get("isOK") or not p.wait(lambda: _pet_gone(p, name, idx), STEP_WAIT):
        raise JobError(f"{name} 寵物 {pid}（第 {idx} 欄）存{where}沒確認：{r}")
    p.log(f"{name} 寵物 {pid}（第 {idx} 欄）存進{where}")


def do_release(p, name: str, idx: int, pid: int) -> None:
    _pet_check(p, name, idx, pid, "放生")
    p.clients[name].send("Pet_DropPet", {"petIndex": idx})       # 伺服器只推該格 petId -1（2026-10-23 錄檔）
    if not p.wait(lambda: _pet_gone(p, name, idx), STEP_WAIT):
        raise JobError(f"{name} 放生寵物 {pid}（第 {idx} 欄）沒確認")
    p.log(f"{name} 放生寵物 {pid}（第 {idx} 欄）")


def apply_plan(p, plan: Plan, confirm_release: bool = False, items_only: bool = False) -> int:
    """照清單執行，回傳做了幾件。任何一步沒確認就丟 JobError（後面的不做）。"""
    n, name = 0, plan.name
    for idx, iid in plan.equip:
        do_equip(p, name, idx, iid); n += 1
    if plan.delete:
        do_delete(p, name, plan.delete); n += len(plan.delete)
    for idx, iid in plan.eat:
        do_eat(p, name, idx, iid); n += 1
    for idx, iid in plan.store_private:
        do_store(p, name, idx, iid, public=False); n += 1
    for idx, iid in plan.store_public:
        do_store(p, name, idx, iid, public=True); n += 1
    if items_only:
        return n
    for idx, pid in plan.store_pet_private:
        do_store_pet(p, name, idx, pid); n += 1
    if confirm_release:
        for idx, pid in plan.release:
            do_release(p, name, idx, pid); n += 1
    return n


def depot_counts(p, rules: Rules) -> Optional[Dict[str, Dict[int, int]]]:
    """規則表有用到 `depot` 條件時，查一次每個人的個人倉庫件數；沒有就不查（不多送請求）。

    查失敗回 None——代表「不知道」，用到 depot 的條件就不成立、那些道具不動，不會誤刪。
    """
    need = {r.id for r in rules.rules if r.kind == "item" and any("depot" in c for c in r.cond)}
    if not need:
        return None
    in_bags = {it["itemId"] for _, name in p.party for it in p.bag(name)}
    if not (need & in_bags):
        return None                                          # 背包裡沒有這些道具 → 不用查倉庫
    from ..party_api import DepotQuery                       # 延後 import：避免循環相依
    code, body = DepotQuery(p).query("depot")
    if code != 200:
        p.log(f"查個人倉庫失敗（{body.get('error')}）：用到個倉件數的規則這次全部不動")
        return None
    out: Dict[str, Dict[int, int]] = {}
    for name, rows in (body.get("personal") or {}).items():
        c: Dict[int, int] = {}
        for row in rows:
            c[row["itemId"]] = c.get(row["itemId"], 0) + int(row.get("count") or 1)
        out[name] = c
    return out


def clean_all(p, rules: Optional[Rules] = None, apply: bool = False, confirm_release: bool = False,
              items_only: bool = False, depot: Optional[Dict[str, Dict[int, int]]] = None,
              parallel: bool = False, stash_all: bool = False, stash_private: bool = False) -> Tuple[str, dict]:
    rules = rules or load()
    if apply and getattr(p, "finished", None) is not None:
        from .quests import drop_finished_tokens       # 已完成任務的交件道具先刪（規則表沒寫、會塞滿背包）
        drop_finished_tokens(p)
    if depot is None:
        depot = depot_counts(p, rules)
    plans = [make_plan(p, name, rules, (depot or {}).get(name) if depot is not None else None,
                       stash_all=stash_all, stash_private=stash_private)
             for _, name in p.party]
    for plan in plans:
        for line in describe(p, plan, confirm_release and not items_only):
            p.log(line)
    if not apply:
        return "只列清單（沒加 --apply，沒有送任何動作）", {"plans": plans}
    if parallel:
        done = apply_parallel(p, plans, confirm_release, items_only)
    else:
        done = sum(apply_plan(p, plan, confirm_release, items_only) for plan in plans)
    return f"完成 {done} 件", {"plans": plans, "done": done}


def apply_parallel(p, plans: List[Plan], confirm_release: bool = False, items_only: bool = False) -> int:
    """五隻同時照清單整理（每隻自己的連線，一隻一條執行緒；請求間隔照樣由各自的 GameClient 強制）。

    使用者 2026-10-25「1.82 四大 英雄島 所有任務執行前 先平行快速整理一次」。
    出錯就停的範圍是**每隻角色各自**（同 catch）：某一隻沒確認就停那一隻、其他照做完，
    全部結束後只要有一隻失敗就丟 JobError，列出每隻的原因（任務不會在背包沒整理好的狀態下開始）。
    """
    import threading
    done: Dict[str, int] = {}
    errors: Dict[str, str] = {}

    def work(plan: Plan) -> None:
        try:
            done[plan.name] = apply_plan(p, plan, confirm_release, items_only)
        except Exception as e:  # noqa: BLE001  每隻各自記，不讓一隻的錯中斷其他隻
            errors[plan.name] = f"{type(e).__name__}: {e}" if not isinstance(e, JobError) else str(e)
    threads = [threading.Thread(target=work, args=(plan,), daemon=True) for plan in plans if plan.item_work()
               or (not items_only and (plan.store_pet_private or (confirm_release and plan.release)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise JobError("平行整理有角色沒完成：" + "；".join(f"{n}：{e}" for n, e in errors.items()))
    return sum(done.values())


def run(p, args) -> Tuple[str, dict]:
    return clean_all(p, apply=bool(getattr(args, "apply", False)),
                     confirm_release=bool(getattr(args, "confirm_release", False)),
                     parallel=bool(getattr(args, "parallel", False)),
                     stash_all=bool(getattr(args, "stash_all", False)),
                     stash_private=bool(getattr(args, "stash_private", False)))
