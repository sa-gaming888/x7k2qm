"""carry：把個人倉庫（第 1 頁）的某個道具取出來，讓每隻背包有 N 個（例：打副本前帶阿布的水）。

    python -X utf8 -m x7k2qm.autoplay.jobs carry --profile 2 --discover --item 2400 --count 8              # 只列
    python -X utf8 -m x7k2qm.autoplay.jobs add carry --profile 2 --discover --apply --item 2400 --count 8

使用者 2026-10-31：「阿布水每人帶 8 瓶」；規則表（阿布的水 store_private）的自動整理把剛買的 40 瓶全存回個倉 →
用這個拿回來（切著情境配置時常駐不再自動整理，LESSONS #120）。
一件一件取：`ItemHandle_GetItemToNewDepot{page 1, isPublic false, uniqueIndex, count 1}`，每件要回 isOK、背包件數變多才算，
個倉不夠就停（不買）；背包要留 1 格。只動個人倉庫第 1 頁。
"""

from typing import Dict, List, Tuple

from . import JobError

NAME = "carry"
HELP = "個人倉庫取出道具，讓每隻背包有 --count 個（--item）"
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731

BAG_SLOTS = 15
MAX_COUNT = 10


def add_args(ap) -> None:
    ap.add_argument("--store", default=None,
                    help="反過來：把背包裡這些道具（ID 逗號分隔）全存進個人倉庫第 1 頁，只動這幾種"
                         "（規則表整理會連帶的東西一起存，例如阿布的水；10-31 騰空間給阿布的水用）")
    ap.add_argument("--item", type=int, default=None, help="道具 ID（例 2400 阿布的水）")
    ap.add_argument("--count", type=int, default=1, help=f"每隻背包要有幾個（1～{MAX_COUNT}）")
    ap.add_argument("--who", default=None, help="只做這些角色（逗號分隔）；預設全隊")
    ap.add_argument("--public", action="store_true",
                    help="改用公共倉庫（共倉，同帳號共用）；使用者 10-31「阿布水存共倉，每個人留 5 個」")
    ap.add_argument("--keep", type=int, default=0, help="--store 時每隻每種留幾個在背包，多的才存（預設 0＝全存）")


def names_of(p, args) -> List[str]:
    names = [n for _, n in p.party]
    who = getattr(args, "who", None)
    if not who:
        return names
    pick = {x.strip() for x in str(who).replace("，", ",").split(",") if x.strip()}
    bad = pick - set(names)
    if bad:
        raise JobError(f"--who 有不在這組的角色：{sorted(bad)}")
    return [n for n in names if n in pick]


def in_bag(p, name: str, iid: int) -> int:
    return sum(it.get("overlay", 1) for it in p.bag(name) if it.get("itemId") == iid)


def depot(p, name: str, public: bool = False) -> List[dict]:
    r = p.clients[name].request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": public}) or {}
    rows = r.get("itemInfoList") or []
    return list(rows.values()) if isinstance(rows, dict) else list(rows)


def store(p, args) -> Tuple[str, dict]:
    """--store：背包裡指定的道具全存個倉（穿著的不動）。每格照 clean.do_store 核對（回 isOK 且那格消失）。"""
    from .clean import do_store
    try:
        ids = {int(x) for x in str(args.store).replace("，", ",").split(",") if x.strip()}
    except ValueError:
        raise JobError(f"--store 要是逗號分隔的道具 ID：{args.store}")
    names = names_of(p, args)
    table = p.item_table()
    public, keep = bool(getattr(args, "public", False)), int(getattr(args, "keep", 0) or 0)
    plan = {}
    for n in names:
        cells, kept = [], {}
        for it in sorted(p.bag(n), key=lambda x: x["itemIndex"]):
            iid = it.get("itemId")
            if iid not in ids:
                continue
            if kept.get(iid, 0) < keep:                  # 每種先留 keep 個在背包
                kept[iid] = kept.get(iid, 0) + it.get("overlay", 1)
                continue
            cells.append((it["itemIndex"], iid))
        plan[n] = cells
        p.log(f"存倉：{n} 要存{'共' if public else '個'}倉 {[(table.get(i, {}) or {}).get('nameTn') or i for _, i in cells]}"
              + (f"（每種留 {keep} 個）" if keep else ""))
    if not getattr(args, "apply", False):
        return "只列要存的（沒加 --apply，沒有送任何動作）", {"plan": plan}
    stored = {}
    for n in names:
        for idx, iid in plan[n]:
            do_store(p, n, idx, iid, public)
            p.nap(0.4)
        stored[n] = len(plan[n])
    return f"存倉：{stored}", {"stored": stored}


def run(p, args) -> Tuple[str, dict]:
    if getattr(args, "store", None):
        return store(p, args)
    iid, count = getattr(args, "item", None), int(getattr(args, "count", 1))
    if not iid:
        raise JobError("要給 --item")
    if not 1 <= count <= MAX_COUNT:
        raise JobError(f"--count 要在 1～{MAX_COUNT}")
    names = names_of(p, args)
    public = bool(getattr(args, "public", False))
    where = "共倉" if public else "個倉"
    name_of = (p.item_table().get(iid, {}) or {}).get("nameTn") or str(iid)
    plan: Dict[str, dict] = {}
    for n in names:
        have = in_bag(p, n, iid)
        rows = [r for r in depot(p, n, public) if r.get("itemId") == iid and r.get("uniqueIndex")]
        plan[n] = {"bag": have, "depot": sum(int(r.get("overlay") or 1) for r in rows), "need": max(0, count - have)}
        p.log(f"帶道具：{n} {name_of} 背包 {have}、{where} {plan[n]['depot']}，要取 {plan[n]['need']}")
    if not getattr(args, "apply", False):
        return f"只列（沒加 --apply，沒有送任何動作）：{name_of}", {"plan": plan}
    if public:                                           # 共倉大家共用：總量要夠全部人取
        total = plan[names[0]]["depot"] if names else 0
        if total < sum(v["need"] for v in plan.values()):
            raise JobError(f"共倉的 {name_of} {total} 個不夠全部要取的 {sum(v['need'] for v in plan.values())} 個，不取（不會買）")
    else:
        short = [n for n in names if plan[n]["depot"] < plan[n]["need"]]
        if short:
            raise JobError(f"{short} 個人倉庫的 {name_of} 不夠，不取（不會買）")
    took: Dict[str, int] = {}
    for n in names:
        took[n] = 0
        while in_bag(p, n, iid) < count:
            if BAG_SLOTS - len(p.bag(n)) < 2:
                raise JobError(f"{n} 背包只剩 {BAG_SLOTS - len(p.bag(n))} 格（要留 1 格），停（已取 {took}）")
            row = next((r for r in depot(p, n, public) if r.get("itemId") == iid and r.get("uniqueIndex")), None)
            if row is None:
                raise JobError(f"{n} {where}找不到 {name_of}（已取 {took}）")
            before = in_bag(p, n, iid)
            g = p.clients[n].request("ItemHandle_GetItemToNewDepot",
                                     {"page": 1, "isPublic": public, "uniqueIndex": row["uniqueIndex"], "count": 1}) or {}
            if not (g.get("isOK") and p.wait(lambda: in_bag(p, n, iid) > before, 5)):
                raise JobError(f"{n} 取出 {name_of} 沒確認：{g}（已取 {took}）")
            took[n] += 1
            p.nap(0.4)
        p.log(f"帶道具：{n} {name_of} 背包 {in_bag(p, n, iid)} 個（取出 {took[n]}）")
    return f"帶道具：{name_of} 每隻 {count} 個，取出 {took}", {"took": took}
