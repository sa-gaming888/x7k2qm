"""隨機註冊 N 組帳號，結果推 Telegram（使用者 2026-11-04：「action 啟動時問要創建幾組，帳密都隨機產生，
帳號用一個單字＋數字、密碼英文＋數字，都不超過 8 位；電話隨機產生合理的台灣手機；結果發到 tg」）。

    python -X utf8 -m x7k2qm.client.reg_random --count 3 --element 地 --host <登入伺服器>

- 每個帳號註冊完接著建五隻角色（使用者 11-04「人物屬性預設地、名字隨機，帳號創建完要再去把五隻人物創建出來」
  「屬性讓我可以在 workflow 選」）：屬性 10 點全加 --element、基礎點數敏 20、款式 1～5、名字照遊戲骰子
  （create_role.random_name）；名字被用了換一個，少於五隻或其他錯誤整批停。`--no-roles` 只註冊。

- 帳號：短英文單字＋數字，6～8 字元（伺服器要 6～12）；密碼：英文＋數字各至少一個，8 字元；電話：09 開頭 10 碼，
  前 4 碼取台灣行動電話實際有在用的字頭。全部用 `secrets`（密碼學亂數）。
- 一次一個、間隔 ≥ 1.2 秒（照 reg_account）；「帳號已註冊」換一個新帳號再試（同一組最多 3 次）；
  其他失敗整批停（規範 2），已經成功的照樣推 TG。
- 帳密只進 Telegram，不印到標準輸出（GitHub Actions 日誌公開）：在 Actions 裡先印 `::add-mask::` 再用。
- Telegram 讀 BOT_TG_TOKEN／BOT_TG_CHAT_ID（Actions 從 secrets 帶）；推不出去結束碼 1，
  並在日誌說「已註冊但沒推到」（帳密不印，只能重設密碼找回）。
"""

import argparse
import random
import os
import secrets
import string
import sys
import time
import urllib.request
from typing import Callable, List, Mapping, Optional, Tuple

from . import create_role as CR
from .config import make_pc_id
from .http_login import HEADERS, LOGIN_HOST
from .reg_account import EXISTS, GAP, OK, RESULTS, RegError, build_url, check, parse_response

MAX_COUNT = 20
ROLES_PER_ACCOUNT = 5                      # 使用者 11-04「帳號創建完，要再去把五隻人物創建出來」
ELEMENT_KEYS = {"地": "earth", "水": "water", "火": "fire", "風": "wind"}
BASE_POINTS = {"vitality": 0, "strength": 0, "stamina": 0, "speed": 20}   # 跟第七、八組一樣敏 20
NODE = "game1"
EXISTS_TRIES = 3
WORDS = ("sun", "moon", "star", "wolf", "bear", "lion", "hawk", "fox", "leaf", "rain", "snow", "wind", "fire", "rock",
         "tide", "jade", "ruby", "mint", "kiwi", "plum", "nova", "echo", "zen", "sky", "blue", "gold", "iron", "oak",
         "pine", "lake", "hill", "cat", "owl", "elk", "ant", "bee", "sea", "ice", "dew", "fig")
# 台灣行動電話字頭（09xx，NCC 核配給各家業者、實際在用的）
MOBILE_PREFIXES = tuple(f"09{n:02d}" for n in (*range(10, 40), *range(52, 59), *range(60, 67), 68, 69,
                                                  *range(70, 80), *range(81, 90), *range(91, 100)))
ALNUM = string.ascii_letters + string.digits


def gen_account(rng=secrets) -> str:
    """單字＋數字，6～8 字元，小寫。"""
    word = rng.choice(WORDS)
    digits = rng.choice(range(max(2, 6 - len(word)), 8 - len(word) + 1))
    return word + "".join(rng.choice(string.digits) for _ in range(digits))


def gen_password(rng=secrets, length: int = 8) -> str:
    """英文＋數字，8 字元，至少各一個。"""
    while True:
        p = "".join(rng.choice(ALNUM) for _ in range(length))
        if any(c.isalpha() for c in p) and any(c.isdigit() for c in p):
            return p


def gen_phone(rng=secrets) -> str:
    return rng.choice(MOBILE_PREFIXES) + "".join(rng.choice(string.digits) for _ in range(6))


def mask(value: str, out=None) -> None:
    """GitHub Actions：之後日誌裡出現這個值都變 ***。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}", file=out or sys.stdout, flush=True)


def element_points(element: str) -> dict:
    if element not in ELEMENT_KEYS:
        raise ValueError(f"屬性要是 地／水／火／風：{element!r}")
    return {k: (10 if k == ELEMENT_KEYS[element] else 0) for k in CR.ELEMENTS}


def make_roles(account: str, password: str, element: str, send_role: Callable[[str, str, str, int], Tuple[int, int]],
               name_fn: Callable[[], str], sleep: Callable[[float], None]) -> List[str]:
    """這個帳號建 5 隻（屬性 element、敏 20、名字隨機；名字被用了換一個）。出錯丟 CR.CreateRoleError。"""
    elem = element_points(element)
    made = CR.create_many(ROLES_PER_ACCOUNT, elem, BASE_POINTS, lambda name, head: send_role(account, password, name, head),
                          name_fn, lambda m: None, sleep)          # 名字不印日誌（只進 TG）
    if len(made) != ROLES_PER_ACCOUNT:
        raise CR.CreateRoleError(f"只建了 {len(made)} 隻（伺服器說角色數已滿）")
    return [n for n, _ in made]


def register_random(count: int, send: Callable[[str, str, str], int], log: Callable[[str], None],
                    sleep: Callable[[float], None] = time.sleep, rng=secrets,
                    on_new: Callable[[str], None] = lambda v: None,
                    roles: Optional[Callable[[str, str], List[str]]] = None) -> Tuple[List[tuple], Optional[str]]:
    """回傳 ([(帳號, 密碼, 電話[, 角色名清單])], 停止原因或 None)。帳密不進 log。
    roles(帳號, 密碼)：註冊成功後建角色（回傳名字）；出錯就停，那組照樣列出（角色欄寫失敗原因）。"""
    done: List[tuple] = []
    sent = 0
    for i in range(count):
        for attempt in range(EXISTS_TRIES):
            acc, pw, phone = gen_account(rng), gen_password(rng), gen_phone(rng)
            for v in (acc, pw, phone):
                on_new(v)
            check(acc, pw, phone)
            if sent:
                sleep(GAP)
            sent += 1
            try:
                result = send(acc, pw, phone)
            except RegError as e:
                return done, f"第 {i + 1} 組：{e}"
            if result == OK:
                log(f"第 {i + 1}/{count} 組：註冊成功")
                if roles is None:
                    done.append((acc, pw, phone))
                    break
                sleep(GAP)
                try:
                    names = roles(acc, pw)
                except (CR.CreateRoleError, ValueError) as e:
                    done.append((acc, pw, phone, f"建角色失敗：{e}"))
                    return done, f"第 {i + 1} 組建角色：{e}"
                for n in names:
                    on_new(n)
                done.append((acc, pw, phone, names))
                log(f"第 {i + 1}/{count} 組：建好 {len(names)} 隻角色")
                break
            if result == EXISTS:
                log(f"第 {i + 1}/{count} 組：隨機帳號剛好有人用了，換一個（第 {attempt + 1} 次）")
                continue
            return done, f"第 {i + 1} 組：{RESULTS.get(result, '未知結果')}（result={result}）"
        else:
            return done, f"第 {i + 1} 組：連續 {EXISTS_TRIES} 個隨機帳號都已被註冊"
    return done, None


def report(done: List[tuple], count: int, why: Optional[str], element: Optional[str] = None, tag: str = "") -> str:
    head = f"🆕 註冊帳號{f'［{tag}］' if tag else ''}：成功 {len(done)}／{count} 組"
    lines = [head + (f"（角色屬性 {element}、敏 20）" if element else "")]
    for n, row in enumerate(done, 1):
        acc, pw, phone = row[:3]
        lines.append(f"{n}. 帳號 {acc}｜密碼 {pw}｜電話 {phone}")
        if len(row) > 3:
            lines.append(f"   角色：{'、'.join(row[3]) if isinstance(row[3], list) else row[3]}")
    if why:
        lines.append(f"⛔ 停止：{why}")
    return "\n".join(lines)


def main(argv=None, environ: Mapping[str, str] = os.environ) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.client.reg_random", description="隨機註冊 N 組帳號，結果推 Telegram")
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--host", default=LOGIN_HOST)
    ap.add_argument("--element", default="地", help="五隻角色的屬性：地／水／火／風（預設地，10 點全加）")
    ap.add_argument("--no-roles", dest="roles", action="store_false", help="只註冊、不建角色")
    ap.add_argument("--tag", default="", help="Telegram 結果標題上的伺服器標記（同一個 TG 群組收好幾個伺服器的帳號時分得出來）")
    args = ap.parse_args(argv)
    if not 1 <= args.count <= MAX_COUNT:
        print(f"停止：--count 要在 1～{MAX_COUNT}")
        return 1
    if args.element not in ELEMENT_KEYS:
        print("停止：--element 要是 地／水／火／風")
        return 1
    if not args.host:
        print("停止：要給 --host（登入伺服器）")
        return 1
    from ..notify.telegram import Telegram, TelegramConfigError
    try:
        tg = Telegram.from_env(environ)                  # 先確認推得出去，才開始註冊（不然帳密會弄丟）
    except TelegramConfigError as e:
        print(f"停止：{e}")
        return 1
    pc_id = make_pc_id()

    def send(account: str, password: str, phone: str) -> int:
        url = build_url(args.host, account, password, phone, pc_id)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
                return parse_response(r.read().decode("utf-8"))
        except RegError:
            raise
        except Exception as e:  # noqa: BLE001  （不把網址放進錯誤訊息：網址含帳密）
            raise RegError(f"註冊請求失敗 {type(e).__name__}") from None

    elem = element_points(args.element)
    name_rng = random.Random(secrets.randbits(64))

    def send_role(account: str, password: str, name: str, head: int) -> Tuple[int, int]:
        mask(name)
        url = CR.build_url(args.host, account, password, NODE, name, head, 0, elem, BASE_POINTS)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
                return CR.parse_response(r.read().decode("utf-8"))
        except CR.CreateRoleError:
            raise
        except Exception as e:  # noqa: BLE001  （網址含帳密，不放進錯誤訊息）
            raise CR.CreateRoleError(f"建角請求失敗 {type(e).__name__}") from None

    roles = None
    if args.roles:
        roles = lambda acc, pw: make_roles(acc, pw, args.element, send_role,  # noqa: E731
                                            lambda: CR.random_name(name_rng), time.sleep)
    done, why = register_random(args.count, send, print, on_new=mask, roles=roles)
    print(f"結束：成功 {len(done)}／{args.count} 組" + (f"；停止：{why}" if why else ""))
    if done or why:
        err = tg.send(report(done, args.count, why, args.element if args.roles else None, args.tag), silent=False)
        if err:
            print(f"⚠️ TG 推送失敗（{err}）：已註冊 {len(done)} 組但帳密沒推到（日誌不印帳密）")
            return 1
        print("結果已推到 Telegram")
    return 0 if why is None else 1


if __name__ == "__main__":
    sys.exit(main())
