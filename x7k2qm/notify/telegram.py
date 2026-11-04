"""Telegram 推送。

環境變數（不寫進任何檔案）：
  BOT_TG_TOKEN    bot token（@BotFather 給的）
  BOT_TG_CHAT_ID  要推送的聊天室 id（群組是負數）

規則：推送失敗只回報錯誤、不重送、不影響呼叫端；錯誤訊息裡的 token 一律遮掉。
"""

import html as html_mod
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000  # Telegram 單則上限 4096 字元，留一點餘裕
# 使用者 2026-10-22：允許有限重試（原本不重送，暫時性斷線會弄丟訊息）。只有這兩次，之後放棄。
RETRY_DELAYS = (5.0, 15.0)


class TelegramConfigError(RuntimeError):
    pass


@dataclass
class Telegram:
    token: str
    chat_id: str
    timeout: float = 10.0

    def __repr__(self) -> str:  # 不把 token 印出來
        return f"Telegram(chat_id={self.chat_id!r})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Telegram":
        """優先用 BOT_TG_TOKEN / BOT_TG_CHAT_ID；沒有的話讀 BOT_TG_CONFIG 指向的 JSON
        （參考專案 sa_battle_feed/config.json 格式：{"telegram": {"bot_token", "chat_id"}}）。"""
        token, chat = environ.get("BOT_TG_TOKEN"), environ.get("BOT_TG_CHAT_ID")
        path = environ.get("BOT_TG_CONFIG")
        if (not token or not chat) and path:
            try:
                with open(path, encoding="utf-8") as fp:
                    t = json.load(fp).get("telegram", {})
            except (OSError, ValueError) as e:
                raise TelegramConfigError(f"讀不到 BOT_TG_CONFIG 指定的檔案：{type(e).__name__}") from None
            token = token or t.get("bot_token")
            chat = chat or (str(t["chat_id"]) if t.get("chat_id") is not None else None)
            if not token or not chat:
                raise TelegramConfigError("BOT_TG_CONFIG 的檔案缺少 telegram.bot_token 或 telegram.chat_id")
        missing = [n for n, v in (("BOT_TG_TOKEN", token), ("BOT_TG_CHAT_ID", chat)) if not v]
        if missing:
            raise TelegramConfigError(f"缺少環境變數 {', '.join(missing)}"
                                      "（或用 BOT_TG_CONFIG 指向含 telegram.bot_token/chat_id 的 JSON）")
        return cls(token, chat)

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def _call(self, method: str, params: dict) -> Tuple[Optional[str], bool, Optional[dict]]:
        """呼叫一次 Bot API；回傳 (錯誤說明或 None, 這個錯誤值不值得重試, result)。"""
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(API.format(token=self.token).replace("/sendMessage", "/" + method),
                                     data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read().decode("utf-8")).get("description", "")
            except Exception:
                desc = ""
            # 4xx 是我們自己錯（token、chat_id、內容）→ 重試沒意義；429／5xx 是對方忙 → 值得重試
            retryable = e.code >= 500 or e.code == 429
            return self._redact(f"Telegram HTTP {e.code} {desc}".strip()), retryable, None
        except Exception as e:  # 連線逾時、DNS、斷線
            return self._redact(f"Telegram 送出失敗：{type(e).__name__}: {e}"), True, None
        if not data.get("ok"):
            return self._redact(f"Telegram 回應錯誤：{data.get('description', data)}"), False, None
        return None, False, data.get("result")

    def _send_once(self, text: str, html: bool = False, silent: bool = False) -> Tuple[Optional[str], bool]:
        """送一次；回傳 (錯誤說明或 None, 這個錯誤值不值得重試)。"""
        params = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}
        if html:
            params["parse_mode"] = "HTML"
        if silent:
            params["disable_notification"] = "true"
        err, retryable, _ = self._call("sendMessage", params)
        if err and html and " 400 " in f" {err} ":       # HTML 標籤被拒 → 拿掉標籤送純文字（參考專案 做法）
            return self._send_once(html_mod.unescape(re.sub(r"<[^>]+>", "", text)), False, silent)
        return err, retryable

    def send(self, text: str, sleep: Callable[[float], None] = time.sleep, html: bool = False,
             silent: bool = False) -> Optional[str]:
        """送出一則訊息；成功回傳 None，失敗回傳錯誤說明（已遮掉 token）。不丟例外。

        使用者 2026-10-22 決定改成**有限重試**：原本「失敗就不重送」，遇到中華電信連 Telegram
        偶發斷線（實測 20 秒逾時連三次、一分鐘後又正常）會把訊息永久弄丟，包含使用者指定
        要看的啟動完整快照。只重試連線層錯誤與 429／5xx；4xx 不重試。
        """
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN - 1] + "…"
        err, retryable = self._send_once(text, html, silent)
        for delay in RETRY_DELAYS:
            if err is None or not retryable:
                break
            sleep(delay)
            err, retryable = self._send_once(text, html, silent)
        return err


class Throttle:
    """每輪摘要的推送節流（使用者 2026-10-22：交易所全掃「一小時送一次就好」）。
    every 秒內只放行第一次；every=0 表示每次都放行。時鐘由呼叫端給（測試用假時鐘）。"""

    def __init__(self, every: float) -> None:
        self.every = every
        self.last: Optional[float] = None

    def ready(self, now: float) -> bool:
        if self.every <= 0 or self.last is None or now - self.last >= self.every:
            self.last = now
            return True
        return False


def round_message(task_name: str, seconds: float, summary: str) -> str:
    """每輪摘要的訊息內容。"""
    return f"✅ {task_name} 完成（{seconds:.0f} 秒）\n{summary}"
