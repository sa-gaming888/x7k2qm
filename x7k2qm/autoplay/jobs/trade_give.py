"""trade-give：兩邊各自的程式交易，給方這一半（收方是 trade-take，在另一個程式／另一台機器）。

    # 只列（不送動作）：東西在不在、能不能交易
    python -X utf8 -m x7k2qm.autoplay.jobs add trade-give --profile 7 --discover --giver 甲 --taker 乙 --taker-rid 123 --pet 1000000143366 --item 1501=3
    # 真的給（收方回報就緒之後才排）
    python -X utf8 -m x7k2qm.autoplay.jobs add trade-give --profile 7 --discover --apply --giver 甲 --taker 乙 --taker-rid 123 --pet 1000000143366

使用者 11-14「讓兩邊不同程式自己交易」「例如讓 twidc 帳號，跟其他帳號交易」：原本 `jobs trade` 是同一個行程從同一個 IP 登兩組，
收方有程式在跑就不能交易。流程（使用者 11-14 定）：AI 通知兩邊 session → 各自停戰鬥、確認東西都在 → 照現有交易規定交易。
這裡只做給方：排進自己常駐（練功、練寵）的佇列，用它的連線；一個視窗做完（最多 7 格），不分批。
協定與停止條件跟 `jobs trade` 同一套（`trade.give_half`）；說明見 doc/autoplay/flows/jobs.md「trade-give／trade-take」。
"""

from typing import List, Tuple

from . import JobError
from . import trade as T
from .buy import item_name

NAME = "trade-give"
HELP = "兩邊各自的程式交易：給方（收方先排 trade-take 回報就緒後才排；不加 --apply 只列）"
SOLO = True                                    # 沒有常駐時只登給的那一隻（--role）
PAUSE = lambda a: bool(getattr(a, "apply", False))    # noqa: E731
HOSTED_OK = True                               # 練寵工作（petgoal）的佇列也收


def add_args(ap) -> None:
    ap.add_argument("--giver", required=True, help="給的角色名字（這組的）")
    ap.add_argument("--role", type=int, default=None, help="沒有常駐時登這組第幾隻人物（1 起算），要跟 --giver 同一隻")
    ap.add_argument("--taker", required=True, help="收的角色名字（另一個帳號）")
    ap.add_argument("--taker-rid", dest="taker_rid", type=int, default=None, help="收的角色 rid（--apply 一定要給；收方日誌「收方就緒」那行有）")
    ap.add_argument("--item", action="append", default=[], help="道具 ID 或完整名稱[=數量]，可多次")
    ap.add_argument("--pet", action="append", type=int, default=[], help="寵物 uniqueIndex，可多次")
    ap.add_argument("--order", default=None, help="交易單號（只寫進日誌，方便兩邊對）")
    ap.add_argument("--half-wait", dest="half_wait", type=float, default=T.HALF_WAIT, help="等收方每一步最多幾秒")
    ap.add_argument("--group4-ok", dest="group4_ok", default=None, help="使用者當下明講要動第四組時填他的原話")


def check(args) -> None:
    T.check_profile(args)
    items, pets = T.parse_items(args.item or []), list(args.pet or [])
    if not items and not pets:
        raise JobError("要給 --item 或 --pet")
    if len(set(pets)) != len(pets) or len(pets) > T.MAX_PETS:
        raise JobError(f"--pet 不能重複、最多 {T.MAX_PETS} 隻：{pets}")
    if len(pets) + len(items) > T.MAX_WINDOW:
        raise JobError(f"一次最多 {T.MAX_WINDOW} 格（道具每種至少 1 格＋寵物每隻 1 格），這張單要分開排")
    if getattr(args, "apply", False) and not args.taker_rid:
        raise JobError("--apply 要給 --taker-rid（收方「收方就緒」那行有）")


def run(p, args) -> Tuple[str, dict]:
    if args.giver not in p.clients:
        raise JobError(f"{args.giver} 不在這次登入的人物裡（{sorted(p.clients)}）")
    order = f"交易單 {args.order}：" if args.order else ""
    want, uniques = T.parse_items(args.item or []), list(args.pet or [])
    g = T.Side(p, args.giver)
    info, pet_depot, problems = T.plan(p, g, want, uniques)
    if not getattr(args, "apply", False):
        return (f"{order}只列（沒加 --apply，沒有送任何動作）："
                f"{'不能交易：' + '；'.join(problems) if problems else '要交易的都有、沒綁定'}"
                f"｜{g.name} rid {g.rid}", {"problems": problems, "rid": g.rid})
    if problems:
        raise JobError(f"{order}不能交易：" + "；".join(problems))
    taken: List[str] = []
    try:
        pets = T.prepare_pets(g, uniques, pet_depot, taken)
        slots, traded = T.prepare(g, dict(want), info, T.MAX_WINDOW - len(pets), taken)
        if traded != want:
            raise JobError(f"{g.name} 一個視窗放不下全部（{traded} ≠ {want}），這張單要分開排")
        p.log(f"交易：{order}{g.name} → {args.taker}（rid {args.taker_rid}）："
              f"{[(item_name(i), n) for i, n in traded.items()]}、寵物 {uniques}")
        T.give_half(g, args.taker_rid, args.taker, slots, pets, wait=args.half_wait)
    except Exception:  # noqa: BLE001
        if taken:
            p.log(f"交易：已從倉庫拿到 {g.name} 身上、沒交易出去：{taken}")
        raise
    return (f"{order}交易完成（給方）：{g.name} → {args.taker}：{T.describe(traded, len(uniques))}",
            {"items": traded, "pets": uniques, "order": args.order})
