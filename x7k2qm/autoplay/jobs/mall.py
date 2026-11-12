"""mall：在商城買道具（`ShopMall_BuyShopMallItem`）。**只准花活力、石幣**；聲望要 `--allow-fame`、鑽石（真錢）要 `--allow-diamond`，兩個都要使用者當次明講。

    python -X utf8 -m x7k2qm.autoplay.jobs mall --profile 3 --discover --id 46                    # 只列：買什麼、多少錢、餘額
    python -X utf8 -m x7k2qm.autoplay.jobs add mall --profile 3 --discover --apply --id 46          # 隊長買 1 個
    python -X utf8 -m x7k2qm.autoplay.jobs add mall --profile 3 --discover --apply --id 46 --count 2 --who 角色81

`--id` 是 `ShopMall.json` 的列號（不是道具 ID）。表的 `group` 就是幣種（1 鑽石、2 活力、3 石幣、8 聲望，
跟 `ShopMallName` 分頁名對得上），`priceDefaultInit` 是單價。常用：
- 46：2倍智慧果（綁定）90638，1,000,000 石幣。**果子只能用石幣在石幣商店買**（使用者 2026-11-02「以後買果子 只能用石幣去石幣商店買」）；
  18 是同一顆果子 60 活力，現在一律拒絕（10-31 使用者「活力 買一顆果子2」那次之後改了）。
- 44：阿布的水 2400，10,000 石幣（`herobf` 自己會買，見 `jobs/herobf.py`）。

花費規則（CLAUDE.md 規範 8）：鑽石（真錢）不花、聲望不主動花 → 那兩個分頁的列直接拒絕；
活力、石幣是遊戲內賺得到的。同一個道具石幣分頁也有（例：46 是同一顆果子 1,000,000 石幣）時要自己比價、挑划算的。
送前核對：餘額夠全部要買的量、背包至少 1 格空。送後核對（每買 1 個）：背包那個道具 **+1**、
幣種餘額 **正好 −單價**；扣了錢卻對不上 → hold 停在原地，沒扣錢 → 停，不重送。
活力、聲望、石幣都是帳號共用（看哪隻都一樣，11-11 實測）；送後核對看買的那隻。
"""

import json
import os
from functools import lru_cache
from typing import Optional, Tuple

from . import JobError, JobHold

NAME = "mall"
HELP = "商城買道具（--id ShopMall 列號、--count；只准活力／石幣，不加 --apply 只列）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

CURRENCY = {1: "鑽石", 2: "活力", 3: "石幣", 8: "聲望"}
FIELD = {2: "gold", 3: "stone"}       # 准花的幣種 → RoleAttr 欄位（快照 money）
STONE = 3
FRUIT_MARK = "果"                     # 道具名有「果」＝果子（智慧果…）：只准石幣分頁（使用者 11-02）
FAME, FAME_FIELD = 8, "lockDenar"     # 聲望：只有加 --allow-fame（使用者當次同意）才准，一次最多 FAME_MAX
FAME_MAX = 10
# 鑽石＝真錢，規範第 8 點「一律不花」。只有加 --allow-diamond（使用者當次明講破例）才准，
# 而且一次最多 DIAMOND_MAX（2026-11-02 第六組：使用者破例買 1 個 3倍聰明的豆子 90399，50 鑽石）。
DIAMOND, DIAMOND_FIELD = 1, "denar"
DIAMOND_MAX = 50
BAG_SLOTS = 15
MAX_COUNT = 5                         # 上限保護：一次最多買 5 個
BUY_WAIT = 8.0


def add_args(ap) -> None:
    ap.add_argument("--id", type=int, default=None, help="ShopMall 列號（例：18＝2倍智慧果（綁定）60 活力）")
    ap.add_argument("--count", type=int, default=1, help=f"買幾個（1～{MAX_COUNT}）")
    ap.add_argument("--who", default=None, help="哪一隻買（預設隊長）")
    ap.add_argument("--allow-diamond", dest="allow_diamond", action="store_true",
                    help=f"准花鑽石＝真錢（規範第 8 點一律不花；使用者當次明講破例才加，一次最多 {DIAMOND_MAX} 鑽石）")
    ap.add_argument("--allow-fame", dest="allow_fame", action="store_true",
                    help=f"准花聲望（使用者當次明講同意才加；一次最多 {FAME_MAX} 聲望）。例：40＝瑪蕾菲雅喜歡的項鏈 19624，1 聲望")


@lru_cache(maxsize=None)
def _rows() -> tuple:
    from ...features.gamedb.tables import CONFIGS
    with open(os.path.join(CONFIGS, "ShopMall.json"), encoding="utf-8") as fp:
        return tuple(json.load(fp))


def row_of(mall_id: int) -> dict:
    row = next((r for r in _rows() if r.get("ID") == mall_id), None)
    if row is None:
        raise JobError(f"商城表沒有第 {mall_id} 列")
    return row


def item_name(p, item_id: int) -> str:
    return (p.item_table().get(item_id, {}) or {}).get("nameTn") or str(item_id)


def count_of(p, name: str, item_id: int) -> int:
    return sum(it.get("overlay", 1) for it in p.bag(name) if it.get("itemId") == item_id)


def balance(p, name: str, field: str) -> Optional[int]:
    with p.lock:
        return (p.money.get(name) or {}).get(field)


def run(p, args) -> Tuple[str, dict]:
    if getattr(args, "id", None) is None:
        raise JobError("要給 --id（ShopMall 列號）")
    row = row_of(args.id)
    group, item, price = row.get("group"), row.get("goodsId"), int(row.get("priceDefaultInit") or 0)
    cur = CURRENCY.get(group, f"幣種 {group}")
    fields = dict(FIELD)
    if getattr(args, "allow_fame", False):
        fields[FAME] = FAME_FIELD
    if getattr(args, "allow_diamond", False):
        fields[DIAMOND] = DIAMOND_FIELD
    if FRUIT_MARK in item_name(p, item) and group != STONE:
        # 使用者 2026-11-02「以後買果子 只能用石幣去石幣商店買」：活力、聲望、鑽石分頁的果子一律不買（旗標也不放行）
        alt = [r["ID"] for r in _rows() if r.get("goodsId") == item and r.get("group") == STONE]
        raise JobError(f"商城第 {args.id} 列的 {item_name(p, item)} 用{cur}賣，不買：果子只能用石幣在石幣商店買"
                       + (f"（石幣的同款是第 {'、'.join(map(str, alt))} 列）" if alt else "（石幣商店沒有這一款）"))
    if group not in fields:
        raise JobError(f"商城第 {args.id} 列用{cur}賣，不買"
                       f"（只准活力、石幣；聲望要 --allow-fame、鑽石要 --allow-diamond）")
    count = int(getattr(args, "count", 1) or 1)
    if not 1 <= count <= MAX_COUNT:
        raise JobError(f"--count 要在 1～{MAX_COUNT}")
    if price <= 0:
        raise JobError(f"商城第 {args.id} 列單價 {price} 不對，不買")
    names = [n for _, n in p.party]
    who = getattr(args, "who", None) or names[0]
    if who not in names:
        raise JobError(f"--who {who} 不在這組")
    field, total = fields[group], price * count
    if group == FAME and total > FAME_MAX:
        raise JobError(f"這次要花 {total} 聲望，超過上限 {FAME_MAX}，不買")
    if group == DIAMOND and total > DIAMOND_MAX:
        raise JobError(f"這次要花 {total} 鑽石（真錢），超過上限 {DIAMOND_MAX}，不買")
    have = balance(p, who, field)
    plan = {"who": who, "item": item_name(p, item), "count": count, "currency": cur, "total": total, "have": have}
    p.log(f"商城：{who} 買 {item_name(p, item)}×{count}（第 {args.id} 列，{price:,} {cur}／個，共 {total:,}；"
          f"現有 {cur} {have if have is None else f'{have:,}'}）")
    if not getattr(args, "apply", False):
        return "只列商城要買什麼（沒加 --apply，沒有送任何動作）", {"plan": plan}
    if have is None or have < total:
        raise JobError(f"{cur} {have} 不夠買 {count} 個（要 {total:,}）")
    if not p.wait(lambda: not p.fighting.get(who), 60):
        raise JobError(f"{who} 還在戰鬥中，商城不會回應")
    c = p.clients[who]
    for k in range(count):
        if BAG_SLOTS - len(p.bag(who)) < 1:
            raise JobError(f"{who} 背包沒空格，買不了")
        m0, c0 = balance(p, who, field), count_of(p, who, item)
        r = c.request("ShopMall_BuyShopMallItem", {"shopMallId": args.id, "itemNum": 1,
                                                    "pcRid": c.rid, "itemId": item}) or {}
        got = p.wait(lambda: count_of(p, who, item) > c0 and balance(p, who, field) == m0 - price, BUY_WAIT)
        if not got:
            if balance(p, who, field) != m0:
                raise JobHold(f"商城買 {item_name(p, item)}：{cur} {m0} → {balance(p, who, field)}、"
                              f"件數 {c0} → {count_of(p, who, item)}，對不上，停在原地")
            raise JobError(f"商城買 {item_name(p, item)} 沒確認（回應 {r}），不再買")
        p.log(f"商城：{who} 買到 {item_name(p, item)}（第 {k + 1}／{count} 個），{cur} {m0:,} → {m0 - price:,}")
        p.nap(0.6)
    return f"商城買了 {item_name(p, item)}×{count}，花 {total:,} {cur}", {"plan": plan}
