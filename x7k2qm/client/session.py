"""一次完成整套登入：HTTP 帳號登入 → 連遊戲伺服器 → 握手 #1~#4 → Role_RoleLogin（進入遊戲）。

注意：同一個角色同時只能在一個地方登入；用無畫面客戶端登入會把正在玩的遊戲客戶端擠下線。
"""

import sys
from typing import Callable, Optional, Tuple

from . import http_login
from .config import ClientConfig
from .connection import GameClient


def open_client(cfg: ClientConfig, use_http: bool = True,
                log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
                client: Optional[GameClient] = None) -> Tuple[GameClient, Optional[http_login.LoginResult]]:
    pc_ip, country = cfg.pc_ip, cfg.country
    if not pc_ip or not country:
        geo = http_login.geo_lookup()
        pc_ip, country = pc_ip or geo["pcIp"], country or geo["country"]

    result = None
    game_host, rid = cfg.game_host, cfg.rid
    if use_http:
        result = http_login.login(cfg.login_host, cfg.account, cfg.password, cfg.pc_id, cfg.client_ver,
                                  pc_ip, country, cfg.language)
        game_host = game_host or result.game_ip
        roles = result.roles(cfg.node)
        if rid is None:
            if not roles:
                raise RuntimeError(f"節點 {cfg.node} 沒有角色")
            rid = roles[0]["rid"]
        log(f"HTTP 登入成功：伺服器 {len(result.servers())} 個、此節點角色 {len(roles)} 個")
    if not game_host or rid is None:
        raise RuntimeError("不走 HTTP 登入時，設定檔必須有 game_host 與 rid")

    c = client or GameClient()
    c.connect(game_host, cfg.game_port, cfg.account, cfg.password, cfg.node, cfg.pc_id, rid)
    log("登入握手完成（已取得本連線金鑰）")
    c.enter_game(cfg.pc_id, cfg.client_ver, pc_ip, country)
    log("已進入遊戲（Role_RoleLogin isOK）")
    return c, result
