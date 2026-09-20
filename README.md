# pi-wechat-draft

**把草稿填进微信输入框，但不替你按发送。** Windows + 微信 4.x 的桌面自动化，带常驻后台服务、逐字回读校验和一套"不打扰使用者"的执行纪律。

> English: A Windows-only toolkit that fills drafts into the WeChat 4.x desktop input box and **never sends on its own**. It runs through a resident background service with a file-based job queue, verifies every write by reading the UI Automation value back character-for-character, and returns focus to whatever window you were using. Ships as a pi skill plus standalone CLI.

---

## 它和"又一个 wxauto 脚本"的区别

微信桌面自动化本身不难写，难的是**在别人的机器上、在别人正在用电脑的时候不出事**。这个仓库的价值主要是踩过的坑：

- **填入方式用 UIA `ValuePattern` 直写**，不用「点击输入框 + 剪贴板 Ctrl+V」。后者依赖坐标点击，实测在同一台机器上会出现"库报填写成功、输入框其实是空的"——因为它的坐标空间和真实屏幕位置对不上，而它自己的"有字没字"检查又会被上方聊天内容误判。
- **写入后逐字回读**：`SetValue` 之后再读一遍控件值，和要写的字逐字比对；还能读到控件自带的会话名（`chat_input_field` 的 Name 就是当前聊天对象），**先确认对象再写**。
- **不最小化任何窗口**：上游库自带的 `ensure_visible()` 会把与微信重叠的窗口全部最小化掉，等于把使用者的工作窗口收走。这里只用 `ShowWindow` 恢复微信自己。
- **抢前台用 Alt 键解锁法**：同一台机器四种方法连测，只有「合成一个 Alt 键事件 + `SetForegroundWindow`」能稳定抢到（10/10）；上游库的置顶法、`SwitchToThisWindow` 都失败。
- **常驻服务 + 文件队列**：微信 UI 操作永远只有一个执行者，不会两个请求同时抢微信；UI Automation 引擎常驻复用（就绪 0.8s，而不是每次冷启 3.8s）。
- **默认只填不发**：`send` 需要显式 `--confirm`，否则以退出码 9 拒发。

完整清单（含每条坑的现象、根因、验证方式）在 [SKILL.md](SKILL.md)。

## 安全模型

| 动作 | 默认行为 |
|---|---|
| `wx draft` | 把草稿填进输入框，**不发送**，回一张待确认卡片 |
| `wx send ... --confirm` | 真的发出（缺 `--confirm` 直接拒发） |
| `wx clear` | 清空输入框（撤掉草稿） |
| 其它 | 不转发、不加好友、不改微信设置/标签 |

设计前提：**发消息这件事，最后一按应该由人来做**。所以 agent 只填，你看一眼自己按回车；确实要代发时，用 `send --confirm` 并且以另一条链路回读确认。

## 环境要求

- Windows 10 / 11
- 微信 **4.x** 桌面版（`Weixin.exe`），已登录
- Python 3.10+
- Git Bash（命令行入口 `wx` 用它跑 shell 脚本；也可直接用 `scripts\wx.cmd`）

## 安装

```powershell
git clone https://github.com/yonghaili/pi-wechat-draft.git
cd pi-wechat-draft
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` 会在仓库目录里建一个独立 venv 并装好依赖（不动全局 Python），最后打印自检结果。

可选：如果你想让它用**另一条独立链路**回读聊天记录做交叉验证（而不只依赖 UIA 回读），把一个只读微信读取器指给它：

```powershell
setx WX_READER "C:\tools\reader\rion_wechat_reader.py"
```

不配置也完全能用——只是少一层证据，`wx resolve`（口语名字解析）和原文回读会被跳过。

把客户端放进 PATH（Git Bash）：

```bash
echo 'export PATH="'"$PWD"'/scripts:$PATH"' >> ~/.bashrc
```

## 用法

```
wx draft  "<会话名>" "<文字>"           # 默认：只填不发
wx send   "<会话名>" "<文字>" --confirm # 真发（需你明确确认过）
wx check  "<会话名>"                    # 开会话 + 读屏 + 原文核对
wx clear  "<会话名>"                    # 清空输入框（撤稿）
wx context "<会话名>" [N]               # 只看最近 N 条原文（不碰界面）
wx resolve "<关键词>"                   # 口语叫法 → 候选会话名（需 WX_READER）
wx result <任务号>                      # 回查排队中的任务
wx svc start|stop|status|log            # 后台服务
```

一次典型来回：

```bash
$ wx draft "示例联系人" "明天上午我去送你吧"
任务号：20260920185527-486-17695
结果：成功
动作：draft  会话：示例联系人  用时：8.6s  窗口：ok
占用前台：8.2s（干完已把焦点还回你原来的窗口）
写入方式：value_pattern
控件回读：'明天上午我去送你吧'

════════ 待你确认（已填入，未发送）════════
会话：示例联系人
草稿：明天上午我去送你吧
字数：9
控件逐字回读：一致
控件所属会话名：示例联系人
下一步：切到微信核对无误后按回车发送；或回一句「发」，我来发送。
        要撤掉草稿就说「清空」，我跑 wx clear "示例联系人"。
════════════════════════════════
```

## 它是怎么工作的

```
你的命令 / agent
      │  写 job 文件
      ▼
<repo>/queue/<seq>.<id>.job
      │  串行领取（改名 .working，保证同一时刻只有一个执行者）
      ▼
wx_service.py（隐藏的常驻进程）
      │  ① 等一次真正的空闲（可配，见下）
      │  ② 恢复微信窗口（只碰微信自己，不最小化别人的窗口）
      │  ③ Alt 键解锁 → 切前台（UIA 树只有在前台才会物化）
      │  ④ 打开会话 → UIA ValuePattern 写入 → 逐字回读
      │  ⑤ 取消置顶 / 还原前台窗口与光标
      ▼
<repo>/queue/<id>.result  ← 客户端读取并打印证据
```

几个刻意的设计：

- **队列是文件**，不是 socket：可审计（`queue/*.result` 留着每次的证据）、可手工检查、服务挂了也不丢任务；服务启动时会把上次遗留的 `.working` 放回队列。
- **非阻塞客户端**：默认最多等 45s，超时就回「已排队，任务号 xxx」而不是失败——任务留在后台继续等一个合适的时机。
- **一个服务在进程列表里会显示成两个进程，这是正常的**：Windows 上 venv 的 `pythonw.exe` 是个「重定向器」，它会再起一个真正的解释器子进程，两者命令行完全相同（实测：父 6MB / 子 19MB 常驻）。PID 文件记的是真正在跑主循环的那个；`wx svc stop`（内部 `svc_sweep`）会把两者一起收掉。排查时别把它们当成“跑重复实例”。
- **空闲闸门可配**：`WX_IDLE_GATE`（写字的动作默认 2s）、`WX_IDLE_MAX_WAIT`（默认 300s，等不到就放回队列）。注意 `GetLastInputInfo` 在有 HID 噪声的机器上可能长期不空闲（本机实测 12 秒采样最长只有 0.3s），所以闸门**绝不允许无限等**。
- **前台闸门**：每次注入输入前检查 `GetForegroundWindow()` 是不是微信主窗，不是就放弃本次输入——防止字被打进别的窗口。

## 实测数据（微信 4.1.13.65 / Windows 11）

| 动作 | 总耗时 | 占用前台 |
|---|---|---|
| `check` | 3.2s | 1.9–2.8s |
| `send --confirm` | 4.9s | 4.5s |
| `draft`（冷启后首单会更慢） | 8.6s | 8.2s |

「占用前台」是微信停留在最前面的时长（操作完成后会把焦点还给你原来的窗口）。所有结果里都会打印这个数字。

## 局限（先说清楚）

- **做不到完全无感。** 微信 4.x 的输入框只有在窗口是前台时才出现在无障碍树里；窗口不在前台时 UIA 树是空壳（`chat_input` / `search_box` 全是 `None`），所以"不切前台静默写入"这条路不存在，只能把占用时间压短。
- **必须要微信窗口可见**（最小化/托盘状态会被先恢复；托盘时 `Get-Process` 的 `MainWindowHandle` 是 0，脚本用 `EnumWindows` + `ShowWindow` 恢复，不会多开实例）。
- **只测过微信 4.x**（本机 4.1.13.65）。微信大版本升级后 UIA 类名/自动化 ID 可能变，`wx svc log` 会给出症状。
- **不做群发、不做定时任务、不碰 hook/协议层。**
- 依赖的上游库 `wechatauto-replica` 是第三方项目；本仓库对它的**返回值一律不采信**，所有判定都走自己的回读/像素检测。

## 目录结构

```
pi-wechat-draft/
├── SKILL.md               # 给 agent 用的流程与完整踩坑清单（也给人类读）
├── README.md
├── install.ps1            # 建 venv + 装依赖 + 自检
├── LICENSE
├── .gitignore             # venv/ queue/ logs/ 都是运行期产物，不入库
└── scripts/
    ├── wx.sh              # 客户端（自定位：脚本在 scripts/，状态在仓库根）
    ├── wx_service.py      # 常驻后台服务：队列 + 空闲闸门 + 抢前台 + 还原
    ├── wx-ocr.ps1         # 截屏 + Windows.Media.Ocr，输出每行文字及窗口内坐标
    ├── wx_task.py         # 上游库的薄封装：probe / fill / send / clear
    └── wx.cmd             # cmd / PowerShell 入口
```

## 作为 pi skill 使用

仓库根就是 skill 目录，直接放进 skills 路径即可：

```bash
git clone https://github.com/yonghaili/pi-wechat-draft.git ~/.pi/agent/skills/wechat-draft
```

`SKILL.md` 里写的是给 agent 的固定流程（先解析对象 → 回读原文 → 只填 → 交付待确认卡片 → 使用者说「发」才发 → 分三层复核）以及每条坑的判据。

## License

MIT。使用前请自行确认符合你所在环境的规定；这个工具会驱动你的微信客户端界面，请先在非重要会话上试跑。
