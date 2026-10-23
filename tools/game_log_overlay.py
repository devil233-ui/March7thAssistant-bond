# -*- coding: utf-8 -*-
"""游戏内日志悬浮窗：把小助手的任务日志贴到游戏窗口左下角。

小助手 GUI 里本来就有同款悬浮窗（`app/log_interface.py` 的 `GameLogOverlay`），
但它绑在 PySide6 的日志界面上；命令行跑 `main.py currencywars` 时没有 GUI，
日志只能看被游戏窗口挡住的 cmd。这个工具复刻同样的行为，完全独立：

- 读 `logs/YYYY-MM-DD.log` 的增量（不干扰小助手写日志）；
- 只在游戏窗口位于前台时贴在它的左下角，其余时候自动隐藏；
- 无边框、半透明、鼠标穿透、不进任务栏、不抢焦点；
- 宽高按游戏客户区比例算（34% × 26%），与小助手悬浮窗一致。

用法：
    pythonw -X utf8 tools/game_log_overlay.py            # 自动选今天/最新的日志
    pythonw -X utf8 tools/game_log_overlay.py --log logs/2026-10-23.log
    python tools/log_overlay.py --console                # 前台跑，看运行信息

退出：关掉本进程即可（任务管理器结束 pythonw，或 kill 掉对应 PID）。
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

# 与小助手一致：按物理像素工作，否则 125%/150% 缩放下坐标会偏
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk

import win32gui


def _ensure_tcl_env():
    """让 venv 里的 tkinter 找得到 Tcl/Tk。

    venv 用的是 base python（D:\\1SOFTWARE）的 Tcl，但解释器默认只按
    `<base>/lib/tcl8.6` 之类的位置去找，实际装在 `<base>/tcl/tcl8.6`，
    不指定就会报 "Can't find a usable init.tcl"。这里显式补齐，免去让用户配环境变量。
    """
    if os.environ.get("TCL_LIBRARY"):
        return
    base = Path(sys.base_prefix)
    for tcl_dir in (base / "tcl" / "tcl8.6", base / "lib" / "tcl8.6", base / "tcl8.6"):
        if (tcl_dir / "init.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            tk_dir = tcl_dir.parent / "tk8.6"
            if (tk_dir / "tk.tcl").exists():
                os.environ["TK_LIBRARY"] = str(tk_dir)
            return


_ensure_tcl_env()

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
_HOTKEY_MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT}

GAME_CLASS = "UnityWndClass"
GAME_TITLE = "崩坏：星穹铁道"

MAX_LINES = 14
MARGIN = 14
WIDTH_RATIO = 0.34
HEIGHT_RATIO = 0.26
_SHOW_DEBUG = False  # 小助手的 DEBUG 行（带 pid/thread 前缀）在游戏里没有价值，默认过滤

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \| (\w+) \| (.*)$")
LEVEL_COLORS = {
    "ERROR": "#ff7b72",
    "WARNING": "#ffd166",
    "DEBUG": "#6b7785",
    "INFO": "#e6edf3",
}
BG_COLOR = "#0d1117"


class LogTail:
    """按文件偏移读日志增量，只保留最后 MAX_LINES 行。"""

    def __init__(self, path: Path, maxlen: int = MAX_LINES):
        self.path = path
        self.buf = deque(maxlen=maxlen)
        self._pos = 0
        self._prime()

    def _prime(self):
        """启动时先读一段尾巴，避免窗口空着等新日志。"""
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                f.seek(max(0, size - 64 * 1024))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return
        for line in data.splitlines()[-MAX_LINES * 4:]:
            item = self.parse(line)
            if item:
                self.buf.append(item)
        self._pos = size

    def poll(self) -> bool:
        """读新增内容；返回是否有变化。"""
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self._pos:  # 日志轮转或被清空
            self._pos = 0
            self.buf.clear()
        if size == self._pos:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
            self._pos = size
        except OSError:
            return False
        for line in chunk.decode("utf-8", "replace").splitlines():
            item = self.parse(line)
            if item:
                self.buf.append(item)
        return True

    @staticmethod
    def parse(raw: str):
        """日志行 -> (显示文本, 级别)；返回 None 表示这行不值得占位置。"""
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            return None
        if set(stripped) <= set("+-| "):  # 纯分隔线
            return None
        if stripped.startswith("|") and stripped.endswith("|"):
            text = stripped.strip("|").strip()
            return (f"—— {text} ——", "INFO") if text else None
        matched = LINE_RE.match(line)
        if matched:
            level = matched.group(3).upper()
            if level == "DEBUG" and not _SHOW_DEBUG:
                return None
            return f"{matched.group(2)}  {matched.group(4)}", level
        return _shorten(stripped), "INFO"


def _shorten(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def find_log_file(explicit: Path | None) -> Path:
    """挑出「正在被写」的那份日志。

    实机跑的是源码克隆（<工作区>-bond），日志也写在克隆的 logs/ 下；
    只看本目录的 logs/ 会读到安装版留下的旧文件（里面全是 GUI 的 DEBUG 噪声）。
    规则：候选目录里最近 10 分钟内还在写的、最新的那份；都没有才退到今天/最新。
    """
    if explicit:
        return explicit
    here = Path(__file__).resolve().parent.parent
    log_dirs = [here / "logs", here.parent / f"{here.name}-bond" / "logs"]
    candidates = []
    for log_dir in log_dirs:
        if log_dir.is_dir():
            candidates.extend(p for p in log_dir.glob("*.log") if p.is_file())
    if not candidates:
        return here / "logs" / time.strftime("%Y-%m-%d.log")
    now = time.time()
    fresh = [p for p in candidates if now - p.stat().st_mtime < 600]
    return max(fresh or candidates, key=lambda p: p.stat().st_mtime)


def client_rect(hwnd):
    """游戏客户区在屏幕上的物理像素矩形（和小助手 _getClientScreenRect 一致）。"""
    try:
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        top_left = wintypes.POINT(0, 0)
        bottom_right = wintypes.POINT(rect.right, rect.bottom)
        if not ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            return None
        if not ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            return None
        if bottom_right.x <= top_left.x or bottom_right.y <= top_left.y:
            return None
        return top_left.x, top_left.y, bottom_right.x, bottom_right.y
    except Exception:
        return None


def game_is_foreground():
    """只在游戏位于前台时显示，避免挡住桌面上的其它操作。"""
    hwnd = win32gui.FindWindow(GAME_CLASS, GAME_TITLE)
    if not hwnd:
        return None
    if win32gui.GetForegroundWindow() != hwnd:
        return None
    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return None
    return hwnd


def parse_hotkey(text: str):
    """'ctrl+alt+l' / 'f9' -> (修饰键掩码, 虚拟键码)；格式不认识返回 None。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    vk = None
    for part in parts:
        if part in _HOTKEY_MODS:
            mods |= _HOTKEY_MODS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        elif re.fullmatch(r"f\d{1,2}", part) and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1  # VK_F1 = 0x70
        else:
            return None
    return (mods, vk) if vk else None


class HotkeyWatcher(threading.Thread):
    """独立线程里的全局热键：每命中一次往队列塞一个事件。

    不能放在主线程：tkinter 的 mainloop 自己吞消息，抢过来会把界面卡死。
    这里 `RegisterHotKey(None, ...)` 把热键注册到本线程，消息也只进本线程队列，
    PeekMessage 只取 WM_HOTKEY，和 tkinter 互不干扰。
    被别的程序占用时 RegisterHotKey 会失败，此时安静退出，不影响悬浮窗本身。
    """

    def __init__(self, combo, event_queue):
        super().__init__(daemon=True)
        self.mods, self.vk = combo
        self.queue = event_queue
        self._stop = threading.Event()

    def run(self):
        user32 = ctypes.windll.user32
        hotkey_id = 1
        if not user32.RegisterHotKey(None, hotkey_id, self.mods | MOD_NOREPEAT, self.vk):
            return
        try:
            msg = wintypes.MSG()
            while not self._stop.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                        self.queue.put("toggle")
                else:
                    time.sleep(0.05)
        finally:
            user32.UnregisterHotKey(None, hotkey_id)

    def stop(self):
        self._stop.set()


class Overlay:
    def __init__(self, tail: LogTail, follow_seconds: float, exit_when_idle: float = 0,
                 hotkey: str = "ctrl+alt+l", verbose: bool = False):
        self.tail = tail
        self.follow_seconds = follow_seconds
        self.exit_when_idle = exit_when_idle
        self.verbose = verbose
        self._last_write = time.monotonic()
        self._shown = False
        self._paused = False
        self._hotkey_queue = queue.Queue()

        combo = parse_hotkey(hotkey)
        self._watcher = HotkeyWatcher(combo, self._hotkey_queue) if combo else None
        if self._watcher:
            self._watcher.start()
        elif verbose:
            print(f"热键格式不认识，已忽略：{hotkey}")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.90)
        self.root.configure(bg=BG_COLOR)

        self.text = tk.Text(
            self.root,
            bg=BG_COLOR,
            fg="#e6edf3",
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            wrap="none",
            padx=12,
            pady=10,
            spacing1=1,
            spacing3=1,
            cursor="arrow",
        )
        self.text.pack(fill="both", expand=True)
        for level, color in LEVEL_COLORS.items():
            self.text.tag_configure(level, foreground=color)
        self.text.configure(state="disabled")
        self._render()
        self.root.after(200, self._click_through)

    def _click_through(self):
        """鼠标穿透 + 不进任务栏 + 不抢焦点（小助手悬浮窗同样处理）。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            )
        except Exception as exc:
            if self.verbose:
                print(f"设置穿透失败：{exc}")

    def _render(self):
        width = self.text.winfo_width()
        if width <= 1:
            width = self.root.winfo_width()
        # 10pt 微软雅黑的中文约 13px 宽：按当前宽度算一行放得下几个字，超出的截断
        max_chars = max(24, (width - 30) // 13) if width > 1 else 60
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line, level in self.tail.buf:
            if len(line) > max_chars:
                line = line[:max_chars - 1] + "…"
            self.text.insert("end", line + "\n", level if level in LEVEL_COLORS else "INFO")
        self.text.configure(state="disabled")
        self.text.see("end")

    def _place(self):
        hwnd = game_is_foreground()
        if not hwnd:
            self._hide()
            return
        rect = client_rect(hwnd)
        if not rect:
            self._hide()
            return
        left, top, right, bottom = rect
        width = max(320, int((right - left) * WIDTH_RATIO))
        height = max(140, int((bottom - top) * HEIGHT_RATIO))
        x = left + MARGIN
        y = max(top + MARGIN, bottom - height - MARGIN)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        if not self._shown:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self._shown = True

    def _hide(self):
        if self._shown:
            self.root.withdraw()
            self._shown = False

    def _tick(self):
        while not self._hotkey_queue.empty():  # 热键线程只投事件，切显示留在主线程做
            self._hotkey_queue.get()
            self._paused = not self._paused
            if self.verbose:
                print("悬浮窗已隐藏" if self._paused else "悬浮窗已显示")

        if self.tail.poll():
            self._render()
            self._last_write = time.monotonic()

        idle = time.monotonic() - self._last_write
        if self.exit_when_idle and idle > self.exit_when_idle:
            self.root.quit()  # 日志早就停了：自己退掉，别留后台进程
            return
        # 手动隐藏优先；其次任务停了就不再占屏（进程留着，下次跑起来会自动回来）
        if self._paused or idle > self.follow_seconds:
            self._hide()
        else:
            self._place()
        self.root.after(400, self._tick)

    def run(self):
        self.root.after(400, self._tick)
        try:
            self.root.mainloop()
        finally:
            if self._watcher:
                self._watcher.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="把小助手日志贴在游戏窗口左下角")
    parser.add_argument("--log", type=Path, help="指定日志文件；默认取 logs/ 下今天或最新的")
    parser.add_argument("--follow-seconds", type=float, default=90,
                        help="日志多少秒没有新内容就隐藏（默认 90）")
    parser.add_argument("--console", action="store_true", help="前台运行并打印诊断信息")
    parser.add_argument("--show-debug", action="store_true",
                        help="连 DEBUG 行一起显示（默认过滤掉，那批带 pid/thread 的行在游戏里没用）")
    parser.add_argument("--hotkey", default="ctrl+alt+l",
                        help="切换显示/隐藏的全局热键（默认 ctrl+alt+l；写 none 则不注册）")
    parser.add_argument("--exit-when-idle", type=float, default=0,
                        help="日志空闲多少秒后自动退出（默认 0=不退出；由 main.py 集成时传 600）")
    args = parser.parse_args()

    global _SHOW_DEBUG
    _SHOW_DEBUG = args.show_debug

    log_path = find_log_file(args.log)
    if not log_path.exists():
        print(f"找不到日志文件：{log_path}", file=sys.stderr)
        return 2
    if args.console:
        print(f"日志：{log_path}")
        print(f"热键：{args.hotkey}（切换显示/隐藏）")

    overlay = Overlay(
        LogTail(log_path),
        args.follow_seconds,
        exit_when_idle=args.exit_when_idle,
        hotkey="" if args.hotkey.strip().lower() == "none" else args.hotkey,
        verbose=args.console,
    )
    overlay.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # pythonw 没有控制台，异常只能落到临时文件里
        crash = Path.home() / "AppData/Local/Temp/m7a_log_overlay_crash.log"
        crash.write_text(traceback.format_exc(), encoding="utf-8")
        raise
