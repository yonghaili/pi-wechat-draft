# -*- coding: utf-8 -*-
"""微信操作后台服务（wxsvc）—— 独占微信自动化，串行排队执行。

为什么要有它
------------
微信 4.x 的文字输入只能走「剪贴板 + 注入按键」，而 Windows 只把注入按键
投递给**前台**窗口。所以真正"完全无感"做不到；能做的是：
  1. **串行**：所有请求进同一个队列，由本进程一个接一个执行，绝不并发抢微信；
  2. **空闲闸门**：每次动手前先等使用者停止鼠标/键盘操作 ≥ IDLE_GATE 秒，
     正在用电脑时不抢；
  3. **热复用**：WeChatGUI / UIA 引擎常驻，不再每次命令冷启动 10s+，
     单次操作窗口从十几秒压到 1~3 秒，冲突面大幅缩小；
  4. **收尾还原**：执行前记住前台窗口与光标位置，执行后还原，不把微信
     留在最前面，也不把光标丢在别处（除非确实点过）；
  5. **不最小化任何窗口**：跳过库自带的 ensure_visible()（它会最小化你的
     其它窗口），只在微信被藏进托盘/最小化时自己把它恢复；
  6. **默认只填不发**：send 必须 job 里带 confirm=true 才执行。

协议（文件队列，便于审计与排障）
--------------------------------
    队列目录 : ~/.pi/wechat-ui/queue/
    任务文件 : <seq>. <id>.job           JSON  {id,action,chat,text,confirm,created}
    执行中   : <...>.job 改名为 <...>.working
    结果文件 : <id>.result               JSON  {id,ok,action,chat,text,evidence,...}
    日志     : ~/.pi/wechat-ui/logs/wx-service-YYYYMMDD.log
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# 自定位：脚本在 <repo>/scripts/ 下，运行期状态（venv/queue/logs）默认放 <repo>/
SCRIPT_DIR = Path(__file__).resolve().parent
BASE = Path(os.environ.get("WX_DIR") or SCRIPT_DIR.parent)
QUEUE = BASE / "queue"
LOGS = BASE / "logs"
PIDFILE = BASE / "wx-service.pid"
STATEFILE = BASE / "wx-service.state.json"
PENDING = BASE / "draft-pending.json"      # 已填入但未发送的草稿（用于阻止“切会话把它冲掉”）
OCR_PS1 = SCRIPT_DIR / "wx-ocr.ps1"
# 可选：更强的独立复核后端（自己的只读微信读取器）。
# 不配置时本工具只靠 UIA 控件回读 + OCR 像素复核，功能完整，
# 只是少了「用另一条链路回读聊天记录」这层证据。
# 例： set WX_READER=C:/tools/reader/rion_wechat_reader.py
_reader_env = os.environ.get("WX_READER", "").strip()
READER = Path(_reader_env) if _reader_env else None
SYS_PY = os.environ.get("WX_SYS_PY", "python")

IDLE_GATE = float(os.environ.get("WX_IDLE_GATE", "2.0"))     # 动手前要求的空闲秒数
IDLE_MAX_WAIT = float(os.environ.get("WX_IDLE_MAX_WAIT", "300"))  # 等不到空闲就先把任务放回队列
POLL = 0.4

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"wx-service-{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 系统级小工具
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not u32.GetLastInputInfo(ctypes.byref(lii)):
        return 1e9
    return max(0.0, (k32.GetTickCount() - lii.dwTime) / 1000.0)


def foreground_window() -> int:
    return int(u32.GetForegroundWindow())


def cursor_pos():
    p = POINT()
    u32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def weixin_pids():
    pids = []
    for name in ("Weixin.exe", "WeChat.exe"):
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout or ""
        for row in out.splitlines():
            parts = [p.strip('" ') for p in row.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def enum_top_windows():
    """枚举顶层窗口，返回 [(hwnd, pid, visible, iconic, class, title)]。"""
    rows = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        rows.append((int(hwnd), int(pid.value), bool(u32.IsWindowVisible(hwnd)),
                     bool(u32.IsIconic(hwnd)), cls.value, title.value))
        return True

    u32.EnumWindows(EnumProc(_cb), 0)
    return rows


def alt_activate(hwnd: int, layer_log=None) -> bool:
    """把窗口抢到前台，成功返回 True。分层升级，每层都记日志。

    实测（2026-09-20）本机四种方法对比：
      库 bring_to_front(keep_topmost=True) → 抢不到；SwitchToThisWindow → 抢不到；
      **纯 Alt + SetForegroundWindow → 10/10 成功**（首选，且不需置顶）。
    Alt 键的作用：Windows 只允许“最后一个输入事件来自本进程”的进程改前台，
    合成一个 Alt 事件就能满足这个条件。
    """
    if not hwnd or not u32.IsWindow(hwnd):
        return False
    SW_RESTORE, VK_MENU, KEYUP = 9, 0x12, 0x2
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

    def press_alt():
        u32.keybd_event(VK_MENU, 0, 0, 0)
        u32.keybd_event(VK_MENU, 0, KEYUP, 0)

    # 第 1 层：最小干预（只 Alt + SetForegroundWindow）
    for _ in range(8):
        if u32.GetForegroundWindow() == hwnd:
            return True
        press_alt()
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
    if u32.GetForegroundWindow() == hwnd:
        return True

    # 第 2 层：AttachThreadInput 解锁
    for _ in range(4):
        press_alt()
        fg = int(u32.GetForegroundWindow() or 0)
        tid_fg = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_t = u32.GetWindowThreadProcessId(hwnd, None)
        try:
            if tid_fg and tid_t and tid_fg != tid_t:
                u32.AttachThreadInput(tid_fg, tid_t, True)
            u32.SetForegroundWindow(hwnd)
            u32.SetActiveWindow(hwnd)
            u32.BringWindowToTop(hwnd)
        finally:
            if tid_fg and tid_t and tid_fg != tid_t:
                u32.AttachThreadInput(tid_fg, tid_t, False)
        time.sleep(0.2)
        if u32.GetForegroundWindow() == hwnd:
            return True

    if layer_log is not None:
        layer_log(f"  alt_activate 两层都失败（前台={u32.GetForegroundWindow()}）")
    return False


def force_foreground(hwnd: int) -> bool:
    """把指定窗口切到前台（Alt 键解锁法）。"""
    return alt_activate(hwnd)


def wechat_main_window():
    pids = set(weixin_pids())
    best = None
    for hwnd, pid, vis, iconic, cls, title in enum_top_windows():
        if pid not in pids:
            continue
        if cls.startswith("Qt") and "QWindowIcon" in cls:
            best = (hwnd, vis, iconic, title)
            if vis and title:
                break
    return best


def ensure_window_usable() -> str:
    """把微信主窗口从托盘/最小化恢复到可见，但不碰任何其它窗口。"""
    info = wechat_main_window()
    if not info:
        return "no_window"
    hwnd, vis, iconic, _ = info
    if iconic:
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.5)
        return "restored_from_minimized"
    if not vis:
        u32.ShowWindow(hwnd, 5)          # SW_SHOW
        time.sleep(0.5)
        return "restored_from_tray"
    return "ok"


# ------------------------------------------------------------------ OCR（复核）
def _ocr_raw(crop: str | None = None, scale: int = 3) -> str:
    cmd = ["powershell", "-NoProfile", "-File", str(OCR_PS1), "-ProcId", "0"]
    if crop:
        cmd += ["-Crop", crop, "-Scale", str(scale)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout or b""
    except Exception as e:
        log(f"  OCR 调用失败: {e}")
        return ""
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp1252"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("utf-8", "replace")


def window_rect():
    """微信主窗口物理矩形 (L,T,R,B)。"""
    info = wechat_main_window()
    if not info:
        return None
    hwnd = info[0]
    r = wintypes.RECT()
    if not u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def fast_ink() -> str:
    """像素法检测输入行有无墨迹（不跑 OCR，约 0.1s）。

    返回 "" = 空；"ink" = 有字；"?" = 未知。
    区域用窗口比例（底部≈0.60~0.67H、右侧 0.40~0.97W，实测输入行在
    0.635H），比库自带的 _input_box_has_text（区域高 200px，会框到聊天内容）
    更靠得住，也比 PowerShell+OCR 快两个数量级。
    """
    rect = window_rect()
    if not rect:
        return "?"
    L, T, R, B = rect
    W, H = R - L, B - T
    box = (L + int(W * 0.40), T + int(H * 0.60), L + int(W * 0.97), T + int(H * 0.67))
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=box).convert("RGB")
    except Exception as e:
        log(f"  像素检测失败: {e}")
        return "?"
    px = img.load()
    dark = 0
    for y in range(0, img.size[1], 2):
        for x in range(0, img.size[0], 2):
            r, g, b = px[x, y]
            if r + g + b < 400:          # 实测：灰色占位符 sum≈600；空框暗点 37，7 个黑字 146
                dark += 1                # （旧值 sum<450 / >25 会把空框误判成有字）
    return "ink" if dark > 60 else ""


def ocr_shot(crop: str | None = None, scale: int = 3):
    """一次 PowerShell+OCR 同时拿到窗口尺寸与识别行：(size, rows)。

    size 从同一次输出的 WINDOW 行解析，省掉为拿尺寸再起一次 PowerShell
    （每次冷启 PowerShell + WinRT OCR 约 2~4s，是耗时大头）。
    """
    text = _ocr_raw(crop, scale)
    size = None
    m = re.search(r"size=(\d+)x(\d+)", text)
    if m:
        size = (int(m.group(1)), int(m.group(2)))
    rows = []
    for ln in text.splitlines():
        mm = re.match(r"\s*(\d+),\s*(\d+)\s+(\d+),\s*(\d+)\s*\|\s*(.*)$", ln)
        if mm:
            rows.append((int(mm.group(1)), int(mm.group(2)), int(mm.group(3)),
                         int(mm.group(4)), mm.group(5).strip()))
    return size, rows


def ocr_lines(crop: str | None = None, scale: int = 3):
    """返回 [(x1,y1,x2,y2,text)]；crop 形如 'x,y,w,h'。"""
    return ocr_shot(crop, scale)[1]


def window_size():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-File", str(OCR_PS1),
                              "-ProcId", "0", "-RawOnly"],
                             capture_output=True, timeout=60).stdout or b""
    except Exception:
        return None
    text = out.decode("utf-8", "replace")
    m = re.search(r"size=(\d+)x(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def input_row_text(size=None) -> str:
    """读底部输入行；返回识别到的文字（空串=无墨迹）。

    size 可传入复用，避免多起一次 PowerShell。
    """
    if not size:
        size = window_size()
    if not size:
        return "<unavailable>"
    W, H = size
    crop = f"{int(W*0.38)},{int(H*0.60)},{int(W*0.60)},{int(H*0.09)}"
    rows = ocr_lines(crop)
    txt = " ".join(t for *_, t in rows if t)
    if re.search(r"按住|语音输入|输入文字", txt):
        return ""
    return txt


def read_context(chat: str, limit: int = 5):
    """用可选的 reader 独立读原文（不碰界面）；未配置 WX_READER 时返回空。"""
    if READER is None or not Path(READER).exists():
        return []
    try:
        out = subprocess.run([SYS_PY, str(READER), "history", "--talker", chat,
                              "--limit", str(limit), "--display-order", "desc"],
                             capture_output=True, timeout=60, text=True,
                             encoding="utf-8", errors="replace",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        raw = out.stdout or ""
        i = raw.find("{")
        if i < 0:
            return []
        msgs = list(reversed(json.loads(raw[i:])["data"]["messages"]))
        return [{"time": m.get("time"), "from_me": bool(m.get("from_me")),
                 "sender": m.get("sender") or "", "text": (m.get("text") or "").strip()[:80]}
                for m in msgs]
    except Exception as e:
        log(f"  reader 读取失败: {e}")
        return []


# --------------------------------------------------------------- 微信 GUI 封装
class Gui:
    """常驻的 WeChatGUI 句柄；只在需要时创建。"""

    def __init__(self):
        self._gui = None
        self._chat = None
        self.front_started = None
        self.topmost_made = False

    def get(self):
        if self._gui is None:
            from wechatauto import WeChatGUI
            t0 = time.time()
            self._gui = WeChatGUI()
            log(f"  WeChatGUI 就绪（{time.time()-t0:.1f}s）")
        return self._gui

    def current_chat_live(self):
        """现场用 UIA 读当前打开的会话名（不信任任何缓存）。

        库自带的 `_current_chat` 是它自己的缓存，**使用者手动切换会话后不会更新**——
        2026-09-20 就是因此把草稿填进了错误的会话：缓存说目标已打开，实际打开的是另一个。
        """
        try:
            u = self.get()._get_uia()
            if u is None or not u.is_materialized():
                return None
            return u.current_chat()
        except Exception:
            return None

    def open(self, chat: str) -> bool:
        t0 = time.time()
        if self.current_chat_live() == chat:      # 现场核对，不信任缓存
            self._chat = chat
            return True
        ok = bool(self.get().open_chat(chat))
        live = self.current_chat_live()
        if ok and (live is None or live == chat):
            self._chat = chat
        else:
            log(f"  打开会话 {chat} 后现场读到的是 {live!r}，判定未打开")
            ok = False
        mark(f"打开会话 {chat}", t0)
        return ok

    def hwnd(self) -> int:
        return int(getattr(self.get(), "main_hwnd", 0) or 0)

    def note_front(self) -> None:
        """记下“微信从这一刻起占着前台”，供统计占用时长。"""
        if self.front_started is None:
            self.front_started = time.time()

    # ---------------- UIA 直写（首选：不点击、不粘剪贴板、不敲键） ----------------
    def input_ctrl(self):
        """取当前会话的输入框 UIA 控件（需微信在前台，否则树是空壳）。"""
        try:
            u = self.get()._get_uia()
            if u is None:
                return None
            if not u.is_materialized():
                u.ensure_materialized(timeout=4.0)
            return u._chat_input()
        except Exception as e:
            log(f"  取输入框控件失败: {e}")
            return None

    def set_input_text(self, text: str):
        """用 ValuePattern 直接写入，返回 (成功, 回读值, 控件会话名)。

        实测（2026-09-20）：微信在前台时 chat_input_field 是标准 EditControl，
        ValuePattern 可写可读，SetValue 后回读与写入逐字一致；控件 Name 就是
        当前会话名，能先校验对象再写，避免写错会话。此路径不碰鼠标/剪贴板/键盘。
        """
        ctrl = self.input_ctrl()
        if ctrl is None:
            return False, "<no-ctrl>", ""
        name = ""
        try:
            name = ctrl.Name or ""
            vp = ctrl.GetValuePattern()
            vp.SetValue(text)
            time.sleep(0.35)
            back = ctrl.GetValuePattern().Value
            return back == text, back, name
        except Exception as e:
            return False, f"<err {type(e).__name__}>", name

    @staticmethod
    def chat_name_matches(ctrl_name: str, chat: str) -> bool:
        """控件 Name 是否属于目标会话。

        实测（2026-09-20）：chat_input_field 的 Name 有时就是会话名（"张三"），
        有时会拼上占位符/内容（"张三按住鼠标语音输入文字"）。所以只能用前缀匹配，
        等值比较会把成功写入误判为失败并降级到回退路径。
        """
        n = (ctrl_name or "").strip()
        c = (chat or "").strip()
        return bool(n) and bool(c) and (n == c or n.startswith(c))

    def read_input_text(self, expect_chat: str | None = None):
        """只读：取当前输入框文本（取不到返回 None）。

        expect_chat 传入时会先核对控件 Name（就是会话名），不对就返回 None，
        避免报出别的会话的内容。
        """
        ctrl = self.input_ctrl()
        if ctrl is None:
            return None
        try:
            if expect_chat and not self.chat_name_matches(ctrl.Name, expect_chat):
                return None
            return ctrl.GetValuePattern().Value
        except Exception:
            return None

    def press_enter_in_input(self) -> bool:
        """在输入框控件上按回车（UIA SetFocus + SendKeys）。"""
        ctrl = self.input_ctrl()
        if ctrl is None:
            return False
        try:
            ctrl.SetFocus()
            time.sleep(0.1)
            ctrl.SendKeys("{Enter}", waitTime=0.05)
            return True
        except Exception as e:
            log(f"  回车失败: {e}")
            return False

    def is_front(self) -> bool:
        h = self.hwnd()
        return bool(h) and foreground_window() == h

    def ensure_front(self) -> bool:
        """把微信切到前台。

        实测（2026-09-20，同一起点对比四种方法）：
          库 bring_to_front(keep_topmost=True) → 抢不到（返回 False）
          SwitchToThisWindow                    → 抢不到
          Alt 键 + SetForegroundWindow           → 成功 ✅（首选）
          置顶 + Alt + SetForegroundWindow       → 成功（备用）
        所以首选 alt_activate（不置顶，无悬浮副作用）；失败才退回库的置顶法，
        且退回后必须在收尾时 restore_topmost() 取消置顶。
        全程不最小化任何其它窗口，也绝不用库自带的 ensure_visible()
        （它会把你其它窗口最小化掉）。
        """
        if self.is_front():
            if self.front_started is None:
                self.front_started = time.time()
            return True
        # 1) 先试 Alt 键解锁法（不置顶，无副作用；分层升级已内建）
        if alt_activate(self.hwnd(), layer_log=log):
            if self.front_started is None:
                self.front_started = time.time()
            return True
        # 2) 退而求其次：库自带的置顶法（成功后必须 restore_topmost 收尾）
        try:
            r = self.get().bring_to_front(keep_topmost=True)
            self.topmost_made = True
            log(f"  bring_to_front(置顶)= {r}")
        except Exception as e:
            log(f"  切前台失败: {e}")
        time.sleep(0.4)
        if self.is_front():
            if self.front_started is None:
                self.front_started = time.time()
            return True
        return False

    def restore_topmost(self) -> None:
        """取消临时置顶，让微信回到普通 Z 序。"""
        if not self.topmost_made:
            return
        try:
            self.get().restore_zorder()
            self.topmost_made = False
        except Exception as e:
            log(f"  取消置顶失败: {e}")

    def require_front(self) -> bool:
        """输入前的前台闸门：不是前台就拒绘输入，避免把字打到你别的窗口里。"""
        if self.is_front():
            self.note_front()
            return True
        if self.ensure_front():
            return True
        log(f"  ✗ 前台闸门未通过：当前前台={foreground_window()} 微信主窗={self.hwnd()} "
            f"可见={bool(u32.IsWindowVisible(wintypes.HWND(self.hwnd())))} 置顶标志={self.topmost_made}")
        return False

    def box(self):
        try:
            return self.get().get_input_box()
        except Exception:
            return None


def wait_idle(need: float = IDLE_GATE, budget: float = IDLE_MAX_WAIT) -> bool:
    t0 = time.time()
    while True:
        if idle_seconds() >= need:
            return True
        if time.time() - t0 > budget:
            return False
        time.sleep(0.3)


def idle_detector_usable(need: float = 1.0, samples: int = 6,
                         interval: float = 0.35) -> bool:
    """自检：本机的 GetLastInputInfo 到底能不能用。

    实测（2026-09-20）本机取样 12 秒，空闲值始终 0.0~0.3s —— 有 HID 设备
    （翻页笔之类）在持续产生输入事件，“空闲”永远达不到。这种机器上拿空闲
    做闸门只会把任务卡死在队列里，必须整条禁用。
    """
    vals = []
    for _ in range(samples):
        vals.append(idle_seconds())
        time.sleep(interval)
    ok = max(vals) >= need
    log(f"空闲检测诊断：{samples*interval:.0f}s 内最长空闲 {max(vals):.1f}s → "
        f"{'能等到空闲窗口' if ok else '这段时间一直在输入（若长期如此，任务会一直排在队列里）'}")
    return ok


# ------------------------------------------------------------------- 各动作
def act_check(gui: Gui, job) -> dict:
    chat = job["chat"]
    ctx = read_context(chat, 5)          # 慢活放在抢焦点之前
    if not gui.require_front():
        return {"ok": False, "evidence": {"context": ctx},
                "error": "微信未能切到前台（你正在用别的窗口），读屏不可靠，已跳过"}
    opened = gui.open(chat)
    # 输入框内容分三层取：① UIA 精确回读（能拿到整段原文，且能核对会话名）；② 像素；
    # ③ OCR。实测像素法会把灰色占位符误判成有字，所以只当备用。
    val = gui.read_input_text(expect_chat=chat)
    if val is not None:
        input_row, via = ("（空）" if val == "" else val), "uia_value"
    else:
        ink = fast_ink()
        if ink == "ink":
            input_row, via = "（有内容，但 UIA 读不到原文）", "pixels"
        elif ink == "":
            input_row, via = "（空）", "pixels"
        else:
            row = input_row_text()
            input_row, via = (row or "（空）"), "ocr"
    size, rows = ocr_shot(None, scale=1)
    W, H = size or (993, 867)
    ev = {"opened": opened, "input_row": input_row, "input_row_via": via,
          "header": [t for x1, y1, x2, y2, t in rows if y1 < H * 0.15],
          "bottom": [t for x1, y1, x2, y2, t in rows if y1 >= H * 0.45 and x1 >= W * 0.40],
          "context": ctx}
    if not opened:
        return {"ok": False, "error": "会话未打开", "evidence": ev}
    return {"ok": True, "evidence": ev}


def act_draft(gui: Gui, job) -> dict:
    chat, text = job["chat"], job.get("text", "")
    ctx = read_context(chat, 3)          # 慢活（reader，不碰界面）放在抢焦点之前
    if not gui.require_front():
        return {"ok": False, "error": "微信未能切到前台（可能你正在操作别的窗口），本次未输入"}
    if not gui.open(chat):
        return {"ok": False, "error": "会话未打开，未输入"}
    if not gui.require_front():          # 开会话可能再次失焦点，输入前再卡一次
        return {"ok": False, "error": "输入前前台闸门未通过，本次未输入"}

    # 首选：UIA ValuePattern 直写 + 逐字回读
    _t = time.time()
    ok, back, ctrl_chat = gui.set_input_text(text)
    mark("ValuePattern 直写+回读", _t)
    if ok and gui.chat_name_matches(ctrl_chat, chat):
        return {"ok": True, "method": "value_pattern", "readback": back,
                "evidence": {"chat": chat, "text": text, "chars": len(text),
                             "ctrl_chat_name": ctrl_chat, "context": ctx}}
    if ctrl_chat and not gui.chat_name_matches(ctrl_chat, chat):
        # 控件属于别的会话：**绝不回退**。回退路径是“往当前打开的会话里粘贴”，
        # 2026-09-20 就是这条把草稿填进了错误会话（“午托”群），必须直接中止。
        return {"ok": False, "method": "aborted_wrong_chat", "readback": back,
                "error": f"目标会话未打开：输入框控件属于「{ctrl_chat}」，已中止且未继续输入",
                "evidence": {"chat": chat, "text": text, "ctrl_chat_name": ctrl_chat, "context": ctx}}
    log(f"  ValuePattern 未成功（回读={back!r} 控件会话={ctrl_chat!r}）")
    live = gui.current_chat_live()
    if live and not gui.chat_name_matches(live, chat):
        return {"ok": False, "error": f"当前打开的是「{live}」而不是目标会话，已中止（不回退粘贴）"}
    log("  回退库的粘贴路径（已确认现场会话就是目标）")

    # 回退：库的「点击输入框 + 剪贴板粘贴」（本机坐标空间不一致，不可靠，必验）
    box = gui.box()
    if not box:
        return {"ok": False, "error": "未探测到输入框，且 ValuePattern 不可用"}
    _t = time.time()
    filled = bool(gui.get().input_text(text, box=box, fast=True))
    mark("库粘贴路径", _t)
    time.sleep(0.3)
    _t = time.time()
    row = input_row_text()               # 回退路径一律用 OCR 读实文字
    mark("OCR 复核", _t)
    ok = bool(row) and not re.search(r"按住|语音输入|输入文字", row)
    return {"ok": ok, "method": "clipboard+paste", "filled_flag": filled, "input_row": row,
            "evidence": {"chat": chat, "text": text, "chars": len(text),
                         "ctrl_chat_name": ctrl_chat, "context": ctx},
            "error": None if ok else "填入未确认（库报 filled=%s，OCR 读回=%r）" % (filled, row)}


def act_clear(gui: Gui, job) -> dict:
    chat = job["chat"]
    if not gui.require_front():
        return {"ok": False, "error": "微信未能切到前台，未清空"}
    if not gui.open(chat):
        return {"ok": False, "error": "会话未打开"}
    if not gui.require_front():
        return {"ok": False, "error": "清空前前台闸门未通过"}

    # 首选：ValuePattern 置空 + 回读确认
    _t = time.time()
    ok, back, ctrl_chat = gui.set_input_text("")
    mark("ValuePattern 清空+回读", _t)
    if ctrl_chat and not gui.chat_name_matches(ctrl_chat, chat):
        return {"ok": False, "method": "aborted_wrong_chat",
                "error": f"目标会话未打开：输入框控件属于「{ctrl_chat}」，已中止（绝不会去清别的会话）"}
    if ok:
        return {"ok": True, "method": "value_pattern", "input_row": "",
                "evidence": {"chat": chat, "ctrl_chat_name": ctrl_chat}}
    live = gui.current_chat_live()
    if live and not gui.chat_name_matches(live, chat):
        return {"ok": False, "error": f"当前打开的是「{live}」而不是目标会话，已中止清空"}
    log(f"  ValuePattern 清空未成功（回读={back!r}），回退 Ctrl+A/Delete 路径")

    g = gui.get()
    for attempt in range(1, 4):
        if not gui.require_front():
            return {"ok": False, "error": "清空前前台闸门未通过"}
        _t = time.time()
        box = gui.box()
        if box:
            g.focus_input(box)
            g._input.key(0x41, ctrl=True)      # VK_A
            g._input.key(0x2E)                 # VK_DELETE
        time.sleep(0.8)
        ink = fast_ink()
        mark(f"清空尝试 {attempt}", _t)
        if ink == "":
            return {"ok": True, "attempts": attempt, "input_row": ""}
        if ink == "?":
            row = input_row_text()
            if not row:
                return {"ok": True, "attempts": attempt, "input_row": ""}
            log(f"  clear 第 {attempt} 次未生效（OCR），输入行: {row}")
        else:
            log(f"  clear 第 {attempt} 次未生效（像素）")
    row = input_row_text()
    return {"ok": False, "error": "3 次后输入行仍有墨迹", "input_row": row}


def duplicate_send_guard(last_msgs, text: str, force: bool):
    """“疑似重复发送”闸门：目标会话最近一条就是同内容（或互相包含）时返回错误 dict。

    为什么需要：使用者可能**在并行手动操作微信**（2026-09-20 实测：我说要发时他已经在手动发送同一条
    文字并手转发了卡片，结果发重了）。判定只看目标会话的最近一条，不做模糊联想；宁可多问一句。
    注意：本闸门依赖 `WX_READER`（要读聊天记录），未配置时直接放行并记日志。
    """
    if force:
        return None
    if not last_msgs:
        return None
    last = last_msgs[-1]
    lt = (last.get("text") or "").strip()
    nt = (text or "").strip()
    if last.get("from_me") and lt and nt and (lt == nt or nt in lt or lt in nt):
        return {"ok": False, "method": "blocked_duplicate_send",
                "error": (f"已中止发送：目标会话最近一条（{last.get('time')}）已是相同内容"
                          f"（{lt[:40]!r}…），疑似重复（使用者可能已手动发过）。"
                          "确实要再发一次请加 --force"),
                "evidence": {"last": last}}
    return None


def act_send(gui: Gui, job) -> dict:
    chat, text = job["chat"], job.get("text", "")
    if not job.get("confirm"):
        return {"ok": False, "error": "缺少 confirm=true，拒绝发送"}
    pre = read_context(chat, 1)
    if READER is None:
        log("  未配置 WX_READER，跳过“疑似重复发送”检查")
    dup = duplicate_send_guard(pre, text, bool(job.get("force")))
    if dup:
        return dup
    if not gui.require_front():
        return {"ok": False, "error": "微信未能切到前台，未发送"}
    if not gui.open(chat):
        return {"ok": False, "error": "会话未打开，未发送"}
    if not gui.require_front():
        return {"ok": False, "error": "发送前前台闸门未通过，未发送"}

    # 首选：ValuePattern 直写（回读逐字一致才继续）
    _t = time.time()
    ok, back, ctrl_chat = gui.set_input_text(text)
    mark("ValuePattern 直写+回读", _t)
    method = "value_pattern"
    if ok and gui.chat_name_matches(ctrl_chat, chat):
        _t = time.time()
        if not gui.press_enter_in_input():
            return {"ok": False, "error": "已写入但回车未发出", "readback": back}
        mark("回车发送", _t)
    elif ctrl_chat and not gui.chat_name_matches(ctrl_chat, chat):
        # 控件属于别的会话：**绝不发送，也不回退**
        return {"ok": False, "method": "aborted_wrong_chat", "readback": back,
                "error": f"目标会话未打开：输入框控件属于「{ctrl_chat}」，已中止（绝不发送）"}
    else:
        live = gui.current_chat_live()
        if live and not gui.chat_name_matches(live, chat):
            return {"ok": False, "error": f"当前打开的是「{live}」而不是目标会话，已中止（绝不发送）"}
        log(f"  ValuePattern 未成功（回读={back!r}），回退库的粘贴+click_send")
        method = "clipboard+click_send"
        box = gui.box()
        if not box:
            return {"ok": False, "error": "未探测到输入框"}
        if not gui.get().input_text(text, box=box, fast=True):
            row = input_row_text()
            if not row:
                return {"ok": False, "error": "填入失败，未发送"}
        time.sleep(0.4)
        gui.get().click_send()

    # 复核分三层：①配了 reader 就用 reader 回读原文（最强证据）；
    # ② 否则看 UIA 输入框是否已清空（发出后微信会清空输入框）；③再退到 OCR 读屏
    last_ctx = []
    if READER is not None:
        for i in range(4):
            time.sleep(1.5 if i == 0 else 2.5)
            last_ctx = read_context(chat, 1)
            if last_ctx and last_ctx[-1]["from_me"] and last_ctx[-1]["text"].strip() == text.strip():
                return {"ok": True, "verified": "reader", "method": method,
                        "evidence": {"sent": last_ctx[-1], "before_last": pre[-1] if pre else None}}
    else:
        for _ in range(6):
            time.sleep(0.5)
            if gui.read_input_text() == "":
                return {"ok": True, "verified": "uia_input_cleared", "method": method,
                        "evidence": {"before_last": pre[-1] if pre else None,
                                     "note": "未配置 WX_READER；以 UIA 回读输入框已清空为判据"}}
    row = input_row_text()
    if not row:
        return {"ok": True, "verified": "input_row_cleared", "method": method,
                "evidence": {"sent": last_ctx[-1] if last_ctx else None,
                             "note": "输入框已清空但未能回读到消息原文"}}
    return {"ok": False, "error": "发送后未能确认发出", "input_row": row, "method": method,
            "hint": "先到微信确认是否已发出，不要直接重发（库的 click_send/_verify_sent 会假阴性）"}


ACTIONS = {"check": act_check, "draft": act_draft, "clear": act_clear, "send": act_send}

GUI = Gui()          # 模块级单例：WeChatGUI / UIA 引擎跨任务复用，不每单重冷启


# --------------------------------------------------------------------- 主循环
def claim_job():
    QUEUE.mkdir(parents=True, exist_ok=True)
    jobs = sorted(p for p in QUEUE.glob("*.job"))
    for p in jobs:
        working = p.with_suffix(".working")
        try:
            p.rename(working)
        except OSError:
            continue
        try:
            return working, json.loads(working.read_text(encoding="utf-8"))
        except Exception:
            working.rename(working.with_suffix(".bad"))
    return None, None


def write_result(job_id: str, payload: dict) -> None:
    tmp = QUEUE / f".{job_id}.result.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(QUEUE / f"{job_id}.result")


def mark(label: str, t0: float) -> None:
    """分阶段计时，写进日志，方便找出到底哪一步慢。"""
    log(f"  · {label} {time.time() - t0:.1f}s")


def load_pending():
    """读“待发草稿”记录；无记录返回 None。"""
    try:
        return json.loads(PENDING.read_text(encoding="utf-8"))
    except Exception:
        return None


def remember_pending(chat: str, text: str, job_id: str) -> None:
    try:
        PENDING.write_text(json.dumps({"chat": chat, "text": text, "job": job_id,
                                       "at": datetime.now().isoformat(timespec="seconds")},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"  记录待发草稿失败: {e}")


def forget_pending() -> None:
    try:
        PENDING.unlink()
    except OSError:
        pass


def run_job(job) -> dict:
    action = job.get("action")
    fn = ACTIONS.get(action)
    if not fn:
        return {"ok": False, "error": f"未知动作 {action}"}

    # 闸门：已有未发送的草稿时，禁止切到别的会话（切换会把草稿冲掉，2026-09-20 实测）
    pend = load_pending()
    if pend and action == "check" and not job.get("force"):
        if not Gui.chat_name_matches(pend.get("chat"), job.get("chat")):
            return {"ok": False, "method": "blocked_by_pending_draft",
                    "error": (f"已阻止切换会话：「{pend.get('chat')}」里有已填入未发送的草稿"
                              f"（{pend.get('text')!r}），切走会把它冲掉。"
                              "要检查其它会话请先处理该草稿（wx send --confirm 或 wx clear），"
                              "确实要先看别的会话就加 --force")}
    gui = GUI
    gui.front_started = None        # 每个任务重新计“微信在前台占了多久”
    started = time.time()
    prev_fg = foreground_window()
    prev_cursor = cursor_pos()
    state = ensure_window_usable()
    gui.note_front()
    mark("窗口准备", started)
    log(f"[{job['id']}] {action} chat={job.get('chat')!r} window={state} "
        f"idle={idle_seconds():.1f}s")
    if state == "no_window":
        return {"ok": False, "error": "微信主窗口不可用（未登录/已退出？）", "window": state}
    try:
        res = fn(gui, job)
    except Exception:
        res = {"ok": False, "error": "异常: " + traceback.format_exc(limit=3)}
    res.setdefault("ok", False)
    # 待发草稿记账：draft 成功则记下（挡住后续切会话）；send/clear 成功则注销
    if res.get("ok"):
        if action == "draft":
            remember_pending(job.get("chat", ""), job.get("text", ""), job.get("id", ""))
        elif action in ("send", "clear"):
            forget_pending()
    res["window"] = state
    res["seconds"] = round(time.time() - started, 1)
    res["foreground_seconds"] = (round(time.time() - gui.front_started, 1)
                                 if gui.front_started else 0)
    # 收尾还原：取消置顶 → 光标 → 原前台窗口
    try:
        gui.restore_topmost()
        if cursor_pos() != prev_cursor:
            u32.SetCursorPos(prev_cursor[0], prev_cursor[1])
        now_fg = foreground_window()
        if now_fg and now_fg != prev_fg and u32.IsWindowVisible(wintypes.HWND(prev_fg)):
            if not force_foreground(prev_fg):
                log(f"  焦点归还失败（你原来的窗口 {prev_fg} 未抢回前台）")
    except Exception as e:
        log(f"  收尾还原异常: {e}")
    return res


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text().strip())
            if old != os.getpid() and k32.OpenProcess(0x1000, False, old):
                log(f"已有服务在跑（pid={old}），退出")
                return 3
        except Exception:
            pass
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    idle_ok = idle_detector_usable(need=2.0, samples=20, interval=0.3)
    gate = IDLE_GATE                 # 闸门始终生效：只在真正空闲时才动手
    log(f"服务启动 pid={os.getpid()} idle_gate={gate}s")
    STATEFILE.write_text(json.dumps(
        {"pid": os.getpid(), "idle_gate": gate, "idle_detector_saw_idle": idle_ok,
         "heartbeat": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    # 清理 3 天前的结果文件，避免队列目录无限增长；把上次被杀进程遗留的 working 任务放回队列
    try:
        cut = time.time() - 3 * 86400
        for p in QUEUE.glob("*.result"):
            if p.stat().st_mtime < cut:
                p.unlink()
        for p in QUEUE.glob("*.working"):
            p.rename(QUEUE / p.name.replace(".working", ".job"))
            log(f"  遗属任务放回队列: {p.name}")
    except Exception:
        pass
    processed = 0
    while True:
        working, job = claim_job()
        if not working:
            time.sleep(POLL)
            continue
        # 写字的动作（draft/send）等一次真正的停顿；只读/纠错的（check/clear）不苛求
        need = IDLE_GATE if job.get("action") in ("draft", "send") else 0.5
        if not wait_idle(need, IDLE_MAX_WAIT):
            log(f"[{job.get('id')}] 你一直在操作电脑，任务放回队列等下一轮")
            working.rename(QUEUE / working.name.replace(".working", ".job"))
            time.sleep(2.0)
            continue
        log(f"[{job.get('id')}] 领到任务 {job.get('action')} chat={job.get('chat')!r}，"
            f"空闲 {idle_seconds():.1f}s，开始执行")
        _t = time.time()
        t0 = _t
        res = run_job(job)
        res.update({"id": job.get("id"), "action": job.get("action"),
                    "chat": job.get("chat"), "text": job.get("text"),
                    "finished_at": datetime.now().isoformat(timespec="seconds")})
        write_result(job["id"], res)
        processed += 1
        STATEFILE.write_text(json.dumps(
            {"pid": os.getpid(), "processed": processed, "last": res,
             "heartbeat": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"[{job['id']}] 完成 ok={res.get('ok')} {time.time()-t0:.1f}s "
            f"idle_gate={idle_seconds():.1f}s {res.get('error') or ''}")
        try:
            working.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
