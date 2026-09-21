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

# 运行期配置文件：<WX_DIR>/wx-service.env（KEY=VALUE，每行一个）。
# 让「静默档」这类本机偏好不用改代码、也不用改自启包装就能生效。
_ENVFILE = BASE / "wx-service.env"
if _ENVFILE.exists():
    try:
        for _line in _ENVFILE.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass

IDLE_GATE = float(os.environ.get("WX_IDLE_GATE", "2.0"))     # 写字的动作要求的空闲秒数
IDLE_GATE_RO = float(os.environ.get("WX_IDLE_GATE_RO", "0.5"))   # 只读动作（check/clear）要求的空闲秒数
IDLE_MAX_WAIT = float(os.environ.get("WX_IDLE_MAX_WAIT", "300"))  # 等不到空闲就先放回队列
# 降级兜底：实测本机 20s 空闲的到达率可能是 0%（一直在用电脑，或 HID 设备持续
# 产生输入）。任务已等超过 IDLE_FALLBACK 秒时，退而求其次：只要能看到
# IDLE_FALLBACK_MIN 秒的安静窗口就执行，避免任务被永久饿死在队列里。
# 设为 0 = 禁用降级（宁可一直等，也绝不打断使用者）。
IDLE_FALLBACK = float(os.environ.get("WX_IDLE_FALLBACK", "600"))
IDLE_FALLBACK_MIN = float(os.environ.get("WX_IDLE_FALLBACK_MIN", "5"))
# 任务收尾时把上游库为了“让路”而最小化的遮挡窗口还原（推荐开）
RESTORE_BLOCKERS = os.environ.get("WX_RESTORE_BLOCKERS", "1").strip().lower() not in ("", "0", "false", "no")
BLOCKERS_STATE = BASE / "blockers.json"    # 被库最小化的窗口句柄（进程被杀后启动时兜底恢复）
# 打开会话的策略：auto（默认，先走我们的无点击路径、不行再回退库）/ uia（只用无点击路径，
# 失败就快速失败、绝不进库的级联）/ library（完全恢复成旧行为）。
OPEN_MODE = (os.environ.get("WX_OPEN_MODE", "auto") or "auto").strip().lower()

# 离屏注入：把微信窗口挪到屏幕外再干活（屏幕上什么都看不到）。
# 与 2026-09-21 上午被否掉的那版不同：当时打开会话说的是库的物理点击路径（窗口不在屏幕上就点不到），
# 现在打开会话与写入都改成了 ValuePattern + 回车（**零点击**），只剩“回车要前台”这一条约束，
# 而窗口挪到屏幕外依然可以是前台 —— 真静默因此成立。
# 开启后强制走 UIA 打开路径（不回退库的点击路径）、失败就干净失败。
OFFSCREEN = os.environ.get("WX_OFFSCREEN", "0").strip().lower() not in ("", "0", "false", "no")
OFFSCREEN_X = int(os.environ.get("WX_OFFSCREEN_X", "-32000"))
SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x0004, 0x0010, 0x0400
WINDOW_STATE = BASE / "window-state.json"     # 离屏前的原位，供崩溃后兜底搬回
_OFFSCREEN_ACTIVE = False                     # 本任务是否正在离屏（供 Gui.open 判断）
# 发送前的确定性敏感词闸门（本地、离线，不依赖网络与模型）。
# 为什么单独硬拦这一层：金额与凭证发出去是不可逆的（发错账户/泄露验证码），
# 所以在这一步做一次确定性硬拦。只拦「客观危险」的两类（金额与凭证）；
# 承诺/时间类不拦——那是日常沟通的常态，由使用者在草稿阶段自己核对。
# 确实要照发必须显式加 --allow-high-risk（会记日志）。
HIGH_RISK_PATTERNS = (
    (re.compile(r"\b\d{16,19}\b"), "16-19 位长数字（银行卡/账号）"),
    (re.compile(r"验证码|校验码|短信码|动态码|一次性密码", re.I), "验证码"),
    (re.compile(r"密码|口令|password|passwd|私钥|密钥|api[\s_-]?key", re.I), "密码/密钥"),
    (re.compile(r"(身份证|银行卡|卡号|账号|账户)\s*[:：]?\s*\d"), "证件/账号信息"),
    (re.compile(r"(转账|汇款|打款|付款|收款码|红包|提现)\s*[:：]?\s*\d"), "转账/付款金额"),
)
POLL = 0.4

# 关于“真静默（把窗口挪到屏幕外）”，两次实测的结论正好相反，以 2026-09-22 为准：
#   2026-09-21 否定——当时打开会话说的是库的物理点击路径（SetCursorPos + mouse_event），
#     窗口不在屏幕上就点不到；而且库自己会改写窗口几何，外部“记原位再还原”不可靠。
#   2026-09-22 推翻——打开会话与写入都改成了 ValuePattern + 回车（零点击），只剩“回车要前台”
#     这一条约束，而窗口即使在屏幕外也能是前台 → 离屏注入成立（WX_OFFSCREEN=1，实测写入落在
#     输入框、且前台窗口的屏幕矩形在 -32000）。但仍有两条硬约束：
#       ① 离屏期间必须走无点击路径，所以强制不回退库的点击级联（Gui.open 里看 _OFFSCREEN_ACTIVE）；
#       ② 不能用 SetWindowPlacement 改 rcNormalPosition 来“搬”——最小化中的窗口随后
#          SW_RESTORE 不走那个位置（实测任务期间窗口出现在 (0,0)+1305x828，屏幕上看得到）。
#          必须先 SW_RESTORE 再 SetWindowPos 直搬，并用 GetWindowRect 现场校验。
# 时机闸门（WX_IDLE_GATE）仍保留：它管的是“什么时候动手”，与“动手时能不能被看见”是两件事。

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# 显式声明原型：64 位下句柄按默认 c_int 传会被截断（本文件 2026-09-21 已踩过一次）。
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetWindowRect.restype = wintypes.BOOL
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.SetWindowPos.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.ShowWindow.restype = wintypes.BOOL
u32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]
u32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]


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
    # 注意：上游库会修改共享的 windll.user32 函数原型（restype），此时
    # GetForegroundWindow() 在没有前台窗口时返回的是 **None** 而不是 0，
    # 直接 int() 会抛 “int() argument must be ... not 'NoneType'” 并把整个任务打挂
    # （2026-09-21 实际踩到两次，traceback 落在 require_front → is_front）。
    return int(u32.GetForegroundWindow() or 0)


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


# ------------------------------------------------ 离屏注入（真静默）与位置还原
class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", wintypes.RECT),
                ("rcDevice", wintypes.RECT)]


def _get_placement(hwnd: int):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not u32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
        return None
    return wp


def _placement_dict(wp) -> dict:
    r = wp.rcNormalPosition
    return {"showCmd": int(wp.showCmd),
            "normal": [int(r.left), int(r.top), int(r.right), int(r.bottom)]}


# 注意：**不要**用 SetWindowPlacement 改 rcNormalPosition 来“搬窗口”——最小化中的窗口
# 随后 SW_RESTORE 不走那个位置（2026-09-22 实测：任务期间窗口照样出现在屏幕正中）。
# 该 API 本文件已不再使用；搬迁一律 SetWindowPos + GetWindowRect 现场校验。


def _window_rect(hwnd: int):
    r = wintypes.RECT()
    if not u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def _is_offscreen(rect) -> bool:
    """右边界 ≤ 0 才算“屏幕上完全看不到”。

    注意：Windows 会把窗口位置夹到 -25600（不是 -32000），所以**不能拿 OFFSCREEN_X 比对**，
    否则会把真正成功的离屏误判为失败（2026-09-22 踩到）。
    """
    return bool(rect) and rect[2] <= 0


def _go_offscreen(hwnd: int):
    """把微信窗口挪到屏幕外（屏幕上什么都看不到），返回还原信息；搬不动则返回 None。

    实测教训（2026-09-22）：**不能用 SetWindowPlacement 改 rcNormalPosition 来搬**——
    窗口处于最小化时，随后的 ShowWindow(SW_RESTORE) 不走那个位置，任务期间窗口照样
    出现在屏幕正中（采样实测 (0,0)+1305x828）。正确做法：
      ① 最小化时先对“最小化的窗口”SetWindowPos（它会写进恢复位置），再 SW_RESTORE；
         若恢复位置没跟着走，才退回“先恢复再搬”（代价是屏幕上一瞬间可见）；
      ② 每次都用 GetWindowRect 现场校验，没搬动就放弃离屏——宁可被看见，
         也不能把无点击路径弄成不可用。
    """
    wp = _get_placement(hwnd)
    if not wp:
        return None
    cur = _placement_dict(wp)
    r = cur["normal"]
    w, h = max(1, r[2] - r[0]), max(1, r[3] - r[1])
    minimized = int(wp.showCmd) == 2
    swp = SWP_NOZORDER | SWP_NOACTIVATE
    if not u32.IsWindowVisible(wintypes.HWND(hwnd)) and not minimized:
        u32.ShowWindow(wintypes.HWND(hwnd), 8)           # 托盘/隐藏状态：先显形（不抢焦点）
        time.sleep(0.2)
    if minimized:
        # 先让微信自己恢复完（Qt 会把自己的缓存几何应用上），**随后**再搬。
        # 反过来（先搬再 SW_SHOWNA）实测不行：应用会在我们搬完之后自己把窗口
        # 恢复到 (1297,243) 盖掉我们的位置（2026-09-22 用 0.08s 采样器抓到）。
        u32.ShowWindow(wintypes.HWND(hwnd), 9)           # SW_RESTORE
        time.sleep(0.25)
    rect = _window_rect(hwnd)
    if rect:
        w, h = max(1, rect[2] - rect[0]), max(1, rect[3] - rect[1])
    # 收敛循环：应用有时会把窗口抢回原位，搬完复检，最多试三拍。
    for i in range(3):
        u32.SetWindowPos(wintypes.HWND(hwnd), None, OFFSCREEN_X, 0, w, h, swp)
        time.sleep(0.18)
        rect = _window_rect(hwnd)
        if _is_offscreen(rect):
            break
        time.sleep(0.12)
    if not _is_offscreen(rect):
        log(f"  [离屏] 搬窗口失败（实际 {rect}），本次按普通模式执行")
        if minimized:
            u32.ShowWindow(wintypes.HWND(hwnd), 7)      # 还回最小化
        return None
    try:
        WINDOW_STATE.write_text(json.dumps({"hwnd": int(hwnd), **cur}), encoding="utf-8")
    except Exception:
        pass
    return cur


def _restore_placement(hwnd: int, cur: dict) -> None:
    """把窗口搬回原位并恢复原显示状态；全程先隐藏，避免搬回去的路上在屏幕上露面。"""
    if cur:
        r = cur["normal"]
        w, h = max(1, r[2] - r[0]), max(1, r[3] - r[1])
        u32.ShowWindow(wintypes.HWND(hwnd), 0)          # SW_HIDE
        time.sleep(0.1)
        u32.SetWindowPos(wintypes.HWND(hwnd), None, r[0], r[1], w, h,
                         SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
        time.sleep(0.12)
        sc = int(cur.get("showCmd", 1))
        if sc == 2:
            u32.ShowWindow(wintypes.HWND(hwnd), 7)      # 恢复成最小化，且不抢焦点
        elif sc == 3:
            u32.ShowWindow(wintypes.HWND(hwnd), 3)      # SW_MAXIMIZE
        else:
            u32.ShowWindow(wintypes.HWND(hwnd), 8)      # SW_SHOWNA
    try:
        WINDOW_STATE.unlink()
    except Exception:
        pass


def _recover_window_state() -> None:
    """启动兜底：上次离屏后进程被杀，把窗口搬回原位。"""
    if not WINDOW_STATE.exists():
        return
    try:
        d = json.loads(WINDOW_STATE.read_text(encoding="utf-8"))
        hwnd = int(d["hwnd"])
        if u32.IsWindow(wintypes.HWND(hwnd)):
            _restore_placement(hwnd, {"showCmd": int(d.get("showCmd", 1)), "normal": d["normal"]})
            log(f"  启动兜底：微信窗口已从屏幕外搬回 {d['normal']}")
            return
    except Exception as e:
        log(f"  窗口位置兜底恢复失败: {e}")
    try:
        WINDOW_STATE.unlink()
    except Exception:
        pass


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

    def current_chat_raw(self):
        """便宜版现场读会话名：直接读输入框控件的 Name，不做 is_materialized 深检。

        实测 `current_chat_live()` 的深检每次约 1.5s，而“打开会话”路径上要查两次——
        这就是打开动作从 2.7s 变成 6s 的差额。这里只读控件名，代价是控件未物化时
        可能返回 None；调用方把 None 当“未确认”处理即可（真正的安全闸门是写入时
        对控件会话名的核对，不靠这个读）。
        """
        try:
            import uiautomation as auto
            aid = "chat_input_field"
            try:
                from wechatauto.uia_driver import CHAT_INPUT_AID as aid  # noqa: F811
            except Exception:
                pass
            win = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
            if not win.Exists(1):
                return None
            e = win.EditControl(AutomationId=aid)
            return (e.Name or None) if e.Exists(0.3) else None
        except Exception:
            return None

    def open(self, chat: str) -> bool:
        t0 = time.time()
        _cur = self.current_chat_raw()
        if _cur and Gui.chat_name_matches(_cur, chat):   # 现场核对，不信任缓存
            self._chat = chat
            return True
        if OPEN_MODE in ("auto", "uia"):
            try:
                how = open_chat_uia(chat)
            except Exception as e:                    # 无点击路径任何异常都不得让任务失败
                log(f"  UIA 打开路径异常（{e}）")
                how = ""
            if how:
                self._chat = chat
                log(f"  打开会话 {chat}：UIA 路径（{how}）{time.time()-t0:.1f}s")
                mark(f"打开会话 {chat}", t0)
                return True
            if OPEN_MODE == "uia" or _OFFSCREEN_ACTIVE:
                log(f"  UIA 路径未成功（{time.time()-t0:.1f}s），"
                    f"{'离屏模式' if _OFFSCREEN_ACTIVE else 'WX_OPEN_MODE=uia'} → 不进库的点击级联")
                mark(f"打开会话 {chat}", t0)
                return False
            log(f"  UIA 路径未成功（{time.time()-t0:.1f}s），回退库的 open_chat")
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


def _job_waited(job) -> float:
    """任务从入库到现在等了多久（秒）。用 job 里的 created 时间戳，无需额外记账。"""
    try:
        return max(0.0, (datetime.now() -
                         datetime.fromisoformat(str(job.get("created")))).total_seconds())
    except Exception:
        return 0.0


# ---------------------------------------------- 上游库扰民行为：进程内补丁
# 为什么要打补丁（2026-09-21 实测）：
#   WeChatGUI._minimize_blockers（guia.py:915，被 ensure_visible 调用）会把所有
#   与微信主窗重叠的其它顶层窗口 ShowWindow(h, 6) 最小化，而且**从不还原**。
#   这个行为对库本身是必需的：它用物理鼠标点击，窗口被覆盖时点击会被覆盖层接走。
#   所以本工具不阻止它，而是把被最小化的窗口记下来，任务收尾时用库自己的
#   _restore_keep_maximize 还原（那个函数会保留最大化状态）。
# 另外：不做"记住微信窗口几何再还原"——库自身会在任务中改写几何，
#   _minimize_blockers 之外还有 _restore_keep_maximize 等路径，外部记录不可靠
#   （实测还原后落到别的坐标），所以只负责把②的遮挡窗口还回去。
_LIB_MINIMIZED: list[int] = []
_PATCHED = False


def _persist_blockers() -> None:
    """把“被库最小化的窗口”落盘：服务若在任务中途被杀，那些窗口不该永远留在最小化态。"""
    try:
        BLOCKERS_STATE.write_text(json.dumps(_LIB_MINIMIZED), encoding="utf-8")
    except Exception as e:
        log(f"  记录遮挡窗口失败（不影响本次任务）: {e}")


def clear_blockers_state() -> None:
    try:
        BLOCKERS_STATE.unlink()
    except Exception:
        pass


def recover_blockers() -> int:
    """服务启动时的兜底：上次进程被杀时被库最小化的窗口，能还原的还原。"""
    if not BLOCKERS_STATE.exists():
        return 0
    try:
        hwnds = json.loads(BLOCKERS_STATE.read_text(encoding="utf-8"))
    except Exception:
        clear_blockers_state()
        return 0
    try:
        from wechatauto import guia
        restore_one = getattr(guia, "_restore_keep_maximize", None)
    except Exception:
        restore_one = None
    n = 0
    for h in (hwnds if isinstance(hwnds, list) else []):
        try:
            if not u32.IsWindow(wintypes.HWND(h)) or not u32.IsIconic(wintypes.HWND(h)):
                continue
            if restore_one is not None:
                restore_one(u32, h)
            else:
                u32.ShowWindow(wintypes.HWND(h), 9)      # SW_RESTORE
            n += 1
        except Exception:
            pass
    clear_blockers_state()
    if n:
        log(f"  启动兜底：已还原上次遗留的被最小化窗口 {n} 个")
    return n


def install_library_patches() -> None:
    """给 wechatauto 的“最小化遮挡窗口”行为打进程内补丁（不修改 site-packages）。"""
    global _PATCHED
    if _PATCHED:
        return
    try:
        from wechatauto import guia
    except Exception as e:
        log(f"  库补丁跳过（导入 wechatauto 失败）: {e}")
        return
    cls = getattr(guia, "WeChatGUI", None)
    if cls is None or not hasattr(cls, "_minimize_blockers"):
        log("  库补丁跳过（找不到 WeChatGUI._minimize_blockers）")
        return
    orig = cls._minimize_blockers

    def _visible_set() -> set:
        return {h for h, _pid, vis, iconic, _cls, _t in enum_top_windows() if vis and not iconic}

    def patched(self):
        before = _visible_set()
        try:
            n = orig(self)
        except Exception as e:
            log(f"  _minimize_blockers 异常: {e}")
            n = 0
        for h in before - _visible_set():
            if h not in _LIB_MINIMIZED:
                _LIB_MINIMIZED.append(h)
        if _LIB_MINIMIZED:
            _persist_blockers()
        return n

    cls._minimize_blockers = patched
    _PATCHED = True
    log("  已给上游库打补丁：遮挡窗口被它最小化后会由本工具还原")


def restore_library_minimized() -> int:
    """把上游库为“让路”而最小化的窗口还原（保留其最大化状态）。"""
    pending = list(_LIB_MINIMIZED)
    _LIB_MINIMIZED.clear()
    if not pending or not RESTORE_BLOCKERS:
        return 0
    try:
        from wechatauto import guia
        restore_one = getattr(guia, "_restore_keep_maximize", None)
    except Exception:
        restore_one = None
    n = 0
    for h in pending:
        try:
            if not u32.IsWindow(wintypes.HWND(h)):
                continue
            if not u32.IsIconic(wintypes.HWND(h)):
                continue          # 已经不是最小化态（使用者自己动过）→ 不碰
            if restore_one is not None:
                restore_one(u32, h)
            else:
                u32.ShowWindow(wintypes.HWND(h), 9)     # SW_RESTORE
            n += 1
        except Exception as e:
            log(f"  还原遮挡窗口 {h} 失败: {e}")
    if n:
        log(f"  已还原被库最小化的遮挡窗口 {n} 个")
    clear_blockers_state()
    return n


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
    # 草稿路径不拦（草稿本来就要使用者核对），但把命中敏感类别的草稿在卡片上提醒一行
    hits = high_risk_hits(text)
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
                "high_risk": hits,
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


def high_risk_hits(text: str) -> list:
    """返回文本命中的高风险类别（本地正则，不联网）。"""
    return [label for pat, label in HIGH_RISK_PATTERNS if pat.search(text or "")]


def open_chat_uia(chat: str) -> str:
    """无点击打开会话：搜索框 UIA `ValuePattern` 直写 + 回车。

    返回 "already" / "search" / ""（未成功，调用方决定是否回退库的路径）。

    为什么比库的路径可靠（2026-09-21 实测）：库走的是“点击搜索框 + 剪贴板 Ctrl+V +
    SendKeys + 点击搜索结果项”，三个易碎环节，而且它为此必须先把挡着微信的窗口
    **最小化**（`_minimize_blockers`）；失败时还会进入无超时的级联（实测同一会话
    一次 63.5s 失败、下一次 4.3s 成功）。这里全程 UIA 写值 + 键盘回车：不点鼠标、
    不用剪贴板、不跑 OCR，也不触发最小化。

    为什么不用侧栏列表项：侧栏虽然能用 UIA 枚举（`session_item_<会话名>`），但实测
    `SelectionItem.Select()` / `Invoke()` **不能真正切换会话**（等 2.4s 白费），而且
    它是虚拟列表、看不到非置顶会话、也没有 UIA 滚动。所以侧栏这条只用于“是否已打开”
    的判断，真正的打开动作全靠搜索框。
    """
    if Gui.chat_name_matches(GUI.current_chat_raw(), chat):
        return "already"
    try:
        import uiautomation as auto
    except Exception as e:
        log(f"  UIA 打开路径不可用（{e}）")
        return ""
    # 常量优先用上游库的（它是权威），拿不到就用本地回退值——库改了也不至于全挂
    try:
        from wechatauto.uia_driver import (CHAT_INPUT_AID, SEARCH_EDIT_CLASSES,
                                           SEARCH_EDIT_NAME, SEARCH_LIST_AIDS)
    except Exception:
        CHAT_INPUT_AID = "chat_input_field"
        SEARCH_EDIT_CLASSES = ("mmui::XValidatorTextEdit",)
        SEARCH_EDIT_NAME = "搜索"
        SEARCH_LIST_AIDS = ("search_result_list",)
    try:
        win = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
        if not win.Exists(3):
            return ""
    except Exception:
        return ""
    box_in = win.EditControl(AutomationId=CHAT_INPUT_AID)

    def live():
        return box_in.Name if box_in.Exists(0.3) else None

    # ② 搜索框直写 + 回车。不点鼠标、不粘剪贴板。
    box = None
    for cls in SEARCH_EDIT_CLASSES:
        for kw in (dict(ClassName=cls, Name=SEARCH_EDIT_NAME),
                   dict(Name=SEARCH_EDIT_NAME), dict(ClassName=cls)):
            e = win.EditControl(**kw)
            if e.Exists(0.6, 0.15):
                box = e
                break
        if box is not None:
            break
    if box is None:
        return ""
    try:
        vp = box.GetValuePattern()
        if vp is None:
            return ""
        vp.SetValue("")
        time.sleep(0.15)
        vp.SetValue(chat)
    except Exception as e:
        log(f"  搜索框直写失败（{e}）")
        return ""

    # 结果列表用纯 UIA 直接读（不经库的 _collect_results）：那条路每次内部要花 3 秒找列表，
    # 而且会构造 WeChatGUI → 触发它「最小化遮挡窗口」。分步实测：整段 2.65s，其中直写搜索框占 1.18s。
    def first_result_name():
        for aid in SEARCH_LIST_AIDS:
            try:
                lst_ = auto.ListControl(searchDepth=0xFFFFFFFF, AutomationId=aid)
            except Exception:
                continue
            if not lst_.Exists(0.2, 0.1):
                continue
            try:
                for k in lst_.GetChildren():
                    if not (k.AutomationId or ""):      # 无 aid 的行是分组标题（“最常使用”等）
                        continue
                    return (k.Name or "").strip()
            except Exception:
                continue
        return None

    # 只有确认“第一条结果就是目标”才敢回车——开错会话比打不开更糟。
    exact = False
    deadline = time.time() + 6.0
    while time.time() < deadline:
        first = first_result_name()
        if first:
            exact = Gui.chat_name_matches(first, chat)
            break
        time.sleep(0.15)
    if not exact:
        log("  搜索结果第一条不是目标会话 → 不回车（避免开错会话）")
        try:
            vp.SetValue("")
        except Exception:
            pass
        return ""
    try:
        box.SetFocus()
    except Exception:
        pass
    auto.SendKeys("{Enter}", waitTime=0.1)
    for _ in range(16):
        time.sleep(0.25)
        if Gui.chat_name_matches(live(), chat):
            try:
                vp.SetValue("")          # 清搜索框，避免残留影响下一次
            except Exception:
                pass
            return "search"
    log("  回车后现场读到的不是目标会话")
    try:
        vp.SetValue("")
    except Exception:
        pass
    return ""


def act_send(gui: Gui, job) -> dict:
    chat, text = job["chat"], job.get("text", "")
    if not job.get("confirm"):
        return {"ok": False, "error": "缺少 confirm=true，拒绝发送"}
    # 确定性敏感词闸门：在任何鼠标/键盘动作之前就拦下
    hits = high_risk_hits(text)
    if hits:
        if not job.get("allow_high_risk"):
            return {"ok": False, "method": "blocked_high_risk",
                    "error": ("已阻止发送：文本里含 " + "、".join(hits) + " 这类内容（金额/凭证发错不可逆）。"
                              "确认无误确要照发，加 --allow-high-risk")}
        log(f"  ⚠ 高风险内容经 --allow-high-risk 放行：{'、'.join(hits)}")
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

    # --now 的任务（immediate）优先取：否则它会排在一个正在等空闲的老任务后面，
    # 而那个老任务能把服务占住 IDLE_MAX_WAIT 那么久，插队能力就白给了。
    def _is_now(p):
        try:
            return bool(json.loads(p.read_text(encoding="utf-8")).get("immediate"))
        except Exception:
            return False

    jobs.sort(key=lambda p: (0 if _is_now(p) else 1, p.name))
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
    # 离屏注入：先把窗口挪到屏幕外，再让它在前台干活 —— 屏幕上什么都看不到。
    global _OFFSCREEN_ACTIVE
    hwnd_wx = 0
    saved_pl = None
    if OFFSCREEN:
        _info = wechat_main_window()
        hwnd_wx = _info[0] if _info else 0
        if hwnd_wx:
            saved_pl = _go_offscreen(hwnd_wx)
            _OFFSCREEN_ACTIVE = bool(saved_pl)
            if not saved_pl:
                log("  [离屏] 挪窗口失败，本次按普通模式执行")
    if saved_pl:
        # 已在屏幕外且可见：**不能**再调 ensure_window_usable（它会 SW_RESTORE，把窗口拉回屏幕）。
        state = "offscreen"
        log(f"  [离屏] 微信窗口 hwnd={hwnd_wx} 已在屏幕外 x={OFFSCREEN_X}"
            f"（原位 {saved_pl['normal']}，showCmd={saved_pl['showCmd']}）")
    else:
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
    # 收尾还原：窗口位置（离屏）→ 上游库最小化的遮挡窗口 → 取消置顶 → 光标 → 原前台窗口
    try:
        if saved_pl and hwnd_wx and u32.IsWindow(wintypes.HWND(hwnd_wx)):
            _restore_placement(hwnd_wx, saved_pl)
            log(f"  [离屏] 微信窗口已搬回原位 {saved_pl['normal']}")
        _OFFSCREEN_ACTIVE = False
    except Exception as e:
        log(f"  窗口位置还原异常: {e}")
    try:
        res["blockers_restored"] = restore_library_minimized()
    except Exception as e:
        log(f"  还原遮挡窗口异常: {e}")
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
    install_library_patches()        # 给上游库的“最小化遮挡窗口”行为打补丁
    recover_blockers()               # 兜底：上次进程被杀遗留的被最小化窗口
    _recover_window_state()          # 兜底：上次离屏后被杀，把微信窗口搬回原位
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
        need = IDLE_GATE if job.get("action") in ("draft", "send") else IDLE_GATE_RO
        if job.get("immediate"):
            need = 0.0               # 使用者显式要求立即执行（wx ... --now）
        elif need > 0 and IDLE_FALLBACK > 0:
            _waited = _job_waited(job)
            if _waited > IDLE_FALLBACK:
                log(f"[{job.get('id')}] 已等 {_waited:.0f}s 仍未见 {need:.0f}s 长空闲"
                    f"（你可能一直在用电脑），降级为「≥{IDLE_FALLBACK_MIN:.0f}s 安静」执行")
                need = IDLE_FALLBACK_MIN
        if not wait_idle(need, IDLE_MAX_WAIT):
            log(f"[{job.get('id')}] 你一直在操作电脑，任务放回队列等下一轮")
            working.rename(QUEUE / working.name.replace(".working", ".job"))
            time.sleep(2.0)
            continue
        log(f"[{job.get('id')}] 领到任务 {job.get('action')} chat={job.get('chat')!r}，"
            f"空闲 {idle_seconds():.1f}s，开始执行")
        _t = time.time()
        t0 = _t
        try:
            res = run_job(job)
        except Exception:
            # 单个任务抛任何异常都不允许把常驻服务带走（2026-09-21 实际踩到：
            # run_job 里一个 NameError 让服务反复崩溃、任务变遗属文件、客户端只看到「已排队」）
            res = {"ok": False, "error": "任务异常: " + traceback.format_exc(limit=4)}
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
