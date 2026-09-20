#!/usr/bin/env python3
"""Repository self-check: syntax, line endings, encodings, and hygiene scans.

Runs in CI (.github/workflows/selfcheck.yml) and locally:

    python scripts/selfcheck.py

Add your own private tokens **without committing them** by passing them in:

    EXTRA_DENY='张三|李四|my-real-name' python scripts/selfcheck.py

The generic scan catches the usual leaks (absolute user paths, tokens, WeChat
internal ids). It deliberately contains *no* personal names, so the checker
itself never becomes the leak.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL_FILES = ["scripts/wx.sh"]
PY_FILES = ["scripts/wx_service.py", "scripts/wx_task.py", "scripts/selfcheck.py"]
LF_ONLY = SHELL_FILES + PY_FILES
ASCII_ONLY = ["scripts/wx-ocr.ps1", "scripts/wx.cmd", "install.ps1"]
TEXT_GLOBS = ("*.md", "*.sh", "*.py", "*.ps1", "*.cmd", "*.yml", "*.yaml", "*.json")

# 扫自己会把模式定义本身当成命中，所以要跳过
SCAN_SKIP = {"scripts/selfcheck.py"}

DENY = [
    (r"[A-Za-z]:\\+[Uu]sers\\+[^\\\s\"'`)]+", "absolute Windows user path"),
    (r"/home/[A-Za-z0-9._-]+/", "absolute POSIX home path"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth token"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub personal access token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained token"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"wxid_[a-z0-9]{6,}", "WeChat internal wxid"),
    (r"[0-9]{6,}@chatroom", "WeChat group id"),
]

problems: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL {msg}")
    problems.append(msg)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def check_syntax() -> None:
    print("== syntax ==")
    for f in SHELL_FILES:
        p = ROOT / f
        if subprocess.run(["bash", "-n", str(p)]).returncode == 0:
            ok(f"bash -n {f}")
        else:
            bad(f"bash -n {f}")
    for f in PY_FILES:
        try:
            ast.parse((ROOT / f).read_text(encoding="utf-8"))
            ok(f"ast {f}")
        except SyntaxError as e:
            bad(f"ast {f}: {e}")


def check_line_endings() -> None:
    print("== line endings ==")
    for f in LF_ONLY:
        data = (ROOT / f).read_bytes()
        if b"\r" in data:
            bad(f"{f} contains CR (must be LF-only)")
        else:
            ok(f"{f} is LF-only")


def check_ascii() -> None:
    print("== ASCII-only (PowerShell / batch) ==")
    for f in ASCII_ONLY:
        p = ROOT / f
        data = p.read_bytes()
        if all(b < 0x80 for b in data):
            ok(f"{f} is pure ASCII (no BOM)")
        else:
            offenders = sorted({b for b in data if b >= 0x80})[:6]
            bad(f"{f} has non-ASCII bytes {offenders} (a BOM-less .ps1 is read as ANSI elsewhere)")


def iter_text_files():
    seen = set()
    for pattern in TEXT_GLOBS:
        for p in sorted(ROOT.rglob(pattern)):
            if ".git" in p.parts or p in seen:
                continue
            seen.add(p)
            yield p


def check_hygiene() -> None:
    print("== hygiene scan ==")
    patterns = list(DENY)
    extra = os.environ.get("EXTRA_DENY", "").strip()
    if extra:
        patterns.append((extra, "EXTRA_DENY"))
    hits = 0
    scanned = 0
    for p in iter_text_files():
        r = rel(p)
        if r in SCAN_SKIP:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, label in patterns:
                if re.search(pat, line):
                    bad(f"{r}:{lineno} [{label}] {line.strip()[:90]}")
                    hits += 1
    if hits == 0:
        ok(f"no hits for {len(patterns)} patterns across {scanned} files")


def main() -> int:
    print(f"self-check root: {ROOT}")
    check_syntax()
    check_line_endings()
    check_ascii()
    check_hygiene()
    print()
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
