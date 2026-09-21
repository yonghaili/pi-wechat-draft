#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发送前的判断层（可选）：用 TypeSafe Jev 给「这条回复该不该发」做一次结构化判断。

为什么需要它
------------
本工具唯一的不可逆动作是 `wx send`。已有的机械闸门只能防「发错人」「发重复」，
防不了这些语义问题：

  * 这条回复对**这个**会话来说是不是自然、贴题（串台 / 答非所问）；
  * 当前这场对话离翻脸有多近；
  * 对方此刻真正需要的是道歉、行动、解释，还是被看见；
  * 这条回复是不是实际上没满足对方的需要（空泛客套）；
  * 文本里有没有金额 / 凭证 / 承诺 / 个人资料这类「发错就麻烦」的内容。

Jev 是**判断模型**（不生成文本）：一次请求同时回答若干是非题 / 选择题 / 打分题，
约 1 秒返回。生成文本由人（或生成模型）负责，判断交给 Jev——这正是把这个工具从
「照抄转录」升级为「有判断的助手」的那一层。

用法
----
    # 判断一条拟发出的回复（默认读该会话最近 8 条原文作上下文）
    python jev_advise.py --chat "徐恒" --text "再约，哈哈，来日方长"

    # 顺便给 3 条候选排序（候选由人或生成模型给出，Jev 只负责排序）
    python jev_advise.py --chat "徐恒" --cand "A" --cand "B" --cand "C"

    # 没有上下文也能用（只判断文本本身，证据弱一档）
    python jev_advise.py --chat "徐恒" --no-context --text "..."

    # 机器可读
    python jev_advise.py --chat "徐恒" --text "..." --json

退出码
------
    0  判断层认为可以发（或没发现需要提醒的点）
    3  建议先别发：危险等级高 / 含敏感内容 / 与上下文不贴 / 没满足对方需要
    4  判断层不可用（无 key、网络失败、接口变更）——**不要**当作「通过」，按人工判断走
    2  用法错误

设计取舍（借鉴 github.com/Finderchangchang/jev-chat-JARVIS 的实践，按本工具调整）
------------------------------------------------------------------------------
* instructions / criteria 一律用英文（Jev 主训练语言是英文），聊天内容保留中文；
* 一次请求发全部问题（实测 3 题约 0.6-0.9s），不要一题一请求；
* state 只放「关系描述 + 最近 N 条」，不塞大段历史；
* 判断结果只作**提醒**，不替代使用者决定；真正硬拦的是确定性的敏感词闸门
  （见 wx_service.py 的 HIGH_RISK_PATTERNS），因为那一条不该依赖网络与模型。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT = 20.0
MAX_RETRIES = 2
DEFAULT_KEY_FILE = "~/.pi/.secrets/typesafe.key"
DEFAULT_CONTEXT_N = 8

# 风险等级图例（0-9，**这条回复发出去可能带来的麻烦**）。
# 为什么不直接照搬 JARVIS 那个“关系危险度”rubric：那个 10 档写的是亲密关系里的
# 道歉-试探-通牒（“下次记得”“你最好”“分手”），用到工作/朋友/家人这种日常聊天上
# 会把一句“再约”也判成 4/9（2026-09-21 实测）。本工具的口径改成“后果”：
# 发出去会不会失礼、误解、承诺过度、伤到对方、泄露信息或造成损失。
RISK_LEGEND = [
    "纯寒暄或玩笑，发出去没有任何代价。",
    "日常信息或确认事项，最坏也只是显得平淡。",
    "表述略笨拙，对方可能轻微困惑，但不会介意。",
    "可能显得敷衍或答非所问，对方得再问一次。",
    "语气可能被误解（玩笑被当真、简短被读成冷淡）。",
    "有明显失礼、越界或承诺过度的风险，改一改再发更稳。",
    "很可能让对方不快或制造麻烦（措辞错、暗示失约、带入他人信息）。",
    "会伤到对方或造成实质损失（错的承诺、金额、隐私）。",
    "几乎肯定引发争执或后果，且不好收场。",
    "可能造成不可逆损失（转账、凭证泄露、关系破裂、合规风险）。",
]


class JevUnavailable(Exception):
    """判断层不可用（与「判断结论」区分开）。"""


def _key_file() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(
        os.environ.get("TYPESAFE_KEY_FILE") or DEFAULT_KEY_FILE))


def api_key() -> str:
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not key:
        f = _key_file()
        if f.exists():
            key = f.read_text(encoding="utf-8").strip()
    if not key:
        raise JevUnavailable(
            f"找不到 TypeSafe key：环境变量 TYPESAFE_API_KEY 与 {_key_file()} 都没有")
    return key


def redact(text: str) -> str:
    try:
        k = api_key()
    except Exception:
        k = ""
    text = str(text)
    return text.replace(k, "[REDACTED]") if k else text


def ask(state, questions: dict, timeout: float = TIMEOUT) -> dict:
    """POST state+questions 给 Jev，返回 {"answers": {...}, "usage": {...}}。"""
    key = api_key()
    payload = json.dumps({"model": MODEL, "state": state, "questions": questions},
                         ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            API_URL, data=payload, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = redact(e.read().decode("utf-8", "replace"))[:500]
            except Exception:
                pass
            last = f"HTTP {e.code}: {body}"
            if e.code in (408, 429, 500, 502, 503, 504, 529) and attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise JevUnavailable(f"Jev 拒绝请求（{last}）") from None
        except Exception as e:                       # 网络/超时/解析
            last = redact(str(e))
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise JevUnavailable(f"Jev 请求失败（{last}）") from None
    raise JevUnavailable(f"Jev 请求失败（{last}）")


# ------------------------------------------------------------------ 读上下文
def read_context(chat: str, n: int):
    """用只读读取器取最近 n 条原文，按时间正序返回 [{from,time,text}]；不可用返回 None。"""
    reader = (os.environ.get("WX_READER") or "").strip()
    if not reader or not pathlib.Path(reader).exists():
        return None
    py = os.environ.get("WX_SYS_PY", "python")
    try:
        out = subprocess.run([py, reader, "history", "--talker", chat,
                              "--limit", str(n), "--display-order", "desc"],
                             capture_output=True, timeout=60).stdout or b""
    except Exception:
        return None
    raw = out.decode("utf-8", "replace")
    i = raw.find("{")
    if i < 0:
        return None
    try:
        msgs = json.loads(raw[i:])["data"]["messages"]
    except Exception:
        return None
    rows = []
    for m in reversed(msgs):
        text = (m.get("text") or "").strip()
        if not text:
            kind = m.get("kind_name") or "非文本"
            text = f"[{kind}]"
        rows.append({"from": "me" if m.get("from_me") else "other",
                     "time": m.get("time", ""), "text": text})
    return rows[-n:]


def build_state(messages, relationship: str | None) -> dict:
    """Jev 的 state：关系描述（可选）+ 最近若干条 + 最后一条是谁说的。"""
    msgs = [{"from": m["from"], "text": m["text"]} for m in messages]
    return {"chat": {"relationship": relationship or "(未提供)",
                     "messages": msgs,
                     "latest_from": msgs[-1]["from"] if msgs else "other"}}


# ------------------------------------------------------------------ 题目集
def build_questions(text: str, candidates: list[str] | None, has_context: bool,
                    has_text: bool = True) -> dict:
    """一次请求里的全部题目。instructions/criteria 用英文，聊天内容保持中文。"""
    q: dict = {}

    q["fits_context"] = {
        "type": "noul",
        "instructions": (
            "Is the proposed reply a natural, on-topic next message in THIS conversation, "
            "and is it addressed to the right person? "
            "Judge against the thread, not the sentence alone. "
            "Answer false if the reply looks like it belongs to a different conversation, "
            "answers something nobody asked, or replies to a different kind of relationship."
        ),
        "criteria": {
            "true": ("It reads as a plausible next turn in this thread, to this person."),
            "false": ("Off-thread, mismatched tone or relationship, or answering the wrong thing."),
        },
    }
    q["risk_level"] = {
        "type": "score",
        "instructions": (
            "How much trouble could SENDING THIS PROPOSED REPLY cause? "
            "Score the consequence of this reply, not the mood of the thread: would it come across "
            "as rude, dismissive, confusing, over-promising, hurtful, or would it leak private details "
            "or commit to something costly? A warm or neutral reply to a friendly thread is 0-2 even if "
            "the thread is work pressure or deadlines. Judge only this reply."
        ),
        "criteria": RISK_LEGEND,
    }
    q["other_needs"] = {
        "type": "choice",
        "instructions": (
            "What does the other person actually need from me right now, judging the LATEST message "
            "first but using the whole thread? "
            "If they genuinely accepted (thanks / 收到 / 没事了 / 那就这样), choose nothing, even if "
            "earlier they wanted action or an apology. Sarcastic 'I'm used to it' or 'whatever' is "
            "NOT genuine acceptance."
        ),
        "criteria": {
            "apology": "A sincere apology for hurt or a mistake, not yet accepted.",
            "action": "A concrete action, time, deliverable, or recap of a named fact.",
            "explanation": "A clear explanation of what happened or why.",
            "care": "Proof I remember, listen, or care — a loyalty or attention test.",
            "nothing": "Nothing further: genuine acceptance, warm casual chat, or they said not to reply.",
        },
    }
    q["sensitive"] = {
        "type": "choice",
        "instructions": (
            "Does the PROPOSED text contain anything that must not go out by mistake? "
            "Only judge the proposed text. money = amounts, transfers, red packets, bank details. "
            "credentials = verification codes, passwords, keys, login secrets. "
            "commitment = a specific promise, deadline or arrangement. "
            "personal_data = ID numbers, addresses, phone numbers, third parties' private details."
        ),
        "criteria": {
            "none": "Nothing from those categories.",
            "money": "Amounts, transfer/payment wording, bank or account details.",
            "credentials": "Codes, passwords, keys, or other login secrets.",
            "commitment": "A specific promise, deadline, or arrangement.",
            "personal_data": "ID numbers, addresses, phone numbers, or others' private details.",
        },
    }
    q["reply_serves_need"] = {
        "type": "noul",
        "instructions": (
            "Setting aside politeness, does the proposed reply actually give what the other person "
            "needs (per other_needs)? Answer false for empty pleasantries that dodge the need, "
            "over-promising invented facts, or a reply that would reopen a closed topic."
        ),
        "criteria": {
            "true": ("It addresses the real need or is an appropriate holding line."),
            "false": ("Empty, dodging, over-promising, inventing facts, or reopening a settled matter."),
        },
    }

    if candidates:
        q["best_reply"] = {
            "type": "choice",
            "instructions": (
                "Which candidate is the most appropriate next message, given the conversation and "
                "what the other person actually needs? Penalize dismissive, over-promising, or "
                "off-topic candidates. Prefer the one matching the real need."
            ),
            "criteria": {f"reply_{chr(65 + i)}": c for i, c in enumerate(candidates)},
        }

    # 没有单一拟发文本时（只排序候选），不能问“这条文本怎么样”——否则 Jev 会去评价
    # 一个占位符字符串（2026-09-21 实测：fits_context 0.48 / reply_serves_need 0.47
    # 就是这么来的假信号）。
    if not has_text:
        for k in ("fits_context", "sensitive", "reply_serves_need"):
            q.pop(k, None)
    return q


# ------------------------------------------------------------------ 判读与输出
def summarise(answers: dict, text: str, candidates: list[str] | None,
              has_text: bool = True) -> tuple[list[str], int]:
    """把答案变成人读的要点 + 退出码建议。

    闸门阈值有意保守：只有**强信号**才建议别发（风险≥6、贴合<0.35、需要<0.35、
    敏感类别属金额/凭证/个人资料）。对 noul 题 Jev 会给一个概率，0.35-0.6 表示它自己
    也不确定——那种情况只标“不确定/证据弱”，不能当成告警，否则会“狼来了”。
    """
    lines: list[str] = []
    code = 0

    rl = answers.get("risk_level") or {}
    if rl.get("type") == "score":
        score = float(rl.get("score", 0))
        conf = float(rl.get("confidence", 0))
        level = RISK_LEGEND[int(round(score))] if 0 <= round(score) < len(RISK_LEGEND) else ""
        lines.append(f"后果风险   {score:.1f}/9（置信 {conf:.2f}）— {level}")
        if score >= 6:
            code = 3

    if has_text:
        fits = answers.get("fits_context") or {}
        if fits.get("type") == "noul":
            p = float(fits.get("noul", 0))
            if p < 0.35:
                lines.append(f"上下文贴合 {p:.2f} — ⚠ 与上下文不贴，可能是串台或答非所问")
                code = 3
            elif p < 0.6:
                lines.append(f"上下文贴合 {p:.2f} — 不确定（证据弱，你自己看一眼）")
            else:
                lines.append(f"上下文贴合 {p:.2f} — 像是这个会话的下一句")

    needs = answers.get("other_needs") or {}
    if needs.get("type") == "choice":
        probs = needs.get("probabilities") or {}
        top = ", ".join(f"{k} {v:.2f}" for k, v in
                        sorted(probs.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"对方需要   {needs.get('choice')}（{top}）")

    if has_text:
        sens = answers.get("sensitive") or {}
        if sens.get("type") == "choice":
            choice = sens.get("choice")
            if choice and choice != "none":
                lines.append(f"敏感内容   ⚠ {choice} — 发送前务必逐字核对")
                if choice in ("money", "credentials", "personal_data"):
                    code = 3
                elif choice == "commitment":
                    lines.append("           （承诺类只提醒不拦，可发但建议自己确认时间/事项）")
            else:
                lines.append("敏感内容   无")

        serves = answers.get("reply_serves_need") or {}
        if serves.get("type") == "noul":
            p = float(serves.get("noul", 0))
            if p < 0.35:
                lines.append(f"满足需要   ⚠ 否（空泛或没对上对方的真实需要）（{p:.2f}）")
                code = 3
            elif p < 0.6:
                lines.append(f"满足需要   不确定（{p:.2f}）")
            else:
                lines.append(f"满足需要   是（{p:.2f}）")

    if candidates:
        best = answers.get("best_reply") or {}
        if best.get("type") == "choice":
            probs = best.get("probabilities") or {}
            order = sorted(probs.items(), key=lambda kv: -kv[1])
            lines.append("候选排序   " + " > ".join(f"{k}({v:.2f})" for k, v in order))
            choice = str(best.get("choice") or "")
            idx = ord(choice[-1:].upper()) - 65 if choice[-1:].isalpha() else -1
            if 0 <= idx < len(candidates):
                lines.append(f"推荐       {choice}：{candidates[idx]}")
    return lines, code


def main() -> int:
    ap = argparse.ArgumentParser(description="发送前的 Jev 判断层")
    ap.add_argument("--chat", required=True, help="会话名（用于读上下文与显示）")
    ap.add_argument("--text", default="", help="拟发出的回复文本")
    ap.add_argument("--cand", action="append", default=[],
                    help="候选回复（给 3 条时 Jev 会排序），可重复")
    ap.add_argument("--n", type=int, default=DEFAULT_CONTEXT_N, help="读最近几条上下文")
    ap.add_argument("--relationship", default="", help="关系描述（可选，喂给 Jev 作条件）")
    ap.add_argument("--no-context", action="store_true", help="不读上下文，只判断文本本身")
    ap.add_argument("--json", action="store_true", help="输出 JSON（含原始答案）")
    ap.add_argument("--show-state", action="store_true",
                    help="打印送给 Jev 的 state（核对上下文顺序与内容，排障用）")
    args = ap.parse_args()

    if not args.text and not args.cand:
        print("需要 --text 或至少一条 --cand", file=sys.stderr)
        return 2
    candidates = [c for c in args.cand if c.strip()]
    if candidates and len(candidates) != 3:
        print("候选排序要求正好 3 条（Jev 的 choice 需要固定标签）", file=sys.stderr)
        return 2

    messages = [] if args.no_context else (read_context(args.chat, args.n) or [])
    has_context = bool(messages)
    has_text = bool(args.text.strip())
    state = build_state(messages, args.relationship)
    target = args.text or "(未给单一文本，仅排序候选)"
    questions = build_questions(target, candidates or None, has_context, has_text)

    if args.show_state:
        print(json.dumps(state, ensure_ascii=False, indent=1))

    try:
        t0 = time.time()
        res = ask(state, questions)
        elapsed = time.time() - t0
    except JevUnavailable as e:
        print(f"判断层不可用：{e}", file=sys.stderr)
        print("→ 不要把它当作「通过」；按人工判断决定发不发。", file=sys.stderr)
        return 4

    answers = res.get("answers") or {}
    usage = res.get("usage") or {}
    lines, code = summarise(answers, target, candidates or None, has_text)

    if args.json:
        print(json.dumps({"chat": args.chat, "text": target, "candidates": candidates,
                          "context_messages": len(messages), "model": res.get("model"),
                          "usage": usage, "verdict": "review" if code == 3 else "ok",
                          "lines": lines, "answers": answers},
                         ensure_ascii=False, indent=1))
        return code

    print(f"会话：{args.chat}   上下文：{len(messages)} 条"
          f"{'' if has_context else '（未读到上下文，证据弱一档）'}")
    if args.text:
        print(f"拟发文本：{args.text}")
    print("─" * 46)
    for ln in lines:
        print(ln)
    print("─" * 46)
    print(("⚠ 建议先别发：以上有需要你确认的点。" if code == 3 else "判断层没发现需要提醒的点。")
          + f"（{res.get('model')} {elapsed:.1f}s"
            f"{'，%s/%s tokens' % (usage.get('input_tokens'), usage.get('output_tokens')) if usage else ''}）")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
