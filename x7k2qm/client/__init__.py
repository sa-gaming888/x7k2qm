"""client - 無畫面客戶端：不開遊戲，自己登入並直接送協定請求。

分層：
  crypto.py      skynet 的 hashkey / hmac64、HTTP 登入參數的字元替換（Obfuscate）
  auth.py        登入握手 #1~#4 的組裝與解析（純函式）
  http_login.py  HTTP 帳號登入（LoginAcccountByGet），取得 uid、遊戲伺服器 IP、區服與角色清單
  connection.py  GameClient：TCP 連線、握手、加密訊框收發、request/response 配對、心跳
  config.py      設定：帳密只從環境變數讀（BOT_ACCOUNT / BOT_PASSWORD）；pcId 照遊戲格式自己產生
  session.py     一次完成 HTTP 登入 → 握手 → 進入遊戲

協定與驗證過程見 doc/REVERSE-ENGINEERING.md §10。帳密不寫進任何檔案。
"""
