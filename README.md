# x7k2qm

無畫面自動練功容器：啟動時帶入帳號密碼，程式自己登入帳號底下的五個角色、組隊、練功，
等級到了自動解任務、轉生，再從低等地圖重新練起。另外開一個唯讀查詢 API，隨時查人物、寵物、道具等狀態。

## 功能

- **新手開局**：1 級角色自動開新手禮盒、吃經驗果、穿裝備、存綁定道具、設定戰寵騎寵。
- **外掛自訂加點**：隊長 體 100、敏 100、剩下全加攻擊；隊員 體 80、敏 80、剩下全加攻擊（`POINT_PLAN` 可改）。
- **戰鬥中補血**：開局穿好裝備後，外掛設成人物／寵物／騎寵血量低於 50% 就放身上的補血技能（新手鎧甲附滋潤的精靈；`BATTLE_HEAL` 可改）。隊伍血量低於 50% 另外會自動回村醫院補滿。
- **隊長屬性精靈**：看隊長是地／水／火／風，去競技場道具屋買對應的 Lv5 金幣（500 石幣）；60 級才能穿，先放背包，到 60 級自動穿上並把首次動作設成放這個精靈。
- **練功**：傳到練功地圖、開快速戰鬥與原地遇敵；全隊到門檻自動換下一張圖，有人受傷自動回村補血。
  5 分鐘內補血 6 次以上，或打了 8 場以上輸兩成，就退回上一張圖；退圖後重新計算補血次數，比退圖時多 3 級或過了 20 分鐘就再往前。
- **劇本**（`config/scenarios/rebirth.toml`）：練到轉生等級 → 整理背包 → 依序解轉生前任務（需要寵物的自動抓寵、練寵、交寵）→ 轉生前檢查 → 轉生 → 轉生後規範 → 從低等圖開練，一直循環。
- **轉生前的同意關卡**：預設停在關卡只練功；`AUTO_REBIRTH=1` 才自動通過。
- **出錯就停**：逾時、被拒、數值對不上就停在原地並記日誌，不自動重試；程式崩潰有限次自動重開（5、10、20 分鐘）；被同角色在別處登入擠下線時不重開。
- **請求間隔**：每個請求等到回應才送下一個，間隔隨機 0.3～0.8 秒。
- **查詢 API**：預設唯讀，不會改變遊戲狀態（POST 只有 `/infolog` 開日誌、`/debug/*` 執行中除錯；除錯裡只有「暫時關快戰」會送封包，時間到自動開回來）。`API_JOBS=1` 才另外開放 `POST /jobs` 排集市上架。

## 快速開始

```bash
docker run -d --name bot \
  -e GAME_ACCOUNT=你的帳號 \
  -e GAME_PASSWORD=你的密碼 \
  -e API_TOKEN=至少16字元的隨機字串 \
  -p 127.0.0.1:8790:8790 \
  -v bot-data:/data \
  ghcr.io/sa-gaming888/x7k2qm:latest

docker logs -f bot          # 看日誌（只印在 console，容器裡不寫日誌檔）
docker stop -t 300 bot      # 等這場戰鬥打完再登出（docker 預設只等 10 秒，所以加 -t）
```

密碼也可以用檔案帶入（docker secret）：`-e GAME_PASSWORD_FILE=/run/secrets/game_password`。

## 註冊帳號（GitHub Actions）

1. Settings → Secrets and variables → Actions 設兩個 secret：`TG_BOT_TOKEN`（Telegram 機器人 token）、`TG_CHAT_ID`（要推送的群組 id）。登入伺服器預設 `eologin.idealsa.org`，要換才設 `GAME_LOGIN_HOST`。
2. Actions → **register** → Run workflow，填要建幾組（1～20）、選角色屬性（地／水／火／風，預設地）。
3. 每組帳號（英文單字＋數字，6～8 字元）、密碼（英文＋數字，8 字元）、台灣手機號碼都隨機產生，一組一組註冊（間隔 1.2 秒以上）；
   隨機帳號剛好有人用了就換一個（最多 3 次），其他失敗就停。
4. 每個帳號註冊完接著建**五隻角色**：屬性 10 點全加選的那一種、基礎點數敏 20、名字隨機（被用了換一個）；少於五隻就停。
5. 結果（帳號、密碼、電話、角色名）**只推到 Telegram**，Actions 日誌只寫幾組成功，帳密與角色名一律遮成 `***`。

本機也可以跑：`BOT_TG_TOKEN=… BOT_TG_CHAT_ID=… python -m x7k2qm.client.reg_random --count 3 --element 地 --host <登入伺服器>`（`--no-roles` 只註冊）；
指定帳密的單筆註冊：`GAME_PW=… python -m x7k2qm.client.reg_account --account <帳號> --phone <電話> --password-env GAME_PW --host <登入伺服器>`。

## 設定檔（掛載）

練功參數、劇本、道具處置規則表、外掛設定全部寫在一個設定檔，改設定不用改程式、不用重建映像：

```bash
curl -fsSLo bot.toml https://raw.githubusercontent.com/sa-gaming888/x7k2qm/main/config/bot.toml   # 拿一份預設的來改
docker run -d --name bot -e GAME_ACCOUNT=… -e GAME_PASSWORD=… \
  -v ./bot.toml:/config/bot.toml:ro -v bot-data:/data ghcr.io/sa-gaming888/x7k2qm:latest
docker restart bot          # 改完設定檔重開生效（會等這場戰鬥打完再登出）
```

沒掛載就用映像內建的 [`config/bot.toml`](config/bot.toml)（內容＝預設值，每一項都有註解）。也可以用 `CONFIG_FILE` 指定別的路徑。

| 區塊 | 內容 |
| :-- | :-- |
| `[bot]` | 練功程式參數：`start` 起始練功點、`auto_spirit`、`no_retreat`、`stay`、`keep_pets`、`shell_exchange`…（練功程式 `--help` 的長選項，`-` 換 `_`；新參數直接加，不用改容器） |
| `[files]` | `scenario` 劇本檔（`builtin`／`none`／路徑）、`item_rules` 道具處置規則表（`builtin`／路徑）；路徑可以相對設定檔，例 `scenario = "rebirth.toml"` ＝ `/config/rebirth.toml` |
| `[waigua]` | 外掛設定，**每次登入（轉生後也會）檢查**，跟現在不一樣才送：`hide` 全部隱藏、`fast_battle` 快速戰鬥、`first_cmd` 首次動作、`auto_points` 推薦加點、`point_plan` 自訂加點、`battle_heal` 戰鬥中技能補血 |
| `[waigua.raw.<協定>]` | 進階：直接寫外掛協定的欄位（例 `[waigua.raw.FastBattleSettingMpChange]` `MpRoleItemFlg = true`、`MpRoleItemPer = 30` 魔力低於 30% 吃藥）；`/waigua` 查得到現值。欄位型別照協定檢查（整數、true／false、字串要加引號）。鎖定戰寵／騎寵由寵物流程管，不能寫；`battle_heal` 開著時不能寫 `hpRole*`、`first_cmd = "spirit"` 時不能寫 `firstRoleCmd*`（兩邊會互蓋） |

設定檔格式錯、有不認得的欄位、寫了練功程式沒有的參數：**一律不登入**，日誌寫 `停止：設定檔：…`。
`docker run --rm -v ./bot.toml:/config/bot.toml:ro <映像> --check` 可以先檢查。

## 環境變數

| 變數 | 必填 | 說明 |
| :-- | :-: | :-- |
| `GAME_ACCOUNT` | ✔ | 帳號 |
| `GAME_PASSWORD` | ✔ | 密碼（或 `GAME_PASSWORD_FILE`） |
| `GAME_LOGIN_HOST` | | HTTP 登入伺服器，預設 `eologin.idealsa.org` |
| `GAME_NODE` | | 伺服器節點，預設 `game1` |
| `GAME_CLIENT_VER` | | 客戶端版本；遊戲改版後帶新版本號 |
| `CONFIG_FILE` | | 設定檔路徑；預設有掛載 `/config/bot.toml` 就用，沒有用內建的 |
| `AUTO_REBIRTH` | | `1`＝轉生關卡自動通過；其他值停在關卡 |
| `API_TOKEN` | | 查詢 API 的 token（至少 16 字元，或 `API_TOKEN_FILE`）；有設才對外開 8790 埠 |
| `API_JOBS` | `0` | `1`＝開放 `POST /jobs`（排集市上架，**會改遊戲狀態**）；要同時設 `API_TOKEN`，只能是 `0` 或 `1` |

下面這幾個**有設才蓋過設定檔**（舊的啟動方式照樣能用）：

| 變數 | 對應設定檔 | 說明 |
| :-- | :-- | :-- |
| `START` | `bot.start` | 起始練功點；空字串＝留在原地 |
| `SCENARIO` | `files.scenario` | 劇本路徑；`none`＝只練功 |
| `AUTO_SPIRIT` | `bot.auto_spirit` | `1`＝隊長買精靈金幣、60 級自動穿；其他值＝不買 |
| `NO_RETREAT` | `bot.no_retreat` | `1`＝不退回上一張圖；其他值＝照常 |
| `POINT_PLAN` | `waigua.point_plan` | 自訂加點 `隊長體,敏,攻/隊員體,敏,攻`；`none`＝不設 |
| `BATTLE_HEAL` | `waigua.battle_heal` | 戰鬥中技能補血門檻 %；`none`＝不設 |

帳號下要先有角色（程式登入帳號底下的全部角色，最多五個）。密碼只放在行程的環境變數，不寫檔、不進日誌。

## 查詢 API

```bash
TOKEN=你的API_TOKEN
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/state
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8790/pet?role=角色名"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/waigua        # 全部外掛設定
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/infolog   # 人物寵物每分鐘印一次，5 分鐘後自動關
curl http://127.0.0.1:8790/openapi.json -H "Authorization: Bearer $TOKEN"   # 完整端點說明
```

| 端點 | 內容 |
| :-- | :-- |
| `/state` | 總覽：地圖、隊伍、每隻人物與戰寵騎寵血量、經驗加成、金錢、劇本進度 |
| `/roles` | 人物一覽：等級、轉數、血量、地圖、座標、金錢 |
| `/role` | 人物完整屬性：經驗、四圍、剩餘點數、屬性、倉庫頁數（`attr` 是伺服器全部欄位） |
| `/bag` | 道具欄 |
| `/equip` | 身上裝備與部位 |
| `/pets` | 寵物一覽：等級、血量、出戰／騎乘／休息、忠誠度（`loyalty`）、檔次（`grade` 標籤、`gradeDiff` 檔差、`growth` 體腕耐速）、評分（`score`） |
| `/pet` | 寵物完整資料：攻防敏、屬性、暴擊等戰鬥數值、技能，加上同 `/pets` 的忠誠度、檔次、評分 |
| `/tasks` | 任務：已完成的任務組、進行中的步驟（`?groups=2000,2031`） |
| `/buffs` | 經驗加成與到期時間 |
| `/battle` | 近 5 分鐘勝敗、遇敵、快速戰鬥 |
| `/waigua` | 外掛設定全部：遇敵、快戰、隱藏、自動加點（方式＋自訂清單）、鎖定戰寵騎寵、首次動作、技能補血、抓寵；`bot` 是練功程式自己的開關 |
| `/depot` | 道具倉庫（個人＋公共；會送查詢請求，結果快取 60 秒） |
| `/petdepot` | 寵物倉庫（同上；每隻也有忠誠度、檔次、評分） |
| `/scenario` | 劇本進度 |
| `/jobs` | 工作佇列：排隊中、執行中、最近完成 |
| `/health` | 連線狀態（不需要 token） |

每個端點都可加 `?role=名字` 只看一隻。只接受 GET，其他方法回 405；
例外 `POST /infolog`：日誌每 60 秒印一次每隻的人物、戰寵、騎寵等級與血量，5 分鐘後自動關（開著再打一次從現在重算），不送遊戲封包。

### 排工作（`POST /jobs`，預設關）

唯一會改遊戲狀態的端點。要 `-e API_JOBS=1` 加上 `API_TOKEN` 才開；沒開回 403，token 錯回 401。
只收白名單工作（目前只有 `auction` 集市上架），跟 `docker exec … jobs add` 進同一個佇列，練功兩場戰鬥之間做。
不帶 `"apply": true` 只列出會上架什麼；帶了才真的上架。

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"job":"auction","item":2400,"price":1000000,"apply":true}' http://127.0.0.1:8790/jobs
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8790/jobs?id=回傳的id"   # 看結果
```

### 執行中除錯（`/debug`）

不重開、不重新登入就能看封包與出招。全部要 token、有時限（`minutes` 預設 5、最多 30），時間到自動關；結果印在日誌（`[debug]` 開頭）。

```bash
API="http://127.0.0.1:8790"; H="Authorization: Bearer $TOKEN"
curl -H "$H" "$API/debug/history?names=Battle_*&limit=20"                 # 最近收到的封包（不用先開，最多約 2 萬筆）
curl -X POST -H "$H" "$API/debug/trace?names=Item_*,Task_*&dir=recv,send&minutes=5"   # 收／送的封包原始內容印到日誌
curl -X POST -H "$H" "$API/debug/fast?minutes=10"                          # 暫時關快戰（快戰時伺服器只回勝負）
curl -X POST -H "$H" "$API/debug/battle?minutes=10"                        # 出招統計
curl -H "$H" "$API/debug/battle"                                           # 每隻放了什麼技能幾次、耗 MP、戰場屬性
curl -H "$H" "$API/debug"                                                  # 現在開著哪些、到幾點
curl -X POST -H "$H" "$API/debug/stop?what=all"                            # 提早關（trace／battle／fast／all）
```

| 端點 | 內容 |
| :-- | :-- |
| `GET /debug/history` | 最近收到的封包：`names`（萬用字元，逗號分隔，預設全部）、`who`（角色）、`limit`（預設 50、最多 500）、`maxlen`（每筆截斷字數，預設 2000） |
| `POST /debug/trace` | 封包追蹤：`names`（必填，`*` 全部）、`who`、`dir`（`recv`、`send`，預設 recv）、`minutes`、`maxlen` |
| `POST /debug/battle` | 出招統計（只算隊長那條連線），結果 `GET /debug/battle`；戰鬥位置對到名字，合擊每個出手的人各算一次 |
| `POST /debug/fast` | 暫時關快戰：由練功主迴圈送（做任務時不送），時間到照平常規則開回來；期間練功會變慢 |
| `POST /debug/stop` | 提早關：`what` 是 `trace`、`battle`、`fast` 或 `all`（預設） |

抓不到的：伺服器沒送的資料（例如快戰時每回合出招，要先 `/debug/fast`）。除錯不能送任意封包。
沒設 `API_TOKEN` 時 API 只在容器內開：`docker exec bot python -m x7k2qm.autoplay.status_api state`。

## 狀態上傳（選用）

同一個映像換指令跑：定時向各練功容器的查詢 API 要 `/state`，整理成「組別總覽、每組人物、出戰騎乘寵物、金錢、即時狀態、程式清單」幾份 JSON，
只把有變的 POST 到你指定的網址。只會送 GET 給查詢 API，不送遊戲封包、不排工作。一個叢集只要跑一份。

```yaml
containers:
  - name: status-upload
    image: ghcr.io/<owner>/x7k2qm:<版本>
    command: ["python", "-X", "utf8", "-m", "x7k2qm.autoplay.status_upload"]
    env:
      - {name: BOT_UPLOAD_TARGETS, value: "2:練功=http://bot-2:8790,3=http://bot-3:8790"}
      - {name: BOT_UPLOAD_URL, value: "https://<你的上傳端>/ingest"}
      - {name: BOT_UPLOAD_PREFIX, value: "/<上傳端規定的前綴>"}
      - {name: BOT_UPLOAD_STATE, value: /data/status_upload_state.json}
      - name: BOT_UPLOAD_KEY
        valueFrom: {secretKeyRef: {name: status-upload, key: key}}
      - name: BOT_UPLOAD_API_TOKEN          # 練功容器的 API_TOKEN（練功容器要設 API_TOKEN，8790 才對外聽）
        valueFrom: {secretKeyRef: {name: bot-api, key: token}}
```

| 變數 | 必填 | 說明 |
| :-- | :-: | :-- |
| `BOT_UPLOAD_TARGETS` | ✔ | `組=網址`，逗號分隔；可加標籤 `組:標籤=網址` |
| `BOT_UPLOAD_URL` | ✔ | 上傳網址 |
| `BOT_UPLOAD_KEY` | ✔ | 上傳金鑰（放 Secret） |
| `BOT_UPLOAD_PREFIX` | ✔ | 每份文件路徑的前綴，以 `/` 開頭 |
| `BOT_UPLOAD_HEADER` | | 金鑰標頭名稱，預設 `X-Ingest-Key` |
| `BOT_UPLOAD_API_TOKEN` | | 查詢 API 的 token（`Authorization: Bearer`） |
| `BOT_UPLOAD_STATE` | | 記錄「上次送了什麼」的檔案；放持久磁碟，重啟後不用全部重送 |
| `BOT_UPLOAD_EVERY` | | 幾秒一輪，預設 300 |

上傳格式：`POST {"docs": [{"path", "body": "<JSON 字串>", "hash"}], "delete": [...], "synced": <秒>}`，對方回 `{"ok": true}` 才算成功。
路徑有 `/groups`、`/groups/<組>`、`/groups/<組>/pets`、`/groups/<組>/money`、`/groups/<組>/live`、`/live`、`/procs`、`/_heartbeat`。
一次 POST 不超過約 0.6 MB；這輪不見的路徑放進 `delete`。失敗只記日誌，下一輪照常；某一組連不上，那組標 `stopped` 並附原因，其他組照送。

## 資料目錄 `/data`

| 路徑 | 內容 |
| :-- | :-- |
| `scenario/1.json` | 劇本進度；容器重開從這一步接著做 |
| `jobs/` | 工作佇列 |
| `owner/` | 帳號鎖（同一個資料目錄不會同時跑兩個） |

劇本停下來（任務失敗、轉生前檢查不通過）後只會練功，原因寫在日誌與 `/scenario`。處理好之後：

```bash
docker exec bot python -m x7k2qm.autoplay.jobs scenario resume --profile 1
docker exec bot python -m x7k2qm.autoplay.jobs scenario approve --profile 1   # 手動通過轉生關卡
```

## 注意

- 同一個帳號只能在一個地方登入：容器在跑時用遊戲客戶端登入，會互相擠下線（容器被擠下線就停止、不重開）。
- 不會花真錢貨幣；石幣只用在劇本需要的道具（例如任務道具、抓寵誘餌）。

## 映像與 CVE 掃描

| 映像 | 怎麼建 | 什麼時候建 |
| :-- | :-- | :-- |
| `ghcr.io/sa-gaming888/x7k2qm-base` | `Dockerfile.base`（workflow **base**） | `Dockerfile.base` 改了或手動觸發；只有 Python 與 CA 憑證，無 shell、非 root、去掉 sqlite |
| `ghcr.io/sa-gaming888/x7k2qm` | `Dockerfile`（workflow **build**） | 程式改了；每次都 `FROM x7k2qm-base:latest`，只放程式 |

基底更新流程：本地 `./scan.sh` 掃過沒有 CVE → push `Dockerfile.base`（或手動跑 **base**）→ 手動跑 **build**。
CVE 掃描在本地做，不在 workflow：`./scan.sh`（本機建兩層再掃）或 `./scan.sh <映像>`；Grype 任何等級有弱點就結束碼 1。
