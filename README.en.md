# pi-wechat-draft

**Fill a draft into the WeChat input box — and never press send for you.** A Windows-only desktop automation toolkit for WeChat 4.x, with a resident background service, character-exact readback of every write, and a set of rules that keep the tool from fighting the person using the machine.

> 中文文档：[README.md](README.md)

---

## Why this exists (and why it isn't just another wxauto script)

Writing a WeChat desktop automation script is easy. Making it not break things **on someone else's machine, while they are using it**, is the hard part. Most of this repository is the scar tissue from that:

- **Writes go through the UI Automation `ValuePattern`**, not "click the input box, then paste from the clipboard". The click-and-paste approach depends on coordinates, and on at least one machine it *reported success while the input box stayed empty* — because its internal input-box coordinates did not match the screen, and its own "is there text?" check sampled a region that overlapped the chat history.
- **Every write is read back and compared character by character.** The control also carries the conversation name (`chat_input_field`'s `Name`), so the target is verified *before* anything is typed.
- **No other window is ever minimized.** The upstream library's `ensure_visible()` minimizes every window that overlaps WeChat — i.e. it takes away the window you were working in. This tool only restores the WeChat window itself, with `ShowWindow`.
- **Foreground is taken with the Alt-key trick.** Four methods were measured on one machine: the upstream library's `bring_to_front(keep_topmost=True)` failed, `bring_to_front(keep_topmost=False)` never worked, `SwitchToThisWindow` failed, and **a synthetic `Alt` keypress followed by `SetForegroundWindow` succeeded 10/10**. Drawback-free: no always-on-top window left behind.
- **A resident service plus a file queue.** Exactly one actor ever drives WeChat, so two requests can never collide; the UIA engine stays warm (ready in 0.8s instead of a 3.8s cold start per call).
- **Draft-only by default.** `send` refuses to run without an explicit `--confirm`.

The full list of pitfalls — symptom, root cause, and how each one is verified — is in [SKILL.md](SKILL.md).

## Safety model

| Action | Default behaviour |
|---|---|
| `wx draft` | Fills the input box, **does not send**, returns a review card |
| `wx send ... --confirm` | Actually sends (without `--confirm` it exits with code 9) |
| `wx clear` | Empties the input box (withdraws the draft) |
| Anything else | No forwarding, no friend requests, no touching WeChat settings or tags |

Design premise: **the last key press before a message leaves should be a human's.** The agent fills; you glance and hit Enter. When you do want it sent for you, `send --confirm` does it and verifies through a second, independent path.

## Requirements

- Windows 10 / 11
- WeChat **4.x** desktop (`Weixin.exe`), logged in
- Python 3.10+
- Git Bash (the `wx` client shim is a shell script; `scripts\wx.cmd` works from cmd / PowerShell too)

## Install

```powershell
git clone https://github.com/yonghaili/pi-wechat-draft.git
cd pi-wechat-draft
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` creates a private venv inside the repo (nothing global) and prints a self-test.

Optional — give it a **second, independent read path** for chat history, so verification does not rely on UIA alone:

```powershell
setx WX_READER "C:\tools\reader\rion_wechat_reader.py"
```

Without it everything still works; you lose the cross-check and `wx resolve` (turning a spoken name into a candidate conversation).

## Usage

```
wx draft  "<chat>" "<text>"           # default: fill only, never send
wx send   "<chat>" "<text>" --confirm # really send (after you confirmed)
wx check  "<chat>"                    # open + read screen + read history
wx clear  "<chat>"                    # empty the input box
wx context "<chat>" [N]               # last N messages (read-only, no UI)
wx resolve "<keyword>"                # spoken name -> candidate chats (needs WX_READER)
wx result <job-id>                    # look up a queued job
wx svc start|stop|status|log          # the background service
```

A typical round trip:

```bash
$ wx draft "Example Contact" "I'll drop your parents off in the morning"
job: 20260920185527-486-17695
result: ok
action: draft  chat: Example Contact  took: 8.6s  window: ok
foreground: 8.2s (focus was returned to your previous window)
method: value_pattern
readback: "I'll drop your parents off in the morning"

════════ review before sending (filled, not sent) ════════
chat: Example Contact
chars: 9
readback matches: yes
control belongs to: Example Contact
next: switch to WeChat and press Enter yourself, or reply "send" and I will.
══════════════════════════════════════════════════════════
```

## How it works

```
your command / the agent
      │  writes a job file
      ▼
<repo>/queue/<seq>.<id>.job
      │  claimed serially (renamed to .working — one actor at a time)
      ▼
wx_service.py  (hidden, resident process)
      │  ① wait for a genuinely idle moment (configurable)
      │  ② restore the WeChat window (only WeChat — never other windows)
      │  ③ Alt-key trick -> foreground (the UIA tree only materializes then)
      │  ④ open chat -> UIA ValuePattern write -> character-exact readback
      │  ⑤ drop the temporary always-on-top flag, return focus and cursor
      ▼
<repo>/queue/<id>.result   ← the client reads it and prints the evidence
```

Deliberate choices:

- **The queue is files, not a socket**: auditable (`queue/*.result` keeps the evidence of every run), inspectable by hand, and nothing is lost if the service dies. On startup the service returns any orphaned `.working` job to the queue.
- **The client never blocks forever**: it waits 45s by default, then reports *“queued, job id …”* rather than failing — the job keeps waiting in the background for a suitable moment.
- **Idle gate, with a hard escape**: `WX_IDLE_GATE` (2s for text-writing actions) and `WX_IDLE_MAX_WAIT` (300s, then the job goes back in the queue). On machines with a noisy HID device `GetLastInputInfo` may never report idle (measured here: 12 seconds of sampling never exceeded 0.3s), so the gate must never wait forever.
- **Foreground gate**: before injecting input, the service checks that `GetForegroundWindow()` is the WeChat main window. If it is not, the write is abandoned — that is how text used to end up in the wrong window.

## Measured (WeChat 4.1.13.65 / Windows 11)

| Action | Total | Foreground time |
|---|---|---|
| `check` | 3.2s | 1.9–2.8s |
| `send --confirm` | 4.9s | 4.5s |
| `draft` | 8.6s | 8.2s |

“Foreground time” is how long WeChat sat in front; focus is handed back afterwards. Every result prints this number.

## Limitations (read first)

- **It cannot be fully invisible.** WeChat 4.x only exposes the input box in the accessibility tree while its window is in the foreground; when it is not, the tree is an empty shell (`chat_input` / `search_box` are `None`). There is no silent background write — only a short foreground window.
- The WeChat window must exist (it is restored from minimized/tray first; a trayed window reports `MainWindowHandle = 0`, so the script enumerates windows per pid and uses `ShowWindow` — never `Start-Process Weixin.exe`, which would open a second instance).
- **Only tested against WeChat 4.x** (4.1.13.65 here). A major upgrade may change UIA class names or automation ids; `wx svc log` will show the symptoms.
- No group broadcasting, no scheduling, no hook/protocol-level tricks.
- The upstream automation library (`wechatauto-replica`) is a third-party project. **This repo does not trust its return values** — every verdict comes from this project's own readback, pixel check or OCR.

## Repository layout

```
pi-wechat-draft/
├── SKILL.md               # procedure + full pitfall list for agents (useful for humans too)
├── README.md / README.en.md
├── install.ps1            # private venv + deps + self-test
├── scripts/
│   ├── wx.sh              # client (self-locating: code in scripts/, state in the repo root)
│   ├── wx_service.py      # resident service: queue, idle gate, foreground, focus hand-back
│   ├── wx-ocr.ps1         # screenshot + Windows.Media.Ocr with in-window coordinates
│   ├── wx_task.py         # thin wrapper over the upstream library
│   ├── wx.cmd             # cmd / PowerShell entry point
│   └── selfcheck.py       # syntax / line endings / ASCII / hygiene scan (also run in CI)
├── .github/workflows/selfcheck.yml
├── LICENSE                # MIT
└── .gitignore, .gitattributes
```

## Use as a pi skill

The repository root *is* a skill directory:

```bash
git clone https://github.com/yonghaili/pi-wechat-draft.git ~/.pi/agent/skills/wechat-draft
```

## Contributing / self-check

```bash
python scripts/selfcheck.py                       # bash -n, ast, LF, ASCII, hygiene scan
EXTRA_DENY='your-name|a-contact' python scripts/selfcheck.py   # add private tokens locally
```

The checker deliberately contains no personal names, so it can never become the leak. The GitHub Actions config lives in [`ci/selfcheck.yml`](ci/selfcheck.yml) rather than `.github/workflows/` because pushing a workflow file needs a token with the `workflow` scope — see [`ci/README.md`](ci/README.md) for the two ways to enable it.

## License

MIT. Check that using it complies with the rules that apply to you; this tool drives your own WeChat client, so try it on a non-critical conversation first.
