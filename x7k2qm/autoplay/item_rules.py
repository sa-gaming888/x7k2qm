"""道具／寵物處置規則：唯一來源是 `doc/autoplay/game/item-rules.md` 的兩張表（這裡只解析、不另存一份）。

- 「處置規則」表：種類（道具／寵物）、ID、名稱、處置、條件、核准、出處。
- 「保護名單」表：保護 ID、名稱、理由。另外 `Task.json` 任何任務條件用到的 itemId／petId 也自動保護。

用法：`rules = load()`；`rules.item_action(itemId, ctx)` → 處置字串或 None（沒有規則或條件不成立）。
ctx 是 `Ctx(trans, finished, depot)`；轉數／完成任務／個倉件數不知道（None）時，
有用到它的條件一律當不成立（不動）——寧可不刪。
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_MD = os.path.join(ROOT, "doc", "autoplay", "game", "item-rules.md")

ITEM_ACTIONS = {"delete", "store_private", "store_public", "equip", "keep", "sell", "eat"}
# release_below3：1 級、檔差未滿 +3 的放生，其他（+3 以上、練過的、看不出檔差的）照 store_pet_private 存個倉
# （使用者 11-09「金虎 金飛 紅爆 藍爆 如果沒+3 以上都丟棄」，選「加進規則表」「只丟 1 級的」）
PET_ACTIONS = {"release", "release_below3", "store_pet_private", "keep"}
IRREVERSIBLE = {"delete", "sell", "release", "release_below3"}       # 不可逆：不能碰保護名單
KIND = {"道具": "item", "寵物": "pet"}
_COND = re.compile(r"^(trans)(=|>=|<=)(\d+)$|^(done)=(\d+)$|^(depot)(<|>=)(\d+)$|^(leftover)$")


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Ctx:
    trans: Optional[int] = None                 # 角色轉數；None＝不知道
    finished: Optional[frozenset] = None        # 已完成的任務群組；None＝不知道
    # 這個角色**個人倉庫**裡每個 itemId 的件數（(itemId, 件數) 的 tuple，frozen 要可雜湊所以不用 dict）。
    # None＝沒查倉庫（不知道）→ 用到 depot 的條件一律不成立，什麼都不動（寧可不刪）。
    depot: Optional[Tuple[Tuple[int, int], ...]] = None

    def depot_count(self, item_id: int) -> Optional[int]:
        if self.depot is None:
            return None
        return dict(self.depot).get(item_id, 0)


@dataclass(frozen=True)
class Rule:
    kind: str           # item / pet
    id: int
    name: str
    action: str
    cond: Tuple[str, ...]
    approved: str
    source: str

    def holds(self, ctx: Ctx) -> bool:
        for c in self.cond:
            m = _COND.match(c)
            if m.group(9):
                continue                            # leftover：只是標記（任務用剩的，使用者核准可丟），永遠成立
            if m.group(1):
                if ctx.trans is None:
                    return False
                n = int(m.group(3))
                ok = {"=": ctx.trans == n, ">=": ctx.trans >= n, "<=": ctx.trans <= n}[m.group(2)]
            elif m.group(4):
                ok = ctx.finished is not None and int(m.group(5)) in ctx.finished
            else:
                have = ctx.depot_count(self.id)     # 個倉件數；不知道就不成立
                if have is None:
                    return False
                n = int(m.group(8))
                ok = have < n if m.group(7) == "<" else have >= n
            if not ok:
                return False
        return True


@dataclass
class Rules:
    rules: List[Rule]
    protect: Dict[int, str]                       # 保護名單（表）：itemId → 理由
    task_items: Set[int] = field(default_factory=set)   # Task.json 條件用到的 itemId
    task_pets: Set[int] = field(default_factory=set)    # Task.json 條件用到的 petId

    def _first(self, kind: str, rid: int, ctx: Ctx) -> Optional[Rule]:
        return next((r for r in self.rules if r.kind == kind and r.id == rid and r.holds(ctx)), None)

    def item_rule(self, item_id: int, ctx: Ctx) -> Optional[Rule]:
        return self._first("item", item_id, ctx)

    def pet_rule(self, pet_id: int, ctx: Ctx) -> Optional[Rule]:
        return self._first("pet", pet_id, ctx)

    def protected_items(self) -> Set[int]:
        return set(self.protect) | self.task_items

    def blocks(self, rule: "Rule") -> bool:
        """不可逆的規則碰到保護道具 → True（不准做）。保護名單（表）一律不准；
        只是任務條件用到的道具，規則**有寫 done=群組**（那個任務這一轉已經做完）或 **leftover**
        （使用者核准：任務用剩的就丟）才放行（耐久力回複藥 1501：英雄島前傳 2031 的條件；
        使用者 2026-10-25「耐久藥是因為任務剩才丟」）。"""
        if rule.action not in IRREVERSIBLE:
            return False
        if rule.kind == "pet":
            return rule.id in self.task_pets
        if rule.id in self.protect:
            return True
        return rule.id in self.task_items and not any(c.startswith("done=") or c == "leftover" for c in rule.cond)

    def ids(self, kind: str, actions: Iterable[str]) -> Set[int]:
        acts = set(actions)
        return {r.id for r in self.rules if r.kind == kind and r.action in acts}


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(text: str) -> List[List[List[str]]]:
    """markdown 裡所有表格（每個表格＝列的清單，第一列是表頭，分隔列已去掉）。"""
    out, cur = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            cells = _cells(line)
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            cur.append(cells)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _int(s: str, where: str) -> int:
    s = s.strip("` ")
    if not s.isdigit():
        raise RuleError(f"{where}：ID 不是數字「{s}」")
    return int(s)


def parse(text: str) -> Tuple[List[Rule], Dict[int, str]]:
    rules: List[Rule] = []
    protect: Dict[int, str] = {}
    seen_rules = seen_protect = False
    for tab in _tables(text):
        head = tab[0]
        if head[:5] == ["種類", "ID", "名稱", "處置", "條件"]:
            seen_rules = True
            for row in tab[1:]:
                where = f"處置規則「{' | '.join(row)}」"
                if len(row) != len(head):
                    raise RuleError(f"{where}：欄數 {len(row)} 不是 {len(head)}")
                kind = KIND.get(row[0])
                if kind is None:
                    raise RuleError(f"{where}：種類只能是 道具／寵物")
                action = row[3].strip("` ")
                if action not in (ITEM_ACTIONS if kind == "item" else PET_ACTIONS):
                    raise RuleError(f"{where}：{row[0]}不能用處置 {action}")
                raw = row[4].strip("` ")
                cond = tuple(c.strip().replace(" ", "") for c in re.split(r"[、,，]", raw)) if raw not in ("—", "-", "") else ()
                for c in cond:
                    if not _COND.match(c):
                        raise RuleError(f"{where}：看不懂的條件 {c}（只認 trans=N、trans>=N、trans<=N、done=群組、depot<N、depot>=N、leftover）")
                if not row[5].strip():
                    raise RuleError(f"{where}：核准日期沒寫")
                rules.append(Rule(kind, _int(row[1], where), row[2], action, cond, row[5], row[6]))
        elif head[:2] == ["保護 ID", "名稱"]:
            seen_protect = True
            for row in tab[1:]:
                protect[_int(row[0], f"保護名單「{' | '.join(row)}」")] = row[2] if len(row) > 2 else ""
    if not seen_rules or not seen_protect:
        raise RuleError("規則檔少了「處置規則」或「保護名單」表（表頭要照原格式）")
    return rules, protect


def task_requirements(tasks: list) -> Tuple[Set[int], Set[int]]:
    """Task.json 任何任務 condition／conditionEx 用到的 (itemId, petId)。"""
    items, pets = set(), set()
    for t in tasks:
        for key in ("condition", "conditionEx"):
            for c in t.get(key) or []:
                if isinstance(c, dict):
                    if c.get("itemId"):
                        items.add(c["itemId"])
                    if c.get("petId"):
                        pets.add(c["petId"])
    return items, pets


def load(path: Optional[str] = None, tasks: Optional[list] = None) -> Rules:
    """path 沒給：環境變數 BOT_ITEM_RULES（設定檔 [files] item_rules，容器可掛載自己的規則表）＞內建 RULES_MD。"""
    path = path or os.environ.get("BOT_ITEM_RULES") or RULES_MD
    with open(path, encoding="utf-8") as fp:
        rules, protect = parse(fp.read())
    if tasks is None:
        from ..features.gamedb.tables import CONFIGS
        with open(os.path.join(CONFIGS, "Task.json"), encoding="utf-8") as fp:
            tasks = json.load(fp)
    items, pets = task_requirements(tasks)
    return Rules(rules, protect, items, pets)
