"""無畫面客戶端的設定：**密碼只從環境變數讀**，不寫進任何檔案（使用者 2026-10-21 決定）。

帳號清單 `config/accounts.json`（使用者 2026-11-02：帳號越來越多、不想一直設環境變數，參考另一個版本
`data/profile/accounts.json`；帳號名稱可以進檔、可以 commit）：
  {"<組>": {"account": "帳號", "password_env": "BOT_PASSWORD_4", ...}, "market": {...}}
  - 檔裡有這組的 account → 帳號照檔、密碼照 password_env 讀環境變數（同密碼的組共用一個變數）。
  - 檔裡沒有 → 照舊讀 BOT_ACCOUNT_<組>／BOT_PASSWORD_<組>。
  - "market"＝不帶 profile 的交易所常駐。路徑可用 BOT_ACCOUNTS_FILE 改（測試指到暫存檔）。

舊做法（檔裡沒有那組時）必填：
  BOT_ACCOUNT     帳號
  BOT_PASSWORD    密碼
選填（不填用預設）：
  BOT_NODE        遊戲伺服器節點（預設 game1）
  BOT_RID         要登入的角色 rid（預設 HTTP 回應中該節點的第一個角色）
  BOT_PC_ID       機器識別（預設照遊戲格式自己產生，見 make_pc_id）
  BOT_LOGIN_HOST  HTTP 登入伺服器（預設 eologin.idealsa.org）
  BOT_GAME_HOST   遊戲伺服器（預設用 HTTP 回應的 IP）
  BOT_CLIENT_VER  客戶端版本（沒設就從本機遊戲檔自動抓，見 game_version.py；都沒有用 1.0.3）
  BOT_PC_IP / BOT_COUNTRY   對外 IP 與國家（預設像遊戲一樣問 ip-api.com）

第二組帳號（自動玩用）：同樣的名稱加後綴 _2，例如 BOT_ACCOUNT_2、BOT_PASSWORD_2、BOT_RID_2（load(profile="2")）。

PowerShell 設定範例（只在目前視窗有效）：
  $env:BOT_ACCOUNT = "你的帳號"; $env:BOT_PASSWORD = "你的密碼"
"""

import hashlib
import json
import os
import random
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional

from .game_version import FALLBACK, local_client_ver
from .http_login import LOGIN_HOST

PREFIX = "BOT_"


class ConfigError(RuntimeError):
    pass


@dataclass
class ClientConfig:
    account: str
    password: str
    pc_id: str
    node: str = "game1"
    rid: Optional[int] = None
    login_host: str = LOGIN_HOST
    game_host: Optional[str] = None
    game_port: int = 11000
    client_ver: str = "1.0.4"
    language: int = 40
    pc_ip: Optional[str] = None
    country: Optional[str] = None

    def __repr__(self) -> str:  # 不把帳密印出來
        return f"ClientConfig(node={self.node!r}, rid={'有' if self.rid else '無'}, login_host={self.login_host!r})"


def make_pc_id(pid: Optional[int] = None, rng: Optional[random.Random] = None) -> str:
    """照遊戲 Preload.setPcId 的格式產生機器識別：
       <abs(機器資訊雜湊)>_ranoc1 + <deviceUniqueIdentifier（40 碼 hex）> + _PCWIN@@ + <行程 ID>
    遊戲的值本身就含行程 ID、每次開遊戲都不同，所以不需要沿用遊戲的值。
    deviceUniqueIdentifier 用本機網卡位址的 SHA1（同一台電腦固定）。"""
    rng = rng or random.Random()
    device = hashlib.sha1(uuid.getnode().to_bytes(6, "big")).hexdigest()
    return f"{rng.randrange(10**8, 2**31)}_ranoc1{device}_PCWIN@@{pid if pid is not None else os.getpid()}"


def env_name(name: str, profile: str = "") -> str:
    """環境變數名稱。profile 是第幾組帳號：""＝BOT_ACCOUNT（交易所常駐用），"2"＝BOT_ACCOUNT_2（自動玩用）。"""
    return PREFIX + name + (f"_{profile}" if profile else "")


ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "config", "accounts.json")
MARKET = "market"                       # accounts.json 裡交易所常駐（profile ""）那組的鍵


def accounts_path() -> str:
    return os.environ.get("BOT_ACCOUNTS_FILE") or ACCOUNTS_FILE


def read_accounts(path: Optional[str] = None) -> dict:
    """整份帳號清單；沒有檔回 {}。檔案壞掉要報錯（不能默默改用別組的環境變數）。"""
    path = path or accounts_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except ValueError as e:
        raise ConfigError(f"{path} 不是正確的 JSON：{e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 最外層要是物件（組別 → 設定）")
    return data


def account_entry(profile: str = "", path: Optional[str] = None) -> Optional[dict]:
    """accounts.json 裡這組的設定（有寫 account 才算）；沒有回 None。"""
    entry = read_accounts(path).get(profile or MARKET)
    return entry if isinstance(entry, dict) and entry.get("account") else None


def load(environ: Mapping[str, str] = os.environ, profile: str = "", accounts: Optional[str] = None) -> ClientConfig:
    def get(name: str) -> Optional[str]:
        v = environ.get(env_name(name, profile))
        return v if v else None

    entry = account_entry(profile, accounts)
    if entry:
        pw_env = entry.get("password_env") or env_name("PASSWORD", profile)
        account, password = str(entry["account"]), environ.get(pw_env) or None
        if not password:
            raise ConfigError(f"缺少環境變數 {pw_env}（第 {profile or MARKET} 組的密碼；帳號在 config/accounts.json）")
    else:
        missing = [env_name(n, profile) for n in ("ACCOUNT", "PASSWORD") if not get(n)]
        if missing:
            raise ConfigError(f"缺少環境變數 {', '.join(missing)}（config/accounts.json 沒有這組；見 x7k2qm/client/config.py）")
        account, password = get("ACCOUNT"), get("PASSWORD")
    rid = get("RID")
    if rid is not None and not rid.isdigit():
        raise ConfigError(f"{env_name('RID', profile)} 必須是數字")
    return ClientConfig(
        account=account, password=password, pc_id=get("PC_ID") or make_pc_id(),
        node=get("NODE") or "game1", rid=int(rid) if rid else None,
        login_host=get("LOGIN_HOST") or LOGIN_HOST, game_host=get("GAME_HOST"),
        client_ver=get("CLIENT_VER") or local_client_ver() or FALLBACK, pc_ip=get("PC_IP"), country=get("COUNTRY"))
