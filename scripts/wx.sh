#!/usr/bin/env bash
# 微信固定操作入口 —— 默认只把草稿填进发送框，绝不自动发送。
#
#   wx draft "<会话名>" "<文字>"    默认动作：交给后台服务填进输入框（不发送），再回报待确认卡片
#   wx send  "<会话名>" "<文字>" --confirm [--force]
#                                  仅当使用者核对过并明确说「发」时才用；缺 --confirm 直接拒发；
#                                  目标会话最近一条已是同内容时会被拦下（疑似重复，多为使用者已手动发过），--force 才强行重发
#   wx check "<会话名>" [--force]  开会话 + 读屏，回报表头、输入框、最近原文；别的会话有未发送草稿时会被拦下
#   wx clear "<会话名>"             清空该会话输入框（撤掉草稿）
#   wx context "<会话名>" [N]       只看最近 N 条原文（默认 5），不碰界面
#   wx peek "<会话名>" [N]          只读数据库回答“最近说了什么、要不要回”——**零前台**，不开微信窗口
#   wx doctor                      自检：服务状态、生效配置、两条链路的边界、微信窗口现状、占用基线
#   wx resolve "<关键词>"           把口语叫法（如「张工」）解析成候选会话名（需配置 WX_READER）
#   wx svc start|stop|status|log    后台服务管理
#
# 工作方式（2026-09-20 使用者要求）：
#   所有碰界面的动作都排进 ~/.pi/wechat-ui/queue/，由常驻服务 wx_service.py
#   串行执行：动手前先等使用者空闲，执行时热复用 UIA 引擎（不冷启动），
#   执行后还原前台窗口与光标位置，且从不最小化其它窗口。默认只填不发。
set -uo pipefail
export PYTHONIOENCODING=utf-8

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WX_DIR="${WX_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"   # repo 根：venv / queue / logs 都在这里
PY="${WX_PY:-$WX_DIR/venv/Scripts/python.exe}"
PYW="${WX_PYW:-$(dirname -- "$PY")/pythonw.exe}"
SERVICE="$SCRIPT_DIR/wx_service.py"
QUEUE="$WX_DIR/queue"
LOGS="$WX_DIR/logs"
PIDFILE="$WX_DIR/wx-service.pid"
# 可选的只读复核后端（自家微信读取器）：不配置也能用，只是少一层“另链路回读原文”的证据
READER="${WX_READER:-}"
SYS_PY="${PYTHON_BIN:-python}"
TIMEOUT="${WX_TIMEOUT:-${WX_WAIT:-45}}"

die() { echo "错误：$*" >&2; exit 1; }

[ -x "$PY" ] || die "找不到微信 venv 的 python: $PY"
[ -f "$SERVICE" ] || die "找不到服务脚本: $SERVICE"
if [ -n "$READER" ] && [ ! -f "$READER" ]; then
  echo "警告：WX_READER 指向的文件不存在（$READER），将跳过原文回读与名字解析。" >&2
  READER=""
fi

# ------------------------------------------------------------------ 服务管理
svc_pid() { [ -f "$PIDFILE" ] && cat "$PIDFILE" 2>/dev/null; }

svc_running() {
  local p; p="$(svc_pid)"
  [ -n "${p:-}" ] || return 1
  powershell -NoProfile -Command "if (Get-Process -Id $p -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >/dev/null 2>&1
}

# 按命令行抹掉本仓库的 wx_service.py 残留进程（PID 文件缺失/过期时的兜底）
# 防御：模式为空或不是 wx_service.py 时直接跳过——否则 '**' 会匹配所有进程
svc_sweep() {
  local win_svc
  win_svc="$(cygpath -w "$SERVICE" 2>/dev/null || echo "$SERVICE")"
  case "$win_svc" in
    *wx_service.py) ;;
    *) echo "svc_sweep: 路径异常，跳过清理（$win_svc）" >&2; return 0 ;;
  esac
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*$win_svc*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
}

svc_start() {
  if svc_running; then return 0; fi
  svc_sweep            # 没记录但还有残留进程时先清干净，避免两个服务同时驱动微信
  [ -x "$PYW" ] || PYW="$PY"
  local win_py win_svc
  win_py="$(cygpath -w "$PYW" 2>/dev/null || echo "$PYW")"
  win_svc="$(cygpath -w "$SERVICE" 2>/dev/null || echo "$SERVICE")"
  powershell -NoProfile -Command "Start-Process -FilePath '$win_py' -ArgumentList '$win_svc' -WindowStyle Hidden" >/dev/null 2>&1
  for _ in $(seq 1 20); do
    sleep 0.5
    svc_running && { echo "后台服务已启动（pid $(svc_pid)）"; return 0; }
  done
  echo "警告：服务启动未确认，请查 $LOGS/wx-service-*.log" >&2
  return 1
}

svc_stop() {
  local p; p="$(svc_pid)"
  local killed=0
  if [ -n "${p:-}" ] && powershell -NoProfile -Command "if (Get-Process -Id $p -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >/dev/null 2>&1; then
    powershell -NoProfile -Command "Stop-Process -Id $p -Force -ErrorAction SilentlyContinue" >/dev/null 2>&1
    killed=1
  fi
  svc_sweep
  rm -f "$PIDFILE"
  if [ "$killed" = 1 ]; then echo "已停止服务 pid=$p"; else echo "服务未在运行（已清理可能残留的实例与 pid 文件）"; fi
}

svc_status() {
  echo "队列目录：$QUEUE"
  if svc_running; then echo "状态：运行中 pid=$(svc_pid)"; else echo "状态：未运行"; fi
  if [ -f "$WX_DIR/wx-service.state.json" ]; then
    "$SYS_PY" -c '
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
last=d.get("last") or {}
print("已处理：%s 次；心跳：%s" % (d.get("processed"), d.get("heartbeat")))
print("最近一次：%s %s → ok=%s %s" % (last.get("action"), last.get("chat"), last.get("ok"), last.get("error") or ""))
' "$WX_DIR/wx-service.state.json"
  fi
  echo "待执行任务：$(ls "$QUEUE"/*.job 2>/dev/null | wc -l) 个；执行中：$(ls "$QUEUE"/*.working 2>/dev/null | wc -l) 个"
}

# ------------------------------------------------------------------ 提交任务
submit() {
  local action="$1" chat="$2" text="${3:-}" confirm="${4:-0}" force="${5:-0}" immediate="${6:-0}" allowhr="${7:-0}"
  svc_start || return 1
  mkdir -p "$QUEUE"
  local id seq
  id="$(date +%Y%m%d%H%M%S)-$$-${RANDOM}"
  seq="$(date +%s%N 2>/dev/null || date +%s)000"
  "$SYS_PY" -c '
import json,sys
job=dict(zip(("id","action","chat","text","confirm","created"),
             (sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6]=="1",sys.argv[7])))
job["force"] = (len(sys.argv) > 8 and sys.argv[8] == "1")
job["immediate"] = (len(sys.argv) > 9 and sys.argv[9] == "1")
job["allow_high_risk"] = (len(sys.argv) > 10 and sys.argv[10] == "1")
open(sys.argv[1],"w",encoding="utf-8").write(json.dumps(job,ensure_ascii=False))
' "$QUEUE/$seq.$id.job" "$id" "$action" "$chat" "$text" "$confirm" "$(date +%Y-%m-%dT%H:%M:%S)" "$force" "$immediate" "$allowhr"
  local start; start=$(date +%s)
  echo "任务号：$id"
  while [ ! -f "$QUEUE/$id.result" ]; do
    if [ $(( $(date +%s) - start )) -gt "$TIMEOUT" ]; then
      echo
      echo "已排队，尚未执行：任务号 $id"
      echo "原因：现在不是空闲时机（后台服务只在你停下手头操作时动手，以免抢你的焦点）。"
      echo "它在后台继续等，你空闲时会自己完成。查进度：wx result $id   查服务：wx svc status"
      return 0
    fi
    sleep 1
  done
  "$SYS_PY" -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
ev=d.get("evidence") or {}
print("结果：%s" % ("成功" if d.get("ok") else "失败"))
print("动作：%s  会话：%s  用时：%ss  窗口：%s" % (d.get("action"), d.get("chat"), d.get("seconds"), d.get("window")))
if d.get("foreground_seconds") is not None:
    print("占用前台：%ss（干完已把焦点还回你原来的窗口）" % d["foreground_seconds"])
if d.get("error"): print("说明：%s" % d["error"])
if d.get("hint"): print("提示：%s" % d["hint"])
if d.get("method"): print("写入方式：%s" % d["method"])
if d.get("readback") is not None: print("控件回读：%r" % d["readback"])
if d.get("high_risk"):
    print("⚠ 提醒：这段草稿含 %s——发给别人前请逐字核对（这层只在 wx send 上硬拦）" % "、".join(d["high_risk"]))
if d.get("input_row") is not None: print("输入行：%s" % (d["input_row"] or "（空）"))
if d.get("filled_flag") is not None: print("库返回 filled：%s" % d["filled_flag"])
if ev.get("opened") is not None: print("会话已开：%s" % ev["opened"])
if ev.get("input_row") is not None: print("输入行：%s%s" % (ev["input_row"], ("（判据：%s）" % ev["input_row_via"]) if ev.get("input_row_via") else ""))
if ev.get("header"): print("表头：%s" % " / ".join(ev["header"][:4]))
if ev.get("bottom"): print("底部：%s" % " / ".join(ev["bottom"][:6]))
if ev.get("context"):
    print("最近原文：")
    for m in ev["context"]:
        who = "我" if m.get("from_me") else (m.get("sender") or "他")
        print("  %s｜%s：%s" % (m.get("time"), who, m.get("text")))
if ev.get("text") is not None and d.get("action") == "draft":
    print()
    print("════════ 待你确认（已填入，未发送）════════")
    print("会话：%s" % d.get("chat"))
    print("草稿：%s" % ev.get("text"))
    print("字数：%s" % ev.get("chars"))
    if d.get("readback") is not None:
        print("控件逐字回读：%s" % ("一致" if d["readback"] == ev.get("text") else "不一致！%r" % d["readback"]))
    if ev.get("ctrl_chat_name"):
        print("控件所属会话名：%s" % ev["ctrl_chat_name"])
    print("下一步：切到微信核对无误后按回车发送；或回一句「发」，我来发送。")
    print("        要撤掉草稿就说「清空」，我跑 wx clear \"%s\"。" % d.get("chat"))
    print("════════════════════════════════")
' "$QUEUE/$id.result"
  local rc=1
  "$SYS_PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1],encoding="utf-8")).get("ok") else 1)' "$QUEUE/$id.result" && rc=0
  return $rc
}

# 打印一个任务的结果（不等待），供“已排队”后回查
show_result() {
  local id="$1"
  [ -n "$id" ] || die "用法：wx result <任务号>"
  local f="$QUEUE/$id.result"
  if [ ! -f "$f" ]; then
    if ls "$QUEUE"/*."$id".job >/dev/null 2>&1; then
      echo "任务 $id 仍在排队（等你空闲）。"
    else
      echo "找不到任务 $id 的结果（已完成且被清理，或用错了任务号）。"
    fi
    return 0
  fi
  "$SYS_PY" -c '
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
ev=d.get("evidence") or {}
print("任务：%s  动作：%s  会话：%s" % (d.get("id"), d.get("action"), d.get("chat")))
print("结果：%s（用时 %ss，窗口 %s）" % ("成功" if d.get("ok") else "失败", d.get("seconds"), d.get("window")))
if d.get("error"): print("说明：%s" % d["error"])
if d.get("input_row") is not None: print("输入行：%s" % (d["input_row"] or "（空）"))
if ev.get("text") is not None and d.get("action") == "draft":
    print("草稿（%s 字）：%s" % (ev.get("chars"), ev.get("text")))
' "$f"
}

# ---- 不碰界面的只读命令：本地直接跑 ----
read_context() {
  local chat="$1" n="${2:-5}"
  if [ -z "$READER" ]; then
    echo "  （未配置 WX_READER，跳过原文核对）"
    return 0
  fi
  "$SYS_PY" "$READER" history --talker "$chat" --limit "$n" --display-order desc 2>/dev/null \
    | "$SYS_PY" -c '
import sys, json
raw = sys.stdin.read()
i = raw.find("{")
if i < 0:
    print("  (读不到原文，请手工核对)"); raise SystemExit
msgs = list(reversed(json.loads(raw[i:])["data"]["messages"]))
for m in msgs:
    who = m.get("sender") or ("我" if m.get("from_me") else "?")
    txt = (m.get("text") or "").strip().replace("\n", " / ") or ("[" + str(m.get("kind_name") or "非文本") + "]")
    print("  %s｜%s｜%s：%s" % (m.get("time", ""), "我" if m.get("from_me") else "他", who, txt[:90]))
'
}

# 只读一条会话的最近 N 条 + 判断“要不要回”——全程只读数据库，不碰微信窗口（零前台）。
# 与 wx check 的区别：check 要开窗口读输入框（占前台几秒），peek 永远不碰界面。
peek() {
  local chat="$1" n="${2:-3}"
  [ -n "$chat" ] || die "用法：wx peek \"<会话名>\" [条数]"
  [ -n "$READER" ] || die "wx peek 需要只读读取器：请设置 WX_READER（或改用 wx context）"
  "$SYS_PY" "$READER" history --talker "$chat" --limit "$n" --display-order desc 2>/dev/null \
    | "$SYS_PY" -c '
import sys, json
raw = sys.stdin.read()
i = raw.find("{")
if i < 0:
    print("  （读不到，请检查读取器与密钥）"); raise SystemExit(2)
msgs = list(reversed(json.loads(raw[i:])["data"].get("messages") or []))
if not msgs:
    print("  最近没有消息"); raise SystemExit(0)
for m in msgs:
    who = m.get("sender") or ("我" if m.get("from_me") else "?")
    txt = (m.get("text") or "").strip().replace("\n", " / ") or ("[" + str(m.get("kind_name") or "非文本") + "]")
    print("  %s｜%s｜%s：%s" % (m.get("time", ""), "我" if m.get("from_me") else "他", who, txt[:90]))
last = msgs[-1]
print("  —— 最后一条是%s（%s）" % ("我发的" if last.get("from_me") else "对方发的", last.get("time", "")))
print("  —— %s" % ("不需要回" if last.get("from_me") else "可能是待回复；要起草就 wx draft"))
print("  （零前台：只读数据库，全程没碰微信窗口）")
'
}

# 自检：把“为什么读能静默、写不能”和当前策略一次说清，并看现场。
doctor() {
  echo "════ 微信工具自检 · $(date '+%F %T') ════"
  if svc_running; then echo "服务：运行中（pid $(svc_pid)）"; else echo "服务：**未运行**（wx svc start）"; fi
  [ -f "$WX_DIR/wx-service.state.json" ] && echo "  队列状态：$(head -c 240 "$WX_DIR/wx-service.state.json")"
  echo
  echo "生效配置（$WX_DIR/wx-service.env）："
  if [ -f "$WX_DIR/wx-service.env" ]; then
    grep -vE '^[[:space:]]*(#|$)' "$WX_DIR/wx-service.env" | sed 's/^/  /'
  else
    echo "  （无，全用默认值）"
  fi
  echo
  envval() {   # 先读 wx-service.env，再回退 shell 环境/默认值——否则 doctor 会报错值
    local k="$1" d="${2:-}" v=""
    if [ -f "$WX_DIR/wx-service.env" ]; then
      v="$(grep -E "^[[:space:]]*$k=" "$WX_DIR/wx-service.env" | tail -1 | cut -d= -f2- | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    fi
    [ -n "$v" ] || v="${!k:-$d}"
    printf '%s' "$v"
  }
  echo "两条链路的边界（为什么“读”能静默、“写”不能）："
  echo "  读（wx peek / context / resolve）＝解密本地 SQLCipher 库，纯文件操作，**永不碰界面、零前台**"
  echo "  写（wx draft / send / clear / check）＝微信没有本地写接口，只能 GUI 注入；注入要求目标在前台，"
  echo "    而且它最小化时 UIA 树是空壳，必须先恢复窗口 —— 这是物理约束，不是实现问题"
  echo "  打开会话：WX_OPEN_MODE=$(envval WX_OPEN_MODE auto)（auto：先试无点击 UIA 路径，失败才回退库的点击路径）"
  echo "  离屏注入：WX_OFFSCREEN=$(envval WX_OFFSCREEN 0)（1：任务期间把窗口挪到屏幕外；首尾各有约 0.1–0.25s 露面）"
  echo "  空闲闸门：WX_IDLE_GATE=$(envval WX_IDLE_GATE 2)s（写）/ WX_IDLE_GATE_RO=$(envval WX_IDLE_GATE_RO 0.5)s（只读）"
  echo
  if [ -n "$READER" ]; then
    [ -f "$READER" ] && echo "只读后端：✓ $READER" || echo "只读后端：✗ 文件不存在 $READER"
  else
    echo "只读后端：未配置 WX_READER"
  fi
  echo
  "$PY" - <<'PY'
import ctypes, glob, json, os, pathlib, subprocess
import ctypes.wintypes as wt
base = pathlib.Path(os.path.expanduser("~")) / ".pi" / "wechat-ui"
u = ctypes.windll.user32
u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
pids = set()
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                     capture_output=True, text=True, errors="replace").stdout or ""
for row in out.splitlines():
    parts = [p.strip('" ') for p in row.split('","')]
    if len(parts) > 1 and parts[1].isdigit():
        pids.add(int(parts[1]))
found = []
def cb(h, l):
    pid = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value in pids:
        c = ctypes.create_unicode_buffer(64); u.GetClassNameW(h, c, 64)
        if c.value.startswith("Qt") and "QWindowIcon" in c.value:
            found.append(int(h))
    return True
u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
if not found:
    print("微信窗口：未找到（微信没开？）")
for h in found:
    r = wt.RECT(); u.GetWindowRect(wt.HWND(h), ctypes.byref(r))
    print("微信窗口：hwnd=%d 矩形=(%d,%d)+%dx%d 可见=%s 最小化=%s%s" % (
        h, r.left, r.top, r.right - r.left, r.bottom - r.top,
        bool(u.IsWindowVisible(wt.HWND(h))), bool(u.IsIconic(wt.HWND(h))),
        "  ← 现在就在屏幕外" if r.right <= 0 else ""))
for name in ("window-state.json", "blockers.json"):
    p = base / name
    if p.exists():
        print("残留状态 %s：%s（窗口状态不对时 wx svc stop && wx svc start 会兜底恢复）" %
              (name, p.read_text(encoding="utf-8")[:110]))
secs = []
for f in sorted(glob.glob(str(base / "queue" / "*.result")))[-15:]:
    try:
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    except Exception:
        continue
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, (int, float)) and "front" in k.lower():
                secs.append(float(v))
if secs:
    print("最近 %d 个任务占用的前台时长：平均 %.1fs／最长 %.1fs" %
          (len(secs), sum(secs) / len(secs), max(secs)))
PY
  echo
  echo "日志：wx svc log 40"
}

cmd="${1:-}"
case "$cmd" in
  draft|fill)
    chat="${2:-}"; text="${3:-}"; now=0
    for a in "$@"; do [ "$a" = "--now" ] && now=1; done
    [ -n "$chat" ] && [ -n "$text" ] || die "用法：wx draft \"<会话名>\" \"<文字>\" [--now]（--now = 不等空闲，立即执行）"
    echo "已提交后台服务（只填不发）…"
    submit draft "$chat" "$text" 0 0 "$now"
    exit $?
    ;;

  send)
    chat="${2:-}"; text="${3:-}"; confirm="${4:-}"; force=0; now=0; allowhr=0
    for a in "$@"; do
      [ "$a" = "--force" ] && force=1
      [ "$a" = "--now" ] && now=1
      [ "$a" = "--allow-high-risk" ] && allowhr=1
    done
    [ -n "$chat" ] && [ -n "$text" ] || die "用法：wx send \"<会话名>\" \"<文字>\" --confirm [--force] [--now] [--allow-high-risk]"
    if [ "$confirm" != "--confirm" ] && [ "${WX_SEND_CONFIRMED:-}" != "1" ]; then
      echo "已阻止发送：send 需要显式确认。"
      echo "  默认请用：wx draft \"$chat\" \"$text\""
      echo "  使用者核对过那条草稿后，再跑：wx send \"$chat\" \"$text\" --confirm"
      exit 9
    fi
    echo "已提交后台服务（会真的发出；若最近一条已是同内容会被拦下，需 --force 才强行重发）…"
    submit send "$chat" "$text" 1 "$force" "$now" "$allowhr"
    exit $?
    ;;

  check)
    chat="${2:-}"; force=0
    [ "${3:-}" = "--force" ] && force=1
    [ -n "$chat" ] || die "用法：wx check \"<会话名>\" [--force]"
    submit check "$chat" "" 0 "$force"
    exit $?
    ;;

  clear)
    chat="${2:-}"
    [ -n "$chat" ] || die "用法：wx clear \"<会话名>\""
    submit clear "$chat" "" 0
    exit $?
    ;;

  context)
    chat="${2:-}"; n="${3:-5}"
    [ -n "$chat" ] || die "用法：wx context \"<会话名>\" [条数]"
    read_context "$chat" "$n"
    ;;

  peek)
    peek "${2:-}" "${3:-3}"
    ;;

  doctor)
    doctor
    ;;

  resolve)
    kw="${2:-}"
    [ -n "$kw" ] || die "用法：wx resolve \"<关键词>\""
    [ -n "$READER" ] || die "wx resolve 需要只读读取器：请设置 WX_READER 指向 rion_wechat_reader.py（不配置时请用完整会话名直接操作）"
    "$SYS_PY" "$READER" resolve-chat "$kw" 2>/dev/null | "$SYS_PY" -c '
import sys, json
raw = sys.stdin.read()
i = raw.find("{")
if i < 0:
    print("  (无法解析，请手工确认会话名)"); raise SystemExit
c = json.loads(raw[i:])["data"].get("candidates", [])
if not c:
    print("  没有匹配的会话"); raise SystemExit
if len(c) > 1:
    print("  有 %d 个候选——必须让使用者确认，不得自行挑选：" % len(c))
for x in c:
    print("  - %s  [%s]  备注=%s  昵称=%s" % (x.get("display_name"), x.get("chat_type"), x.get("remark") or "-", x.get("nick_name") or "-"))
'
    ;;

  result)
    show_result "${2:-}"
    ;;

  svc)
    case "${2:-status}" in
      start) svc_start ;;
      stop) svc_stop ;;
      status) svc_status ;;
      log) tail -n "${3:-40}" "$LOGS/wx-service-$(date +%Y%m%d).log" 2>/dev/null || echo "今天还没有日志" ;;
      *) die "用法：wx svc start|stop|status|log [行数]" ;;
    esac
    ;;

  ""|-h|--help|help)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    ;;

  *)
    die "未知动作 $cmd（可用：draft / send / check / clear / context / peek / resolve / result / doctor / svc）"
    ;;
esac
