"""建角色（無畫面）：照遊戲 `AccountNetManager.SendCreateRole` 送 HTTP GET `CreateRole`（doc/autoplay/game/character.md §1.4）。

    python -X utf8 -m x7k2qm.client.create_role --profile 3 --count 5 --water 10 --speed 20
    python -X utf8 -m x7k2qm.client.create_role --profile 7 --fire 10 --speed 20 --names 角色98,敖睿昕,敖璿鳳,敖睿芯,敖睿翎

- 帳密只從環境變數讀（BOT_ACCOUNT_<組> / BOT_PASSWORD_<組>）；網址含混淆後的帳密，**不寫日誌、不放進錯誤訊息**。
- 四屬性共 10 點（協定每點 ×10）；地火互斥、水風互斥。基礎屬性（體力、腕力、耐力、速度）共 20 點，都要分完。
- 外觀：imageId = 100000 + (款式 − 1) × 20 + 顏色 × 5（款式 1～12、顏色 0～3；現有角色封包核對過 6 組）。
- 名字：照遊戲骰子 `StringUtil.RandomName`（Config.nameFirstTw + nameLastTw）。
- 回應 `CreateRoleByGet|<result>|<rid>|...`：1 成功、3／4 名字問題（換名重試）、5 角色數已滿。
- 建角色只是 HTTP，不會讓角色上線（不影響同機在線上限）。一次一隻、間隔 ≥ 1 秒；其他錯誤就停。
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Tuple

from .config import load
from .crypto import obfuscate
from .http_login import HEADERS

ELEMENTS = ("earth", "water", "fire", "wind")
BASES = ("vitality", "strength", "stamina", "speed")


class CreateRoleError(RuntimeError):
    pass


def image_id(head: int, color: int = 0) -> int:
    if not (1 <= head <= 12 and 0 <= color <= 3):
        raise ValueError("款式 1～12、顏色 0～3")
    return 100000 + (head - 1) * 20 + color * 5


def check_points(elem: dict, base: dict) -> None:
    if sum(elem.values()) != 10 or any(v < 0 for v in elem.values()):
        raise ValueError(f"四屬性要剛好 10 點：{elem}")
    if (elem["earth"] and elem["fire"]) or (elem["water"] and elem["wind"]):
        raise ValueError("地火互斥、水風互斥")
    if sum(base.values()) != 20 or any(v < 0 for v in base.values()):
        raise ValueError(f"基礎屬性要剛好 20 點：{base}")


def build_url(host: str, account: str, password: str, node: str, name: str, head: int, color: int,
              elem: dict, base: dict, language: int = 40) -> str:
    check_points(elem, base)
    params = [("serverNode", "account1"), ("name", name), ("imageId", str(image_id(head, color))), ("headId", str(head))]
    params += [(k, str(elem[k] * 10)) for k in ELEMENTS]                 # 協定裡每點是 10
    params += [(k, str(base[k])) for k in BASES]
    params += [("cdkey", obfuscate(account)), ("password", obfuscate(password)), ("gameNode", node),
               ("language", str(language))]
    return f"http://{host}/CreateRole?" + urllib.parse.urlencode(params) + "&"


def parse_response(body: str) -> Tuple[int, int]:
    parts = body.split("|")
    if len(parts) < 3 or parts[0] != "CreateRoleByGet":
        raise CreateRoleError(f"建角回應格式不符：{body[:30]!r}")
    return int(parts[1]), int(parts[2] or 0)


def random_name(rng: random.Random, configs: Optional[str] = None) -> str:
    from ..features.gamedb.tables import CONFIGS
    with open(os.path.join(configs or CONFIGS, "Config.json"), encoding="utf-8") as fp:
        c = json.load(fp)
    c = c[0] if isinstance(c, list) else c
    return rng.choice(c["nameFirstTw"]) + rng.choice(c["nameLastTw"])


def create_many(count: int, elem: dict, base: dict, send: Callable[[str, int], Tuple[int, int]],
                name_fn: Callable[[], str], log: Callable[[str], None], sleep: Callable[[float], None],
                heads: Optional[List[int]] = None, tries: int = 3) -> List[Tuple[str, int]]:
    """send(name, head) → (result, rid)。名字重複／非法（3、4）換名重試；5 角色數已滿、其他結果就停。"""
    check_points(elem, base)
    made: List[Tuple[str, int]] = []
    heads = heads or [1, 2, 3, 4, 5]
    for i in range(count):
        head = heads[i % len(heads)]
        for attempt in range(tries):
            name = name_fn()
            result, rid = send(name, head)
            sleep(1.2)
            if result == 1:
                made.append((name, rid))
                log(f"建立角色 {name}（rid {rid}、款式 {head}）")
                break
            if result in (3, 4):
                log(f"名字 {name} 不能用（{result}），換一個")
                continue
            if result == 5:
                log("角色數已滿（5），停止")
                return made
            raise CreateRoleError(f"建角失敗 result={result}")
        else:
            raise CreateRoleError(f"連續 {tries} 個名字都不能用")
    return made


MALE, FEMALE = range(1, 7), range(7, 13)        # 款式 1～6 男、7～12 女（語言表 10000117～10000128 的角色類型名）


def parse_heads(text: str) -> Optional[List[int]]:
    """`--heads`：逗號分隔的款式；「女」＝7～11、「男」＝1～5。空字串回 None（用預設）。"""
    text = (text or "").strip()
    if not text:
        return None
    if text in ("女", "female"):
        return list(FEMALE)[:5]
    if text in ("男", "male"):
        return list(MALE)[:5]
    try:
        heads = [int(x) for x in text.replace("，", ",").split(",") if x.strip()]
    except ValueError:
        raise CreateRoleError(f"--heads 要是數字或「男」「女」：{text}") from None
    bad = [h for h in heads if not 1 <= h <= 12]
    if not heads or bad:
        raise CreateRoleError(f"--heads 款式要在 1～12：{text}")
    return heads


def create_named(names: List[str], elem: dict, base: dict, send: Callable[[str, int], Tuple[int, int]],
                 log: Callable[[str], None], sleep: Callable[[float], None],
                 heads: Optional[List[int]] = None) -> List[Tuple[str, int]]:
    """照指定名字依序建；名字不能用（3、4）、角色已滿（5）或其他結果都停（不自己換名，規範 2）。"""
    check_points(elem, base)
    if len(names) != len(set(names)):
        raise CreateRoleError(f"名字重複：{names}")
    heads = heads or [1, 2, 3, 4, 5]
    made: List[Tuple[str, int]] = []
    for i, name in enumerate(names):
        if i:
            sleep(1.2)
        head = heads[i % len(heads)]
        result, rid = send(name, head)
        if result != 1:
            why = {3: "名字重複或含非法字元", 4: "名字已被使用", 5: "角色數已滿"}.get(result, "建角失敗")
            raise CreateRoleError(f"{name}：{why}（result={result}）；已建 {made}，後面 {names[i + 1:]} 沒建")
        made.append((name, rid))
        log(f"建立角色 {name}（rid {rid}、款式 {head}）")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.client.create_role")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--heads", default="", help="外觀款式，逗號分隔、照順序輪流（1～6 男：豆丁、賽亞人、辮子男孩、酷哥、熊皮男、大個；"
                                                "7～12 女：豆丁妹、熊皮妹、帽子妹、短髮夾妹、手套女、辣妹；語言表 10000117～10000128）；預設 1,2,3,4,5")
    ap.add_argument("--names", default="", help="指定名字，逗號分隔、照順序（使用者 11-02）；給了就照這些建，名字不能用就停、不換隨機名")
    for k, zh in zip(ELEMENTS, "地水火風"):
        ap.add_argument(f"--{k}", type=int, default=0, help=f"{zh}屬性點（四屬性共 10）")
    for k, zh in zip(BASES, ("體力", "腕力", "耐力", "速度")):
        ap.add_argument(f"--{k}", type=int, default=0, help=f"{zh}（基礎共 20）")
    args = ap.parse_args(argv)
    elem = {k: getattr(args, k) for k in ELEMENTS}
    base = {k: getattr(args, k) for k in BASES}
    cfg = load(profile=args.profile)
    rng = random.Random()

    def send(name: str, head: int) -> Tuple[int, int]:
        url = build_url(cfg.login_host, cfg.account, cfg.password, cfg.node, name, head, 0, elem, base)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
                return parse_response(r.read().decode("utf-8"))
        except CreateRoleError:
            raise
        except Exception as e:  # noqa: BLE001  （不把網址放進錯誤訊息：網址含帳密）
            raise CreateRoleError(f"建角請求失敗：{type(e).__name__}") from None
    names = [n.strip() for n in args.names.replace("，", ",").replace("、", ",").split(",") if n.strip()]
    try:
        heads = parse_heads(args.heads)
        if names:
            made = create_named(names, elem, base, send, print, time.sleep, heads=heads)
        else:
            made = create_many(args.count, elem, base, send, lambda: random_name(rng), print, time.sleep, heads=heads)
    except CreateRoleError as e:
        print(f"停止：{e}")
        return 1
    print(f"完成：建立 {len(made)} 隻 {[n for n, _ in made]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
