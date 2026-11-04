"""註冊遊戲帳號（無畫面）：照遊戲 `AccountNetManager.SendRegAccount` 送 HTTP GET `RegAccountByGet`（使用者 2026-11-02 要批量註冊）。

    python -X utf8 -m x7k2qm.client.reg_account --account abc123 --phone 0912345678 --password-env BOT_PASSWORD_7
    python -X utf8 -m x7k2qm.client.reg_account --batch 清單.txt          # 每行：帳號,電話,密碼環境變數名稱
    ... --profile 7 --label 新帳號      # 成功後加進 config/accounts.json（只寫帳號與 password_env）

協定（反編譯 HotUpdate.dll：`NewUIRegisterScreen.OnClickCdkeyPasswordRegOk`、`AccountNetManager.SendRegAccount`、
`NetDataManager.regAccounthttpGet`）：
  http://<帳號伺服器>/RegAccountByGet?serverNode=account1&pcId=…&cdKey=Obfuscate(帳號)&passWord=Obfuscate(密碼)&phone=…&icode=…&
  回應 `RegAccountByGet|{"result":N}`：1 成功、2 失敗、3 帳密錯誤、4 手機號碼錯誤、5 機器碼錯誤、6 帳號已註冊、7 系統錯誤、8 邀請碼不存在。
- 帳號、密碼 6～12 字元（客戶端檢查；畫面寫「僅限數字英文」）；電話必填、至少 6 碼，只是改密碼／找回用，不發簡訊。
- 畫面上的 5 碼驗證碼是客戶端自己產生自己比對，**不送伺服器**。
- 密碼只從環境變數讀（規範 8）；網址含混淆後的帳密，**不寫日誌、不放進錯誤訊息**。
- 一次一個、間隔 ≥ 1.2 秒；6（已註冊）略過，其他失敗整批停（規範 2）。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, Iterable, List, Mapping, Optional, Tuple

from .config import accounts_path, make_pc_id
from .crypto import obfuscate
from .http_login import HEADERS, LOGIN_HOST

RESULTS = {1: "註冊成功", 2: "註冊失敗", 3: "帳號密碼錯誤", 4: "手機號碼錯誤", 5: "機器碼錯誤",
           6: "帳號已註冊", 7: "系統錯誤", 8: "邀請碼不存在"}
OK, EXISTS = 1, 6
GAP = 1.2
_NAME = re.compile(r"^[A-Za-z0-9]{6,12}$")


class RegError(RuntimeError):
    pass


def check(account: str, password: str, phone: str) -> None:
    """客戶端註冊畫面的檢查（不合格伺服器那邊也不會過，先擋）。錯誤訊息不含密碼。"""
    if not _NAME.match(account or ""):
        raise RegError(f"帳號 {account!r} 要 6～12 個英文或數字")
    if not _NAME.match(password or ""):
        raise RegError(f"帳號 {account} 的密碼要 6～12 個英文或數字")
    if not re.fullmatch(r"\d{6,}", phone or ""):
        raise RegError(f"帳號 {account} 的電話要至少 6 個數字")


def build_url(host: str, account: str, password: str, phone: str, pc_id: str, icode: str = "") -> str:
    check(account, password, phone)
    params = [("serverNode", "account1"), ("pcId", pc_id), ("cdKey", obfuscate(account)),
              ("passWord", obfuscate(password)), ("phone", phone), ("icode", icode)]
    return f"http://{host}/RegAccountByGet?" + urllib.parse.urlencode(params) + "&"


def parse_response(body: str) -> int:
    head, sep, rest = body.partition("|")
    if head != "RegAccountByGet" or not sep:
        raise RegError(f"註冊回應格式不符：{body[:30]!r}")
    try:
        return int(json.loads(rest.split("|")[0])["result"])
    except (ValueError, KeyError, TypeError):
        raise RegError(f"註冊回應格式不符：{body[:40]!r}") from None


def read_batch(lines: Iterable[str]) -> List[Tuple[str, str, str]]:
    """每行 `帳號,電話,密碼環境變數名稱`；空行與 # 開頭略過。清單檔不放密碼本身。"""
    out = []
    for n, line in enumerate(lines, 1):
        line = line.strip().replace("，", ",")
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3 or not all(parts):
            raise RegError(f"第 {n} 行要是「帳號,電話,密碼環境變數名稱」")
        out.append((parts[0], parts[1], parts[2]))
    if not out:
        raise RegError("清單是空的")
    return out


def resolve(items: List[Tuple[str, str, str]], environ: Mapping[str, str]) -> List[Tuple[str, str, str, str]]:
    """(帳號, 電話, 密碼變數) → (帳號, 密碼, 電話, 密碼變數)；全部先檢查，有一個不對就整批不送。"""
    out, seen = [], set()
    for account, phone, pw_env in items:
        password = environ.get(pw_env) or ""
        if not password:
            raise RegError(f"帳號 {account} 的密碼環境變數 {pw_env} 沒設")
        check(account, password, phone)
        if account.lower() in seen:
            raise RegError(f"帳號 {account} 重複")
        seen.add(account.lower())
        out.append((account, password, phone, pw_env))
    return out


def register_many(items: List[Tuple[str, str, str, str]], send: Callable[[str, str, str], int],
                  log: Callable[[str], None], sleep: Callable[[float], None]) -> List[Tuple[str, int]]:
    """逐一註冊。回傳 [(帳號, result)]，只含 1 和 6；其他結果丟 RegError（後面不送）。"""
    done: List[Tuple[str, int]] = []
    for i, (account, password, phone, _) in enumerate(items):
        if i:
            sleep(GAP)
        try:
            result = send(account, password, phone)
        except RegError as err:
            err.done = done
            raise
        if result == OK:
            log(f"{account}：註冊成功")
        elif result == EXISTS:
            log(f"{account}：帳號已註冊，略過")
        else:
            err = RegError(f"{account}：{RESULTS.get(result, '未知結果')}（result={result}）；"
                           f"後面 {[a for a, *_ in items[i + 1:]]} 不送；已完成 {done}")
            err.done = done
            raise err
        done.append((account, result))
    return done


def add_accounts(path: str, entries: List[Tuple[str, str, str, str]]) -> List[str]:
    """把 (組, 帳號, 密碼變數, 標籤) 加進帳號清單；組已存在就不覆蓋（回傳略過的組）。"""
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    skipped = []
    for profile, account, pw_env, label in entries:
        if profile in data:
            skipped.append(profile)
            continue
        data[profile] = {"label": label, "account": account, "password_env": pw_env}
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
        fp.write("\n")
    return skipped


def main(argv=None, environ: Mapping[str, str] = os.environ) -> int:
    ap = argparse.ArgumentParser(prog="x7k2qm.client.reg_account", description="註冊遊戲帳號（密碼只從環境變數讀）")
    ap.add_argument("--account")
    ap.add_argument("--phone")
    ap.add_argument("--password-env", help="密碼所在的環境變數名稱")
    ap.add_argument("--batch", help="清單檔：每行 帳號,電話,密碼環境變數名稱")
    ap.add_argument("--icode", default="", help="邀請碼（選填）")
    ap.add_argument("--profile", help="成功後加進 config/accounts.json 的組別（--batch 時從這個數字往上排）")
    ap.add_argument("--label", default="", help="加進帳號清單的標籤")
    ap.add_argument("--host", default=LOGIN_HOST)
    args = ap.parse_args(argv)
    try:
        if args.batch:
            with open(args.batch, encoding="utf-8") as fp:
                raw = read_batch(fp)
        elif args.account and args.phone and args.password_env:
            raw = [(args.account, args.phone, args.password_env)]
        else:
            raise RegError("要給 --account --phone --password-env，或 --batch")
        if args.profile and not args.profile.isdigit():
            raise RegError("--profile 要是數字")
        items = resolve(raw, environ)
    except RegError as e:
        print(f"停止：{e}")
        return 1
    pc_id = make_pc_id()

    def send(account: str, password: str, phone: str) -> int:
        url = build_url(args.host, account, password, phone, pc_id, args.icode)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
                return parse_response(r.read().decode("utf-8"))
        except RegError:
            raise
        except Exception as e:  # noqa: BLE001  （不把網址放進錯誤訊息：網址含帳密）
            raise RegError(f"{account}：註冊請求失敗 {type(e).__name__}") from None

    code, done = 0, []
    try:
        done = register_many(items, send, print, time.sleep)
    except RegError as e:
        print(f"停止：{e}")
        code = 1
        done = list(getattr(e, "done", done))
    if args.profile and done:
        envs = {a: env for a, _, _, env in items}
        start = int(args.profile)
        entries = [(str(start + i), a, envs[a], args.label) for i, (a, _) in enumerate(done)]
        skipped = add_accounts(accounts_path(), entries)
        print(f"加進帳號清單：{[(p, a) for p, a, *_ in entries if p not in skipped]}；組別已存在沒加：{skipped}")
    print(f"結束：{len(done)} 個完成 {done}")
    return code


if __name__ == "__main__":
    sys.exit(main())
