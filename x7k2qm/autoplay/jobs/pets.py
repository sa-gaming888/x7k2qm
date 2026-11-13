"""pets：寵物欄整理（取代暫存資料夾的 kartan_pets.py／store_all_pets.py／swap_tiger.py／pet_depot.py）。

    python -X utf8 -m x7k2qm.autoplay.jobs pets --profile 2 --discover                          # 列寵物欄＋個人寵物倉庫
    python -X utf8 -m x7k2qm.autoplay.jobs pets --profile 2 --discover --apply --mode park       # 卡坦前：金虎存倉
    ... --apply --mode rest          # 交寵物前：鎖定清 0、全部改休息
    ... --apply --mode restore       # 卡坦後：金虎（不高於人物等級裡最高的）領回、鎖 戰寵＝主戰寵／騎寵＝金虎
    ... --apply --mode store-all     # 全部寵物存個人寵物倉庫（轉生打完機暴後；rebirth 也呼叫這裡）
    ... --apply --mode swap-tiger --who 角色52,角色26   # 轉生後騎寵換成 1 級金虎
    ... --apply --mode take --pet-id 1381                 # 指定的寵物（身上沒有就從個倉領）鎖成戰寵，騎寵照舊
    ... --apply --mode release-public --pet-id 355 --confirm-release   # 共倉裡規則表是 release 的 1 級寵物逐隻領出放生
    ... --apply --mode release-private --pet-id 329 --confirm-release  # 同上，每隻角色放自己個倉的

不加 `--apply`（或 `--mode list`）：只列，不停練功、不送任何改動。

每一步都看封包確認，沒確認就停（規範第 2 點，丟 JobError，後面的角色也不做）：
- 外掛寵物鎖定 `WaiGua_NomalSettingHandlePet{fightPetIndex, ridePetIndex}` 填**寵物唯一編號**；
  **鎖著的寵物不能改狀態／存倉**，所以一律先改鎖定再動寵物。
- 出戰（2）／騎乘（3）的要先改休息（1）才存得進倉庫：`NewPet_NewPetChangeStatus` **內層不帶 session、
  沒有 response → 只能 send**（用 request 會逾時），結果看推回的 `Pet_PetInfo.status`。
- 存倉 `NewPet_SavePetToNewDepot{page:1, isPublic, petIndex}`：回 isOK 且那欄 petId ≤ 0。
  **個倉第 1 頁滿了改存共倉第 1 頁**（使用者 2026-10-26「改存共倉」，LESSONS #89）；兩邊都滿就停（第 2 頁要鑽石，不碰）。
- 領回 `NewPet_GetPetFromNewDepot{page:1, isPublic, uniqueIndex}`：回 isOK 且寵物欄多一隻那個 petId；
  isPublic 照倉庫那筆是哪個倉庫來的（`Party.pet_depots` 個倉＋共倉一起找）。

restore 與 swap-tiger 的差別（兩個都是「金虎領回當騎寵」，挑的金虎不同）：
- `restore`：卡坦的願望（2019）前後用 → 領**等級不高於人物**的金虎裡最高的那隻（騎不上的不拿），戰寵＝主戰寵（目前鎖定的，沒有就身上最高等、不是金虎的）。
- `swap-tiger`：轉生後用，人物回到 1 級騎不上高等坐騎 → 身上的高等金虎存倉、領 **1 級**金虎，戰寵沿用原本的。
  2026-10-24 第二組：init_after_trans 給角色52、角色26領回 128 級金虎，狀態直接變休息（騎不上）。
"""

import json
import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from . import JobError

NAME = "pets"
HELP = ("寵物欄整理（--mode list|grade|park|rest|restore|store-all|swap-tiger|refresh|store|release-public|"
        "release-private；不加 --apply 只列；grade 只讀：每隻寵物滿檔／缺 n 檔；info 只讀：每隻完整資訊）")
MODES = ("list", "grade", "info", "park", "rest", "restore", "store-all", "swap-tiger", "ride-train", "refresh", "equip-fight", "take", "withdraw", "deposit", "store", "release-worst", "release-public",
         "release-private", "release-below", "dragons", "share", "graduate", "ride", "retire")
GRADUATE_SCORE = 1000                  # 使用者 11-05：分數滿 1000 的寵物改名成分數、存倉、換寵
FIVE_DRAGONS = (54, 55, 56, 57, 481)   # 五色人龍（利則諾頓、揚奇洛斯、邦浦洛斯、邦奇諾、布魯頓）
KEEP_DIFF = 3                          # 使用者 2026-11-01：五色人龍留 +3 檔以上練，練到有機會 1000 分
READ_ONLY = ("list", "grade", "info")
STATUS = {2: "出戰", 3: "騎乘"}          # 其他值＝休息（同 party_api.STATUS_NAMES）
PAUSE = lambda a: bool(getattr(a, "apply", False)) and getattr(a, "mode", "list") not in READ_ONLY   # noqa: E731

TIGER, DRAGON = 310, 1589        # 金格薩貝魯（金虎）、人龍
KATAN_PETS = (319, 320, 321, 322, 346, 347, 348, 349)   # 卡坦的願望 2019 要練的 4 色鯊魚、4 色雞
REST, FIGHT, RIDE = 1, 2, 3      # Pet_PetInfo.status
SLOT_MAX = 5


def add_args(ap) -> None:
    ap.add_argument("--mode", default="list", choices=MODES, help="要做哪一種整理（預設 list，只列）")
    ap.add_argument("--who", default=None, help="只做這些角色（名字逗號分隔；預設全隊）")
    ap.add_argument("--slot", type=int, default=None, help="store 模式：存哪一格（寵物欄 1～5）")
    ap.add_argument("--ride-pet-id", type=int, default=None,
                    help="take 模式：連騎寵也指定這個 petId（身上沒有就從個倉領；跟戰寵同一隻會停）")
    ap.add_argument("--pet-id", type=int, default=None,
                    help="store 模式：那一格必須是這個 petId（核對用，不符就停）；take：要鎖成戰寵的 petId；"
                         "release-public／release-private：要放生的 petId")
    ap.add_argument("--any-level", dest="any_level", action="store_true",
                    help="take：滿級（140）的寵物也拿（預設只拿未滿 140）")
    ap.add_argument("--pet-uid", dest="pet_uid", default=None, help="share、withdraw：寵物唯一編號（withdraw 同種多隻時指定）")
    ap.add_argument("--pet-name", dest="pet_name", default=None, help="share：改成這個名字")
    ap.add_argument("--public", action="store_true",
                    help="deposit：改存共倉（共倉放不下就停，不改存個倉；使用者 2026-11-05）")
    ap.add_argument("--min-score", dest="min_score", type=int, default=GRADUATE_SCORE,
                    help=f"graduate：評分到這個分數就改名存倉（預設 {GRADUATE_SCORE}）")
    ap.add_argument("--confirm-release", action="store_true",
                    help="release-public／release-private 要明確加（放生不可逆）")
    ap.add_argument("--exception", default=None,
                    help="release-public／release-private 單次破例：填使用者原話，不看規則表的 release（規則表不改）；"
                         "1 級、任務條件寵物、保護名單照樣檢查")
    ap.add_argument("--count", type=int, default=None,
                    help="release-public／release-private 最多放生幾隻（每個倉庫各算；不給＝全部）。使用者選「只丟 1 隻」時給 1")


def check(args) -> None:
    mode = getattr(args, "mode", "list") or "list"
    if mode not in MODES:
        raise JobError(f"--mode 只能是 {'、'.join(MODES)}：{mode}")
    if mode == "store" and (not getattr(args, "slot", None) or not getattr(args, "pet_id", None)
                            or not getattr(args, "who", None)):
        raise JobError("store 模式要給 --who、--slot、--pet-id（三個都核對過才存）")
    if mode == "take" and not getattr(args, "pet_id", None):
        raise JobError("take 模式要給 --pet-id（要鎖成戰寵的寵物）")
    if mode == "ride" and (not getattr(args, "pet_uid", None) or not getattr(args, "who", None)):
        raise JobError("ride 模式要給 --who 和 --pet-uid（要當騎寵的那隻）")
    if mode == "withdraw" and (not getattr(args, "pet_id", None) or not getattr(args, "who", None)):
        raise JobError("withdraw 模式要給 --who 和 --pet-id")
    if mode == "deposit" and not getattr(args, "pet_id", None):
        raise JobError("deposit 模式要給 --pet-id")
    if mode in ("release-public", "release-private") and getattr(args, "apply", False) and (
            not getattr(args, "pet_id", None) or not getattr(args, "confirm_release", False)):
        raise JobError(f"{mode} 要給 --pet-id 和 --confirm-release（放生不可逆）；沒有送任何東西")
    exc = getattr(args, "exception", None)
    if exc is not None and (mode not in ("release-public", "release-private", "release-worst") or not str(exc).strip()):
        raise JobError("--exception 只能配 release-public／release-private，而且要填使用者原話")
    cnt = getattr(args, "count", None)
    if cnt is not None and (mode not in ("release-public", "release-private", "release-worst") or cnt < 1):
        raise JobError("--count 只能配 release-public／release-private，而且要 ≥ 1")


def names_of(p, args) -> List[str]:
    """--who 指定的角色（要在這組隊伍裡），沒指定就全隊。"""
    everyone = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if not who:
        return everyone
    want = [x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()]
    bad = [n for n in want if n not in everyone]
    if bad:
        raise JobError(f"--who 不在這組隊伍裡：{bad}（隊伍 {everyone}）")
    return want


# ---- 小動作：每個都等封包確認，沒確認丟 JobError ----
def slots(p, name: str) -> Dict[int, dict]:
    """寵物欄（petId > 0 的格子）：格號 → 寵物。petId ≤ 0 是空格（存倉／交出後那格變 -1）。"""
    with p.lock:
        return {k: dict(v) for k, v in p.petinfo.get(name, {}).items() if (v.get("petId") or 0) > 0}


def _desc(p, pets) -> List[tuple]:
    return [(p.pet_name(v), v.get("petId"), v.get("level"), v.get("status")) for v in pets]


def unlock(p, name: str, fight: int = 0, ride: int = 0) -> None:
    """改外掛寵物鎖定（唯一編號；0＝清掉）。鎖著的寵物不能改狀態、不能存倉，所以動寵物之前先改這個。"""
    r = p.clients[name].request("WaiGua_NomalSettingHandlePet", {"fightPetIndex": fight, "ridePetIndex": ride}) or {}
    if not r.get("isOK"):
        raise JobError(f"{name} 改外掛寵物鎖定（戰 {fight}、騎 {ride}）被拒：{r}")
    p.nap(1)


def rest_pet(p, name: str, idx: int) -> None:
    """出戰／騎乘中的改休息。NewPet_NewPetChangeStatus 沒有 response → 只 send，看 Pet_PetInfo.status。"""
    pet = slots(p, name).get(idx)
    if not pet or pet.get("status") not in (FIGHT, RIDE):
        return
    p.clients[name].send("NewPet_NewPetChangeStatus", {"petIndex": idx, "status": REST})
    if not p.wait(lambda: p.petinfo[name].get(idx, {}).get("status") == REST, 5):
        raise JobError(f"{name} 第 {idx} 欄 {p.pet_name(pet)} 改休息沒確認")


def store_pet(p, name: str, idx: int) -> dict:
    """存寵物倉庫第 1 頁（先改休息）：個倉還有空位存個倉，滿了改存共倉（LESSONS #89），兩邊都滿就停。
    回 isOK 且那欄 petId ≤ 0 才算數。"""
    pet = slots(p, name).get(idx)
    if not pet:
        raise JobError(f"{name} 第 {idx} 欄沒有寵物，不能存倉")
    public = p.pet_depot_target(name)
    if public is None:
        raise JobError(f"{name} 個倉、共倉第 1 頁都滿了，{p.pet_name(pet)}（{pet.get('level')} 級）存不進去"
                       f"（第 2 頁要鑽石解鎖，不碰）")
    where = "共倉" if public else "個人寵物倉庫"
    rest_pet(p, name, idx)
    s = p.clients[name].request("NewPet_SavePetToNewDepot", {"page": 1, "isPublic": public, "petIndex": idx}) or {}
    if not (s.get("isOK") and p.wait(lambda: (p.petinfo[name].get(idx, {}).get("petId") or -1) <= 0, 5)):
        raise JobError(f"{name} {p.pet_name(pet)}（{pet.get('level')} 級）存倉沒確認（{where}）：{s}")
    p.log(f"{name} {p.pet_name(pet)}（{pet.get('level')} 級）存{where}")
    return pet


def take_pet(p, name: str, row: dict) -> dict:
    """從寵物倉庫領回一隻（倉庫那筆的 uniqueIndex；isPublic 照那筆）。回 isOK 且寵物欄多一隻那個 petId。回傳寵物欄裡那隻。"""
    pid = row.get("petId")
    if len(slots(p, name)) >= SLOT_MAX:
        raise JobError(f"{name} 寵物欄已滿 {SLOT_MAX} 格，領不回 {p.pet_name(row)}")
    before = sum(1 for v in slots(p, name).values() if v.get("petId") == pid)
    g = p.clients[name].request("NewPet_GetPetFromNewDepot",
                                {"page": p.pet_page(name, row), "isPublic": bool(row.get("isPublic")),
                                 "uniqueIndex": row["uniqueIndex"]}) or {}
    ok = g.get("isOK") and p.wait(
        lambda: sum(1 for v in p.petinfo.get(name, {}).values() if v.get("petId") == pid) > before, 6)
    if not ok:
        raise JobError(f"{name} 從倉庫領回 {p.pet_name(row)}（{row.get('level')} 級）沒確認：{g}")
    p.nap(1)
    got = next((v for v in slots(p, name).values() if v.get("uniqueIndex") == row["uniqueIndex"]), None) \
        or max((v for v in slots(p, name).values() if v.get("petId") == pid), key=lambda v: v.get("level") or 0)
    p.log(f"{name} 從{'共倉' if row.get('isPublic') else '個人寵物倉庫'}領回 {p.pet_name(got)}（{got.get('level')} 級）")
    return got


def lock(p, name: str, fight: int, ride: int) -> None:
    """鎖戰寵／騎寵（唯一編號），用推回的 wgNomalSetting 或寵物狀態確認（Party.set_fight_pet）。"""
    if not p.set_fight_pet(name, fight, ride):
        raise JobError(f"{name} 鎖 戰寵 {fight}／騎寵 {ride} 沒確認")


def _find(p, name: str, pid: int) -> Optional[Tuple[int, dict]]:
    return next(((k, v) for k, v in slots(p, name).items() if v.get("petId") == pid), None)


# ---- 各模式（一隻角色）----
def main_fight(p, name: str) -> Optional[dict]:
    """這隻角色的主戰寵：目前鎖定的戰寵（還在寵物欄）；沒有就挑身上等級最高、不是金虎的那隻。
    不寫死人龍：三轉後戰寵是「未滿 140 最高等」的瑪斯貝卡，人龍在倉庫（2026-10-25 第二組劇本卡坦前，#83）。"""
    pets = slots(p, name)
    fight_u, _ = p.locks(name)
    cur = next((v for v in pets.values() if fight_u and v.get("uniqueIndex") == fight_u), None)
    if cur is not None and cur.get("petId") != TIGER:
        return cur
    others = sorted((v for v in pets.values() if v.get("petId") != TIGER), key=lambda v: -(v.get("level") or 0))
    return others[0] if others else None


def do_park(p, name: str) -> None:
    """卡坦前：騎寵鎖定清掉（戰寵留原本的主戰寵）→ **把騎寵存倉**，空出格子給 4 隻鯊魚／雞。

    **戰寵以外的全部存倉**（騎寵、還有留在欄位裡休息的）：卡坦一步要同時帶 4 隻，寵物欄只有 5 格，
    只留戰寵才夠。只認金虎 310 的舊版對第四、五組（騎寵 307／309）等於沒做；
    只存騎寵也不夠，還會剩休息的那隻（2026-10-27 兩組實機都卡在「超過上限」）。
    """
    fight_u, ride_u = p.locks(name)
    # 有鎖定的戰寵（不是騎寵）就留那隻，金虎也一樣：使用者指定練金虎時 main_fight 會挑到休息的非金虎，
    # 或身上只剩金虎就停（11-05 第八組角色21、角色42，LESSONS #168）。
    # 其他 main_fight 呼叫：catch.make_room 已改（#167）；equip-fight／refresh 本來就是「換」戰寵，不受影響
    fight = next((v for v in slots(p, name).values()
                  if fight_u and fight_u != ride_u and v.get("uniqueIndex") == fight_u), None) or main_fight(p, name)
    if not fight:
        raise JobError(f"{name} 寵物欄沒有金虎以外的寵物可以當戰寵")
    if not hasattr(p, "parked_fight"):
        p.parked_fight = {}
    p.parked_fight[name] = fight.get("uniqueIndex")     # restore 時戰寵用回這隻（交寵物前 rest 會把鎖定清成 0）
    if ride_u and ride_u != fight.get("uniqueIndex"):
        # 記住原本的騎寵，restore 時倉庫沒金虎就領回這隻（2026-11-03 第七、八組騎瑪斯貝卡，LESSONS #154）
        if not hasattr(p, "parked_ride"):
            p.parked_ride = {}
        p.parked_ride[name] = ride_u
    unlock(p, name, fight["uniqueIndex"], 0)
    for k, v in sorted(slots(p, name).items()):
        if v.get("uniqueIndex") == fight.get("uniqueIndex"):
            continue                                   # 留著當戰寵的那隻不動
        store_pet(p, name, k)                          # 其餘（騎寵、休息的）全部存倉


def do_equip_fight(p, name: str) -> None:
    """**換上最強的戰寵**：寵物欄＋個倉＋共倉裡不是金虎、等級最高的那隻，鎖成戰寵（騎寵維持現狀）。

    2026-10-29 第三組：卡坦交完寵物後身上只剩任務送的 1 級朵拉比斯，`restore` 只領金虎當騎寵、
    `refresh` 要戰寵滿 140 才會動，於是拿 1 級戰寵去打英雄島，一出場就被秒、任務判定「有人被打死」。
    戰寵等級**不限制**（只有騎寵不能高於人物等級）。已經是最強的就不動。
    """
    # **未滿 140**：滿級寵物練功不長經驗，常駐會自動排 refresh 把它換掉（2026-10-29 實機：
    # equip-fight 配上 140 級的瑪斯貝卡／迪基格斯，下一輪 refresh 立刻換成 42 級的，兩個功能打架）。
    # 鎖定中的騎寵也跳過（不一定是金虎，例：第十組騎斯天多斯 333；11-04 伏特加的戰寵挑到自己的騎寵、
    # 鎖定失敗戰寵被清成 0，LESSONS #160）
    _, ride_now = p.locks(name)
    here = [v for v in slots(p, name).values()
            if v.get("petId") != TIGER and (v.get("level") or 0) < MAX_PET_LV
            and not (ride_now and v.get("uniqueIndex") == ride_now)]
    rows = [r for r in p.pet_depots(name)
            if r.get("petId") != TIGER and (r.get("level") or 0) < MAX_PET_LV]
    best_here = max(here, key=lambda v: v.get("level") or 0, default=None)
    best_row = max(rows, key=lambda r: r.get("level") or 0, default=None)
    if best_here is None and best_row is None:
        raise JobError(f"{name} 寵物欄和倉庫都沒有金虎以外的寵物可以當戰寵")
    use_depot = best_row is not None and (best_here is None
                                          or (best_row.get("level") or 0) > (best_here.get("level") or 0))
    if use_depot:
        if len(slots(p, name)) >= SLOT_MAX:                 # 沒空位：先把身上等級最低的存倉
            low = min(slots(p, name).items(), key=lambda kv: kv[1].get("level") or 0)
            store_pet(p, name, low[0])
        best = take_pet(p, name, best_row)
    else:
        best = best_here
    _, ride_u = p.locks(name)
    with p.lock:
        locked = (p.wg.get(name) or {}).get("fightPetIndex") or 0
    if locked and locked == best.get("uniqueIndex"):      # 看**真的鎖定**的那隻；鎖定被清成 0 就要重鎖
        p.log(f"{name} 戰寵已經是 {p.pet_name(best)}（{best.get('level')} 級），不動")
        return
    lock(p, name, best["uniqueIndex"], ride_u)
    p.log(f"{name} 戰寵 → {p.pet_name(best)}（{best.get('level')} 級）"
          f"{'（從倉庫領回）' if use_depot else ''}")


def do_take(p, name: str, pid: int, any_level: bool = False, ride_pid: Optional[int] = None) -> None:
    """**指定的寵物**（--pet-id）鎖成戰寵、騎寵照舊。equip-fight 只挑等級最高的，指定不了哪一隻。

    使用者 2026-10-30「取出帖拉所伊朵 並鎖定開始練功」：五轉打完機暴拿到的帖拉所伊朵（1381）跟著轉生
    全部存倉，一直放在個倉。身上已經有就直接用；沒有就從**個倉**領未滿 140 裡等級最高的那隻
    （共倉同帳號共用，不拿）。寵物欄滿了就停（不自己挑一隻存掉）；原本的戰寵留在寵物欄休息。
    any_level：滿級的也拿（使用者 2026-11-01：132 的戰寵邦司涼朵 1647 是 140 級、放在個倉）。
    """
    fight_u, ride_u = p.locks(name)
    fits = lambda x: x.get("petId") == pid and (any_level or (x.get("level") or 0) < MAX_PET_LV)   # noqa: E731
    # 身上那隻同種的若是鎖定騎寵（例：騎金虎又要拿個倉另一隻金虎當戰寵），跳過它改從個倉領
    # （11-04 角色53，舊版挑到騎寵直接停，LESSONS #159）
    keep_ride = ride_u if ride_pid is None else None
    here = [v for v in slots(p, name).values() if fits(v) and not (keep_ride and v.get("uniqueIndex") == keep_ride)]
    if here:
        new = max(here, key=lambda v: v.get("level") or 0)
    else:
        rows = [r for r in p.pet_depot(name) if fits(r)]
        if not rows:
            limit = "" if any_level else f"未滿 {MAX_PET_LV} 級的"
            raise JobError(f"{name} 寵物欄和個人寵物倉庫都沒有{limit}寵物 {pid}")
        new = take_pet(p, name, max(rows, key=lambda r: r.get("level") or 0))
    if ride_pid is not None:
        # --ride-pet-id：連騎寵也指定（使用者 2026-11-01「騎貝魯卡、放布魯頓，每個人物都是」）。
        # 身上找不到就從個倉領；跟戰寵同一隻就停。
        rides = [v for v in slots(p, name).values() if v.get("petId") == ride_pid]
        if not rides:
            rows = [r for r in p.pet_depot(name) if r.get("petId") == ride_pid]
            if not rows:
                raise JobError(f"{name} 寵物欄和個人寵物倉庫都沒有寵物 {ride_pid}（要當騎寵）")
            rides = [take_pet(p, name, max(rows, key=lambda r: r.get("level") or 0))]
        ride = max(rides, key=lambda v: v.get("level") or 0)
        if ride.get("uniqueIndex") == new.get("uniqueIndex"):
            raise JobError(f"{name} 戰寵和騎寵指到同一隻（{p.pet_name(new)}），不送")
        ride_u = ride["uniqueIndex"]
    if ride_u and new.get("uniqueIndex") == ride_u:
        raise JobError(f"{name} {p.pet_name(new)} 是鎖定的騎寵，不能同時當戰寵")
    with p.lock:
        locked = (p.wg.get(name) or {}).get("fightPetIndex") or 0
    with p.lock:
        locked_ride = (p.wg.get(name) or {}).get("ridePetIndex") or 0
    if locked and locked == new.get("uniqueIndex") and (ride_pid is None or locked_ride == ride_u):
        p.log(f"{name} 戰寵已經是 {p.pet_name(new)}（{new.get('level')} 級），不動")
        return
    lock(p, name, new["uniqueIndex"], ride_u)
    p.log(f"{name} 戰寵 → {p.pet_name(new)}（{new.get('level')} 級）"
          + (f"、騎寵 → {ride_pid}" if ride_pid is not None else ""))


def do_ride(p, name: str, uid: int) -> None:
    """指定唯一編號的寵物換成騎寵、戰寵不動（使用者 11-05「第三組騎寵換一下」：騎寵 140 不長經驗）。
    身上沒有就從個倉／共倉領；比人物等級高騎不上就停。原本的騎寵留在寵物欄休息。"""
    fight_u, ride_u = p.locks(name)
    if uid == ride_u:
        p.log(f"{name} 騎寵已經是 {uid}，不動")
        return
    if uid == fight_u:
        raise JobError(f"{name} 寵物 {uid} 是鎖定的戰寵，不能同時當騎寵")
    pet = next((v for v in slots(p, name).values() if v.get("uniqueIndex") == uid), None)
    if pet is None:
        rows = [r for r in p.pet_depots(name) if r.get("uniqueIndex") == uid]
        if not rows:
            raise JobError(f"{name} 身上和個倉、共倉都找不到寵物 {uid}")
        lv = char_level(p, name)
        if lv and (rows[0].get("level") or 0) > lv:
            raise JobError(f"{name} 寵物 {uid}（{rows[0].get('level')} 級）高於人物 {lv} 級，騎不上")
        pet = take_pet(p, name, rows[0])
    lock(p, name, fight_u, uid)
    p.log(f"{name} 騎寵 → {p.pet_name(pet)}（{pet.get('level')} 級）")


def ensure_ride(p, name: str) -> Optional[dict]:
    """沒有騎寵（沒鎖定、也沒有騎乘中的）就騎一隻：身上＋個倉＋共倉裡不用學騎乘（Ride.json needlearn false）、
    不高於人物等級、不是戰寵的寵物，挑等級最高的。11-06 第四、五組（0 轉、原本騎瑪斯貝卡）卡坦 park 把騎寵存倉、
    Pod 重開後 restore 忘了原騎寵 → 沒騎寵打英雄島龍王守衛，連四次有人被打死。回傳騎上的那隻；不用騎／沒得騎回 None。"""
    fight_u, ride_u = p.locks(name)
    if ride_u:
        return None
    if name in (getattr(p, "parked_fight", None) or {}):
        # 卡坦 park 過、還沒 restore：騎寵是故意存倉空格子的，restore 會領回。11-06 cgy 一組抓鯊魚失敗回練功，
        # 這裡把瑪斯貝卡領回來佔滿 5 格，重試的抓寵每次「超過上限 5」
        return None
    rides = p.rideable()
    lv = char_level(p, name)

    def ok(v) -> bool:
        return (v.get("petId") in rides and v.get("uniqueIndex") not in (fight_u, None, 0)
                and (not lv or (v.get("level") or 0) <= lv))
    here = [v for v in slots(p, name).values() if ok(v)]
    there = [r for r in p.pet_depots(name) if ok(r)]
    best = max(here + there, key=lambda v: (v.get("level") or 0, v in here), default=None)
    if best is None:
        return None
    do_ride(p, name, best["uniqueIndex"])
    return best


def loadout_pet_ids(profile, name: str, folder: Optional[str] = None) -> set:
    """共用情境配置（config/loadouts/*.json：英雄戰場、副本…）這組／這隻角色會用到的戰寵種類。
    retire 不能把它們存倉：切配置只找身上那隻、寵物倉不自動領（11-05 第二組菲奇亞 2258 被收進個倉，LESSONS #170）。"""
    from ..loadout import SHARED_DIR
    folder = folder or SHARED_DIR
    out = set()
    try:
        files = [f for f in os.listdir(folder) if f.endswith(".json") and ".example." not in f]
    except OSError:
        return out
    for f in files:
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fp:
                d = json.load(fp)
        except (OSError, ValueError):
            continue
        layers = [d.get("all") or {}, (d.get("roles") or {}).get(name) or {}]
        prof = (d.get("profiles") or {}).get(str(profile)) or {}
        layers += [prof.get("*") or {}, prof.get(name) or {}]
        for layer in layers:
            pid = (layer.get("pet") or {}).get("petId") if isinstance(layer, dict) else None
            if pid:
                out.add(int(pid))
    return out


def do_retire(p, name: str) -> None:
    """身上滿 140、不是鎖定戰寵／騎寵的寵物改名成評分數字、存倉（使用者 11-05「140的把分數改名後存倉庫」，
    同 graduate 的命名）。已經是那個名字就不再改。"""
    fight_u, ride_u = p.locks(name)
    keep = loadout_pet_ids(getattr(p, "profile", None), name)
    olds = [v for v in slots(p, name).values() if (v.get("level") or 0) >= MAX_PET_LV
            and v.get("uniqueIndex") not in (fight_u, ride_u) and v.get("petId") not in keep]
    for v in olds:
        if v.get("score"):
            rename_pet(p, name, v["uniqueIndex"], str(v["score"]))
        idx = next(k for k, x in slots(p, name).items() if x.get("uniqueIndex") == v["uniqueIndex"])
        store_pet(p, name, idx)


def do_withdraw(p, name: str, pid: int, uid: Optional[int] = None) -> None:
    """只領出、不鎖定（使用者 2026-11-01「我是取出 沒有要戰鬥」）：個倉＋共倉找那種寵物，戰寵、騎寵都不動。
    倉庫有多隻就停，不替使用者挑。"""
    # 指定了唯一編號就不會拿錯，身上有同種也照領（使用者 11-03：女寵 150074 放到已帶老鼠三號的角色15身上）
    if uid is None and any(v.get("petId") == pid for v in slots(p, name).values()):
        raise JobError(f"{name} 身上已經有寵物 {pid}，不再領（同種要多帶一隻用 --pet-uid 指定）")
    if uid is not None and any(v.get("uniqueIndex") == uid for v in slots(p, name).values()):
        raise JobError(f"{name} 身上已經有寵物 {uid}，不用領")
    rows = [r for r in p.pet_depots(name) if r.get("petId") == pid and (uid is None or r.get("uniqueIndex") == uid)]
    if len(rows) != 1:
        raise JobError(f"{name} 個倉＋共倉有 {len(rows)} 隻寵物 {pid}（要剛好 1 隻才領；同種多隻用 --pet-uid 指定）")
    got = take_pet(p, name, rows[0])
    p.log(f"{name} 取出 {p.pet_name(got)}（{got.get('level')} 級）：體 {got.get('vitArg')}／腕 {got.get('atkArg')}"
          f"／耐 {got.get('defArg')}／速 {got.get('dexArg')}，沒有鎖定")


def do_deposit(p, name: str, pid: int, public: bool = False, uid: Optional[int] = None) -> None:
    """身上那種寵物存**個人**寵物倉庫（withdraw 的相反，使用者 2026-11-02「把mm存到個人倉庫」）：
    個倉滿了就停，不像 store_pet 改存共倉；要剛好 1 隻、不能是鎖定的戰寵／騎寵。
    `--public` 改存**共倉**（使用者 11-05「存共倉」：五隻剛抓的邦浦洛斯）；共倉滿了就停，不改存個倉。"""
    # --pet-uid：同種多隻時指定哪一隻（使用者 11-08 第六組每人 2 隻黑暴，只留 +2 以上的練）
    hits = [k for k, v in slots(p, name).items() if v.get("petId") == pid and (uid is None or v.get("uniqueIndex") == uid)]
    if len(hits) != 1:
        raise JobError(f"{name} 身上有 {len(hits)} 隻寵物 {pid}{'' if uid is None else f'（唯一編號 {uid}）'}"
                       f"（要剛好 1 隻才存；同種多隻用 --pet-uid 指定）")
    uid = slots(p, name)[hits[0]].get("uniqueIndex")
    if uid in p.locks(name):
        raise JobError(f"{name} 第 {hits[0]} 格是鎖定的戰寵／騎寵，不存")
    if public:
        to_public(p, name, uid)
        p.log(f"{name} 寵物 {pid}（{uid}）存共倉")
        return
    if p.pet_depot_target(name) is not False:
        raise JobError(f"{name} 個人寵物倉庫第 1 頁滿了，不改存共倉，停")
    store_pet(p, name, hits[0])


def do_rest(p, name: str) -> None:
    """交寵物前：鎖定清成 0 → 全部改休息（出戰中的交不出去，doc/autoplay/quests/2019.md）。"""
    unlock(p, name, 0, 0)
    for k in slots(p, name):
        rest_pet(p, name, k)


def char_level(p, name: str) -> int:
    with p.lock:
        return int(p.s.levels.get(name) or p.attrs.get(name, {}).get("level") or 0)


def store_katan_leftovers(p, name: str) -> None:
    """寵物欄滿了就把卡坦留下的鯊魚、雞（沒鎖定的）存倉，金虎才領得回來
    （11-12 夜裡 19 組卡坦 2019 交完，4 隻雞還在身上 5/5，restore「領不回金格薩貝魯」重試了一整晚）。"""
    if len(slots(p, name)) < SLOT_MAX:
        return
    locked = set(p.locks(name))
    for k, v in sorted(slots(p, name).items()):
        if v.get("petId") in KATAN_PETS and v.get("uniqueIndex") not in locked:
            store_pet(p, name, k)


def do_restore(p, name: str) -> None:
    """卡坦後：身上沒金虎就從倉庫領回金虎 → 鎖 戰寵＝主戰寵、騎寵＝金虎。
    領的是**等級不高於人物**的金虎裡最高的那隻：比人物高的騎不上（使用者 2026-10-25「再取出時要確保是金虎，
    同時等級不能高於人物等級」；三轉後人物 125 級、倉庫有 129 級金虎，舊版挑最高等就挑到它）。"""
    store_katan_leftovers(p, name)
    lv = char_level(p, name)
    kept_u = (getattr(p, "parked_fight", None) or {}).get(name)
    kept = next((v for v in slots(p, name).values() if kept_u and v.get("uniqueIndex") == kept_u), None)
    if kept_u and not kept:
        # park 留下的那隻被別的工作存進倉庫了（交任務前整理寵物欄會把休息中的寵物存倉）：用唯一編號領回
        # （11-06 第七、八組卡坦停在「沒有金虎以外的寵物可以當戰寵」，LESSONS #175）
        row = next((r for r in p.pet_depots(name) if r.get("uniqueIndex") == kept_u), None)
        if row:
            p.log(f"{name} 原本的戰寵 {kept_u} 在倉庫，領回來")
            kept = take_pet(p, name, row)
    # park 留下的戰寵是金虎時，牠不能被當成騎寵（#168）
    cur = next(((k, v) for k, v in slots(p, name).items()
                if v.get("petId") == TIGER and v.get("uniqueIndex") != kept_u), None)
    if cur and lv and (cur[1].get("level") or 0) > lv:
        raise JobError(f"{name} 身上的金虎 {cur[1].get('level')} 級高於人物 {lv} 級，騎不上；先處理再 restore")
    if not cur:
        tigers = sorted((r for r in p.pet_depots(name) if r.get("petId") == TIGER
                         and (not lv or (r.get("level") or 0) <= lv)), key=lambda r: -(r.get("level") or 0))
        parked = (getattr(p, "parked_ride", None) or {}).get(name)
        if not tigers and parked:
            # 還沒打英雄島、騎的不是金虎（第七、八組騎瑪斯貝卡）：領回 park 存進去的那隻原騎寵
            here = next((v for v in slots(p, name).values() if v.get("uniqueIndex") == parked), None)
            row = None if here else next((r for r in p.pet_depots(name) if r.get("uniqueIndex") == parked), None)
            if here or row:
                ride = here or take_pet(p, name, row)
                fight = kept or main_fight(p, name)
                if fight and fight.get("uniqueIndex") == ride.get("uniqueIndex"):     # 鎖定被清掉時別挑到騎寵
                    fight = max((v for v in slots(p, name).values() if v.get("petId") != TIGER
                                 and v.get("uniqueIndex") != ride.get("uniqueIndex")),
                                key=lambda v: v.get("level") or 0, default=None)
                if not fight:
                    raise JobError(f"{name} 寵物欄沒有原騎寵以外的寵物可以當戰寵")
                lock(p, name, fight["uniqueIndex"], ride["uniqueIndex"])
                p.parked_ride.pop(name, None)
                return
        if not tigers:
            # 沒有金虎（還沒轉生的本來就沒有；park 記下的原騎寵在 Pod 重開後也會忘掉）：只換回最強的戰寵、
            # 不騎，照樣往下（11-06 第四、五組卡坦交完雞停在這裡，劇本卡住、還空手回 130 級圖）
            p.log(f"{name} 個人寵物倉庫沒有{f' {lv} 級以下的' if lv else ''}金虎：只換回最強的戰寵、不騎")
            do_equip_fight(p, name)
            (getattr(p, "parked_fight", None) or {}).pop(name, None)
            return
        got = take_pet(p, name, tigers[0])
        cur = (None, got)
    tiger = cur[1]
    fight = kept or main_fight(p, name)
    if not fight:
        raise JobError(f"{name} 寵物欄沒有金虎以外的寵物可以當戰寵")
    lock(p, name, fight["uniqueIndex"], tiger["uniqueIndex"])
    (getattr(p, "parked_fight", None) or {}).pop(name, None)


def _release_ids(p, name: str) -> set:
    """規則表（item-rules.md）寫 release、沒被保護名單／任務條件擋住的寵物 petId。"""
    from .clean import ctx_of
    from ..item_rules import load
    rules, ctx, out = load(), ctx_of(p, name), set()
    for v in slots(p, name).values():
        pid = v.get("petId")
        rule = rules.pet_rule(pid, ctx)
        if rule is not None and rule.action == "release" and not rules.blocks(rule) \
                and pid not in rules.task_pets and pid not in rules.protect:
            out.add(pid)
    return out


def store_all(p, name: str, keep=()) -> int:
    """全部寵物存個人寵物倉庫（使用者 2026-10-24：轉生打完機暴後全存，守護獸才放得進寵物欄）。
    外掛戰寵／騎寵鎖定清 0 → 出戰／騎乘的改休息 → 逐隻存倉 → 確認寵物欄全空。回傳存了幾隻。
    keep：這些種類每種留一隻在身上（轉生覲見精靈王要帶的守護獸）。"""
    unlock(p, name, 0, 0)
    for k in slots(p, name):
        rest_pet(p, name, k)
    n = 0
    drop = _release_ids(p, name)
    kept = {}
    for k, v in sorted(slots(p, name).items()):
        if v.get("petId") in keep and v["petId"] not in kept:
            kept[v["petId"]] = k
            continue
        # 規則表寫 release 的 1 級寵物直接放生、不存倉（2026-11-03 第六組轉生把奇娜、多魯寶、烏力斯坦全存進個倉，
        # 使用者：「之前定義轉身可以丟的你怎麼沒丟」，LESSONS #153）
        if v.get("petId") in drop and (v.get("level") or 0) == 1:
            from .clean import do_release
            do_release(p, name, k, v["petId"])
            continue
        store_pet(p, name, k)
        n += 1
    left = {k: v for k, v in slots(p, name).items() if k not in kept.values()}
    if left:
        raise JobError(f"{name} 寵物欄還有 {_desc(p, left.values())}")
    p.log(f"{name} 寵物欄已清空（存了 {n} 隻）" if not kept else f"{name} 寵物欄只留 {sorted(kept)}（存了 {n} 隻）")
    return n


def do_swap_tiger(p, name: str) -> None:
    """轉生後騎寵換成 1 級金虎：戰寵沿用（鎖定的那隻；沒有就挑非金虎裡最高等的）→ 騎寵鎖定先清 →
    身上高等金虎存倉 → 領回 1 級金虎 → 鎖 戰寵＝原戰寵、騎寵＝1 級金虎。已經是 1 級金虎就不動。"""
    pets = slots(p, name)
    tigers = [(k, v) for k, v in pets.items() if v.get("petId") == TIGER]
    if any((v.get("level") or 0) == 1 for _, v in tigers):
        p.log(f"{name} 身上已經有 1 級金虎，不用換")
        return
    fight_now, _ = p.locks(name)
    fight = next((v for v in pets.values() if v.get("uniqueIndex") == fight_now and v.get("petId") != TIGER), None) \
        or max((v for v in pets.values() if v.get("petId") != TIGER), key=lambda v: v.get("level") or 0, default=None)
    if not fight:
        raise JobError(f"{name} 寵物欄沒有金虎以外的戰寵：{_desc(p, pets.values())}")
    small = next((r for r in p.pet_depot(name) if r.get("petId") == TIGER and (r.get("level") or 0) == 1), None)
    if not small:
        raise JobError(f"{name} 個人寵物倉庫找不到 1 級金虎")
    _replace_tigers(p, name, fight, tigers, small)


def do_ride_train(p, name: str) -> None:
    """騎寵練到 140 就換一隻還沒滿的金虎來練（使用者 2026-11-03 第三組「取出倉庫尚未140的騎寵，來練」）：
    身上的金虎有一隻 140 → 存倉，從**個人**寵物倉庫領未滿 140 裡等級最高的金虎鎖成騎寵，戰寵沿用。
    身上金虎都沒滿 140 就不動；個倉沒有未滿 140 的金虎就停（共倉同帳號共用，不拿）。"""
    pets = slots(p, name)
    tigers = [(k, v) for k, v in pets.items() if v.get("petId") == TIGER]
    if not any((v.get("level") or 0) >= MAX_PET_LV for _, v in tigers):
        p.log(f"{name} 身上的金虎都還沒滿 {MAX_PET_LV} 級，不用換")
        return
    fight_now, _ = p.locks(name)
    fight = next((v for v in pets.values() if v.get("uniqueIndex") == fight_now and v.get("petId") != TIGER), None) \
        or max((v for v in pets.values() if v.get("petId") != TIGER), key=lambda v: v.get("level") or 0, default=None)
    if not fight:
        raise JobError(f"{name} 寵物欄沒有金虎以外的戰寵：{_desc(p, pets.values())}")
    rows = [r for r in p.pet_depot(name) if r.get("petId") == TIGER and (r.get("level") or 0) < MAX_PET_LV]
    if not rows:
        raise JobError(f"{name} 個人寵物倉庫沒有未滿 {MAX_PET_LV} 級的金虎")
    best = max(rows, key=lambda r: r.get("level") or 0)
    _replace_tigers(p, name, fight, tigers, best)
    p.log(f"{name} 騎寵 → 金格薩貝魯 {best.get('level')} 級（原本 {MAX_PET_LV} 級的存倉）")


def _replace_tigers(p, name: str, fight: dict, tigers, row: dict) -> None:
    """騎寵鎖定先清 → 身上金虎全部存倉 → 領回倉庫那隻 → 鎖 戰寵＝原戰寵、騎寵＝新金虎。"""
    unlock(p, name, fight["uniqueIndex"], 0)
    for k, _ in tigers:
        store_pet(p, name, k)
    new = take_pet(p, name, row)
    lock(p, name, fight["uniqueIndex"], new["uniqueIndex"])


MAX_PET_LV = 140                   # 寵物等級上限（Config.petMaxLevel）：滿級再練也不長經驗


def maxed(p, name: str) -> Tuple[Optional[dict], Optional[dict]]:
    """目前鎖定的戰寵、騎寵裡已經滿級（≥140）的那隻；沒有回 None。"""
    fight_u, ride_u = p.locks(name)
    pets = slots(p, name).values()
    f = next((v for v in pets if fight_u and v.get("uniqueIndex") == fight_u and (v.get("level") or 0) >= MAX_PET_LV), None)
    r = next((v for v in pets if ride_u and v.get("uniqueIndex") == ride_u and (v.get("level") or 0) >= MAX_PET_LV), None)
    return f, r


def _swap_in(p, name: str, old: dict, ok, what: str) -> dict:
    """把滿級的 old 換成「符合 ok 條件、未滿 140 裡等級最高」的一隻：寵物欄有就直接用，沒有從個倉領
    （寵物欄滿就先把 old 存倉騰位）。回傳換上的那隻（還沒鎖定）。"""
    here = [v for v in slots(p, name).values() if v.get("uniqueIndex") != old.get("uniqueIndex")
            and (v.get("level") or 0) < MAX_PET_LV and ok(v)]
    if here:
        return max(here, key=lambda v: v.get("level") or 0)
    rows = [r for r in p.pet_depots(name) if (r.get("level") or 0) < MAX_PET_LV and ok(r)]
    if not rows:
        raise JobError(f"{name} {what}已滿 {MAX_PET_LV} 級，但寵物欄和個倉都沒有未滿 {MAX_PET_LV} 級可以換的")
    best = max(rows, key=lambda r: r.get("level") or 0)
    if len(slots(p, name)) >= SLOT_MAX:
        raise JobError(f"{name} 寵物欄已滿 {SLOT_MAX} 格，領不回替換的{what}")
    return take_pet(p, name, best)


def do_refresh(p, name: str) -> None:
    """使用者 2026-10-25「組隊練功時，戰寵或騎寵已經 140，自行判斷挑一隻 <140 置換」：
    戰寵 → 不是金虎、未滿 140 裡最高等；騎寵 → 金虎、未滿 140、**不高於人物等級**裡最高等（LESSONS #85）。
    換上後鎖定，換下來的滿級寵物存個人寵物倉庫。兩隻都沒滿級就什麼都不做。"""
    f, r = maxed(p, name)
    if not f and not r:
        return
    lv = char_level(p, name)
    fight_u, ride_u = p.locks(name)
    # 挑戰寵時要排除**正在當騎寵的那隻**：第四組 2026-10-28 鎖定被清成 0，locks() 改用「目前騎乘中」的 309，
    # 而 309 也是未滿 140 裡等級最高的 → 戰寵、騎寵填到同一個 uniqueIndex，伺服器不受理（連兩次沒確認）。
    new_f = _swap_in(p, name, f, lambda v: v.get("petId") != TIGER and v.get("uniqueIndex") != ride_u,
                     "戰寵") if f else None
    new_r = _swap_in(p, name, r, lambda v: (v.get("petId") == TIGER and (not lv or (v.get("level") or 0) <= lv)
                                            and v.get("uniqueIndex") != (new_f or {}).get("uniqueIndex", fight_u)),
                     "騎寵") if r else None
    if new_f and new_r and new_f.get("uniqueIndex") == new_r.get("uniqueIndex"):
        raise JobError(f"{name} 換寵挑到同一隻（{p.pet_name(new_f)}）當戰寵和騎寵，不送")
    lock(p, name, new_f["uniqueIndex"] if new_f else fight_u, new_r["uniqueIndex"] if new_r else ride_u)
    for old, new, what in ((f, new_f, "戰寵"), (r, new_r, "騎寵")):
        if not old:
            continue
        idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == old.get("uniqueIndex")), None)
        if idx is not None:
            store_pet(p, name, idx)
        p.log(f"{name} {what} {p.pet_name(old)}（{old.get('level')} 級滿級）換成 {p.pet_name(new)}（{new.get('level')} 級）")


def do_store_slot(p, name: str, args) -> None:
    """指定一格存個人寵物倉庫（可逆）：那一格要是 --pet-id、不是鎖定的戰寵／騎寵，否則停。
    2026-10-25 角色52等邦恩吉 240 秒時外掛多抓一隻克邦凱斯，寵物欄 5/5 補抓不了 → 先把多的那隻存倉。"""
    slot, pid = args.slot, args.pet_id
    pet = slots(p, name).get(slot)
    if not pet or pet.get("petId") != pid:
        raise JobError(f"{name} 第 {slot} 格不是寵物 {pid}（實際 {pet.get('petId') if pet else '空'}），不存")
    fight_u, ride_u = p.locks(name)
    if pet.get("uniqueIndex") in (fight_u, ride_u):
        raise JobError(f"{name} 第 {slot} 格是鎖定的戰寵／騎寵，不存")
    store_pet(p, name, slot)


def release_public(p, name: str, pid: int, public: bool = True, exception: Optional[str] = None,
                   count: Optional[int] = None) -> int:
    """共倉（public=False 時是 name 的個倉）第 1 頁裡 petId＝pid 的 **1 級**寵物逐隻領到 name 身上 → 放生
    （不可逆，進回收站 48 小時）。規則表（item-rules.md）那個 petId 要是 release、而且不是任務條件要的寵物才做，
    否則停、什麼都不送。
    使用者 2026-10-26：四轉要全部存倉、共倉只剩 5 格 → 照規則表放生共倉的 5 隻摩娜西普（355）騰位；
    同日「奇娜 多魯寶 烏力斯坦 都可以丟」「登入處理」→ 個倉的也要放（release-private）。
    共倉同帳號共用，只用一隻角色做。回傳放生幾隻。
    exception＝使用者單次破例的原話（2026-10-26「這次破例丟 金布一」「不設置放生表」）：不看規則表的 release、
    規則表也不改；任務條件寵物、保護名單、只放 1 級照樣檢查。"""
    from .clean import ctx_of, do_release
    from ..item_rules import load
    where = "共倉" if public else f"{name} 的個倉"
    rules = load()
    if exception:
        if pid in rules.task_pets or pid in rules.protect:
            raise JobError(f"寵物 {pid} 是任務條件／保護名單上的，破例也不放生，不動")
        p.log(f"放生寵物 {pid}：使用者單次破例「{exception}」（規則表不改）")
    else:
        rule = rules.pet_rule(pid, ctx_of(p, name))
        if rule is None or rule.action != "release" or rules.blocks(rule):
            raise JobError(f"寵物 {pid} 在規則表不是可以放生的（{rule.action if rule else '沒有規則'}），不動")
    rows = [r for r in p.pet_depot(name, public=public) if r.get("petId") == pid]
    if any((r.get("level") or 0) != 1 for r in rows):
        raise JobError(f"{where}裡的寵物 {pid} 有不是 1 級的 {[r.get('level') for r in rows]}，可能練過，不動")
    if count is not None:              # 使用者選「只丟 N 隻」（2026-10-26 丟寵物 skill）：先丟檔次最差的
        from ..petgrade import grade
        rows = sorted(rows, key=lambda r: (grade(r)[1] is None, grade(r)[1] or 0))[:count]
    n = 0
    for r in rows:
        got = take_pet(p, name, r)
        idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == got.get("uniqueIndex")), None)
        if idx is None:
            raise JobError(f"{name} 領回 {p.pet_name(r)} 後在寵物欄找不到它（唯一編號 {got.get('uniqueIndex')}），不放生")
        do_release(p, name, idx, pid)
        n += 1
    p.log(f"{name} {where}的寵物 {pid} 放生 {n} 隻")
    return n


def release_worst(p, name: str, count: int, exception: str, public: bool = False) -> int:
    """倉庫裡**1 級、檔次最差**的那幾隻放生（不分 petId）。使用者 2026-10-29 原話「丟1等檔位最差的」：
    轉生要把寵物全存倉，個倉共倉都 20/20，但他不想按種類整批丟，而是留檔好的、丟檔差的。

    一律只碰 **1 級**（練過的不丟）、**不是金虎**（轉生後要當騎寵）、不是任務條件寵物、不在保護名單。
    `exception` 是使用者這次的原話（會記進日誌）；**規則表不改**，所以不受 release 規則限制。
    回傳放生幾隻。"""
    from .clean import do_release
    from ..item_rules import load
    from ..petgrade import grade
    if not exception or not str(exception).strip():
        raise JobError("release-worst 要給 --exception（使用者原話），放生不可逆")
    rules = load()
    rows = [r for r in p.pet_depot(name, public=public)
            if (r.get("level") or 0) <= 1 and r.get("petId") != TIGER
            and r.get("petId") not in rules.task_pets and r.get("petId") not in rules.protect]
    if not rows:
        p.log(f"{'共倉' if public else name + ' 的個倉'}沒有可以丟的 1 級寵物（金虎、任務寵、保護名單不算）")
        return 0
    rows = sorted(rows, key=lambda r: (grade(r)[1] is None, grade(r)[1] or 0))[:count]
    p.log(f"放生{'共倉' if public else name + ' 個倉'}檔次最差的 {len(rows)} 隻：使用者原話「{exception}」"
          f"（規則表不改）：{[(p.pet_name(r), grade(r)[0]) for r in rows]}")
    n = 0
    for r in rows:
        got = take_pet(p, name, r)
        idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == got.get("uniqueIndex")), None)
        if idx is None:
            raise JobError(f"{name} 領回 {p.pet_name(r)} 後在寵物欄找不到它，不放生")
        do_release(p, name, idx, r.get("petId"))
        n += 1
    return n


def below_rows(p, names: List[str], min_diff: int = KEEP_DIFF, species=FIVE_DRAGONS) -> List[dict]:
    """身上＋個倉＋共倉裡 species、1 級、檔差 < min_diff 的寵物（用唯一編號記）。戰寵、騎寵、沒檔差的不算。"""
    from ..petgrade import grade
    out = []

    def add(who, where, pet, slot=None):
        _, diff = grade(pet)
        if pet.get("petId") in species and (pet.get("level") or 0) == 1 and diff is not None and diff < min_diff:
            out.append({"who": who, "where": where, "slot": slot, "diff": diff, "pet": pet})
    for n in names:
        fight, ride = p.locks(n)
        for i, v in slots(p, n).items():
            if v.get("uniqueIndex") not in (fight, ride):
                add(n, "身上", v, i)
        for r in p.pet_depot(n):
            add(n, "個倉", r)
    for r in p.pet_depot(names[0], public=True):
        add(None, "共倉", r)
    return out


def grade_name(diff: int) -> str:
    return f"＋{diff}檔" if diff >= 0 else f"{diff}檔"          # 半形 + 伺服器拒絕（錯誤 30859，11-01 實機）


def find_dragons(p, names: List[str]) -> List[dict]:
    """要練的五色人龍：1 級、檔差 ≥ +3，檔差高的在前。holder 是誰手上，public 是共倉。"""
    from ..petgrade import grade
    out = []

    def add(pet, holder, spot):
        _, diff = grade(pet)
        if pet.get("petId") in FIVE_DRAGONS and (pet.get("level") or 0) == 1 and diff is not None and diff >= KEEP_DIFF:
            out.append({"pet": pet, "diff": diff, "holder": holder, "spot": spot})
    for n in names:
        for v in slots(p, n).values():
            add(v, n, "身上")
        for r in p.pet_depot(n):
            add(r, n, "個倉")
    for r in p.pet_depot(names[0], public=True):
        add(r, None, "共倉")
    return sorted(out, key=lambda x: -x["diff"])


def rename_pet(p, name: str, uid: int, want: str) -> None:
    idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == uid), None)
    if idx is None:
        raise JobError(f"{name} 身上沒有寵物 {uid}，不能改名")
    if p.petinfo[name][idx].get("name") == want:
        return
    # 改名成功但伺服器不回 response（11-01 實機）→ send，看 Pet_PetInfo 的名字
    p.clients[name].send("Pet_ChangePetName", {"petIndex": idx, "petName": want})
    if not p.wait(lambda: p.petinfo[name].get(idx, {}).get("name") == want, 6):
        raise JobError(f"{name} 寵物改名「{want}」沒確認，停")
    p.log(f"{name} 寵物 {uid} 改名「{want}」")


def to_public(p, name: str, uid: int) -> None:
    idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == uid), None)
    if idx is None:
        raise JobError(f"{name} 身上沒有寵物 {uid}，不能存共倉")
    rest_pet(p, name, idx)
    r = p.clients[name].request("NewPet_SavePetToNewDepot", {"page": 1, "isPublic": True, "petIndex": idx}) or {}
    if not r.get("isOK") or not p.wait(lambda: uid not in {v.get("uniqueIndex") for v in slots(p, name).values()}, 6):
        raise JobError(f"{name} 寵物 {uid} 存共倉沒確認：{r}")


def share_pet(p, names: List[str], args) -> Tuple[str, dict]:
    uid, name = int(args.pet_uid), getattr(args, "pet_name", None)
    who = next((n for n in names if uid in {v.get("uniqueIndex") for v in slots(p, n).values()}), None)
    if who is None:
        raise JobError(f"寵物欄找不到寵物 {uid}，不動")
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply）：{who} 的寵物 {uid} 要改名「{name}」存共倉", {"mode": "share", "done": []}
    fight, ride = p.locks(who)
    if uid in (fight, ride):
        raise JobError(f"{who} 的寵物 {uid} 是鎖定的戰寵／騎寵，不存")
    if name:
        rename_pet(p, who, uid, name)
    to_public(p, who, uid)
    return f"{who} 的寵物 {uid} 已改名存共倉", {"mode": "share", "done": [who]}


def graduate(p, names: List[str], args) -> Tuple[str, dict]:
    """分數滿門檻的寵物「畢業」（使用者 11-05「分數滿1000 將寵物改名，存倉換寵」，選「個倉評分最高、未滿 1000 的金虎」）：
    身上未滿 140、評分 ≥ 門檻、不是騎寵的 → 改名成評分數字、存倉；牠是戰寵就先從個倉領評分最高、未滿門檻、
    2～139 級的金虎鎖成戰寵（騎寵不動）。140 級的不算（不是在練的）；個倉沒有合適金虎就停、不動任何寵物。"""
    need = int(getattr(args, "min_score", None) or GRADUATE_SCORE)
    plan = []
    for n in names:
        fight_u, ride_u = p.locks(n)
        grads = [v for v in slots(p, n).values() if (v.get("score") or 0) >= need
                 and (v.get("level") or 0) < MAX_PET_LV and v.get("uniqueIndex") != ride_u]
        if not grads:
            continue
        new = None
        if fight_u in {v.get("uniqueIndex") for v in grads}:
            pool = [r for r in p.pet_depot(n) if r.get("petId") == TIGER and 1 < (r.get("level") or 0) < MAX_PET_LV
                    and (r.get("score") or 0) < need]
            if not pool:
                raise JobError(f"{n} 個倉沒有 2～139 級、評分未滿 {need} 的金虎可以換戰寵，停（還沒動任何寵物）")
            new = max(pool, key=lambda r: (r.get("score") or 0, r.get("level") or 0))
        plan.append((n, grads, new))
    show = [(n, [(p.pet_name(v), v.get("level"), v.get("score")) for v in g],
             new and (p.pet_name(new), new.get("level"), new.get("score"))) for n, g, new in plan]
    p.log(f"寵物畢業（評分 ≥ {need}）：{show}")
    data = {"mode": "graduate", "plan": show, "done": []}
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply）：{len(plan)} 隻角色有寵物滿 {need} 分", data
    for n, grads, new in plan:          # 一隻角色出錯就停（JobError 往上丟）
        fight_u, ride_u = p.locks(n)
        if new is not None:
            got = take_pet(p, n, new)
            lock(p, n, got["uniqueIndex"], ride_u)
        for v in grads:
            rename_pet(p, n, v["uniqueIndex"], str(v.get("score")))
            idx = next(k for k, x in slots(p, n).items() if x.get("uniqueIndex") == v["uniqueIndex"])
            store_pet(p, n, idx)
        data["done"].append(n)
    return f"寵物畢業完成：{data['done']}（評分 ≥ {need} 改名存倉）", data


def assign_dragons(p, names: List[str], args) -> Tuple[str, dict]:
    """+3 檔以上的五色人龍依序分給五隻（檔差高的給排前面的人物），身上改名「+N檔」，準備練（使用者 2026-11-01
    「把人龍依序分配去練功」「上面人龍需改名 +x 檔」）。不在要的人身上就經共倉轉過去，全程用唯一編號。"""
    rows = find_dragons(p, names)
    if len(rows) != len(names):
        raise JobError(f"要 {len(names)} 隻人龍配 {len(names)} 隻人物，實際 {len(rows)} 隻，停")
    plan = [dict(x, to=n) for x, n in zip(rows, names)]
    show = [(x["to"], p.pet_name(x["pet"]), grade_name(x["diff"]), f"現在 {x['holder'] or ''}{x['spot']}") for x in plan]
    p.log(f"人龍分配：{show}")
    data = {"mode": "dragons", "plan": show, "done": []}
    if not getattr(args, "apply", False):
        return "只列人龍分配（沒加 --apply，沒有送任何動作）", data
    for x in plan:                                       # 先把要換手的存共倉，騰出寵物欄
        uid = x["pet"]["uniqueIndex"]
        if x["holder"] and x["holder"] != x["to"]:
            if x["spot"] == "個倉":
                take_pet(p, x["holder"], x["pet"])
            to_public(p, x["holder"], uid)
    for x in plan:
        uid, n = x["pet"]["uniqueIndex"], x["to"]
        if uid not in {v.get("uniqueIndex") for v in slots(p, n).values()}:
            row = next((r for r in p.pet_depots(n) if r.get("uniqueIndex") == uid), None)
            if row is None:
                raise JobError(f"找不到人龍 {uid}，停")
            take_pet(p, n, row)
        rename_pet(p, n, uid, grade_name(x["diff"]))
    return f"人龍分配完成：{show}", data


def keep_one_tiger(p, names: List[str], rows: List[dict]) -> List[dict]:
    """每隻角色留一隻 1 級金虎（轉生後當騎寵，`reset_pets_after_trans` 只領 1 級的）：
    身上、個倉都沒有放生名單以外的 1 級金虎，就從名單拿掉檔差最高的那隻。"""
    out = list(rows)
    for n in names:
        mine = [x for x in out if x["who"] == n and x["pet"].get("petId") == TIGER]
        if not mine:
            continue
        listed = {x["pet"].get("uniqueIndex") for x in mine}
        fight, ride = p.locks(n)
        have = [v for v in list(slots(p, n).values()) + p.pet_depot(n)
                if v.get("petId") == TIGER and (v.get("level") or 0) == 1
                and v.get("uniqueIndex") not in listed | {fight, ride}]
        if not have:
            out.remove(max(mine, key=lambda x: x["diff"]))
    return out


def release_below(p, names: List[str], args) -> Tuple[str, dict]:
    """五色人龍低於 +3 檔的全部放生（使用者 2026-11-01「<+3 檔的人龍丟棄」）。不可逆（回收站 48 小時）：
    先放身上的騰出寵物欄，再領倉庫的；每隻用唯一編號核對，任務條件／保護名單上的不動。"""
    from .clean import do_release
    from ..item_rules import load
    rules = load()
    # 五色人龍＋規則表 release_below3 的物種（使用者 11-09 金虎、金飛、紅暴、藍暴）
    species = set(FIVE_DRAGONS) | rules.ids("pet", {"release_below3"})
    rows = below_rows(p, names, species=species)
    rows = keep_one_tiger(p, names, rows)
    block = sorted({x["pet"].get("petId") for x in rows} & (set(rules.task_pets) | set(rules.protect)))
    show = [(x["who"] or "共倉", x["where"], p.pet_name(x["pet"]), f"{x['diff']:+d}") for x in rows]
    p.log(f"放生低於 +{KEEP_DIFF} 檔的 1 級寵物（五色人龍、規則表 release_below3）{len(rows)} 隻：{show}")
    if block:
        raise JobError(f"寵物 {block} 在任務條件／保護名單上，不放生，什麼都沒動")
    data = {"mode": "release-below", "count": len(rows), "rows": show, "done": []}
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply，沒有送任何動作）：要放生 {len(rows)} 隻", data
    if not getattr(args, "confirm_release", False):
        raise JobError("release-below 要加 --confirm-release（放生不可逆）；沒有送任何東西")
    rows.sort(key=lambda x: x["where"] != "身上")          # 身上先放，寵物欄才有空位領倉庫的
    done = 0
    for x in rows:
        who = x["who"] or next((n for n in names if len(slots(p, n)) < SLOT_MAX), None)
        if who is None:
            raise JobError(f"五隻寵物欄都滿了，領不了共倉的寵物（已放生 {done} 隻）")
        pet = x["pet"] if x["where"] == "身上" else take_pet(p, who, x["pet"])
        idx = next((k for k, v in slots(p, who).items() if v.get("uniqueIndex") == pet.get("uniqueIndex")), None)
        if idx is None:
            raise JobError(f"{who} 找不到要放生的 {p.pet_name(pet)}（已放生 {done} 隻）")
        do_release(p, who, idx, pet.get("petId"))
        done += 1
    return f"放生低於 +{KEEP_DIFF} 檔的 1 級寵物 {done} 隻", dict(data, released=done)


ACTIONS = {"park": do_park, "rest": do_rest, "restore": do_restore, "store-all": store_all,
           "swap-tiger": do_swap_tiger, "ride-train": do_ride_train, "refresh": do_refresh,
           "equip-fight": do_equip_fight}


def list_lines(p, names: List[str]) -> List[str]:
    out = []
    for n in names:
        onhand = [v for _, v in sorted(slots(p, n).items())]
        dep = p.pet_depot(n)
        fight, ride = p.locks(n)
        out.append(f"{n}｜寵物欄 {len(onhand)}/{SLOT_MAX} {_desc(p, onhand)}｜鎖定 戰 {fight}、騎 {ride}"
                   f"｜個倉 {len(dep)} 隻 {[(p.pet_name(r), r.get('petId'), r.get('level')) for r in dep]}")
        for v in onhand:               # 技能（使用者 2026-11-01「技能你幫看一下」）
            if v.get("skills"):
                out.append(f"{n}｜{p.pet_name(v)} 技能 "
                           f"{[(s.get('skillId'), s.get('skillLevel')) for s in v['skills']]}")
    if names:                          # 共倉同帳號共用，列一次
        pub = p.pet_depot(names[0], public=True)
        out.append(f"共倉 {len(pub)} 隻 {[(p.pet_name(r), r.get('petId'), r.get('level')) for r in pub]}")
    return out


def _grade_row(p, where: str, pet: dict) -> dict:
    from ..petgrade import forecast_score, grade
    label, diff = grade(pet)
    fc = forecast_score(pet) if (pet.get("level") or 0) < 140 else None
    return {"where": where, "name": p.pet_name(pet), "petId": pet.get("petId"), "level": pet.get("level"),
            "trans": pet.get("trans") or 0, "grade": label, "diff": diff,
            "args": [pet.get(k) for k in ("vitArg", "atkArg", "defArg", "dexArg")], "score": pet.get("score"),
            "forecast": fc and fc["score"]}


def forecast_text(r: dict) -> str:
    """「｜預估 140 級 1012」（使用者 11-08；116 筆實測平均誤差 1.6、最大 6）。推不出來（140 級、轉生寵、缺欄）回空字串。"""
    return f"｜預估 140 級 {r['forecast']}" if r.get("forecast") else ""


def grade_rows(p, names: List[str]) -> List[dict]:
    """身上＋個倉＋共倉（共倉只查一次）每隻寵物的檔次（petgrade：參考專案 psa-grow 同法）。只讀，不送改動。"""
    rows = []
    for n in names:
        with p.lock:
            onhand = [dict(v) for _, v in sorted(p.petinfo.get(n, {}).items()) if (v.get("petId") or 0) > 0]
        rows += [_grade_row(p, f"{n} 身上", v) for v in onhand]
        rows += [_grade_row(p, f"{n} 個倉", r) for r in p.pet_depot(n)]
    if names:
        rows += [_grade_row(p, "共倉", r) for r in p.pet_depot(names[0], public=True)]
    return rows


@lru_cache(None)
def _skill_names() -> Dict[int, str]:
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "SkillBattle.json"), encoding="utf-8") as fp:
        return {r["ID"]: r.get("nameTn") or "" for r in json.load(fp)}


def skill_text(skills) -> str:
    """技能名稱：SkillBattle 的 ID＝技能 ID×1000＋等級；表裡沒有就寫編號，不猜。"""
    rows = skills.values() if isinstance(skills, dict) else (skills or [])
    out = []
    for s in rows:
        sid, lv = int(s.get("skillId") or 0), int(s.get("skillLevel") or 0)
        if sid:
            out.append(_skill_names().get(sid * 1000 + lv) or f"{sid} Lv{lv}")
    return "、".join(out) or "無"


def info_rows(p, names: List[str]) -> List[dict]:
    """身上＋個倉＋共倉每隻寵物的完整資訊。檔次、評分沿用 grade_rows，其他欄位直接讀 PetInfo。只讀。"""
    rows = []
    def add(where: str, pet: dict) -> None:
        rows.append(dict(_grade_row(p, where, pet), pet=pet, species=p.pet_name({"petId": pet.get("petId")})))
    for n in names:
        with p.lock:
            onhand = [dict(v) for _, v in sorted(p.petinfo.get(n, {}).items()) if (v.get("petId") or 0) > 0]
        for v in onhand:
            add(f"{n} 身上", v)
        for r in p.pet_depot(n):
            add(f"{n} 個倉", r)
    if names:
        for r in p.pet_depot(names[0], public=True):
            add("共倉", r)
    return rows


def info_line(r: dict) -> str:
    x = r["pet"]
    g = lambda k: "—" if x.get(k) is None else x[k]    # noqa: E731
    v, a, d, s = r["args"]
    flags = "、".join(t for t, k in (("綁定", "bind"), ("上鎖", "petLock")) if x.get(k)) or "無"
    return (f"{r['where']}｜{r['species']}（{r['petId']}）暱稱 {r['name']}｜Lv{g('level')}/{g('maxLevel')} {g('trans')} 轉"
            f"｜{STATUS.get(x.get('status'), '休息')}｜HP {g('hp')}/{g('hpMax')} MP {g('mp')}/{g('mpMax')}"
            f"｜攻 {g('physicalAttack')} 防 {g('physicalDefense')} 敏 {g('agile')}"
            f"｜地 {g('earth')} 水 {g('water')} 火 {g('fire')} 風 {g('wind')}"
            f"｜暴擊 {g('physicalCrit')} 抗暴 {g('physicalResistCrit')} 暴傷 {g('physicalCritDamage')}"
            f" 命中 {g('physicalHit')} 閃避 {g('physicalDodge')} 穿透 {g('physicalPenetrate')} 反擊 {g('physicalBeatBack')}"
            f"｜檔次 {r['grade']} 體{v} 腕{a} 耐{d} 速{s}｜評分 {r['score']}{forecast_text(r)}"
            f"｜忠誠 {g('allegiance')} 星 {g('petStar')} 經驗 {g('exp')}｜{flags}"
            f"｜技能 {skill_text(x.get('skills'))}｜編號 {g('uniqueIndex')}")


def run(p, args) -> Tuple[str, dict]:
    mode = getattr(args, "mode", "list") or "list"
    names = names_of(p, args)
    if mode == "info":                 # 只讀：使用者 2026-11-02「列出寵物列表及完整資訊」（/show-pet-info）
        rows = info_rows(p, names)
        for r in rows:
            p.log(info_line(r))
        return f"寵物完整資訊：{len(rows)} 隻（只讀，沒有送任何改動）", {"mode": mode, "count": len(rows)}
    if mode == "grade":              # 只讀：使用者 2026-10-26「倉庫所有寵物做一次判斷，滿檔寫滿檔，缺檔寫 -n」
        rows = grade_rows(p, names)
        for r in rows:
            total = "" if r["diff"] is None else f"（總 {sum(r['args'])}）"
            v, a, d, x = r["args"]
            p.log(f"{r['where']}｜{r['name']}（{r['petId']}）Lv{r['level']}｜{r['grade']}{total}"
                  f"｜個體檔 體{v} 腕{a} 耐{d} 速{x}｜評分 {r['score']}{forecast_text(r)}")
        full = sum(1 for r in rows if r["grade"] == "滿檔")
        return f"寵物檔次：{len(rows)} 隻，滿檔 {full} 隻（只讀，沒有送任何改動）", {"mode": mode, "rows": rows}
    if mode == "release-below":
        return release_below(p, names, args)
    if mode == "share":                # 指定唯一編號的寵物改名、存共倉，當共用品（使用者 2026-11-01）
        return share_pet(p, names, args)
    if mode == "dragons":
        return assign_dragons(p, names, args)
    if mode == "graduate":
        return graduate(p, names, args)
    if mode == "list" or not getattr(args, "apply", False):
        for line in list_lines(p, names):
            p.log(line)
        tail = "" if mode == "list" else f"；沒加 --apply，{mode} 沒有執行"
        return f"只列寵物欄與個人寵物倉庫（沒有送任何改動）{tail}", {"mode": mode, "done": []}
    if mode == "release-public":       # 共倉共用：只用第一隻角色做一次
        if not getattr(args, "pet_id", None) or not getattr(args, "confirm_release", False):
            raise JobError("release-public 要給 --pet-id 和 --confirm-release（放生不可逆）；沒有送任何東西")
        n = release_public(p, names[0], args.pet_id, exception=getattr(args, "exception", None),
                           count=getattr(args, "count", None))
        return f"pets release-public 完成：{names[0]} 放生共倉寵物 {args.pet_id} {n} 隻", \
            {"mode": mode, "done": [names[0]], "released": n}
    if mode == "release-worst":        # 不分 petId，丟 1 級裡檔次最差的（使用者 2026-10-29「丟1等檔位最差的」）
        cnt = int(getattr(args, "count", 0) or 0)
        if cnt < 1 or not getattr(args, "confirm_release", False):
            raise JobError("release-worst 要給 --count 和 --confirm-release（放生不可逆）；沒有送任何東西")
        exc = getattr(args, "exception", None)
        counts = {"共倉": release_worst(p, names[0], cnt, exc, public=True)}
        for n in names:
            counts[n] = release_worst(p, n, cnt, exc, public=False)
        return f"pets release-worst 完成：放生 1 級檔次最差的 {counts}", \
            {"mode": mode, "done": list(counts), "released": sum(counts.values())}
    if mode == "release-private":      # 每隻角色放自己個倉的；一隻出錯就停
        if not getattr(args, "pet_id", None) or not getattr(args, "confirm_release", False):
            raise JobError("release-private 要給 --pet-id 和 --confirm-release（放生不可逆）；沒有送任何東西")
        counts = {}
        for n in names:
            counts[n] = release_public(p, n, args.pet_id, public=False, exception=getattr(args, "exception", None),
                                       count=getattr(args, "count", None))
        return f"pets release-private 完成：放生個倉寵物 {args.pet_id} {counts}", \
            {"mode": mode, "done": list(counts), "released": sum(counts.values())}
    act = ACTIONS.get(mode)
    done = []
    if mode == "deposit" and getattr(args, "public", False):
        # 共倉全帳號共用：送第一筆之前就算好放不放得下，不存一半才停
        from ..headless_party import PET_DEPOT_NUM
        want = sum(1 for n in names if any(v.get("petId") == args.pet_id for v in slots(p, n).values()))
        have = len(p.pet_depot(names[0], public=True))
        if have + want > PET_DEPOT_NUM:
            raise JobError(f"共倉第 1 頁 {have}/{PET_DEPOT_NUM}，放不下 {want} 隻（第 2 頁要鑽石解鎖，不碰）；沒有送任何東西")
    for n in names:                    # 一隻出錯就停（JobError 往上丟），後面的角色不做
        if mode == "store":
            do_store_slot(p, n, args)
        elif mode == "take":
            do_take(p, n, args.pet_id, getattr(args, "any_level", False),
                    getattr(args, "ride_pet_id", None))
        elif mode == "withdraw":
            do_withdraw(p, n, args.pet_id, int(args.pet_uid) if getattr(args, "pet_uid", None) else None)
        elif mode == "deposit":
            do_deposit(p, n, args.pet_id, public=bool(getattr(args, "public", False)),
                       uid=int(args.pet_uid) if getattr(args, "pet_uid", None) else None)
        elif mode == "ride":
            do_ride(p, n, int(args.pet_uid))
        elif mode == "retire":
            do_retire(p, n)
        else:
            act(p, n)
        p.log(f"{n} {mode} 完成｜寵物欄 {_desc(p, slots(p, n).values())}")
        done.append(n)
    return f"pets {mode} 完成：{done}", {"mode": mode, "done": done}
