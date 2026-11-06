"""auction：背包裡的道具上架集市（交易所）換石幣（使用者 2026-11-06「這兩個帳號各上架一個阿布水，賣一百萬石幣，
然後把到期日告訴我，我用其他人去買」——用自己別的帳號買下，把石幣轉過來）。

    python -X utf8 -m x7k2qm.autoplay.jobs auction --profile 4 --item 2400 --price 1000000                 # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs add auction --profile 4 --item 2400 --price 1000000 --apply --confirm-list

協定 `Other_MyItemShopGoods{action, itemIndex, price, currency, itemNum, extra, canCallPrice}` →
`{itemList: newshopGoodList[], isUploadOk}`（`data/sproto_schema.json`；**本專案還沒實機**，action 的值沒有錄檔）：
- action 0、itemIndex 0：查自己的上架清單（打開交易所「我的上架」；不改任何東西）。
- action 1：上架背包那一格。
成功＝`isUploadOk` true、背包那格少了、清單多一筆（uniqueIndex 新的、price 對）。三樣有一樣對不上就停：
上架前沒確認 → JobError；送了上架、狀態對不上 → JobHold（不重送，停在原地等人看）。
清單每筆的 `overTime` 是到期 Unix 秒，結果裡回報給使用者。
"""

import time
from typing import List, Tuple

from . import JobError, JobHold

NAME = "auction"
HELP = "道具上架集市（--item 道具 ID、--price 單價，石幣；--apply 要再加 --confirm-list）"
PAUSE = lambda a: bool(getattr(a, "apply", False))      # noqa: E731

ACTION_LIST, ACTION_UPLOAD = 0, 1
STONE = 3                           # 幣種：石幣（doc/features/shop.md §3.2）
MAX_PRICE = 100_000_000             # 上限保護（打錯一個 0）
UPLOAD_WAIT = 8.0


def add_args(ap) -> None:
    ap.add_argument("--item", type=int, required=True, help="要上架的道具 ID（例：阿布的水 2400，不能是綁定的）")
    ap.add_argument("--price", type=int, required=True, help="單價（石幣）")
    ap.add_argument("--count", type=int, default=1, help="上架幾個（預設 1）")
    ap.add_argument("--who", default=None, help="哪一隻上架（預設隊長）")
    ap.add_argument("--confirm-list", dest="confirm_list", action="store_true", help="--apply 時一定要加（上架會把道具交給交易所）")


def check(args) -> None:
    if not 1 <= int(args.price) <= MAX_PRICE:
        raise JobError(f"--price 要在 1～{MAX_PRICE:,}")
    if int(getattr(args, "count", 1) or 1) < 1:
        raise JobError("--count 要 ≥ 1")
    if getattr(args, "apply", False) and not getattr(args, "confirm_list", False):
        raise JobError("上架要加 --confirm-list")


def when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def my_goods(c) -> List[dict]:
    r = c.request("Other_MyItemShopGoods", {"action": ACTION_LIST, "itemIndex": 0, "price": 0, "currency": 0,
                                           "itemNum": 0, "extra": "", "canCallPrice": False}) or {}
    rows = r.get("itemList") or []
    return list(rows.values()) if isinstance(rows, dict) else list(rows)


def run(p, args) -> Tuple[str, dict]:
    check(args)
    names = [n for _, n in p.party]
    who = getattr(args, "who", None) or names[0]
    if who not in names:
        raise JobError(f"--who {who} 不在這組")
    item, price, count = int(args.item), int(args.price), int(getattr(args, "count", 1) or 1)
    name = (p.item_table().get(item, {}) or {}).get("nameTn") or str(item)
    cells = [it for it in p.bag(who) if it.get("itemId") == item and not it.get("bind")]
    have = sum(int(it.get("overlay") or 1) for it in cells)
    p.log(f"集市：{who} 上架 {name}×{count}，單價 {price:,} 石幣（背包有 {have} 個）")
    if not getattr(args, "apply", False):
        return "只列要上架什麼（沒加 --apply，沒有送任何動作）", {"who": who, "item": item, "have": have}
    if have < count:
        raise JobError(f"{who} 背包只有 {have} 個 {name}（要 {count}），先買或從倉庫領")
    if not p.wait(lambda: not p.fighting.get(who), 60):
        raise JobError(f"{who} 還在戰鬥中，不上架")
    c = p.clients[who]
    before = {g.get("uniqueIndex") for g in my_goods(c)}
    cell = cells[0]
    r = c.request("Other_MyItemShopGoods", {"action": ACTION_UPLOAD, "itemIndex": cell["itemIndex"], "price": price,
                                           "currency": STONE, "itemNum": count, "extra": "", "canCallPrice": False}) or {}
    gone = p.wait(lambda: sum(int(it.get("overlay") or 1) for it in p.bag(who)
                              if it.get("itemId") == item and not it.get("bind")) <= have - count, UPLOAD_WAIT)
    rows = r.get("itemList")
    rows = (list(rows.values()) if isinstance(rows, dict) else list(rows or [])) or my_goods(c)
    new = [g for g in rows if g.get("uniqueIndex") not in before and int(g.get("price") or 0) == price]
    if not r.get("isUploadOk") and not gone and not new:
        raise JobError(f"{who} 上架 {name} 被拒（回應 isUploadOk={r.get('isUploadOk')}），道具還在背包")
    if not (r.get("isUploadOk") and gone and new):
        raise JobHold(f"{who} 上架 {name} 對不上：isUploadOk={r.get('isUploadOk')}、背包少了={gone}、清單新的一筆={new}，停在原地")
    g = new[0]
    p.log(f"集市：{who} 上架 {name}×{count}，{price:,} 石幣，到期 {when(g.get('overTime'))}（編號 {g.get('uniqueIndex')}）")
    return (f"{who} 上架 {name}×{count}，{price:,} 石幣，到期 {when(g.get('overTime'))}",
            {"who": who, "item": item, "price": price, "overTime": g.get("overTime"), "uniqueIndex": g.get("uniqueIndex")})
