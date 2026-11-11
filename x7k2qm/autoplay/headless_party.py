"""無畫面五開：自己登入 5 個角色、組隊、傳送、開原地遇敵，並持續監管（不需要遊戲視窗、不佔滑鼠）。

    python -X utf8 -m x7k2qm.autoplay.headless_party --log captures/autoplay_headless.log

- 帳號、密碼讀第二組環境變數 BOT_ACCOUNT_2 / BOT_PASSWORD_2（密碼由使用者自己設定）。
- 外掛設定（自動／快速戰鬥、寵物、經驗果、加點）存在伺服器，登入就生效；快速戰鬥時伺服器自己算完，
  隊員連線只收到 Battle_BattleEnd，不用回 PlayOver（2026-10-21 封包確認）。
- 登入會把遊戲視窗裡的同一個角色擠下線（同角色只能一處登入）。
- 決策沿用 supervisor.decide；每個動作等封包，失敗就停（規範第 2 點）。
- 要停止：建立 `<日誌檔>.stop`（例：`touch captures/autoplay_headless.log.stop`），下一輪就關遇敵、
  每隻送 Role_Exit 登出再結束。不要直接砍行程（不會登出）。
- `--api-port 8791`：開本機唯讀查詢 API（`party_api.py`），隨時查背包、寵物、任務、倉庫，不用另外登入。
- 登入前拿帳號擁有者鎖（`owner.py`，`captures/owner/<組>.json`）：同組已有別的程式在用就不登入。
- 工作佇列（`jobqueue.py`）：`python -m x7k2qm.autoplay.jobs add <工作> --profile N` 排的工作，
  主迴圈每輪取一件、用自己的連線做（不另外登入），做完回練功（doc/autoplay/flows/jobs.md）。
- `--scenario config/scenarios/rebirth.toml`：劇本；佇列空、沒有 hold 時照劇本往下排工作（`scenario.py`、
  doc/autoplay/flows/scenario.md），進度記在 captures/scenario/<組>.json，重開接著做。
"""

import argparse
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..client.config import load
from ..client.session import open_client
from .debugtap import DebugTap
from .logrotate import RotatingFile
from .supervisor import REPORT_EVERY, ROUTE, SPOTS, State, decide

# (rid, 名字)；第一個是隊長
PARTY = [(10003754, "角色60"), (10003796, "角色49"), (10003799, "角色109"), (10003803, "角色38"), (10003804, "角色65")]
TRANSFER_POINT = {11001: 16, 10001: 17, 31401: 18, 31706: 19, 11004: 20, 500: 21, 502: 23, 60049: 22, 300: 40}
# 路線外的 SPOTS 也能當 --start（配 --stay 鎖著練，supervisor.SPOTS）
NAME_TO_POINT = {name: TRANSFER_POINT[m] for name, m, _ in ROUTE + SPOTS if m in TRANSFER_POINT}
NAME_TO_MAP = {name: m for name, m, _ in ROUTE + SPOTS}
PET_DETAIL_KEYS = ("exp", "mp", "mpMax", "trans", "maxLevel", "earth", "water", "fire", "wind", "allegiance", "petStar",
                   "physicalAttack", "physicalDefense", "agile", "physicalCrit", "physicalResistCrit",
                   "physicalCritDamage", "physicalHit", "physicalDodge", "physicalPenetrate", "physicalBeatBack")
# 遊戲客戶端進入遊戲後送的請求：前四個伺服器會回應，Other_GendManorFightIsOpen 不回
POST_LOGIN_REQUESTS = ["Battle_BattleAllInfo", "ReCharge_GetLimitDateRechargeList", "Other_GetRoleMessageTip",
                       "NewEmail_NewFriendRedTip"]
POST_LOGIN_SENDS = ["Other_GendManorFightIsOpen"]
LOGOUT = "Role_Exit"
LOGOUT_GRACE = 1.0          # 送出登出後等伺服器處理的秒數（伺服器不回 response）
RETRY_LEVELS = 3
# 退回後最多等這麼久就再試，不管等級有沒有多 RETRY_LEVELS（LESSONS #164）：100 級在柯奧4F 一小時才升 1 級、
# 76 級在五兄弟兩小時沒升級，只看等級等於卡死在低等圖
RETRY_SECONDS = 1200
EXP_FRUITS = {90398, 90399, 90638, 90639}   # Config.autoEatExpItemList：外掛可自動吃的經驗道具
SPIRIT_BOW = 2775                            # 精靈王之弓：轉生後 1 級立刻穿上（使用者 2026-11-11）
# 人物裝備的道具類型（Item.type）：0～4、17～19 武器，6 兜，7 鎧，8 手環，9 貝鈴，10 項鍊，11 戒指，13 耳飾，
# 15 護身符，24 腰帶，25 盾，26 鞋，27 手套。29～37 是寵物裝備、16 聖水、20 肉、21 雜項、22 寶石、23 行李：不是人物裝備。
# 迎新助力禮盒（內含新手裝、經驗果、1轉新手禮盒）、1～5轉新手禮盒（90328～90333）。
# 使用者 2026-10-24：「新手禮盒不一定一轉」——每轉都有自己的禮盒，轉到幾轉就開到幾轉（boxes_for_trans）
GIFT_BOXES = {90334, 90328, 90329, 90330, 90331, 90332, 90333}
TRANS_BOX = {1: 90328, 2: 90329, 3: 90330, 4: 90331, 5: 90332}


def boxes_for_trans(trans: int) -> set:
    """這個轉數**現在打得開**的禮盒：迎新助力禮盒＋1 轉到目前轉數的新手禮盒（5 轉兩種都算）。
    還沒到的轉數的禮盒不取出、不開，留在倉庫（取出來打不開只會佔背包）。"""
    out = {90334} | {b for t, b in TRANS_BOX.items() if t <= trans}
    if trans >= 5:
        out.add(90333)
    return out
CHAR_EQUIP_TYPES = {0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 13, 15, 17, 18, 19, 24, 25, 26, 27}
WEAPON_TYPES = {0, 1, 2, 3, 4, 17, 18, 19}


def EQUIP_SLOT(t):
    """裝備部位：武器類型共用一個部位，其他類型各自一個。"""
    return "武器" if t in WEAPON_TYPES else t
# 回村補血的固定路徑（2026-10-21 遊戲實際走的格子，captures/quests/heal_薩村醫院.jsonl）
def species_of(pid, name: str) -> tuple:
    """練寵模式要練的物種：一種（int）、一組（tuple，五隻都可以練其中任一種），
    或每隻角色各自一種（dict 角色→petId；使用者 11-12「每一隻人物負責一種老虎及人龍」）。"""
    if isinstance(pid, dict):
        v = pid.get(name)
        return () if v is None else (v if isinstance(v, tuple) else (v,))
    return pid if isinstance(pid, tuple) else (pid,)


def p_level(p):
    return p.get("level") or 0


GOLD_TIGER = 310          # 金格薩貝魯（金虎）：轉生後固定當騎寵（使用者 2026-10-24）
RED_TIGER = 309           # 格魯西斯（紅虎）：沒有 1 級金虎時改騎 1 級紅虎（使用者 11-09「去抓五隻紅虎來騎」）
SPIRIT_ITEM_TYPE = 9      # Item.json type：精靈道具（貝鈴／石笛／木笛／金幣）；其他裝備的 skill 不是精靈
PET_LEVEL_MAX = 140       # 滿級；戰寵要挑**未滿 140** 裡等級最高的（滿級的練不動）
PET_DEPOT_NUM = 20        # 寵物倉庫一頁幾格（Config perPetDepotNum／P_perPetDepotNum）
PET_DEPOT_PAGES = 5       # 找寵物在第幾頁時最多查幾頁（上限保護；實機共倉開到第 2 頁）
SIGNIN_LEVEL = 120          # 每日簽到的等級門檻（Activity[5].need levela:120；沒到門檻伺服器不回應）
TRANSFER_RETRY_WAIT = 20.0  # 補血傳送被拒：最多等這麼久讓戰鬥打完，再重送一次
TRANSFER_RETRY_NAP = 5.0    # 重送前再等幾秒（每日 00:00 重置當下伺服器會拒）

HEAL_PATH_VILLAGE = [(92, 99), (91, 98), (90, 98), (89, 98), (88, 98), (87, 97), (87, 96), (87, 95), (87, 94),
                     (87, 93), (87, 92), (87, 91), (87, 90), (86, 89), (86, 88), (86, 87), (86, 86), (86, 85),
                     (86, 84), (86, 83), (86, 82), (86, 81), (86, 80), (86, 79), (86, 78), (86, 77), (86, 76),
                     (86, 75), (86, 74), (86, 73), (86, 72), (85, 71), (84, 70), (83, 69), (82, 68), (81, 67), (80, 66)]
HEAL_PATH_HOSPITAL = [(26, 65), (26, 64), (26, 63), (26, 62), (26, 61), (25, 61), (24, 60), (23, 60), (22, 60)]


class Party:
    def __init__(self, log: Callable[[str], None], open_fn=None, stop_file: Optional[str] = None,
                 profile: str = "2", party: Optional[List[Tuple[int, str]]] = None, discover: bool = False,
                 size: int = 5, pick: Optional[int] = None, picks: Optional[List[int]] = None) -> None:
        self.log = log
        self.stop_file = stop_file
        # 多帳號：profile＝第幾組環境變數（BOT_ACCOUNT_<profile>）。每組帳號一個行程、一個 pcId（組內 5 條共用）。
        # 角色清單：第 2 組沿用寫死的 PARTY；其他組從 HTTP 帳號登入結果取（登入時才知道）。
        # size：最多登入幾隻（帳號角色清單的前 size 隻；單人工作如 herobf 用 1 = 只登第一個人物）。
        self.profile = profile
        self.size = size
        self.pick = pick                              # 從帳號第幾隻人物開始取（None＝第 1 隻）
        # picks：只登帳號的這幾隻（1 起算，例 [3, 4, 5]；herobf --roles，使用者 10-31「帳號 3 第 3、4、5 隻同時打」）。
        # 一樣同一個 pcId、同一個行程：同帳號開兩個程式會互踢（System_KickConnect reason 1）。給了就不看 pick／size
        self.picks: Optional[List[int]] = list(picks) if picks else None
        # 帳號的人物順序（第 N 隻 → (rid, 名字)）；登入結果拿到才知道（第 2 組寫死名單就是 PARTY）。herobf --roles 用來對名字
        self.roster: Optional[List[Tuple[int, str]]] = list(PARTY) if profile == "2" and not discover else None
        # 第 2 組沒加 --discover 用寫死名單：pick 也要套（審查 10-31：原本只在探索時套，--role 2 會默默打成第 1 隻）
        if self.picks and not party and profile == "2" and not discover:
            self.party = self._choose(list(PARTY))
        else:
            self.party: List[Tuple[int, str]] = list(party) if party else (
                list(PARTY)[(pick or 1) - 1:][:size] if profile == "2" and not discover else [])
        if pick and profile == "2" and not discover and not party and not self.party:
            raise RuntimeError(f"第 2 組只有 {len(PARTY)} 隻人物，沒有第 {pick} 隻")
        self.open_fn = open_fn or self._open
        self.clients: Dict[str, object] = {}
        # 可重入：wait() 握著鎖呼叫條件函式，條件裡常會呼叫 bag()、pet_index() 這類也要拿鎖的方法。
        # 用 threading.Lock 會死結（連收封包的監聽器一起卡住），LESSONS #29 犯過、2026-10-24 又犯一次（#72）
        self.lock = threading.RLock()
        self.s = State()
        self.battles: List[tuple] = []          # (時間, win)
        self.last_battle: Optional[tuple] = None   # 最後一場 (unix 秒, win)；換圖清 battles 也不清它（快照 lastBattle）
        self.trans: Dict[str, int] = {}          # 角色 → 轉數（RoleAttr.trans）
        self.finished: Dict[str, List[int]] = {}  # 角色 → 已完成任務群組（RoleAttr.finishTasks；處置規則 done= 條件用）
        # 角色 → 貨幣（RoleAttr gold＝幣種 2 活力、lockDenar＝幣種 8 聲望、stone＝幣種 3 石幣；快照 money）
        self.money: Dict[str, Dict[str, int]] = {}
        self.fast: Dict[str, bool] = {}         # 角色 → 快速戰鬥是否開著（Role_RoleInfo.fastBattleFlg）
        self.pending: List[str] = []            # 收到出招序列、還沒回 PlayOver 的角色
        # 非快速戰鬥只回 PlayOver、不回 EscapeOver（herobf 開；官方當前角色就是這樣，參考專案 ops/battle IL 註解）：
        # 每回合少送一筆＝少等一次 0.3～0.8 秒間隔。沒人逃跑才省；送了 PlayOver 之後 ESCAPE_FALLBACK 秒伺服器
        # 還沒進下一回合 → 補送 EscapeOver 並退回原做法（使用者 10-31 選「1 不送 EscapeOver」）
        self.skip_escape_over = False
        self.escape_seen: set = set()            # 這回合序列裡有人逃跑（skillType 9）的角色
        self.rounds_seen: Dict[str, int] = {}    # 每隻收到幾次 CmdBegin／BattleEnd（看伺服器有沒有進下一回合）
        self.escape_wait: Dict[str, Tuple[float, int]] = {}   # 只送了 PlayOver 的角色 → (時間, 當時 rounds_seen)
        # 戰鬥中刷新（照 參考專案 ops/battle.refreshing；使用者 10-31「先用 參考專案 的戰鬥刷新看看」，**未實機**）。
        # 預設關：關著時 service() 跟原本完全一樣（只回 PlayOver＋EscapeOver）。見 service_refresh()
        self.battle_refresh = False
        self.seq_round: Dict[str, int] = {}      # 收到 CmdSeq 當下的 rounds_seen（刷新前看伺服器是不是已經往下走）
        # 這回合的刷新：{"round": 當時隊長 rounds_seen, "last": 上次送的時間, "repeat": 已重送幾次}；None＝這回合沒刷新
        self.refresh_round: Optional[dict] = None
        self.refresh_count = 0                   # 總共刷新幾次（含重送）
        # 每回合計時（隊長連線：CmdBegin → 下一個 CmdBegin／BattleEnd）；round_stats() 取統計、reset_round_stats() 歸零
        self.round_times: List[float] = []       # 每回合秒數
        self.battle_rounds: List[Tuple[int, float]] = []   # 每場 (回合數, 秒數：第一個 CmdBegin → BattleEnd)
        self._round_t0: Optional[float] = None   # 這回合 CmdBegin 的時間
        self._battle_t0: Optional[float] = None  # 這場第一個 CmdBegin 的時間
        self._battle_n = 0                       # 這場目前幾個回合
        self.history: List[tuple] = []          # (角色, 事件)：登入後收到的所有事件（超過 20000 筆會砍掉舊的）
        # 執行中除錯（查詢 API /debug；使用者 2026-11-05「需要一個 runtime debug 方式」）：有時限、時間到自動關
        self.debug = DebugTap(log)
        self.debug_fast_until = 0.0              # 暫時關快戰到幾點（0＝沒在關）；主迴圈 tick_debug 送、到時間開回來
        self.debug_fast_pending = False          # API 要求關快戰，等主迴圈送
        # 任務狀態另外記一份完整的：history 會被砍，常駐跑幾小時後解任務重播不到「第 2 步進行中」，
        # 把空的第 1 步當可接重接 → 多拿一隻金豬（2026-10-25 第二組劇本，LESSONS #41 換條路再犯，#82）
        self.task_status: Dict[str, Dict[int, int]] = {}
        self.petinfo: Dict[str, Dict[int, dict]] = {}   # 寵物欄（petId、level、status）；角色登入後才有
        self.train_pet: Optional[Tuple[int, int]] = None   # 練寵模式：(petId, 目標等級)
        self.orig_fight: Dict[str, Tuple[int, int]] = {}   # 練寵中的角色 → 原本的（戰寵, 騎寵）唯一編號
        self.train_uid: Dict[str, int] = {}  # 練寵中的角色 → 正在練的那隻唯一編號（同種多隻時判斷到級／換圖看牠，LESSONS #184）
        self.train_skip: set = set()     # 練寵時不換戰寵的角色（jobs petgoal：這輪沒有寵物可練的人照常用原本戰寵）
        self.wg: Dict[str, dict] = {}            # 角色 → 外掛一般設定（wgNomalSetting）
        self.battle: Dict[str, dict] = {}        # 角色 → 外掛戰鬥設定（battleSetting；首次動作等）
        self.catch: Dict[str, dict] = {}         # 角色 → 外掛抓寵設定（catchPetSetting；設定檔 raw 比對用）
        self.heal_first = False                  # 開始前先回村補血
        self.stay = False                        # 鎖在指定地圖：不退圖也不前進（--stay）
        self.keep_pets = False                   # 使用者指定的戰寵／騎寵：不自動換寵（--keep-pets）
        self.auto_spirit = False                 # 隊長依屬性去競技場買精靈金幣、60 級自動穿（--auto-spirit，容器版）
        self.last_spirit = 0.0                   # 上次自動排 spirit 的時間
        self.spirit_tries = 0                    # 自動排的 spirit 失敗幾次（成功不算；到 SPIRIT_TRIES_MAX 就不再排）
        self.spirit_job: Optional[str] = None    # 上一件自動排的 spirit 工作 id（看結果用）
        self.spirit_have = False                 # 已經買好、等 60 級（背包或個倉）：等級不到不再排
        self.point_plan = None                   # 外掛自訂加點目標（--point-plan）：{"lead": (體, 敏, 攻), "member": (...)}
        self.battle_heal: Optional[int] = None   # 外掛戰鬥中技能補血門檻 %（--battle-heal；None＝不動）
        self.waigua = None                       # 設定檔 [waigua]（botconfig.Waigua）；None＝沒用設定檔，照舊
        self.waigua_done = False                 # 這次登入已經照設定檔檢查過外掛設定
        self.waigua_aligned: set = set()         # 這次登入已經成功對齊外掛的角色（align_waigua 記；apply_waigua 不再重做）
        self.plan_sent: Dict[str, dict] = {}     # 角色 → 這次送成功的自訂加點 {type: value}（伺服器還沒推新值時比對用）
        self.info_until = 0.0                    # 印人物寵物到這個時間（查詢 API POST /infolog 打開；0＝關，預設關）
        self.info_next = 0.0                     # 下一次印人物寵物的時間
        self.no_retreat = False                  # 只擋退圖、前進照舊（--no-retreat，使用者 2026-10-23「不允許退回」）
        self.point_type: Dict[str, int] = {}     # 角色 → 自動加點方式（1 自訂、2 推薦；0／沒有＝沒設過）
        self.items: Dict[str, Dict[int, dict]] = {}   # 角色 → 背包＋身上裝備（Item_ItemInfo，itemIndex → 欄位）
        self.hide: Dict[str, bool] = {}          # 角色 → 外掛全部隱藏（RoleAttr.hideMode）
        self.fighting: Dict[str, bool] = {}      # 角色 → 是不是在戰鬥中（RoleAttr.status 1）
        self.hp: Dict[str, int] = {}             # 角色 → 人物血量（RoleAttr.hp；被打死送回記錄點是 1）
        self.hp_max: Dict[str, int] = {}         # 角色 → 人物最大血量（RoleAttr.hpMax）
        self.heals = 0                           # 連續補血次數（打贏或血量回到 50% 以上歸零）
        self.heal_times: List[float] = []        # 每次補血的時間（近 5 分鐘次數給 supervisor 判斷要不要退圖）
        self._monsters: Optional[Dict[int, str]] = None   # petId → 寵物名（data/configs/Monster.json nameTn）
        self._items: Optional[Dict[int, dict]] = None  # 道具表（data/configs/Item.json）
        self._rides: Optional[set] = None        # 不用學就能騎的 petId（data/configs/Ride.json needlearn false）
        self.pet_start: Optional[str] = None     # 練寵起點（使用者：人物等級高、寵物低，從海底開始）
        self.pet_jump = 0                        # 跳級：寵物差幾級就可以進下一張圖（使用者 11-10「練級要盡可能用跳級去練」）
        # 練功中經驗果加成沒了就自己吃（使用者 11-07「沒吃果子記得自己吃」、11-11「練寵都沒有吃果子」）：
        # 背包、倉庫有就吃，沒有就用石幣在商城石幣分頁買（fruit_buy）。出錯一次就整個關掉，不每圈重試
        self.auto_fruit = False
        self.fruit_buy = False
        self.fruit_next = 0.0
        self.fruit_off = False
        self._cfg = None
        self.advance_after: Dict[str, Tuple[int, float]] = {}  # 地點 → 退回後（練到幾級, 或到哪個時間）才能再去
        self.buffs: Dict[str, Dict[int, int]] = {}   # 角色 → {buffId: 到期 unix 秒}（RoleAttr.newRolebuffs）
        self._exp_buffs: Optional[Dict[int, str]] = None   # 有經驗加成的 RoleBuff → 名稱（RoleBuff.json expBuffInfo）
        self.signin_day: Optional[str] = None    # 最後一次「試過」每日簽到的本地日期（不論成敗，一天只試一次）
        # 隊長目前位置 (地圖, x, y)：伺服器推的 RoleAttr.nowPos，或我們自己走完的最後一格；換圖但沒給座標就清掉（不知道）
        self.leader_pos: Optional[Tuple[int, int, int]] = None
        self.leader_dir = 0                      # 隊長面向（RoleAttr.dir／走路最後一步；沒推過用 0，同 參考專案）
        # 每一隻自己的位置（同 leader_pos 的規則：nowPos、自己走完的最後一格，換圖沒給座標就 None）。
        # 隊長仍以 leader_pos 為準（pos_of）；多隻各自行動（herobf --roles）時其他人用這份
        self.positions: Dict[str, Optional[Tuple[int, int, int]]] = {}
        self.history_cut = 0                     # history 砍掉過幾筆（讀 history 的模組用「cut＋索引」當絕對位置）
        self.point_switched: set = set()         # 這次執行已試過改系統推薦加點的角色（每隻只試一次）
        self.attrs: Dict[str, dict] = {}         # 角色 → 收到過的 RoleAttr 全部欄位（合併；本機查詢 API /role 用）
        # 千年龜的殼：背包快滿原地存共倉、回村時存＋滿 10 兌換（使用者 2026-10-24；--no-shell-exchange 關）
        from .shell_exchange import ShellExchange
        self.shells = ShellExchange(self)
        # 工作佇列（jobqueue.Queue；main 依 --jobs-dir 設定，None＝不收工作）：主迴圈每輪取一件，用這個 Party 做
        self.jobq = None
        self.job: Optional[dict] = None          # 目前在做的工作 {id, name, since}
        self.hold: Optional[dict] = None         # 停在原地等人 {reason, place, since}：不監管、不回練功，只收工作
        # 劇本（scenario.Runner；main 依 --scenario 設定，None＝不跑劇本）：佇列空、沒有 hold 時每輪 tick 一次
        self.scenario = None
        # 做完一件工作時佇列裡還有下一件：先不回練功，記住要回去的練功地圖 {place}，最後一件做完（或佇列空了）才回
        self.carry: Optional[dict] = None
        self.last_autoclean = 0.0               # 上次因為背包快滿自動排 clean 的時間（間隔至少 AUTOCLEAN_EVERY）
        self.last_petrefresh = 0.0              # 上次因為戰寵／騎寵滿 140 自動排換寵的時間

    # ---- 連線 ----
    def _open(self, rid: int):
        # 5 條連線共用同一份設定（同一個 pcId）：遊戲視窗五開就是這樣。每個角色各產生 pcId 時，
        # 下一隻登入會把上一隻踢掉（System_KickConnect reason 1，2026-10-21 實機）。HTTP 帳號登入只做一次。
        if self._cfg is None:
            self._cfg = load(profile=self.profile)
            self._cfg.rid = rid                       # None＝還不知道角色：open_client 取這個帳號的第一個角色
            c, result = open_client(self._cfg, log=lambda m: None)
            self._cfg.game_host = self._cfg.game_host or result.game_ip
            if not self.party:
                roles = [(r["rid"], r.get("name") or r.get("roleName") or f"角色{r['rid']}")
                         for r in result.roles(self._cfg.node)]
                self.roster = list(roles)
                if self.picks:                        # 指定哪幾隻（herobf --roles 3,4,5）
                    try:
                        self.party = self._choose(roles)
                    except RuntimeError:
                        c.close()
                        raise
                    return c
                if self.pick:                         # 指定第幾隻人物（單人工作，使用者 10-31「帳號 3 角色 2」）
                    if not 1 <= self.pick <= len(roles):
                        c.close()
                        raise RuntimeError(f"第 {self.profile} 組帳號只有 {len(roles)} 隻人物，沒有第 {self.pick} 隻")
                    roles = roles[self.pick - 1:]
                self.party = roles[:self.size]
            return c
        self._cfg.rid = rid
        c, _ = open_client(self._cfg, use_http=False, log=lambda m: None)
        return c

    def _choose(self, roles: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
        """picks（1 起算）→ 那幾隻人物，照 picks 的順序；有一個超出範圍、或重複就停（什麼都不登）。"""
        bad = [i for i in self.picks if not 1 <= i <= len(roles)]
        if bad:
            raise RuntimeError(f"第 {self.profile} 組帳號只有 {len(roles)} 隻人物，沒有第 {'、'.join(map(str, bad))} 隻")
        if len(set(self.picks)) != len(self.picks):
            raise RuntimeError(f"人物編號重複：{self.picks}")
        return [roles[i - 1] for i in self.picks]

    def login_all(self) -> None:
        if not self.party:
            # 角色清單還不知道（非第 2 組）：先登入帳號的第一個角色，從 HTTP 登入結果取得全部角色
            c = self.open_fn(None)
            if not self.party:
                raise RuntimeError(f"第 {self.profile} 組帳號沒有角色")
            if getattr(c, "rid", None) is not None and all(r != c.rid for r, _ in self.party):
                c.close()                             # 帳號第一隻不在名單裡（pick ≥ 2）：它只是用來拿角色清單，登出換指定那隻
                c = self.open_fn(self.party[0][0])
            first = next(((r, n) for r, n in self.party if r == getattr(c, "rid", None)), self.party[0])
            self.party.remove(first)
            self.party.insert(0, first)               # 第一個登入的當隊長
            self._register(first[1], c)
        for rid, name in self.party:
            if name in self.clients:
                continue
            self._register(name, self.open_fn(rid))

    def drop_connections(self) -> None:
        """伺服器已經把全帳號登出（離線掛機 NewRole_RoleAfk 成功）之後用：**什麼都不送**、只關掉我方連線，
        清掉跟這次連線綁在一起的狀態（背包、寵物、地圖、隊伍、快戰、踢線旗標…），下次 login_all 從登入推送重建。
        不能用 close()：它會送 Role_UnLoopBattle、Role_Exit（離線掛機靠遇敵在打，關掉就白掛了）。
        HTTP 帳號登入結果也清掉（_cfg），重登時重新拿（等了幾分鐘，舊的 token 不一定還能用）。"""
        for c in list(self.clients.values()):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            self.clients = {}
            self._cfg = None
            for d in (self.items, self.petinfo, self.fast, self.fighting, self.hp, self.hp_max, self.attrs,
                      self.positions, self.escape_wait, self.rounds_seen, self.seq_round):
                d.clear()
            self.pending = []
            self.escape_seen = set()
            self.leader_pos = None
            self.s.member_maps = {}
            self.s.team = []
            self.s.leader_map = None
            self.s.loop_on = False
            self.s.kicked = False

    def _watch_team_sends(self, c) -> None:
        """這條連線送出「離隊／解散」就清隊伍快取：解散後伺服器推的 Team_TeamInfo 會被當局部更新略過，
        快取一直是舊的 5 人 → form_team 以為不用重組、傳送只有隊長走（#77、#83、2026-10-25 catch --who 後練寵傳送失敗 #86）。
        包在連線的 send 上，不管哪個工作送都清，新工作也不會漏。"""
        orig = getattr(c, "send", None)
        if orig is None or getattr(orig, "_team_watch", False):
            return

        def send(name, data=None, *a, **kw):
            if name in ("Team_TeamLeave", "Team_TeamDismiss"):
                with self.lock:
                    self.s.team = []
            return orig(name, data, *a, **kw)
        send._team_watch = True
        c.send = send

    def _register(self, name: str, c) -> None:
        self._watch_team_sends(c)
        self._watch_debug_sends(name, c)
        self.clients[name] = c
        self.petinfo.setdefault(name, {})
        self.items.setdefault(name, {})
        on = self._listener(name)
        c.listeners.append(on)
        # 登入過程中伺服器已推送的事件（地圖、等級、隊伍）在監聽器掛上前就進了佇列，補處理一次（2026-10-21 漏掉隊伍資料）
        if hasattr(c, "drain"):
            c.drain(on_event=on)
        self.after_login(name)
        with self.lock:
            if self.s.kicked:
                raise RuntimeError(f"{name} 登入時有角色被踢下線（System_KickConnect）")
        self.log(f"{name} 登入成功")

    def after_login(self, name: str) -> None:
        """照遊戲客戶端登入後的順序補送請求（2026-10-21 擷取 Role_RoleLogin 之後的 c2s）。"""
        c = self.clients[name]
        for req in POST_LOGIN_REQUESTS:
            c.request(req, {})
        for req in POST_LOGIN_SENDS:
            c.send(req, {})

    def _watch_debug_sends(self, name: str, c) -> None:
        """送出的封包也給 /debug/trace dir=send 看（沒開追蹤時只多一次判斷）。"""
        orig = getattr(c, "send", None)
        if orig is None or getattr(orig, "_debug_watch", False):
            return

        def send(proto, data=None, *a, **kw):
            self.debug.on_send(name, proto, data)
            return orig(proto, data, *a, **kw)
        send._debug_watch = True
        send._team_watch = getattr(orig, "_team_watch", False)
        c.send = send

    def _listener(self, name: str):
        def on(e):
            n = e.get("name")
            d = e.get("data") or {}
            self.debug.on_recv(name, e, bool(self.party) and name == self.party[0][1])
            with self.lock:
                # 留一份事件紀錄，給之後才掛上的模組（例如無畫面解任務）重播登入時的推送（任務、背包、已完成任務）
                self.history.append((name, e))
                if len(self.history) > 20000:
                    del self.history[:5000]
                    self.history_cut += 5000
                if n in ("Battle_BattleCmdBegin", "Battle_BattleEnd"):
                    # 伺服器進下一回合／結束了（只送 PlayOver 的回合靠它判斷要不要補 EscapeOver；不在下面 elif 鏈裡，
                    # 免得搶走隊長 BattleEnd 的勝負統計）
                    self.rounds_seen[name] = self.rounds_seen.get(name, 0) + 1
                    if name == self.party[0][1]:
                        self._time_round(n == "Battle_BattleEnd")
                if n == "Task_TaskInfo":
                    for t in d.get("taskInfo") or []:
                        if t.get("taskId") is not None:
                            self.task_status.setdefault(name, {})[t["taskId"]] = t.get("taskStatus")
                if n == "Role_RoleInfo":
                    a = d.get("roleAttr") if isinstance(d.get("roleAttr"), dict) else d
                    if a.get("rid") not in (None, self.clients[name].rid if name in self.clients else None):
                        return
                    self.attrs.setdefault(name, {}).update(a)
                    here, m = a.get("nowPos"), a["map"] if "map" in a else self.s.member_maps.get(name)
                    if isinstance(here, dict) and m is not None:
                        self.positions[name] = (m, here.get("x", 0), here.get("y", 0))
                    elif "map" in a and a["map"] != self.s.member_maps.get(name):
                        self.positions[name] = None              # 這隻換圖了但沒給座標 → 位置不知道
                    if "map" in a:
                        self.s.member_maps[name] = a["map"]
                        if name == self.party[0][1]:
                            if a["map"] != self.s.leader_map and not isinstance(a.get("nowPos"), dict):
                                self.leader_pos = None           # 換圖了但沒給座標 → 位置不知道
                            self.s.leader_map = a["map"]
                    if name == self.party[0][1]:
                        p = a.get("nowPos")
                        if isinstance(p, dict) and self.s.leader_map is not None:
                            self.leader_pos = (self.s.leader_map, p.get("x", 0), p.get("y", 0))
                        if "dir" in a:
                            self.leader_dir = a["dir"]
                    if "level" in a:
                        self.s.levels[name] = a["level"]
                    if name == self.party[0][1] and "autoEncountFlag" in a:
                        self.s.loop_on = bool(a["autoEncountFlag"])
                    if "fastBattleFlg" in a:
                        self.fast[name] = bool(a["fastBattleFlg"])
                    if "autoSetPointType" in a:
                        self.point_type[name] = a["autoSetPointType"]
                    if "autoPointDiyList" in a:
                        self.plan_sent.pop(name, None)          # 伺服器推了真正的值：之後照它比，不再看當初送的
                    if "hideMode" in a:
                        self.hide[name] = bool(a["hideMode"])
                    if "status" in a:
                        self.fighting[name] = a["status"] == 1      # 1＝戰鬥中（pause 要等它變 0）
                    if "hp" in a:
                        self.hp[name] = a["hp"]
                    if "hpMax" in a:
                        self.hp_max[name] = a["hpMax"]
                    if "trans" in a:
                        self.trans[name] = a["trans"]
                    if "finishTasks" in a:
                        self.finished[name] = list(a["finishTasks"] or [])
                    for k in ("gold", "lockDenar", "stone", "denar"):    # denar＝鑽石（花石幣時核對不能變）
                        if k in a:
                            self.money.setdefault(name, {})[k] = a[k]
                    if "newRolebuffs" in a:
                        nb = a["newRolebuffs"] or {}
                        self.buffs[name] = {b.get("buffId"): b.get("buffOverTime") or 0
                                            for b in (nb.values() if isinstance(nb, dict) else nb) if b.get("buffId")}
                    if isinstance(a.get("wgNomalSetting"), dict):
                        self.wg.setdefault(name, {}).update(a["wgNomalSetting"])
                    if isinstance(a.get("battleSetting"), dict):
                        self.battle.setdefault(name, {}).update(a["battleSetting"])
                    if isinstance(a.get("catchPetSetting"), dict):
                        self.catch.setdefault(name, {}).update(a["catchPetSetting"])
                elif n == "Item_ItemInfo":
                    for it in d.get("itemInfo") or []:
                        self.items.setdefault(name, {}).setdefault(it["itemIndex"], {}).update(it)
                elif n == "Pet_PetInfo":
                    for p in d.get("petInfo") or []:
                        x = self.petinfo[name].setdefault(p["petIndex"], {})
                        if p.get("uniqueIndex") and x.get("uniqueIndex") and p["uniqueIndex"] != x["uniqueIndex"]:
                            # 這格換成另一隻寵物：舊那隻的欄位全清掉。沒改過名的寵物伺服器不帶 name，
                            # 舊版沿用前一隻改好的「1000」→ petgoal 把剛領出／剛抓的 1 級當成已達標存倉（11-06，LESSONS #177）
                            x.clear()
                        x.update({k: p[k] for k in ("petId", "level", "status", "uniqueIndex", "hp", "hpMax", "name",
                                                    # 四個成長欄位；瑪蕾菲雅（女寵）這四欄是餵靈石進度 0～50
                                                    # （參考專案 ops/marketpet：IL PetLogic.isMM；jobs/lingshi 查，使用者 10-31）
                                                    "vitArg", "atkArg", "defArg", "dexArg", "skills", "score",
                                                    "bind", "petLock",      # jobs/trade：綁定／上鎖的不交易
                                                    *PET_DETAIL_KEYS)       # pets --mode info
                                  if k in p})
                elif n == "Battle_BattleCmdSeq":
                    # 照遊戲客戶端：收到出招序列要回 PlayOver＋EscapeOver，否則一般戰鬥會卡住
                    # （2026-10-22 做任務後快速戰鬥被關掉，無畫面版沒回 → 戰鬥停住）。交給主迴圈送，不在收封包的執行緒送。
                    if name not in self.pending:
                        self.pending.append(name)
                    self.seq_round[name] = self.rounds_seen.get(name, 0)
                    if any((s.get("skillInfo") or {}).get("skillType") == 9 for s in d.get("battleSeq") or []):
                        self.escape_seen.add(name)        # 這回合有人逃跑：照樣送 EscapeOver
                elif n == "Role_LoopBattleFlg" and name == self.party[0][1]:
                    self.s.loop_on = bool(d.get("isLoop"))
                elif n == "Team_TeamInfo" and name == self.party[0][1]:
                    team = [m.get("roleAttr", {}).get("name") for m in d.get("teamMembers") or []]
                    # 只有血量等局部更新的 Team_TeamInfo 沒有名字（2026-10-22 補血後）→ 略過，否則會誤判少人而一直重組隊
                    if all(team) and (len(team) > 1 or not self.s.team):
                        self.s.team = team
                elif n == "Battle_BattleEnd" and name == self.party[0][1]:
                    self.battles.append((time.time(), d.get("win")))
                    self.last_battle = self.battles[-1]
                    if d.get("win") == 1:
                        self.heals = 0                    # 打贏了 → 連續補血次數歸零（supervisor 3 次就停）
                elif n == "System_KickConnect":
                    self.s.kicked = True
        return on

    def leader(self):
        return self.clients[self.party[0][1]]

    # ---- 動作（都等封包確認）----
    def service(self) -> int:
        """送出累積的戰鬥確認（PlayOver＋EscapeOver）。回傳處理了幾個角色。
        skip_escape_over 開著、非快速戰鬥、這回合沒人逃跑 → 只送 PlayOver；ESCAPE_FALLBACK 秒內伺服器沒進下一回合
        → 補送 EscapeOver、關掉 skip（退回原做法，記日誌）。
        battle_refresh 開著 → 先看要不要送戰鬥中刷新（_service_refresh）；關著時下面跟原本完全一樣。"""
        if self.battle_refresh:
            return self._service_refresh()
        return self._service_plain()

    def _service_plain(self) -> int:
        with self.lock:
            todo, self.pending = self.pending, []
        for name in todo:
            c = self.clients[name]
            c.send("Battle_BattlePlayOver", {})
            with self.lock:
                skip = self.skip_escape_over and self.fast.get(name) is not True and name not in self.escape_seen
                self.escape_seen.discard(name)
                if skip:
                    self.escape_wait[name] = (time.time(), self.rounds_seen.get(name, 0))
            if not skip:
                c.send("Battle_BattleEscapeOver", {})
        now = time.time()
        with self.lock:
            late = [n for n, (t, k) in self.escape_wait.items()
                    if self.rounds_seen.get(n, 0) == k and now - t >= self.ESCAPE_FALLBACK]
            for n in list(self.escape_wait):
                if n in late or self.rounds_seen.get(n, 0) != self.escape_wait[n][1]:
                    del self.escape_wait[n]
            if late:
                self.skip_escape_over = False
        for name in late:
            self.clients[name].send("Battle_BattleEscapeOver", {})
            self.log(f"{name} 只送 PlayOver 後 {self.ESCAPE_FALLBACK:.0f} 秒伺服器沒進下一回合 → 補送 EscapeOver，退回原做法")
        return len(todo)

    ESCAPE_FALLBACK = 5.0

    # ---- 戰鬥中刷新（照 參考專案 tools/headless/ops/battle.py refreshing()／refresh_plan；**未實機**）----
    # 官方畫面版每回合按「切換角色 → 刷新」送的那串（參考專案 用 p1 官方錄檔整理）：
    #   開面板 Role_SwitchRoleGetInfo{getRid 0, getAll true}（外層不帶 head）
    #   → 戰鬥中每個登入角色 PlayOver{}＋EscapeOver{}（各自連線、帶 head）→ Battle_BattleAllInfo（帶 head）
    #   → 回主畫面：POST_LOGIN_REQUESTS 去掉 AllInfo ＋ POST_LOGIN_SENDS。
    # 跟 參考專案 不同的地方：PlayOver＋EscapeOver 只送一輪（參考專案 照錄檔兩輪＋SwitchRole 不帶 head 的 PlayOver）、
    # 不送 isBattle 後的 SynRolePos、**不連送**（每筆照 GameClient 0.3～0.8 秒間隔，規範 3；參考專案 的連送例外我們沒有）。
    # 時機：隊長收到 CmdSeq（這回合指令到齊、開演）→ 送一次；送完 REFRESH_GAP 秒還沒收到下一回合 CmdBegin／BattleEnd
    # → 再送，同一回合最多重送 REFRESH_REPEAT_MAX 次（之後不再送，等伺服器）。快速戰鬥開著的角色不送刷新。
    # 任何一筆出錯（逾時、斷線）→ 關掉刷新、丟出例外停下來，不重試（規範 2）。
    REFRESH_GAP = 3.0
    REFRESH_REPEAT_MAX = 20

    def _service_refresh(self) -> int:
        lead = self.party[0][1]
        now = time.time()
        with self.lock:
            rs = self.rounds_seen.get(lead, 0)
            st = self.refresh_round
            if st is not None and st["round"] != rs:
                st = self.refresh_round = None             # 伺服器進下一回合／這場結束了
            fresh = (lead in self.pending and lead in self.clients and self.fast.get(lead) is not True
                     and self.seq_round.get(lead) == rs and lead not in self.escape_seen)
            again = (not fresh and st is not None and now - st["last"] >= self.REFRESH_GAP
                     and st["repeat"] < self.REFRESH_REPEAT_MAX)
            fighters: List[str] = []
            if fresh or again:
                fighters = [n for _, n in self.party if n in self.clients and self.fast.get(n) is not True
                            and (n == lead or n in self.pending or self.fighting.get(n))]
                self.pending = [n for n in self.pending if n not in fighters]   # 刷新串裡會送它們的 PlayOver
                for n in fighters:
                    self.escape_seen.discard(n)
            capped = (not fresh and st is not None and st["repeat"] >= self.REFRESH_REPEAT_MAX
                      and not st.get("capped"))
            if capped:
                st["capped"] = True
        if capped:
            self.log(f"戰鬥刷新：同一回合已重送 {self.REFRESH_REPEAT_MAX} 次還沒進下一回合，不再送，等伺服器")
        done = self._service_plain()
        if not (fresh or again):
            return done
        self._send_refresh(lead, fighters)
        with self.lock:
            self.refresh_count += 1
            if fresh:
                self.refresh_round = {"round": rs, "last": time.time(), "repeat": 0}
            else:
                st["last"] = time.time()                  # 送完才開始算下一次的 3 秒
                st["repeat"] += 1
        return done + len(fighters)

    def _send_refresh(self, lead: str, fighters: List[str]) -> None:
        """照順序送一串刷新；每筆都經 GameClient（0.3～0.8 秒間隔、request 等到回應才送下一筆）。出錯就關刷新、丟例外。"""
        c = self.clients[lead]
        try:
            c.request("Role_SwitchRoleGetInfo", {"getRid": 0, "getAll": True})     # 開切換角色面板
            for n in fighters:
                self.clients[n].send("Battle_BattlePlayOver", {})
                self.clients[n].send("Battle_BattleEscapeOver", {})
            c.request("Battle_BattleAllInfo", {})
            for req in POST_LOGIN_REQUESTS:
                if req != "Battle_BattleAllInfo":
                    c.request(req, {})
            for req in POST_LOGIN_SENDS:
                c.send(req, {})
        except Exception as e:
            with self.lock:
                self.battle_refresh = False
                self.refresh_round = None
            self.log(f"戰鬥刷新出錯（{e}），關掉刷新、停下來，不重送")
            raise RuntimeError(f"戰鬥刷新出錯：{e}") from e

    # ---- 每回合計時（隊長連線）----
    ROUND_KEEP = 20000                           # 最多留幾筆回合／場次紀錄（常駐跑很久不無限長）

    def _time_round(self, end: bool) -> None:
        """隊長收到 CmdBegin（end False）或 BattleEnd（end True）時呼叫（已握鎖）。"""
        now = time.time()
        if self._round_t0 is not None:
            self.round_times.append(now - self._round_t0)
            if len(self.round_times) > self.ROUND_KEEP:
                del self.round_times[:len(self.round_times) - self.ROUND_KEEP]
        if end:
            if self._battle_t0 is not None:
                self.battle_rounds.append((self._battle_n, now - self._battle_t0))
                if len(self.battle_rounds) > self.ROUND_KEEP:
                    del self.battle_rounds[:len(self.battle_rounds) - self.ROUND_KEEP]
            self._round_t0 = self._battle_t0 = None
            self._battle_n = 0
        else:
            if self._battle_t0 is None:
                self._battle_t0 = now
            self._round_t0 = now
            self._battle_n += 1

    def reset_round_stats(self) -> None:
        with self.lock:
            self.round_times, self.battle_rounds = [], []
            self._round_t0 = self._battle_t0 = None
            self._battle_n = 0

    def round_stats(self) -> dict:
        """每回合花多久：battles 場數（有 CmdBegin 的才算；快速戰鬥沒有回合不算）、rounds 回合數、
        avg／min／max 每回合秒數、battle_avg 每場平均秒數、rounds_per_battle 每場平均回合數；沒資料的是 None。"""
        with self.lock:
            r, b = list(self.round_times), list(self.battle_rounds)
        return {"battles": len(b), "rounds": len(r),
                "avg": sum(r) / len(r) if r else None, "min": min(r) if r else None, "max": max(r) if r else None,
                "battle_avg": sum(s for _, s in b) / len(b) if b else None,
                "rounds_per_battle": sum(k for k, _ in b) / len(b) if b else None}

    def round_summary(self) -> str:
        s = self.round_stats()
        if not s["rounds"]:
            return "還沒有回合計時"
        return (f"每回合平均 {s['avg']:.1f} 秒（{s['rounds']} 回合、最短 {s['min']:.1f}／最長 {s['max']:.1f}）"
                + (f"，每場平均 {s['battle_avg']:.1f} 秒" if s["battles"] else ""))

    def ensure_fast(self, timeout: float = 6.0) -> List[str]:
        """5 隻都要開快速戰鬥（`WaiGua_FastBattleHandle{flg}`）；**只能在練功地圖開**（使用者；
        例外：四大洞窟靈石 1 樓，jobs lingshi --afk，使用者 2026-10-31 同意），所以要在傳送之後、
        開原地遇敵之前呼叫。做任務的地圖會把它關掉。回傳沒確認到的角色（空＝5 隻都開了）。
        設定檔 [waigua] fast_battle = false：不動。/debug/fast 暫時關快戰期間也不動。"""
        if self.debug_fast_until:
            return []
        if self.waigua is not None and not self.waigua.fast_battle:
            return []
        with self.lock:
            off = [n for _, n in self.party if self.fast.get(n) is not True]
        for name in off:
            self.clients[name].request("WaiGua_FastBattleHandle", {"flg": True})
            self.log(f"{name} 快速戰鬥原本關著，已打開")
        self.wait(lambda: all(self.fast.get(n) is True for _, n in self.party), timeout)
        with self.lock:
            return [n for _, n in self.party if self.fast.get(n) is not True]

    def nap(self, seconds: float) -> None:
        """等待時也持續回戰鬥確認。"""
        end = time.time() + seconds
        while True:
            self.service()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.3, left))

    def wait(self, cond, timeout: float) -> bool:
        """握著 self.lock 呼叫 cond()；鎖是 RLock，所以 cond 裡呼叫 bag()／pet_index() 等會拿鎖的方法也不會死結。"""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if cond():
                    return True
            self.nap(0.3)
        return False

    def set_loop(self, on: bool) -> bool:
        if on:
            # 照官方外掛「原地遇敵」：先 NewRole_SynRolePos{目前地圖、座標、面向}（不帶 head），再 Role_LoopBattle
            # （時光官方錄檔、參考專案-learnings §1.8）。座標不知道就只送 LoopBattle。
            with self.lock:
                pos, d, here = self.leader_pos, self.leader_dir, self.s.leader_map
            if pos is not None and pos[0] == here:
                self.leader().send("NewRole_SynRolePos", {"mapId": pos[0], "x": pos[1], "y": pos[2], "dir": d})
            else:
                self.log(f"開遇敵：不知道隊長座標（地圖 {here}、已知 {pos}），只送 Role_LoopBattle")
        self.leader().send("Role_LoopBattle" if on else "Role_UnLoopBattle", {})
        return self.wait(lambda: self.s.loop_on == on, 10)

    def pause(self, timeout: float = 40.0) -> bool:
        """關遇敵並**等到真的不在戰鬥中**才回來。

        舊版固定 `nap(6)`（快速戰鬥一場約 5 秒）就當打完了，但偶爾會沒打完 →
        接著送 `Transfer_TransferTo` 被伺服器拒（`isOK:false`）→ 補血失敗 → 整個練功停掉。
        2026-10-23 第三組在波拉島因此停了兩次（其中一次沒人發現，空轉 50 分鐘）。
        改成看 `Role_RoleInfo.status`（1＝戰鬥中）：還有人在打就繼續等，等不到才放棄。
        """
        if not self.set_loop(False):
            return False
        self.nap(1)
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                busy = [n for n, f in self.fighting.items() if f]
            if not busy:
                self.nap(1)                     # 再緩一拍，讓結算封包走完
                return True
            self.nap(1)
        with self.lock:
            busy = [n for n, f in self.fighting.items() if f]
        self.log(f"關遇敵後等 {timeout:.0f} 秒還在戰鬥中：{busy}")
        return False

    def dismiss_team(self) -> None:
        """全隊離隊＋隊長解散，並清掉隊伍快取。
        解散後推來的 Team_TeamInfo 只剩 1 人或沒名字，上面為了局部更新會略過 → s.team 還是舊的 5 人
        → form_team 以為「不用重組」→ 隊伍其實散了（2026-10-24 大亂鬥：每次報名被拒前都是這樣，LESSONS #77）。"""
        for _, n in self.party:
            self.clients[n].send("Team_TeamLeave", {})
        self.leader().send("Team_TeamDismiss", {})
        self.nap(1.5)
        with self.lock:
            self.s.team = []

    def form_team(self) -> bool:
        with self.lock:
            if len(self.s.team) == len(self.party):
                spots = {self.s.member_maps.get(n) for _, n in self.party}
                if None in spots or len(spots) == 1:
                    self.log("隊伍已經是 5 人（伺服器保留原隊伍），不用重組")
                    return True
                # 快取是 5 人、但隊員分散在不同地圖：隊伍其實已經散了（伺服器解散沒推 Team_TeamInfo、
                # 換隊長還原後解散……）。11-06 轉生補進度後隊長一個人跳去交步驟、別人留在原地 → 清快取真的重組
                self.log(f"隊伍快取是 5 人，但隊員不在同一張地圖 "
                         f"{ {n: self.s.member_maps.get(n) for _, n in self.party} }：隊伍已經散了，重新組隊")
                self.s.team = []
        with self.lock:
            maps = {n: self.s.member_maps.get(n) for _, n in self.party}
        quick_ok = {1000, 1005}                  # 實測可以一鍵組隊：薩姆吉爾村、村裡醫院（大亂鬥在醫院組）
        if None not in maps.values() and not (len(set(maps.values())) == 1 and set(maps.values()) <= quick_ok):
            # 不在同一張圖、或同一張圖但不是薩村（練功地圖都不支援一鍵組隊，15:55 實測全在地雷區還是 0 人）
            # 快速組隊要同一張地圖：2026-10-25 恐龍博士交寵物後伺服器解散隊伍，回練功只有隊長傳走，
            # 其他 4 隻留在研究所 11006 → 重開常駐「組隊：隊伍 0 人」停止（LESSONS #84）。
            # 沒有隊伍時每隻可以自己傳送 → 全部各自傳回薩姆吉爾村（point 1）再組；任一隻傳不過去就停
            # 集合在薩姆吉爾村：練功地圖不支援一鍵組隊（15:51 實測地雷區 → 提示 33231「當前地圖不支持一鍵組隊」）。
            # 隊員先傳、隊長最後傳：隊長剛登入馬上傳送會被拒（15:45 實測 isOK false），多等隊員那幾趟
            self.log(f"組隊前隊員不在同一張地圖 {maps}：全部離隊、各自傳回薩姆吉爾村集合再組隊")
            # 先關遇敵、等沒人在戰鬥：斷線前開著原地遇敵的角色，重登後伺服器照樣幫他遇敵，戰鬥中傳送一定被拒
            # （10-31 第三組：前晚 23:48 斷線時角色91在地雷區開著遇敵，07:46／07:48 兩次傳回村都 isOK false）
            if not self.pause():
                self.log("組隊：還有人在戰鬥中、停不了遇敵，不傳送")
                return False
            # 先全部離隊：隊長離隊後其他 4 隻在伺服器上還是一隊（新隊長是下一隻），隊員不能自己傳送
            # （16:30 實測角色109 isOK false；提示 30746 隊員不能傳送）
            self.dismiss_team()
            for _, n in self.party[1:] + self.party[:1]:
                if n == self.party[0][1] and maps.get(n) != 1000:
                    # 隊長剛登入馬上傳送會被拒（15:45 實測）：原本靠隊員先傳拉開時間，隊員都已經在村裡時就沒間隔了
                    self.nap(self.LEADER_TRANSFER_WAIT)
                if maps.get(n) != 1000 and not self.transfer(n, 1, 1000):
                    self.log(f"組隊：{n} 傳不回薩姆吉爾村")
                    return False
        rids = "-".join(str(self.clients[n].rid) for _, n in self.party)
        # 伺服器不回這個請求的 response，結果以推送的 Team_TeamInfo 為準（2026-10-21 實機：request 會逾時）
        self.leader().send("Role_NomalHandle", {"type": 12, "extra": 0, "str": rids})
        ok = self.wait(lambda: len(self.s.team) == len(self.party), 10)
        self.log(f"組隊：隊伍 {len(self.s.team)} 人")
        return ok

    LEADER_TRANSFER_WAIT = 8.0                   # 集合時隊長傳送前至少等幾秒（剛登入馬上傳會被拒）

    def transfer(self, name: str, point: int, mapid: int) -> bool:
        r = self.clients[name].request("Transfer_TransferTo", {"point": point})
        if not r.get("isOK"):
            self.log(f"{name} 傳送 {point} 被拒：{r}")
            return False
        ok = self.wait(lambda: self.s.member_maps.get(name) == mapid, 15)
        if ok and point == 1 and mapid == 1000:
            self.set_pos(name, (1000,) + HEAL_PATH_VILLAGE[0])   # 傳回薩村固定落在 (92,99)；伺服器不一定推座標
        return ok

    def teleport_team(self, place: str) -> bool:
        mapid = NAME_TO_MAP[place]
        # 已經全隊在那張圖就不送：剛登出又馬上登入時，傳送到同一張圖會被拒（2026-10-24 05:58、06:08 實測兩次 isOK false）
        if all(self.s.member_maps.get(n) == mapid for _, n in self.party):
            return True
        arrived = lambda: all(self.s.member_maps.get(n) == mapid for _, n in self.party)  # noqa: E731
        if not self.transfer(self.party[0][1], NAME_TO_POINT[place], mapid):
            return False
        if self.wait(arrived, 15):
            return True
        # 隊長到了、有隊員沒跟上：隊伍多半已被伺服器解散，快取還是 5 人（11-06 兩組轉生後 after-trans
        # 「傳送到 柯奧龜洞Lv10 失敗」，日誌只有「隊伍已經是 5 人，不用重組」）→ 清快取真的重組、再傳一次
        with self.lock:
            behind = {n: self.s.member_maps.get(n) for _, n in self.party if self.s.member_maps.get(n) != mapid}
            self.s.team = []
        self.log(f"傳送到 {place}：隊員沒跟上 {behind}，重新組隊再傳一次")
        if not self.form_team():
            return False
        if arrived():
            return True
        return self.transfer(self.party[0][1], NAME_TO_POINT[place], mapid) and self.wait(arrived, 15)

    def stop_requested(self) -> bool:
        """有停止檔就結束（之後 close() 會送 Role_Exit 登出）。強制砍行程不會跑登出，要停一律用停止檔。"""
        if self.stop_file and os.path.exists(self.stop_file):
            os.remove(self.stop_file)
            return True
        return False

    def hold_advance(self, act: str, arg: Optional[str], s: State) -> bool:
        """退回後要再多 RETRY_LEVELS 級、或過了 RETRY_SECONDS 才回去，避免等級還夠就又跳過去、輸了又退（來回打轉）。
        回傳 True＝這次先不前進。"""
        low = min(s.levels.values()) if s.levels else 0
        if act == "retreat":
            here = next(n for n, m, _ in ROUTE if m == s.leader_map)
            self.advance_after[here] = (low + RETRY_LEVELS, time.time() + RETRY_SECONDS)
            return False
        if act != "advance" or arg not in self.advance_after:
            return False
        level, until = self.advance_after[arg]
        return low < level and time.time() < until

    # ---- 監管迴圈 ----
    def state(self) -> State:
        with self.lock:
            now = time.time()
            recent = [w for t, w in self.battles if now - t <= 300]
            s = State(leader_map=self.s.leader_map, member_maps=dict(self.s.member_maps), levels=dict(self.s.levels),
                      team=list(self.s.team), loop_on=self.s.loop_on, loop_off_by_us=True,
                      wins=recent.count(1), losses=recent.count(2), kicked=self.s.kicked, heals=self.heals,
                      heals_recent=sum(1 for t in self.heal_times if now - t <= 300), stay=self.stay,
                      no_retreat=self.no_retreat)
        s.hp_percent = self.hp_percent()
        return s

    # ---- 回村補血（使用者規則：死掉或被打飛要回村補血；doc/autoplay/flows/heal-samugil.md）----
    def walk(self, mapid: int, path: List[Tuple[int, int]], who: Optional[str] = None) -> None:
        """隊長（或 who 那一隻自己）沿固定路徑走（含起點），依路徑長度等足時間（4 格/秒，不瞬移）。
        who 不給＝隊長，行為跟以前一樣；給了就用那隻的連線送、只更新那隻的位置（herobf --roles 各自走）。"""
        from .pathing import walk_payload
        payload = walk_payload(mapid, path)
        name = who if who is not None else self.party[0][1]
        lead = name == self.party[0][1]
        (self.leader() if lead else self.clients[name]).send("Role_RoleWalk", payload)
        if len(path) >= 2:                       # 伺服器不推走路後的座標：用自己走的最後一格（開遇敵 SynRolePos 用）
            with self.lock:
                end = (mapid, path[-1][0], path[-1][1])
                self.positions[name] = end
                if lead:
                    self.leader_pos, self.leader_dir = end, payload["dir"]
        self.nap((len(path) - 1) / 4.0 + 0.5)

    def pos_of(self, name: str) -> Optional[Tuple[int, int, int]]:
        """這隻現在的位置 (地圖, x, y)；隊長照舊看 leader_pos（別的模組會直接改它），其他人看 positions。"""
        with self.lock:
            if self.party and name == self.party[0][1]:
                return self.leader_pos
            return self.positions.get(name)

    def set_pos(self, name: str, pos: Optional[Tuple[int, int, int]]) -> None:
        """自己知道落點時補記（例：踩傳送點進醫院、伺服器沒推座標）。隊長同時改 leader_pos。"""
        with self.lock:
            self.positions[name] = pos
            if self.party and name == self.party[0][1]:
                self.leader_pos = pos

    def heal_at_samugil(self) -> bool:
        """傳到薩姆吉爾村 → 走到醫院門口踩傳送 → 走到小護士旁 → 恢復氣血（全隊人物＋寵物回滿）。
        醫院裡 A* 算不出路，路徑用 2026-10-21 遊戲實際走的格子。"""
        lead = self.party[0][1]
        with self.lock:
            maps = {self.s.member_maps.get(n) for _, n in self.party}
            pos = self.leader_pos
        if maps == {1000} and pos and pos[0] == 1000:
            # 全隊已經在薩村（例：任務陣亡後各自傳回村重新組隊）：同一張圖傳送會被拒，從隊長現在的位置直接走到醫院門口
            from .pathing import astar, load_grid
            path = astar(load_grid(1000), (pos[1], pos[2]), HEAL_PATH_VILLAGE[-1]) or []
            if not path:
                self.log(f"補血：人在薩村 {pos[1:]}，算不出到醫院的路"); return False
            self.walk(1000, path)
            return self._heal_from_hospital_door(lead)
        r = self.leader().request("Transfer_TransferTo", {"point": 1})
        if not (r or {}).get("isOK"):
            # 被拒等一下重送一次：11-05 16:00（台灣 00:00 每日重置）第一次傳送被拒，舊版直接停掉整組、等容器重開
            self.wait(lambda: not any(self.fighting.values()), TRANSFER_RETRY_WAIT)
            self.nap(TRANSFER_RETRY_NAP)
            self.log(f"補血：傳到薩姆吉爾村被拒，等一下重送一次：{r}")
            r = self.leader().request("Transfer_TransferTo", {"point": 1})
        if not (r or {}).get("isOK") or not self.wait(lambda: all(self.s.member_maps.get(n) == 1000 for _, n in self.party), 15):
            self.log(f"補血：傳到薩姆吉爾村失敗 {r}"); return False
        self.walk(1000, HEAL_PATH_VILLAGE)
        return self._heal_from_hospital_door(lead)

    def _heal_from_hospital_door(self, lead: str) -> bool:
        """（隊長已在薩村醫院門口）踩傳送進醫院 → 走到小護士旁 → 恢復氣血。"""
        r = self.leader().request("Role_RoleWarp", {"map": 1000, "x": 80, "y": 66})
        if not self.wait(lambda: self.s.member_maps.get(lead) == 1005, 8):
            self.log(f"補血：進不了醫院 {r}"); return False
        self.walk(1005, HEAL_PATH_HOSPITAL)
        self.leader().send("Npc_NpcInteract", {"npcId": 2720, "npcArg": {"scriptId": 300002, "taskId": 0, "isPetNpc": False,
                                                                         "npcExtraType": 0, "npcExtraId": 0}})
        ok = self.wait(lambda: all(p.get("hp", 0) > 1 for _, n in self.party for p in self.petinfo[n].values()
                                   if p.get("petId")), 8)
        if not ok:
            ok = self.heal_stragglers()
        self.log("補血：全隊回滿" if ok else "補血：沒看到寵物血量回來")
        return ok

    def heal_stragglers(self) -> bool:
        """沒補到的人各自走到小護士面前再互動一次。

        只有隊長走路、只有隊長跟護士互動：被打輸送回記錄點時隊員會散在村子和醫院兩邊，
        沒走到護士面前的那幾隻補不到（primaljason-home 2026-10-24 大亂鬥實測）。
        """
        from .pathing import walk_payload
        left = self.wounded()
        if not left:
            return True
        self.log(f"補血：還沒補到的 {left}，各自走到護士面前")
        for name in left:
            c = self.clients.get(name)
            if c is None:
                continue
            if self.s.member_maps.get(name) != 1005:          # 還在村子 → 自己走到門口踩傳送
                c.send("Role_RoleWalk", walk_payload(1000, HEAL_PATH_VILLAGE))
                self.nap(len(HEAL_PATH_VILLAGE) / 4.0 + 0.5)
                c.request("Role_RoleWarp", {"map": 1000, "x": 80, "y": 66})
                self.nap(1.5)
            c.send("Role_RoleWalk", walk_payload(1005, HEAL_PATH_HOSPITAL))
            self.nap(len(HEAL_PATH_HOSPITAL) / 4.0 + 0.5)
            c.send("Npc_NpcInteract", {"npcId": 2720, "npcArg": {"scriptId": 300002, "taskId": 0, "isPetNpc": False,
                                                                 "npcExtraType": 0, "npcExtraId": 0}})
            self.nap(1.5)
        return not self.wounded()

    # ---- 開局基礎規範（使用者 2026-10-22，每組、每次開局都要）----
    # 1 吃經驗果 2 綁定道具存個人倉庫 3 戰寵騎寵一定要設 4 裝備全穿 5 外掛全部隱藏 6 練功地圖開快戰 7 自動配點
    def item_table(self) -> Dict[int, dict]:
        if self._items is None:
            import json as _json
            import os as _os
            from ..features.gamedb.tables import CONFIGS
            with open(_os.path.join(CONFIGS, "Item.json"), encoding="utf-8") as fp:
                self._items = {r["ID"]: r for r in _json.load(fp)}
        return self._items

    def rideable(self) -> set:
        """不用先學騎乘就能騎的 petId。needlearn 的寵物設成騎寵，伺服器回 isOK 卻不套用（ridePetIndex 維持 0，
        2026-10-22 第三組迪基格斯）。"""
        if self._rides is None:
            import json as _json
            import os as _os
            from ..features.gamedb.tables import CONFIGS
            with open(_os.path.join(CONFIGS, "Ride.json"), encoding="utf-8") as fp:
                self._rides = {r["petId"] for r in _json.load(fp) if not r.get("needlearn")}
        return self._rides

    def snapshot_events(self) -> List[tuple]:
        """目前狀態合成的事件（任務狀態、背包、寵物欄、地圖／座標／已完成任務），接在 history 後面給後掛的模組重播。
        history 會被砍（超過 20000 筆），只重播 history 會漏掉幾小時前推的狀態（LESSONS #82）；
        放在最後，重播完一定是現在的值。"""
        out: List[tuple] = []
        lead = self.party[0][1] if self.party else None
        with self.lock:
            for _, n in self.party:
                rid = getattr(self.clients.get(n), "rid", None)
                a = {"rid": rid}
                if n in self.s.member_maps:
                    a["map"] = self.s.member_maps[n]
                if n == lead and self.leader_pos:
                    a["map"], a["nowPos"] = self.leader_pos[0], {"x": self.leader_pos[1], "y": self.leader_pos[2]}
                if n in self.finished:
                    a["finishTasks"] = list(self.finished[n])
                out.append((n, {"name": "Role_RoleInfo", "data": {"roleAttr": a}}))
                if self.task_status.get(n):
                    out.append((n, {"name": "Task_TaskInfo", "data": {"taskInfo": [
                        {"taskId": k, "taskStatus": v} for k, v in self.task_status[n].items()]}}))
                if self.items.get(n):
                    out.append((n, {"name": "Item_ItemInfo", "data": {"itemInfo": [
                        dict(v, itemIndex=k) for k, v in self.items[n].items()]}}))
                if self.petinfo.get(n):
                    out.append((n, {"name": "Pet_PetInfo", "data": {"petInfo": [
                        dict(v, petIndex=k) for k, v in self.petinfo[n].items()]}}))
        return out

    def bag(self, name: str) -> List[dict]:
        with self.lock:
            return [dict(it) for it in self.items[name].values()
                    if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) == 0 and it.get("itemId")]

    def sign_in_daily(self, today: Optional[str] = None, now: Optional[float] = None) -> bool:
        """每日簽到（`doc/autoplay/game/daily.md` §2）。回傳 True＝這次簽到成功。

        - **一個帳號一天一次**，不是每隻角色：用隊長一隻送就好。
        - **等級 < 120 不送**：`Activity[5].need` 的 `levela 120` 是條件，未達門檻伺服器**完全不回**
          （逾時、也沒有 SystemTip），不是參數錯，不可以當成要重試。
        - 地圖不是條件：120 級以上在練功地圖直接簽，不用回村（2026-10-22 第三組 Mac 實測）。
        - 這是附加功能：任何失敗（逾時、isOK false）**只記日誌、不重試、不影響練功**；
          一天只試一次（記在 `signin_day`），常駐跨過 0 點日期變了會自動再試。
        """
        today = today or time.strftime("%Y-%m-%d", time.localtime(now))
        if self.signin_day == today:
            return False
        lv = self.s.levels.get(self.party[0][1]) if self.party else None
        if lv is None or lv < SIGNIN_LEVEL:
            self.signin_day = today
            self.log(f"簽到：等級 {lv} 未達 {SIGNIN_LEVEL}，今天跳過")
            return False
        self.signin_day = today                  # 先記下來：底下不管成功失敗，今天都不再送
        lead = self.party[0][1]
        try:
            info = (self.leader().request("Activity_EveryDayRewardGetInfo", {}, timeout=8) or {})
        except Exception as e:
            self.log(f"簽到：查詢失敗（{type(e).__name__}），今天不再試")
            return False
        d = info.get("everyDayRewardInfo") or {}
        times, last = d.get("times") or 0, d.get("date") or 0
        if last and time.strftime("%Y-%m-%d", time.localtime(last)) == today:
            self.log(f"簽到：今天已經簽過（第 {times} 天）")
            return False
        before = {it["itemIndex"]: it.get("overlay", 1) for it in self.bag(lead)}

        def got_something():
            # ⚠ wait() 是**持有 self.lock** 呼叫這個條件的，裡面不能再呼叫 bag()（它也要上同一把鎖，
            # threading.Lock 不可重入 → 死結）。直接讀 items（2026-10-22 寫測試時卡住才發現）。
            return any(it.get("itemId") and it.get("itemStorge", 0) == 0
                       and (it["itemIndex"] not in before
                            or it.get("overlay", 1) > before[it["itemIndex"]])
                       for it in list(self.items.get(lead, {}).values()))

        try:
            r = self.leader().request("Activity_EveryDayRewardGet", {"day": times + 1}, timeout=8) or {}
        except Exception as e:
            self.log(f"簽到：送出失敗（{type(e).__name__}），今天不再試")
            return False
        if not r.get("isOK"):
            self.log(f"簽到：被拒 {r}，今天不再試")
            return False
        day = (r.get("everyDayRewardInfo") or {}).get("times") or times + 1
        self.wait(got_something, 5)
        table = self.item_table()
        got = [f"{table.get(it['itemId'], {}).get('nameTn', it['itemId'])}"
               f"×{it.get('overlay', 1) - before.get(it['itemIndex'], 0)}"
               for it in self.bag(lead)
               if it["itemIndex"] not in before or it.get("overlay", 1) > before[it["itemIndex"]]]
        self.log(f"簽到：成功 第 {day} 天，獲得 {'、'.join(got) if got else '（沒看到背包變化）'}")
        return True

    def eat_exp_fruit(self, name: str) -> bool:
        """規範 1：背包有經驗果就吃一顆（itemScriptId 取道具表 scripts）。戰鬥中不能吃，呼叫前要先停戰鬥。
        Item_ItemUse 伺服器不回 response（2026-10-22 Mac 實機 request 逾時）→ 只送，結果看 Item_ItemInfo 數量減少。"""
        fruit = next((it for it in self.bag(name) if it["itemId"] in EXP_FRUITS), None)
        if not fruit:
            return False
        idx, before = fruit["itemIndex"], fruit.get("overlay", 1)
        script = (self.item_table().get(fruit["itemId"], {}).get("scripts") or [0])[0]
        self.clients[name].send("Item_ItemUse", {"itemIndex": idx, "itemNum": 1, "itemScriptId": script,
                                                    "data": [], "useMuch": False})
        ok = self.wait(lambda: self.items[name].get(idx, {}).get("overlay", 0) < before, 5)
        self.log(f"{name} 吃經驗果 {fruit['itemId']}{'' if ok else '（沒看到數量減少）'}")
        return ok

    def open_boxes(self, name: str) -> int:
        """新手禮盒先打開（裡面有新手裝、經驗果、另一個禮盒），不然會被當成綁定道具存進倉庫。
        Item_ItemUse 的 itemScriptId 取道具表 scripts（禮盒是 1058）。最多開 5 次（禮盒裡還有禮盒）。
        打不開的（1轉新手禮盒 90328 在 1 級打不開，2026-10-22 第三組實機）跳過、繼續找下一個，不中斷。"""
        n, skipped = 0, set()
        for _ in range(5):
            ok_boxes = boxes_for_trans(self.trans.get(name) or 0)
            box = next((it for it in self.bag(name) if it["itemId"] in ok_boxes and it["itemIndex"] not in skipped), None)
            if not box:
                break
            idx, before = box["itemIndex"], box.get("overlay", 1)
            script = (self.item_table().get(box["itemId"], {}).get("scripts") or [0])[0]
            self.clients[name].send("Item_ItemUse", {"itemIndex": idx, "itemNum": 1, "itemScriptId": script,
                                                        "data": [], "useMuch": False})
            if not self.wait(lambda: self.items[name].get(idx, {}).get("overlay", 0) < before, 5):
                self.log(f"{name} 禮盒 {box['itemId']} 打不開（可能等級／轉生不夠），跳過")
                skipped.add(idx)
                continue
            n += 1
            self.log(f"{name} 打開禮盒 {self.item_table().get(box['itemId'], {}).get('nameTn')}")
        return n

    def store_bound_items(self, name: str) -> int:
        """規範 2：綁定道具（裝備、經驗果除外）存個人倉庫第 1 頁。"""
        table, n = self.item_table(), 0
        for it in self.bag(name):
            t = table.get(it["itemId"], {}).get("type")
            if it.get("bind") != 1 or it["itemId"] in EXP_FRUITS or it["itemId"] in GIFT_BOXES or t in CHAR_EQUIP_TYPES:
                continue
            r = self.clients[name].request("ItemHandle_SaveItemToNewDepot",
                                           {"page": 1, "isPublic": False, "itemIndex": it["itemIndex"]}) or {}
            if not r.get("isOK"):
                self.log(f"{name} 綁定道具 {it['itemId']} 存不進個人倉庫：{r}")
                return n
            n += 1
        if n:
            self.log(f"{name} 綁定道具 {n} 件存進個人倉庫")
        return n

    def equip_all(self, name: str, level: int) -> int:
        """規範 4：每個部位穿上等級符合、等級最高的裝備（只看人物裝備；寵物裝備不碰）。"""
        table, n = self.item_table(), 0
        with self.lock:
            worn = {EQUIP_SLOT(table.get(it.get("itemId"), {}).get("type")): table.get(it.get("itemId"), {}).get("level", 0)
                    for it in self.items[name].values() if it.get("overlay", 1) > 0 and it.get("itemStorge", 0) != 0}
        best: Dict[object, dict] = {}
        for it in self.bag(name):
            row = table.get(it["itemId"], {})
            if row.get("type") not in CHAR_EQUIP_TYPES or row.get("level", 0) > level:
                continue
            slot = EQUIP_SLOT(row["type"])
            if slot not in best or row.get("level", 0) > table[best[slot]["itemId"]].get("level", 0):
                best[slot] = it
        for slot, it in best.items():
            if slot in worn and worn[slot] >= table[it["itemId"]].get("level", 0):
                continue
            r = self.clients[name].request("ItemHandle_itemEquipOn", {"itemIndex": it["itemIndex"]}) or {}
            if r.get("isOK"):
                n += 1
                self.log(f"{name} 穿上 {table[it['itemId']].get('nameTn')}")
            else:
                self.log(f"{name} 穿不上 {table[it['itemId']].get('nameTn')}：{r}")
        return n

    # ---- 轉生後的寵物配置（使用者 2026-10-24）----
    def pet_depot(self, name: str, public: bool = False) -> List[dict]:
        """寵物倉庫第 1 頁的內容（預設個人；public=True 是同帳號共用的公共倉庫）。
        `NewPet_GetNewPetDepotInfo{page, isPublic, isAll}`。每筆加上 `isPublic`，領回時照它送。"""
        r = self.clients[name].request("NewPet_GetNewPetDepotInfo",
                                       {"page": 1, "isPublic": public, "isAll": True}) or {}
        info = r.get("petInfoList")
        rows = list(info.values()) if isinstance(info, dict) else list(info or [])
        return [dict(p, isPublic=public) for p in rows if isinstance(p, dict) and (p.get("petId") or 0) > 0]

    def pet_page(self, name: str, row: dict) -> int:
        """倉庫那隻寵物在第幾頁（領回要送對的 page）。`isAll` 查詢會把全部頁一起回、但不說哪隻在哪頁，
        只好領之前一頁一頁查（page N、isAll false），查到那隻就回那頁；查不到或那頁被拒（沒解鎖）就回 1（照舊）。
        2026-11-03 第四組共倉 22 隻分兩頁（10＋12），第 2 頁的貝魯伊卡、3 隻金虎領回一律 isOK false。"""
        uid, public = row.get("uniqueIndex"), bool(row.get("isPublic"))
        for page in range(1, PET_DEPOT_PAGES + 1):
            r = self.clients[name].request("NewPet_GetNewPetDepotInfo",
                                           {"page": page, "isPublic": public, "isAll": False}) or {}
            if not r.get("isOK"):
                break
            info = r.get("petInfoList")
            rows = list(info.values()) if isinstance(info, dict) else list(info or [])
            if any(isinstance(x, dict) and x.get("uniqueIndex") == uid for x in rows):
                return page
        return 1

    def pet_depots(self, name: str) -> List[dict]:
        """個倉＋共倉第 1 頁（取回寵物時兩邊都找：個倉滿了的會存在共倉）。"""
        return self.pet_depot(name) + self.pet_depot(name, public=True)

    def pet_depot_target(self, name: str) -> Optional[bool]:
        """寵物要存哪個倉庫：個倉第 1 頁還有空位 → False（個倉）；滿了改共倉第 1 頁 → True；兩邊都滿 → None。
        使用者 2026-10-26「改存共倉」：第二組四轉打完機暴要全部存倉，角色60個倉 20/20 存不進去（LESSONS #89）。
        第 2 頁以後要鑽石解鎖（個倉 100、共倉 200），不碰。"""
        if len(self.pet_depot(name)) < PET_DEPOT_NUM:
            return False
        if len(self.pet_depot(name, public=True)) < PET_DEPOT_NUM:
            return True
        return None

    def reset_pets_after_trans(self, name: str) -> bool:
        """轉生後重配寵物（使用者 2026-10-24）：
        **身上所有寵物先存個人倉庫**，再取出「金虎」與「未滿 140 級裡等級最高的」，
        金虎鎖騎寵、另一隻鎖戰寵。

        為什麼要先全存再取：轉生後身上常是上一輪的隊伍（練過的任務寵、守護獸…），
        直接挑會受寵物欄 5 格限制；而且外掛鎖定還指著舊的唯一編號，寵物一存倉就失效
        （2026-10-23 實機：存倉後練寵模式「戰寵換不成」）。
        滿 140 的不當戰寵：已經練不動了。
        """
        c = self.clients[name]
        r = c.request("WaiGua_NomalSettingHandlePet",       # 先解鎖，鎖著的寵物不能改狀態／存倉
                      {"fightPetIndex": 0, "ridePetIndex": 0}) or {}
        if not r.get("isOK"):
            self.log(f"{name} 轉生後解開寵物鎖定失敗：{r}"); return False
        with self.lock:
            onhand = [(i, dict(p)) for i, p in self.petinfo[name].items() if (p.get("petId") or 0) > 0]
        for idx, p in sorted(onhand):
            if p.get("status") in (2, 3):                   # 出戰／騎乘中要先休息才存得進去
                c.send("NewPet_NewPetChangeStatus", {"petIndex": idx, "status": 1})
                self.nap(1)
            public = self.pet_depot_target(name)
            if public is None:
                self.log(f"{name} 個倉、共倉第 1 頁都滿了，寵物存不進去（第 {idx} 格）"); return False
            rr = c.request("NewPet_SavePetToNewDepot", {"page": 1, "isPublic": public, "petIndex": idx}) or {}
            if not rr.get("isOK"):
                self.log(f"{name} 寵物存{'共' if public else '個'}倉失敗（第 {idx} 格）：{rr}"); return False
            self.nap(0.8)
        self.log(f"{name} 轉生後把身上 {len(onhand)} 隻寵物存進倉庫")

        rows = self.pet_depots(name)
        # **只挑 1 級的金虎**（使用者原話「取出 1 級的金虎」）：轉生後人物是 1 級，
        # 騎不上高等坐騎——2026-10-24 第二組領回 128 級金虎，狀態直接變休息（3a492c3 的 bug）。
        tiger = next((p for kind in (GOLD_TIGER, RED_TIGER) for p in rows
                      if p.get("petId") == kind and (p.get("level") or 0) == 1), None)
        best = max((p for p in rows
                    if (p.get("level") or 0) < PET_LEVEL_MAX and p.get("petId") not in (GOLD_TIGER, RED_TIGER)),
                   key=lambda p: p.get("level") or 0, default=None)
        caught = None
        if tiger is None and best is not None:
            # 11-09 cgy 兩組：英雄島給的 1 級金虎被 release_below3 放生，轉生後沒有騎寵、傳送被拒
            caught = tiger = self.catch_red_tiger(name)
        if tiger is None or best is None:
            others = [p.get("level") for p in rows if p.get("petId") == GOLD_TIGER]
            why = (f"沒有 1 級金虎（倉庫裡的金虎等級：{others}）" if others and tiger is None
                   else "倉庫裡沒有金虎" if tiger is None else "")
            self.log(f"{name} 轉生後配寵物失敗：{why or ''}"
                     f"{'；' if why and best is None else ''}"
                     f"{f'沒有未滿 {PET_LEVEL_MAX} 級的寵物' if best is None else ''}"
                     f"｜倉庫 {[(p.get('petId'), p.get('level')) for p in rows]}")
            return False
        for p, what in ((tiger, "騎寵"), (best, f"戰寵（{p_level(best)} 級）")):
            if p is caught:
                continue                                    # 剛抓的紅虎就在身上
            rr = c.request("NewPet_GetPetFromNewDepot",
                           {"page": self.pet_page(name, p), "isPublic": bool(p.get("isPublic")),
                            "uniqueIndex": p["uniqueIndex"]}) or {}
            if not rr.get("isOK"):
                self.log(f"{name} 從倉庫領回{what}失敗：{rr}"); return False
            self.nap(0.8)
        self.log(f"{name} 領回騎寵（petId {tiger.get('petId')}）與戰寵（petId {best.get('petId')}、{p_level(best)} 級）")
        return self.set_fight_pet(name, best["uniqueIndex"], tiger["uniqueIndex"])

    def catch_red_tiger(self, name: str) -> Optional[dict]:
        """倉庫沒有 1 級金虎、也沒有 1 級紅虎 → 用誘餌抓一隻 1 級紅虎當騎寵（誘餌不夠用石幣補買）。
        抓不到就回 None，照舊停下來回報。"""
        from .jobs import JobError
        from .jobs.catch import SLOT_MAX, catch_one
        try:
            catch_one(self, name, [(RED_TIGER, 1)], SLOT_MAX, buy=True)
        except JobError as e:
            self.log(f"{name} 抓紅虎失敗：{e}")
            return None
        with self.lock:
            got = [dict(p) for p in self.petinfo[name].values()
                   if p.get("petId") == RED_TIGER and (p.get("level") or 0) == 1 and p.get("uniqueIndex")]
        self.log(f"{name} 沒有 1 級金虎，抓了 1 級紅虎當騎寵：{'成功' if got else '沒抓到'}")
        return got[0] if got else None

    def ensure_pets(self, name: str) -> bool:
        """規範 3：戰寵、騎寵一定要設（唯一編號）。騎寵只能挑不用學的（Ride.json needlearn false）、等級最高的；
        戰寵＝其餘等級最高的。外掛鎖定已經指到身上的寵物（騎寵也能騎）就沿用。"""
        rides = self.rideable()
        with self.lock:
            pets = sorted(((p.get("level", 0), p.get("uniqueIndex"), p.get("petId")) for p in self.petinfo[name].values()
                           if (p.get("petId") or 0) > 0 and p.get("uniqueIndex")), reverse=True)
            wg = dict(self.wg.get(name, {}))
        have = {u for _, u, _ in pets}
        can_ride = [u for _, u, pid in pets if pid in rides]
        if wg.get("fightPetIndex") in have and wg.get("ridePetIndex") in can_ride \
                and wg.get("fightPetIndex") != wg.get("ridePetIndex"):
            return True
        if len(pets) < 2:
            self.log(f"{name} 寵物不到 2 隻（{len(pets)}），戰寵／騎寵設不齊")
            return False
        if not can_ride:
            self.log(f"{name} 沒有能騎的寵物（身上的都要先學騎乘，Ride.json needlearn），騎寵設不了")
            return False
        fight = wg.get("fightPetIndex") if wg.get("fightPetIndex") in have else pets[0][1]
        others = [u for u in can_ride if u != fight]
        if wg.get("ridePetIndex") in others:
            ride = wg.get("ridePetIndex")
        elif others:
            ride = others[0]
        else:                                             # 唯一能騎的就是戰寵 → 它改當騎寵，戰寵換其餘最高的
            ride = fight
            fight = next(u for _, u, _ in pets if u != ride)
        return self.set_fight_pet(name, fight, ride)

    def ensure_hidden(self, name: str, need_state: bool = False) -> bool:
        """規範 5：外掛全部隱藏（`NewRole_RealHideMode` 是切換開關，只在 hideMode 還沒開時送）。
        設定檔 [waigua] hide = false：不動。
        need_state：一筆角色狀態（RoleAttr）都還沒收到就不送（切換開關，盲送可能反而關掉）、回 False。
        收到過狀態但沒有 hideMode＝伺服器沒帶預設值＝沒開 → 送。"""
        if self.waigua is not None and not self.waigua.hide:
            return True
        with self.lock:
            if self.hide.get(name):
                return True
            seen = name in self.attrs
        if need_state and not seen:
            self.log(f"{name} 還沒收到角色狀態，不知道隱藏開了沒，不送")
            return False
        r = self.clients[name].request("NewRole_RealHideMode", {}) or {}
        ok = bool(r.get("isOK")) and self.wait(lambda: bool(self.hide.get(name)), 5)
        self.log(f"{name} 全部隱藏{'' if ok else f'（沒確認到：{r}）'}")
        return ok

    # ---- 外掛首次動作：跟著身上的精靈金幣走 ----
    FIRST_CMD_SKILL = 3                       # firstRoleCmd：1 攻擊／2 防禦／3 技能／9 逃跑
    FIRST_CMD_TARGET_ENEMY = 3                # 目標規則：1 自己、2 自己的寵物、3 隨機敵人、4 隨機隊友
    # Item.json 的 type：9＝精靈道具（貝鈴／石笛／木笛／金幣），只有這類的技能才是「放精靈」

    def spirit_coin(self, name: str) -> Optional[Tuple[int, int, int]]:
        """身上戴著的精靈金幣 →（itemId, skillId, skillLevel）。沒戴回 None。

        技能編號不寫死：直接讀 `Item.json` 的 `skill: [{skillId, skillLevel}]`
        （阿薩＝大地 10185、阿昆尼斯＝水 10190、芙雷美雅＝火 10195、溫蒂妮＝風 10200…），
        所以地隊戴大地金幣就會算出 10185，不會被改成別的。

        **只看 `type == 9`（精靈道具：貝鈴／石笛／木笛／金幣）**：很多裝備也帶 skill
        （新手鎧甲 type 7 是 10111、月光的手鐲 type 8 是 10215、薩姆吉爾的首飾 type 10 是 10211），
        不限定種類就會挑到鎧甲的技能——2026-10-25 第三組三轉後就被設成 10111，使用者問
        「你又自轉放水嗎」才發現（隊長戴的是阿昆尼斯水金幣）。
        """
        table = self.item_table()
        with self.lock:
            worn = [dict(v) for v in self.items.get(name, {}).values()
                    if v.get("overlay", 1) > 0 and v.get("itemStorge", 0) != 0]
        for it in worn:
            row = table.get(it.get("itemId"), {})
            if row.get("type") != SPIRIT_ITEM_TYPE:
                continue
            sk = row.get("skill") or []
            if sk and sk[0].get("skillId"):
                return it["itemId"], int(sk[0]["skillId"]), int(sk[0].get("skillLevel") or 1)
        return None

    def ensure_first_cmd(self, name: str) -> bool:
        """首次動作＝身上那枚精靈金幣的技能、目標固定「隨機敵人」。

        **轉生會把外掛設定洗掉**：2026-10-24 第三組二轉後，隊長變成「大地的精靈 10185、目標 1＝自己」，
        但他戴的是阿昆尼斯（水 10190）金幣——技能和金幣對不上，第一回合等於白放，
        使用者問「放水 轉對方嗎」才發現。沒戴精靈金幣的角色不動（多數隊員第五格戴首飾）。
        設定檔 [waigua] first_cmd = "none"：不動。
        """
        if self.waigua is not None and self.waigua.first_cmd == "none":
            return True
        coin = self.spirit_coin(name)
        if coin is None:
            return True
        item_id, skill, level = coin
        want = {"firstRoleCmdFlg": True, "firstRoleCmd": self.FIRST_CMD_SKILL,
                "firstRoleCmdArg": str(skill), "firstRoleCmdArgEx": str(level),
                "firstRoleCmdArgTargetRule": self.FIRST_CMD_TARGET_ENEMY}
        with self.lock:
            now = dict(self.battle.get(name, {}))
        if all(str(now.get(k)) == str(v) for k, v in want.items()):
            return True
        r = self.clients[name].request("WaiGua_FastBattleSettingChange", dict(want)) or {}
        ok = bool(r.get("isOK"))
        self.log(f"{name} 首次動作 → 技能 {skill} Lv{level}（金幣 {item_id}）、目標隨機敵人"
                 f"{'' if ok else f'（沒確認：{r}）'}")
        if ok:
            with self.lock:
                self.battle.setdefault(name, {}).update(want)
        return ok

    def init_baseline(self) -> bool:
        """開局基礎規範 1～5、7（6 快戰要到練功地圖才能開，在傳送之後做）。先停戰鬥；每項失敗就回 False。

        **只套在「1 級**而且**0 轉」的角色**：
        - 使用者 2026-10-22「是 1 等開局才套」——第二組 124 級重啟被套用 → 山賊的羽飾頂掉月光的手鐲（LESSONS #54）。
        - 使用者 2026-10-23「應該改成 1 轉那時候的」——**轉生會把等級打回 1，但那不是新角色**：
          裝備、寵物、倉庫都是練滿一輪整理好的，再套 0 轉規範會用新手裝換掉好裝備、
          把四大證明當綁定道具存倉。轉生後的 1 級走 `init_after_trans()`。
        等級不知道不套。轉數不知道當 0（實機登入的 `Role_RoleInfo` 一定帶 `trans`）。
        """
        with self.lock:
            fresh = [n for _, n in self.party
                     if self.s.levels.get(n) == 1 and (self.trans.get(n) or 0) == 0]
            reborn = [n for _, n in self.party
                      if self.s.levels.get(n) == 1 and (self.trans.get(n) or 0) >= 1]
        if reborn:
            return self.init_after_trans(reborn)
        if not fresh:
            self.log("不是 1 級 0 轉開局，略過開局基礎規範")
            if self.battle_heal and self.waigua is None:      # 有設定檔的由 apply_waigua 統一對齊
                # 戰鬥中補血只在開局穿完裝、轉生後才設：功能加入前就開局的隊伍一直沒設到
                # （11-05 容器實跑五隻都沒開，波拉島血量掉到 16%）。已經一樣的不送，所以每次登入補一次
                self.ensure_battle_heal([n for _, n in self.party])
            return True
        if not self.pause():
            self.log("開局初始化：停不了戰鬥"); return False
        self.take_exp_fruits(fresh)
        ok = True
        for name in fresh:
            level = 1
            self.open_boxes(name)                         # 先開禮盒（新手裝、經驗果都在裡面）
            self.eat_exp_fruit(name)
            self.equip_all(name, level)
            self.store_bound_items(name)                  # 穿完、吃完才存剩下的綁定道具
            ok = self.ensure_pets(name) and ok
        # 隱藏、首次動作、加點、戰鬥中補血（穿完裝才有技能）、設定檔 raw：跟登入、轉生同一套
        return not self.align_waigua(fresh, hide_if_unknown=True) and ok

    # ---- 外掛戰鬥中技能補血（--battle-heal，使用者 2026-11-04「你要設定上去，預設 50% 補血」）----
    HEAL_SKILLS = (10119, 10111)                 # 恩惠的精靈（靈力鎧）優先，再來滋潤的精靈（新手鎧甲 Lv3，單體補 450）

    def heal_skill(self, name: str) -> Optional[Tuple[int, int]]:
        """身上穿的裝備附的補血技能 (skillId, 等級)，照 HEAL_SKILLS 的順序挑；沒有回 None。
        一定要是身上的：沒有那個技能伺服器照樣回 isOK 卻不套用（jobs/wgheal.py）。"""
        table = self.item_table()
        with self.lock:
            worn = [dict(v) for v in self.items.get(name, {}).values()
                    if v.get("overlay", 1) > 0 and v.get("itemStorge", 0) != 0]
        found: Dict[int, int] = {}
        for it in worn:
            for s in (table.get(it.get("itemId"), {}) or {}).get("skill") or []:
                sid, lv = s.get("skillId"), int(s.get("skillLevel") or 0)
                if sid in self.HEAL_SKILLS and lv > found.get(sid, 0):
                    found[sid] = lv
        sid = next((s for s in self.HEAL_SKILLS if s in found), None)
        return (sid, found[sid]) if sid else None

    def ensure_battle_heal(self, names: List[str], failed: Optional[List[str]] = None) -> List[str]:
        """`WaiGua_FastBattleSettingHpChange`：人物、寵物、騎寵血量低於 battle_heal % 就放補血技能。
        沒有補血技能的角色跳過（記日誌，不算失敗）；被拒記日誌、不重試，有給 failed 就記進去。已經是同樣設定的不送。"""
        per = int(self.battle_heal)
        done = []
        for n in names:
            hs = self.heal_skill(n)
            if hs is None:
                self.log(f"{n} 身上裝備沒有補血技能，外掛補血不設")
                continue
            skill, level = hs
            want = {"hpRoleCmdFlg": True, "hpRoleCmdArg": str(skill), "hpRoleCmdArgEx": str(level),
                    "hpRoleHpRolePer": per, "hpRoleHpPetPer": per, "hpRoleHpRidePer": per}
            with self.lock:
                now = dict(self.battle.get(n, {}))
            if all(str(now.get(k)) == str(v) for k, v in want.items()):
                continue
            r = self.clients[n].request("WaiGua_FastBattleSettingHpChange", dict(want)) or {}
            if not r.get("isOK"):
                self.log(f"{n} 外掛補血設定被拒：{r}")
                if failed is not None:
                    failed.append(n)
                continue
            with self.lock:
                self.battle.setdefault(n, {}).update(want)
            done.append(n)
            self.log(f"{n} 外掛補血：技能 {skill} Lv{level}，人物／寵物／騎寵血 < {per}% 就補")
        return done

    # ---- 設定檔 [waigua]：每次登入照設定檔檢查外掛（使用者 2026-11-05「把外掛設定全部拉成配置檔」）----
    def apply_waigua(self) -> List[str]:
        """登入時照設定檔對齊外掛（一次登入一次）。開局／轉生流程這次已經**成功**對齊過的角色（waigua_aligned）跳過，
        其餘隊員照樣檢查（只有部分隊員轉生時，其他人也要對齊）。沒用設定檔（waigua None）什麼都不做。
        回傳沒設成的「角色：項目」；呼叫端照規範第 2 點停止。"""
        if self.waigua is None or self.waigua_done:
            return []
        self.waigua_done = True
        names = [n for _, n in self.party if n not in self.waigua_aligned]
        return self.align_waigua(names) if names else []

    def align_waigua(self, names: List[str], hide_if_unknown: bool = False, trans_points: bool = False) -> List[str]:
        """外掛設定對齊的**唯一**一份（開局、轉生後、登入都呼叫這裡，新增設定只改這裡）：
        隱藏、首次動作、加點（自訂計畫；沒有就轉生加點 trans_points／推薦）、戰鬥中補血、`[waigua.raw]`。
        各項都照設定檔開關（沒用設定檔＝內建預設），跟伺服器現值一樣就不送。
        hide_if_unknown：隱藏是切換開關，平常現值不知道不送（免得反而關掉）；開局／轉生流程照舊直接設。
        全部設成的角色記進 waigua_aligned；回傳沒設成的「角色：項目」（不重試，呼叫端停止）。"""
        bad: List[str] = []
        for n in names:
            if not self.ensure_hidden(n, need_state=not hide_if_unknown):   # hide = false 回 True
                bad.append(f"{n}：隱藏")
            if not self.ensure_first_cmd(n):              # first_cmd = "none" 或沒戴精靈金幣都回 True
                bad.append(f"{n}：首次動作")
        if self.point_plan:
            todo = [n for n in names if not self.plan_points_match(n)]
            if todo:
                done = set(self.set_plan_points(todo))
                bad += [f"{n}：自訂加點" for n in todo if n not in done]
        elif trans_points:
            done = set(self.set_trans_points(names))
            bad += [f"{n}：轉生加點" for n in names if n not in done]
        if self.battle_heal:
            failed: List[str] = []
            self.ensure_battle_heal(names, failed)
            bad += [f"{n}：戰鬥中補血" for n in failed]
        failed = []
        self.ensure_auto_points(names, failed)            # 已經有加點模式的不動
        bad += [f"{n}：自動加點" for n in failed]
        bad += self.apply_raw(names)
        self.waigua_aligned.update(n for n in names if not any(b.startswith(f"{n}：") for b in bad))
        if bad:
            self.log(f"外掛設定沒設成：{'、'.join(bad)}")
        return bad

    def apply_raw(self, names: List[str]) -> List[str]:
        """設定檔 `[waigua.raw]`：每個協定只跟**它自己那份**伺服器推送的現值比（battleSetting／catchPetSetting／
        wgNomalSetting，見 botconfig.RAW_SOURCE；不跟 RoleAttr 的 hp 之類撞名欄位混在一起）。一樣就不送。
        推送裡沒有的欄位當 sproto 預設值（false／0／空字串／空陣列：伺服器不送預設值）。
        伺服器不推現值的協定（AutoEatOffLineItem）每次登入送一次。逾時、編碼錯誤、被拒回傳「角色：協定」。"""
        from .botconfig import RAW_SOURCE
        w = self.waigua
        if w is None or not w.raw:
            return []
        stores = {"battleSetting": self.battle, "catchPetSetting": self.catch, "wgNomalSetting": self.wg}
        bad: List[str] = []
        for proto, fields in w.raw.items():
            store = stores.get(RAW_SOURCE[proto[7:]] or "")
            for n in names:
                if store is not None:
                    with self.lock:
                        now = dict(store.get(n, {}))
                    if all(_same_value(now.get(k, type(v)()), v) for k, v in fields.items()):
                        continue
                try:
                    r = self.clients[n].request(proto, dict(fields)) or {}
                except Exception as e:  # noqa: BLE001  逾時／編碼錯誤：記下原因交給呼叫端停止
                    r = {"error": f"{type(e).__name__}: {e}"}
                if not r.get("isOK"):
                    self.log(f"{n} 設定檔外掛 {proto} 沒設成：{r}")
                    bad.append(f"{n}：{proto[7:]}")
                    continue
                if store is not None:
                    with self.lock:
                        store.setdefault(n, {}).update(fields)
                self.log(f"{n} 設定檔外掛 {proto[7:]} → {fields}")
        return bad

    def plan_points_match(self, name: str) -> bool:
        """自訂加點已經是設定檔的計畫（自訂模式、四項目標一樣）就不用再送。"""
        vit, spd, atk = self.plan_for(name)
        want = {1: vit, 4: spd, 2: atk, 3: 0}
        with self.lock:
            a = self.attrs.get(name, {})
            mode = self.point_type.get(name, a.get("autoSetPointType"))
            diy = a.get("autoPointDiyList")
            sent = self.plan_sent.get(name)
        rows = list(diy.values()) if isinstance(diy, dict) else list(diy or [])
        have = {int(d.get("type") or 0): int(d.get("value") or 0) for d in rows if isinstance(d, dict)}
        # 剛設完、伺服器還沒推新的 RoleAttr：看這次送成功的（不然馬上又判斷不一致重送）
        return mode == 1 and (have == want or sent == want)

    # ---- 外掛自訂加點目標（--point-plan，使用者 2026-11-04「隊長 體敏 100 攻擊力 999、隊員 體敏 80 攻擊力 999」）----
    def plan_for(self, name: str) -> Tuple[int, int, int]:
        lead = self.party[0][1] if self.party else None
        return self.point_plan["lead" if name == lead else "member"]

    def set_plan_points(self, names: List[str]) -> List[str]:
        """`WaiGua_AutoRolePointSet{autoPointDiyList}`＋`{autoSetPointType:1}`：體力、速度到目標，剩下全加腕力（攻擊）。
        type 1 體力、2 腕力、3 耐力、4 速度；sort 照使用者講的順序（體 → 敏 → 攻）。腕力目標 999＝之後的點數全加攻擊，
        所以這個模式**不會**改回系統推薦（points_ready_for_recommend 看 point_plan 就跳過）。被拒記日誌、不重試。"""
        done = []
        for n in names:
            vit, spd, atk = self.plan_for(n)
            diy = [{"type": 1, "value": vit, "sort": 1}, {"type": 4, "value": spd, "sort": 2},
                   {"type": 2, "value": atk, "sort": 3}, {"type": 3, "value": 0, "sort": 4}]
            c = self.clients[n]
            r1 = c.request("WaiGua_AutoRolePointSet", {"autoPointDiyList": diy}) or {}
            r2 = c.request("WaiGua_AutoRolePointSet", {"autoSetPointType": 1}) or {}
            if r1.get("isOK") and r2.get("isOK"):
                with self.lock:
                    self.point_type[n] = 1
                    self.plan_sent[n] = {d["type"]: d["value"] for d in diy}
                done.append(n)
                self.log(f"{n} 外掛自訂加點：體 {vit}、敏 {spd}、攻 {atk}")
            else:
                self.log(f"{n} 外掛自訂加點被拒：清單 {r1}、切換 {r2}")
        return done

    # ---- 隊長屬性精靈（--auto-spirit，使用者 2026-11-04「先買，60 級後隊長穿上去」）----
    def autospirit_due(self, now: Optional[float] = None) -> bool:
        """要不要排 `spirit --apply`：隊長沒戴精靈道具，而且（背包沒有那枚 → 要買）或（背包有、等級到了 → 要穿）。
        只在有佇列、沒有 hold、不在工作中、佇列空的時候排；10 分鐘內不重排（失敗不會每圈都排）。"""
        if not self.auto_spirit or not self.party:
            return False
        now = time.time() if now is None else now
        if self.jobq is None or self.hold or self.job is not None or self.jobq.pending():
            return False
        if now - self.last_spirit < self.AUTOCLEAN_EVERY:
            return False
        self._spirit_result()
        if self.spirit_tries >= self.SPIRIT_TRIES_MAX:
            return False                             # 規範 2：自動排的工作失敗 3 次就不再排（要人看）
        from .jobs.spirit import plan, wears_element
        try:
            pl = plan(self)
        except Exception:  # noqa: BLE001  屬性還沒收到等：下一輪再看
            return False
        if wears_element(self, pl.get("worn"), pl.get("itemId")):
            return False
        if (pl["level"] or 0) < pl["need"] and (pl["inBag"] is not None or self.spirit_have):
            return False                             # 已經買好（背包或個倉），等 60 級
        return True

    def _spirit_result(self) -> None:
        """看上一件自動排的 spirit 結果：失敗才算一次（成功不算）；結果說「等 60 級」就記下已經有金幣
        （之後被自動整理存進個倉也不會再排去買）。還沒做完就什麼都不改。"""
        if not self.spirit_job or self.jobq is None:
            return
        r = self.jobq.result(self.spirit_job)
        if r is None:
            return
        self.spirit_job = None
        if r.get("status") == "ok":
            if (r.get("data") or {}).get("waiting"):
                self.spirit_have = True
        else:
            self.spirit_tries += 1

    def autospirit(self, now: Optional[float] = None) -> bool:
        if not self.autospirit_due(now):
            return False
        self.last_spirit = time.time() if now is None else now
        job = self.jobq.add("spirit", ["spirit", "--apply", "--profile", self.profile])
        self.spirit_job = job["id"]
        self.log(f"隊長屬性精靈：自動排 spirit（{job['id']}；已失敗 {self.spirit_tries}/{self.SPIRIT_TRIES_MAX} 次；"
                 f"背包、個倉都沒有才去競技場買，60 級才穿）")
        return True

    SPIRIT_TRIES_MAX = 3

    # ---- 練功中自動吃經驗果 ----
    FRUIT_EVERY = 300.0                          # 幾秒看一次加成（不必每圈看）
    FRUIT_LOW = 600                              # 果子加成剩不到 10 分鐘就當沒有
    FRUIT_MALL_ID = 46                           # 商城石幣分頁：2倍智慧果（綁定），1,000,000 石幣（只准石幣買果子，使用者 11-02）
    FRUIT_RESERVE = 1_000_000                    # 買完至少還要留這麼多石幣，不夠就不買

    def fruit_missing(self, now: Optional[float] = None) -> List[str]:
        """沒有經驗果加成（名稱有「果」的 buff，剩不到 FRUIT_LOW 秒也算沒有）的角色。月卡祝福等不算果子。"""
        now = time.time() if now is None else now
        return [n for _, n in self.party
                if not any("果" in (b.get("name") or "") and (not b.get("until") or b["until"] - now > self.FRUIT_LOW)
                           for b in self.exp_buffs(n, now=now))]

    def buy_fruit(self, name: str) -> bool:
        """商城石幣分頁買 1 顆 2倍智慧果（jobs mall 同一套檢查與送後核對）。石幣不夠（留 FRUIT_RESERVE）就不買，回 False。"""
        from types import SimpleNamespace
        from .jobs import mall
        price = int(mall.row_of(self.FRUIT_MALL_ID).get("priceDefaultInit") or 0)
        have = mall.balance(self, name, "stone")
        if have is None or have < price + self.FRUIT_RESERVE:
            self.log(f"自動吃果子：{name} 石幣 {have} 不夠（買一顆 {price:,}，還要留 {self.FRUIT_RESERVE:,}），不買")
            return False
        mall.run(self, SimpleNamespace(id=self.FRUIT_MALL_ID, count=1, who=name, apply=True))
        return True

    def autofruit(self, now: Optional[float] = None) -> bool:
        """有人沒有經驗果加成 → 停遇敵（吃東西不能在戰鬥中）→ 倉庫的先取出 → 一隻一隻吃（每吃一顆重看一次，
        加成算整個帳號時只吃一顆就夠）→ 沒有果子且有 fruit_buy 就用石幣買 → 重開遇敵。
        出錯（吃了數量沒減少、買不成）就記一行、這次登入不再自動吃（規範 2：不每圈重試）。回傳有沒有動作。"""
        now = time.time() if now is None else now
        if not self.auto_fruit or self.fruit_off or now < self.fruit_next:
            return False
        self.fruit_next = now + self.FRUIT_EVERY
        miss = self.fruit_missing()
        if not miss:
            return False
        self.log(f"自動吃果子：{'、'.join(miss)} 沒有經驗果加成")
        if not self.pause():
            self.log("自動吃果子：停不了戰鬥，下次再看")
            return False
        try:
            self.take_exp_fruits(miss)
            for n in miss:
                if n not in self.fruit_missing():
                    continue                             # 前面那隻吃了，整個帳號都有了
                if not any(it.get("itemId") in EXP_FRUITS for it in self.bag(n)):
                    if not (self.fruit_buy and self.buy_fruit(n)):
                        self.log(f"自動吃果子：{n} 背包、倉庫都沒有果子{'' if self.fruit_buy else '（沒開自動買）'}")
                        continue
                if not self.eat_exp_fruit(n):
                    raise RuntimeError(f"{n} 吃了果子沒看到數量減少")
                self.wait(lambda: n not in self.fruit_missing(), 5)
            left = self.fruit_missing()
            self.log("自動吃果子：" + (f"還是沒有加成：{'、'.join(left)}" if left else "全隊都有經驗果加成了"))
        except Exception as e:                           # noqa: BLE001  吃果子出錯不停練功，但不再自動吃
            self.fruit_off = True
            self.log(f"自動吃果子出錯（{e}），這次登入不再自動吃")
        finally:
            self.set_loop(True)
        return True

    def init_after_trans(self, names: List[str]) -> bool:
        """轉生後規範（使用者 2026-10-23「應該改成 1 轉那時候的」）：轉生回到 1 級時做的事。

        和 0 轉開局的差別——**不碰裝備、不存綁定道具**：
        身上穿的是練滿一輪的好裝（月光的手鐲等），新手裝會把它換掉；四大證明等綁定道具
        二轉還要用，存倉只是多繞路。
        做的事：開 1轉新手禮盒（倉庫有就先取出）、吃經驗果、寵物鎖定、**武器換精靈王之弓**、全部隱藏、自動加點。
        """
        self.log(f"轉生後規範（1 級但已轉生）：{'、'.join(names)}")
        if not self.pause():
            self.log("轉生後規範：停不了戰鬥"); return False
        self.take_exp_fruits(names)                       # 禮盒／經驗果在倉庫就先取回
        ok = True
        for name in names:
            self.open_boxes(name)
            self.eat_exp_fruit(name)
            ok = self.reset_pets_after_trans(name) and ok   # 騎寵金虎＋戰寵「未滿 140 最高等」
            ok = self.wear_spirit_bow(name) and ok          # 使用者 11-11「每次轉身完，立刻改穿裝備武器-精靈王之弓」
        # 轉生會洗掉外掛設定（首次動作、補血、raw…），照登入同一套設回；沒有自訂加點計畫就用轉生加點
        return not self.align_waigua(names, hide_if_unknown=True, trans_points=True) and ok

    def wear_spirit_bow(self, name: str) -> bool:
        """轉生後 1 級立刻把武器換成精靈王之弓（使用者 2026-11-11「1轉1等 後，每次轉身完，立刻改穿裝備武器-精靈王之弓」）。
        已經穿著就不動；背包沒有就從個人倉庫、再從共倉取一把（16F 換到的弓平常被規則表存個倉）。
        `ItemHandle_itemEquipOn{itemIndex}` 要回 isOK 且那件變成穿著（itemStorge ≠ 0）才算；找不到、取不出、穿不上 → False（規範 2：停）。"""
        with self.lock:
            if any(it.get("itemId") == SPIRIT_BOW and it.get("itemStorge", 0) != 0 and it.get("overlay", 1) > 0
                   for it in self.items[name].values()):
                return True
        bow = next((it for it in self.bag(name) if it["itemId"] == SPIRIT_BOW), None)
        c = self.clients[name]
        if bow is None:
            for public in (False, True):
                r = c.request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": public}) or {}
                row = next((it for it in r.get("itemInfoList") or []
                            if it.get("itemId") == SPIRIT_BOW and it.get("uniqueIndex")), None)
                if row is None:
                    continue
                g = c.request("ItemHandle_GetItemToNewDepot", {"page": 1, "isPublic": public,
                                                              "uniqueIndex": row["uniqueIndex"], "count": 1}) or {}
                if not g.get("isOK") or not self.wait(
                        lambda: any(it.get("itemId") == SPIRIT_BOW and it.get("itemStorge", 0) == 0
                                    and it.get("overlay", 1) > 0 for it in self.items[name].values()), 5):
                    self.log(f"{name} 精靈王之弓從{'共倉' if public else '個人倉庫'}取不出來：{g}"); return False
                self.log(f"{name} 從{'共倉' if public else '個人倉庫'}取出精靈王之弓")
                bow = next((it for it in self.bag(name) if it["itemId"] == SPIRIT_BOW), None)
                break
        if bow is None:
            self.log(f"{name} 背包、個倉、共倉都沒有精靈王之弓，沒換武器"); return False
        idx = bow["itemIndex"]
        r = c.request("ItemHandle_itemEquipOn", {"itemIndex": idx}) or {}
        worn = bool(r.get("isOK")) and self.wait(
            lambda: any(it.get("itemId") == SPIRIT_BOW and it.get("itemStorge", 0) != 0
                        for it in self.items[name].values()), 5)
        self.log(f"{name} 穿上精靈王之弓" if worn else f"{name} 精靈王之弓穿不上：{r}")
        return worn

    # ---- 新角色：自動加點 ----
    def ensure_auto_points(self, names: Optional[List[str]] = None, failed: Optional[List[str]] = None) -> List[str]:
        """沒設過自動加點的角色設成推薦加點（新角色不設的話升級點數不分配、血量一直很低）。
        已經設過的（例如第 2 組隊長自訂加點、敏捷要高）不動。回傳這次設定的角色。names＝只看這些角色。
        被拒的有給 failed 就記進去。設定檔 [waigua] auto_points = "none"：不動。"""
        if self.waigua is not None and self.waigua.auto_points == "none":
            return []
        done = []
        for name in names if names is not None else [n for _, n in self.party]:
            with self.lock:
                cur = self.point_type.get(name)
            if cur:
                continue
            r = self.clients[name].request("WaiGua_AutoRolePointSet", {"autoSetPointType": 2}) or {}
            if not r.get("isOK"):
                self.log(f"{name} 設定推薦加點被拒：{r}")
                if failed is not None:
                    failed.append(name)
                continue
            with self.lock:
                self.point_type[name] = 2
            done.append(name)
            self.log(f"{name} 自動加點設成推薦加點")
        return done

    AUTOCLEAN_EVERY = 600.0                      # 規範 10：自動排的工作間隔最短 10 分鐘

    def autoclean_if_full(self, now: Optional[float] = None) -> bool:
        """練功中任一隻背包快滿（空格 ≤ 2）→ 排一次 `clean --apply --confirm-release --parallel`（使用者 2026-10-25：
        「不希望人物跑到一半空間不夠」；練到轉生等級要解任務時背包本來就是乾淨的）。
        只在有佇列、沒有 hold、不在工作中、佇列空的時候排；同一次快滿 10 分鐘內不重排（整理不掉的不會每圈都排）。
        回傳這次有沒有排。"""
        from .shell_exchange import FULL_FREE
        now = time.time() if now is None else now
        if self.jobq is None or self.hold or self.job is not None or self.jobq.pending():
            return False
        if now - self.last_autoclean < self.AUTOCLEAN_EVERY:
            return False
        if self.loadout_switched():
            # 情境配置中背包裡是刻意帶的東西（例：副本帶 8 瓶阿布的水）：規則表會把它們存回個倉
            # （2026-10-31 第二組剛買的 40 瓶阿布的水被自動整理全存回去，LESSONS #120）
            return False
        with self.lock:
            full = [n for _, n in self.party if self.shells.bag_free(n) <= FULL_FREE]
        if not full:
            return False
        self.last_autoclean = now
        job = self.jobq.add("clean", ["clean", "--apply", "--confirm-release", "--parallel", "--profile", self.profile])
        self.log(f"背包快滿（{'、'.join(full)}）：自動排整理 clean（{job['id']}），兩場戰鬥之間做")
        return True

    def loadout_switched(self) -> bool:
        """這組有沒有「切走還沒切回」的人物配置（`captures/loadouts/pending_<組>.json`）。"""
        if not self.profile:
            return False
        from .loadout import Store, default_dir
        return os.path.exists(Store(default_dir()).pending_path(str(self.profile)))

    def autorefresh_pets(self, now: Optional[float] = None) -> bool:
        """使用者 2026-10-25「組隊練功時，戰寵或騎寵已經 140，自行判斷挑一隻 <140 置換」：
        有人的鎖定戰寵／騎寵滿 140 級 → 排一次 `pets --apply --mode refresh`。練寵模式中（戰寵是要練的那隻）不排；
        佇列有東西／hold 中不排；10 分鐘內不重排（換不了的不會每圈都排）。"""
        now = time.time() if now is None else now
        if self.jobq is None or self.hold or self.job is not None or self.jobq.pending():
            return False
        if self.train_pet or self.orig_fight or now - self.last_petrefresh < self.AUTOCLEAN_EVERY:
            return False
        if self.keep_pets:
            # 使用者 2026-11-02 第六組「我把戰寵換布魯頓」「用我的指定的寵物，不能更換」：滿 140 也不自動換
            return False
        if self.loadout_switched():
            # 切到情境配置中（例：副本戰寵 2D利則諾頓 140 級）：換寵會把情境指定的戰寵換掉。
            # 2026-10-31 第二組切「副本」後馬上被排 pets refresh，差一點把 2D 地人龍換回（LESSONS #119）
            return False
        full = []
        for _, n in self.party:
            fight, ride = self.locks(n)
            with self.lock:
                if any(v.get("uniqueIndex") in (fight, ride) and v.get("uniqueIndex") and (v.get("level") or 0) >= 140
                       for v in self.petinfo.get(n, {}).values()):
                    full.append(n)
        if not full:
            return False
        self.last_petrefresh = now
        job = self.jobq.add("pets", ["pets", "--apply", "--mode", "refresh", "--profile", self.profile])
        self.log(f"戰寵／騎寵滿 140 級（{'、'.join(full)}）：自動排換寵 pets refresh（{job['id']}）")
        return True

    def point_goal(self, name: str, trans: int) -> Tuple[int, int]:
        """這隻角色的自訂加點目標 (速度, 體力)。**五轉以後全隊速度 200、體力 200**
        （使用者 2026-10-28：「五轉時，所有人物配點要改成敏200 血200，敏優先」，隊長不再是特例）；
        四轉以前照舊：速度 100、隊長 150，體力 100。不取鎖（呼叫端自己讀 trans），因為
        `points_ready_for_recommend` 是在鎖裡呼叫的，`threading.Lock` 不可重入（LESSONS #29）。"""
        if (trans or 0) >= 5:
            return 200, 200
        lead = self.party[0][1] if self.party else None
        return (150 if name == lead else 100), 100

    def set_trans_points(self, names: List[str]) -> List[str]:
        """轉生後規範第 3 點：自訂加點 速度、體力優先（目標值見 `point_goal`），滿了由
        points_ready_for_recommend 改推薦。
        上一轉後半已經改成推薦（autoSetPointType 2），轉生不會自己變回來（2026-10-25 第二組三轉：
        ensure_auto_points 看到「已設過」就跳過 → 一路推薦加點）。格式照 2026-10-24 擷取：4 列 {type, value, sort}，
        type 1 體力、2 腕力、3 耐力、4 速度。每隻送兩次（先清單、再切自訂），被拒就記日誌、不重試。"""
        done = []
        for n in names:
            with self.lock:
                trans = (self.attrs.get(n) or {}).get("trans") or 0
            spd, vit = self.point_goal(n, trans)
            diy = [{"type": 4, "value": spd, "sort": 1}, {"type": 1, "value": vit, "sort": 2},
                   {"type": 2, "value": 0, "sort": 3}, {"type": 3, "value": 0, "sort": 4}]
            c = self.clients[n]
            r1 = c.request("WaiGua_AutoRolePointSet", {"autoPointDiyList": diy}) or {}
            r2 = c.request("WaiGua_AutoRolePointSet", {"autoSetPointType": 1}) or {}
            if r1.get("isOK") and r2.get("isOK"):
                with self.lock:
                    self.point_type[n] = 1
                self.point_switched.discard(n)
                done.append(n)
                self.log(f"{n} 自訂加點：速度 {spd}、體力 {vit} 優先（滿了改推薦）")
            else:
                self.log(f"{n} 設定自訂加點被拒：清單 {r1}、切換 {r2}")
        return done

    def points_ready_for_recommend(self) -> List[str]:
        """使用者規則（2026-10-23）：敏、血優先（目標值見 `point_goal`：四轉以前 100／隊長敏 150、
        **五轉以後全隊 200／200**），**滿了就改系統推薦**。
        自訂加點只列敏、血兩項，加滿後升級點數就**不再分配、一直累積**（2026-10-24 查到每隻 183～221 點沒加）。
        回傳：自訂模式（1）、速度與體力都到標、還有剩餘點數、這次執行還沒試過的角色。
        `--point-plan`（腕力 999）不改推薦：剩下的點數本來就照計畫全加攻擊。"""
        out = []
        if self.point_plan:
            return out
        with self.lock:
            for _, n in self.party:
                a = self.attrs.get(n, {})
                need_spd, need_vit = self.point_goal(n, a.get("trans") or 0)
                if (self.point_type.get(n) == 1 and n not in self.point_switched
                        and a.get("speed", 0) >= need_spd and a.get("vitality", 0) >= need_vit
                        and a.get("potentialPoint", 0) > 0):
                    out.append(n)
        return out

    def switch_to_recommend(self, names: List[str]) -> List[str]:
        """把這些角色改成系統推薦加點（autoSetPointType 2）。每隻每次執行只試一次，被拒就記日誌、不重試。"""
        done = []
        for n in names:
            self.point_switched.add(n)
            r = self.clients[n].request("WaiGua_AutoRolePointSet", {"autoSetPointType": 2}) or {}
            if r.get("isOK"):
                done.append(n)
                self.log(f"{n} 敏／血已到標，自動加點改成系統推薦（剩餘點數會自動分配）")
            else:
                self.log(f"{n} 改系統推薦被拒：{r}")
        return done

    # ---- 練功前取出倉庫裡的經驗果（使用者 2026-10-22）----
    def take_exp_fruits(self, names: Optional[List[str]] = None) -> int:
        """查個人倉庫第 1 頁與公共倉庫第 1 頁，有可自動吃的經驗道具就整疊取出（外掛已設自動吃）。回傳取出幾疊。
        names＝只查這些角色（開局只查 1 級的）；None＝全隊。"""
        got = 0
        for name in names if names is not None else [n for _, n in self.party]:
            c = self.clients[name]
            for public in (False, True):
                r = c.request("ItemHandle_GetNewItemDepotInfo", {"page": 1, "isPublic": public}) or {}
                for it in r.get("itemInfoList") or []:
                    # 經驗果；以及被存進倉庫的新手禮盒（59b11a7 之前的版本會把它存進去，要取回來開）
                    if it.get("itemId") not in EXP_FRUITS | boxes_for_trans(self.trans.get(name) or 0) \
                            or not it.get("uniqueIndex"):
                        continue
                    g = c.request("ItemHandle_GetItemToNewDepot", {"page": 1, "isPublic": public,
                                                                  "uniqueIndex": it["uniqueIndex"],
                                                                  "count": it.get("overlay", 1)}) or {}
                    ok = bool(g.get("isOK"))
                    self.log(f"{name} 從{'公共' if public else '個人'}倉庫取出{'禮盒' if it['itemId'] in GIFT_BOXES else '經驗果'} {it['itemId']}×{it.get('overlay', 1)}"
                             f"{'' if ok else f'失敗：{g}'}")
                    got += ok
        return got

    # ---- 練寵模式（使用者 2026-10-22：猜謎三兄弟的金豬要練到 77）----
    def pet_index(self, name: str, pet_id, below: Optional[int] = None) -> Optional[int]:
        """pet_id 可以是一種或一組（tuple）：五隻各練不同種人龍時（2026-11-01）每隻找自己身上那隻。
        below＝練寵目標等級：同種多隻時挑**還沒到級**裡最高的那隻，都到級才回最高的。
        11-02 第六組 2016 要虎 307 練 30，身上 307 是 128 級騎寵貝魯卡：只挑最高的會當成已到級、新抓的虎不練。"""
        ids = species_of(pet_id, name)
        with self.lock:
            cands = [(p.get("level", 0), i) for i, p in self.petinfo[name].items() if p.get("petId") in ids]
        if below is not None:
            low = [c for c in cands if c[0] < below]
            if low:
                return max(low)[1]
        return max(cands)[1] if cands else None

    def locks(self, name: str) -> Tuple[int, int]:
        """目前外掛鎖定的（戰寵, 騎寵）寵物唯一編號；被清掉（0／-1）的改用目前出戰（2）、騎乘（3）的那隻。"""
        with self.lock:
            wg = self.wg.get(name, {})
            by_status = {p.get("status"): p.get("uniqueIndex") for p in self.petinfo[name].values()}
            fight, ride = wg.get("fightPetIndex") or 0, wg.get("ridePetIndex") or 0
        return (fight if fight > 0 else by_status.get(2) or 0, ride if ride > 0 else by_status.get(3) or 0)

    def set_fight_pet(self, name: str, fight_unique: int, ride_unique: int) -> bool:
        """`WaiGua_NomalSettingHandlePet` 填的是**寵物唯一編號（uniqueIndex）**，不是第幾格；
        兩個欄位都要填實際值（2026-10-22 填格子 4 → 伺服器把戰寵清成 0、騎寵 -1 也照存）。用推回的 wgNomalSetting 確認。

        **一併關掉「自動換戰寵」**（`autoChangeFightPetFlg`）：開著的時候鎖戰寵會被伺服器拒（提示
        40000000021「開啟自動換戰寵不能鎖戰寵」），而且它會自己挑等級最高的上場——2026-10-30 第五組
        練四色鯊魚，角色30的 319（56 級）一直佔著出戰、要練的 322 停在 18 級，`take` 回報成功但
        戰寵鎖還是 0。鎖定跟自動換本來就互斥，要鎖就一定要關。"""
        r = self.clients[name].request("WaiGua_NomalSettingHandlePet",
                                       {"fightPetIndex": fight_unique, "ridePetIndex": ride_unique,
                                        "autoChangeFightPetFlg": False})
        if not (r or {}).get("isOK"):
            self.log(f"{name} 設定戰寵被拒：{r}")
            return False
        def status_of(unique):
            return next((p.get("status") for p in self.petinfo[name].values() if p.get("uniqueIndex") == unique), None)

        def by_status():
            """填 0 的那一邊＝不鎖，就不要求牠的狀態（0 對不到任何寵物，`status_of(0)` 永遠是 None）。
            2026-10-28 第四組：交完寵物鎖定全清成 0，練寵想鎖戰寵（騎寵 0）時伺服器也沒推 wgNomalSetting，
            舊條件兩邊都對不上 → 連兩次「戰寵換不成」，但伺服器其實 isOK。至少要有一邊是實際寵物。"""
            if not fight_unique and not ride_unique:
                return False
            return ((status_of(fight_unique) == 2 or not fight_unique)
                    and (status_of(ride_unique) == 3 or not ride_unique))
        # 確認：推回的 wgNomalSetting，或寵物狀態變成出戰 2／騎乘 3。登入時會推 wgNomalSetting；第三組「沒推」
        # 其實是騎寵選到要先學的寵物、伺服器沒套用（2026-10-22 Mac 診斷，LESSONS #51），不是新角色不推
        def confirmed():
            return ((self.wg.get(name, {}).get("fightPetIndex") == fight_unique
                     and self.wg.get(name, {}).get("ridePetIndex") == ride_unique)
                    or by_status())
        ok = self.wait(confirmed, 6)
        if not ok:
            ok = self.put_on_stage(name, fight_unique, ride_unique) and self.wait(confirmed, 6)
        self.log(f"{name} 戰寵 → {fight_unique}、騎寵 {ride_unique}{'' if ok else '（伺服器沒確認）'}")
        return ok

    def put_on_stage(self, name: str, fight_unique: int, ride_unique: int) -> bool:
        """寵物**全都在休息**時，外掛鎖定伺服器雖然收下（isOK），卻不會自己把牠叫上場
        （2026-10-28 第四組：2032 交完寵物、`pets --mode rest` 之後鎖戰寵，狀態一直停在休息 1）。
        以前能成功是因為當時有寵物正在出戰，鎖定一換伺服器就替補上去。
        所以確認不到時主動送 `NewPet_NewPetChangeStatus`（沒有 response，只能 send 後看 `Pet_PetInfo`）
        把牠改成出戰 2／騎乘 3。回傳有沒有送出任何一個（都不用送就 False，不必再等一次）。"""
        sent = False
        for unique, status in ((fight_unique, 2), (ride_unique, 3)):
            if not unique:
                continue
            with self.lock:
                idx = next((i for i, p in self.petinfo[name].items()
                            if p.get("uniqueIndex") == unique and p.get("status") != status), None)
            if idx is None:
                continue
            self.clients[name].send("NewPet_NewPetChangeStatus", {"petIndex": idx, "status": status})
            self.log(f"{name} 寵物 {unique} 還在休息，改成{'出戰' if status == 2 else '騎乘'}（第 {idx} 格）")
            sent = True
        return sent

    def start_pet_training(self) -> bool:
        """5 隻的戰寵都改成要練的寵物，記住原本的戰寵；已經夠等級的不改。"""
        pid, lv = self.train_pet
        self.train_uid.clear()               # 上一輪留下的（petgoal／train 直接清 orig_fight 時沒清這個）
        for _, name in self.party:
            if name in self.train_skip:
                continue
            idx = self.pet_index(name, pid, below=lv)
            if idx is None:
                self.log(f"{name} 身上沒有寵物 {pid}"); return False
            with self.lock:
                level = self.petinfo[name][idx].get("level", 0)
                target = self.petinfo[name][idx].get("uniqueIndex")
            fight, ride = self.locks(name)
            if fight == target or not fight:
                # 上次練寵中途結束沒改回，鎖定還停在要練的寵物（2026-10-22 誤把金豬記成原本的戰寵）；
                # 或戰寵鎖定被清成 0（卡坦交寵物前全部休息、解鎖，2026-10-25 第二組練雞時「戰 0」停，#88）
                # → 原本的戰寵取「除了它、騎寵、金虎以外等級最高的那隻」
                # 都沒有就退一步取騎寵以外的金虎（11-06 第二、三組練人龍：原本戰寵就是金虎，斷線時戰寵沒改回、
                # 停在人龍上，重開「找不到原本的戰寵」停，LESSONS #178）
                trained = species_of(pid, name)
                with self.lock:
                    rest = [p for p in self.petinfo[name].values()
                            if p.get("uniqueIndex") not in (target, ride) and (p.get("petId") or 0) > 0
                            and p.get("petId") not in trained]
                others = [(p.get("level", 0), p.get("uniqueIndex")) for p in rest if p.get("petId") != GOLD_TIGER] \
                    or [(p.get("level", 0), p.get("uniqueIndex")) for p in rest]
                # 連金虎都沒有（身上只剩要練的人龍＋騎寵）：原本的戰寵就當成要練的那隻，練完不用換回
                # （11-07 第二、七組鎖定清成 0、身上只有人龍和金虎，「找不到原本的戰寵」連停，LESSONS #179）
                fight = max(others)[1] if others else target
            if level >= lv:
                continue
            # 騎寵可以是 0：卡坦 2019 要同時帶 4 隻，金虎得先存倉，這時候身上本來就沒有騎寵
            # （2026-10-24 第三組卡在這裡）。戰寵和要練的那隻一定要有。
            if not fight or not target:
                self.log(f"{name} 找不到原本的戰寵或要練的寵物（戰 {fight}、騎 {ride}、目標 {target}）"); return False
            self.orig_fight[name] = (fight, ride)
            self.train_uid[name] = target
            if not self.set_fight_pet(name, target, ride):
                return False
        return True

    def training_index(self, name: str, pid, lv: int) -> Optional[int]:
        """正在練的那隻在第幾格：開始時記下的唯一編號還在身上就用牠；不在（舊進度、被存倉）才照舊挑「還沒到級裡最高」。
        11-08 第六組每人身上 2 隻 1 級黑暴練 40：舊版用 pet_index(below=40)，練的那隻過 40 就改看另一隻 1 級的，
        永遠「沒到級」、地圖也照 1 級留在 31401，練到 57 才被我發現（LESSONS #184）。"""
        uid = self.train_uid.get(name)
        if uid:
            with self.lock:
                idx = next((i for i, p in self.petinfo[name].items() if p.get("uniqueIndex") == uid), None)
            if idx is not None:
                return idx
        return self.pet_index(name, pid, below=lv)

    def check_pet_training(self) -> bool:
        """練到等級的就停戰鬥、把戰寵改回原本的、再開遇敵（可以超過指定等級）。"""
        pid, lv = self.train_pet
        for name, (fight, ride) in list(self.orig_fight.items()):
            idx = self.training_index(name, pid, lv)
            with self.lock:
                level = self.petinfo[name].get(idx, {}).get("level", 0) if idx else 0
            if level < lv:
                continue
            self.log(f"{name} 的寵物 {pid} 到 {level} 級，戰寵改回 {fight}")
            if not self.pause() or not self.set_fight_pet(name, fight, ride) or not self.set_loop(True):
                return False
            del self.orig_fight[name]
            self.train_uid.pop(name, None)
            if not self.orig_fight:
                self.log(f"5 隻的寵物 {pid} 都到 {lv} 級以上了")
        return True

    def pet_route(self, s: State) -> Tuple[str, Optional[str]]:
        """練寵時的地圖：照練功路線門檻，但用「還在練的寵物裡最低等級」判斷；只往前、不退。
        pet_jump：門檻放寬幾級（跳級練，經驗多但寵物較常倒、要回村補血）。"""
        pid, lv = self.train_pet
        levels = []
        for name in self.orig_fight:
            idx = self.training_index(name, pid, lv)
            with self.lock:
                levels.append(self.petinfo[name].get(idx, {}).get("level", 0) if idx else 0)
        if not levels:
            return "ok", None
        low = min(levels) + max(0, int(self.pet_jump or 0))
        target = None
        for place, mapid, need in ROUTE:
            if need <= low and NAME_TO_MAP.get(self.pet_start or "", 0) and \
                    [m for _, m, _ in ROUTE].index(mapid) >= [m for _, m, _ in ROUTE].index(NAME_TO_MAP[self.pet_start]):
                target = (place, mapid)
        if target and target[1] != s.leader_map:
            cur = next((i for i, (_, m, _) in enumerate(ROUTE) if m == s.leader_map), -1)
            if [m for _, m, _ in ROUTE].index(target[1]) > cur:
                return "advance", target[0]
        return "ok", None

    def wounded(self) -> List[str]:
        """被打死過的角色：人物 hp 剩 1（送回記錄點）或有寵物 hp 0。"""
        with self.lock:
            return [n for _, n in self.party if self.hp.get(n) == 1
                    or any(p.get("hp") == 0 and p.get("petId", 0) > 0 for p in self.petinfo.get(n, {}).values())]

    def pet_name(self, pet: dict) -> str:
        if pet.get("name"):
            return pet["name"]
        if self._monsters is None:
            import json as _json
            import os as _os
            from ..features.gamedb.tables import CONFIGS
            with open(_os.path.join(CONFIGS, "Monster.json"), encoding="utf-8") as fp:
                self._monsters = {r["ID"]: r.get("nameTn", "") for r in _json.load(fp)}
        return self._monsters.get(pet.get("petId"), str(pet.get("petId")))

    def exp_buffs(self, name: str, now: Optional[float] = None) -> List[dict]:
        """還沒到期的經驗加成 buff：[{name, until}]（until＝到期 unix 秒；0＝無期限）。"""
        if self._exp_buffs is None:
            import json as _json
            import os as _os
            from ..features.gamedb.tables import CONFIGS
            with open(_os.path.join(CONFIGS, "RoleBuff.json"), encoding="utf-8") as fp:
                self._exp_buffs = {r["ID"]: r.get("nameTn", "") for r in _json.load(fp) if r.get("expBuffInfo")}
        now = time.time() if now is None else now
        return [{"name": self._exp_buffs[b], "until": t} for b, t in sorted(self.buffs.get(name, {}).items())
                if b in self._exp_buffs and (not t or t > now)]

    def snapshot(self) -> dict:
        """給 tg_watch 的完整資料（2026-10-22 使用者：通知要有人物與寵物的血量、等級；經驗加成）。戰寵＝狀態 2、騎寵＝狀態 3。"""
        with self.lock:
            roles = []
            for _, n in self.party:
                r = {"name": n, "lv": self.s.levels.get(n), "hp": self.hp.get(n), "hpMax": self.hp_max.get(n),
                     "exp": self.exp_buffs(n)}
                for key, st in (("fight", 2), ("ride", 3)):
                    p = next((p for p in self.petinfo.get(n, {}).values()
                              if p.get("status") == st and (p.get("petId") or 0) > 0), None)
                    r[key] = None if p is None else {"name": self.pet_name(p), "lv": p.get("level"),
                                                     "hp": p.get("hp"), "hpMax": p.get("hpMax")}
                # 2026-10-23 新增（只加不改，tg_watch 讀舊鍵）：轉數、該角色石幣
                r["trans"] = self.trans.get(n)
                r["stone"] = self.money.get(n, {}).get("stone")
                roles.append(r)
            # 活力、聲望、石幣都是帳號共用 → 取隊長的值（隊長還沒收到石幣就取第一個知道的；都不知道 None，不猜）。
            # 11-11 以前石幣五隻相加，查詢 API／TG／網頁的石幣都放大 5 倍（參考專案 發現，實測五隻同值，LESSONS #195）
            lead = self.money.get(self.party[0][1], {}) if self.party else {}
            stone = lead.get("stone")
            if stone is None:
                stone = next((r["stone"] for r in roles if r["stone"] is not None), None)
            money = {"gold": lead.get("gold"), "lockDenar": lead.get("lockDenar"), "stone": stone}
            last = None if self.last_battle is None else {"t": int(self.last_battle[0]), "win": self.last_battle[1]}
            return {"map": self.s.leader_map, "team": len(self.s.team), "loop": bool(self.s.loop_on),
                    "fast": all(self.fast.get(n) for _, n in self.party), "roles": roles,
                    "money": money, "lastBattle": last}

    INFO_EVERY = 60.0                            # 人物寵物：打開後每 60 秒印一次
    INFO_WINDOW = 300.0                          # 打開 5 分鐘後自動關

    # ---- /debug/fast：暫時關快戰（快戰時伺服器只回勝負、不送出招；2026-11-05 容器實跑）----
    def debug_fast_off(self, minutes: float) -> float:
        """查詢 API 呼叫：只記時間，封包由主迴圈 tick_debug 送（API 執行緒不送、不拿 Party.lock 送請求）。
        已經在關就從現在重算。回傳開回來的時間。"""
        self.debug_fast_until = time.time() + minutes * 60
        self.debug_fast_pending = True
        return self.debug_fast_until

    def debug_fast_stop(self) -> None:
        """提早結束：下一輪主迴圈就開回來。"""
        if self.debug_fast_until:
            self.debug_fast_until = min(self.debug_fast_until, time.time())

    def tick_debug(self) -> None:
        """主迴圈每輪呼叫：除錯時間到自動關；要關快戰就送；關快戰時間到照平常規則開回來（ensure_fast）。
        只在主迴圈（練功）呼叫，不在 nap／service：做任務時快戰是故意關的，不能在那裡被開回來。"""
        self.debug.tick()
        if self.debug_fast_pending:
            self.debug_fast_pending = False
            for _, n in self.party:
                if n in self.clients:
                    try:
                        self.clients[n].request("WaiGua_FastBattleHandle", {"flg": False})
                    except Exception as e:  # noqa: BLE001  除錯不能讓練功停下來
                        self.log(f"[debug] {n} 關快戰失敗：{type(e).__name__}: {e}")
            self.log(f"[debug] 暫時關快戰（看出招用），到 {time.strftime('%H:%M:%S', time.localtime(self.debug_fast_until))}"
                     " 自動開回來")
        if self.debug_fast_until and time.time() >= self.debug_fast_until:
            self.debug_fast_until = 0.0
            bad = self.ensure_fast()
            self.log(f"[debug] 暫時關快戰時間到，開回來{'' if not bad else f'（沒確認：{bad}）'}")

    def start_info_log(self) -> float:
        """查詢 API `POST /infolog` 呼叫（使用者 2026-11-04「打 api 開啟時，每一分鐘會打印一次，五分鐘後自動關閉」）。
        只改兩個時間，不送封包；主迴圈下一輪就印第一次。已經開著再打＝從現在重算 5 分鐘。回傳關閉時間。"""
        now = time.time()
        self.info_next = now
        self.info_until = now + self.INFO_WINDOW
        return self.info_until

    def tick_info_log(self) -> None:
        """主迴圈每輪呼叫：開著、到時間就印一次；過了 5 分鐘記一行關閉。"""
        if not self.info_until:
            return
        now = time.time()
        if now >= self.info_until:
            self.info_until = 0.0
            self.log("人物寵物：5 分鐘到，停止定時印")
            return
        if now >= self.info_next:
            self.info_next = now + self.INFO_EVERY
            self.log_info()

    def log_info(self) -> None:
        """每隻一行人物、戰寵、騎寵的等級與血量。"""
        def hp(x):
            return f"{x.get('hp', '?')}/{x.get('hpMax', '?')}"
        for r in self.snapshot()["roles"]:
            pets = "｜".join(f"{label} " + (f"{x['name']} Lv{x['lv']} 血 {hp(x)}" if x else "無")
                            for label, x in (("戰寵", r["fight"]), ("騎寵", r["ride"])))
            self.log(f"人物寵物：{r['name']} Lv{r['lv']} 血 {hp(r)}｜{pets}")

    def log_snapshot(self, extra: Optional[dict] = None) -> None:
        """extra：定時那份加上 wins／losses（近 5 分鐘）與 judge，一則通知就有全部狀態。"""
        import json as _json
        d = {**self.snapshot(), **(extra or {})}
        self.log("快照：" + _json.dumps(d, ensure_ascii=False, separators=(",", ":")))   # 單行，tg_watch 才不會拆開

    def hp_lows(self) -> Tuple[Optional[int], Optional[int]]:
        """(人物最低 %, 戰寵＋騎寵最低 %)；不知道的是 None（不猜 100）。"""
        def low(pairs):
            v = [hp * 100 // mx for hp, mx in pairs if hp is not None and mx]
            return min(v) if v else None
        snap = self.snapshot()["roles"]
        pets = [(r[k]["hp"], r[k]["hpMax"]) for r in snap for k in ("fight", "ride") if r[k]]
        return low((r["hp"], r["hpMax"]) for r in snap), low(pets)

    def hp_percent(self) -> Optional[int]:
        """給 supervisor 的隊伍最低血量：人物與寵物兩者較小值；都不知道就 None。"""
        v = [x for x in self.hp_lows() if x is not None]
        return min(v) if v else None

    def hp_summary(self) -> str:
        """狀態行用：人物、寵物（戰寵＋騎寵）各自最低的血量百分比；不知道就寫 ?。"""
        role, pet = self.hp_lows()
        def fmt(x):
            return "?" if x is None else f"{x}%"
        return f"人物最低 {fmt(role)}／寵物最低 {fmt(pet)}"

    def heal_and_return(self, why: Optional[str], place: Optional[str]) -> bool:
        """使用者 2026-10-22：隊伍血量 50% 以下優先回去補血，補完要發通知。
        停戰鬥 → 回村補血 → 傳回原本的練功地點 → 5 隻快戰 → 開遇敵。任何一步失敗就停（回 False）。"""
        with self.lock:
            self.heals += 1
            n = self.heals
            self.heal_times.append(time.time())
            self.heal_times[:] = [t for t in self.heal_times if time.time() - t <= 300]
        self.log(f"補血：開始（{why}，第 {n} 次），練功地點 {place or '不知道'}")
        if not place:
            self.log("停止：補血：不知道原本的練功地點，補完血傳不回去"); return False
        if not self.pause():
            self.log("停止：補血：停不了戰鬥"); return False
        if not self.heal_at_samugil():
            self.log("停止：補血失敗"); return False
        self.shells.on_village()                         # 回村順便存殼、滿 10 兌換、用券（失敗不影響補血流程）
        if not self.teleport_team(place):
            self.log(f"停止：補血：傳回 {place} 失敗"); return False
        bad = self.ensure_fast()
        if bad:
            self.log(f"停止：補血：快速戰鬥沒開成 {bad}"); return False
        if not self.set_loop(True):
            self.log("停止：補血：遇敵沒開成"); return False
        self.log(f"補血：完成，全隊回滿，已回到 {place}")
        self.log_snapshot()                              # 讓使用者直接看到五隻和寵物的血量確實回滿
        return True

    # ---- 工作佇列（doc/autoplay/flows/jobs.md「工作佇列」）----
    def resume_training(self, place: Optional[str]) -> bool:
        """做完工作回練功：組隊 → 傳回原本的練功地圖 → 5 隻快戰 → 開原地遇敵。任何一步失敗回 False（呼叫端停）。"""
        if not self.form_team():
            self.log("回練功：組隊失敗"); return False
        self.ensure_fight_pets()
        if place and not self.teleport_team(place):
            self.log(f"回練功：傳回 {place} 失敗"); return False
        bad = self.ensure_fast()
        if bad:
            self.log(f"回練功：快速戰鬥沒開成 {bad}"); return False
        if not self.set_loop(True):
            self.log("回練功：遇敵沒開成"); return False
        return True

    def ensure_fight_pets(self) -> List[str]:
        """回練功前：沒有戰寵（沒鎖定、也沒有出戰中的）的角色，自動換上最強的戰寵（pets equip-fight）。
        11-06 兩組卡坦抓寵失敗（石幣不夠買誘餌）：劇本前一件 `pets park` 已經把戰寵改休息、鎖定清掉，
        回地雷區 Lv130 等於空手打，五隻一起倒、連敗。換不了只記日誌，照舊回練功。回傳換過的角色。"""
        from .jobs.pets import do_equip_fight
        done = []
        for _, n in self.party:
            if self.locks(n)[0]:
                continue
            try:
                do_equip_fight(self, n)
                done.append(n)
            except Exception as e:  # noqa: BLE001
                self.log(f"回練功：{n} 沒有戰寵，自動換戰寵失敗（{e}）")
        if done:
            self.log(f"回練功：{'、'.join(done)} 沒有戰寵，已換上最強的")
        # 騎寵也補：0 轉沒有金虎、原本騎瑪斯貝卡的組，卡坦 park 後 Pod 重開就忘了原騎寵（11-06 英雄島龍王守衛連四次陣亡）
        from .jobs.pets import ensure_ride
        for _, n in self.party:
            try:
                got = ensure_ride(self, n)
                if got:
                    self.log(f"回練功：{n} 沒有騎寵，已騎上 {self.pet_name(got)}（{got.get('level')} 級）")
            except Exception as e:  # noqa: BLE001
                self.log(f"回練功：{n} 沒有騎寵，自動騎寵失敗（{e}）")
        return done

    def process_job(self, place: Optional[str]) -> Optional[str]:
        """取佇列裡最舊的一件來做。沒有工作回 None；做了回結果狀態（ok／failed／hold）；回練功失敗回 "fatal"。

        - 工作要停練功（PAUSE）：run_job 先 pause()（關遇敵、等這場打完），做完 resume_training(place)。
        - JobError → failed，照常回練功；JobHold 或工作程式例外（狀態不明）→ hold，停在原地不回練功。
        - 連線斷了／被擠下線 → failed、不回練功；主迴圈下一輪看到就停。
        - 停在原地（hold）中也收工作（例：先 clean 整理再 resume）；只有 resume 清掉 hold 後才回練功。
        - 做完時佇列裡還有下一件（例：劇本一步裡的多件工作、自動練寵後重跑 quests）：**先不回練功**（記在 carry），
          接著做下一件，最後一件做完才回；下一件被取消、佇列空了也會在這裡回練功。
        - 工作的 data 帶 `resume_place`（例 after-trans 已經傳到低等圖）：回練功改去那張圖，不是工作前那張。
        - 有劇本（`self.scenario`）：結果先交給劇本（記進度、需要的話馬上排下一件），再決定要不要回練功。
        """
        if self.jobq is None:
            return None
        job = self.jobq.take()
        if job is None:
            if self.carry is not None and not self.hold:
                place = self.carry.get("place")
                self.carry = None
                if not self.resume_training(place):
                    self.log("停止：工作都做完了，回練功失敗"); return "fatal"
                self.log(f"工作都做完了，已回練功（{place or '原地'}）")
            return None
        from .jobs import JobError, JobHold, pauses, run_job
        from .jobs.__main__ import parse
        carried = self.carry
        self.carry = None
        if carried is not None:
            place = carried.get("place")             # 上一件做完沒回練功：回練功的目標沿用上一件之前的地圖
        if self.hold and self.hold.get("place"):
            place = self.hold["place"]               # 停在原地中：回練功的目標是進 hold 前的練功地圖
        was_hold = bool(self.hold)
        started = time.time()
        # 巢狀：練寵工作（train）在自己的練功迴圈裡也會收佇列的下一件；做完要把 self.job 還給外層，
        # 不能清成 None，否則劇本看到「外層工作在 running、常駐卻沒在做工作」就停（11-04 第七、八組，LESSONS #155）
        outer = self.job
        self.job = {"id": job["id"], "name": job.get("name"), "since": started}
        self.log(f"工作開始：{' '.join(job.get('argv') or [])}（{job['id']}）")
        status, detail, data, paused = "failed", "", {}, False
        try:
            mod, args = parse(job.get("argv") or [])
            if str(args.profile) != str(self.profile):
                raise JobError(f"工作是第 {args.profile} 組的，這裡是第 {self.profile} 組")
            paused = pauses(mod, args)
            detail, data = run_job(mod, args, self, login=False)
            status = "ok"
        except JobHold as e:
            status, detail = "hold", str(e)
        except JobError as e:
            detail = str(e)
        except Exception as e:  # noqa: BLE001  工作程式出錯：送到一半狀態不明，停在原地等人
            status, detail = "hold", f"例外 {type(e).__name__}: {e}"
        data = data if isinstance(data, dict) else {"data": data}
        if status == "ok" and isinstance(data.get("resume_place"), str) and data["resume_place"] in NAME_TO_MAP:
            place = data["resume_place"]             # 工作自己換了練功地圖（after-trans）：回練功去那裡
        broken = [n for n, c in self.clients.items() if getattr(c, "closed", False)]
        with self.lock:
            kicked = self.s.kicked
        if status == "hold":
            self.hold = {"reason": f"{job.get('name')}：{detail}"[:300], "place": place, "since": time.time()}
            if self.s.loop_on and not broken and not kicked:
                self.pause()                         # 沒停練功的工作也一樣：停在原地，不再打
            detail += "｜停在原地（hold）：不回練功，處理完排 `jobs add resume` 或重啟常駐"
        if self.scenario is not None:
            self.scenario.on_result(self, job, status, detail)   # 劇本記結果、需要的話馬上排下一件
        rc: Optional[str] = status
        if broken or kicked:
            detail += f"｜連線中斷或被擠下線（{'、'.join(broken) or '被擠'}），不回練功"
        elif not self.hold and (paused or was_hold or carried is not None) and self.jobq.pending():
            self.carry = {"place": place}
            detail += "｜佇列還有下一件，先不回練功"
        elif not self.hold and (paused or was_hold or carried is not None):
            if self.resume_training(place):
                detail += f"｜已回練功（{place or '原地'}）"
            else:
                detail += "｜回練功失敗，常駐停止"
                rc = "fatal"
        self.jobq.finish(job, status, detail, data, started)
        self.log(f"工作結束：{job.get('name')} {status}：{detail}")
        self.job = outer
        return rc

    def run(self, start_place: Optional[str], interval: float = 15.0) -> int:
        self.login_all()
        self.nap(3)
        if self.state().kicked:
            self.log("停止：登入後有角色被踢下線"); return 1
        return self.train_loop(start_place, interval)

    def train_loop(self, start_place: Optional[str], interval: float = 15.0,
                   until: Optional[Callable[[], bool]] = None) -> int:
        """登入之後的練功：組隊 → 補血 → 開局規範 → 傳送 → 快戰 → 遇敵 → 主迴圈（`run()` 的後半段）。
        `until`：每輪檢查完練寵之後呼叫，成立就關遇敵、回 0（固定指令 `train` 依序練寵用）；不給就一直練到停止。"""
        if not self.form_team():
            self.log("停止：組隊失敗"); return 1
        # 補血要在開局規範之前：被打死的人物 hp 1、寵物倒下就進不了出戰／騎乘狀態，戰寵設定永遠確認不了
        # （2026-10-22 第三組 Mac 實機）。指定 --heal 或登入時有人帶傷就先補。
        if (self.heal_first or self.wounded()) and not (self.pause() and self.heal_at_samugil()):
            self.log("停止：補血失敗"); return 1
        if self.heal_first or self.s.leader_map in (1000, 1005):
            self.shells.on_village()                     # 人在村裡（剛補完血）：順便存殼／兌換／用券
        if not self.init_baseline():                     # 開局基礎規範 1～5、7；戰寵騎寵設不齊等就停
            self.log("停止：開局基礎規範沒做完"); return 1
        bad = self.apply_waigua()                        # 設定檔的外掛設定（沒用設定檔不做）
        if bad:                                          # 規範第 2 點：出錯就停，不跳過繼續
            self.log(f"停止：外掛設定沒設成 {bad}"); return 1
        if start_place and not self.teleport_team(start_place):
            self.log(f"停止：傳送到 {start_place} 失敗"); return 1
        # 順序（使用者）：先到練功地圖 → 5 隻都開快速戰鬥並確認 → 才開隊長原地遇敵
        bad = self.ensure_fast()
        if bad:
            self.log(f"停止：快速戰鬥沒開成 {bad}"); return 1
        if self.train_pet and not self.start_pet_training():          # 遇敵開之前換戰寵（狀態變更先停戰鬥）
            self.log("停止：戰寵換不成"); return 1
        self.sign_in_daily()                     # 遇敵還沒開＝一定不在戰鬥中，是最安全的簽到時機
        # 啟動時的完整資料：主迴圈第一圈就寫第一筆狀態＋快照（last_report=0），這裡不另寫，否則一開始會連推兩則一樣的
        if not self.set_loop(True):
            self.log("停止：遇敵沒開成"); return 1
        names = [n for _, n in self.party]
        down: set = set(self.wounded())                  # 已經記過快照的倒下角色
        fails, last_report, last_move = 0, 0.0, time.time()
        last_kind: Optional[str] = None              # 上一次換圖是 advance 還是 retreat（冷卻只跟在退圖後面）
        held: Optional[Tuple[str, Optional[str]]] = None    # 上一輪被擋下的換圖動作（只記一次日誌）
        place: Optional[str] = start_place
        while True:
            if self.stop_requested():
                self.log("收到停止檔，正常登出結束"); return 0
            dead =[n for n, c in self.clients.items() if getattr(c, "closed", False)]
            if dead:
                self.log(f"停止：連線中斷（{'、'.join(dead)}）"); return 1
            if self.jobq is not None:
                with self.lock:
                    lm = self.s.leader_map
                place = next((n for n, m, _ in ROUTE if m == lm), place)
                r = self.process_job(place)              # 一輪最多一件；做完下一輪重新看狀態
                if r == "fatal":
                    return 1
                if self.hold:
                    # 停在原地等人：不監管（不換圖、不判斷「不在練功地圖」），只收工作與停止檔
                    if self.state().kicked:
                        self.log("停止：被擠下線"); return 1
                    self.tick_info_log()
                    self.tick_debug()
                    self.nap(interval if r is None else 1)
                    continue
                if r is not None:
                    continue
                if self.scenario is not None and self.job is None and not self.jobq.pending():
                    # 劇本：佇列空、沒有 hold、不在工作裡（練寵工作裡的巢狀練功不算）才往下看；
                    # 只排工作、寫進度，不送封包。排了的工作下一輪由 process_job 做
                    self.scenario.tick(self)
            if self.train_pet and not self.check_pet_training():
                self.log("停止：戰寵換不回來"); return 1
            if until is not None and until():
                if not self.pause():
                    self.log("停止：條件達成，但關不掉遇敵"); return 1
                self.log("條件達成，關遇敵結束這段練功"); return 0
            s = self.state()
            now_down = set(self.wounded())
            if now_down - down:                          # 有人新倒下（人物 hp 1 或寵物 hp 0）→ 再送一次完整快照
                self.log(f"有人倒下：{'、'.join(sorted(now_down - down))}")
                self.log_snapshot()
            down = now_down
            act, arg = decide(s, names)
            if self.train_pet and act in ("ok", "advance", "retreat"):
                # 練寵時換圖看寵物等級，不看人物等級（人物 110 會想去波拉島，低等寵物上場就被秒）。
                # "ok" 也要問：常駐帶 --stay 時 decide 永遠回 ok，舊版練寵就一直停在起點圖
                # （11-02 第六組金豬 55 級還在海主人隧道Lv30，20 分鐘只升 5 級）。
                act, arg = self.pet_route(s)
            if time.time() - last_report >= REPORT_EVERY:
                last_report = time.time()
                lv = "、".join(f"{n}{s.levels.get(n, '?')}" for n in names)
                self.log(f"狀態：地圖 {s.leader_map}｜等級 {lv}｜隊伍 {len(s.team)} 人｜近 5 分鐘 勝 {s.wins}／敗 {s.losses}"
                         f"｜遇敵 {'開' if s.loop_on else '關'}"
                         f"｜快戰 {'開' if all(self.fast.get(n) for n in names) else '關'}｜血量 {self.hp_summary()}"
                         f"｜判斷 {act}")
                # 使用者：固定通知（含第一次）都要有人物、寵物的完整狀態
                self.log_snapshot({"wins": s.wins, "losses": s.losses, "judge": act})
            self.tick_info_log()
            self.tick_debug()
            if act == "stop":
                self.log(f"停止：{arg}"); return 1
            if self.signin_day != time.strftime("%Y-%m-%d") and act in ("ok", "wait"):
                # 常駐跨過 0 點：先關遇敵把這場打完再簽（簽到不能在戰鬥中送），簽完重開。
                # 一天只會進來一次（sign_in_daily 內部用日期擋），不影響練功節奏。
                if self.pause():
                    self.sign_in_daily()
                    self.set_loop(True)
                else:
                    # 關遇敵失敗就今天不簽：不然每一圈都會再試一次關遇敵，整個練功節奏會被拖垮
                    self.signin_day = time.strftime("%Y-%m-%d")
                    self.log("簽到：關不掉遇敵，今天跳過")
            ready = self.points_ready_for_recommend() if act in ("ok", "wait") else []
            if ready and self.pause():                   # 改設定前先把這場打完（同簽到）
                self.switch_to_recommend(ready)
                self.set_loop(True)
            here = next((n for n, m, _ in ROUTE if m == s.leader_map), None)
            if here:
                place = here                            # 最後一次在的練功地點（補完血要傳回來）
            if act in ("ok", "wait") and self.shells.store_due():
                # 背包快滿（空格 ≤2）才原地停遇敵存殼；沒快滿不停，殼留在背包等回村（使用者 2026-10-24「不然太浪費時間」）
                if self.shells.store_in_place() == "fatal":
                    return 1
            if act in ("ok", "wait"):
                self.autoclean_if_full() or self.autorefresh_pets() or self.autospirit() or self.autofruit()
            if s.hp_percent is not None and s.hp_percent >= 50:
                with self.lock:
                    self.heals = 0
            if act == "heal":
                if not self.heal_and_return(arg, place):
                    return 1
                self.nap(interval)
                continue
            # 換圖冷卻 240 秒只擋「退圖」與「剛退圖後又前進」（防來回打轉）；正常前進不冷卻：
            # 1 轉後 1 級＋133 級戰寵幾十秒就升好幾級，舊版每換一張圖都白等 3～4 分鐘（使用者 2026-10-23：「為什麼練功這麼沒效率」）
            cooling = time.time() - last_move < 240 and (act == "retreat" or last_kind == "retreat")
            if act in ("retreat", "advance") and (self.hold_advance(act, arg, s) or cooling):
                # 這輪不會真的換圖 → 不停戰鬥、不重開遇敵，同一個動作只記一次。舊版先 pause() 再判斷，
                # 冷卻 240 秒內每輪都關遇敵等 6 秒，第三組 30 分鐘裡約 12 分鐘在空轉（2026-10-22 Mac 診斷）
                if held != (act, arg):
                    held = (act, arg)
                    self.log(f"想{'前進' if act == 'advance' else '退回'} {arg}，剛換過地圖或等級還不夠，先繼續練")
                self.nap(interval)
                continue
            held = None
            if act not in ("ok", "wait"):
                self.log(f"動作：{act} {arg or ''}")
                ok = True
                if act in ("fetch", "team", "retreat", "advance"):
                    ok = self.pause()
                if ok and act == "fetch":
                    i = next(i for i, (_, m, _) in enumerate(ROUTE) if m == s.leader_map)
                    ok = self.transfer(arg, NAME_TO_POINT[ROUTE[i][0]], s.leader_map) and self.form_team()
                elif ok and act == "team":
                    ok = self.form_team()
                elif ok and act in ("retreat", "advance"):     # 被擋下的情況上面已經跳過
                    ok = self.teleport_team(arg); last_move, last_kind = time.time(), act
                    with self.lock:
                        self.battles.clear()
                        # 補血時間戳也要清（supervisor：「退完由呼叫端清掉補血時間戳」）：舊版沒清，
                        # 柯奧4F 補的 4 次留在 5 分鐘窗內，退到五兄弟又想再退海主人Lv30（2026-11-04 容器實跑）
                        self.heal_times.clear()
                if ok and act in ("retreat", "advance", "fetch"):
                    bad = self.ensure_fast()             # 換到練功地圖後再確認 5 隻快速戰鬥
                    if bad:
                        self.log(f"快速戰鬥沒開成 {bad}"); ok = False
                if ok:
                    ok = self.set_loop(True)
                fails = 0 if ok else fails + 1
                if fails >= 3:
                    self.log("停止：連續 3 次動作失敗"); return 1
            self.nap(interval)

    def close(self) -> None:
        """正常登出：先關遇敵，每個角色送 Role_Exit 再斷線，不直接砍連線。
        2026-10-21 實機：伺服器不回 Role_Exit 的 response，之後不再往該連線送資料，
        其他連線收到該角色 isAfk:true（變成離線掛機狀態）。所以只送不等，留 1 秒讓伺服器處理。"""
        if self.clients:
            try:
                self.leader().send("Role_UnLoopBattle", {})
            except Exception as e:  # noqa: BLE001
                self.log(f"關遇敵失敗：{e}")
        # 練寵中途結束：戰寵改回原本的，免得之後一般練功還是讓低等寵物上場
        for name, (fight, ride) in list(self.orig_fight.items()):
            try:
                self.clients[name].request("WaiGua_NomalSettingHandlePet", {"fightPetIndex": fight, "ridePetIndex": ride})
                self.log(f"{name} 戰寵改回 {fight}")
            except Exception as e:  # noqa: BLE001
                self.log(f"{name} 戰寵改不回去：{e}")
        for name, c in self.clients.items():
            try:
                c.send(LOGOUT, {})
                time.sleep(LOGOUT_GRACE)
                self.log(f"{name} 已登出")
            except Exception as e:  # noqa: BLE001
                self.log(f"{name} 登出送不出去（{e}），直接斷線")
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def _same_value(now, want) -> bool:
    """設定檔 raw 的值跟伺服器推送的現值一不一樣。sproto 陣列推來是 {索引: 值}（見 autoPointDiyList），
    照索引順序轉成串列再比；布林只跟布林／0、1 比（不用 str，免得 True 對 "True" 這類巧合）。"""
    if isinstance(want, list):
        if isinstance(now, dict):
            now = [now[k] for k in sorted(now, key=lambda k: int(k))]
        return isinstance(now, (list, tuple)) and [int(x) for x in now] == [int(x) for x in want]
    if isinstance(want, bool):
        return now in (True, 1) if want else now in (False, 0, None)
    if isinstance(want, int):
        return not isinstance(now, bool) and isinstance(now, int) and now == want
    return str(now) == str(want)


def parse_point_plan(text: str) -> dict:
    """「100,100,999/80,80,999」→ {"lead": (100,100,999), "member": (80,80,999)}（體, 敏, 攻）。"""
    parts = [p.strip() for p in (text or "").split("/")]
    if len(parts) != 2:
        raise ValueError(f"要寫 隊長體,敏,攻/隊員體,敏,攻：{text!r}")
    out = {}
    for key, part in zip(("lead", "member"), parts):
        try:
            vals = tuple(int(x) for x in part.split(","))
        except ValueError:
            raise ValueError(f"要是數字：{part!r}") from None
        if len(vals) != 3 or any(not 0 <= v <= 999 for v in vals):
            raise ValueError(f"每組三個 0～999 的數字（體,敏,攻）：{part!r}")
        out[key] = vals
    return out


class _Stdout:
    """`--log -`：每行直接印到 stdout 並 flush（被 pipe 時才不會卡在緩衝區）。"""

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x7k2qm.autoplay.headless_party")
    ap.add_argument("--log", required=True, help="日誌檔；「-」＝只印到 stdout、不寫檔（容器用，使用者 11-04）")
    ap.add_argument("--stop-file", default=None, help="停止檔（看到就在兩場戰鬥之間登出）；預設 <--log>.stop，--log - 時必填")
    ap.add_argument("--start", default=None, help="登入後先傳到哪個練級點（例：波拉島Lv120）；不給就留在原地")
    ap.add_argument("--stay", action="store_true",
                    help="鎖在這張圖：**不退圖也不前進**（補血、組隊、接人照舊）。"
                         "用在「下一張較低的圖已經沒經驗」或練寵時（使用者 2026-10-22；"
                         "2026-10-23 加上不前進，練寵被帶去高等圖寵物會被秒）")
    ap.add_argument("--no-retreat", action="store_true",
                    help="不允許退回上一張圖（前進照舊）。使用者 2026-10-23：轉生後血薄、"
                         "退圖只會在低經驗圖空轉")
    ap.add_argument("--keep-pets", action="store_true",
                    help="使用者指定的戰寵／騎寵不自動換（滿 140 也不排 pets refresh）。使用者 2026-11-02 第六組布魯頓")
    ap.add_argument("--train-pet", default=None, help="練寵模式 petId:等級（例：14:77）；戰寵改成它，到等級就改回原本的")
    ap.add_argument("--heal", action="store_true", help="開始前先回薩姆吉爾村醫院補血（全隊人物＋寵物）")
    ap.add_argument("--profile", default="2", help="第幾組帳號（環境變數 BOT_ACCOUNT_<組>）；每組一個行程、一個 pcId")
    ap.add_argument("--discover", action="store_true", help="角色清單從登入結果取（第 2 組預設用寫死的 5 隻）")
    ap.add_argument("--api-port", type=int, default=None,
                    help="開本機唯讀查詢 API（只聽 127.0.0.1；建議第 2 組 8791、第 N 組 8789+N）；不給就不開")
    ap.add_argument("--auto-fruit", action="store_true",
                    help="練功中經驗果加成沒了就自己吃（背包、倉庫有就吃，沒有就用石幣在商城石幣分頁買 1,000,000／顆，留 100 萬石幣）")
    ap.add_argument("--auto-spirit", action="store_true",
                    help="隊長依人物屬性去競技場買精靈金幣（500 石幣），60 級才穿、首次動作設好（容器版預設開）")
    ap.add_argument("--point-plan", default=None,
                    help="外掛自訂加點：隊長體,敏,攻/隊員體,敏,攻（例 100,100,999/80,80,999；攻 999＝剩下全加攻擊）")
    ap.add_argument("--battle-heal", type=int, default=None,
                    help="外掛戰鬥中技能補血：人物／寵物／騎寵血量低於幾 %% 放身上的補血技能（1～99；容器版預設 50）")
    ap.add_argument("--api-host", default="127.0.0.1",
                    help="查詢 API 聽的位址；不是本機（例 0.0.0.0，容器對外）時一定要設環境變數 BOT_API_TOKEN")
    ap.add_argument("--shell-exchange", dest="shell_exchange", action="store_true", default=True,
                    help="千年龜的殼：背包快滿原地存公共倉庫；回村（補血）時順便存，共倉滿 10 隊長兌換 4013 並用掉石幣券（預設開）")
    ap.add_argument("--no-shell-exchange", dest="shell_exchange", action="store_false", help="關掉龜殼存倉／兌換")
    ap.add_argument("--jobs-dir", default=None,
                    help="工作佇列目錄（預設 BOT_JOBS_DIR 或 captures/jobs）；`jobs add` 排的工作在兩場戰鬥之間做")
    ap.add_argument("--owner-dir", default=None, help="帳號擁有者鎖目錄（預設 BOT_OWNER_DIR 或 captures/owner）")
    ap.add_argument("--scenario", default=None,
                    help="劇本檔（例 config/scenarios/rebirth.toml）：佇列空時照劇本往下排工作，進度記在 "
                         "captures/scenario/<組>.json（doc/autoplay/flows/scenario.md）")
    ap.add_argument("--scenario-dir", default=None, help="劇本進度目錄（預設 BOT_SCENARIO_DIR 或 captures/scenario）")
    ap.add_argument("--no-auto-spirit", dest="auto_spirit", action="store_false",
                    help="不買／不穿隊長屬性精靈（設定檔開了 auto_spirit 時用來關）")
    ap.add_argument("--config", default=None,
                    help="設定檔（TOML，格式見 x7k2qm/autoplay/botconfig.py、config/bot.toml）：[bot] 參數、[files] 劇本／"
                         "規則表、[waigua] 外掛設定（每次登入檢查）。命令列有給的參數照命令列")
    ap.add_argument("--set", action="append", default=[], metavar="區塊.鍵=值",
                    help="覆寫設定檔的一項（可多次；沒寫區塊＝bot），例 --set waigua.battle_heal=60 --set start=波拉島Lv120")
    return ap


def load_config(ap: argparse.ArgumentParser, argv=None, set_env: bool = True):
    """解析命令列；有 --config 就讀設定檔、當成參數預設值再解析一次（命令列照樣優先）。回傳 (args, Config 或 None)。
    設定檔不合法丟 botconfig.ConfigError（還沒登入、什麼都沒送）。set_env=False：只檢查（容器 --check），不改環境變數。"""
    from . import botconfig
    args = ap.parse_args(argv)
    cfg = None
    if args.config:
        cfg = botconfig.load(args.config, ap, args.set)
        ap.set_defaults(**cfg.bot, scenario=cfg.scenario)
        args = ap.parse_args(argv)
        botconfig.check_overlap(cfg, args.battle_heal, cfg.waigua.first_cmd)
        if cfg.item_rules and set_env:
            os.environ[botconfig.RULES_ENV] = cfg.item_rules       # 工作在同一個行程裡讀規則表
    elif args.set:
        raise botconfig.ConfigError("--set 要搭配 --config")
    if args.scenario and args.scenario.strip().lower() == "none":
        args.scenario = None
    return args, cfg


def main(argv=None) -> int:
    from .botconfig import ConfigError
    ap = build_parser()
    try:
        args, cfg = load_config(ap, argv)
    except ConfigError as e:
        print(f"停止：設定檔：{e}", flush=True)
        return 1
    args.config_obj = cfg
    if args.log == "-":
        if not args.stop_file:
            ap.error("--log - 要搭配 --stop-file")
        fp = _Stdout()
    else:
        fp = RotatingFile(args.log, max_bytes=5 * 1024 * 1024, keep=10)
    args.stop_file = args.stop_file or args.log + ".stop"

    def log(msg: str) -> None:
        fp.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    from . import jobqueue, owner
    jdir = args.jobs_dir or jobqueue.default_dir()
    odir = args.owner_dir or owner.default_dir()
    if args.scenario:                            # 劇本格式錯就不登入（什麼都還沒送）
        from .scenario import ScenarioError, load
        try:
            args.scenario_obj = load(args.scenario)
        except ScenarioError as e:
            log(f"停止：劇本不合法：{e}")
            return 1
    try:                                         # 登入前先拿帳號擁有者鎖：別的程式在用這組就不登入（不互相擠下線）
        owner.acquire(odir, args.profile, owner.KIND_PARTY, api_port=args.api_port, jobs_dir=jdir, log=log)
    except owner.OwnerBusy as e:
        log(f"停止：{e}")
        return 1
    try:
        return _main_owned(args, log, jdir)
    finally:
        owner.release(odir, args.profile)


def _main_owned(args, log, jdir: str) -> int:
    from .jobqueue import Queue
    log(f"無畫面五開開始（第 {args.profile} 組帳號）")
    p = Party(log, stop_file=args.stop_file, profile=args.profile, discover=args.discover)
    p.jobq = Queue(jdir, args.profile)
    sc = getattr(args, "scenario_obj", None)
    if sc is not None:
        from .scenario import Progress, Runner, ScenarioError, default_dir
        p.scenario = Runner(sc, Progress(args.scenario_dir or default_dir(), args.profile), p.jobq, args.profile, log)
        try:
            p.scenario.start()
        except ScenarioError as e:
            log(f"停止：劇本進度檔：{e}")
            return 1
    if args.train_pet:
        pid, lv = map(int, args.train_pet.split(":"))
        p.train_pet = (pid, lv)
    p.heal_first = args.heal
    p.stay = args.stay
    p.keep_pets = args.keep_pets
    p.no_retreat = args.no_retreat
    p.auto_spirit = args.auto_spirit
    p.auto_fruit = p.fruit_buy = bool(getattr(args, "auto_fruit", False))
    if args.battle_heal is not None:
        if not 1 <= args.battle_heal <= 99:
            log("停止：--battle-heal 要在 1～99")
            return 1
        p.battle_heal = args.battle_heal
    if args.point_plan:
        try:
            p.point_plan = parse_point_plan(args.point_plan)
        except ValueError as e:
            log(f"停止：--point-plan：{e}")
            return 1
    p.shells.enabled = args.shell_exchange
    p.pet_start = args.start if args.train_pet else None
    cfg = getattr(args, "config_obj", None)
    if cfg is not None:
        p.waigua = cfg.waigua
        w = cfg.waigua
        log(f"設定檔 {cfg.path}：起點 {args.start or '原地'}｜劇本 {args.scenario or '不跑'}｜"
            f"規則表 {cfg.item_rules or '內建'}｜外掛 隱藏 {w.hide}、快戰 {w.fast_battle}、首次動作 {w.first_cmd}、"
            f"加點 {args.point_plan or w.auto_points}、補血 {args.battle_heal or '不設'}"
            + (f"、協定直設 {sorted(k[7:] for k in w.raw)}" if w.raw else ""))
    api = None
    if args.api_port is not None:
        from .party_api import TOKEN_ENV, serve
        try:
            api = serve(p, args.api_port, host=args.api_host, token=os.environ.get(TOKEN_ENV) or None)
            log(f"{'本機' if args.api_host in ('127.0.0.1', 'localhost') else '對外'}查詢 API："
                f"http://{args.api_host}:{api.server_address[1]}/state（唯讀"
                f"{'，要 token' if os.environ.get(TOKEN_ENV) else '，只聽本機'}）")
        except (OSError, ValueError) as e:       # port 被佔用、對外沒 token：只記日誌，不影響練功
            log(f"查詢 API 開不起來（{args.api_host}:{args.api_port}）：{e}")
    try:
        return p.run(args.start)
    except Exception as e:  # noqa: BLE001
        log(f"停止：例外 {type(e).__name__}: {e}")
        return 1
    finally:
        if api is not None:
            api.shutdown()
        p.close()


if __name__ == "__main__":
    sys.exit(main())
