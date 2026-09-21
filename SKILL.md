---
name: wechat-draft
description: Fill a draft into the WeChat 4.x desktop input box on Windows and never send it on your own — UIA ValuePattern write with character-exact readback, a resident background service, focus hand-back to whatever the user was doing. Use when asked to 给某人发微信 / 把这段话放进微信输入框 / 撤掉草稿 / 核对刚发出的消息, or when a WeChat desktop UI operation must run without disturbing the user.
---

# WeChat draft (fill, don't send)

驱动微信 4.x 桌面客户端把文字填进输入框，**默认不发送**。所有 UI 操作由常驻后台服务串行执行，每次写入都用另一条链路回读校验，用完把焦点还回使用者原来的窗口。

## When to Use

- 使用者要求「给某人发一条微信」「把这段放进输入框」「撤掉草稿」「核对刚发出去的那条」。
- 需要把重复的微信 UI 操作做成可审计、不打扰使用者的流程。
- 不适用：群发、定时任务、hook/协议类方案、非 Windows、非微信桌面客户端。

## Safety Model

1. **默认只填不发**：`wx draft` 只写入输入框并回一张待确认卡片。使用者自己按回车，或对**那条具体草稿**明确说「发」之后才执行 `wx send ... --confirm`。
2. **硬闸**：`send` 缺少 `--confirm` 时以退出码 9 拒绝执行，不要为了跑通而绕过它。
3. **对象闸门**：写入前后都要能确认会话对象。多候选名字必须让使用者确认，不得自行挑选。
4. **前台闸门**：注入输入前确认 `GetForegroundWindow()` 是微信主窗；不是就放弃本次输入（否则字会打进别的窗口）。
5. 不转发、不加好友、不改微信设置或标签。

## Setup

```powershell
git clone https://github.com/yonghaili/pi-wechat-draft.git
cd pi-wechat-draft
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1   # 建 venv + 装依赖 + 自检
```

可选：`WX_READER` 指向一个只读微信读取器（能读本地聊天库的 CLI），用于「另链路回读原文」这层证据与口语名字解析。不配置时功能完整，只是少这层证据。

**静默档（建议开）**：`<WX_DIR>/wx-service.env`（每行 `KEY=VALUE`，改完 `wx svc stop && wx svc start`）：`WX_IDLE_GATE=20`（写字动作只在使用者停手 ≥20s 后才动手；默认 2s 会打断他）、`WX_IDLE_GATE_RO=0.5`（只读动作门槛）、`WX_IDLE_FALLBACK=600` / `WX_IDLE_FALLBACK_MIN=5`（等满 10 分钟无长空闲就降级，避免任务饿死）、`WX_IDLE_MAX_WAIT=60`（配合 `--now` 插队）、`WX_RESTORE_BLOCKERS=1`（还原被库最小化的遮挡窗口）。需要立即执行：`wx draft/send ... --now`。

## Procedure

1. **确认目标**。口语称呼先用 `wx resolve "<关键词>"` 解析；返回多个候选时**必须问**，不要自己挑（实测同一称呼曾解析出 10 个候选，其中 9 个是零聊天记录的重名联系人）。挑选依据可以是「哪个有近期聊天记录」——用只读读取器逐个查证，而不是猜。
2. **核对原文**。`wx context "<会话名>" 5`（只读，不碰界面）。看最后一条实质消息是谁发的、在聊什么。涉及时间的草稿（「这就过去」「明天见」）放久了会失效，对不上就重新确认，不要照字面发。
3. **默认只填**。`wx draft "<会话名>" "<文字>"`。服务会：等一次真正的空闲 → 恢复微信窗口（只碰微信自己）→ Alt 键解锁切前台 → 打开会话 → UIA `ValuePattern` 写入 → **逐字回读** → 取消置顶、还原前台窗口与光标 → 写结果文件。
4. **交付待确认卡片**：会话名、草稿全文、字数、写入方式、控件回读是否一致、**本次占用前台多少秒**。然后明确写「核对无误后自己按回车；或回一句『发』由我发送」。**此时绝不发送。**
5. **发送（仅在被明确要求时）**。内容与已确认草稿逐字一致才执行 `wx send "<会话名>" "<文字>" --confirm`。使用者只说「发」而内容有变化时，先复述新内容再发。
6. **复核分三层**（`wx send` 内置）：① 配了 `WX_READER` → 轮询回读原文，比对时间/方向/文本；② 没配 → 读 UIA 输入框是否已被微信清空；③ 再退到 OCR 读屏。看到失败**不要重发**，先确认是否已发出。
7. **撤稿**：`wx clear "<会话名>"`（内部带重试 + 回读确认）。撤掉草稿比撤回消息可靠。
8. **误发补救**：上游库有 `Chat.RecallLastMessage`，两分钟内可撤回。

## Pitfalls

每条都是实测结论，含判据。

**抢前台：只能用 Alt 键解锁法。** 同一台机器、同一起点连测四种方法：上游库 `bring_to_front(keep_topmost=True)` 抢不到（返回 False）；`bring_to_front(keep_topmost=False)` 从来没成功过；`SwitchToThisWindow` 抢不到；**`keybd_event(VK_MENU down/up)` 紧接 `SetForegroundWindow` → 10/10 成功**。原理：Windows 只允许「最后一个输入事件来自本进程」的进程修改前台。抢到之后要把焦点还给使用者原来的窗口（同样用 Alt 法）。

**上游库会最小化使用者正在用的窗口，而且自己从不还原。** `WeChatGUI._minimize_blockers()`（`guia.py:915`）把所有与微信主窗重叠的其它顶层窗口 `ShowWindow(h, 6)` 最小化。这对库本身是必需的——它用物理点击，窗口被盖住时点击会被覆盖层接走；但它**从不还原**。注意：**`guia.open_chat()` 内部自己会调 `ensure_visible()`**（`guia.py:1306`），所以「调用方不用它的 ensure_visible」并不能避免这件事，必须打补丁。本工具的做法（`wx_service.py` 的 `install_library_patches()` / `restore_library_minimized()`）：保留最小化行为，但把被它最小化的窗口记下来，任务收尾时用库自己的 `_restore_keep_maximize(u32, hwnd)` 还原（会保留最大化状态）；已经不是最小化态的窗口不碰（使用者自己动过就不硬抢）。恢复微信**自己**只用 `ShowWindow(hwnd, SW_SHOW=5 / SW_RESTORE=9)`。

**微信不在前台时，它的无障碍树是空壳。** `chat_input` / `search_box` / `session_list` 全是 `None`，`describe_layout().anchors` 全空。所以「不切前台静默写入」不存在，别在这条路上浪费时间；先切前台再取控件。

**首选写入方式是 UIA `ValuePattern`（需前台）。** 微信在前台时 `chat_input_field` 是标准 `EditControl`：`GetValuePattern()` 可写可读，`SetValue` 之后回读与写入逐字一致；而且**控件 Name 就是当前会话名**，可以先校验对象再写。这条路径不点击鼠标、不用剪贴板、不注入键盘事件。

**上游库的「点击 + 剪贴板」路径会假成功。** 实测出现过：库报 `filled: True`，而输入框其实是空的。根因有二：① 库内部那套输入框坐标 `(214,675,975,788)` 与真实屏幕位置对不上（真实输入行在窗口图的约 0.63H 处，即 y≈551），点击可能打偏、焦点没进输入框；② 它自己的"有没有字"检查取样区域高达 200px，会把上方聊天内容误判成输入框里的字。所以：只用它作回退路径，且**回退路径一律用 OCR 读实文字**，不采信它的布尔返回值。

**库的其它返回值也不可信**：`click_send: False` 但消息其实已发出；`_verify_sent` 假阴性（数据库落库延迟、venv 内没有解密密钥）；`clear` 报「输入框仍有内容: True」但也可能真的没清掉（实测第一次不生效、第二次才干净）。结论：**判定只走外部证据**——UIA 回读、像素检测、OCR 读屏、独立读取器回读。

**像素墨迹检测比 OCR 快两个数量级**（约 0.1s vs 每次启动 PowerShell + WinRT OCR 2~4s）。取样区域用窗口比例：输入行约 y 0.60~0.67H、x 0.40~0.97W；判定阈值取"深色像素点数 > 25"。上游库的 `_input_box_has_text` 区域偏高，容易误判，不要用。

**`GetLastInputInfo` 空闲闸门不可靠，必须允许超时。** 实测某台机器 12 秒采样空闲值长期只在 0.0~0.3s（疑似 HID 设备持续产生输入事件），但同一台机器别的时刻又能看到 9.2s 的空闲窗口。所以：闸门可以用，但**等不到就把任务放回队列**（默认上限 300s），客户端也必须非阻塞（默认 45s 后回「已排队」而不是失败）。

**慢活要排在抢焦点之前。** 读取原文、解析窗口尺寸这类不碰界面的步骤先做完，再抢前台；否则占用前台的时长会被无关步骤拉长。同理会话/引擎必须进程内常驻复用：冷启一次约 3.8s，常驻后约 0.8s。

**Windows OCR 对 17px 中文丢字严重**（「微信」→「微亻言」、「洗澡」→「氵先澡」、全角「？」→「7」）。OCR 只能定位与确认"有没有墨迹"，**不能当转写核对**；反过来说，OCR 认不出某个字不代表它不在。

**不要用分步 SendKeys。** 微信搜索面板会自动关闭，跨工具调用注入按键必然失效，字会落进当前会话的输入框。要打字就在同一次调用里完成，或者用本仓库的队列/服务。

**托盘与最小化。** 微信窗口藏进托盘时 `Get-Process Weixin` 拿到的 `MainWindowHandle` 是 0（同一进程名下常见 5 个进程，只有非 0 的那个是主窗），而且 Qt 主窗的 `IsWindowVisible` 会变成 False。恢复：用 `EnumWindows` 按 pid 找 `Qt*QWindowIcon` 类名的顶层窗口，再 `ShowWindow(hwnd, 5)`。**不要** `Start-Process Weixin.exe`——那会多开一个实例并弹登录窗。

**`act_ui` 的坐标系是观察图像空间**（通常 900x790），不是物理像素也不是屏幕坐标；直接传物理坐标会被拒（outside the latest look image bounds）。同一 `stateId` 每经一次新观察就失效。

## 发送前的敏感内容闸门

**第一层·离线敏感内容闸门（确定性，不联网）**：`wx send` 在碰鼠标键盘之前先扫文本，命中「16-19 位长数字 / 验证码 / 密码密钥 / 证件账号 / 转账付款金额」直接拒发（`method: blocked_high_risk`，实测占用前台 **0.0s**）。普通金额与时间表述**不拦**（“明天上午9点见”照发）。确实要照发：`--allow-high-risk`（日志留痕）。

⚠️ **这层只覆盖 `wx send`。** 使用者自己在输入框按回车发送不经过任何服务端检查；所以草稿阶段的逐字核对不能省。

## Verification

1. **填入后**：结果里 `写入方式: value_pattern`，`控件回读` 与草稿一致，`控件所属会话名` == 目标会话；同时聊天区**不应**出现新气泡（`wx context` 最后一条仍是旧消息）。
2. **清空后**：结果里 `输入行：（空）`，或放大裁剪 OCR（`wx-ocr.ps1 -Crop "<x>,<y>,<w>,<h>" -Scale 3`）只看到聊天区时间戳。
3. **发送后**：`verified: reader`（配了 `WX_READER`）或 `uia_input_cleared`，随后用独立的 `wx context "<会话名>"` 再确认一次出现 `｜<本人昵称>：<内容>`。
4. **干扰面**：结果里的「占用前台」秒数在个位数内（本机实测 check 1.9~2.8s、send 4.5s、draft 8.2s）；日志里不应出现「焦点归还失败」。
5. **服务健康**：`wx svc status` 显示运行中、队列为 0；`wx svc log` 里每次任务都有「领到任务 → 分阶段耗时 → 完成」三段。

## Troubleshooting

| 症状 | 先看什么 |
|---|---|
| 结果里 `微信未能切到前台` | 使用者正在操作别的窗口。任务会重排；不要改成"强行置顶"绕过（那会把微信悬浮在所有窗口之上） |
| `未探测到输入框` / `chat_input` 为 None | 微信不在前台，或窗口被最小化/隐藏 → `wx svc log` 看 `window=` 字段 |
| 库报 filled 但 `wx check` 显示输入行为空 | 回退路径被用了。检查 `写入方式` 字段；正常应为 `value_pattern` |
| 任务一直「已排队」 | 空闲闸门等不到空闲窗口（HID 噪声）→ 调小 `WX_IDLE_GATE`，或让使用者在不用电脑时再提交 |
| 服务起不来/无响应 | `wx svc stop` 后 `wx svc start`；看 `<repo>/logs/wx-service-YYYYMMDD.log`；服务启动时会把遗留的 `.working` 任务放回队列 |
