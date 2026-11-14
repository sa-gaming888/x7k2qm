"""petgoal：同一種寵物依檔次各自練到目標，到了就改名存倉、從倉庫領下一隻接著練（使用者 2026-11-06）。

    python -X utf8 -m x7k2qm.autoplay.jobs petgoal --profile 2 --discover --pet-id 56            # 只列計畫
    python -X utf8 -m x7k2qm.autoplay.jobs petgoal --profile 2 --discover --pet-id 56 --apply
    ... --low-level 120 --cut 3 --score 1000 --name 1000 --start 海主人隧道Lv30                # 預設值

使用者原話：「邦浦洛斯 檔次<3 練到 120，存倉；檔次>=3 練到 1000分，並將名稱改 1000」。
- 檔差 < `--cut`：練到 `--low-level` 級 → 存倉（名字不改）。
- 檔差 ≥ `--cut`：練到伺服器評分 ≥ `--score` → 改名 `--name` → 存倉。
  練到 140 還不到分數 → 改名成評分數字存倉（同 `pets --mode retire`），不會一直練一隻不長經驗的。
- 檔次算不出來（無資料、轉生寵、異常）→ 停，不猜。
- `--sell-low`（使用者 11-06「+3 以下練的120 賣給寵物店」）：檔差 < `--cut` 練到 `--low-level` 的**不存倉、賣給
  薩村寵物店**（`jobs petsell` 的 `sell_targets`）。練功中到級的先改回戰寵、留在身上，等這輪結束（全隊停戰鬥）
  下一輪開始前一起賣（隊員的經共倉移給隊長）；賣失敗就整個停（送出後對不上是 JobHold，停在原地）。

每一輪：
1. 每隻角色身上有沒練完的那種寵物就練牠；沒有就從**自己的個倉**、再從**共倉**領一隻沒練完的（共倉的同一隻不會兩個人搶）。
2. 有寵物的角色一起練（`headless_party` 練寵模式：戰寵換成牠、換圖看寵物等級只往前）；沒有的角色照常用原本戰寵跟著打。
3. 某隻到目標：關遇敵 → 戰寵改回原本的 → 改名（要的話）→ 存倉（個倉滿才存共倉）→ **當場換下一隻**（`swap_in`）→ 其他人繼續練。
   換下一隻（使用者 11-13，LESSONS #203）：先領倉庫沒練完的指定寵物；沒有就從自己個倉拿沒滿 140 的頂替練（`fill_row`），
   頂替的滿 140 就收回再找下一隻；指定的都練完（只剩頂替的在練）→ 頂替的全收回、這輪結束。這輪一開始就沒寵物的角色也一樣。
   有 `--refill`、倉庫沒有指定的、這隻角色抓得到 → 不頂替，**這輪提早收**（還在練的留在身上），回到輪與輪之間補抓同種
   （使用者 11-14 同意）；頂替只用在抓不到的（沒開 --refill、指定野外抓不到的、到上限沒 --release-low）。
4. 這輪都收完 → 下一輪領下一批；倉庫裡也沒有沒練完的 → 結束。
練功自己停了（停止檔、連線中斷、補血失敗…）或 6 小時沒有任何一隻升級 → 整個停（規範第 2 點），還沒改回的戰寵改回原本的。

`--refill`（使用者 11-06「如果沒達標準人龍賣掉，或是已經達標滿分後，需要抓新的人龍來補」）：
第 4 步「倉庫裡也沒有沒練完的」的角色不再閒著，**在輪與輪之間**幫牠抓一隻新的同種（1 級）接著練，一直循環：
- 時機：這輪全部收完 → 賣（--sell-low）／存倉 → 每隻角色先領倉庫沒練完的 → 還是沒有的才抓。倉庫有就不抓。
- 地點：`--catch-at`（預設 地雷區Lv130，11-06 五組實機人龍都在這裡 15 秒內抓到）；不在那裡先全隊傳過去。
  抓完下一輪照舊從 `--start` 起練、換圖看寵物等級只往前（1 級的在地雷區上場會被秒，所以 `--start` 要在抓寵地點之前）。
- 一隻一隻抓（`jobs catch` 的 `catch_one`，不再從倉庫領同種——倉庫的已經由 fetch 挑過，練完的不能再領出來）。
  任何一隻抓不到、誘餌不夠、開不了 → JobError 整個停，不重試、後面的不抓（規範第 2 點）。
- 上限：這次執行最多補抓 `--refill-max` 隻（預設 20），到了就不再抓，剩下的練完就正常結束。
  有 `--release-low` 時（低檔放生也算額度）：到了上限還沒寵物練的角色**再抓 1 隻、不挑檔次、不放生**，
  照檔次練到分數或練到級賣（使用者 11-13「如果上限 30 次都沒抓到，就在抓一次隨便抓一隻去練」，LESSONS #203）；每輪都這樣，所以會一直輪下去。
- 誘餌不夠加 `--buy-bait` 才用石幣補買（`catch.buy_bait`，每個 5 萬、只花石幣）。
- 補抓為什麼在輪與輪之間：五隻是同一隊、在同一張圖；中途有人練完時隊伍多半在 120 級以上的圖，
  1 級新寵上場會被秒，抓寵又要離隊（整隊練功中斷）。所以補抓等輪結束；這輪剩下的時間由第 3 步的「當場換下一隻」補位，不讓角色空手。
"""

import argparse
import time
from typing import Dict, List, Optional, Tuple

from . import JobError, JobHold
from . import loyalty
from . import train as T
from .pets import MAX_PET_LV, rename_pet, slots, store_pet, take_pet
from ..petgrade import grade

NAME = "petgoal"
HELP = ("同一種寵物依檔次各自練到目標（檔差<3 練到 120 級、≥3 練到 1000 分改名），到了就存倉、領下一隻"
        "（--refill：倉庫沒有就抓新的補）")
PAUSE = True
TRAIN_LV = MAX_PET_LV + 1        # 交給練寵模式的目標等級：永遠到不了，收不收由這個工作自己判斷
CATCH_AT = "地雷區Lv130"          # --refill 預設抓寵地點（11-06 第二、三、七、八、十組人龍都在這裡抓到）
REFILL_MAX = 20                  # --refill 一次執行最多補抓幾隻（上限保護）
REFILL_LIMIT = 100               # --refill-max 能設的最大值
NO_WILD = {310}                  # 野外抓不到的（金虎是英雄島後傳 2032 任務給的）：補抓跳過
GRADE_WAIT = 5.0                 # 抓到後等幾秒看檔次欄位推齊（算不出檔次就停）
REFILL_TIMEOUT: Optional[float] = None   # 每隻最多等幾秒抓到；None＝同 jobs catch（240 秒）。測試調短用


def add_args(ap) -> None:
    ap.add_argument("--pet-id", dest="pet_id", required=True,
                    help="要練的寵物種類（petId，例：邦浦洛斯 56）；多種用逗號（例 54,55,56,57,481 五色人龍，使用者 11-11）")
    ap.add_argument("--cut", type=int, default=3, help="檔差門檻：< 這個練到 --low-level 級，≥ 這個練到 --score 分（預設 3）")
    ap.add_argument("--low-level", dest="low_level", type=int, default=120, help="檔差低的練到幾級就存倉（預設 120）")
    ap.add_argument("--score", type=int, default=1000, help="檔差高的練到伺服器評分幾分（預設 1000）")
    ap.add_argument("--name", default="1000", help="檔差高的練到分數後改成這個名字（預設 1000）")
    ap.add_argument("--start", default=T.START, help=f"每輪都從這個練級點開始（預設 {T.START}；換圖只往前）")
    ap.add_argument("--stop-file", default=None, help="停止檔（預設 <--log>.stop）；放了就正常結束")
    ap.add_argument("--sell-low", dest="sell_low", action="store_true",
                    help="檔差 < --cut、練到 --low-level 的不存倉，改賣給薩村寵物店（jobs petsell；不可逆）")
    ap.add_argument("--refill", action="store_true",
                    help="某角色練完（賣掉／存倉）而倉庫也沒有沒練完的同種 → 輪與輪之間幫牠抓一隻新的接著練（預設關）")
    ap.add_argument("--refill-max", dest="refill_max", type=int, default=REFILL_MAX,
                    help=f"--refill 這次執行最多補抓幾隻（預設 {REFILL_MAX}，最多 {REFILL_LIMIT}）")
    ap.add_argument("--catch-at", dest="catch_at", default=CATCH_AT,
                    help=f"--refill 抓寵地點（預設 {CATCH_AT}；不在那裡先全隊傳過去）")
    ap.add_argument("--buy-bait", dest="buy_bait", action="store_true",
                    help="--refill 誘餌不夠時用石幣補買（每個 50,000 石幣、只花石幣，同 jobs catch --buy-bait）")
    ap.add_argument("--jump", default="0",
                    help="跳級：寵物等級加這麼多再判斷換圖門檻（例 40：80 級就進地雷區Lv130；使用者 11-10「練級要盡可能用跳級去練」）；"
                         "auto：從 40 起看補血次數自己調（使用者 11-13「高等陪低等練輸出才會快…跳級練」）")
    ap.add_argument("--fruit", action=argparse.BooleanOptionalAction, default=True,
                    help="練寵中經驗果加成剩不到 1 小時就自己吃，沒有就用石幣買（Party.autofruit；使用者 11-11「練寵都沒有吃果子」、"
                         "11-12 預設開），--no-fruit 關掉")
    ap.add_argument("--uids", default="",
                    help="只練這幾隻（寵物唯一編號，逗號分隔；例：只練預估到得了 1100 的黑豹）。給了就不補抓戰寵")
    ap.add_argument("--ride-ids", dest="ride_ids", default="",
                    help="騎寵也照檔次練：這幾種（petId，逗號分隔，例 307,308）輪與輪之間換上沒練完的當騎寵，"
                         "到目標就跟戰寵一樣改名存倉／賣（使用者 11-10「統一 騎老虎 練人龍」）")
    ap.add_argument("--ride-cut", dest="ride_cut", type=int, default=None, help="騎寵的檔差門檻（預設同 --cut）")
    ap.add_argument("--ride-score", dest="ride_score", type=int, default=1000, help="騎寵檔差高的練到幾分（預設 1000）")
    ap.add_argument("--ride-name", dest="ride_name", default="1000", help="騎寵到分數後改成這個名字（預設 1000）")
    ap.add_argument("--assign", default="",
                    help="每隻人物固定練哪種：人物:戰寵petId:騎寵petId，逗號分隔；人物寫第幾隻（1～5，照隊伍順序）或角色名"
                         "（例 1:54:307,2:481:310；使用者 11-12「每一隻人物負責一種老虎及人龍」）。"
                         "戰寵要在 --pet-id、騎寵要在 --ride-ids 裡；騎寵寫 0＝照 --ride-ids 不限")
    ap.add_argument("--release-low", dest="release_low", action="store_true",
                    help="--refill 抓到檔差 < --cut 的（1 級）當場放生、再抓下一隻，只有 ≥ --cut 的才練"
                         "（使用者 11-11「缺的去抓 要+3 以上才練」；只限五色人龍與規則表 release_below3 的物種，放生不可逆）")
    ap.add_argument("--refill-want", dest="refill_want", type=int, default=0,
                    help="--refill 這次留下（要練）幾隻就不再抓（預設 0＝不限，只看 --refill-max）")
    ap.add_argument("--ride-refill", dest="ride_refill", action="store_true",
                    help="騎寵倉庫也沒有沒練完的 → 輪與輪之間依 --ride-ids 輪流抓一隻新的（額度跟 --refill-max 共用）")


def id_list(text) -> List[int]:
    return [int(x) for x in str(text or "").replace("，", ",").split(",") if x.strip()]


def uid_set(args) -> set:
    return set(id_list(getattr(args, "uids", "")))


def ride_ids(args) -> List[int]:
    return id_list(getattr(args, "ride_ids", ""))


def pet_ids(args) -> Tuple[int, ...]:
    """--pet-id 的種類：一種或逗號分隔多種（使用者 11-11「倉庫 +3 人龍拿出來練到達分」：五色人龍一起練）。"""
    v = args.pet_id
    if isinstance(v, (tuple, list)):
        return tuple(int(x) for x in v)
    return tuple(id_list(v))


def parse_assign(args) -> List[Tuple[str, int, int]]:
    """--assign → [(人物, 戰寵 petId, 騎寵 petId 或 0)]；人物是 "1"～"5" 或角色名。格式錯丟 JobError。"""
    out = []
    for part in str(getattr(args, "assign", "") or "").replace("，", ",").split(","):
        if not part.strip():
            continue
        bits = [b.strip() for b in part.split(":")]
        if len(bits) != 3 or not bits[0] or not bits[1].isdigit() or not bits[2].isdigit():
            raise JobError(f"--assign 每一段要寫 人物:戰寵petId:騎寵petId：{part}")
        out.append((bits[0], int(bits[1]), int(bits[2])))
    if len({who for who, _, _ in out}) != len(out):
        raise JobError("--assign 同一隻人物寫了兩次")
    return out


def assigned(args) -> Dict[str, Tuple[int, int]]:
    """run 開頭解析好的 角色名 → (戰寵, 騎寵)；沒給 --assign 是空的。"""
    return getattr(args, "_assign", None) or {}


def resolve_assign(names: List[str], args) -> Dict[str, Tuple[int, int]]:
    out = {}
    for who, f, r in parse_assign(args):
        if who.isdigit():
            i = int(who)
            if not 1 <= i <= len(names):
                raise JobError(f"--assign 第 {i} 隻人物不存在（隊伍 {len(names)} 隻）")
            who = names[i - 1]
        elif who not in names:
            raise JobError(f"--assign 的人物 {who} 不在隊伍裡：{names}")
        if who in out:
            raise JobError(f"--assign 的人物 {who} 寫了兩次")
        out[who] = (f, r)
    return out


def rids_of(args, name: Optional[str]) -> List[int]:
    """這隻角色的騎寵要練哪幾種：--assign 有指定就只那一種，沒有照 --ride-ids。"""
    a = assigned(args).get(name) if name else None
    if a and a[1]:
        return [a[1]]
    return ride_ids(args)


def train_key(args, names: Optional[List[str]] = None):
    """交給練寵模式的 petId：一種給 int（行為同以前），多種給 tuple（`Party.pet_index` 本來就收 tuple）；
    --assign 給 dict 角色→petId（每隻只找自己那種，身上別種人龍不會被挑去練）。"""
    a = assigned(args)
    if a:
        ids = pet_ids(args)
        return {n: (a[n][0] if n in a else ids) for n in (names or a)}
    ids = pet_ids(args)
    return ids[0] if len(ids) == 1 else ids


def ride_goal(args):
    """騎寵用的目標：跟戰寵同一套 is_done／to_sell／finish，只換門檻、分數、名字（等級、賣不賣照戰寵的）。"""
    from types import SimpleNamespace
    cut = getattr(args, "ride_cut", None)
    return SimpleNamespace(pet_id=None, ride=True, cut=args.cut if cut is None else int(cut),
                           score=int(getattr(args, "ride_score", 1000)), name=str(getattr(args, "ride_name", "1000")),
                           low_level=args.low_level, sell_low=bool(getattr(args, "sell_low", False)))


def mine(v: dict, args, name: Optional[str] = None) -> bool:
    """這隻是不是這個工作要練的（騎寵目標看 --ride-ids；戰寵看 --pet-id，給了 --uids 只算那幾隻）。
    --assign：name 那隻角色只算指定的那一種（name 是 None＝共倉列表，照 --pet-id）。"""
    if getattr(args, "ride", False):
        return False
    if v.get("petId") not in pet_ids(args):
        return False
    a = assigned(args).get(name) if name else None
    if a and v.get("petId") != a[0]:
        return False
    if (v.get("trans") or 0) >= 1:
        # 轉生過的同種不是練寵目標（檔次不跟 0 轉物種檔比，算不出來）：略過，不停。
        # 11-10 23:02 第十組改練 307，倉庫一隻 1 轉的 307「檔次算不出來」整組停（LESSONS #190）；
        # 0 轉卻算不出來（無資料、異常）照舊停
        return False
    only = uid_set(args)
    return not only or v.get("uniqueIndex") in only


def refilling(args) -> bool:
    return bool(getattr(args, "refill", False))


def jump_of(args) -> int:
    """--jump 的起始值：數字照用，auto 從 Party.JUMP_AUTO_START 起。"""
    v = str(getattr(args, "jump", "0") or "0").strip().lower()
    if v == "auto":
        from ..headless_party import Party
        return Party.JUMP_AUTO_START
    try:
        return max(0, int(v))
    except ValueError:
        raise JobError(f"--jump 要是數字或 auto（收到 {v!r}）")


def check(args) -> None:
    jump_of(args)
    if not 1 < int(args.low_level) <= MAX_PET_LV:
        raise JobError(f"--low-level 要在 2～{MAX_PET_LV}")
    from .petsell import MIN_LEVEL
    if getattr(args, "sell_low", False) and int(args.low_level) < MIN_LEVEL:
        raise JobError(f"--sell-low 時 --low-level 不能低於 {MIN_LEVEL}（店長只收 {MIN_LEVEL} 級以上）")
    if int(args.score) <= 0 or not str(args.name or "").strip():
        raise JobError("--score 要大於 0、--name 不能空白")
    from ..supervisor import ROUTE
    if args.start not in {n for n, _, _ in ROUTE}:      # 換圖照練功路線往前，路線外的地點不行
        raise JobError(f"--start 不是練功路線上的地點：{args.start}")
    try:
        ids = pet_ids(args)
    except ValueError:
        raise JobError(f"--pet-id 要是數字，多種用逗號分隔：{args.pet_id}")
    if not ids or min(ids) <= 0:
        raise JobError(f"--pet-id 要大於 0：{args.pet_id}")
    both = set(ids) & set(ride_ids(args))
    if both:
        raise JobError(f"--ride-ids 不能包含要練的戰寵 {sorted(both)}")
    plan_a = parse_assign(args)
    for who, f, r in plan_a:
        if f not in ids:
            raise JobError(f"--assign {who} 的戰寵 {f} 不在 --pet-id {list(ids)} 裡")
        if r and r not in ride_ids(args):
            raise JobError(f"--assign {who} 的騎寵 {r} 不在 --ride-ids {ride_ids(args)} 裡")
    if plan_a and uid_set(args):
        raise JobError("--assign 跟 --uids 不能一起用")
    if len(ids) > 1 and refilling(args) and not plan_a:
        raise JobError("--pet-id 多種時不能 --refill（不知道要抓哪一種；用 --assign 指定每隻抓哪種）")
    if refilling(args) and uid_set(args):
        raise JobError("--uids 只練指定的那幾隻，不能同時 --refill（補抓的不會在名單裡）")
    if getattr(args, "ride_refill", False) and not ride_ids(args):
        raise JobError("--ride-refill 要配 --ride-ids")
    if (getattr(args, "release_low", False) or int(getattr(args, "refill_want", 0) or 0)) and not refilling(args):
        raise JobError("--release-low、--refill-want 要配 --refill")
    if int(getattr(args, "refill_want", 0) or 0) < 0:
        raise JobError("--refill-want 不能是負的")
    if getattr(args, "release_low", False):
        from ..item_rules import load
        from .pets import FIVE_DRAGONS
        rules = load()
        ok = set(FIVE_DRAGONS) | rules.ids("pet", {"release_below3"})
        bad = sorted(set(ids) - ok) + sorted(set(ids) & (set(rules.task_pets) | set(rules.protect)))
        if bad:
            raise JobError(f"--release-low 只放生五色人龍與規則表 release_below3 的物種（不在任務條件／保護名單）：{bad} 不行")
    if refilling(args) or getattr(args, "ride_refill", False):
        from ..headless_party import NAME_TO_POINT
        at = getattr(args, "catch_at", CATCH_AT)
        if at not in NAME_TO_POINT:
            raise JobError(f"--catch-at 不是能傳送的練級點：{at}")
        if not 1 <= int(getattr(args, "refill_max", REFILL_MAX)) <= REFILL_LIMIT:
            raise JobError(f"--refill-max 要在 1～{REFILL_LIMIT}")
        order = [n for n, _, _ in ROUTE]
        if at in order and order.index(args.start) >= order.index(at):
            # 補抓的都是 1 級：從抓寵地點（或更後面）起練會被秒
            raise JobError(f"--refill 時 --start（{args.start}）要在抓寵地點 {at} 之前（新抓的是 1 級）")


def diff_of(pet: dict) -> int:
    label, d = grade(pet)
    if d is None:
        raise JobError(f"{pet.get('name') or pet.get('petId')}（{pet.get('uniqueIndex')}）檔次算不出來：{label}，停")
    return d


def goal_text(pet: dict, args) -> str:
    if diff_of(pet) >= args.cut:
        return f"練到 {args.score} 分改名「{args.name}」"
    return f"練到 {args.low_level} 級" + ("賣給寵物店" if getattr(args, "sell_low", False) else "")


def is_done(pet: dict, args) -> bool:
    lv = pet.get("level") or 0
    if diff_of(pet) < args.cut:
        return lv >= args.low_level
    return (pet.get("score") or 0) >= args.score or pet.get("name") == args.name or lv >= MAX_PET_LV


def on_body(p, name: str, args) -> List[dict]:
    return [v for v in slots(p, name).values() if mine(v, args, name)]


def to_sell(pet: dict, args) -> bool:
    """--sell-low：檔差低、練到級的那隻要賣，不存倉（使用者 11-06「+3 以下練的120 賣給寵物店」）。"""
    return bool(getattr(args, "sell_low", False)) and diff_of(pet) < args.cut and is_done(pet, args)


def free_for_sale(p, n: str, uid: int) -> None:
    """要賣的這隻還是戰寵（身上只有人龍和騎寵時，原本的戰寵就是牠自己，練完沒得換回）：解掉戰寵鎖定、改休息。
    11-07 18:38 第三組角色52 120 級人龍「出戰中，不賣」整組停（LESSONS #181）。"""
    from .pets import rest_pet, unlock
    fight, ride = p.locks(n)
    with p.lock:
        idx = next((i for i, v in p.petinfo[n].items() if v.get("uniqueIndex") == uid), None)
    if fight == uid:
        unlock(p, n, 0, 0 if ride == uid else ride)
        p.log(f"寵物目標：{n} 要賣的 {uid} 還是戰寵，先解掉戰寵鎖定")
    elif ride == uid:
        # 改練另一種時，以前當騎寵練的同種到級要賣，牠還鎖著騎寵、騎乘中：只解戰寵的舊版改休息沒確認整組停
        # （11-10 23:06 第十組改練 307，角色54騎著的 307「改休息沒確認」，LESSONS #191）。騎寵空著，下一步 ride_step 補
        unlock(p, n, fight, 0)
        p.log(f"寵物目標：{n} 要賣的 {uid} 還是騎寵，先解掉騎寵鎖定")
    if idx is not None:
        rest_pet(p, n, idx)


def sell(p, targets: List[Tuple[str, dict]], args) -> List[str]:
    """這些（角色, 寵物）賣給寵物店（全隊已停戰鬥；隊員的經共倉移給隊長）。失敗丟 JobError／JobHold，整個停。"""
    from .petsell import sell_targets
    # 先確定全隊在同一隊：賣之前只有隊長傳回薩村，隊伍散了隊員不會跟過去
    # （11-07 16:02 改版重開，伺服器拆了隊伍，一開頭就賣 → 「傳到薩姆吉爾村失敗 isOK True」，第二、九、十組停，LESSONS #180）
    if not p.form_team():
        raise JobError("賣寵物前組不起隊，不賣")
    for n, v in targets:
        free_for_sale(p, n, v["uniqueIndex"])
    gain = {x["uid"]: x["gain"] for x in sell_targets(p, [(n, v["uniqueIndex"]) for n, v in targets])}
    out = [f"{n} {diff_of(v):+d} 檔 {v.get('level')} 級 → 賣 +{gain[v['uniqueIndex']]}" for n, v in targets]
    for m in out:
        p.log(f"寵物目標：{m}")
    return out


def finish(p, name: str, pet: dict, args) -> str:
    """到目標的那隻：改名（檔差高的）→ 存倉。回傳一句結果。牠必須已經不是鎖定的戰寵／騎寵。"""
    uid = pet["uniqueIndex"]
    fight, ride = p.locks(name)
    if uid == ride:
        raise JobError(f"{name} 寵物 {uid} 是鎖定的騎寵，不存")
    if uid == fight:
        # 身上只有人龍和騎寵時，原本的戰寵就是牠自己（#179），「改回原本的」還是牠 → 跟賣的那條一樣先解鎖、改休息再存
        # （11-08 15:27 第九組角色28、15:46 第二組角色57人龍到 1000 分改名存倉時「還是鎖定的戰寵」整組停，LESSONS #185）
        free_for_sale(p, name, uid)
    d = diff_of(pet)
    if d >= args.cut:
        new = args.name if (pet.get("score") or 0) >= args.score else str(pet.get("score") or "")
        if new:
            rename_pet(p, name, uid, new)
    idx = next(k for k, v in slots(p, name).items() if v.get("uniqueIndex") == uid)
    store_pet(p, name, idx)
    msg = f"{name} {d:+d} 檔 {pet.get('level')} 級 評分 {pet.get('score')} → 存倉"
    p.log(f"寵物目標：{msg}")
    return msg


def todo_rows(p, name: str, args, claimed: set, public: bool) -> List[dict]:
    return [r for r in p.pet_depot(name, public=public)
            if mine(r, args, name) and r.get("uniqueIndex") not in claimed and not is_done(r, args)]


def fetch_ready(p, name: str, args, claimed: set) -> bool:
    """個倉或共倉有沒有沒練完的指定寵物（只查、不領）。"""
    return any(todo_rows(p, name, args, claimed, public) for public in (False, True))


def fetch(p, name: str, args, claimed: set) -> Optional[dict]:
    """從個倉、再從共倉領一隻沒練完的（共倉裡別人這輪領過的不算）。"""
    for public in (False, True):
        rows = todo_rows(p, name, args, claimed, public)
        if rows:
            row = max(rows, key=lambda r: (diff_of(r), r.get("level") or 0))      # 檔次高的先練
            claimed.add(row["uniqueIndex"])
            return take_pet(p, name, row)
    return None


NO_FILL = (1479,)                # 拿倉庫寵物頂替時不拿的種類：瑪蕾菲雅（女寵，打靈石用）
DIGIGUS = 1589                   # 迪基格斯：人物五轉的組禁止練（使用者 11-10「如果人物五轉 迪基格斯 禁止練」）


def no_fill_ids(p) -> tuple:
    """這組不拿來頂替的種類：女寵；隊裡有人五轉再加迪基格斯（11-13 第七組角色68、第八組角色69被頂替成 1589，LESSONS #204）。"""
    if any((v or 0) >= 5 for v in getattr(p, "trans", {}).values()):
        return NO_FILL + (DIGIGUS,)
    return NO_FILL


def fill_row(p, name: str, args, claimed: set) -> Optional[dict]:
    """沒有指定的寵物可練時，**自己個倉**裡沒滿 140、等級最高的那隻（使用者 11-13「如果沒有指定的寵物可以練，
    就拿倉庫沒滿140的練」）。不拿：共倉（132 共用寵物等別人的東西在那）、女寵、五轉組的迪基格斯、上鎖的、
    已經練完的指定種類（拿出來馬上又會被收回去）。"""
    rows = []
    skip = no_fill_ids(p)
    for r in p.pet_depot(name):
        if (r.get("level") or 0) >= MAX_PET_LV or r.get("uniqueIndex") in claimed or r.get("petLock") == 1 \
                or r.get("petId") in skip:
            continue
        # 指定練的那幾種、騎寵那幾種照各自的規則（--uids 外的、練完的、騎寵候選都不拿來頂替）
        if r.get("petId") in pet_ids(args) or r.get("petId") in rids_of(args, name):
            continue
        rows.append(r)
    return max(rows, key=lambda r: r.get("level") or 0) if rows else None


def swap_in(p, name: str, args, key, claimed: set, fills: Optional[Dict[str, int]]) -> bool:
    """練功中（已關遇敵、不在戰鬥）幫這隻角色換上下一隻要練的：先領倉庫沒練完的指定寵物（fetch），
    沒有就拿個倉沒滿 140 的（fill_row，記在 fills）。換上後加進 orig_fight／train_uid，跟一開始就在練的一樣收。
    身上 5 格滿了、倉庫都沒有 → 回 False（照舊用原本戰寵跟著打）。
    舊版要等這一輪其他人都練完才領下一隻：11-12 21:08 第二組角色42布魯頓存倉後空手跟隊 13 小時（LESSONS #203）。"""
    from .catch import SLOT_MAX
    if len(slots(p, name)) >= SLOT_MAX:
        p.log(f"寵物目標：{name} 身上 {SLOT_MAX} 格滿了，領不出下一隻，先用原本戰寵跟著打")
        return False
    pet = fetch(p, name, args, claimed)
    what = "指定的"
    if pet is None:
        if fills is None:                          # 指定的都沒人在練了：不拿頂替的
            return False
        row = fill_row(p, name, args, claimed)
        if row is None:
            return False
        claimed.add(row["uniqueIndex"])
        pet = take_pet(p, name, row)
        fills[name] = pet["uniqueIndex"]
        what = "（沒有指定的可練）個倉沒滿 140 的"
    fight, ride = p.locks(name)
    if fight == pet["uniqueIndex"]:
        fight = 0
    p.orig_fight[name] = (fight, ride)
    p.train_uid[name] = pet["uniqueIndex"]
    p.train_skip.discard(name)
    if not p.set_fight_pet(name, pet["uniqueIndex"], ride):
        raise JobError(f"{name} 換上下一隻要練的 {pet['uniqueIndex']} 沒確認")
    p.log(f"寵物目標：{name} 接著練{what} {p.pet_name(pet)}（{pet.get('petId')}）{pet.get('level')} 級，唯一編號 {pet['uniqueIndex']}")
    return True


def drop_fill(p, name: str, fills: Dict[str, int]) -> str:
    """頂替的那隻收掉（練到 140、或這輪指定的都練完了）：戰寵改回原本的 → 存回倉庫。"""
    from .pets import rest_pet
    uid = fills.pop(name)
    fight, ride = p.orig_fight.pop(name, (0, 0))
    p.train_uid.pop(name, None)
    if fight or ride:
        if not p.set_fight_pet(name, fight, ride):
            raise JobError(f"{name} 頂替的寵物收掉時戰寵改不回 {fight}")
    else:
        from .pets import unlock
        unlock(p, name, 0, ride)
    idx = next((k for k, v in slots(p, name).items() if v.get("uniqueIndex") == uid), None)
    if idx is None:
        return f"{name} 頂替的 {uid} 不在身上了"
    pet = slots(p, name)[idx]
    rest_pet(p, name, idx)
    store_pet(p, name, idx)
    msg = f"{name} 頂替的 {p.pet_name(pet)}（{pet.get('petId')}）練到 {pet.get('level')} 級 → 存倉"
    p.log(f"寵物目標：{msg}")
    return msg


def go_catch_place(p, who: List[str], place: str) -> None:
    """要抓的角色都在抓寵地點就不動；不在就先關遇敵、全隊傳過去（傳不過去就停）。"""
    from ..headless_party import NAME_TO_MAP
    mapid = NAME_TO_MAP[place]
    with p.lock:
        there = all(p.s.member_maps.get(n) == mapid for n in who)
    if there:
        return
    p.log(f"寵物目標：補抓前全隊傳到 {place}")
    if not p.pause() or not p.teleport_team(place):
        raise JobError(f"補抓：傳不到 {place}，不抓")


def keep_of(p, name: str, args) -> set:
    """補抓時寵物欄放不下也不能存掉的：身上正在練（沒練完）的戰寵目標、騎寵目標（LESSONS #188）。"""
    fight, _ = p.locks(name)
    rids, g = rids_of(args, name), ride_goal(args)
    out = set()
    for v in slots(p, name).values():
        if mine(v, args, name) and grade(v)[1] is not None and not is_done(v, args):
            out.add(v.get("uniqueIndex"))
        elif rids and ride_ok(v, rids, g, fight):
            out.add(v.get("uniqueIndex"))
    return out


def park_unlisted(p, names: List[str], args) -> None:
    """--uids：身上同種、不在名單上的先存倉——練寵模式照物種挑戰寵（pet_index），同種兩隻會挑錯
    （11-10 第九組角色28身上 +3 黑豹 169453 和 +0 的 169423，換成了 169423，LESSONS #188）。鎖定中的不動。"""
    only = uid_set(args)
    if not only:
        return
    for n in names:
        fight, ride = p.locks(n)
        for idx, v in list(slots(p, n).items()):
            u = v.get("uniqueIndex")
            if v.get("petId") in pet_ids(args) and u not in only and u not in (fight, ride):
                p.log(f"寵物目標：{n} 身上同種不在名單的 {u}（{v.get('level')} 級）先存倉，免得練錯隻")
                store_pet(p, n, idx)


def share_unassigned(p, names: List[str], args) -> List[str]:
    """--assign 開頭做一次：每隻角色身上（休息中）和個倉裡、**指定給別人**的那幾種、還沒練完的，移到共倉，
    讓負責那種的角色從共倉領去練（個倉別人領不到）。共倉第 1 頁滿了就不再移（記日誌、不停）；
    要從個倉領出來但身上 5 格滿的那隻跳過。回傳移了哪些。"""
    from ..headless_party import PET_DEPOT_NUM
    from .catch import SLOT_MAX
    from .pets import to_public
    a = assigned(args)
    g = ride_goal(args)
    fights = {f for f, _ in a.values()}
    rides = {r for _, r in a.values() if r}
    moved: List[str] = []

    def other(n, v):
        pid, (f, r) = v.get("petId"), a.get(n, (0, 0))
        if (v.get("trans") or 0) >= 1 or grade(v)[1] is None:
            return False
        if pid in fights and pid != f:
            return not is_done(v, args)
        if pid in rides and pid != r:
            return not is_done(v, g)
        return False
    for n in names:
        if n not in a:
            continue
        fight, ride = p.locks(n)
        todo = [("身上", v) for v in slots(p, n).values()
                if v.get("uniqueIndex") not in (fight, ride, None, 0) and v.get("status") not in (2, 3) and other(n, v)]
        todo += [("個倉", r) for r in p.pet_depot(n) if other(n, r)]
        for where, v in todo:
            if len(p.pet_depot(names[0], public=True)) >= PET_DEPOT_NUM:
                p.log(f"寵物目標：共倉第 1 頁滿了，{n} {where}的 {v.get('uniqueIndex')}（{v.get('petId')}）先不移")
                return moved
            if where == "個倉":
                if len(slots(p, n)) >= SLOT_MAX:
                    p.log(f"寵物目標：{n} 身上滿了，個倉的 {v.get('uniqueIndex')}（{v.get('petId')}）先不移")
                    continue
                take_pet(p, n, v)
            to_public(p, n, v["uniqueIndex"])
            moved.append(f"{n} {where} {v['uniqueIndex']}（{v.get('petId')}）→ 共倉")
            p.log(f"寵物目標：{moved[-1]}（指定給別人練）")
    return moved


def can_refill(name: str, args, state: dict) -> bool:
    """--refill 會不會幫這隻角色抓新的戰寵（同 refill 的篩選：沒指定、指定野外抓不到的、留夠了、
    到上限又沒 --release-low 的都不抓）。只判斷、不送封包。"""
    if not refilling(args):
        return False
    a = assigned(args)
    if a and name not in a:
        return False
    sp = a.get(name, (0, 0))[0]
    if sp in NO_WILD or not (sp or [s for s in pet_ids(args) if s not in NO_WILD]):
        return False
    wanted = int(getattr(args, "refill_want", 0) or 0)
    if wanted and state.get("kept", 0) >= wanted:
        return False
    return state["caught"] < int(getattr(args, "refill_max", REFILL_MAX)) or bool(getattr(args, "release_low", False))


def of_species(p, name: str, species: int) -> List[dict]:
    return [v for v in slots(p, name).values() if v.get("petId") == species]


def refill(p, who: List[str], args, state: dict, species: Optional[List[int]] = None, goal=None) -> List[str]:
    """--refill：這些角色身上、倉庫都沒有沒練完的 → 各抓一隻新的同種。回傳抓到的角色。
    一隻一隻抓；任何一隻抓不到／出錯就丟 JobError（不重試、後面的不抓）。最多抓到 --refill-max 隻。
    騎寵（--ride-refill）：species 給 --ride-ids，每抓一隻輪到下一種；goal 是騎寵的目標。"""
    from .catch import CATCH_TIMEOUT, SLOT_MAX, catch_one
    species = species or list(pet_ids(args))
    goal = goal or args
    ride = bool(getattr(goal, "ride", False))
    a = assigned(args)
    if a:
        # --assign：每隻抓自己指定的那種；沒指定的角色不抓（騎寵指定 0 的照 --ride-ids 輪流）
        who = [n for n in who if n in a and (not ride or a[n][1] or ride_ids(args))]
        if not who:
            return []
    # 野外抓不到的不補抓：11-13 一組 --assign 指定 310 的騎寵練完，每次在地雷區等 9 分鐘抓不到 → 整個工作停、
    # 戰寵也沒照跳級練，30 分鐘後重做又停。那隻角色就不補騎寵，其他照抓
    species = [s for s in species if s not in NO_WILD]
    k = 1 if ride else 0
    wild = [n for n in who if a.get(n, (0, 0))[k] not in NO_WILD]
    if len(wild) < len(who) and not state.get("no_wild_log"):
        state["no_wild_log"] = True
        p.log(f"寵物目標：{'、'.join(n for n in who if n not in wild)} 指定的{'騎寵' if ride else '戰寵'}野外抓不到"
              f"（{'、'.join(map(str, sorted(NO_WILD)))} 是任務給的），不補抓")
    who = [n for n in wild if a.get(n, (0, 0))[k] or species]
    if not who:
        return []
    cap = int(getattr(args, "refill_max", REFILL_MAX))
    # 到上限不再讓角色空手（使用者 11-13「如果上限 30 次都沒抓到，就在抓一次隨便抓一隻去練，根據檔次決定練到達分or 賣」）：
    # 每次補抓、每隻還沒寵物練的角色在額度用完後**再抓 1 隻、不放生**，照檔次走原本的目標（≥ --cut 練到分數、< --cut 練到級賣／存）。
    # 舊版到上限就 return，11-13 第七組角色51補抓邦奇諾 30 隻都低於 +3 全放生，之後整輪空手跟隊（LESSONS #203）
    release = bool(getattr(args, "release_low", False)) and not ride     # 騎寵照舊練到 120 賣
    wanted = 0 if ride else int(getattr(args, "refill_want", 0) or 0)
    if wanted and state.get("kept", 0) >= wanted:
        if not state.get("wanted_log"):
            state["wanted_log"] = True
            p.log(f"寵物目標：補抓已留下 {wanted} 隻要練的，不再抓（{'、'.join(who)} 沒寵物練）")
        return []
    go_catch_place(p, who, getattr(args, "catch_at", CATCH_AT))
    got: List[str] = []
    timeout = REFILL_TIMEOUT or CATCH_TIMEOUT
    role = "騎寵" if ride else ""
    for n in who:
        last = False                   # 額度用完後的最後一隻：不管檔次都留下
        while True:                    # --release-low：低檔的放生後同一隻角色再抓，直到留下一隻或額度用完
            if wanted and state.get("kept", 0) >= wanted:
                state["wanted_log"] = True
                p.log(f"寵物目標：補抓已留下 {wanted} 隻要練的，不再抓")
                break
            if state["caught"] >= cap:
                if last or not release:        # 沒有 --release-low：抓到的本來就全留，到上限照舊停
                    if not state["capped"]:
                        state["capped"] = True
                        p.log(f"寵物目標：補抓已達上限 {cap} 隻，不再抓（{n} 沒寵物練）")
                    break
                if not state["capped"]:
                    state["capped"] = True
                last = True
                p.log(f"寵物目標：補抓已達上限 {cap} 隻，{n} 還沒寵物練 → 再抓 1 隻、不挑檔次（照檔次練到分數或練到級賣）")
            mine_a = a.get(n, (0, 0))[1 if ride else 0]
            if mine_a:
                sp = mine_a
            else:
                sp = species[state.get("turn", 0) % len(species)]
                state["turn"] = state.get("turn", 0) + 1
            before = {v.get("uniqueIndex") for v in of_species(p, n, sp)}
            want = len(before) + 1
            p.log(f"寵物目標：{n} 身上、倉庫都沒有沒練完的{role} {sp}，補抓 1 隻（第 {state['caught'] + 1}／{cap} 隻）")
            # reuse=False：倉庫的同種已經由 fetch 挑過（沒練完的才領）；catch 自己的「先領倉庫同種」會把練完的領出來
            res = catch_one(p, n, [(sp, want)], SLOT_MAX, timeout=timeout,
                            buy=bool(getattr(args, "buy_bait", False)), reuse=False, keep=keep_of(p, n, args))
            new = [v for v in of_species(p, n, sp) if v.get("uniqueIndex") not in before]
            if (res or {}).get(sp, 0) < 1 or not new:
                raise JobError(f"{n} 補抓 {sp} 沒抓到（{res}），停（不重試；已補抓 {state['caught']} 隻）")
            uid = new[0].get("uniqueIndex")
            state["caught"] += 1

            def graded() -> bool:                     # p.wait 的條件：持鎖中，只讀 petinfo、不呼叫會上鎖的方法
                v = next((x for x in p.petinfo.get(n, {}).values() if x.get("uniqueIndex") == uid), {})
                return grade(v)[1] is not None
            p.wait(graded, GRADE_WAIT)
            pet = next(v for v in of_species(p, n, sp) if v.get("uniqueIndex") == uid)
            d = diff_of(pet)                                                   # 算不出檔次 → 停
            if release and d < args.cut and not last:
                release_caught(p, n, pet)
                state["released"] = state.get("released", 0) + 1
                state["log"].append(f"{n} 補抓 {uid}（{sp}）{d:+d} 檔｜低於 +{args.cut}，放生")
                p.log(f"寵物目標：{state['log'][-1]}")
                continue
            if not ride:
                state["kept"] = state.get("kept", 0) + 1
            got.append(n)
            state["log"].append(f"{n} 補抓{role} {uid}（{sp}）{d:+d} 檔｜{goal_text(pet, goal)}")
            p.log(f"寵物目標：{state['log'][-1]}")
            break
    with p.lock:
        p.s.team = []        # 抓寵時有人離隊了：清掉隊伍快取，下一輪 train_loop 的 form_team 才會真的重組
    return got


def release_caught(p, n: str, pet: dict) -> None:
    """--release-low：剛抓到、檔差不夠的那隻（1 級）放生。用唯一編號找格子；不是 1 級、任務條件／保護名單上的不放。"""
    from .clean import do_release
    from ..item_rules import load
    rules = load()
    uid, pid = pet.get("uniqueIndex"), pet.get("petId")
    if pid in set(rules.task_pets) | set(rules.protect) or (pet.get("level") or 0) != 1:
        raise JobError(f"{n} 剛抓的 {uid}（{pid}）不是 1 級或在任務條件／保護名單上，不放生，停")
    if uid in p.locks(n):
        # 抓之前沒有戰寵（上一隻練完存倉了）時，catch 的 restore_fight_pet 會挑身上等級最高的＝剛抓的 1 級當戰寵，
        # 放生就被「是戰寵／騎寵，不放生」擋下整組停（11-12 13:40 第十組角色58，LESSONS #200）。剛抓的 1 級先解鎖改休息再放
        free_for_sale(p, n, uid)
    idx = next((k for k, v in slots(p, n).items() if v.get("uniqueIndex") == uid), None)
    if idx is None:
        raise JobError(f"{n} 找不到剛抓的 {uid}，不放生，停")
    do_release(p, n, idx, pid)


def ride_ok(v: dict, rids: List[int], g, fight: int) -> bool:
    """可以當騎寵練的：--ride-ids 那幾種、不是戰寵、檔次算得出來（轉生寵算不出來就跳過，不停）、還沒到目標。"""
    if v.get("petId") not in rids or v.get("uniqueIndex") == fight or grade(v)[1] is None:
        return False
    return not is_done(v, g)


RIDE_LOYAL = 100                         # 騎寵忠誠不到 100 伺服器不讓騎（11-12 實機，LESSONS #198）
RIDE_LOYAL_BUDGET = 400000               # 一隻最多花多少石幣補（0 補到 100＝20 次×2 萬）


def mount(p, n: str, fight, uid) -> bool:
    """鎖好的騎寵要真的在騎（狀態 3）才有經驗。休息中而且忠誠不到 100 → 用石幣補到 100 再送一次鎖定
    （使用者 11-12「全部補，以後自動補」）。石幣不夠、花到上限 → 記日誌、騎寵先空著、照練；送了對不上（JobHold）照停。"""
    def at():
        with p.lock:
            return next(((i, dict(v)) for i, v in p.petinfo[n].items() if v.get("uniqueIndex") == uid), (None, None))
    idx, v = at()
    if v is None or v.get("status") == 3:
        return v is not None
    if int(v.get("allegiance") or 0) < RIDE_LOYAL:
        try:
            times, cost = loyalty.raise_to(p, n, idx, RIDE_LOYAL, RIDE_LOYAL_BUDGET)
        except JobHold:
            raise
        except JobError as e:
            p.log(f"寵物目標：{e}；{n} 騎寵先空著")
            if "石幣不夠" in str(e):        # 這輪賣完寵物後由 fund_broke 讓牠自己賣檔差的換石幣再補
                p.__dict__.setdefault("ride_broke", set()).add(n)
            return False
        p.log(f"寵物目標：{n} 騎寵 {uid} 忠誠 {v.get('allegiance') or 0} 騎不上，補到 {RIDE_LOYAL}（{times} 次、{cost} 石幣）")
        p.set_fight_pet(n, fight, uid)
    idx, v = at()
    if v is None or v.get("status") != 3:
        p.log(f"寵物目標：{n} 騎寵 {uid} 還是沒騎上（忠誠 {(v or {}).get('allegiance')}）")
        return False
    return True


FUND_LEVEL = 120                         # 賣來換石幣的最低等級
FUND_MAX = 3                             # 一隻角色一次最多賣幾隻


def fund_broke(p, names: List[str], args) -> List[str]:
    """使用者 11-12「如果石幣不夠可以賣 120 以上檔數差的老虎或人龍」：補騎寵忠誠時石幣不夠的角色，
    把**自己**身上＋個倉 120 級以上、檔差不到 +cut（人龍看 --cut、老虎看 --ride-cut）的人龍／老虎賣掉（隊長賣就好：石幣是整個帳號共用的，
    11-12 第三組實測賣前五隻都 2000、賣後五隻都 662000），夠付補到 100 就停、一次最多 3 隻；賣完再補忠誠叫騎寵上場。沒得賣就記日誌、騎寵照空著。"""
    broke = getattr(p, "ride_broke", None)
    if not broke:
        return []
    from .petsell import price_of, refuse_reason, sell_all
    dragons, tigers = set(pet_ids(args)), set(ride_ids(args))
    rg = ride_goal(args)
    out: List[str] = []
    for n in [x for x in names if x in broke]:
        fight, ride = p.locks(n)
        cur = next((v for v in slots(p, n).values() if v.get("uniqueIndex") == ride), None) if ride else None
        if cur is None:
            continue
        need = -(-(RIDE_LOYAL - int(cur.get("allegiance") or 0)) // 5) * loyalty.LOYAL_COST
        pool = []
        for where, rows in (("身上", list(slots(p, n).values())), ("個倉", p.pet_depot(n))):
            for r in rows:
                sp = r.get("petId")
                if sp not in dragons | tigers or (r.get("level") or 0) < FUND_LEVEL:
                    continue
                d = grade(r)[1]
                cut = args.cut if sp in dragons else rg.cut
                if d is None or d >= cut or refuse_reason(p, n, r, where == "身上"):
                    continue
                pool.append({"who": n, "where": where, "uid": r["uniqueIndex"], "level": r.get("level"), "diff": d, "pet": r})
        pool.sort(key=lambda c: c["level"] or 0, reverse=True)
        pick, est = [], 0
        for c in pool:
            if est >= need or len(pick) >= FUND_MAX:
                break
            pick.append(c)
            est += price_of(c["level"] or 0)
        if not pick:
            p.log(f"寵物目標：{n} 石幣不夠補騎寵忠誠，也沒有 {FUND_LEVEL} 級以上、檔差不夠的老虎／人龍可賣，騎寵先空著")
            continue
        p.log(f"寵物目標：{n} 石幣不夠補騎寵忠誠（要 {need}），賣自己的 {len(pick)} 隻換石幣")
        if not p.form_team():
            raise JobError("賣寵物前組不起隊，不賣")
        for x in sell_all(p, pick):
            out.append(f"{n} {x['level']} 級 {x['uid']} → 賣 +{x['gain']}（補忠誠用）")
            p.log(f"寵物目標：{out[-1]}")
        broke.discard(n)
        mount(p, n, fight, ride)
    return out


def ride_step(p, names: List[str], args, selling: list, done: list) -> List[str]:
    """--ride-ids：輪與輪之間（全隊已停戰鬥）每隻角色的騎寵換成沒練完的那幾種，到目標的跟戰寵一樣收。
    挑法：身上的先、再個倉、再共倉，檔次高的先（使用者 11-10「只騎 +3 以上的貝魯卡／貝魯伊卡」；
    不到 --ride-cut 的騎到 --low-level 級就賣／存，同人龍規則）。回傳身上、倉庫都沒有的角色（給 --ride-refill 抓）。
    換騎寵用 set_fight_pet（看推回的外掛設定確認），失敗就停。"""
    if not ride_ids(args):
        return []
    g = ride_goal(args)
    claimed: set = set()
    need: List[str] = []
    for n in names:
        rids = rids_of(args, n)               # --assign：這隻只騎指定的那種
        fight, ride = p.locks(n)
        # 身上休息中、已經到目標的那幾種（上次換下來沒處理、或上次中斷留著的）先收／賣，不然一直佔格子：
        # 11-10 18:36 第七組角色68只有 4 格，兩隻 120 級以上的 307 留在身上，補抓騎寵時 make_room 不動要抓的物種 →
        # 「現在 4 隻、要抓 1 隻，超過上限 4」整組停（LESSONS #189）
        queued = {id(x) for _, x in selling}
        for v in list(slots(p, n).values()):
            if (v.get("petId") in rids and v.get("uniqueIndex") not in (fight, ride, None, 0)
                    and v.get("status") not in (2, 3) and id(v) not in queued
                    and grade(v)[1] is not None and is_done(v, g)):
                p.log(f"寵物目標：{n} 身上休息的騎寵 {v.get('uniqueIndex')}（{v.get('petId')}）"
                      f"{diff_of(v):+d} 檔 {v.get('level')} 級已到目標，這輪一起{'賣' if to_sell(v, g) else '收'}")
                if to_sell(v, g):
                    selling.append((n, v))
                else:
                    done.append(finish(p, n, v, g))
        cur = next((v for v in slots(p, n).values() if v.get("uniqueIndex") == ride), None) if ride else None
        if cur is not None and ride_ok(cur, rids, g, fight):
            if cur.get("status") == 1:        # 鎖著卻在休息＝沒在騎、沒經驗（LESSONS #198）：補忠誠、叫牠上場
                p.log(f"寵物目標：{n} 騎寵 {ride}（{cur.get('petId')}）鎖著卻在休息，叫牠上場")
                mount(p, n, fight, ride)
            continue
        pick = sorted((v for v in slots(p, n).values() if ride_ok(v, rids, g, fight) and v.get("uniqueIndex") != ride),
                      key=lambda v: (diff_of(v), v.get("level") or 0), reverse=True)
        new = pick[0] if pick else None
        if new is None:
            for public in (False, True):
                rows = [r for r in p.pet_depot(n, public=public)
                        if r.get("uniqueIndex") not in claimed and ride_ok(r, rids, g, fight)]
                if rows:
                    row = max(rows, key=lambda r: (diff_of(r), r.get("level") or 0))
                    claimed.add(row["uniqueIndex"])
                    new = take_pet(p, n, row)
                    break
        if new is None:
            need.append(n)
            # 沒有指定那種可騎，身上鎖著的是別種老虎、還沒到目標 → 先騎著（有經驗總比空著好），休息中就補忠誠叫上場。
            # 11-12 --assign 後第十組角色58指定 310、身上鎖的是 307 忠誠 96 一直休息，舊版直接略過（LESSONS #198）
            if (cur is not None and cur.get("status") == 1 and cur.get("petId") in ride_ids(args)
                    and grade(cur)[1] is not None and not is_done(cur, g)):
                p.log(f"寵物目標：{n} 沒有指定的騎寵可換，先騎著身上的 {ride}（{cur.get('petId')}）")
                mount(p, n, fight, ride)
            continue
        uid = new["uniqueIndex"]
        if not p.set_fight_pet(n, fight, uid):
            raise JobError(f"{n} 騎寵換不成 {uid}，停")
        p.log(f"寵物目標：{n} 騎寵 → {uid}（{new.get('petId')}）{diff_of(new):+d} 檔 {new.get('level')} 級｜{goal_text(new, g)}")
        mount(p, n, fight, uid)
        if cur is not None and cur.get("petId") in rids and grade(cur)[1] is not None and is_done(cur, g):
            if to_sell(cur, g):
                selling.append((n, cur))
            else:
                done.append(finish(p, n, cur, g))
    return need


def plan(p, names: List[str], args) -> Dict[str, list]:
    """只讀：每隻角色身上＋個倉，和共倉，那種寵物的檔次、等級、評分、目標、練完沒。"""
    def row(v, where):
        return {"where": where, "uid": v.get("uniqueIndex"), "level": v.get("level"), "score": v.get("score"),
                "diff": diff_of(v), "goal": goal_text(v, args), "done": is_done(v, args)}
    out: Dict[str, list] = {}
    for n in names:
        out[n] = [row(v, "身上") for v in on_body(p, n, args)] + \
                 [row(r, "個倉") for r in p.pet_depot(n) if mine(r, args, n)]
    out["共倉"] = [row(r, "共倉") for r in p.pet_depot(names[0], public=True) if mine(r, args)]
    return out


def run(p, args) -> Tuple[str, dict]:
    names = [n for _, n in p.party]
    args._assign = resolve_assign(names, args)
    if args._assign:
        p.log("寵物目標：每隻固定練 " + "、".join(f"{n} 戰寵 {f}／騎寵 {r or ride_ids(args)}"
                                          for n, (f, r) in args._assign.items()))
    rows = plan(p, names, args)
    for who, lst in rows.items():
        for r in lst:
            p.log(f"寵物目標：{who} {r['where']} {r['diff']:+d} 檔 {r['level']} 級 評分 {r['score']}｜"
                  f"{r['goal']}｜{'已完成' if r['done'] else '要練'}")
    if not getattr(args, "apply", False):
        return "只列，沒有練（加 --apply 才練）", {"plan": rows}

    old = (p.train_pet, p.pet_start, p.no_retreat, p.stop_file, set(p.train_skip), p.pet_jump,
           p.auto_fruit, p.fruit_buy)
    p.pet_jump, p.pet_jump_auto = jump_of(args), str(getattr(args, "jump", "0")).strip().lower() == "auto"
    p.jump_heals, p.jump_changed = [], time.time()
    if getattr(args, "fruit", False):
        p.auto_fruit = p.fruit_buy = True
    if getattr(args, "stop_file", None):
        p.stop_file = args.stop_file
    elif p.stop_file is None and getattr(args, "log", None):
        p.stop_file = args.log + ".stop"
    p.pet_start = args.start
    p.no_retreat = True
    done: List[str] = []
    rounds = 0
    refill_state = {"caught": 0, "capped": False, "log": []}
    if ride_ids(args):
        g = ride_goal(args)
        p.log(f"寵物目標：騎寵也練 {ride_ids(args)}（檔差 ≥ {g.cut} 練到 {g.score} 分改名「{g.name}」，"
              f"其他練到 {g.low_level} 級{'賣' if g.sell_low else '存倉'}）"
              + ("，倉庫沒有就補抓" if getattr(args, "ride_refill", False) else ""))
    if refilling(args):
        p.log(f"寵物目標：--refill 開，倉庫沒有沒練完的就在 {getattr(args, 'catch_at', CATCH_AT)} 補抓，"
              f"這次最多 {getattr(args, 'refill_max', REFILL_MAX)} 隻"
              + (f"，低於 +{args.cut} 的當場放生" if getattr(args, "release_low", False) else "")
              + (f"，留下 {args.refill_want} 隻就不再抓" if getattr(args, "refill_want", 0) else ""))
    shared: List[str] = []
    try:
        if assigned(args):
            shared = share_unassigned(p, names, args)
        while True:
            claimed: set = set()
            active = []
            selling: List[Tuple[str, dict]] = []
            for n in names:
                for v in on_body(p, n, args):          # 上一輪到目標（或上次中斷留在身上）的先收
                    if to_sell(v, args):
                        selling.append((n, v))         # --sell-low：一起賣（全隊已停戰鬥）
                    elif is_done(v, args):
                        done.append(finish(p, n, v, args))
            ride_need = ride_step(p, names, args, selling, done)     # --ride-ids：騎寵換沒練完的、到目標的一起收
            if selling:
                done.extend(sell(p, selling, args))
            done.extend(fund_broke(p, names, args))             # 補騎寵忠誠石幣不夠：自己賣檔差的換錢再補
            idle = []
            for n in names:
                left = [v for v in on_body(p, n, args) if not is_done(v, args)]
                if not left and fetch(p, n, args, claimed) is not None:
                    left = [v for v in on_body(p, n, args) if not is_done(v, args)]
                if left:
                    active.append(n)
                else:
                    idle.append(n)
            if idle and refilling(args):
                # --refill：倉庫也沒有沒練完的 → 抓新的補（在輪與輪之間，全隊已停戰鬥、該賣的已賣）
                got = refill(p, idle, args, refill_state)
                active = [n for n in names if n in active or n in got]
            if ride_need and getattr(args, "ride_refill", False):
                got = refill(p, ride_need, args, refill_state, species=ride_ids(args), goal=ride_goal(args))
                later: List[Tuple[str, dict]] = []
                rest = ride_step(p, got, args, later, done)       # 剛抓到的換上當騎寵
                if later:                                         # 換下來到目標的舊騎寵：舊版丟進用完即棄的清單，沒賣、一直佔格子
                    done.extend(sell(p, later, args))
                if rest:
                    raise JobError(f"{'、'.join(rest)} 補抓到騎寵卻換不上，停")
            if not active:
                break
            park_unlisted(p, active, args)
            rounds += 1
            key = train_key(args, names)
            p.log(f"寵物目標：第 {rounds} 輪，{'、'.join(active)} 練 {args.pet_id}（{args.start} 起）")
            p.train_pet = (key, TRAIN_LV)
            p.train_skip = set(names) - set(active)
            p.orig_fight.clear()
            stall = T.stall_guard(p, key)
            fills: Dict[str, int] = {}             # 這輪頂替練的（指定的沒了，拿個倉沒滿 140 的）：角色 → 唯一編號
            tried: set = set()                     # 這輪找過下一隻、倉庫沒有的角色（不每 15 秒查一次倉庫）
            started = set(active)                  # 這輪開始時有指定寵物練的（這輪中途練完的才會提早收輪去補抓）

            def end_round(who: List[str]) -> None:
                """這輪中途有人練完、倉庫沒有指定的、--refill 抓得到 → 這輪先收（頂替的存回、還在練的戰寵改回原本的，
                沒練完的留在身上下一輪接著練），回到輪與輪之間補抓同種。舊版改拿個倉別的寵物頂替、等其他人練完才補抓：
                11-14 第十二組三隻人龍 23:45 練完，另兩隻 138 級的 6 小時沒升級，那三人 6 小時沒練人龍（使用者 11-14 同意改）。"""
                halt()
                for n in list(fills):
                    done.append(drop_fill(p, n, fills))
                for n in list(p.orig_fight):
                    fight, ride = p.orig_fight.pop(n)
                    p.train_uid.pop(n, None)
                    if not p.set_fight_pet(n, fight, ride):
                        raise JobError(f"{n} 這輪提早收，戰寵改不回 {fight}")
                p.log(f"寵物目標：{'、'.join(who)} 練完、倉庫沒有指定的 → 這輪先收，補抓同種再練（沒練完的下一輪接著練）")

            def refresh() -> None:
                """沒在練的角色當場補下一隻（使用者 11-13：存進去就要換一隻，不要等這輪結束）。
                倉庫沒有、--refill 抓得到 → 這輪先收去補抓（end_round）；抓不到才拿個倉沒滿 140 的頂替。
                指定的都練完、只剩頂替的 → 頂替的收掉，讓這輪結束去賣／補抓（不讓頂替的拖住輪次）。"""
                def designated() -> bool:
                    return any(m not in fills for m in p.orig_fight)
                idle = [n for n in names if n not in p.orig_fight and n not in tried]
                catch = [n for n in idle if n in started and p.orig_fight and can_refill(n, args, refill_state)
                         and not fetch_ready(p, n, args, claimed)]
                if catch:
                    end_round(catch)
                    return
                for n in idle:
                    # 指定的都沒人在練了就不拿頂替的（拿了這輪也馬上結束，白領白存）
                    if not designated() and not fetch_ready(p, n, args, claimed):
                        tried.add(n)
                        continue
                    halt()
                    if not swap_in(p, n, args, key, claimed, fills if designated() else None):
                        tried.add(n)
                if fills and not designated():
                    halt()
                    for n in list(fills):
                        done.append(drop_fill(p, n, fills))

            off = {"v": False}                     # 這次 until 有沒有關過遇敵（最後要開回來）

            def halt() -> None:
                if not off["v"]:
                    if not p.pause():
                        raise JobError("練寵中要換寵物，關不掉遇敵")
                    off["v"] = True

            def until() -> bool:
                off["v"] = False
                for n in list(p.orig_fight):
                    # 看開練時換上的那隻（train_uid）：多種一起練時身上可能同時有兩隻要練的，照等級挑會看錯隻
                    idx = p.training_index(n, key, TRAIN_LV)
                    with p.lock:
                        pet = dict(p.petinfo[n].get(idx, {})) if idx else {}
                    if n in fills:
                        if pet and (pet.get("level") or 0) < MAX_PET_LV:
                            continue
                        halt()
                        done.append(drop_fill(p, n, fills))
                        tried.discard(n)
                        continue
                    if not pet or not is_done(pet, args):
                        continue
                    fight, ride = p.orig_fight[n]
                    halt()
                    if not p.set_fight_pet(n, fight, ride):
                        raise JobError(f"{n} 寵物到目標了，但戰寵改不回 {fight}")
                    del p.orig_fight[n]
                    p.train_uid.pop(n, None)
                    if to_sell(pet, args):
                        # 不在這裡賣：其他人還在練，賣要整隊走去寵物店；留在身上，下一輪開始時一起賣
                        p.log(f"寵物目標：{n} {pet.get('level')} 級 檔差 {diff_of(pet):+d} 到目標，這輪結束後賣")
                    else:
                        done.append(finish(p, n, pet, args))
                    tried.discard(n)                      # 下面 refresh 當場換下一隻
                refresh()
                if off["v"] and p.orig_fight and not p.set_loop(True):
                    raise JobError("換完寵物後遇敵開不回來")
                return stall()

            code = p.train_loop(args.start, T.INTERVAL, until=until)
            if code != 0 or p.orig_fight:
                why = "收到停止檔" if code == 0 else f"練功停止（結束碼 {code}）"
                raise JobError(f"寵物目標第 {rounds} 輪沒練完（{why}）；已收 {done}"
                               + (f"；已補抓 {refill_state['caught']} 隻" if refilling(args) else ""))
    finally:
        bad = T.restore(p)
        if bad:
            p.log(f"還原戰寵：{bad} 改不回去（要手動設）")
        (p.train_pet, p.pet_start, p.no_retreat, p.stop_file, p.train_skip, p.pet_jump,
         p.auto_fruit, p.fruit_buy) = old
        p.pet_jump_auto = False
    if bad:
        raise JobError(f"練完了，但 {bad} 的戰寵改不回去（要手動設）")
    data = {"done": done, "rounds": rounds}
    if shared:
        data["shared"] = shared
    if refilling(args) or getattr(args, "ride_refill", False):
        data["caught"] = refill_state["log"]
        return (f"寵物目標完成：{len(done)} 隻（{rounds} 輪，補抓 {refill_state['caught']} 隻"
                f"{'，放生低檔 %d 隻' % refill_state['released'] if refill_state.get('released') else ''}"
                f"{'，已達上限' if refill_state['capped'] else ''}）"), data
    return f"寵物目標完成：{len(done)} 隻（{rounds} 輪）", data
