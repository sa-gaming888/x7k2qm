"""petex：寵物合成 2D人龍自選蛋（配方 4428，使用者 2026-11-13「盤點一下所有人物，可以做幾隻2D 人龍」「先合掉，然後恢復練功」）。
**不可逆**：五隻寵物合成後就沒了，要加 --confirm-exchange。

    python -X utf8 -m x7k2qm.autoplay.jobs petex --profile 7 --discover --who 角色54 --uids u1,u2,u3,u4,u5            # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs petex --profile 7 --discover --who 角色54 --uids u1,u2,u3,u4,u5 --apply --confirm-exchange

配方（data/configs/ExchangeItem.json 4428）：利則諾頓 54、揚奇洛斯 55、邦浦洛斯 56、邦奇諾 57、布魯頓 481 各 1 隻
（伺服器評分 score ≥ petPoint 1000）＋ 2D人龍進化石 93022 ×1 → 2D人龍自選蛋 92159。

協定（來源：參考專案 參考專案 `ops/petexchange.py`，官方錄檔 psa_20261024_232726、psa_20261028_011207，A 級）：
- 先送 `Role_NomalHandle{type 6, extra 0}`（開兌換介面；同龜殼兌換，只送不等）。
- `Item_NewExchangeItem{ID 4428, petIndexList [{petIndex}, …]}`：petIndex 是**身上寵物欄格號**（不是唯一編號），
  順序＝配方 requireInfos 的寵物順序（54 → 55 → 56 → 57 → 481）。進化石不放在請求裡，伺服器自己從背包扣。
- 成功回 `{result 1, ID}`，之前推 Pet_PetInfo（那五格 petId -1）、Item_ItemInfo（進化石 −1、蛋 +1）。
- result 3＝有一隻評分不夠，伺服器什麼都沒扣（錄檔 10-28）；本工作送前就擋。

流程（任何一步沒確認就停，JobError）：
1. 送任何東西之前全部核對：五個唯一編號找得到（合成的人身上、他的個倉、共倉、同組別人的個倉）、
   物種剛好是配方五種各 1、評分夠、背包有進化石、要搬的那幾隻放得下（別人身上要有空格、共倉第 1 頁有空位）。
2. 別人個倉的：那個人領到身上 → 存共倉；合成的人再從共倉領（同帳號搬寵物走共倉）。
3. 合成的人：記下戰寵／騎寵鎖定 → 解鎖 → 身上不是這五隻的全部存倉（個倉滿了改共倉）→ 五隻領到身上。
4. 開兌換介面 → 合成 → 核對五隻都不在身上、進化石 −1、蛋 +1。
5. **不管成功失敗都還原**：沒合成掉的五隻存回倉庫，原本身上的寵物領回來、戰寵／騎寵鎖回原本的唯一編號。

送出合成之後回應不對／核對不符而且寵物或道具有變化 → JobHold（可能已經合成了，**絕對不能重送**，停在原地等人看）。
蛋留在背包，不自動開（要開用 `egg-pick`）。
"""

import json
import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from . import JobError, JobHold
from . import pets as P

NAME = "petex"
HELP = "寵物合成 2D人龍自選蛋（配方 4428；--who 合成的人、--uids 五隻唯一編號；不加 --apply 只列，--apply 還要 --confirm-exchange，不可逆）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

RECIPE = 4428                      # 預設：2D人龍自選蛋（有官方錄檔）
# 開放的配方（產物名稱）。4427 2D老虎自選蛋：金虎系 307、308、309、310 各 1 隻評分 ≥1000 ＋ 2D老虎進化石 93021，
# 跟 4428 同一個表、同一個協定（使用者 2026-11-14「老虎也合成」）。
RECIPES = {4428: "2D人龍自選蛋", 4427: "2D老虎自選蛋"}
OPEN_UI = {"type": 6, "extra": 0}  # Role_NomalHandle：開兌換介面
WAIT = 8.0


def add_args(ap) -> None:
    ap.add_argument("--who", required=True, help="合成的人（五隻寵物會領到他身上、進化石要在他背包）")
    ap.add_argument("--uids", required=True, help="配方要的寵物唯一編號，逗號分隔（每種各 1 隻，不自動挑；人龍 5 隻、老虎 4 隻）")
    ap.add_argument("--recipe", type=int, default=RECIPE, choices=sorted(RECIPES),
                    help="配方：4428 2D人龍自選蛋（預設）、4427 2D老虎自選蛋")
    ap.add_argument("--confirm-exchange", dest="confirm_exchange", action="store_true",
                    help="合成不可逆（五隻寵物會消失），要明確加")


@lru_cache(maxsize=None)
def recipe(rid: int = RECIPE) -> dict:
    """ExchangeItem 表那一筆 → {pets [(petId, petPoint)]（配方順序）, items [(itemId, num)], target}。
    只收「每種寵物 1 隻＋道具」的形狀（有幣種或其他欄位就拒絕：這個工作不花錢）。"""
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "ExchangeItem.json"), encoding="utf-8") as fp:
        row = next((r for r in json.load(fp) if r.get("ID") == rid), None)
    if row is None:
        raise JobError(f"ExchangeItem 表沒有配方 {rid}")
    if row.get("targetType") != 1:
        raise JobError(f"配方 {rid} 的產物不是道具（targetType {row.get('targetType')}），不支援")
    pets, items = [], []
    for q in row.get("requireInfos") or []:
        if q.get("petId"):
            if q.get("petNum") != 1:
                raise JobError(f"配方 {rid} 要 {q.get('petNum')} 隻 {q['petId']}，只支援每種 1 隻")
            pets.append((int(q["petId"]), int(q.get("petPoint") or 0)))
        elif q.get("itemId"):
            items.append((int(q["itemId"]), int(q.get("itemNum") or 1)))
        else:
            raise JobError(f"配方 {rid} 有看不懂的需求（可能要花錢）：{q}")
    return {"id": rid, "pets": tuple(pets), "items": tuple(items), "target": int(row["targetId"])}


def parse_uids(s) -> List[int]:
    try:
        out = [int(x) for x in str(s).replace("，", ",").split(",") if x.strip()]
    except ValueError:
        raise JobError(f"--uids 要是唯一編號用逗號分隔：{s}") from None
    if len(out) != len(set(out)):
        raise JobError(f"--uids 有重複：{s}")
    return out


def bag_count(p, name: str, iid: int) -> int:
    return sum(int(it.get("overlay", 1) or 0) for it in p.bag(name) if it.get("itemId") == iid)


def body_uids(p, name: str) -> Dict[int, int]:
    """身上：唯一編號 → 格號。"""
    return {v.get("uniqueIndex"): k for k, v in P.slots(p, name).items()}


def locate(p, who: str, uids: List[int]) -> Dict[int, dict]:
    """每個唯一編號在哪：{uid: {where 身上|倉庫, owner 誰的（共倉＝who）, row 寵物資料}}。找不到就不放。
    找的順序：合成的人身上 → 他的個倉＋共倉 → 同組其他人的個倉。"""
    out: Dict[int, dict] = {}
    for k, v in P.slots(p, who).items():
        if v.get("uniqueIndex") in uids:
            out[v["uniqueIndex"]] = {"where": "身上", "owner": who, "row": v}
    left = [u for u in uids if u not in out]
    if left:
        for r in p.pet_depots(who):
            if r.get("uniqueIndex") in left:
                out[r["uniqueIndex"]] = {"where": "共倉" if r.get("isPublic") else "個倉", "owner": who, "row": r}
    for _, n in p.party:
        left = [u for u in uids if u not in out]
        if not left or n == who:
            continue
        for r in p.pet_depot(n):
            if r.get("uniqueIndex") in left:
                out[r["uniqueIndex"]] = {"where": "個倉", "owner": n, "row": r}
        for v in P.slots(p, n).values():
            if v.get("uniqueIndex") in left:
                raise JobError(f"{n} 身上有寵物 {v['uniqueIndex']}（要先存倉；身上的可能是鎖定的戰寵／騎寵，不替使用者動）")
    return out


def plan(p, who: str, uids: List[int], rec: dict) -> Tuple[Dict[int, dict], List[str]]:
    """全部核對（零動作封包）。回傳 (位置, 計畫說明)；不符丟 JobError 並列出所有問題。"""
    from ..headless_party import PET_DEPOT_NUM
    want = dict(rec["pets"])
    if len(uids) != len(want):
        raise JobError(f"配方 {rec['id']} 要 {len(want)} 隻（每種 1 隻），--uids 給了 {len(uids)} 個")
    where = locate(p, who, uids)
    bad, seen = [], {}
    for u in uids:
        x = where.get(u)
        if x is None:
            bad.append(f"找不到寵物 {u}（{who} 身上、個倉、共倉、同組別人個倉都沒有）")
            continue
        r = x["row"]
        pid, sc = r.get("petId"), r.get("score")
        if pid not in want:
            bad.append(f"寵物 {u}（{p.pet_name(r)}，{pid}）不是配方要的物種 {sorted(want)}")
        elif pid in seen:
            bad.append(f"寵物 {u} 跟 {seen[pid]} 是同一種（{pid}），每種只要 1 隻")
        else:
            seen[pid] = u
            if not isinstance(sc, int):
                bad.append(f"寵物 {u}（{p.pet_name(r)}）沒有評分，核對不了 ≥ {want[pid]}")
            elif sc < want[pid]:
                bad.append(f"寵物 {u}（{p.pet_name(r)}）評分 {sc} < {want[pid]}")
        if r.get("petLock") == 1:
            bad.append(f"寵物 {u}（{p.pet_name(r)}）上鎖了")
    for pid in want:
        if pid not in seen and not any(str(pid) in b for b in bad):
            bad.append(f"缺寵物 {pid}")
    for iid, n in rec["items"]:
        have = bag_count(p, who, iid)
        if have < n:
            bad.append(f"{who} 背包道具 {iid} 只有 {have} 個，要 {n} 個（在倉庫先 carry 取出）")
    movers = [u for u in uids if u in where and where[u]["owner"] != who]
    for n in {where[u]["owner"] for u in movers}:
        k = sum(1 for u in movers if where[u]["owner"] == n)
        if len(P.slots(p, n)) + 1 > P.SLOT_MAX:
            bad.append(f"{n} 身上 {len(P.slots(p, n))} 格滿了，領不出要轉給 {who} 的 {k} 隻")
    if movers and len(p.pet_depot(who, public=True)) + 1 > PET_DEPOT_NUM:
        bad.append(f"共倉第 1 頁滿了，轉不了別人個倉的 {len(movers)} 隻（第 2 頁要鑽石，不碰）")
    if bad:
        raise JobError(f"{who} 合成配方 {rec['id']} 不能做：" + "；".join(bad))
    lines = []
    for u in uids:
        x = where[u]
        r = x["row"]
        src = x["where"] if x["owner"] == who else f"{x['owner']}的個倉（經共倉）"
        lines.append(f"{p.pet_name(r)}（{r.get('petId')}）Lv{r.get('level')} 評分 {r.get('score')} uid {u}：{src}")
    return where, lines


def bring(p, who: str, uids: List[int], where: Dict[int, dict]) -> List[int]:
    """把五隻弄到 who 身上，回傳為了騰位置存進倉庫的原本寵物（唯一編號）。"""
    for u in uids:                                   # 1. 別人個倉的：那個人領出 → 存共倉
        x = where[u]
        if x["owner"] == who:
            continue
        P.take_pet(p, x["owner"], x["row"])
        P.to_public(p, x["owner"], u)
        p.log(f"合成：{x['owner']} 的寵物 {u} 轉到共倉給 {who}")
    P.unlock(p, who, 0, 0)                            # 2. 鎖著的寵物不能改狀態／存倉
    stored = []
    for k, v in sorted(P.slots(p, who).items()):     # 3. 身上不是這五隻的全部存倉
        if v.get("uniqueIndex") in uids:
            continue
        P.store_pet(p, who, k)
        stored.append(v["uniqueIndex"])
    have = body_uids(p, who)
    rows = {r.get("uniqueIndex"): r for r in p.pet_depots(who)} if any(u not in have for u in uids) else {}
    for u in uids:                                   # 4. 五隻領到身上
        if u in have:
            continue
        if u not in rows:
            raise JobError(f"{who} 的個倉、共倉找不到寵物 {u}，領不到身上")
        P.take_pet(p, who, rows[u])
    for k, v in P.slots(p, who).items():             # 出戰／騎乘的改休息（領回來的本來就是休息）
        if v.get("uniqueIndex") in uids:
            P.rest_pet(p, who, k)
    return stored


def restore(p, who: str, uids: List[int], stored: List[int], locks: Tuple[int, int]) -> List[str]:
    """還原：沒合成掉的五隻存回倉庫 → 原本的寵物領回 → 戰寵／騎寵鎖回原本的。每一步各自做，回傳失敗訊息。"""
    errs = []
    for k, v in sorted(P.slots(p, who).items()):
        if v.get("uniqueIndex") in uids:
            try:
                P.store_pet(p, who, k)
            except JobError as e:
                errs.append(str(e))
    have = body_uids(p, who)
    need = [u for u in stored if u not in have]
    rows = {r.get("uniqueIndex"): r for r in p.pet_depots(who)} if need else {}
    for u in need:
        try:
            if u not in rows:
                raise JobError(f"{who} 原本的寵物 {u} 在個倉、共倉都找不到")
            P.take_pet(p, who, rows[u])
        except JobError as e:
            errs.append(str(e))
    fight, ride = locks
    have = body_uids(p, who)
    fight, ride = (fight if fight in have else 0), (ride if ride in have else 0)
    if fight or ride:
        try:
            P.lock(p, who, fight, ride)
        except JobError as e:
            errs.append(str(e))
    return errs


def exchange(p, who: str, uids: List[int], rec: dict) -> str:
    """開兌換介面 → 合成 → 核對。被拒而且什麼都沒變 → JobError；送出後有變化但對不上 → JobHold。"""
    idx = body_uids(p, who)
    by_pid = {P.slots(p, who)[idx[u]]["petId"]: idx[u] for u in uids}
    order = [by_pid[pid] for pid, _ in rec["pets"]]
    data = {"ID": rec["id"], "petIndexList": [{"petIndex": i} for i in order]}
    s0 ={iid: bag_count(p, who, iid) for iid, _ in rec["items"]}
    e0 = bag_count(p, who, rec["target"])
    p.log(f"合成：{who} 開兌換介面、送配方 {rec['id']} 格序 {order}")
    p.clients[who].send("Role_NomalHandle", dict(OPEN_UI))
    p.nap(1)
    try:
        r = p.clients[who].request("Item_NewExchangeItem", data) or {}
    except Exception as e:  # noqa: BLE001 — 送出後沒回應：可能已經合成，不能重送
        raise JobHold(f"{who} 合成 {rec['id']} 送出後沒收到回應（{type(e).__name__}: {e}），停在原地，不要重送") from None

    def gone() -> bool:
        return not any(u in body_uids(p, who) for u in uids)

    def done() -> bool:
        return (gone() and all(bag_count(p, who, iid) == s0[iid] - n for iid, n in rec["items"])
                and bag_count(p, who, rec["target"]) == e0 + 1)

    def changed() -> bool:
        return (any(u not in body_uids(p, who) for u in uids) or bag_count(p, who, rec["target"]) != e0
                or any(bag_count(p, who, iid) != s0[iid] for iid in s0))
    if r.get("result") != 1:
        p.wait(changed, 2)
        if changed():
            raise JobHold(f"{who} 合成 {rec['id']} 回 {r}，但寵物或道具有變化，停在原地，不要重送")
        raise JobError(f"{who} 合成 {rec['id']} 被拒（{r}；result 3＝有一隻評分不夠），寵物和道具都沒動")
    if not p.wait(done, WAIT):
        left = [u for u in uids if u in body_uids(p, who)]
        raise JobHold(f"{who} 合成 {rec['id']} 回 result 1，但 {int(WAIT)} 秒內核對不符（身上還有 {left}、"
                      f"進化石 {s0} → {[bag_count(p, who, i) for i in s0]}、蛋 {e0} → {bag_count(p, who, rec['target'])}），"
                      f"停在原地，不要重送")
    return f"{who} 合成 {RECIPES.get(rec['id'], rec['id'])}成功：{len(uids)} 隻消失、進化石 −1、蛋 {e0} → {e0 + 1}"


def run(p, args) -> Tuple[str, dict]:
    who = str(args.who)
    if who not in [n for _, n in p.party]:
        raise JobError(f"--who {who} 不在這組")
    uids = parse_uids(args.uids)
    rid = int(getattr(args, "recipe", RECIPE) or RECIPE)
    if rid not in RECIPES:
        raise JobError(f"配方 {rid} 沒開放（只開放 {sorted(RECIPES)}）")
    rec = recipe(rid)
    where, lines = plan(p, who, uids, rec)
    for x in lines:
        p.log(f"合成計畫：{x}")
    data = {"who": who, "uids": uids, "recipe": rec["id"], "plan": lines}
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply，沒有送任何動作）：{who} 合成配方 {rec['id']}，{len(uids)} 隻都核對過、背包有進化石", data
    if not getattr(args, "confirm_exchange", False):
        raise JobError("合成不可逆（五隻寵物會消失），要加 --confirm-exchange；沒有送任何東西")
    locks = p.locks(who)
    stored: List[int] = []
    msg, err = None, None
    try:
        stored = bring(p, who, uids, where)
        msg = exchange(p, who, uids, rec)
    except JobError as e:                            # JobHold 也是 JobError：一樣先還原原本身上的寵物
        err = e
    errs = restore(p, who, uids, stored, locks)
    tail = f"；還原沒做完：{'；'.join(errs)}" if errs else f"；原本身上的 {len(stored)} 隻領回、戰寵／騎寵鎖回原本的"
    if err is not None:
        raise type(err)(f"{err}{tail}")
    if errs:
        raise JobHold(f"{msg}{tail}（寵物在倉庫，人看過再處理）")
    p.log(f"合成：{msg}{tail}")
    return msg + tail, dict(data, stored=stored)
