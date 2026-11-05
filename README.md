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
- **查詢 API**：唯讀，不會改變遊戲狀態（唯一的 POST 是 `/infolog`，只開日誌）。

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

## 環境變數

| 變數 | 必填 | 說明 |
| :-- | :-: | :-- |
| `GAME_ACCOUNT` | ✔ | 帳號 |
| `GAME_PASSWORD` | ✔ | 密碼（或 `GAME_PASSWORD_FILE`） |
| `GAME_LOGIN_HOST` | | HTTP 登入伺服器，預設 `eologin.idealsa.org` |
| `GAME_NODE` | | 伺服器節點，預設 `game1` |
| `GAME_CLIENT_VER` | | 客戶端版本；遊戲改版後帶新版本號 |
| `START` | | 起始練功點，預設 `柯奧龜洞Lv10`；空字串＝留在原地（已在練功點的角色） |
| `SCENARIO` | | 劇本路徑，預設內建轉生劇本；`none`＝只練功 |
| `AUTO_REBIRTH` | | `1`＝轉生關卡自動通過；其他值停在關卡 |
| `AUTO_SPIRIT` | | `1`（預設）＝隊長依屬性買精靈金幣、60 級自動穿；`0`＝不買 |
| `POINT_PLAN` | | 外掛自訂加點 `隊長體,敏,攻/隊員體,敏,攻`，預設 `100,100,999/80,80,999`；`none`＝系統推薦 |
| `BATTLE_HEAL` | | 外掛戰鬥中技能補血門檻 %（人物／寵物／騎寵），預設 `50`；`none`＝不設 |
| `NO_RETREAT` | | `1`＝不退回上一張圖（補血太頻繁、輸太多都不退；前進、回村補血照舊）；預設 `0` |
| `API_TOKEN` | | 查詢 API 的 token（至少 16 字元，或 `API_TOKEN_FILE`）；有設才對外開 8790 埠 |

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
唯一例外 `POST /infolog`：日誌每 60 秒印一次每隻的人物、戰寵、騎寵等級與血量，5 分鐘後自動關（開著再打一次從現在重算），不送遊戲封包。
沒設 `API_TOKEN` 時 API 只在容器內開：`docker exec bot python -m x7k2qm.autoplay.status_api state`。

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
