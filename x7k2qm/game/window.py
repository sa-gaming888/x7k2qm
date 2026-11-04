"""Win32 視窗點擊：以遊戲「客戶區」座標點擊（不受視窗位置與 DPI 縮放影響）。"""

import ctypes
import time
from ctypes import wintypes

GAME_TITLE = "本遊戲"


class GameWindow:
    def __init__(self, title: str = GAME_TITLE) -> None:
        self.user32 = ctypes.windll.user32
        self.user32.SetProcessDPIAware()
        self.hwnd = self.user32.FindWindowW(None, title)
        if not self.hwnd:
            raise RuntimeError(f"找不到遊戲視窗 {title!r}，請先開啟遊戲")

    def client_size(self):
        r = wintypes.RECT()
        self.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return r.right, r.bottom

    def click(self, x: int, y: int) -> None:
        u = self.user32
        u.ShowWindow(self.hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(self.hwnd)
        pt = wintypes.POINT(x, y)
        u.ClientToScreen(self.hwnd, ctypes.byref(pt))
        u.SetCursorPos(pt.x, pt.y)
        time.sleep(0.05)
        u.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.04)
        u.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
