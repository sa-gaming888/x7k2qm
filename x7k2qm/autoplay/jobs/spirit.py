"""spirit：隊長依自己的屬性去競技場買精靈金幣，60 級才穿（使用者 2026-11-04：「新手登入時，隊長要去 pk 場買大地精靈裝備上去」
「會偵測人物是什麼屬性，然後去 pk 場買對應的精靈」「可以先買，60 級後，隊長穿上去」）。

    python -X utf8 -m x7k2qm.autoplay.jobs add spirit --profile 2 --discover            # 只列：屬性、要買哪一個、現在戴什麼
    python -X utf8 -m x7k2qm.autoplay.jobs add spirit --profile 2 --discover --apply

- 屬性：隊長 RoleAttr 地／水／火／風最高的那一種（`trip_spirit.element_of`）。
- 買：競技場道具屋 Lv5 金幣，500 石幣（全遊戲最便宜；地／水在 2043、火／風在 2044），只買 1 個（戰場屬性全隊共用）。
  流程同 `trip_spirit.Trip.arena_buy`：回村 → 鬥技場嚮導（要先拆隊）→ 道具屋 → 回村；回練功由常駐做（resume_training）。
- 穿：金幣要 60 級（Item.level）。隊長已經 ≥ 60 → 當場穿、首次動作設成這個精靈打隨機敵人；不到 60 → 放背包。
  常駐帶 `--auto-spirit` 時，隊長到 60 級會自己再排一次這個工作（背包有就只穿，不再買）。
- 已經戴著同屬性、精靈 Lv5 以上的道具 → 什麼都不做（同屬性但比較弱的，例 Lv1 貝鈴，照樣買、60 級換上）。
  背包已經有那枚 → 不再買。背包沒有就先查個倉第 1 頁（規則表「備用的精靈金幣存個倉」：等 60 級時被自動整理存進去）
  → 有就不買，60 級再取出來穿。
- 出錯就停（規範 2）：買到的不是要的那個、穿戴被拒、傳送等不到 → JobError，不重試。
"""

from typing import Optional, Tuple

from . import JobError

NAME = "spirit"
HELP = "隊長依屬性去競技場買精靈金幣（500 石幣），60 級才穿；不加 --apply 只列"
PAUSE = lambda args: bool(getattr(args, "apply", False))   # noqa: E731  只列時不用停練功


def add_args(ap) -> None:
    pass


def plan(p) -> dict:
    """隊長屬性、要買的金幣、現在戴的／背包裡的、等級夠不夠。只讀記憶體。"""
    from ..trip_spirit import ARENA_BY_ELEMENT, ARENA_SPIRITS, element_of
    if not p.party:
        raise JobError("沒有角色")
    lead = p.party[0][1]
    with p.lock:
        attr = dict(p.attrs.get(lead) or {})
        items = [dict(v) for v in p.items.get(lead, {}).values() if v.get("overlay", 1) > 0 and v.get("itemId")]
    element = element_of(attr)
    if element is None:
        raise JobError(f"{lead} 的人物屬性看不出來（地水火風都是 0 或還沒收到 RoleAttr）")
    shop, _, index, item_id = ARENA_BY_ELEMENT[element]
    need = int(p.item_table().get(item_id, {}).get("level") or 0)
    worn = p.spirit_coin(lead)
    in_bag = next((v for v in items if v.get("itemId") == item_id and v.get("itemStorge", 0) == 0), None)
    return {"lead": lead, "element": element, "itemId": item_id, "name": ARENA_SPIRITS[item_id], "shop": shop,
            "shopIndex": index, "level": attr.get("level"), "need": need, "worn": worn,
            "inBag": in_bag.get("itemIndex") if in_bag else None}


def wears_element(p, worn: Optional[Tuple[int, int, int]], item_id: int) -> bool:
    """身上已經戴著同屬性、精靈等級不比要買的低的道具 → 不用再買。
    同屬性但比較弱的（例：Lv1 貝鈴）照樣買 Lv5 金幣，60 級換上。"""
    if worn is None:
        return False
    sk = (p.item_table().get(item_id, {}).get("skill") or [{}])[0]
    return worn[1] == sk.get("skillId") and worn[2] >= int(sk.get("skillLevel") or 1)


def in_depot(p, lead: str, item_id: int) -> Optional[dict]:
    """個人倉庫第 1 頁有沒有這枚（唯讀查詢）。規則表「備用的精靈金幣存個倉」：60 級前先買的那枚，
    背包快滿自動整理時會被存進去——不查就會再花 500 石幣買第二枚。查詢被拒 → JobError（不猜）。"""
    r = p.clients[lead].request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": False}) or {}
    if r.get("isOK") is False:
        raise JobError(f"{lead} 查個人倉庫被拒：{r}")
    rows = r.get("itemInfoList") or []
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)
    return next((x for x in rows if x.get("itemId") == item_id), None)


def take_out(p, lead: str, row: dict, item_id: int, name: str) -> int:
    """從個倉取出到背包，回傳背包 itemIndex。"""
    r = p.clients[lead].request("ItemHandle_GetItemToNewDepot",
                                {"page": 1, "isPublic": False, "uniqueIndex": row["uniqueIndex"], "count": 1}) or {}
    if not r.get("isOK"):
        raise JobError(f"{lead} 從個倉取出 {name} 被拒：{r}")
    p.nap(2)                                          # 等 Item_ItemInfo 推回背包那一格
    with p.lock:
        idx = next((v["itemIndex"] for v in p.items.get(lead, {}).values()
                    if v.get("itemId") == item_id and v.get("itemStorge", 0) == 0 and v.get("overlay", 1) > 0), None)
    if idx is None:
        raise JobError(f"{lead} 取出 {name} 回 isOK，但背包沒看到")
    return idx


def equip(p, lead: str, index: int, name: str) -> None:
    r = p.clients[lead].request("ItemHandle_itemEquipOn", {"itemIndex": index}) or {}
    if not r.get("isOK"):
        raise JobError(f"{lead} 穿 {name} 被拒：{r}")
    p.nap(2)                                          # 等 Item_ItemInfo 推回 itemStorge，spirit_coin 才看得到
    if p.spirit_coin(lead) is None:
        raise JobError(f"{lead} 穿 {name} 回 isOK，但身上沒看到精靈道具")
    if not p.ensure_first_cmd(lead):
        raise JobError(f"{lead} 已穿 {name}，但首次動作沒設成")


def run(p, args) -> Tuple[str, dict]:
    pl = plan(p)
    lead, name = pl["lead"], pl["name"]
    head = f"{lead}（{pl['element']}屬性、Lv{pl['level']}）"
    if wears_element(p, pl["worn"], pl["itemId"]):
        return f"{head} 已經戴著{pl['element']}屬性的精靈，不用買", pl
    level_ok = (pl["level"] or 0) >= pl["need"]
    if not getattr(args, "apply", False):
        todo = ("背包已經有，" + ("現在就穿" if level_ok else f"等 {pl['need']} 級再穿")) if pl["inBag"] is not None else \
               f"要去競技場 NPC {pl['shop']} 第 {pl['shopIndex']} 格買（500 石幣）" + ("、當場穿" if level_ok else f"、{pl['need']} 級再穿")
        return f"只列：{head} → {name}：{todo}", pl
    if pl["inBag"] is not None:
        if not level_ok:
            return f"{head} 背包已經有 {name}，等 {pl['need']} 級再穿", dict(pl, waiting=True)
        equip(p, lead, pl["inBag"], name)
        return f"{head} 穿上背包裡的 {name}，首次動作設好", pl
    row = in_depot(p, lead, pl["itemId"])
    if row is not None:
        if not level_ok:
            return f"{head} 個倉已經有 {name}，等 {pl['need']} 級再取出來穿", dict(pl, waiting=True)
        equip(p, lead, take_out(p, lead, row, pl["itemId"], name), name)
        return f"{head} 從個倉取出 {name} 穿上，首次動作設好", pl
    from ..trip_spirit import Stop, Trip
    t = Trip(p.clients[lead], log=p.log)
    # 常駐登入時就把佇列裡的事件 drain 掉了（_register），石幣、地圖那筆推送 Trip 等不到：
    # 11-04 容器第一次實跑「讀取石幣：8 秒內沒等到封包」。用常駐記憶體裡的值起頭（石幣 0 也是有效值）。
    with p.lock:
        t.stone = (p.money.get(lead) or {}).get("stone")
        t.map = p.s.member_maps.get(lead)
    try:
        got, index, worn = t.arena_buy(pl["element"], equip=False)
    except Stop as e:
        raise JobError(f"{lead} 買精靈：{e}") from None
    p.nap(2)
    if level_ok:
        equip(p, lead, index, name)
        return f"{head} 買了 {name}（500 石幣）並穿上，首次動作設好", dict(pl, bought=got)
    return f"{head} 買了 {name}（500 石幣）放背包，{pl['need']} 級再穿", dict(pl, bought=got, waiting=True)
