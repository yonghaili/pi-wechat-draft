#!/usr/bin/env bash
# 微信固定操作入口 —— 默认只把草稿填进发送框，绝不自动发送。
#
#   wx draft "<会话名>" "<文字>"    默认动作：交给后台服务填进输入框（不发送），再回报待确认卡片
#   wx send  "<会话名>" "<文字>" --confirm
#                                  仅当使用者核对过并明确说「发」时才用；缺 --confirm 直接拒发
#   wx check "<会话名>"             开会话 + 读屏，回报表头、输入框、最近原文
#   wx clear "<会话名>"             清空该会话输入框（撤掉草稿）
#   wx context "<会话名>" [N]       只看最近 N 条原文（默认 5），不碰界面
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
  local action="$1" chat="$2" text="${3:-}" confirm="${4:-0}"
  svc_start || return 1
  mkdir -p "$QUEUE"
  local id seq
  id="$(date +%Y%m%d%H%M%S)-$$-${RANDOM}"
  seq="$(date +%s%N 2>/dev/null || date +%s)000"
  "$SYS_PY" -c '
import json,sys
job=dict(zip(("id","action","chat","text","confirm","created"),
             (sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6]=="1",sys.argv[7])))
open(sys.argv[1],"w",encoding="utf-8").write(json.dumps(job,ensure_ascii=False))
' "$QUEUE/$seq.$id.job" "$id" "$action" "$chat" "$text" "$confirm" "$(date +%Y-%m-%dT%H:%M:%S)"
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

cmd="${1:-}"
case "$cmd" in
  draft|fill)
    chat="${2:-}"; text="${3:-}"
    [ -n "$chat" ] && [ -n "$text" ] || die "用法：wx draft \"<会话名>\" \"<文字>\""
    echo "已提交后台服务（只填不发）…"
    submit draft "$chat" "$text" 0
    exit $?
    ;;

  send)
    chat="${2:-}"; text="${3:-}"; confirm="${4:-}"
    [ -n "$chat" ] && [ -n "$text" ] || die "用法：wx send \"<会话名>\" \"<文字>\" --confirm"
    if [ "$confirm" != "--confirm" ] && [ "${WX_SEND_CONFIRMED:-}" != "1" ]; then
      echo "已阻止发送：send 需要显式确认。"
      echo "  默认请用：wx draft \"$chat\" \"$text\""
      echo "  使用者核对过那条草稿后，再跑：wx send \"$chat\" \"$text\" --confirm"
      exit 9
    fi
    echo "已提交后台服务（会真的发出）…"
    submit send "$chat" "$text" 1
    exit $?
    ;;

  check)
    chat="${2:-}"
    [ -n "$chat" ] || die "用法：wx check \"<会话名>\""
    submit check "$chat" "" 0
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
    die "未知动作 $cmd（可用：draft / send / check / clear / context / resolve / result / svc）"
    ;;
esac
