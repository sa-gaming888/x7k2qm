"""HTTP 帳號登入（AccountNetManager.SendAccountLogin → NetDataManager.loginAccounthttpGet）。

  GET http://<登入伺服器>/LoginAcccountByGet?serverNode=account1&pcId=..&cdKey=Obfuscate(帳號)
      &passWord=Obfuscate(密碼)&clientVer=..&isVisitorLogin=0&language=40&key=adsl888&pcIp=..&country=..&
  回應：lac@@<遊戲伺服器 IP>|{"result":1,"uid":..,"areaList":[{"areaName":..,"serverList":[
         {"gameNode":"game1","isMaintain":false,"roleList":[{"rid":..,"name":..,"level":..}, ...]}]}]}

遊戲客戶端先打 http://ip-api.com/json/ 取得自己的對外 IP 與國家填進 pcIp / country。
"""

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .crypto import obfuscate

HTTP_KEY = "adsl888"
GEO_URL = "http://ip-api.com/json/"
LOGIN_HOST = "eologin.idealsa.org"  # 2026-10-21 實測（解析到 伺服器位址）
UNITY_VERSION = "2022.3.62f3c1"
HEADERS = {
    "User-Agent": f"UnityPlayer/{UNITY_VERSION} (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
    "Accept": "*/*",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "X-Unity-Version": UNITY_VERSION,
}


@dataclass
class LoginResult:
    game_ip: str
    uid: int
    areas: List[Dict[str, Any]] = field(default_factory=list)

    def servers(self) -> List[Dict[str, Any]]:
        return [s for a in self.areas for s in a.get("serverList", [])]

    def roles(self, node: Optional[str] = None) -> List[Dict[str, Any]]:
        return [r for s in self.servers() if node in (None, s.get("gameNode"))
                for r in s.get("roleList", []) or []]


class HttpLoginError(RuntimeError):
    pass


def build_url(host: str, account: str, password: str, pc_id: str, client_ver: str,
              pc_ip: str, country: str, language: int = 40) -> str:
    params = [("serverNode", "account1"), ("pcId", pc_id), ("cdKey", obfuscate(account)),
              ("passWord", obfuscate(password)), ("clientVer", client_ver), ("isVisitorLogin", "0"),
              ("language", str(language)), ("key", HTTP_KEY), ("pcIp", pc_ip), ("country", country)]
    return f"http://{host}/LoginAcccountByGet?" + urllib.parse.urlencode(params) + "&"


def parse_response(body: str) -> LoginResult:
    head, sep, js = body.partition("|")
    if not sep or not head.startswith("lac@@"):
        raise HttpLoginError(f"登入回應格式不符：{body[:30]!r}")
    data = json.loads(js)
    if data.get("result") != 1:
        # 把伺服器回的其他欄位一起帶出來：舊版只印 result，2026-10-23 伺服器維護後
        # 三個帳號都回 result=4（官方客戶端也一樣），但我們看不到有沒有附訊息或新版本號。
        extra = {k: v for k, v in data.items() if k != "result"}
        raise HttpLoginError(f"登入失敗 result={data.get('result')}"
                             + (f"，伺服器還回了 {extra}" if extra else "（回應只有 result）"))
    return LoginResult(game_ip=head[len("lac@@"):], uid=int(data["uid"]), areas=data.get("areaList", []))


def geo_lookup(timeout: float = 10.0) -> Dict[str, str]:
    with urllib.request.urlopen(urllib.request.Request(GEO_URL, headers=HEADERS), timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return {"pcIp": d.get("query", ""), "country": d.get("country", "")}


def login(host: str, account: str, password: str, pc_id: str, client_ver: str,
          pc_ip: str, country: str, language: int = 40, timeout: float = 15.0) -> LoginResult:
    url = build_url(host, account, password, pc_id, client_ver, pc_ip, country, language)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_response(r.read().decode("utf-8"))
