"""GameClient：無畫面連上遊戲伺服器，完成登入握手後直接送協定請求。

  c = GameClient()
  c.connect(host, 11000, account, password, node, pc_id, rid)   # 握手 #1~#4，拿到本連線 DES 金鑰
  c.enter_game(pc_id, client_ver, pc_ip, country)               # Role_RoleLogin，之後自動心跳
  data = c.request("Other_GetNewShopBoothGoodType", {...})      # 等到對應 response 才回傳
  c.close()

送出格式（照遊戲客戶端；doc/REVERSE-ENGINEERING.md §10.4）：
  內層 Package{type, session=n} + payload → 0-pack → GateMessage{content[{networkMessage}],
  [head{rid, notMain=false}]}，外層 session=n+1 → 0-pack → DES → [2B 長度][密文][4B 序號]
  - 外層 head **依協定**：官方 `SocketDriver.SprotoBufferSend(msg, _rid)` 只有 rid > 0 才帶 head，
    各協定傳不傳 rid 見 GATE_HEAD／GATE_NO_HEAD 兩張表（不在表裡的照舊帶，並由測試要求程式送的協定都要列）。
  - 內層 session：協定有 response 才帶；NO_INNER_SESSION 例外（官方不帶，伺服器也不回 response）；
    INNER_SESSION_NO_RESPONSE 例外（沒有 response，官方照樣帶）。
  - session 編號：官方只有一個計數器，內層、外層各取一號；不帶內層 session 的協定只用掉外層那一號。
  以前這裡寫「逐位元組相同（以真實擷取驗證）」，其實只驗證過登入、心跳等少數協定（LESSONS #66）。

收到的事件同時：交給 listeners、放進 EventQueue（wait_for / drain，與即時模式相同介面）、
並用 session 喚醒等待中的 request。
"""

import queue
import random
import socket
import struct
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..live import EventQueue
from ..sproto import Schema, encode_gate, encode_rpc, sproto_pack
from ..stream import PacketStream
from . import auth
from .crypto import des_encrypt

GAME_PORT = 11000
# 規範第 3 點（使用者 2026-10-21 決定）：任兩個送出之間隨機間隔 0.3～0.8 秒（像真人、不規律），
# 下限 0.3 秒不能再調低。
REQUEST_INTERVAL = (0.3, 0.8)
MIN_REQUEST_INTERVAL = REQUEST_INTERVAL[0]
ROLE_LOGIN_KEY = "gongxifacai888"  # Role_RoleLogin.key（客戶端常數）
CONN = "client"

# ---- 送出格式對照表（2026-10-23；證據與統計見 doc/REVERSE-ENGINEERING.md §10.4）----
# 證據代號：
#   初心錄檔 = 本專案 captures/ 的官方畫面版錄檔（2026-10-21，8 次登入；只讀統計、不 commit）
#   時光錄檔 = 前置專案 參考專案 的官方畫面版錄檔（另一個版本，協定與客戶端程式相同，23 份）
#   IL       = HotUpdate.dll 的 ClientSendProtocolMsg：SendProtocolMsg(req, session, …, rid) 的 rid 是參數（帶）
#              或常數 0／4 參數多載 Common.SendProtocolMsg（固定 rid 0 → 不帶）
#   參考專案     = doc/autoplay/參考專案-learnings.md §1（他用官方錄檔／IL 的結論；我方沒有樣本時才只靠它）
# 外層 GateMessage 帶 head{rid=本連線, notMain=false}
GATE_HEAD = frozenset({
    "Role_RoleLogin",                 # 初心錄檔 8/8、IL
    "Role_Heart",                     # 初心錄檔 433/433、IL
    "Battle_BattleAllInfo",           # 初心錄檔 8/8、IL
    "Battle_BattlePlayOver",          # 時光錄檔 1360/1501 帶（不帶的 141 筆是隊長連線在一串 PlayOver／EscapeOver 後多送的一筆，時序另案 §1.10）、IL
    "Battle_BattleEscapeOver",        # 時光錄檔 1328/1329、IL
    "Item_ItemUse",                   # 時光錄檔 6/6、IL
    "ShopMall_BuyShopMallItem",       # 商城買阿布的水（jobs/herobf）：參考專案 實機照預設帶 head 成功（他們列 UNMEASURED，沒有官方錄檔）；我方無樣本
    "ItemHandle_GetNewItemDepotInfo",  # 時光錄檔 30/30、IL
    "ItemHandle_GetItemToNewDepot",   # 時光錄檔 7/7、IL
    "ItemHandle_SaveItemToNewDepot",  # 時光錄檔 6/6、IL
    "ItemHandle_itemEquipOn",         # 時光錄檔 5/5、IL
    "Role_LoopBattle",                # 外掛「原地遇敵」：時光錄檔 2 筆帶（主畫面遇敵鈕 7 筆不帶，我們不走那條）、IL rid 參數
    "Role_UnLoopBattle",              # 時光錄檔 6/6、IL
    "WaiGua_FastBattleHandle",        # 時光錄檔 5/5、IL
    "WaiGua_NomalSettingHandlePet",   # 時光錄檔 1/1、IL
    "WaiGua_AutoRolePointSet",        # IL、參考專案（我方無樣本）
    "Role_RolePointSave",             # 套用配點方案（jobs/loadout，只送 action 3）：參考專案 官方錄檔 psa_20261023_082526 2/2 帶 head、
                                      # 參考專案 2026-10-23 實測帶 head；我方無樣本
    "NewPet_NewPetChangeStatus",      # 時光錄檔 5/5、IL
    "Pet_DropPet",                    # IL、參考專案（我方無樣本）
    "Item_batchDelItem",              # 參考專案（官方錄檔 15 組，一次 4～5 格、帶 head；我方無樣本）
    "Other_eatMMItem",                # IL：SendeatMMItem(…, rid) rid 是參數（UINourishScreen 傳角色 rid）；jobs/lingshi 餵女寵（10-31）
    "Pet_ChangePetName",              # IL：SendChangePetName(…, rid) rid 是參數；jobs/lingshi 女寵餵滿改名（10-31）
    "Pet_NewPetAllegiance",           # IL：SendNewPetAllegiance(petIndex, action, rid) rid 是參數；jobs/lingshi 補戰寵忠誠（11-04）
    "NewPet_SavePetToNewDepot",       # 參考專案（錄檔 psa_20261022_113535 全部帶 head；我方無樣本）
    "NewPet_GetNewPetDepotInfo",      # 我方實機 2026-10-24：第三組查寵物倉庫、領回金虎都成功（預設帶 head）
    "NewPet_GetPetFromNewDepot",      # 同上；和 ItemHandle_GetItemToNewDepot／SavePetToNewDepot 同系列都帶 head
    "WaiGua_FastBattleSettingChange",  # 參考專案（IL：外掛畫面送出 rid＝選中角色、session true；我方無樣本）
    "WaiGua_FastBattleSettingReviveSkillChange",  # 外掛技能復活（jobs/revive）：我方無樣本，照 WaiGua_* 同系列帶 head
    "WaiGua_FastBattleSettingMpChange",  # 外掛物品補氣（jobs/mpitem）：我方無樣本，照 WaiGua_* 同系列帶 head
    "WaiGua_FastBattleSettingHpChange",  # 外掛人物技能補血（jobs/wgheal）：我方無樣本，照 WaiGua_* 同系列帶 head
    # 設定檔 [waigua.raw]（使用者 2026-11-05「把外掛設定全部拉成配置檔」）可以直設的：我方無樣本，照 WaiGua_* 同系列帶 head
    "WaiGua_FastBattleSettingMpSkillChange",
    "WaiGua_NomalSettingEatExpItem",
    "WaiGua_NomalSettingHandleHeal",
    "WaiGua_AutoEatOffLineItem",
    "WaiGua_CatchPetSetting",         # 外掛自動抓寵：primaljason-home 2026-10-24 第二組實機（request 有回 isOK）；與其他 WaiGua_* 同系列都帶 head
    "Item_oneKeyBuyItem",             # 抓寵「+」買誘餌：參考專案 錄檔 psa_20261022_165938 逐位元組核對 A 級（session、rid→head）；我方 2026-10-25 首次用
    # 大亂鬥（jobs/melee）：我方實機 2026-10-22～24 第二／三組照預設帶 head，報名、查狀態、下指令、讀系統信都成功（無官方樣本）
    "NewRandomPkRec_GetRandomPkInfo",
    "NewRandomPkRec_GetRandomPkRoleStatus",
    "NewRandomPkRec_JoinorLeaveRandomPk",
    "Battle_BattleRoleCmd",
    "NewEmail_GetNewSystemThingsList",
    "NewRoleWTask_GetTaskInfo",      # 每日委託清單：2026-10-28 第三組實機（當時不在表裡＝預設帶 head）
    "NewRoleWTask_UploadWTask",      # 交每日委託：同上
    "Item_ItemBook",                 # 料理合成：同上（每次扣 1000 石幣）
    "NewRole_RoleAfk",               # 離線掛機：2026-10-27 第五組實機 isOK 成功時還不在任何表裡＝照預設**帶 head**（LESSONS #95 的規矩：以實機送出的格式為準）
    "Role_Exit",                     # 官方從不送（IL 沒有送出方法）；沿用舊做法，拿掉與否見 參考專案-learnings §1.3
    "NewRole_GetRoleMagicInfo",       # 查魔法抗性／熟練（jobs/kangxing，醫院黑暗精靈王「抗性查詢」背後的協定）：
                                      # 我方無樣本、IL 未查 → 照預設帶 head；實機回 isOK 才算對（LESSONS #95 的規矩）
    # 接力回村（jobs/lingshi.relay_trip）：2026-11-03 第 9 組實機（captures/team_afk_probe.log）成功時還不在任何表裡＝照預設**帶 head**；
    # 邀請、同意入隊（被傳到隊長身邊）、轉隊長都成功。IL 未查、無官方樣本（LESSONS #95：以實機送出的格式為準）
    "Team_TeamInvite",
    "Team_InviteResponse",            # 伺服器不回 response（實機 request 逾時，動作照樣生效）→ 只能 send
    "Team_TransferLeader",
    "Task_NewTaskGiveUp",             # 放棄任務：2026-10-26 第四組實機 5/5 成功時還不在任何表裡＝照預設**帶 head**；
                                      # 沒有 response，send 後看推回的 Task_TaskInfo。不要跟著 Task_RequestNavigate 搬去不帶（LESSONS #95）
})
# 外層 GateMessage 不帶 head（官方 rid 傳 0）
GATE_NO_HEAD = frozenset({
    "ReCharge_GetLimitDateRechargeList",  # 初心錄檔 8/8、IL
    "Other_GetRoleMessageTip",        # 初心錄檔 8/8、IL
    "NewEmail_NewFriendRedTip",       # 初心錄檔 8/8、IL
    "Other_GendManorFightIsOpen",     # 初心錄檔 8/8、IL
    "Other_GetNewShopBoothGoodType",  # 初心錄檔 66/66、IL（常駐交易所掃描以前都帶）
    "Other_GetNewShopBoothGoodInfo",  # 初心錄檔 1480/1480、IL
    "Other_NewShopGetItemPriceRecord",  # 初心錄檔 739/739、IL
    "Other_NewShopGetPetPriceRecord",  # 初心錄檔 744/744、IL
    "Other_GetNewShopGoodByIndex",    # 交易所點一筆看詳細（jobs/marketpets）：參考專案 官方錄檔 psa_20261023_102736 等外層不帶 head；11-14 實機 19 筆都有回
    "Other_MyItemShopGoods",          # 集市上架（jobs/auction）：**未實機驗證**，沒有錄檔；照同系列 Other_*Shop* 錄檔都不帶 head
    "Other_MyPetShopGoods",           # 讀自己的寵物攤位（jobs/petauction）：參考專案 官方錄檔 psa_20261023_130926 不帶 head；11-14 實機第八組帶 head 送也有回（登記前的預設），不帶的還沒實機
    "Other_NewUploadPetDepotToAh",    # 個人寵物倉上架交易所（jobs/petauction）：同上錄檔 2 筆不帶 head；11-14 實機帶 head 送 4 筆都成功，不帶的還沒實機
    "Other_NewShopBuyGood",           # 交易所購買（jobs/marketbuy）：參考專案 官方錄檔 2/2 head false、IL SendNewShopBuyGood rid 0
    "Npc_NpcInteract",                # 時光錄檔 53/53、IL
    "Role_RoleWalk",                  # 時光錄檔 167/167、IL
    "Role_NomalHandle",               # 時光錄檔 5/5、IL
    "Team_TeamDismiss",               # 時光錄檔 1/1、IL
    "Transfer_TransferTo",            # 時光錄檔 1/1、IL
    "NewRole_SynRolePos",             # 時光錄檔 122/122、IL
    "Role_RoleWarp",                  # IL、參考專案（錄檔 10 筆）
    "Item_NewExchangeItem",           # IL：SendNewExchangeItem 與 SendRoleWarp 同樣 session true、rid 常數 0（2026-10-24 查）
    "Other_ExChangeMMItem",           # IL：SendExChangeMMItem 與 SendRoleWarp 同樣 session true、rid 常數 0（2026-10-31 查，jobs/lingshi 換靈項鏈；未實機）
    "Team_TeamLeave",                 # IL、參考專案
    "Task_RequestNavigate",           # IL、參考專案
    "Task_Transfer",                  # IL、參考專案
    "Task_TaskwaitThingCheck",        # IL（參考專案 沒列）
    "NewRole_RealHideMode",           # IL（參考專案 沒列）
    "Activity_EveryDayRewardGetInfo",  # IL（參考專案 沒列）
    "Activity_EveryDayRewardGet",     # IL（參考專案 沒列）
    "Role_SwitchRoleGetInfo",         # 戰鬥中刷新開切換角色面板（headless_party.battle_refresh）：參考專案 官方錄檔 psa_20261023_094115 5/5 不帶 head（UISwitchRoleScreen.OnInit）；我方未實機
    "Role_SwitchRoleMange",           # 外掛自動戰鬥切換（jobs/herobf）：參考專案-learnings §「人物一般動作」A 級＝走隊長連線、外層不帶 head；我方無樣本
    # 夢幻森林（jobs/forest）：參考專案 錄檔 psa_20261023_134155 GetPetZooInfo 不帶 head、內層 session；ExitPetZoo 照 IL（session true、rid 0），沒錄檔樣本
    "Other_GetPetZooInfo",
    "Other_ExitPetZoo",
    # 角色交易（jobs/trade）：時光官方錄檔 official_trade_20261027.pcapng；我方無樣本、未實機
    "RoleTrade_TradeRequest",         # 時光錄檔 1/1
    "RoleTrade_TradeResponse",        # 時光錄檔 1/1
    "RoleTrade_TradeUploadThing",     # 時光錄檔 5/5
    "RoleTrade_TradeSure",            # 時光錄檔 2/2
    "RoleTrade_TradeStop",            # 時光錄檔 2/2
    "RoleTrade_GetTradeSaveList",     # 時光錄檔 6/6
    "RoleTrade_GetTradeSaveItemPet",  # 時光錄檔 7/7
})
# 協定有 response、但官方內層不帶 session 的（IL session 參數 false；伺服器不回 response）。
# 這些不能用 request() 等回應，只能 send() 後看推送。
NO_INNER_SESSION = frozenset({
    "Other_GendManorFightIsOpen",     # 初心錄檔 8/8、時光錄檔 158/158、IL
    "Role_NomalHandle",               # 時光錄檔 5/5（IL session 是參數，快速組隊傳 false）
    "NewPet_NewPetChangeStatus",      # 時光錄檔 5/5、IL
    "Pet_DropPet",                    # IL、參考專案（我方無樣本）
})
# 協定沒有 response、但官方內層照樣帶 session 的（只能 send() 看推送）。
INNER_SESSION_NO_RESPONSE = frozenset({
    "RoleTrade_TradeSure",            # 時光錄檔 2/2（official_trade_20261027.pcapng）
})
assert not GATE_HEAD & GATE_NO_HEAD, "同一個協定不能同時在帶／不帶 head 兩張表"


def gate_head(name: str) -> bool:
    """這個協定的外層 GateMessage 要不要帶 head。不在兩張表裡的照舊帶（測試要求程式送的協定都要列）。"""
    return name not in GATE_NO_HEAD


def has_inner_session(schema: Schema, name: str) -> bool:
    """內層 Package 要不要帶 session：協定有 response 且不在 NO_INNER_SESSION；或在 INNER_SESSION_NO_RESPONSE。"""
    if name in INNER_SESSION_NO_RESPONSE:
        return True
    return bool(schema.protocols[name]["response"]) and name not in NO_INNER_SESSION

Event = Dict[str, Any]


class ClientError(RuntimeError):
    pass


_SECRET_FIELDS = {"pcId", "key", "password", "passWord", "cdKey", "account"}


def _brief(data: Optional[Dict[str, Any]], limit: int = 120) -> str:
    """流量日誌用的參數摘要：敏感欄位遮掉、太長截斷。"""
    if not data:
        return ""
    shown = {k: ("***" if k in _SECRET_FIELDS else v) for k, v in data.items()}
    text = str(shown)
    return text if len(text) <= limit else text[:limit] + "…"


EVENTS_KEEP = 2000   # 事件佇列最多留幾筆，超過丟最舊的（沒人取的佇列不無限長）


class GameClient(EventQueue):
    def __init__(self, schema: Optional[Schema] = None, timeout: float = 10.0,
                 heartbeat_interval: float = 5.0, interval: Tuple[float, float] = REQUEST_INTERVAL,
                 # 頻率限制用高解析度時鐘：Windows 的 time.monotonic 解析度只有 15.6ms，
                 # 曾讓實際間隔低於下限（實測 0.294 秒，LESSONS #23）
                 clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep,
                 rng: Optional[random.Random] = None) -> None:
        super().__init__()
        lo, hi = interval
        if lo < MIN_REQUEST_INTERVAL or hi < lo:
            raise ValueError(f"請求間隔範圍 {interval} 不合法：下限不能小於 {MIN_REQUEST_INTERVAL} 秒（規範第 3 點）")
        self.interval, self._clock, self._sleep = (lo, hi), clock, sleep
        self._rng = rng or random.Random()
        self._next_allowed: Optional[float] = None
        self.schema = schema or Schema.load()
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.listeners: List[Callable[[Event], None]] = []
        self.sock: Optional[socket.socket] = None
        self.stream = PacketStream(self.schema)
        self.key: Optional[bytes] = None
        self.rid: Optional[int] = None
        self.info: Optional[auth.TokenInfo] = None
        self.seq = 0
        self.session = 0
        self._send_lock = threading.Lock()
        self._pending: Dict[int, Future] = {}
        self._closed = threading.Event()
        self._threads: List[threading.Thread] = []
        self.sent = 0
        self.received = 0
        # 流量日誌（選用）：每個送出/收到的封包一行；敏感欄位不記（見 _brief）
        self.trace: Optional[Callable[[str], None]] = None

    # ---- 連線與握手 ----
    def _recv_line(self, buf: bytearray) -> bytes:
        deadline = time.monotonic() + self.timeout
        while b"\n" not in buf:
            if time.monotonic() > deadline:
                raise ClientError("握手逾時")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ClientError("伺服器在握手時關閉連線")
            buf += chunk
        i = buf.index(b"\n") + 1
        line = bytes(buf[:i])
        del buf[:i]
        return line

    def connect(self, host: str, port: int, account: str, password: str, node: str,
                pc_id: str, rid: int, index: int = 1) -> auth.TokenInfo:
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        buf = bytearray()
        tr = self.trace or (lambda s: None)
        tr(f"連線 {host}:{port}")
        self.sock.sendall(auth.token_request(account, password, node, pc_id))
        tr(f"送 握手#1（帳密不記錄）node={node}")
        self.info = auth.parse_token_response(self._recv_line(buf))
        tr(f"收 握手#2 uid={self.info.uid} subId={self.info.sub_id}（金鑰不記錄）")
        self.key, self.rid = self.info.key, rid
        self.sock.sendall(auth.contrast_request(self.info, node, rid, index=index, seq=2))
        tr(f"送 握手#3 seq=2 index={index}")
        auth.check_contrast_response(self._recv_line(buf))
        tr("收 握手#4 200")
        self.seq = 3
        self.stream.set_key(CONN, self.key)
        self.sock.settimeout(None)
        if buf:
            self._on_bytes(bytes(buf))
        self._start(self._reader, "x7k2qm-reader")
        return self.info

    def _start(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # ---- 收 ----
    def _reader(self) -> None:
        try:
            while not self._closed.is_set():
                data = self.sock.recv(65536)
                if not data:
                    break
                self._on_bytes(data)
        except OSError:
            pass
        finally:
            self._closed.set()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ClientError("連線已關閉"))

    def _on_bytes(self, data: bytes) -> None:
        for e in self.stream.feed(CONN, "s2c", data, time.time()):
            self.received += 1
            if self.trace:
                self.trace(f"收 {e.get('kind')} s={e.get('session')} {e.get('name')} "
                           f"{_brief(e.get('data')) if e.get('kind') in ('error', 'unknown', 'decode_error') else ''}")
            if e.get("kind") == "response" and e.get("session") in self._pending:
                fut = self._pending.pop(e["session"])
                if not fut.done():
                    fut.set_result(e)
            for f in self.listeners:
                f(e)
            self.events.put(e)
            # 佇列只給 wait_for／drain 用（例如 trip_spirit 先 drain 再等）；常駐練功只靠 listeners、
            # 登入後就沒人取，舊版無上限：k8s 常駐每小時多 3 萬多筆事件、7～8 小時就碰 512Mi 被 OOMKilled（11-06）
            while self.events.qsize() > EVENTS_KEEP:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break

    # ---- 送 ----
    def send(self, name: str, data: Optional[Dict[str, Any]] = None,
             _future: Optional[Future] = None, head: Optional[bool] = None) -> Optional[int]:
        """送出一個 request；帶內層 session 的協定回傳 session（之後用它對回應），其餘回傳 None。
        head：None＝照 GATE_HEAD／GATE_NO_HEAD 表；True／False＝這一筆強制帶／不帶外層 head（同一協定官方不同呼叫點
        rid 不同時用。例：Role_NomalHandle type 6／12 官方傳 0 不帶，type 13 開自選蛋傳角色 rid 要帶——參考專案 官方錄檔
        psa_20261031_234410 逐位元組、IL `SendRoleNomalHandle(13, …, role.roleAttr.rid)`）。"""
        if self.key is None:
            raise ClientError("尚未完成登入握手")
        if self._closed.is_set():
            raise ClientError("連線已關閉")
        with_session = has_inner_session(self.schema, name)
        with self._send_lock:
            if self._next_allowed is not None:  # 頻率限制：在鎖內等，所有執行緒（含心跳）共用同一個節奏
                wait = self._next_allowed - self._clock()
                if wait > 0:
                    self._sleep(wait)
            # 官方只有一個 session 計數器：帶內層 session 的協定用掉兩號（內層 n、外層 n+1），不帶的只用掉外層一號
            inner_session = None
            if with_session:
                inner_session = self.session
                self.session += 1
            outer_session = self.session
            self.session += 1
            # 伺服器的回應是「外層 GateMessage 的 response」（session = 外層），裡面再包內層 response
            # （session = 內層）；兩層都要登記，dispatch 才能先拆信封、再對到協定。
            self.stream.conns[CONN].sessions.remember(outer_session, "GateMessage")
            if inner_session is not None:
                self.stream.conns[CONN].sessions.remember(inner_session, name)
                if _future is not None:  # 在鎖內登記，回應不可能比登記早到、也不會被別的執行緒搶走 session
                    self._pending[inner_session] = _future
            inner = encode_rpc(self.schema, name, data, inner_session)
            if gate_head(name) if head is None else head:
                outer = encode_gate(self.schema, inner, rid=self.rid, session=outer_session, not_main=False)
            else:
                outer = encode_gate(self.schema, inner, session=outer_session)
            cipher = des_encrypt(self.key, sproto_pack(outer))
            frame = struct.pack(">H", len(cipher) + 4) + cipher + struct.pack("<I", self.seq)
            self.seq += 1
            self.sock.sendall(frame)
            if self.trace:
                self.trace(f"送 seq={self.seq - 1} s={inner_session}/{outer_session} {name} {_brief(data)}")
            self._next_allowed = self._clock() + self._rng.uniform(*self.interval)
            self.sent += 1
        return inner_session

    def request(self, name: str, data: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """送出並等到同 session 的 response，回傳其 data。"""
        if not self.schema.protocols[name]["response"]:
            raise ClientError(f"{name} 沒有 response，請用 send()")
        if name in NO_INNER_SESSION:
            # 官方不帶內層 session、伺服器也不回 response：用 request 只會逾時，改 send() 後看推送
            raise ClientError(f"{name} 官方內層不帶 session（伺服器不回 response），請用 send() 再看推送")
        fut: Future = Future()
        sess = self.send(name, data, _future=fut)
        try:
            return fut.result(timeout or self.timeout)["data"]
        except TimeoutError:
            self._pending.pop(sess, None)
            raise ClientError(f"{name} 等待回應逾時") from None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ---- 進入遊戲、心跳 ----
    def enter_game(self, pc_id: str, client_ver: str, pc_ip: str, country: str) -> Dict[str, Any]:
        resp = self.request("Role_RoleLogin", {"loginRid": self.rid, "pcId": pc_id, "clientVer": client_ver,
                                               "pcIp": pc_ip, "key": ROLE_LOGIN_KEY, "country": country})
        if resp.get("ipMaxFlg"):
            # 同 IP／同機器在線數超過上限：伺服器會把這台機器的角色全部登出（語系 40000000105）。不重試。
            raise ClientError(f"登錄上限已達到（ipMaxFlg）：{resp}")
        if not resp.get("isOK"):
            raise ClientError(f"Role_RoleLogin 失敗：{resp}")
        if self.heartbeat_interval:
            self._start(self._heartbeat, "x7k2qm-heartbeat")
        return resp

    def _heartbeat(self) -> None:
        while not self._closed.wait(self.heartbeat_interval):
            try:
                self.send("Role_Heart", {"clientTime": int(time.monotonic() * 1000) & 0x7FFFFFFF})
            except (ClientError, OSError):
                return

    def close(self) -> None:
        self._closed.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        for t in self._threads:
            t.join(timeout=2)

    def __enter__(self) -> "GameClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
