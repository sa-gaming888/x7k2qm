"""人物配置（loadout）：每組一份「平常配置」（每隻完整一份）＋情境配置（英雄戰場、亂鬥、副本…只記跟平常不同的地方）。

參考另一個版本 參考專案（doc/loadouts.md、參考專案/autoplay/loadout*.py；同一套協定，官方錄檔對過）。
這支只處理資料（產生、合併、讀寫檔、備份），不送任何封包；切換在 `loadout_apply.py`，登入後的指令在 `jobs/loadout.py`。

    python -X utf8 -m x7k2qm.autoplay.loadout list    --profile 3
    python -X utf8 -m x7k2qm.autoplay.loadout show    英雄戰場 --profile 3
    python -X utf8 -m x7k2qm.autoplay.loadout set     英雄戰場 --profile 3 --name 某某 --role-cmd 1,0,0
    python -X utf8 -m x7k2qm.autoplay.loadout delete  英雄戰場 --profile 3 [--name 某某]
    python -X utf8 -m x7k2qm.autoplay.loadout history --profile 3   /  revert <備份檔名>

檔案（根目錄 `BOT_LOADOUT_DIR`，預設 captures/loadouts/；captures/ 整個 gitignore）：
- `base/<組>.json`：平常配置。登入後 `jobs loadout --do save` 從線上狀態產生；含 rid、uniqueIndex，**不 commit**。
- `scenarios/<情境>.json`：情境差異（這台機器自己的，可能含寵物／裝備 uniqueIndex，不 commit）。
- `pending_<組>.json`：切走還沒切回的紀錄（loadout_apply）。`history/`：每次寫入前的備份＋log.jsonl。
- 共用規則（不含任何 rid／uniqueIndex，可以 commit）：`config/loadouts/<情境>.json`；這台沒有自己的情境檔時讀它，
  `set` 第一次寫時複製一份到 scenarios/ 再改。

情境檔：`{"all": {...}, "roles": {"隊長": {...}}, "profiles": {"<組>": {"<角色名>": {...}}}}`，套用由寬到窄、後蓋前：
all → roles.<職責> → profiles.<組>.<角色名>。值跟平常配置同一個結構，dict 深度合併、寫 null 代表移除；
`equip.<格>`、`ride`、`pet` 整塊替換（不然只寫 itemId 會留著平常那件的 uniqueIndex，指到別件）。

能自動切換的欄位（loadout_apply 檔頭列協定）：plan（配點方案名）、equip.<格>（itemId，可加 uniqueIndex 指定哪一件）、
ride／pet（uniqueIndex；情境也可以只寫種類 `{"petId": N}`，切換時找身上那一隻，沒有或兩隻以上就不切）、waigua 人物一般動作 autoCmd／autoCmdArg／autoCmdArgEx、寵物一般動作 petAutoCmdType／…Arg／…ArgEx。
**只記錄、不切換**：外掛自動戰鬥開關 waigua.isAuto（見 loadout_apply 檔頭 TODO）、外掛 battleSetting
（首次動作、補血、補魔、復活門檻；沒有官方改設定的錄檔，2026-10-31 herobf 照 schema 猜著送打輸）、wg_normal 其他欄位、配點數字。
"""

import argparse
import copy
import glob
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .jobs import JobError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_DIR = "BOT_LOADOUT_DIR"
SHARED_DIR = os.path.join(ROOT, "config", "loadouts")      # 共用規則（commit；不能有 rid／uniqueIndex）
BASE_NAME = "平常"
ALIASES = {"平常": BASE_NAME, "一般": BASE_NAME, "base": BASE_NAME,
           "132": "英雄戰場", "herobf": "英雄戰場", "英雄戰場": "英雄戰場",
           "亂鬥": "大亂鬥", "melee": "大亂鬥", "大亂鬥": "大亂鬥",
           "朱雀": "靈石朱雀", "靈石朱雀": "靈石朱雀"}

# ItemInfo.itemStorge 41～50 ＝身上 10 個部位（doc/autoplay/game/inventory.md §2.1；RoleAttr.*Equip 存該部位的 itemIndex）
STORGE_SLOT = {41: "weaponEquip", 42: "shieldEquip", 43: "headEquip", 44: "armorEquip", 45: "shoesEquip",
               46: "beltEquip", 47: "gloveEquip", 48: "necklaceEquip", 49: "ringEquip", 50: "amuletEquip"}
SLOT_STORGE = {v: k for k, v in STORGE_SLOT.items()}
EQUIP_SLOTS = list(SLOT_STORGE)
SLOT_ZH = {"weaponEquip": "武器", "shieldEquip": "盾", "headEquip": "頭", "armorEquip": "身", "shoesEquip": "鞋",
           "beltEquip": "腰帶", "gloveEquip": "手套", "necklaceEquip": "項鍊", "ringEquip": "戒指", "amuletEquip": "護身符"}
RIDE, FIGHT, REST = 3, 2, 1            # 寵物 status：3 騎乘、2 出戰、1 休息
CMD_SKILL = 3                          # 一般動作 cmdType：1 攻擊、2 防禦、3 技能、9 逃跑
WAIGUA_KEYS = ("isAuto", "autoCmd", "autoCmdArg", "autoCmdArgEx", "petAutoCmdType", "petAutoCmdArg", "petAutoCmdArgEx")
POINT_KEYS = ("vitality", "strength", "stamina", "speed", "potentialPoint", "autoSetPointType")
ATOMIC = ("ride", "pet")               # 整塊替換的鍵（equip.<格> 另外處理）
DUTY_LEADER, DUTY_MEMBER = "隊長", "隊員"


class LoadoutError(JobError):
    """配置切不過去／檔案不對（理由給人看）。是 JobError：常駐收到照常回練功，單獨執行結束碼 1。"""


# ---- 小工具 ----
def scenario_name(s: str) -> str:
    """使用者說的名字 → 情境（檔名）。不在別名表的照原字（副本名、自訂情境）。"""
    s = (s or "").strip()
    return ALIASES.get(s, s)


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def norm_cmd(cmd: Any) -> Optional[Tuple[int, int, int]]:
    """一般動作 (類型, 技能, 等級) 正規化：不是技能時技能欄一律 0（官方送法，參考專案 ops/rolecmd.normal_of）。"""
    if cmd is None:
        return None
    t = _int(cmd[0])
    return (t, _int(cmd[1]), _int(cmd[2])) if t == CMD_SKILL else (t, 0, 0)


def role_cmd_of(w: dict) -> Optional[Tuple[int, int, int]]:
    return None if w.get("autoCmd") is None else norm_cmd((w.get("autoCmd"), w.get("autoCmdArg"), w.get("autoCmdArgEx")))


def pet_cmd_of(w: dict) -> Optional[Tuple[int, int, int]]:
    return None if w.get("petAutoCmdType") is None else norm_cmd(
        (w.get("petAutoCmdType"), w.get("petAutoCmdArg"), w.get("petAutoCmdArgEx")))


def plans_of(a: dict) -> list:
    v = a.get("pointSave") or []
    return list(v.values()) if isinstance(v, dict) else list(v)


def active_plan(a: dict) -> Optional[str]:
    """目前四項配點＋屬性跟哪一套配點方案一樣 → 方案名（第 N 格沒名字叫「方案N」）；都不一樣 → None。"""
    cur = tuple(a.get(k) for k in ("vitality", "strength", "stamina", "speed")) + \
        tuple(a.get(k) or 0 for k in ("earth", "water", "fire", "wind"))
    for i, p in enumerate(plans_of(a)):
        if tuple(p.get(k) for k in ("hp", "gj", "fy", "mj")) + \
                tuple(p.get(k) or 0 for k in ("earth", "water", "fire", "wind")) == cur:
            return p.get("name") or f"方案{i + 1}"
    return None


def plan_index(a: dict, name: str) -> Optional[int]:
    return next((i for i, p in enumerate(plans_of(a)) if (p.get("name") or f"方案{i + 1}") == name), None)


# ---- 從線上狀態（headless_party.Party）產生平常配置 ----
def body_pets(p, name: str) -> Dict[int, dict]:
    """身上的寵物（存倉／交出後那格只推 petId -1，要濾掉）。呼叫端要握 p.lock。"""
    return {i: dict(x) for i, x in (p.petinfo.get(name) or {}).items()
            if (x.get("petId") or 0) > 0 and x.get("uniqueIndex")}


def worn(p, name: str) -> Dict[str, dict]:
    """身上 10 格：部位 → {itemIndex, itemId, uniqueIndex}（看 Item_ItemInfo 的 itemStorge 41～50）。呼叫端要握 p.lock。"""
    out = {}
    for idx, it in (p.items.get(name) or {}).items():
        slot = STORGE_SLOT.get(it.get("itemStorge"))
        if slot and (it.get("overlay", 1) or 0) > 0 and it.get("itemId"):
            out[slot] = {"itemIndex": idx, "itemId": it["itemId"], "uniqueIndex": it.get("uniqueIndex")}
    return out


def _pet_brief(x: Optional[dict]) -> Optional[dict]:
    if not x:
        return None
    return {"uniqueIndex": x.get("uniqueIndex"), "petId": x.get("petId"), "name": x.get("name"), "level": x.get("level")}


def snapshot_role(p, name: str) -> dict:
    """一隻角色的完整配置（平常配置的一筆）。外掛一般動作讀不到就停（不存半套）。"""
    with p.lock:
        a = dict(p.attrs.get(name) or {})
        eq = worn(p, name)
        pets = body_pets(p, name)
        wg = dict(p.wg.get(name) or {})
        bs = dict(p.battle.get(name) or {})
        rid = p.clients[name].rid if name in p.clients else a.get("rid")
    miss = [k for k in ("autoCmd", "petAutoCmdType") if k not in a]
    if miss:
        raise LoadoutError(f"{name} 讀不到外掛一般動作（RoleAttr {miss}），不存半套配置")
    ride = next((x for x in pets.values() if x.get("status") == RIDE), None)
    war = next((x for x in pets.values() if x.get("status") == FIGHT), None)
    return {
        "rid": rid, "name": name,
        "plan": active_plan(a),
        "plans": [p_.get("name") or f"方案{i + 1}" for i, p_ in enumerate(plans_of(a))],
        "points": {k: a.get(k) for k in POINT_KEYS if k in a},
        "equip": {s: {"itemId": v["itemId"], "uniqueIndex": v.get("uniqueIndex")} for s, v in eq.items()},
        "ride": _pet_brief(ride),
        "pet": _pet_brief(war),
        "waigua": {k: a.get(k) for k in WAIGUA_KEYS if k in a},
        "wg_normal": wg,                 # 只記錄：外掛一般設定（騎寵／戰寵鎖、自動換寵、自動補血…）
        "battleSetting": bs,             # 只記錄：首次動作、補血、補魔、復活門檻（不切換，見檔頭）
    }


def snapshot(p, names: List[str], old: Optional[dict] = None) -> dict:
    """這組的平常配置。職責第一次自動猜（隊伍第一隻＝隊長，其他隊員），舊檔有的保留（使用者手改過的不蓋掉）。"""
    keep = {r.get("name"): r.get("duty") for r in (old or {}).get("roles") or []}
    order = [n for _, n in p.party]
    roles = []
    for n in names:
        r = snapshot_role(p, n)
        r["duty"] = keep.get(n) or (DUTY_LEADER if order and order[0] == n else DUTY_MEMBER)
        roles.append(r)
    if old:                                            # 只存幾隻：其他隻照舊檔
        roles += [r for r in old.get("roles") or [] if r.get("name") not in names]
        roles.sort(key=lambda r: order.index(r["name"]) if r.get("name") in order else len(order))
    return {"profile": str(p.profile), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "roles": roles}


# ---- 合併 ----
def merge(dst: Any, src: Any, path: str = "") -> Any:
    """深度合併：dict 遞迴、其他直接蓋；值 None 代表移除。ride／pet／equip.<格> 整塊替換。"""
    if not isinstance(dst, dict) or not isinstance(src, dict) or path in ATOMIC or path.startswith("equip."):
        return copy.deepcopy(src)
    out = copy.deepcopy(dst)
    for k, v in src.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge(out.get(k), v, f"{path}.{k}" if path else str(k))
    return out


def changes(old: Any, new: Any, prefix: str = "") -> List[str]:
    """兩份配置的差異路徑。"""
    if isinstance(old, dict) and isinstance(new, dict):
        out: List[str] = []
        for k in sorted(set(old) | set(new), key=str):
            out += changes(old.get(k), new.get(k), f"{prefix}.{k}" if prefix else str(k))
        return out
    return [] if old == new else [prefix]


def layers_for(sc: Optional[dict], tag: str, role: dict) -> List[dict]:
    if not sc:
        return []
    group = (sc.get("profiles") or {}).get(str(tag)) or {}
    # "*"：這一組全部人物共用（使用者 2026-11-01「五支人物配置都一樣，有甚麼方式可以統一管理」），個別人物再蓋過去
    return [x for x in (sc.get("all"), (sc.get("roles") or {}).get(role.get("duty") or ""),
                        group.get("*"), group.get(role.get("name"))) if x]


def resolve(sc: Optional[dict], tag: str, base: dict) -> List[dict]:
    """每隻在這個情境的完整配置（平常＋差異），附 changed（跟平常不同的欄位路徑）。sc None＝平常。"""
    out = []
    for r in base.get("roles") or []:
        cur = copy.deepcopy(r)
        for layer in layers_for(sc, tag, r):
            cur = merge(cur, layer)
        cur["changed"] = changes(r, {k: v for k, v in cur.items() if k != "changed"})
        out.append(cur)
    return out


SUPPORTED_PREFIX = ("plan", "equip.", "ride", "pet", "waigua.autoCmd", "waigua.petAutoCmd")


def target_of(me: dict) -> Tuple[Dict[str, Any], List[str]]:
    """完整配置 → (要切成的目標, 跟平常不同但不支援自動切的欄位)。
    目標放完整配置裡**所有**支援的欄位（不只差異）：線上被手動改過也切得回來（參考專案 2026-10-23 教訓）；
    真正要送什麼由 loadout_apply.diff 對照線上決定。"""
    tgt: Dict[str, Any] = {}
    if me.get("plan") is not None:
        tgt["plan"] = me["plan"]
    eq = {s: dict(v) for s, v in (me.get("equip") or {}).items() if isinstance(v, dict) and v.get("itemId")}
    if eq:
        tgt["equip"] = eq
    for k in ("ride", "pet"):
        v = me.get(k)
        if isinstance(v, dict) and v.get("uniqueIndex"):
            tgt[k] = {"uniqueIndex": v["uniqueIndex"], "petId": v.get("petId"), "name": v.get("name")}
    w = me.get("waigua") or {}
    if role_cmd_of(w) is not None:
        tgt["role_cmd"] = role_cmd_of(w)
    if pet_cmd_of(w) is not None:
        tgt["pet_cmd"] = pet_cmd_of(w)
    changed = me.get("changed") or []
    skip = set()                                       # 換騎寵／戰寵時會一起鎖：那個鎖定欄位不算另外的差異
    for k, lock in (("ride", "ridePetIndex"), ("pet", "fightPetIndex")):
        if any(c == k or c.startswith(k + ".") for c in changed):
            skip.add(f"wg_normal.{lock}")
    unsupported = [c for c in changed
                   if not c.startswith(SUPPORTED_PREFIX) and c not in skip and c != "duty"]
    return tgt, unsupported


# ---- 檔案 ----
def default_dir() -> str:
    return os.environ.get(ENV_DIR) or os.path.join(ROOT, "captures", "loadouts")


def load_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except OSError:
        return None
    except ValueError as e:
        raise LoadoutError(f"{path} 不是合法的 JSON：{e}") from None


def dump(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
        fp.write("\n")
    os.replace(tmp, path)


class Store:
    """配置檔的讀寫。root 一定要給（規範 13：會寫檔的不能預設寫到真實路徑；CLI／工作用 default_dir()）。"""

    def __init__(self, root: str, shared: Optional[str] = SHARED_DIR) -> None:
        if not root:
            raise ValueError("Store 一定要給 root")
        self.root, self.shared = root, shared
        self.history_dir = os.path.join(root, "history")

    def base_path(self, tag: str) -> str:
        return os.path.join(self.root, "base", f"{tag}.json")

    def scenario_path(self, sc: str) -> str:
        return os.path.join(self.root, "scenarios", f"{sc}.json")

    def pending_path(self, tag: str) -> str:
        return os.path.join(self.root, f"pending_{tag}.json")

    def load_base(self, tag: str) -> Optional[dict]:
        return load_json(self.base_path(tag))

    def require_base(self, tag: str) -> dict:
        b = self.load_base(tag)
        if not b or not b.get("roles"):
            raise LoadoutError(f"第 {tag} 組沒有平常配置（{self.base_path(tag)}）：先 `jobs loadout --do save --apply`")
        return b

    def load_scenario(self, sc: str) -> Optional[dict]:
        """情境檔：這台自己的優先，沒有就讀 config/loadouts/ 的共用規則。平常＝{}（沒有差異）。"""
        if sc == BASE_NAME:
            return {}
        own = load_json(self.scenario_path(sc))
        if own is not None:
            return own
        return load_json(os.path.join(self.shared, f"{sc}.json")) if self.shared else None

    def scenarios(self) -> List[str]:
        names = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(self.root, "scenarios", "*.json"))}
        if self.shared:
            names |= {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(self.shared, "*.json"))
                      if not f.endswith(".example.json")}
        return [BASE_NAME] + sorted(names)

    def backup(self, path: str, why: str) -> Optional[str]:
        if not os.path.exists(path):
            return None
        os.makedirs(self.history_dir, exist_ok=True)
        stem = os.path.basename(os.path.dirname(path)) + "_" + os.path.basename(path)[:-5]
        out = os.path.join(self.history_dir, f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.json")
        shutil.copy2(path, out)
        with open(os.path.join(self.history_dir, "log.jsonl"), "a", encoding="utf-8") as fp:
            fp.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "file": os.path.relpath(path, self.root),
                                 "backup": os.path.basename(out), "why": why}, ensure_ascii=False) + "\n")
        return out

    def write(self, path: str, data: Any, why: str) -> Optional[str]:
        """寫入前先備份舊版（參考專案：每次寫入前備份歷史，可以 revert）。回傳備份檔路徑（新檔 None）。"""
        b = self.backup(path, why)
        dump(path, data)
        return b

    def history(self) -> List[dict]:
        p = os.path.join(self.history_dir, "log.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fp:
            return [json.loads(x) for x in fp if x.strip()]

    def revert(self, backup: str) -> str:
        row = next((r for r in self.history() if r["backup"] == os.path.basename(backup)), None)
        src = os.path.join(self.history_dir, os.path.basename(backup))
        if row is None or not os.path.exists(src):
            raise LoadoutError(f"找不到備份 {backup}（history 列得出來的才可以）")
        dst = os.path.join(self.root, row["file"])
        self.write(dst, load_json(src), f"revert {os.path.basename(src)}")
        return dst


# ---- 顯示 ----
def describe_cmd(c: Optional[tuple]) -> str:
    if c is None:
        return "-"
    t, sid, lv = c
    return f"技能 {sid} Lv{lv}" if t == CMD_SKILL else {1: "攻擊", 2: "防禦", 9: "逃跑"}.get(t, f"類型 {t}")


def describe_target(t: Dict[str, Any]) -> str:
    parts = []
    if "plan" in t:
        parts.append(f"配點 {t['plan']}")
    for s, v in (t.get("equip") or {}).items():
        parts.append(f"{SLOT_ZH.get(s, s)} {v.get('itemId')}" + (f"（{v['uniqueIndex']}）" if v.get("uniqueIndex") else ""))
    for k, zh in (("ride", "騎寵"), ("pet", "戰寵")):
        if k in t:
            parts.append(f"{zh} {t[k].get('name') or t[k].get('petId')}（{t[k].get('uniqueIndex')}）")
    if "role_cmd" in t:
        parts.append(f"人物一般動作 {describe_cmd(t['role_cmd'])}")
    if "pet_cmd" in t:
        parts.append(f"寵物一般動作 {describe_cmd(t['pet_cmd'])}")
    for k in ("deposit",):
        if t.get(k):
            parts.append(f"放回個人倉 {[d[0] for d in t[k]]}")
    return "、".join(parts) or "（沒有）"


def show(store: Store, sc: str, tag: str, names: Optional[List[str]] = None) -> List[str]:
    sc = scenario_name(sc)
    base = store.require_base(tag)
    d = store.load_scenario(sc)
    if d is None:
        raise LoadoutError(f"沒有情境「{sc}」（{store.scenario_path(sc)}，也沒有 config/loadouts/{sc}.json）")
    lines = [f"第 {tag} 組「{sc}」（平常配置：{base.get('time')}）"]
    for me in resolve(d, tag, base):
        if names and me["name"] not in names:
            continue
        tgt, unsup = target_of(me)
        diff = [c for c in me["changed"] if c != "duty"]
        lines.append(f"  {me['name']}（{me.get('duty') or '-'}）：{describe_target(tgt)}")
        lines.append(f"      跟平常不同：{'、'.join(diff) if diff else '無'}")
        for u in unsup:
            lines.append(f"      （只記錄、不自動切：{u}）")
    return lines


# ---- 離線指令 ----
def _cmd_tuple(s: str) -> List[Any]:
    p = [x.strip() for x in str(s).replace("，", ",").split(",")]
    if len(p) != 3 or not all(x.lstrip("-").isdigit() for x in p):
        raise LoadoutError(f"一般動作要寫 類型,技能ID,等級（例 1,0,0 攻擊；3,10185,5 大地的精靈）：{s}")
    return [int(p[0]), p[1], p[2]]


def set_layer(store: Store, sc: str, tag: str, a) -> dict:
    """改情境檔的一層（--name 角色／--duty 職責／--all）：只改給的欄位，其他照舊。回傳改完那一層。"""
    sc = scenario_name(sc)
    if sc == BASE_NAME:
        raise LoadoutError("平常配置不手改：在遊戲裡調好再 `jobs loadout --do save --apply`（平常是所有情境的底）")
    picked = [x for x in (a.name, a.duty, a.all or None) if x]
    if len(picked) != 1:
        raise LoadoutError("一次改一層：--name 角色、--duty 職責、--all 三選一")
    base = store.require_base(tag)
    d = copy.deepcopy(store.load_scenario(sc) or {"note": f"{sc}（loadout set 建立）"})
    if a.name:
        if a.name not in [r.get("name") for r in base["roles"]]:
            raise LoadoutError(f"第 {tag} 組平常配置裡沒有 {a.name}")
        layer = d.setdefault("profiles", {}).setdefault(str(tag), {}).setdefault(a.name, {})
    elif a.duty:
        layer = d.setdefault("roles", {}).setdefault(a.duty, {})
    else:
        layer = d.setdefault("all", {})
    if a.plan:
        layer["plan"] = a.plan
    for e in a.equip or []:
        slot, _, rest = e.partition("=")
        slot = next((k for k, v in SLOT_ZH.items() if v == slot), slot)
        iid, _, uid = rest.partition(":")
        if slot not in SLOT_STORGE or not iid.isdigit() or (uid and not uid.isdigit()):
            raise LoadoutError(f"--equip 要寫 部位=道具ID[:uniqueIndex]（例 necklaceEquip=12345 或 項鍊=12345）：{e}")
        layer.setdefault("equip", {})[slot] = {"itemId": int(iid), **({"uniqueIndex": int(uid)} if uid else {})}
    for k, v in (("ride", a.ride), ("pet", a.pet)):
        if v:
            if not str(v).isdigit():
                raise LoadoutError(f"--{k} 要給寵物 uniqueIndex（數字）：{v}")
            layer[k] = {"uniqueIndex": int(v)}
    if a.role_cmd:
        t = _cmd_tuple(a.role_cmd)
        layer.setdefault("waigua", {}).update(autoCmd=t[0], autoCmdArg=t[1], autoCmdArgEx=t[2])
    if a.pet_cmd:
        t = _cmd_tuple(a.pet_cmd)
        layer.setdefault("waigua", {}).update(petAutoCmdType=t[0], petAutoCmdArg=t[1], petAutoCmdArgEx=t[2])
    if not a.dry_run:
        store.write(store.scenario_path(sc), d, f"set {sc} {tag} {a.name or a.duty or 'all'}")
    return layer


def delete_layer(store: Store, sc: str, tag: str, names: Optional[List[str]], dry_run: bool = False) -> str:
    sc = scenario_name(sc)
    if sc == BASE_NAME:
        raise LoadoutError("平常配置不能刪（是所有情境的底）")
    d = load_json(store.scenario_path(sc))
    if d is None:
        raise LoadoutError(f"這台沒有自己的「{sc}」情境檔（{store.scenario_path(sc)}）")
    prof = d.get("profiles") or {}
    if str(tag) not in prof:
        raise LoadoutError(f"第 {tag} 組在「{sc}」沒有自己的設定")
    if names:
        for n in names:
            prof[str(tag)].pop(n, None)
    if not names or not prof[str(tag)]:
        prof.pop(str(tag), None)
    if not dry_run:
        store.write(store.scenario_path(sc), d, f"delete {sc} {tag} {names or '整組'}")
    return f"第 {tag} 組「{sc}」刪掉：{'、'.join(names) if names else '整組'}"


def main(argv: Optional[List[str]] = None, out=print) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.loadout", description="人物配置（離線：查詢、改情境、備份還原）")
    ap.add_argument("--dir", default=None, help=f"配置根目錄（預設 {ENV_DIR} 或 captures/loadouts）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list")
    s.add_argument("--profile", required=True)
    s = sub.add_parser("show")
    s.add_argument("scenario")
    s.add_argument("--profile", required=True)
    s.add_argument("--name", nargs="+")
    s = sub.add_parser("set")
    s.add_argument("scenario")
    s.add_argument("--profile", required=True)
    s.add_argument("--name")
    s.add_argument("--duty", help="職責（隊長／隊員…）：這一層套在所有這個職責的角色")
    s.add_argument("--all", action="store_true", help="改 all 層（所有組、所有角色）")
    s.add_argument("--plan")
    s.add_argument("--equip", action="append", help="部位=道具ID[:uniqueIndex]，可多次")
    s.add_argument("--ride", help="騎寵 uniqueIndex")
    s.add_argument("--pet", help="戰寵 uniqueIndex")
    s.add_argument("--role-cmd", help="人物一般動作 類型,技能ID,等級（1,0,0 攻擊）")
    s.add_argument("--pet-cmd", help="寵物一般動作 類型,技能ID,等級")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("delete")
    s.add_argument("scenario")
    s.add_argument("--profile", required=True)
    s.add_argument("--name", nargs="+")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("history")
    s = sub.add_parser("revert")
    s.add_argument("backup")
    a = ap.parse_args(argv)
    store = Store(a.dir or default_dir())
    try:
        if a.cmd == "list":
            base = store.require_base(a.profile)
            out(f"第 {a.profile} 組（{'、'.join(r['name'] for r in base['roles'])}）；平常配置 {base.get('time')}")
            for sc in store.scenarios():
                out(f"  {sc}")
        elif a.cmd == "show":
            for line in show(store, a.scenario, a.profile, a.name):
                out(line)
        elif a.cmd == "set":
            layer = set_layer(store, a.scenario, a.profile, a)
            out(f"{'（dry-run，沒寫入）' if a.dry_run else '已寫入'}：{json.dumps(layer, ensure_ascii=False)}")
        elif a.cmd == "delete":
            out(delete_layer(store, a.scenario, a.profile, a.name, a.dry_run))
        elif a.cmd == "history":
            rows = store.history()
            for r in rows:
                out(f"  {r['t']}  {r['file']}  ← 備份 {r['backup']}（{r['why']}）")
            if not rows:
                out("還沒有備份紀錄")
        elif a.cmd == "revert":
            out(f"已還原 {store.revert(a.backup)}")
    except LoadoutError as e:
        out(f"停止：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
