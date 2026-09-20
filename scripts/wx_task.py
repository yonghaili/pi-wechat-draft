# -*- coding: utf-8 -*-
"""微信 4.x 会话操作封装：只填不发 / 发送 / 清空输入框。

依赖 wechatauto-replica（装在 ~/.pi/wechat-ui/venv）。
用法：
    python wx_task.py fill  <会话名> <文字>   # 只填入输入框，绝不发送
    python wx_task.py send  <会话名> <文字>   # 填入并发送（带数据库回读确认）
    python wx_task.py clear <会话名>          # 清空该会话输入框里的内容
    python wx_task.py probe <会话名>          # 只打开会话并报告输入框位置
"""
from __future__ import annotations

import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from wechatauto import WeChatGUI, WxResponse  # noqa: E402


def emit(*a):
    print(*a, flush=True)


def main() -> int:
    if len(sys.argv) < 3:
        emit(__doc__)
        return 2
    action, target = sys.argv[1], sys.argv[2]
    text = sys.argv[3] if len(sys.argv) > 3 else ""

    gui = WeChatGUI()
    emit("ensure_visible:", gui.ensure_visible())
    opened = gui.open_chat(target)
    cur = getattr(gui, "_current_chat", None)
    emit(f"open_chat: {opened}  current_chat={cur!r}")
    if not opened:
        emit("ABORT: 会话未打开")
        return 3

    box = gui.get_input_box()
    emit("input_box(rel):", box)
    if not box:
        emit("ABORT: 未探测到输入框")
        return 4

    if action == "probe":
        return 0

    if action == "clear":
        gui.focus_input(box)
        gui._input.key(0x41, ctrl=True)   # VK_A
        gui._input.key(0x2E)              # VK_DELETE
        time.sleep(0.4)
        has = gui._input_box_has_text(box)
        emit("clear 完成，输入框仍有内容:", has)
        return 0 if not has else 5

    if action == "fill":
        ok = gui.input_text(text, box=box, fast=True)
        emit(f"fill: {ok}  text={text!r}")
        emit("状态: 已填入，未发送")
        return 0 if ok else 6

    if action == "send":
        # 先填（fast 路径不含按回车的拼音兜底），确认填入成功再单独触发发送
        if not gui.input_text(text, box=box, fast=True):
            emit("ABORT: 填入失败，未发送")
            return 6
        time.sleep(0.5)
        sent = gui.click_send()
        emit("click_send:", sent)
        if not sent:
            emit("ABORT: 发送未确认")
            return 7
        resp: WxResponse = gui.send_msg  # 仅为类型提示占位
        for _ in range(10):
            if gui._verify_sent(text, target):
                emit(f"DB 回读确认成功: 会话={target} 内容={text!r}")
                return 0
            time.sleep(1.0)
        emit("WARN: 已触发发送，但数据库未回读确认")
        return 8

    emit(f"未知动作 {action}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
